"""Offline source checks over the restored context (spec §3.2, §3.3, §1.3 d-e)."""

import fnmatch
import json
import os
import posixpath
import shlex
from pathlib import Path

from deployer.reproduce.dockerfile import Instruction, ParsedDockerfile
from deployer.reproduce.ignore import IgnoreRules, excluded_by, glob_to_regex
from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence

LISTING_REF = "../../source.json#tree"
_REMOTE_PREFIXES = ("http://", "https://", "git@", "git://")
_HEREDOC_REASON = "heredoc source not modelled"
_UNMODELLED_FLAGS = ("--parents", "--exclude")


def copy_source_checks(
    parsed: ParsedDockerfile, context: Path, dockerfile: str, rules: IgnoreRules
) -> list[ReproductionCheck]:
    """Every local COPY/ADD source against ``context`` minus ignored paths."""
    if rules.unsupported is not None:
        return [
            ReproductionCheck(
                check_id="copy_sources",
                status="skipped",
                reason=f"ignore pattern not modelled: {rules.unsupported}",
            )
        ]
    files = _context_paths(context)
    findings: list[ReproductionCheck] = []
    skipped: list[ReproductionCheck] = []
    checked = 0  # a pass needs at least one source actually checked
    for inst in parsed.instructions:
        if inst.keyword not in ("COPY", "ADD"):
            continue
        sources, why_skipped = _sources(inst)
        if why_skipped is not None:
            skipped.append(_skip(inst, why_skipped))
            continue
        for raw in sources:
            if inst.keyword == "ADD" and raw.startswith(_REMOTE_PREFIXES):
                skipped.append(_skip(inst, f"remote ADD source {raw}"))
                continue
            source = _norm(raw)
            if _has_unmodelled_chars(source):
                skipped.append(_skip(inst, f"source pattern not modelled: {source}"))
                continue
            checked += 1
            findings.extend(
                _check_source(inst, source, files, context, dockerfile, rules)
            )
    if not findings and checked:
        findings = [ReproductionCheck(check_id="copy_sources", status="passed")]
    return findings + skipped


def from_ref_checks(parsed: ParsedDockerfile) -> list[ReproductionCheck]:
    """Every ``--from=`` recorded as a stage or an external image (§3.3)."""
    stages = _stage_names(parsed)
    out: list[ReproductionCheck] = []
    for inst in parsed.instructions:
        for ref in _from_flags(inst):
            finding = (
                f"--from={ref} resolved to a stage of this Dockerfile"
                if ref.lower() in stages
                else f"--from={ref} is an external image dependency"
            )
            out.append(
                ReproductionCheck(
                    check_id="from_ref", status="observation", finding=finding
                )
            )
    return out


def external_images(parsed: ParsedDockerfile) -> list[str]:
    """Images the build pulls: FROM images and ``--from=<image>``, not stages."""
    stages = _stage_names(parsed)
    images: list[str] = []
    for inst in parsed.instructions:
        if inst.keyword == "FROM":
            tokens = [t for t in inst.args.split() if not t.startswith("--")]
            if tokens and tokens[0].lower() not in stages and tokens[0] != "scratch":
                images.append(tokens[0])
        images.extend(r for r in _from_flags(inst) if r.lower() not in stages)
    return list(dict.fromkeys(images))


def context_conditions(parsed: ParsedDockerfile) -> list[str]:
    """Exactness conditions (d) and (e) of §1.3, as unmet-condition strings.

    (d) is deliberately narrow: this slice never tries to prove that the
    ignore file keeps ``.git`` out of the context — each attempt to reason
    about exclusions and re-inclusions produced a new counter-example. Any
    local COPY/ADD source that may reach ``.git`` (the context root, a first
    path segment that can match ``.git``, or a form this slice cannot read)
    is an unmet condition, so the restoration is an approximation.
    """
    unmet: list[str] = []
    for inst in parsed.instructions:
        if inst.keyword in ("COPY", "ADD") and not _from_flags(inst):
            unmet.extend(_git_conditions(inst))
        if inst.keyword == "RUN" and any(
            t.startswith("--mount") for t in inst.args.split()
        ):
            unmet.append(f"RUN --mount at line {inst.first_line}")
    return unmet


def _git_conditions(inst: Instruction) -> list[str]:
    """Unmet (d) conditions of one local COPY/ADD."""
    where = f"{inst.keyword} at line {inst.first_line}"
    sources, why = _sources(inst)
    if why is not None:
        if why == _HEREDOC_REASON:
            return []  # inline content: nothing is read from the context
        return [f".git exclusion not proven: {where} ({why})"]
    return [
        f".git exclusion not proven: {inst.keyword} {source} at line {inst.first_line}"
        for source in (_norm(s) for s in sources)
        if _may_reach_git(source)
    ]


