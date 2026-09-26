"""The pure decision (A §6.1): verified facts → the ``admission`` section.

No I/O, no subprocess, no clock, no environment: every input arrives in
:class:`VerifiedFacts`, prepared by the preparation layer. Every unmet reason
reachable without inventing evidence is collected, one entry per condition
(A §1): (1) ownership (A §2), (2) the defect (A §3), (3) the link (A §4). A
check that needs a fact which is not established — a defect class, the
instruction, the verified snapshot — is not run, so no reason claims more than
the facts show.

The template matchers (``templates``) deliberately skip the binding rules; this
module enforces them: the CI span equals R's recorded CI instruction, a log
matching rows of both classes is refused, the §3.1 form is checked, the row
object is bound to one source of that instruction and to the proven-absent
path, ``from-args/podman`` needs exactly one bad FROM, and both sides must match.
"""

import json
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from deployer.admission import templates
from deployer.admission.model import (
    AdmissionSection,
    Binding,
    Defect,
    DefectClass,
    DifferenceDecision,
    Link,
    Ownership,
    SideLink,
    Unmet,
    ownership_from_facts,
)
from deployer.admission.ownership import OwnershipFacts
from deployer.admission.templates import (
    AMBIGUOUS,
    CopyMatch,
    FromMatch,
    Row,
    match_copy_ci,
    match_copy_local,
    match_from_ci,
    match_from_local,
)
from deployer.provenance.model import TreeRow
from deployer.reproduce.checks import is_modelled_source
from deployer.reproduce.compare import REQUIRED_DIMENSIONS
from deployer.reproduce.dockerfile import (
    Instruction,
    ParsedDockerfile,
    normalise,
    opens_heredoc,
    syntax_checks,
    unread_reason,
)
from deployer.reproduce.model import ReproductionCheck, ReproductionSection

Hashes = tuple[tuple[str | None, str | None], tuple[str | None, str | None]]
Match = CopyMatch | FromMatch
Condition = Literal[1, 2, 3]

LINKED_STATES = ("reproduced", "reproduced_with_differences")
"""R §7.3 states that bind both sides to the same span (A §4.3)."""
TOLERATED_UNKNOWN = ("host_arch", "base_image_digests")
"""Dimensions tolerated as ``unknown`` once the rows prove the defect."""
ROW_BACKEND = {"docker": "buildkit", "podman": "podman"}
"""R's backend value → the template table's backend."""
LINK_MODES = {"120000": "symlink", "160000": "submodule"}
"""Tree modes that make an ancestor path unreadable as a directory (A §3.1)."""
EXCLUDED_FLAGS = ("--from", "--parents", "--exclude")
"""COPY/ADD flags outside A §3.1's form (``--from``; R leaves the others
unmodelled)."""
REMOTE_PREFIXES = ("http://", "https://", "git@", "git://")
GLOB_CHARS = frozenset("*?[")
_ABSENT_RE = re.compile(r"source (\S+) absent from the context")
_LEADING_FLAGS_RE = re.compile(r"((?:--\S+\s+)*)")


@dataclass(frozen=True)
class VerifiedFacts:
    """Everything the decision reads, verified by the preparation layer.

    ``ignore_hashes`` is ``((ci_path, ci_sha256), (local_path,
    local_sha256))`` of the effective ignore files; ``head_listing`` is the
    ``head_sha`` tree listing R stored in ``source.json``, and
    ``head_listing_complete`` is ``False`` when R marked it truncated.
    """

    binding: Binding
    ownership: OwnershipFacts
    reproduction: ReproductionSection
    parsed: ParsedDockerfile
    head_listing: list[TreeRow]
    head_listing_complete: bool
    ci_text: str
    ci_evidence_file: str
    local_stdout: str
    local_stderr: str
    ignore_hashes: Hashes
    syntax_directive_ci: str | None
    syntax_directive_local: str | None


@dataclass(frozen=True)
class _Candidate:
    """A defect R reported: its class, file, span and (copy only) path."""

    cls: DefectClass
    file: str
    lines: tuple[int, int]
    path: str | None


@dataclass(frozen=True)
class _Subject:
    """A candidate bound to the instruction at its span."""

    candidate: _Candidate
    instruction: Instruction


@dataclass(frozen=True)
class _Side:
    """One side's matched row and the object it bound (copy only)."""

    row: Row
    match: Match
    object: str | None


