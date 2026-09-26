"""CI evidence of one attempt, recurrence, and the evaluation (design §7.3–§7.4).

:func:`attempt_evidence` reads the log of the job an attempt was qualified
on: positive evidence is :func:`deployer.fix.templates.match_ci` returning
``passed`` on the bound build step's own section of the log as read
(:func:`build_section`: the block after the one ``##[group]Run <build line>``
header, up to the next ``##[group]`` line or the post phase, split on
``\\n`` only);
recurrence is the admitted class's admission matcher
(:mod:`deployer.admission.templates`) binding to the corrected instruction's
line span, whatever its object. A non-qualified attempt becomes evidence
through :func:`from_qualification`, so nothing is dropped between
qualification and evaluation.

:func:`evaluate` is order-independent and turns every attempt's evidence
into ``ci_confirmed`` or ``ci_confirmation_insufficient`` with one reason
from the closed §7.5 list. Neither function raises.
"""

import re
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
    instruction_compare_key,
    match_copy_ci,
    match_from_ci,
)
from deployer.fix import templates
from deployer.fix.qualify import Qualification, Qualified
from deployer.forge import (
    ANSI_CSI_RE,
    RUNNER_GROUP_PREFIX,
    RUNNER_LOG_TIMESTAMP_RE,
    Completeness,
    FailedJob,
    StepInfo,
    build_failed_job,
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
_ENDGROUP = "##[endgroup]"
_RUNNER_TS_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z(?: |$)"
)
"""The runner's timestamp on every line it writes, exactly as every
C-recording shows it (``2026-09-25T14:30:21.1506466Z #0 building with …``,
``c1`` line 109; an empty output line is the timestamp and one space). Lines
of an ``env:`` value echoed by the runner carry none (review N2)."""
_POST = "Post "
_POST_JOB = "Post job cleanup."
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
    ``binding_ambiguous`` (ruling T). The log's lines after the section, to
    its end, go to the template as ``after``: section-end markers end the
    positive evidence but never hide a failing vertex (ruling AA). The bound
    build step's API conclusion goes with it (ruling AB): a step not
    identified uniquely has none, which refuses COPY evidence."""
    step = _build_step(q)
    title = step.name if step is not None and step.name.startswith(_RUN) else None
    steps = frozenset(s.name for s in (q.job.all_steps if q.job else None) or [])
    section = _section(log, title, steps) if title is not None else None
    if not isinstance(section, tuple):
        reason = section or "the bound build step has no runner group title"
        return templates.Outcome("binding_ambiguous", (), reason, None), ()
    start, end, read = section
    outcome = templates.match_ci(
        _KINDS[cls],
        corrected,
        "\n".join(read[start:end]),
        dockerfile=dockerfile,
        after="\n".join(read[end:]),
        conclusion=step.conclusion if step is not None else None,
    )
    return outcome, tuple(start + n for n in outcome.lines)


def _ends_section(line: str, steps: frozenset[str]) -> bool:
    """Whether ``line`` is the runner's own start of what follows the build
    step: any ``##[group]`` line (the next step's ``##[group]Run …``, e.g.
    ``c6``'s second build at log line 208), the job's post phase
    ``Post job cleanup.`` (every C-recording prints it right after the build
    output, e.g. ``c1`` line 208, ``c4`` line 213 after ``##[error]Process
    completed with exit code 1.``), or ``Post <step name>`` for a step of the
    job (the form of a post step's title; the recordings' one post step,
    ``Post Run actions/checkout@…``, prints only ``Post job cleanup.``)."""
    if line.startswith(RUNNER_GROUP_PREFIX) or line == _POST_JOB:
        return True
    return line.startswith(_POST) and (line in steps or line[len(_POST) :] in steps)


def _build_step(q: Qualified) -> StepInfo | None:
    """The bound build step as the jobs API lists it, when exactly one step
    has its number, else ``None``. Its name, when of the form ``Run <build
    line>``, is the runner group title; its conclusion binds COPY evidence."""
    if q.job is None or q.shape is None:
        return None
    steps = [s for s in q.job.all_steps or [] if s.number == q.shape.build_step]
    return steps[0] if len(steps) == 1 else None


def build_section(
    log: str, title: str, steps: frozenset[str] = frozenset()
) -> tuple[int, str] | str:
    """The bound build step's own section of the job log as read.

    ``log`` is split on ``\\n`` only (a leading BOM dropped — the recordings
    carry one before the first line's timestamp; a final newline opens no
    line); each line is read as forge reads it (one trailing ``\\r`` of a CRLF
    ending, the runner timestamp and ANSI sequences removed). The section is
    the lines after the first ``##[endgroup]`` that follows the one line
    ``##[group]<title>`` — the runner's echo of the script, ``shell:`` and
    ``env:`` is not the build's output — up to the first line that ends the
    build step's output (:func:`_ends_section`; ``steps`` are the job's step
    names) or the end. Returns ``(offset, text)`` — section line ``n`` is line
    ``offset + n`` of ``log`` — or the reason there is no such section: no
    header or several; no ``##[endgroup]`` after it; another
    ``##[endgroup]`` inside the section; a section line without the runner's
    timestamp (:data:`_RUNNER_TS_RE`); or a line break other than ``\\n`` (a
    lone ``\\r``, ``\\x0b``, ``\\x0c``, ``\\x1c``-``\\x1e``, ``\\x85``,
    U+2028, U+2029) on a section line, which forge's own reading would have
    split. The rest of the log after the section is still searched for
    failing vertices (:func:`_template`, ruling AA)."""
    section = _section(log, title, steps)
    if isinstance(section, str):
        return section
    start, end, read = section
    return start, "\n".join(read[start:end])


def _section(
    log: str, title: str, steps: frozenset[str]
) -> tuple[int, int, list[str]] | str:
    """:func:`build_section` as ``(start, end, read)``: the section is
    ``read[start:end]`` of the log's lines as read, or the reason."""
    raw = log.removeprefix("\ufeff").split("\n")
    if raw and raw[-1] == "" and len(raw) > 1:
        raw.pop()
    ended = log.endswith("\n")
    last = len(raw) - 1
    lines = [
        line.removesuffix("\r") if ended or i < last else line
        for i, line in enumerate(raw)
    ]
    read = [
        ANSI_CSI_RE.sub("", RUNNER_LOG_TIMESTAMP_RE.sub("", line, count=1))
        for line in lines
    ]
    header = f"{RUNNER_GROUP_PREFIX}{title}"
    at = [i for i, line in enumerate(read) if line == header]
    if len(at) != 1:
        return f"{len(at)} runner group headers {header!r} in the log (need one)"
    closed = next(
        (i for i in range(at[0] + 1, len(read)) if read[i] == _ENDGROUP), None
    )
    if closed is None:
        return f"no {_ENDGROUP} after {header!r}"
    start = closed + 1
    end = next(
        (i for i in range(start, len(read)) if _ends_section(read[i], steps)),
        len(read),
    )
    for i in range(start, end):
        problem = _section_line_problem(lines[i], read[i])
        if problem is not None:
            return f"log line {i + 1}: {problem}"
    return start, end, read


def _section_line_problem(line: str, read: str) -> str | None:
    """Why a build-section line cannot be read as the runner wrote it."""
    bad = next((c for c in line if c in _BREAKS), None)
    if bad is not None:
        return f"line break U+{ord(bad):04X} inside the build step's section"
    if _RUNNER_TS_RE.match(line) is None:
        return "section line without a runner timestamp"
    if read == _ENDGROUP:
        return f"a second {_ENDGROUP} inside the build step's section"
    return None


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
        key = instruction_compare_key(copy.step_text or "")
        return (
            copy.evidence_lines if key == instruction_compare_key(corrected) else None
        )
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
    return build_failed_job(
        record, job.job_id, log, [], Completeness("present", "absent")
    )


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
