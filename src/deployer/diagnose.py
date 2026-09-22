"""Pure classification of a :class:`FailedRun` snapshot: verdicts with evidence.

No network, no I/O: the module is a function from the dataclasses ``forge.py``
produced to a verdict per failure. Three outcomes, kept deliberately distinct:
``CLASSIFIED`` (a rule established the cause and cites it), ``UNCLASSIFIED``
("I looked and do not know") and ``EVIDENCE_UNAVAILABLE`` ("I could not look").
Rules are data, every rule is evaluated against every piece of evidence, and a
conflict between kinds is reported as ambiguity, never resolved by order.

What a rule may say, and what only ``SYMPTOMS`` may say, is fixed by the
owner's unified rule, quoted verbatim above ``RULES``.
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
SYMPTOM_NOTE = "symptom"

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


def _symptom(name: str, pattern: str) -> Rule:
    """A marker that names a SYMPTOM, not a cause: observed, never classified.

    The kind is ``UNKNOWN`` — the one ``CLASSIFIED`` never admits — so a
    symptom cannot establish a class even if it is read by mistake.
    """
    return Rule(
        FailureKind.UNKNOWN, name, re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    )


# The unified rule this catalogue is audited against (owner, 2026-09-22):
#
# - The LEVEL of a message decides how evidence is weighed (warning/notice →
#   observation only), never establishes a cause.
# - AUTHORING requires POSITIVE evidence of a defect in the AUTHORED ARTIFACT
#   (Dockerfile / workflow / image entrypoint). A bare `no such file` is a
#   SYMPTOM — project, environment or invocation can all produce it — and is
#   insufficient.
# - Every positive rule has a NEGATIVE TWIN: the same marker text in a context
#   with a different cause must NOT yield the class.
#
# So each AUTHORING rule below matches a shape only the tool that owns the
# artifact prints: buildkit's and docker's own COPY/ADD failures, the runner's
# own workflow-YAML errors, the container runtime's own `exec:` line. The
# symptoms those failures share with every other cause live in `SYMPTOMS`.
_QUOTED_PATH_NOT_FOUND = r'[^\n]*"[^"\n]*": not found[ \t]*$'

RULES: tuple[Rule, ...] = (
    _prose(FailureKind.AUTHORING, "dockerfile parse error", r"dockerfile parse error"),
    _prose(FailureKind.AUTHORING, "unknown instruction", r"unknown instruction"),
    # buildkit's own shape for a COPY/ADD source missing from the build
    # context: it names the path it could not find, quoted, at the line's end.
    # `failed to solve` alone heads every buildkit failure, a RUN step that
    # exited non-zero included, so the quoted path is what makes the line an
    # artifact defect rather than a symptom of one.
    _prose(
        FailureKind.AUTHORING,
        "copy/add source not found",
        rf"failed to (?:solve|compute cache key){_QUOTED_PATH_NOT_FOUND}",
    ),
    # docker's own shape for the same defect (classic builder, and buildkit
    # when it reports the stat behind the failure).
    _prose(
        FailureKind.AUTHORING,
        "copy/add failed in build context",
        r"(?:COPY|ADD) failed:[^\n]*"
        r"(?:no such file or directory|file not found in build context)",
    ),
    _prose(FailureKind.AUTHORING, "unresolvable action", r"unable to resolve action"),
    _prose(
        FailureKind.AUTHORING,
        "unrecognized named-value",
        r"unrecognized named-value",
    ),
    # docker's/containerd's own shape when the image's CMD/ENTRYPOINT names a
    # binary the image does not carry: `exec: "<binary>": executable file not
    # found in $PATH`. Both halves on one line, because the sentence alone is
    # printed by anything that spawns a process.
    _prose(
        FailureKind.AUTHORING,
        "entrypoint executable not found",
        r"exec:[^\n]*executable file not found in \$PATH",
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

SYMPTOMS: tuple[Rule, ...] = (
    # A missing file is what a defect in the artifact, in the project, in the
    # environment and in the invocation all look like from the outside. The
    # exception-shaped lookahead keeps a Python `FileNotFoundError` to the one
    # `exception:` observation it already gets, rather than reporting it twice.
    _symptom(
        "no such file",
        rf"^(?!{_LINE_PREFIX}{_EXCEPTION_LINE})[^\n]*no such file",
    ),
    # An amd64 image on an arm64 runner is an environment mismatch as readily
    # as a wrong `--platform` in the Dockerfile.
    _symptom("exec format error", r"exec format error"),
    # The bare sentence, outside the `exec:` shape above: pytest, tox and any
    # process spawner print it about a binary that is nobody's entrypoint.
    _symptom(
        "executable not found",
        r"^(?![^\n]*exec:[^\n]*executable file not found in \$PATH)"
        r"[^\n]*executable file not found",
    ),
)
"""Markers that name a SYMPTOM, never a cause (the second bullet above).