def decide(facts: VerifiedFacts) -> AdmissionSection:
    """Decide ``admitted`` or ``insufficient_grounds`` (A §1) from ``facts``.

    Pure: the same facts always give the same section.
    """
    ownership, owner_reasons = _ownership(facts.ownership)
    unmet: dict[Condition, list[str]] = {1: owner_reasons}
    repro = facts.reproduction
    if repro.status != "attempted" or repro.comparison is None:
        unmet[2] = [f"reproduction {repro.status}: no defect evidence"]
        unmet[3] = [f"reproduction {repro.status}: no link evidence"]
        return _section(facts.binding, ownership, unmet, None, None)
    candidate, unmet[2] = _candidate(repro.checks)
    subject, reasons = _subject(facts.parsed, candidate)
    if subject is not None:
        reasons = _defect_reasons(facts, subject)
    unmet[2] += reasons
    # The subject-bound link checks need the defect; an unproven one gives
    # them nothing to bind to, so they are not run (no reason is invented).
    proven = subject if not reasons else None
    cls = candidate.cls if candidate is not None else None
    link, unmet[3] = _link(facts, cls, proven)
    defect = _defect(proven) if proven is not None else None
    return _section(facts.binding, ownership, unmet, defect, link)


def _section(
    binding: Binding,
    ownership: Ownership,
    unmet: dict[Condition, list[str]],
    defect: Defect | None,
    link: Link | None,
) -> AdmissionSection:
    """The section: ``admitted`` only with no reason and both parts built."""
    entries = [
        Unmet(condition=condition, reason="; ".join(reasons))
        for condition, reasons in sorted(unmet.items())
        if reasons
    ]
    if not entries and defect is not None and link is not None:
        return AdmissionSection(
            verdict="admitted",
            binding=binding,
            ownership=ownership,
            defect=defect,
            link=link,
        )
    if not entries:  # unreachable by construction; refuse rather than admit
        entries = [Unmet(condition=3, reason="link not established")]
    return AdmissionSection(
        verdict="insufficient_grounds",
        binding=binding,
        ownership=ownership,
        unmet=entries,
    )


# --- condition (1) --------------------------------------------------------


def _ownership(facts: OwnershipFacts) -> tuple[Ownership, list[str]]:
    """(1) A §2.4: the section's ownership and its unmet reasons.

    Total: facts the ``Ownership`` model rejects (e.g. ``confirmed`` without a
    fingerprint) are refused as inconsistent rather than raised.
    """
    try:
        ownership = ownership_from_facts(facts)
    except ValidationError as error:
        reason = f"ownership facts inconsistent: {error.errors()[0]['msg']}"
        return Ownership(status="not_confirmed", reason=reason), [reason]
    if facts.status == "confirmed":
        return ownership, []
    return ownership, [f"ownership not confirmed: step {facts.step}: {facts.reason}"]


# --- condition (2) --------------------------------------------------------


def _candidate(
    checks: list[ReproductionCheck],
) -> tuple[_Candidate | None, list[str]]:
    """(2) The one admissible defect R reported (A §3), or the reasons.

    Candidates: ``copy_sources`` "source <p> absent from the context" and a
    failed ``syntax_from_args``; any other failed syntax check is refused.
    """
    candidates: list[_Candidate] = []
    reasons: list[str] = []
    for check in checks:
        if check.status != "failed":
            continue
        found = _as_candidate(check)
        if found is not None:
            candidates.append(found)
        elif check.check_id.startswith("syntax_"):
            reasons.append(f"check {check.check_id} not admissible")
    if not candidates:
        return None, [*reasons, "no admissible defect"]
    if len(candidates) > 1:
        return None, [*reasons, f"{len(candidates)} defect candidates, one required"]
    return candidates[0], reasons


def _as_candidate(check: ReproductionCheck) -> _Candidate | None:
    """A failed check read as a defect candidate, when it is one."""
    location = check.location
    if location is None:
        return None
    if check.check_id == "syntax_from_args":
        return _Candidate("from_argument_count", location.file, location.lines, None)
    absent = _ABSENT_RE.fullmatch(check.finding or "")
    if check.check_id == "copy_sources" and absent is not None:
        return _Candidate(
            "missing_copy_source", location.file, location.lines, absent.group(1)
        )
    return None


