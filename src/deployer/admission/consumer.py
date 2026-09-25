"""The consumer gate (A §7): may a verdict document open ``ci-fix-authoring``?

:func:`accept_for_fix` is the single entry point. It refuses by default and
never raises: only a well-formed ``admitted`` section of schema 1.3, bound to
the caller's target on all four fields, whose evidence files all resolve in
the try directory with their referenced lines in range, is ``Accepted``.
Everything else, including any exception on the way, is ``Refused`` with a
specific reason. Refusals follow A §7's order:

1. the ``admission`` section is absent, of an unknown schema version, or has
   an unknown ``verdict``;
2. the section is malformed: any A §1 / §6.2 invariant broken, including a
   document whose ``reproduction`` was not ``attempted``, a missing
   ``unmet`` key, and a link that disagrees with the closed template table,
   the defect or the binding (see :func:`_link_problem`);
3. the verdict is ``insufficient_grounds``;
4. the ``binding`` differs from the target (a ``null`` artifact hash first);
5. an evidence file is missing or unreadable, or a referenced line is out of
   range.

The gate reads the document and the evidence files only; it never re-derives
admission from the logs.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from deployer.admission.decide import LINKED_STATES
from deployer.admission.fsread import Unreadable, read_in_tree
from deployer.admission.model import (
    ADMISSION_VERDICT_SCHEMA_VERSION,
    AdmissionSection,
    Binding,
    Defect,
    Link,
    SideLink,
)
from deployer.admission.templates import ROWS, split_lines
from deployer.reproduce.compare import REQUIRED_DIMENSIONS

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
"""An evidence file longer than this is not read (and the gate refuses)."""
VERDICTS = ("admitted", "insufficient_grounds")
CI_EVIDENCE = ("ci.log",)
LOCAL_EVIDENCE = ("build.stdout", "build.stderr")
"""The typed evidence references of A §6.2, per side (R §6 path bases)."""
BINDING_FIELDS = ("repo", "head_sha", "artifact_path", "artifact_sha256")
REQUIRED_DIFFERENCES = frozenset(REQUIRED_DIMENSIONS) | {"restoration"}
"""The differences ``decide`` always considers for an admitted class (A §4.3):
each must be present exactly once."""
_ROWS_BY_ID = {row.id: row for row in ROWS}
_SHOWN = 80
_ERRORS_SHOWN = 5


@dataclass(frozen=True)
class Target:
    """What ``ci-fix-authoring`` is about to fix: the admission must hold for
    exactly this repository, commit, artifact path and artifact bytes."""

    repo: str
    head_sha: str
    artifact_path: str
    artifact_sha256: str


@dataclass(frozen=True)
class Accepted:
    """The gate opens: ``section`` is the validated ``admitted`` section."""

    section: AdmissionSection


@dataclass(frozen=True)
class Refused:
    """The gate stays shut, for ``reason``."""

    reason: str


def accept_for_fix(
    document: Mapping[str, object], try_dir: Path, target: Target
) -> Accepted | Refused:
    """Decide whether verdict ``document`` opens fix authoring for ``target``
    (A §7), reading evidence under ``try_dir``. Never raises."""
    try:
        return _accept(document, try_dir, target)
    except Exception as exc:  # noqa: BLE001 — the gate never raises (A §7)
        return Refused(f"refused on an unexpected {_describe(exc)}")


def _accept(
    document: Mapping[str, object], try_dir: Path, target: Target
) -> Accepted | Refused:
    """A §7's refusals in order; ``Accepted`` only when none applies."""
    raw = _raw_section(document)
    if isinstance(raw, Refused):
        return raw
    section = _validated(document, raw)
    if isinstance(section, Refused):
        return section
    refusal = (
        _verdict_refusal(section)
        or _binding_refusal(section.binding, target)
        or _evidence_refusal(section, try_dir)
    )
    return refusal or Accepted(section)


