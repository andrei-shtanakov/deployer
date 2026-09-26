"""CI evidence of one attempt, recurrence, and the evaluation (design §7.3–§7.4).

:func:`attempt_evidence` reads the log of the job an attempt was qualified
on: positive evidence is :func:`deployer.fix.templates.match_ci` returning
``passed`` on the bound build step's own section of the log as read
(:func:`build_section`: the block after the one ``##[group]Run <build line>``
header, up to the next ``##[group]Run `` header, split on ``\\n`` only);
recurrence is the admitted class's admission matcher
(:mod:`deployer.admission.templates`) binding to the corrected instruction's
line span, whatever its object. A non-qualified attempt becomes evidence
through :func:`from_qualification`, so nothing is dropped between
qualification and evaluation.

:func:`evaluate` is order-independent and turns every attempt's evidence
into ``ci_confirmed`` or ``ci_confirmation_insufficient`` with one reason
from the closed §7.5 list. Neither function raises.
"""

from dataclasses import dataclass
from typing import Any, Literal

from deployer.admission.model import DefectClass
from deployer.admission.templates import (
    AMBIGUOUS as AMBIGUOUS_MATCH,
)
from deployer.admission.templates import (
    Ambiguous,
    CopyMatch,
    FromMatch,
    _instruction_key,
    match_copy_ci,
    match_from_ci,
)
from deployer.fix import templates
from deployer.fix.qualify import Qualification, Qualified
from deployer.forge import (
    _ANSI_RE,
    _GROUP_PREFIX,
    _LOG_TIMESTAMP_RE,
    Completeness,
    FailedJob,
    _build_job,
)
from deployer.reproduce.shape import job_text

Outcome = Literal["ci_confirmed", "ci_confirmation_insufficient"]

UNDETERMINED = "qualification undetermined"
CONTRADICTORY = "contradictory runs"
RECURRED = "defect recurred"
NO_QUALIFYING = "no qualifying run"
NOT_ENABLED = "templates not enabled"
NOT_REACHED = "build step not reached"
UNKNOWN_FORMAT = "unknown format"
AMBIGUOUS = "binding ambiguous"
NOT_EVALUATED = "qualified attempt not evaluated"
FAILED_BEFORE = "CI failed before the build"

_KINDS: dict[str, templates.Kind] = {
    "missing_copy_source": "copy",
    "from_argument_count": "from",
}
_GREEN = frozenset({"success", "skipped", "neutral"})
_RUN = "Run "
_RUN_GROUP = f"{_GROUP_PREFIX}{_RUN}"
_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
"""Line breaks other than ``\\n`` that ``str.splitlines`` splits on: on a
section line they are refused, never re-split (ruling T)."""
# The most specific reason first, when no attempt is positive or recurred;
# ``"failed_before"`` marks an attempt whose job failed before its build step.
_SPECIFIC: tuple[tuple[str, str], ...] = (
    ("binding_ambiguous", AMBIGUOUS),
    ("failed_before", FAILED_BEFORE),
    ("not_confirmed", NOT_REACHED),
    ("unknown_format", UNKNOWN_FORMAT),
    ("not_enabled", NOT_ENABLED),
)


@dataclass(frozen=True)
class AttemptEvidence:
    """What one attempt says about the fix.

    ``key`` is ``(run_id, attempt, job key or "")``; ``qualification`` is the
    attempt's, or ``undetermined`` when a qualified attempt's job text could
    not be read here. ``lines`` are the admission match's evidence lines when
    the defect recurred (1-based, in the job text); ``log_lines`` the
    template's (1-based lines of the job log as read, split on ``\\n``).
    ``template`` is the CI template's verdict (``None`` when not read);
    ``failed_before_build`` is set when the template did not pass and a step
    of the bound job before its build step failed. ``ambiguous_recurrence``
    is set when the admitted class's admission matcher found its diagnostic
    but could not bind it (``"ambiguous"``): not a recurrence, but it blocks
    ``ci_confirmed`` as ``binding ambiguous`` (ruling AB).
    """

    key: tuple[int, int, str]
    qualification: Qualification
    positive: bool
    recurred: bool
    detail: str | None
    lines: tuple[int, ...]
    template: templates.Evidence | None = None
    failed_before_build: bool = False
    ambiguous_recurrence: bool = False
    log_lines: tuple[int, ...] = ()


def from_qualification(q: Qualified) -> AttemptEvidence:
    """Evidence of an attempt that was not read for evidence: its
    qualification and reason, neither positive nor recurred.

    A ``qualified`` attempt must be read by :func:`attempt_evidence`; one
    passed here was never checked for recurrence, so it is ``undetermined``.
    """
    if q.status == "qualified":
        return _undetermined(q, NOT_EVALUATED)
    return AttemptEvidence(_key(q), q.status, False, False, q.reason, ())