def _subject(
    parsed: ParsedDockerfile, candidate: _Candidate | None
) -> tuple[_Subject | None, list[str]]:
    """The instruction spanning exactly the candidate's lines."""
    if candidate is None:
        return None, []
    start, end = candidate.lines
    hits = [
        i for i in parsed.instructions if (i.first_line, i.last_line) == (start, end)
    ]
    if len(hits) != 1:
        return None, [f"no single instruction spans lines {start}-{end}"]
    return _Subject(candidate, hits[0]), []


def _defect_reasons(facts: VerifiedFacts, subject: _Subject) -> list[str]:
    """(2) Form and absence (A §3.1) or the FROM check (A §3.2)."""
    unread = unread_reason(facts.parsed)
    if unread is not None:
        return [f"form not admissible: Dockerfile not fully read ({unread})"]
    path = subject.candidate.path
    if path is None:
        return _from_form(subject.instruction)
    reasons = _copy_form(subject.instruction, path)
    snapshot = facts.ownership.snapshot
    if snapshot is None:
        if facts.ownership.status == "confirmed":
            reasons.append("absence not proven: no verified source snapshot")
    elif not snapshot.tree_complete:
        reasons.append("absence not proven: source snapshot listing incomplete")
    else:
        reasons += _absence(path, snapshot.tree, "source snapshot")
    reasons += _head_absence(path, facts)
    return reasons


def _head_absence(path: str, facts: VerifiedFacts) -> list[str]:
    """(2) Absence at ``head_sha``: only provable from a complete listing that
    holds the artifact itself as a blob (a truncated, empty or foreign
    listing proves nothing)."""
    if not facts.head_listing_complete:
        return ["head_sha listing incomplete; absence not provable"]
    artifact = facts.binding.artifact_path
    if not any(r.path == artifact and r.type == "blob" for r in facts.head_listing):
        return [f"head listing lacks {artifact}; absence at head_sha not provable"]
    return _absence(path, facts.head_listing, "head_sha listing")


def _from_form(instruction: Instruction) -> list[str]:
    """(2) A §3.2: R's finding is on a FROM instruction."""
    if instruction.keyword != "FROM":
        return [f"syntax_from_args finding on {instruction.keyword}, not FROM"]
    return []


def _copy_form(instruction: Instruction, path: str) -> list[str]:
    """(2) A §3.1 form: every source a literal local path in R's alphabet,
    no excluded flag, no heredoc, no JSON escape, and ``path`` among them."""
    sources, why = _copy_sources(instruction)
    if why is not None:
        return [f"form not admissible: {why}"]
    reasons = [
        f"form not admissible: {why}"
        for source in sources
        if (why := _source_form(instruction.keyword, source)) is not None
    ]
    if not reasons and path not in {_norm(s) for s in sources}:
        reasons.append(f"form not admissible: {path} is not a source of the COPY/ADD")
    return reasons


def _copy_sources(instruction: Instruction) -> tuple[list[str], str | None]:
    """The raw sources of a COPY/ADD as written, or why they are not read."""
    if instruction.keyword not in ("COPY", "ADD"):
        return [], f"{instruction.keyword} is not COPY/ADD"
    args = instruction.args.strip()
    if opens_heredoc(instruction.keyword, args):
        return [], "heredoc source"
    lead = _LEADING_FLAGS_RE.match(args)
    flags = lead.group(1).split() if lead else []
    body = args[lead.end(1) :].strip() if lead else args
    rest, why = _json_tokens(body) if body.startswith("[") else (body.split(), None)
    if why is not None:
        return [], why
    flags += [t for t in rest if t.startswith("--")]
    rest = [t for t in rest if not t.startswith("--")]
    excluded = [f for f in flags if f.startswith(EXCLUDED_FLAGS)]
    if excluded:
        return [], f"flag {excluded[0]} excluded"
    if len(rest) < 2:
        return [], "no source/destination pair"
    return rest[:-1], None


def _json_tokens(body: str) -> tuple[list[str], str | None]:
    """The JSON array form's tokens; an escape makes the source unknown."""
    if "\\" in body:
        return [], "escape in JSON form"
    try:
        tokens = json.loads(body)
    except json.JSONDecodeError:
        return [], "unparseable JSON form"
    if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
        return [], "unparseable JSON form"
    return tokens, None


