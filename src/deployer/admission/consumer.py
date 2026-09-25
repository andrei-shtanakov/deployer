"""The consumer gate (A §7): may a verdict document open ``ci-fix-authoring``?

:func:`accept_for_fix` is the single entry point. It refuses by default and
never raises: only a well-formed ``admitted`` section of schema 1.3, bound to
the caller's target on all four fields, whose evidence files all resolve in
the try directory with their referenced lines in range, is ``Accepted``.
Everything else, including any exception on the way, is ``Refused`` with a
specific reason. Refusals follow A §7's order:

1. the ``admission`` section is absent, of an unknown schema version, or has
   an unknown ``verdict``;
2. the section is malformed (any A §1 / §6.2 invariant broken);
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

from deployer.admission.fsread import Unreadable, read_in_tree
from deployer.admission.model import (
    ADMISSION_VERDICT_SCHEMA_VERSION,
    AdmissionSection,
    Binding,
    SideLink,
)

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
"""An evidence file longer than this is not read (and the gate refuses)."""
VERDICTS = ("admitted", "insufficient_grounds")
CI_EVIDENCE = ("ci.log",)
LOCAL_EVIDENCE = ("build.stdout", "build.stderr")
"""The typed evidence references of A §6.2, per side (R §6 path bases)."""
BINDING_FIELDS = ("repo", "head_sha", "artifact_path", "artifact_sha256")
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
    section = _validated(raw)
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


def _validated(raw: Mapping[str, object]) -> AdmissionSection | Refused:
    """The section under the model's invariants, or refusal (2): malformed."""
    try:
        section = AdmissionSection.model_validate(dict(raw))
    except ValidationError as exc:
        return Refused(f"malformed admission section: {_errors(exc)}")
    return _evidence_type_refusal(section) or section


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


def _verdict_refusal(section: AdmissionSection) -> Refused | None:
    """Refusal (3): anything but ``admitted``, with the unmet conditions."""
    if section.verdict == "admitted":
        return None
    unmet = "; ".join(f"({u.condition}) {u.reason}" for u in section.unmet)
    return Refused(f"verdict {section.verdict}: {unmet}")


def _binding_refusal(binding: Binding, target: Target) -> Refused | None:
    """Refusal (4): a ``null`` artifact hash means the binding differs from
    any target, before any value is compared; then each field in turn."""
    if binding.artifact_sha256 is None:
        return Refused(
            "binding artifact_sha256 is null (the artifact's bytes at head_sha "
            "were not obtained): the binding differs from the target"
        )
    for field in BINDING_FIELDS:
        have, want = getattr(binding, field), getattr(target, field)
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
    written (UTF-8, newlines untranslated, ``str.splitlines``).

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
    return len(text.splitlines())


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