def _raw_section(document: object) -> Mapping[str, object] | Refused:
    """The raw ``admission`` mapping, or refusal (1): absent, unknown schema
    version, unknown verdict."""
    if not isinstance(document, Mapping):
        return Refused(f"the document is not a mapping: {type(document).__name__}")
    if "admission" not in document:
        return Refused(
            "no admission section: the document predates verdict schema "
            f"{ADMISSION_VERDICT_SCHEMA_VERSION} or its reproduction was not attempted"
        )
    version = document.get("verdict_schema_version")
    if not isinstance(version, str) or version != ADMISSION_VERDICT_SCHEMA_VERSION:
        return Refused(
            f"unknown verdict schema version {_show(version)}; the gate reads "
            f"{ADMISSION_VERDICT_SCHEMA_VERSION!r}"
        )
    raw = document["admission"]
    if not isinstance(raw, Mapping):
        return Refused(f"the admission section is not a mapping: {_show(raw)}")
    verdict = raw.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return Refused(f"unknown admission verdict {_show(verdict)}")
    return raw


def _validated(
    document: Mapping[str, object], raw: Mapping[str, object]
) -> AdmissionSection | Refused:
    """The section under the model's invariants, or refusal (2): malformed."""
    problem = _wire_problem(document, raw)
    if problem is not None:
        return Refused(f"malformed admission section: {problem}")
    try:
        section = AdmissionSection.model_validate(dict(raw))
    except ValidationError as exc:
        return Refused(f"malformed admission section: {_errors(exc)}")
    return (
        _evidence_type_refusal(section)
        or _link_refusal(section)
        or _document_refusal(document, section)
        or section
    )


def _wire_problem(
    document: Mapping[str, object], raw: Mapping[str, object]
) -> str | None:
    """Shape the model defaults away (A §6.2): the section exists only for an
    ``attempted`` reproduction, and ``unmet`` is always on the wire."""
    reproduction = document.get("reproduction")
    status = reproduction.get("status") if isinstance(reproduction, Mapping) else None
    if status != "attempted":
        return f"reproduction status {_show(status)} is not 'attempted'"
    if "unmet" not in raw:
        return "the unmet key is missing"
    return None


def _document_refusal(
    document: Mapping[str, object], section: AdmissionSection
) -> Refused | None:
    """Refusal (2) for a section the rest of the document contradicts: the
    ``run`` it was decided for, R's binding, restoration and comparison."""
    problem = _run_problem(document, section.binding) or (
        _reproduction_problem(document, section)
    )
    if problem is None:
        return None
    return Refused(f"malformed admission section: {problem}")


def _run_problem(document: Mapping[str, object], binding: Binding) -> str | None:
    """``run.repo`` (case-insensitive) and ``run.head_sha`` are the binding's,
    and R built the bound artifact."""
    repo = _get(document, "run", "repo")
    if not isinstance(repo, str) or repo.casefold() != binding.repo.casefold():
        return f"run.repo {_show(repo)} is not binding.repo {_show(binding.repo)}"
    head = _get(document, "run", "head_sha")
    if head != binding.head_sha:
        return f"run.head_sha {_show(head)} is not binding.head_sha"
    dockerfile = _get(document, "reproduction", "binding", "dockerfile")
    if dockerfile != binding.artifact_path:
        return (
            f"reproduction.binding.dockerfile {_show(dockerfile)} is not "
            f"binding.artifact_path {_show(binding.artifact_path)}"
        )
    return None


def _reproduction_problem(
    document: Mapping[str, object], section: AdmissionSection
) -> str | None:
    """For ``admitted``: R restored ``head_sha`` exactly, its comparison is a
    linked state, and each difference's value is R's (A §4.3)."""
    if section.link is None:
        return None
    restoration = _get(document, "reproduction", "restoration", "state")
    if restoration != "exact":
        return f"reproduction.restoration.state {_show(restoration)} is not 'exact'"
    restored = _get(document, "reproduction", "restoration", "sha")
    if restored != section.binding.head_sha:
        return f"reproduction.restoration.sha {_show(restored)} is not head_sha"
    state = _get(document, "reproduction", "comparison", "state")
    if not isinstance(state, str) or state not in LINKED_STATES:
        return f"reproduction.comparison.state {_show(state)} is not linked"
    return _dimensions_problem(document, section.link)