def _source_form(keyword: str, source: str) -> str | None:
    """Why one source is not a literal local path (A §3.1), or ``None``."""
    if source.startswith(REMOTE_PREFIXES):
        return f"remote source {source}"
    if not is_modelled_source(source):
        return f"source {source} outside the closed alphabet"
    if GLOB_CHARS & set(source):
        return f"glob source {source}"
    if _norm(source) == ".":
        return f"source {source} is the context root"
    return None


def _absence(path: str, rows: list[TreeRow], listing: str) -> list[str]:
    """(2) A §3.1: no entry equals ``path``, none lies under it, and no
    ancestor is a symlink or a submodule in ``rows``."""
    parts = path.split("/")
    ancestors = {"/".join(parts[:n]) for n in range(1, len(parts))}
    reasons: list[str] = []
    for row in rows:
        if row.path == path:
            reasons.append(f"{path} present")
        elif row.path.startswith(f"{path}/"):
            reasons.append(f"entry {row.path} under {path}")
        elif row.path in ancestors and row.mode in LINK_MODES:
            reasons.append(f"ancestor {row.path} is a {LINK_MODES[row.mode]}")
    return [f"absence not proven in the {listing}: {r}" for r in reasons]


def _defect(subject: _Subject) -> Defect:
    """The defect (A §6.2): the source path, or the FROM instruction text."""
    candidate = subject.candidate
    return Defect(
        cls=candidate.cls,
        file=candidate.file,
        lines=candidate.lines,
        object=candidate.path or subject.instruction.text,
    )


# --- condition (3) --------------------------------------------------------


def _link(
    facts: VerifiedFacts, cls: DefectClass | None, subject: _Subject | None
) -> tuple[Link | None, list[str]]:
    """(3) A §4: comparison, restoration, dialect, both rows, the binding and
    the differences. Class- and subject-bound checks run only when known."""
    reasons = [*_state_reasons(facts), *_dialect_reasons(facts)]
    decisions, difference_reasons = _differences(facts, cls)
    reasons += difference_reasons
    ci, ci_reasons = _row_match(facts, "ci", cls)
    local, local_reasons = _row_match(facts, "local", cls)
    reasons += ci_reasons + local_reasons
    if subject is None or ci is None or local is None:
        return None, reasons
    ci_side, bind_reasons = _bind_ci(facts, ci, subject)
    local_side, local_bind = _bind_local(facts, local, subject)
    reasons += bind_reasons + local_bind
    if reasons or ci_side is None or local_side is None:
        return None, reasons
    build = facts.reproduction.build
    stderr = build.stderr if build is not None else "build.stderr"
    link = Link(
        ci=_side_link(ci_side, facts.ci_evidence_file),
        local=_side_link(local_side, stderr),
        differences=decisions,
    )
    return link, []


def _side_link(side: _Side, evidence_file: str) -> SideLink:
    """One side of the link with its typed evidence reference."""
    return SideLink(
        row=side.row.id,
        object=side.object,
        evidence_file=evidence_file,
        evidence_lines=list(side.match.evidence_lines),
    )


def _state_reasons(facts: VerifiedFacts) -> list[str]:
    """(3) A §4.3: R's comparison on the same span, restoration ``exact``."""
    repro = facts.reproduction
    reasons: list[str] = []
    comparison = repro.comparison
    if comparison is not None and comparison.state not in LINKED_STATES:
        reasons.append(f"comparison {comparison.state}, not reproduced")
    state = repro.restoration.state if repro.restoration is not None else "absent"
    if state != "exact":
        reasons.append(f"restoration {state}, not exact")
    return reasons


def _dialect_reasons(facts: VerifiedFacts) -> list[str]:
    """(3) A §4.3: a ``# syntax=`` directive on either side is refused."""
    sides = {
        "CI": facts.syntax_directive_ci,
        "local": facts.syntax_directive_local,
        "Dockerfile": facts.parsed.syntax_directive,
    }
    return [
        f"unknown dialect: # syntax={value} ({side})"
        for side, value in sides.items()
        if value is not None
    ]