def _may_reach_git(source: str) -> bool:
    """Whether a COPY/ADD source can name ``.git`` or a path under it.

    True for the context root, and for any source whose first path segment
    can match ``.git`` — literally or as a glob (``*``, ``**``, ``?``,
    ``[...]``; ``fnmatch`` lets ``*`` match a leading dot, as BuildKit does).
    """
    first = source.split("/", 1)[0]
    return source == "." or fnmatch.fnmatchcase(".git", first)


def _sources(inst: Instruction) -> tuple[list[str], str | None]:
    args = inst.args.strip()
    if "<<" in args:
        return [], _HEREDOC_REASON
    if args.startswith("["):
        try:
            tokens = [str(t) for t in json.loads(args)]
        except json.JSONDecodeError:
            return [], "unparseable JSON form"
    else:
        try:
            tokens = shlex.split(args)
        except ValueError:
            return [], "unparseable quoting"
    flags = [t for t in tokens if t.startswith("--")]
    rest = [t for t in tokens if not t.startswith("--")]
    if any(f.startswith("--from") for f in flags):
        return [], "--from source is checked in its stage or image, not the context"
    if any(f.startswith(_UNMODELLED_FLAGS) for f in flags):
        return [], "flag not modelled: " + ", ".join(flags)
    if len(rest) < 2:
        return [], "no source/destination pair"
    return rest[:-1], None


def _norm(source: str) -> str:
    """Context-relative form of a local source; ``.`` for the root.

    BuildKit clamps a COPY/ADD source to the context root, the way a leading
    ``..`` in a URL path is clamped to the site root: prefixing a leading
    ``/`` before calling ``posixpath.normpath`` makes any ``..`` collapse
    against that synthetic root instead of resolving relative to the host's
    real filesystem, which is what ``os.path.normpath`` would do for a
    source such as ``../../x``.
    """
    return posixpath.normpath("/" + source).lstrip("/") or "."


def _has_unmodelled_chars(source: str) -> bool:
    """Character classes and escapes are a source form this slice doesn't model."""
    return "[" in source or "\\" in source


def _check_source(
    inst: Instruction,
    source: str,
    files: list[str],
    context: Path,
    dockerfile: str,
    rules: IgnoreRules,
) -> list[ReproductionCheck]:
    location = Location(file=dockerfile, lines=(inst.first_line, inst.last_line))
    ignore_ev = ReproEvidence(kind="ignore_file", path=rules.file)
    if any(ch in source for ch in "*?"):
        regex = glob_to_regex(source)
        matched = [f for f in files if regex.match(f) and excluded_by(rules, f) is None]
        if matched:
            return []
        return [
            ReproductionCheck(
                check_id="copy_sources",
                status="failed",
                finding=f"source {source} matches nothing in the context",
                location=location,
                evidence=[
                    ReproEvidence(kind="path_absent", path=source, listing=LISTING_REF),
                    ignore_ev,
                ],
            )
        ]
    if source != "." and not (context / source).exists():
        return [
            ReproductionCheck(
                check_id="copy_sources",
                status="failed",
                finding=f"source {source} absent from the context",
                location=location,
                evidence=[
                    ReproEvidence(kind="path_absent", path=source, listing=LISTING_REF),
                    ignore_ev,
                ],
            )
        ]
    line = excluded_by(rules, source) if source != "." else None
    if line is None:
        return []
    return [
        ReproductionCheck(
            check_id="copy_sources",
            status="failed",
            finding=f"source {source} excluded by {rules.file} line {line}",
            location=location,
            evidence=[
                ReproEvidence(kind="ignore_file", path=rules.file, text=f"line {line}")
            ],
        )
    ]


def _skip(inst: Instruction, reason: str) -> ReproductionCheck:
    return ReproductionCheck(
        check_id="copy_sources",
        status="skipped",
        reason=f"{inst.keyword} at line {inst.first_line}: {reason}",
    )


def _stage_names(parsed: ParsedDockerfile) -> set[str]:
    names: set[str] = set()
    for inst in parsed.instructions:
        tokens = inst.args.split()
        if inst.keyword == "FROM" and len(tokens) >= 3 and tokens[-2].upper() == "AS":
            names.add(tokens[-1].lower())
    return names


def _from_flags(inst: Instruction) -> list[str]:
    if inst.keyword not in ("COPY", "ADD"):
        return []
    return [t.split("=", 1)[1] for t in inst.args.split() if t.startswith("--from=")]


def _context_paths(context: Path) -> list[str]:
    out: list[str] = []
    for root, dirs, names in os.walk(context):
        rel_root = Path(root).relative_to(context)
        for name in [*dirs, *names]:
            out.append((rel_root / name).as_posix())
    return out