def _dimensions_problem(document: Mapping[str, object], link: Link) -> str | None:
    """The differences are exactly R's dimensions (required ones defaulting
    to ``unknown``, as ``decide`` reads them), value for value."""
    dimensions = _get(document, "reproduction", "comparison", "dimensions")
    if not isinstance(dimensions, Mapping):
        return "reproduction.comparison.dimensions is not a mapping"
    expected = {name: "unknown" for name in REQUIRED_DIMENSIONS} | dict(dimensions)
    decided = {d.name: d.value for d in link.differences}
    for name in sorted(set(expected) | set(decided)):
        if decided.get(name) != expected.get(name):
            return (
                f"difference {name} {_show(decided.get(name))} is not R's "
                f"{_show(expected.get(name))}"
            )
    return None


def _get(node: object, *keys: str) -> object:
    """``node[k1][k2]…`` through Mappings only; ``None`` where one is absent
    or not a Mapping."""
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _evidence_type_refusal(section: AdmissionSection) -> Refused | None:
    """Refusal (2) for a link whose evidence reference is not the side's typed
    file (A §6.2: ``ci.log``; ``build.stdout`` / ``build.stderr``)."""
    if section.link is None:
        return None
    sides = (
        ("ci", section.link.ci, CI_EVIDENCE),
        ("local", section.link.local, LOCAL_EVIDENCE),
    )
    for name, side, allowed in sides:
        if side.evidence_file not in allowed:
            return Refused(
                f"malformed admission section: {name} evidence_file "
                f"{_show(side.evidence_file)} is not one of {list(allowed)}"
            )
    return None


def _link_refusal(section: AdmissionSection) -> Refused | None:
    """Refusal (2) for a link that disagrees with the closed table, the
    defect or the binding (A §4, §6.2)."""
    if section.link is None or section.defect is None:
        return None
    problem = _link_problem(section.link, section.defect, section.binding)
    if problem is None:
        return None
    return Refused(f"malformed admission section: {problem}")


def _link_problem(link: Link, defect: Defect, binding: Binding) -> str | None:
    """The first problem among: rows, objects, the defect's file, evidence
    line order, the differences."""
    return (
        _rows_problem(link, defect)
        or _objects_problem(link, defect)
        or _file_problem(defect, binding)
        or _lines_order_problem(link)
        or _differences_problem(link)
    )


def _rows_problem(link: Link, defect: Defect) -> str | None:
    """Each side's row is a closed-table row of that side and the defect's
    class (A §4.1)."""
    for name, side in (("ci", link.ci), ("local", link.local)):
        row = _ROWS_BY_ID.get(side.row)
        if row is None:
            return f"{name} row {_show(side.row)} is not in the template table"
        if row.side != name:
            return f"{name} row {side.row} is a {row.side} row"
        if row.cls != defect.cls:
            return f"{name} row {side.row} is not of class {defect.cls}"
    return None


def _objects_problem(link: Link, defect: Defect) -> str | None:
    """One object on the defect and both sides for ``missing_copy_source``;
    none on the sides for ``from_argument_count`` (A §4.2, §6.2)."""
    expected = defect.object if defect.cls == "missing_copy_source" else None
    for name, side in (("ci", link.ci), ("local", link.local)):
        if side.object != expected:
            return (
                f"{name} object {_show(side.object)} disagrees with the "
                f"{defect.cls} defect (expected {_show(expected)})"
            )
    return None


def _file_problem(defect: Defect, binding: Binding) -> str | None:
    """The defect is in the bound artifact."""
    if defect.file != binding.artifact_path:
        return (
            f"defect file {_show(defect.file)} is not the binding's "
            f"artifact_path {_show(binding.artifact_path)}"
        )
    return None


def _lines_order_problem(link: Link) -> str | None:
    """Evidence lines strictly increasing (sorted, unique), as produced."""
    for name, side in (("ci", link.ci), ("local", link.local)):
        lines = side.evidence_lines
        if any(a >= b for a, b in zip(lines, lines[1:])):
            return f"{name} evidence_lines {lines} are not strictly increasing"
    return None


def _differences_problem(link: Link) -> str | None:
    """Every required difference exactly once, and none refused (A §4.3)."""
    names = [d.name for d in link.differences]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        return f"differences {duplicated} appear more than once"
    missing = sorted(REQUIRED_DIFFERENCES - set(names))
    if missing:
        return f"required differences {missing} are missing"
    refused = [f"{d.name}={d.value}" for d in link.differences if not d.allowed]
    if refused:
        return f"differences {refused} are not allowed in an admitted section"
    return None