def _differences(
    facts: VerifiedFacts, cls: DefectClass | None
) -> tuple[list[DifferenceDecision], list[str]]:
    """(3) A §4.3: each difference considered and its decision. ``backend``
    needs the class, so it is not judged without one."""
    comparison = facts.reproduction.comparison
    assert comparison is not None
    required = {name: "unknown" for name in REQUIRED_DIMENSIONS}
    dimensions = required | dict(comparison.dimensions)
    decisions: list[DifferenceDecision] = []
    for name, value in dimensions.items():
        if name == "backend" and cls is None:
            continue
        allowed = _difference_allowed(facts, cls, name, value)
        decisions.append(DifferenceDecision(name=name, value=value, allowed=allowed))
    reasons = [
        f"difference {d.name}={d.value} not allowed" for d in decisions if not d.allowed
    ]
    return decisions, reasons


def _difference_allowed(
    facts: VerifiedFacts, cls: DefectClass | None, name: str, value: str
) -> bool:
    """Whether one dimension's value is tolerated for ``cls`` (A §4.3)."""
    if name == "backend":
        return value in ("same", "differs") and _backend_pair_verified(facts, cls)
    if name in TOLERATED_UNKNOWN:
        return value in ("same", "unknown")
    if name == "ignore_file":
        return value == "same" and _ignore_same(facts.ignore_hashes)
    return value == "same"


def _backend_pair_verified(facts: VerifiedFacts, cls: DefectClass | None) -> bool:
    """Both backends covered by verified rows of ``cls`` on their side."""
    ci, local = _backends(facts)
    return (
        _rows(cls, "ci", ci) != [] and _rows(cls, "local", local) != []
        if cls is not None
        else False
    )


def _ignore_same(hashes: Hashes) -> bool:
    """The effective ignore file: same path and content hash, or absent on
    both sides."""
    (ci_path, ci_sha), (local_path, local_sha) = hashes
    if ci_path is None and local_path is None:
        return ci_sha is None and local_sha is None  # a hash needs a path
    return ci_path == local_path and ci_sha is not None and ci_sha == local_sha


def _backends(facts: VerifiedFacts) -> tuple[str | None, str | None]:
    """R's recorded backend per side (``docker``/``podman``)."""
    comparison = facts.reproduction.comparison
    if comparison is None:
        return None, None
    return comparison.values.get("backend", (None, None))


def _rows(cls: DefectClass | None, side: str, backend: str | None) -> list[Row]:
    """The table's rows for one side's backend (and class, when given)."""
    wanted = ROW_BACKEND.get(backend or "")
    return [
        row
        for row in templates.ROWS
        if row.side == side
        and row.backend == wanted
        and (cls is None or row.cls == cls)
    ]


_MATCHERS: dict[str, Callable[[VerifiedFacts], object]] = {
    "copy-missing/buildkit": lambda f: match_copy_ci(f.ci_text),
    "copy-missing/podman": lambda f: match_copy_local(f.local_stderr),
    "from-args/buildkit": lambda f: match_from_ci(f.ci_text),
    "from-args/podman": lambda f: match_from_local(f.local_stdout, f.local_stderr),
}
"""Each row id's matcher over the side's text (A §4.1)."""


def _row_match(
    facts: VerifiedFacts, side: str, cls: DefectClass | None
) -> tuple[tuple[Row, Match] | None, list[str]]:
    """(3) The one row of ``cls`` matching this side; a log matching rows of
    both classes, none, or ambiguously is refused (A §4.2)."""
    label = "CI" if side == "ci" else "local"
    backend = _backends(facts)[0 if side == "ci" else 1]
    if ROW_BACKEND.get(backend or "") is None:
        return None, [f"{label} backend {backend} has no rows"]
    hits = [
        (row, found)
        for row in _rows(None, side, backend)
        if (found := _MATCHERS[row.id](facts)) is not None
    ]
    if len({row.cls for row, _ in hits}) > 1:
        return None, [f"{label} output matches rows of both classes"]
    if cls is None:
        return None, []
    own = [(row, found) for row, found in hits if row.cls == cls]
    if not own:
        return None, [
            f"{label} output matches no {cls} row: unknown format or not the same check"
        ]
    if len(own) > 1 or own[0][1] == AMBIGUOUS:
        return None, [f"{label} row {own[0][0].id} ambiguous"]
    row, found = own[0]
    assert isinstance(found, (CopyMatch, FromMatch))
    return (row, found), []