def attempt_evidence(
    q: Qualified,
    cls: DefectClass,
    corrected_text: str,
    lines: tuple[int, int],
    log: str,
    *,
    dockerfile: bytes,
) -> AttemptEvidence:
    """Positive evidence and recurrence of one attempt (§7.3).

    ``log`` is the bound job's log as GitHub returns it (``_Gh.logs``);
    ``lines`` the corrected instruction's Dockerfile span; ``dockerfile``
    the corrected Dockerfile's bytes at the fix commit (the CI template reads
    its strict form and, for COPY/ADD, the corrected text's uniqueness in
    it). A non-qualified
    attempt is :func:`from_qualification`. When the job text cannot be
    rebuilt from ``log`` — blank, not the text the attempt was qualified on,
    or any failure — the evidence is ``undetermined`` with the reason.
    """
    try:
        if q.status != "qualified":
            return from_qualification(q)
        return _read(q, cls, corrected_text, lines, log, dockerfile)
    except Exception as exc:  # noqa: BLE001 — totality: never raise
        return _undetermined(q, f"evidence failed: {type(exc).__name__}: {exc}")


def evaluate(
    evidence: list[AttemptEvidence], listing_complete: bool
) -> tuple[Outcome, str | None]:
    """The §7.4 outcome and reason over every attempt's evidence.

    Order-independent. An incomplete listing or any ``undetermined``
    attempt blocks the claim first; ``excluded`` attempts count toward
    nothing. Any failure here is ``qualification undetermined``.
    """
    try:
        return _evaluate(evidence, listing_complete)
    except Exception:  # noqa: BLE001 — totality: never raise
        return "ci_confirmation_insufficient", UNDETERMINED


def _evaluate(
    evidence: list[AttemptEvidence], listing_complete: bool
) -> tuple[Outcome, str | None]:
    """:func:`evaluate` without the totality guard."""
    if not listing_complete or any(e.qualification == "undetermined" for e in evidence):
        return "ci_confirmation_insufficient", UNDETERMINED
    qualified = [e for e in evidence if e.qualification == "qualified"]
    positive = any(e.positive for e in qualified)
    recurred = any(e.recurred for e in qualified)
    if positive and recurred:
        return "ci_confirmation_insufficient", CONTRADICTORY
    if recurred:
        return "ci_confirmation_insufficient", RECURRED
    if any(e.ambiguous_recurrence for e in qualified):
        return "ci_confirmation_insufficient", AMBIGUOUS
    if positive:
        return "ci_confirmed", None
    if not qualified:
        return "ci_confirmation_insufficient", NO_QUALIFYING
    seen: set[str | None] = {e.template for e in qualified}
    if any(e.failed_before_build for e in qualified):
        seen.add("failed_before")
    reason = next((r for t, r in _SPECIFIC if t in seen), NOT_REACHED)
    return "ci_confirmation_insufficient", reason


def _read(
    q: Qualified,
    cls: DefectClass,
    corrected_text: str,
    lines: tuple[int, int],
    log: str,
    dockerfile: bytes,
) -> AttemptEvidence:
    """The job text from ``log``, then the template and the recurrence."""
    job = q.job
    if job is None or job.all_steps is None:
        return _undetermined(q, "qualified job has no steps to read its log")
    if not log.strip():
        return _undetermined(q, f"log of job {job.job_id} is empty")
    text = job_text(_rebuilt(job, log))
    if text != job_text(job):
        return _undetermined(
            q, f"log of job {job.job_id} is not the one it was qualified on"
        )
    outcome, log_lines = _template(q, cls, corrected_text, log, dockerfile)
    recurrence = _recurrence(cls, text, corrected_text, tuple(lines))
    ambiguous = recurrence == AMBIGUOUS_MATCH
    detail = outcome.detail
    if isinstance(recurrence, tuple):
        detail = f"defect recurred at lines {lines[0]}-{lines[1]}"
    elif ambiguous:
        detail = "admission match ambiguous"
    return AttemptEvidence(
        key=_key(q),
        qualification="qualified",
        positive=outcome.evidence == "passed",
        recurred=isinstance(recurrence, tuple),
        detail=detail,
        lines=recurrence if isinstance(recurrence, tuple) else (),
        template=outcome.evidence,
        failed_before_build=outcome.evidence != "passed" and _failed_before(q),
        ambiguous_recurrence=ambiguous,
        log_lines=log_lines,
    )


def _template(
    q: Qualified, cls: DefectClass, corrected: str, log: str, dockerfile: bytes
) -> tuple[templates.Outcome, tuple[int, ...]]:
    """The CI template over the bound build step's section of ``log`` only,
    and its evidence lines as lines of ``log``. No section is
    ``binding_ambiguous`` (ruling T)."""
    title = _build_title(q)
    section = build_section(log, title) if title is not None else None
    if not isinstance(section, tuple):
        reason = section or "the bound build step has no runner group title"
        return templates.Outcome("binding_ambiguous", (), reason, None), ()
    offset, text = section
    outcome = templates.match_ci(_KINDS[cls], corrected, text, dockerfile=dockerfile)
    return outcome, tuple(offset + n for n in outcome.lines)