def _verdict_refusal(section: AdmissionSection) -> Refused | None:
    """Refusal (3): anything but ``admitted``, with the unmet conditions."""
    if section.verdict == "admitted":
        return None
    unmet = "; ".join(f"({u.condition}) {u.reason}" for u in section.unmet)
    return Refused(f"verdict {section.verdict}: {unmet}")


def _binding_refusal(binding: Binding, target: Target) -> Refused | None:
    """Refusal (4): a ``null`` artifact hash means the binding differs from
    any target, before any value is compared; then each field in turn. The
    repo is compared case-insensitively (GitHub owner/name are)."""
    if binding.artifact_sha256 is None:
        return Refused(
            "binding artifact_sha256 is null (the artifact's bytes at head_sha "
            "were not obtained): the binding differs from the target"
        )
    for field in BINDING_FIELDS:
        have, want = getattr(binding, field), getattr(target, field)
        if field == "repo" and isinstance(want, str):
            have, want = have.casefold(), want.casefold()
        if have != want:
            return Refused(
                f"binding {field} {_show(have)} differs from target {_show(want)}"
            )
    return None


def _evidence_refusal(section: AdmissionSection, try_dir: Path) -> Refused | None:
    """Refusal (5), CI side first: every evidence file resolves and every
    referenced line is in range."""
    if section.link is None:
        return Refused("admitted section without a link")
    for name, side in (("ci", section.link.ci), ("local", section.link.local)):
        refusal = _side_refusal(name, side, try_dir)
        if refusal is not None:
            return refusal
    return None


def _side_refusal(name: str, side: SideLink, try_dir: Path) -> Refused | None:
    """One side's evidence: at least one line, each within the file."""
    if not side.evidence_lines:
        return Refused(f"{name} evidence {side.evidence_file} references no lines")
    count = _line_count(name, side.evidence_file, try_dir)
    if isinstance(count, Refused):
        return count
    bad = [n for n in side.evidence_lines if n < 1 or n > count]
    if bad:
        return Refused(
            f"{name} evidence {side.evidence_file} lines {bad} out of range "
            f"(the file has {count} lines)"
        )
    return None


def _line_count(name: str, rel: str, try_dir: Path) -> int | Refused:
    """Lines of evidence file ``rel`` under ``try_dir``, counted as they were
    written and numbered: UTF-8, newlines untranslated, split by the
    producer's own rule (:func:`templates.split_lines`, ``\\n`` only), the
    empty element after a final ``\\n`` not being a line.

    ``rel`` is read with the no-follow reader: an absolute path, ``..``, a NUL,
    a symlink or a non-regular file (a directory, a FIFO) is refused, as is a
    file over :data:`MAX_EVIDENCE_BYTES`. Decoding the bytes directly equals a
    UTF-8 read with ``newline=""`` (no newline translation).
    """
    try:
        data = read_in_tree(try_dir, rel, MAX_EVIDENCE_BYTES)
    except Unreadable as exc:
        return Refused(f"{name} evidence file unreadable: {exc}")
    except OSError as exc:
        return Refused(
            f"{name} evidence file {_show(rel)} unreadable: {_describe(exc)}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Refused(f"{name} evidence file {_show(rel)} is not UTF-8: {exc}")
    return len(split_lines(text))


def _errors(exc: ValidationError) -> str:
    """The first validation errors as ``loc: message``, naming the problem."""
    errors = exc.errors()
    shown = [
        f"{'.'.join(str(part) for part in e['loc']) or 'section'}: {e['msg']}"
        for e in errors[:_ERRORS_SHOWN]
    ]
    more = len(errors) - _ERRORS_SHOWN
    return "; ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def _show(value: object) -> str:
    """``repr(value)`` cut to a readable length."""
    text = repr(value)
    return text if len(text) <= _SHOWN else text[: _SHOWN - 3] + "..."


def _describe(exc: BaseException) -> str:
    """The exception's type and message; a message that itself fails to
    render is left out."""
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 — describing must not raise either
        message = ""
    return f"{type(exc).__name__}: {message}"[: _SHOWN * 4]
