"""The reading layer over a :class:`FailedRun` snapshot. It asserts no cause.

The contract (owner's decision, 2026-09-22 — causal classification removed
from this layer):

- Kept: the facts ``forge.py`` read, the evidence, the observations, and the
  completeness of the read.
- A snapshot read completely is ``UNCLASSIFIED``: every line an observation
  shape matched is reported and its block cited; nothing more is concluded.
  A snapshot read incompletely is ``EVIDENCE_UNAVAILABLE``, with what is
  missing named beside the observations the partial read did yield.
- ``CLASSIFIED`` is never produced here, and neither is a kind or a cause.
  ``Outcome``, ``FailureVerdict.kind`` and ``RunDiagnosis.causes`` keep their
  places so the verdict document's shape is stable; from this module they
  read ``null``/``[]``, always. Whether a cause can be established at all is
  the question of the reproduction line
  (``docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md``),
  not of a phrase found in a log.

No network, no I/O: a pure function from the dataclasses ``forge.py``
produced to one verdict per failure and a run summary. Every observation
shape is data (``OBSERVATIONS``), every shape is tried against every piece of
evidence, and every matched line is reported once, as ``"<name>: <line>"`` —
prefixed ``warning-shaped: `` when a tool marked the line as noticed rather
than fatal.

Verdict schema 1.1 (additive over 1.0): ``causes`` is always ``[]`` and every
``kind`` is ``null``; no key was added or removed.
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter

from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
    FailedStep,
    StepRef,
)

# ``CLASSIFIED`` is kept for the document's schema; this module never produces it.
Outcome = Literal["CLASSIFIED", "UNCLASSIFIED", "EVIDENCE_UNAVAILABLE"]

VERDICT_SCHEMA_VERSION = "1.1"

_JOB_LEVEL_NOTE = "cited evidence is job-level (no step binding)"
_NO_OBSERVATION_NOTE = "no observation matched"
_EMPTY_SET_NOTE = "failed run exposes no failed job or step"
WARNING_SHAPED_NOTE = "warning-shaped"

# The shape of a Python exception line, optionally under pytest's ``E`` prefix.
_EXCEPTION_NAME = r"[A-Za-z_]\w*(?:\.\w+)*(?:Error|Exception)"
_EXCEPTION_LINE = rf"(?:E[ \t]+)?{_EXCEPTION_NAME}: "
# `docker build` frames every RUN-step output line as `#<step> <seconds> `
# (buildkit); diagnose, not forge, knows what docker is, so the framing is
# tolerated here, at the line-shape patterns, not stripped at the source.
_LINE_PREFIX = r"[ \t]*(?:#\d+ \d+\.\d+ )?"


@dataclass(frozen=True)
class Observation:
    """One shape worth reporting: a name to report it under, and its pattern."""

    name: str
    pattern: re.Pattern[str]


def _prose(name: str, pattern: str) -> Observation:
    """A shape over prose: case-insensitive, one line at a time."""
    return Observation(name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))


def _exact(name: str, pattern: str) -> Observation:
    """A shape over a machine-shaped line: case-sensitive, one line at a time."""
    return Observation(name, re.compile(pattern, re.MULTILINE))


# The tool framings the network shapes require, each named for the tool that
# prints it. They are line fragments, never anchored to the line's head:
# buildkit frames a step's output as `#N t.ttt ` and then reprints the failing
# lines a second time, under `------`, with a bare `t.ttt ` instead. The bare
# phrases (`connection timed out`, `503 Service Unavailable`, `no space left on
# device`, ...) are printed verbatim by any application under test that
# exercises a retry path, so on their own they are not a shape.
_APT_ERROR = r"(?:^|[ \t])E: "
_APT_UNREACHABLE = r"could not connect to \S+?:\d+"
_CURL = r"curl: \(\d+\)"
_GIT_ACCESS = r"fatal: unable to access '[^'\n]*':"
_PIP_ERROR = r"(?:^|[ \t])ERROR: "
_UV_ERROR = r"(?:error: Failed to fetch:|Caused by:)"
_BUILDKIT = r"failed to solve:"
_DAEMON = r"(?:Error response from daemon:|error during connect:)"
_TIMED_OUT = r"(?:connection timed out|operation timed out|i/o timeout)"
# buildkit's ending for a COPY/ADD source it could not find in the build
# context: the path, quoted, at the line's end. An image reference it could
# not pull ends `: not found` too, without the quotes.
_QUOTED_PATH_NOT_FOUND = r'[^\n]*"[^"\n]*": not found[ \t]*$'

OBSERVATIONS: tuple[Observation, ...] = (
    # --- what the tool that owns an artifact said about its text ------------
    # The Dockerfile parser's own sentence. A line that also carries
    # `unknown instruction` is reported under those words instead, once.
    _prose(
        "dockerfile parse error",
        r"dockerfile parse error(?![^\n]*unknown instruction)",
    ),
    _prose("unknown instruction", r"unknown instruction"),
    # GitHub's workflow validator prints "The workflow is not valid." and/or
    # the `.github/workflows/...` path on the SAME line as the phrase; a test
    # or a third-party tool quoting the phrase carries neither.
    _prose(
        "unrecognized named-value",
        r"(?:workflow is not valid|\.github/workflows/)[^\n]*unrecognized named-value",
    ),
    # The runner could not fetch a `uses:` reference.
    _prose("unresolvable action", r"unable to resolve action"),
    # --- the build context ---------------------------------------------------
    # buildkit's shape for a COPY/ADD source missing from the build context,
    # and the legacy daemon's shape for the same.
    _prose(
        "copy/add source not found",
        rf"failed to (?:solve|compute cache key){_QUOTED_PATH_NOT_FOUND}",
    ),
    _prose(
        "copy/add failed in build context",
        r"(?:COPY|ADD) failed:[^\n]*"
        r"(?:no such file or directory|file not found in build context)",
    ),
    # --- the container runtime ------------------------------------------------
    # docker's/containerd's own shape when the process it was told to start
    # is not in the image: `exec: "<binary>": executable file not found in
    # $PATH`. The bare sentence, outside that shape, is reported apart --
    # pytest, tox and any process spawner print it about a binary of theirs.
    _prose(
        "entrypoint executable not found",
        r"exec:[^\n]*executable file not found in \$PATH",
    ),
    _prose(
        "executable not found",
        r"^(?![^\n]*exec:[^\n]*executable file not found in \$PATH)"
        r"[^\n]*executable file not found",
    ),
    _prose("exec format error", r"exec format error"),
    # --- a tool's own report of the network, the registry, the runner --------
    _prose(
        "docker daemon unreachable",
        r"(?:cannot connect to the docker daemon|error during connect:)",
    ),
    _prose(
        "host unresolvable",
        rf"(?:{_CURL}|{_GIT_ACCESS}|{_APT_ERROR}|{_UV_ERROR})"
        r"[^\n]*could not resolve host",
    ),
    # apt names the host it could not resolve, which is framing enough; pip
    # and uv need their own error framing, since pip's `WARNING: Retrying`
    # says the same words about a problem it went on to recover from.
    _prose(
        "name resolution failure",
        r"(?:temporary failure resolving '[^'\n]+'"
        rf"|(?:{_APT_ERROR}|{_PIP_ERROR})[^\n]*temporary failure in name resolution"
        rf"|{_UV_ERROR}[^\n]*failed to lookup address)",
    ),
    # apt's and uv's own fetch failures. jest's `TypeError: Failed to fetch`
    # carries neither framing: no `E: ` at the head of a word, no trailing colon.
    _prose(
        "fetch failure",
        rf"(?:{_APT_ERROR}Failed to fetch\b|error: Failed to fetch:)",
    ),
    # apt's `Err:` detail line names the host and port it could not reach;
    # the other tools carry their own framing before the phrase.
    _prose(
        "connection timed out",
        rf"(?:{_APT_UNREACHABLE}[^\n]*connection timed out"
        rf"|(?:{_APT_ERROR}|{_CURL}|{_GIT_ACCESS}|{_UV_ERROR}|{_BUILDKIT})"
        rf"[^\n]*{_TIMED_OUT})",
    ),
    _prose(
        "registry rate limit",
        rf"(?:toomanyrequests:[^\n]*rate limit|{_BUILDKIT}[^\n]*toomanyrequests)",
    ),
    _prose(
        "service unavailable",
        rf"(?:{_BUILDKIT}|{_DAEMON}|{_CURL}|{_APT_ERROR}|{_UV_ERROR})"
        r"[^\n]*503 service unavailable",
    ),
    _prose(
        "disk full",
        rf"(?:{_BUILDKIT}|{_DAEMON}|/var/lib/docker[^\n]*?:)"
        r"[^\n]*no space left on device",
    ),
    _prose("runner shutdown", r"the runner has received a shutdown signal"),
    # --- assertions, in the shapes Python and pytest print them --------------
    _exact("assertion error", rf"^{_LINE_PREFIX}(?:E[ \t]+)?AssertionError: "),
    _exact(
        "pytest failed with assertion", rf"^{_LINE_PREFIX}FAILED \S+ - AssertionError"
    ),
    _exact("pytest bare assert", rf"^{_LINE_PREFIX}FAILED \S+ - assert "),
    _exact("pytest assert", rf"^{_LINE_PREFIX}E[ \t]+assert "),
    # --- any other exception line ----------------------------------------------
    _exact(
        "exception",
        rf"^{_LINE_PREFIX}(?:E[ \t]+)?(?!AssertionError\b){_EXCEPTION_NAME}: .+$",
    ),
    # --- the sentence every missing file prints -------------------------------
    # The exception-shaped lookahead keeps a Python `FileNotFoundError` to the
    # one `exception` observation it already gets, rather than reporting it
    # twice.
    _prose("no such file", rf"^(?!{_LINE_PREFIX}{_EXCEPTION_LINE})[^\n]*no such file"),
)
"""Every shape is tried against every piece of evidence; order is the order
the observations are reported in, and nothing else."""


@dataclass(frozen=True)
class FailureVerdict:
    """The verdict for one failure: a step, or a job with no itemised steps.

    ``evidence`` cites every block in which an observation matched, once, in
    pool order; ``observations`` name every matched line. ``kind`` is always
    ``None``: it keeps its key in the document, and this layer asserts no
    cause.
    """

    where: StepRef | int
    outcome: Outcome
    evidence: list[Evidence]
    observations: list[str]
    kind: None = None


@dataclass(frozen=True)
class RunDiagnosis:
    """The run summary: every verdict, one outcome, and run-level observations.

    ``causes`` is always empty: it keeps its key in the document, and this
    layer asserts no cause. ``observations`` are run-level: what is missing,
    or that the run exposed nothing to diagnose.
    """

    run: FailedRun
    failures: list[FailureVerdict]
    outcome: Outcome
    causes: list[str]
    observations: list[str]


@dataclass(frozen=True)
class _Match:
    shape: Observation
    evidence: Evidence
    line: str

    def describe(self) -> str:
        label = f"{self.shape.name}: {_for_operator(self.evidence, self.line)}"
        if _is_warning_shaped(self.evidence, self.line):
            return f"{WARNING_SHAPED_NOTE}: {label}"
        return label


def diagnose_run(snapshot: FailedRun) -> RunDiagnosis:
    """One verdict per failed step (job-level when none is itemised), summarised.

    Each verdict is judged against ITS OWN job's ``completeness``: a sibling
    job whose log could not be fetched does not speak for a job that was
    read completely, so what that job's read found is never lost to it
    (spec §4). The run summary still reports the gap.

    Precedence: evidence incomplete anywhere → ``EVIDENCE_UNAVAILABLE``; else
    ``UNCLASSIFIED``, including for a run that exposes nothing to diagnose.

    With zero kept jobs forge fetched nothing, so its worst-of reads
    ``unavailable``/``absent`` without anything having been lost; there only
    an ``error`` state counts as incompleteness.
    """
    failures = [
        read_failure(job, step, job.completeness)
        for job in snapshot.jobs
        for step in (job.steps or [None])
    ]
    lost = (
        _lost(snapshot.completeness)
        if not snapshot.jobs
        else _incomplete(snapshot.completeness)
    )
    if lost or any(v.outcome == "EVIDENCE_UNAVAILABLE" for v in failures):
        return RunDiagnosis(
            snapshot, failures, "EVIDENCE_UNAVAILABLE", [], _run_missing(snapshot)
        )
    observations = [] if failures else [_EMPTY_SET_NOTE]
    return RunDiagnosis(snapshot, failures, "UNCLASSIFIED", [], observations)


def read_failure(
    job: FailedJob, step: FailedStep | None, completeness: Completeness
) -> FailureVerdict:
    """Read one failure from its evidence pool: observations and citations.

    ``step=None`` is the job-level verdict for a job without kept steps.
    Every shape is tried against every piece of the pool; every matched line
    is an observation and every block with a match is cited. A step verdict
    that cites job-level evidence says so. Incompleteness names what could
    not be read, beside what the partial read DID show (spec §3: "markers
    already found are preserved as observations") — an unreadable run must
    not be made to look emptier than the part of it that was read.
    """
    where = step.ref if step is not None else job.job_id
    matches = [match for item in _evidence_pool(job, step) for match in _observe(item)]
    cited = list(dict.fromkeys(match.evidence for match in matches))
    observations = [match.describe() for match in matches]
    if step is not None and any(not isinstance(item.source, StepRef) for item in cited):
        observations.append(_JOB_LEVEL_NOTE)
    if _incomplete(completeness):
        return FailureVerdict(
            where, "EVIDENCE_UNAVAILABLE", cited, observations + _missing(completeness)
        )
    return FailureVerdict(
        where, "UNCLASSIFIED", cited, observations or [_NO_OBSERVATION_NOTE]
    )


def _evidence_pool(job: FailedJob, step: FailedStep | None) -> list[Evidence]:
    """What one verdict may cite.

    A step verdict sees the step's own blocks plus the job's evidence that is
    bound to no other step (``None`` and job-id sources are job-level; a block
    bound to a sibling step, green or not, is that step's fact). A job-level
    verdict sees everything the job has.
    """
    if step is None:
        return list(job.evidence)
    return step.evidence + [
        item
        for item in job.evidence
        if not isinstance(item.source, StepRef) or item.source == step.ref
    ]


def _observe(item: Evidence) -> list[_Match]:
    """Every shape against one piece of evidence: every matched line, once each."""
    return [
        _Match(shape, item, line)
        for shape in OBSERVATIONS
        for line in _matched_lines(shape, item)
    ]


def _matched_lines(shape: Observation, item: Evidence) -> list[str]:
    """Every line of ``item`` the shape matched: document order, once each.

    ALL of them, not the first: the citation is the block, so a verdict that
    named only the first match would report one missing file out of two and
    leave the operator to guess there was a second. Deduplicated by line
    text — a pattern can land twice inside one line, and an operator reads
    lines, not match offsets.
    """
    return list(
        dict.fromkeys(
            _line_at(item.text, hit.start())
            for hit in shape.pattern.finditer(item.text)
        )
    )


def _line_at(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start : end if end >= 0 else len(text)]


def _for_operator(item: Evidence, line: str) -> str:
    """One line of evidence as an operator should read it.

    An annotation's level is data, not text (`forge.Evidence`), so it is
    rendered back onto the line here -- at the one place a human reads it,
    and nowhere a shape can trip over it.
    """
    return line if item.level is None else f"{item.level}: {line}"


# One notion, two sources. A log line says it for itself: apt prefixes a
# recovered problem with `W: ` and a real one with `E: `, and compilers, pip
# and shell tooling write `warning: `/`notice: ` at the head of the line, in
# whatever case. A GitHub annotation instead carries a level of its own,
# which `forge` records as `Evidence.level`; it governs the whole message,
# whichever line matched, and being data no text can hide it. A log block
# has no level, so it is judged on the matched line itself, never on where
# the block happens to start. `failure`/`error`, any level this catalogue
# has not seen, and an unprefixed log line are not warning-shaped.
_LINE_WARNING_RE = re.compile(rf"^{_LINE_PREFIX}(?:W|warning|notice): ", re.IGNORECASE)
_NOTICED_LEVELS = frozenset({"warning", "notice"})


def _is_warning_shaped(item: Evidence, line: str) -> bool:
    """Whether `line`, matched inside `item`, was marked noticed, not fatal."""
    if item.level is not None:
        return item.level in _NOTICED_LEVELS
    return bool(_LINE_WARNING_RE.match(line))


def _incomplete(completeness: Completeness) -> bool:
    """Logs unreadable or annotations failed to fetch; ``absent`` is optional data."""
    return completeness.logs in ("unavailable", "error") or (
        completeness.annotations == "error"
    )


def _lost(completeness: Completeness) -> bool:
    """A fetch failed; the stricter test for a snapshot that fetched nothing."""
    return completeness.logs == "error" or completeness.annotations == "error"


def _run_missing(snapshot: FailedRun) -> list[str]:
    """What is missing, naming the job it is missing from.

    "logs fetch error" alone leaves the operator to guess which job of a
    matrix was not read. The run's own worst-of is the fallback: with no
    kept jobs there is nothing to name, and a hand-built snapshot may carry
    an aggregate no job accounts for.
    """
    per_job = [
        f"job {job.job_id}: {note}"
        for job in snapshot.jobs
        for note in _missing(job.completeness)
    ]
    return per_job or _missing(snapshot.completeness)


def _missing(completeness: Completeness) -> list[str]:
    missing: list[str] = []
    if completeness.logs == "unavailable":
        missing.append("logs unavailable")
    elif completeness.logs == "error":
        missing.append("logs fetch error")
    if completeness.annotations == "error":
        missing.append("annotations fetch error")
    return missing


_diagnosis_adapter: TypeAdapter[RunDiagnosis] = TypeAdapter(RunDiagnosis)


def render_verdict(diagnosis: RunDiagnosis) -> str:
    """Serialize a verdict document as versioned JSON (``deployer diagnose``).

    ``verdict_schema_version`` is the document's own schema version and is
    inserted as the first key; the nested ``run`` keeps its own
    ``snapshot_schema_version`` (``forge.py``) untouched.
    """
    payload = _diagnosis_adapter.dump_python(diagnosis, mode="json")
    document = {"verdict_schema_version": VERDICT_SCHEMA_VERSION, **payload}
    return json.dumps(document, indent=2) + "\n"