They are NOT classifying: they are reported as ``symptom: <name>: <line>``
observations so the operator still sees what the run printed, and they take
no part in the ambiguity check — a symptom beside an established cause is a
detail of that failure, not a second kind competing with it.
"""


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
        return f"{self.rule.name}: {_for_operator(self.evidence, self.line)}"


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
        f"exception: {_for_operator(item, line)}"
        for item in pool
        for line in _EXCEPTION_LINE_RE.findall(item.text)
    ]
    symptoms = [
        f"{SYMPTOM_NOTE}: {match.describe()}"
        for item in pool
        for match in _symptom_matches(item)
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
            [_ambiguity(matches, kinds), *warnings, *exceptions, *symptoms],
        )
    if len(kinds) == 1:
        cited = list(dict.fromkeys(match.evidence for match in matches))
        observations = (
            [match.describe() for match in matches] + warnings + exceptions + symptoms
        )
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
        warnings + exceptions + symptoms or [_NO_RULE_NOTE],
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


# One notion, two sources. A log line says it for itself: apt prefixes a
# recovered problem with `W: ` and a real one with `E: `, and compilers, pip
# and shell tooling write `warning: `/`notice: ` at the head of the line.
# A GitHub annotation instead carries a level of its own, which `forge`
# records as `Evidence.level` (round 7). So `warning`/`notice`, as a level
# or as a line's own prefix, is a noticed problem, while `failure`/`error`
# — and any level this catalogue has not seen, and an unprefixed log line
# — are failure evidence.
# Because in practice every step's evidence pool is the whole job log (all
# real citations are job-level), one warning-shaped line was enough to pair
# with a genuine AUTHORING marker and turn a clean verdict into `ambiguous:`,
# or to classify ENVIRONMENT off a retry that succeeded. A warning is not the
# failure whatever it mentions — `no such file optional-cache.json;
# continuing without cache` says the build carried on — so NO rule of any
# kind may establish a class off such a match; every kind keeps it as an
# observation. (Round 2 applied this to ENVIRONMENT rules only, which left
# that annotation reading as AUTHORING and exiting 0 on a class nobody had
# evidence for.)
#
# The two sources are judged differently because they are differently
# shaped. An annotation's level governs its WHOLE message, whichever line a
# rule matched — and being data, no text can hide it: rounds 5 and 6 read
# the level off the text, so a `warning: ` prefix on line 1 only, and then an
# empty first line, each let a later line establish a class the run had no
# evidence for. A log block has no level at all, so it is judged on the
# matched line itself, never on where the block happens to start: a
# warning-prefixed line mid-block is warning-shaped on its own, and a block
# that DOES open with one must not blanket-exclude a genuine marker on a
# later line (round 5, finding 1). Round 7 first dropped the prose prefixes
# from the line rule as an artefact of forge's old rendering; that held for
# annotations and was wrong about logs, whose own `warning: ` lines predate
# and outlive any rendering of ours.
# The first mitigation for todo://deployer/diagnose-rule-catalogue-precision.
_LINE_WARNING_RE = re.compile(rf"^{_LINE_PREFIX}(?:W|warning|notice): ")
_NOTICED_LEVELS = frozenset({"warning", "notice"})

WARNING_SHAPED_NOTE = "warning-shaped"


def _is_warning_shaped(item: Evidence, line: str) -> bool:
    """Whether `line`, matched inside `item`, reports a noticed, not fatal,
    problem.

    An annotation (`item.level` is not None) is judged by its level alone,
    for every line of its message. A log block carries no level, so it is
    judged per matched line instead: apt's `W: `, or a `warning: `/`notice: `
    the tool wrote itself, at the head of that one line.
    """
    if item.level is not None:
        return item.level in _NOTICED_LEVELS
    return bool(_LINE_WARNING_RE.match(line))


def _for_operator(item: Evidence, line: str) -> str:
    """One line of evidence as an operator should read it.

    An annotation's level is data, not text (`forge.Evidence`), so it is
    rendered back onto the line here -- at the one place a human reads it,
    and nowhere a rule can trip over it.
    """
    return line if item.level is None else f"{item.level}: {line}"


def _matches(item: Evidence) -> tuple[list[_Match], list[_Match]]:
    """Every rule against one piece of evidence: (established, warning-shaped).

    A rule of ANY kind keeps looking past a match it judged warning-shaped,
    and the first one it passed over is returned separately so the verdict can
    observe it without citing it.
    """
    found: list[_Match] = []
    warned: list[_Match] = []
    for rule in RULES:
        skipped: _Match | None = None
        for hit in rule.pattern.finditer(item.text):
            line = _line_at(item.text, hit.start())
            if _is_warning_shaped(item, line):
                skipped = skipped or _Match(rule, item, line)
                continue
            found.append(_Match(rule, item, line))
            break
        else:
            if skipped is not None:
                warned.append(skipped)
    return found, warned


def _symptom_matches(item: Evidence) -> list[_Match]:
    """The symptom markers one piece of evidence carries: observed, not cited.

    Unlike :func:`_matches` these establish nothing, so the warning shape
    changes nothing about them either: the rendered line carries its own
    ``W: `` prefix or annotation level for the operator to read.
    """
    found: list[_Match] = []
    for rule in SYMPTOMS:
        hit = rule.pattern.search(item.text)
        if hit is not None:
            found.append(_Match(rule, item, _line_at(item.text, hit.start())))
    return found


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