def _build_title(q: Qualified) -> str | None:
    """The bound build step's runner group title (``Run <build line>``): the
    step's name when it has that form, else ``None``."""
    if q.job is None or q.shape is None:
        return None
    names = [s.name for s in q.job.all_steps or [] if s.number == q.shape.build_step]
    if len(names) != 1 or not names[0].startswith(_RUN):
        return None
    return names[0]


def build_section(log: str, title: str) -> tuple[int, str] | str:
    """The bound build step's own section of the job log as read.

    ``log`` is split on ``\\n`` only (a leading BOM dropped); each line is
    read as forge reads it (one trailing ``\\r`` of a CRLF ending, the runner
    timestamp and ANSI sequences removed). The section is the lines after the
    one line ``##[group]<title>``, up to the next ``##[group]Run `` line or
    the end. Returns ``(offset, text)`` — section line ``n`` is line
    ``offset + n`` of ``log`` — or the reason there is no such section: no
    header or several, or a line break other than ``\\n`` (a lone ``\\r``,
    ``\\x0b``, ``\\x0c``, ``\\x1c``-``\\x1e``, ``\\x85``, U+2028, U+2029) on a
    section line, which forge's own reading would have split."""
    raw = log.removeprefix("\ufeff").split("\n")
    last = len(raw) - 1
    lines = [
        line.removesuffix("\r") if i < last else line for i, line in enumerate(raw)
    ]
    read = [
        _ANSI_RE.sub("", _LOG_TIMESTAMP_RE.sub("", line, count=1)) for line in lines
    ]
    header = f"{_GROUP_PREFIX}{title}"
    at = [i for i, line in enumerate(read) if line == header]
    if len(at) != 1:
        return f"{len(at)} runner group headers {header!r} in the log (need one)"
    start = at[0] + 1
    end = next(
        (i for i in range(start, len(read)) if read[i].startswith(_RUN_GROUP)),
        len(read),
    )
    for i in range(start, end):
        bad = next((c for c in lines[i] if c in _BREAKS), None)
        if bad is not None:
            return (
                f"log line {i + 1}: line break U+{ord(bad):04X} inside the "
                "build step's section"
            )
    return start, "\n".join(read[start:end])


def _failed_before(q: Qualified) -> bool:
    """A step of the bound job before its build step concluded non-green."""
    if q.job is None or q.shape is None:
        return False
    return any(
        s.number < q.shape.build_step
        and s.conclusion is not None
        and s.conclusion not in _GREEN
        for s in q.job.all_steps or []
    )


def _recurrence(
    cls: DefectClass, text: str, corrected: str, lines: tuple[int, ...]
) -> tuple[int, ...] | Ambiguous | None:
    """The admission match's evidence lines when it binds to the corrected
    instruction by A §4.2's rules (``admission.decide._bind_ci``).

    COPY: the ``CopyMatch`` span (R's ``>>>`` block span) equals ``lines``
    and its block text is the corrected instruction, whatever its object.
    FROM: the parse-error line equals the instruction's first line
    ``lines[0]``. ``None`` is no recurrence; the matcher's ``"ambiguous"``
    is returned as it is (no recurrence, but it blocks the claim).
    """
    if cls == "missing_copy_source":
        copy = match_copy_ci(text)
        if copy == AMBIGUOUS_MATCH:
            return AMBIGUOUS_MATCH
        if not isinstance(copy, CopyMatch) or copy.lines != lines:
            return None
        key = _instruction_key(copy.step_text or "")
        return copy.evidence_lines if key == _instruction_key(corrected) else None
    found = match_from_ci(text)
    if found == AMBIGUOUS_MATCH:
        return AMBIGUOUS_MATCH
    if isinstance(found, FromMatch) and found.line is not None:
        if found.line == lines[0]:
            return found.evidence_lines
    return None


def _rebuilt(job: FailedJob, log: str) -> FailedJob:
    """``job`` rebuilt from ``log`` by forge's own reading of a job."""
    record: dict[str, Any] = {
        "name": job.name,
        "conclusion": job.conclusion,
        "steps": [
            {"number": s.number, "name": s.name, "conclusion": s.conclusion}
            for s in job.all_steps or []
        ],
    }
    return _build_job(record, job.job_id, log, [], Completeness("present", "absent"))


def _undetermined(q: Qualified, reason: str) -> AttemptEvidence:
    """A qualified attempt whose evidence could not be read."""
    return AttemptEvidence(_key(q), "undetermined", False, False, reason, ())


def _key(q: Qualified) -> tuple[int, int, str]:
    """``(run_id, attempt, job key or "")``; never raises, so a malformed
    ``q`` still yields evidence (``(0, 0, "")`` for missing parts)."""
    run_id, attempt = getattr(q, "run_id", 0), getattr(q, "attempt", 0)
    job_key = getattr(q, "job_key", None)
    return (
        run_id if isinstance(run_id, int) else 0,
        attempt if isinstance(attempt, int) else 0,
        job_key if isinstance(job_key, str) else "",
    )
