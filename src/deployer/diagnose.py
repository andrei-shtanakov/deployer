"""Pure classification of a :class:`FailedRun` snapshot: verdicts with evidence.

No network, no I/O: the module is a function from the dataclasses ``forge.py``
produced to a verdict per failure. Three outcomes, kept deliberately distinct:
``CLASSIFIED`` (a rule established the cause and cites it), ``UNCLASSIFIED``
("I looked and do not know") and ``EVIDENCE_UNAVAILABLE`` ("I could not look").
Rules are data, every rule is evaluated against every piece of evidence, and a
conflict between kinds is reported as ambiguity, never resolved by order.
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
from deployer.models import FailureKind

Outcome = Literal["CLASSIFIED", "UNCLASSIFIED", "EVIDENCE_UNAVAILABLE"]

VERDICT_SCHEMA_VERSION = "1.0"

_JOB_LEVEL_NOTE = "cited evidence is job-level (no step binding)"
_NO_RULE_NOTE = "no rule matched"
_EMPTY_SET_NOTE = "failed run exposes no failed job or step"

# The shape of a Python exception line, optionally under pytest's ``E`` prefix.
_EXCEPTION_NAME = r"[A-Za-z_]\w*(?:\.\w+)*(?:Error|Exception)"
_EXCEPTION_LINE = rf"(?:E[ \t]+)?{_EXCEPTION_NAME}: "
# `docker build` frames every RUN-step output line as `#<step> <seconds> `
# (buildkit); diagnose, not forge, knows what docker is, so the framing is
# tolerated here, at the line-shape rules, not stripped at the source.
_LINE_PREFIX = r"[ \t]*(?:#\d+ \d+\.\d+ )?"
# Exception lines other than the assertion rules below: recorded as
# observations, never as a class (spec §5: wrong dependencies or a wrong
# invocation produce the same symptom as a project defect).
_EXCEPTION_LINE_RE = re.compile(
    rf"^{_LINE_PREFIX}(?:E[ \t]+)?((?!AssertionError\b){_EXCEPTION_NAME}: .+)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Rule:
    """One marker: the kind it establishes, a name to cite, and its pattern."""

    kind: FailureKind
    name: str
    pattern: re.Pattern[str]


def _prose(kind: FailureKind, name: str, pattern: str) -> Rule:
    """A rule over prose: case-insensitive, one line at a time."""
    return Rule(kind, name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))


def _exact(kind: FailureKind, name: str, pattern: str) -> Rule:
    """A rule over a machine-shaped line: case-sensitive, one line at a time."""
    return Rule(kind, name, re.compile(pattern, re.MULTILINE))


RULES: tuple[Rule, ...] = (
    _prose(FailureKind.AUTHORING, "dockerfile parse error", r"dockerfile parse error"),
    _prose(FailureKind.AUTHORING, "unknown instruction", r"unknown instruction"),
    _prose(
        FailureKind.AUTHORING,
        "copy/add source not found",
        r"failed to (?:solve|compute cache key)[^\n]*: not found",
    ),
    # A Python ``FileNotFoundError`` line is an exception observation, not a
    # Dockerfile/shell marker: the rule skips exception-shaped lines.
    _prose(
        FailureKind.AUTHORING,
        "no such file",
        rf"^(?!{_LINE_PREFIX}{_EXCEPTION_LINE})[^\n]*no such file",
    ),
    _prose(FailureKind.AUTHORING, "executable not found", r"executable file not found"),
    _prose(FailureKind.AUTHORING, "exec format error", r"exec format error"),
    _prose(FailureKind.AUTHORING, "unresolvable action", r"unable to resolve action"),
    _prose(
        FailureKind.AUTHORING,
        "unrecognized named-value",
        r"unrecognized named-value",
    ),
    _prose(
        FailureKind.ENVIRONMENT,
        "docker daemon unreachable",
        r"cannot connect to the docker daemon",
    ),
    _prose(FailureKind.ENVIRONMENT, "host unresolvable", r"could not resolve host"),
    _prose(
        FailureKind.ENVIRONMENT,
        "name resolution failure",
        r"temporary failure (?:resolving|in name resolution)",
    ),
    _prose(FailureKind.ENVIRONMENT, "fetch failure", r"failed to fetch"),
    _prose(FailureKind.ENVIRONMENT, "connection timed out", r"connection timed out"),
    _prose(FailureKind.ENVIRONMENT, "registry rate limit", r"toomanyrequests"),
    _prose(FailureKind.ENVIRONMENT, "service unavailable", r"503 service unavailable"),
    _prose(FailureKind.ENVIRONMENT, "disk full", r"no space left on device"),
    _prose(
        FailureKind.ENVIRONMENT,
        "runner shutdown",
        r"the runner has received a shutdown signal",
    ),
    _exact(
        FailureKind.PROJECT,
        "assertion error",
        rf"^{_LINE_PREFIX}(?:E[ \t]+)?AssertionError: ",
    ),
    _exact(
        FailureKind.PROJECT,
        "pytest failed with assertion",
        rf"^{_LINE_PREFIX}FAILED \S+ - AssertionError",
    ),
    _exact(
        FailureKind.PROJECT,
        "pytest bare assert",
        rf"^{_LINE_PREFIX}FAILED \S+ - assert ",
    ),
    _exact(FailureKind.PROJECT, "pytest assert", rf"^{_LINE_PREFIX}E[ \t]+assert "),
)
"""Every rule is evaluated against every piece of evidence; order is cosmetic."""


@dataclass(frozen=True)
class FailureVerdict:
    """The verdict for one failure: a step, or a job with no itemised steps."""

    where: StepRef | int
    outcome: Outcome
    kind: FailureKind | None
    evidence: list[Evidence]
    observations: list[str]


@dataclass(frozen=True)
class RunDiagnosis:
    """The run summary: every verdict, one outcome, the distinct causes.

    ``causes`` lists the kinds established by ``CLASSIFIED`` verdicts, sorted
    and deduplicated, and is kept whatever ``outcome`` says. ``observations``
    are run-level: what is missing, or that the run exposed nothing to
    diagnose.
    """

    run: FailedRun
    failures: list[FailureVerdict]
    outcome: Outcome
    causes: list[FailureKind]
    observations: list[str]


@dataclass(frozen=True)
class _Match:
    rule: Rule
    evidence: Evidence
    line: str

    def describe(self) -> str:
        return f"{self.rule.name}: {self.line}"


def diagnose_run(snapshot: FailedRun) -> RunDiagnosis:
    """One verdict per failed step (job-level when none is itemised), summarised.

    Each verdict is judged against ITS OWN job's ``completeness``: a sibling
    job whose log could not be fetched does not speak for a job that was
    read completely, so an established cause is never lost to it (spec §4).
    The run summary still reports the gap.

    Precedence: evidence incomplete anywhere → ``EVIDENCE_UNAVAILABLE``; else
    any unclassified failure, or nothing to diagnose at all → ``UNCLASSIFIED``;
    else ``CLASSIFIED``. The empty set is never vacuously classified. Whatever
    the outcome, ``causes`` keeps the kinds the ``CLASSIFIED`` verdicts
    established.

    With zero kept jobs forge fetched nothing, so its worst-of reads
    ``unavailable``/``absent`` without anything having been lost; there only
    an ``error`` state counts as incompleteness.
    """
    failures = [
        classify_failure(job, step, job.completeness)
        for job in snapshot.jobs
        for step in (job.steps or [None])
    ]
    observations: list[str] = []
    outcome: Outcome
    lost = (
        _lost(snapshot.completeness)
        if not snapshot.jobs
        else _incomplete(snapshot.completeness)
    )
    if lost or any(v.outcome == "EVIDENCE_UNAVAILABLE" for v in failures):
        outcome = "EVIDENCE_UNAVAILABLE"
        observations = _run_missing(snapshot)
    elif not failures or any(v.outcome == "UNCLASSIFIED" for v in failures):
        outcome = "UNCLASSIFIED"
        if not failures:
            observations = [_EMPTY_SET_NOTE]
    else:
        outcome = "CLASSIFIED"
    causes = sorted(
        {v.kind for v in failures if v.outcome == "CLASSIFIED" and v.kind},
        key=lambda kind: kind.value,
    )
    return RunDiagnosis(snapshot, failures, outcome, causes, observations)


def classify_failure(
    job: FailedJob, step: FailedStep | None, completeness: Completeness
) -> FailureVerdict:
    """Classify one failure from its evidence pool; never from rule order.

    ``step=None`` is the job-level verdict for a job without kept steps. All
    matches are collected first; then incompleteness wins over any marker,
    two distinct kinds are ambiguity, one kind is a class with its citations,
    and none is ``UNKNOWN`` with whatever was observed. A marker that landed
    on warning-shaped evidence establishes nothing and is carried through as
    an observation, whatever the outcome.
    """
    where = step.ref if step is not None else job.job_id
    pool = _evidence_pool(job, step)
    per_item = [_matches(item) for item in pool]
    matches = [match for found, _ in per_item for match in found]
    warnings = [
        f"{WARNING_SHAPED_NOTE}: {match.describe()}"
        for _, warned in per_item
        for match in warned
    ]
    exceptions = [
        f"exception: {line}"
        for item in pool
        for line in _EXCEPTION_LINE_RE.findall(item.text)
    ]
    if _incomplete(completeness):
        found = [match.describe() for match in matches]
        return FailureVerdict(
            where,
            "EVIDENCE_UNAVAILABLE",
            None,
            [],
            found + warnings + _missing(completeness),
        )
    kinds = sorted({match.rule.kind for match in matches}, key=lambda k: k.value)
    if len(kinds) >= 2:
        return FailureVerdict(
            where,
            "UNCLASSIFIED",
            FailureKind.UNKNOWN,
            [],
            [_ambiguity(matches, kinds), *warnings, *exceptions],
        )
    if len(kinds) == 1:
        cited = list(dict.fromkeys(match.evidence for match in matches))
        observations = [match.describe() for match in matches] + warnings + exceptions
        if step is not None and any(
            not isinstance(item.source, StepRef) for item in cited
        ):
            observations.append(_JOB_LEVEL_NOTE)
        return FailureVerdict(where, "CLASSIFIED", kinds[0], cited, observations)
    return FailureVerdict(
        where,
        "UNCLASSIFIED",
        FailureKind.UNKNOWN,
        [],
        warnings + exceptions or [_NO_RULE_NOTE],
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


# One notion, two sources. apt prefixes a recovered problem with `W: ` and a
# real one with `E: `, per line inside a log block; and `forge._build_job`
# renders a job annotation as `<level>: <message>`, so a whole block reading
# `warning: `/`notice: ` is GitHub saying it noticed something, while
# `error: `/`failure: ` (and an unprefixed log line) are failure evidence.
# Because in practice every step's evidence pool is the whole job log (all
# real citations are job-level), one warning-shaped line was enough to pair
# with a genuine AUTHORING marker and turn a clean verdict into `ambiguous:`,
# or to classify ENVIRONMENT off a retry that succeeded. A warning is not the
# failure whatever it mentions — `warning: no such file optional-cache.json;
# continuing without cache` says the build carried on — so NO rule of any
# kind may establish a class off such a match; every kind keeps it as an
# observation. (Round 2 applied this to ENVIRONMENT rules only, which left
# that annotation reading as AUTHORING and exiting 0 on a class nobody had
# evidence for.) The first mitigation for
# todo://deployer/diagnose-rule-catalogue-precision.
_APT_WARNING_RE = re.compile(rf"^{_LINE_PREFIX}W: ")
_ANNOTATION_WARNING_RE = re.compile(r"^(?:warning|notice): ")

WARNING_SHAPED_NOTE = "warning-shaped"


def _is_warning_shaped(text: str, line: str) -> bool:
    """Whether `line` of the evidence `text` reports a noticed, not fatal, problem.

    Two shapes, because the two sources scope differently: apt's `W: ` marks
    the one line it prefixes, an annotation level opens the whole block.
    """
    return bool(_APT_WARNING_RE.match(line) or _ANNOTATION_WARNING_RE.match(text))


def _matches(item: Evidence) -> tuple[list[_Match], list[_Match]]:
    """Every rule against one piece of evidence: (established, warning-shaped).

    A rule of ANY kind keeps looking past a match on warning-shaped evidence,
    and the first one it passed over is returned separately so the verdict can
    observe it without citing it.
    """
    found: list[_Match] = []
    warned: list[_Match] = []
    for rule in RULES:
        skipped: _Match | None = None
        for hit in rule.pattern.finditer(item.text):
            line = _line_at(item.text, hit.start())
            if _is_warning_shaped(item.text, line):
                skipped = skipped or _Match(rule, item, line)
                continue
            found.append(_Match(rule, item, line))
            break
        else:
            if skipped is not None:
                warned.append(skipped)
    return found, warned


def _line_at(text: str, index: int) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start : end if end >= 0 else len(text)]


def _ambiguity(matches: list[_Match], kinds: list[FailureKind]) -> str:
    parts: list[str] = []
    for kind in kinds:
        markers = "; ".join(m.describe() for m in matches if m.rule.kind is kind)
        parts.append(f"{kind.value} [{markers}]")
    return "ambiguous: " + " vs ".join(parts)


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