def _bind_ci(
    facts: VerifiedFacts, hit: tuple[Row, Match], subject: _Subject
) -> tuple[_Side | None, list[str]]:
    """(3) The CI row bound to R's CI instruction and the defect (rule a)."""
    row, found = hit
    comparison = facts.reproduction.comparison
    ref = comparison.ci_instruction if comparison is not None else None
    lines = subject.candidate.lines
    if isinstance(found, CopyMatch):
        reasons = []
        if ref is None or ref.kind != "span" or found.lines != ref.lines:
            reasons.append(f"CI span {found.lines} is not R's CI instruction")
        if found.lines != lines:
            reasons.append(f"CI span {found.lines} is not the defect at {lines}")
        key = _instruction_key(found.step_text or "")
        if key != subject.instruction.text:
            reasons.append(f"CI block {key} is not the defect instruction")
        obj, why = _bind_object(found.path, subject, "CI")
        reasons += why
        return (_Side(row, found, obj) if not reasons else None), reasons
    assert isinstance(found, FromMatch)
    reasons = []
    if ref is None or ref.kind != "parse" or found.line != ref.lines[0]:
        reasons.append(f"CI parse line {found.line} is not R's CI instruction")
    if found.line != lines[0]:
        reasons.append(f"CI parse line {found.line} is not the defect at {lines}")
    return (_Side(row, found, None) if not reasons else None), reasons


def _bind_local(
    facts: VerifiedFacts, hit: tuple[Row, Match], subject: _Subject
) -> tuple[_Side | None, list[str]]:
    """(3) The local row bound to the defect instruction (A §4.2)."""
    row, found = hit
    if isinstance(found, FromMatch):
        reasons = _one_bad_from(facts.parsed, subject)
        return (_Side(row, found, None) if not reasons else None), reasons
    assert isinstance(found, CopyMatch)
    key = _instruction_key(found.step_text or "")
    spans = [
        (i.first_line, i.last_line) for i in facts.parsed.instructions if i.text == key
    ]
    if len(spans) > 1:
        return None, [f"local binding ambiguous: {len(spans)} instructions {key}"]
    reasons = []
    if spans != [subject.candidate.lines]:
        reasons.append(f"local STEP {key} is not the defect instruction")
    obj, why = _bind_object(found.path, subject, "local")
    reasons += why
    return (_Side(row, found, obj) if not reasons else None), reasons


def _one_bad_from(parsed: ParsedDockerfile, subject: _Subject) -> list[str]:
    """(3) ``from-args/podman`` names no line: exactly one FROM with a wrong
    argument count, and it is the defect's (rule e)."""
    bad = [
        c.location.lines
        for c in syntax_checks(parsed, subject.candidate.file)
        if c.check_id == "syntax_from_args"
        and c.status != "passed"
        and c.location is not None
    ]
    if len(bad) != 1:
        return [f"local from-args binding: {len(bad)} FROM candidates, one required"]
    if bad[0] != subject.candidate.lines:
        return [f"local from-args binding: the bad FROM is at {bad[0]}"]
    return []


def _bind_object(
    raw: str, subject: _Subject, label: str
) -> tuple[str | None, list[str]]:
    """(3) The row's ``<P>`` (rule d): normalised, one source of that
    instruction, and the path proven absent."""
    path = _object_path(raw)
    if path is None:
        return None, [f"{label} object {raw} not bound: normalisation ambiguous"]
    sources, _ = _copy_sources(subject.instruction)
    count = sum(_norm(s) == path for s in sources)
    if count == 0:
        return None, [f"{label} object {path} not bound: not a source of it"]
    if count > 1:
        return None, [f"{label} object {path} not bound: {count} candidates"]
    if path != subject.candidate.path:
        return None, [f"{label} object {path} not bound: not the absent source"]
    return path, []


def _object_path(raw: str) -> str | None:
    """``<P>`` relative to the context root, or ``None`` when its reading is
    not certain (outside the alphabet, a glob, the root itself)."""
    if not is_modelled_source(raw) or GLOB_CHARS & set(raw):
        return None
    path = _norm(raw)
    return None if path == "." else path


def _norm(source: str) -> str:
    """Context-relative form of a source, clamped at the root; ``.`` for it."""
    return posixpath.normpath("/" + source).lstrip("/") or "."


def _instruction_key(text: str) -> str:
    """Normalised instruction text with an upper-case keyword, as R compares."""
    head, _, rest = normalise(text).partition(" ")
    return f"{head.upper()} {rest}".strip()
