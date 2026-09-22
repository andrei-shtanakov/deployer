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
    """One marker: the kind it establishes, a name to cite, and its pattern.

    ``endpoint`` marks the rules about an UNREACHABLE ENDPOINT, which carry
    one more requirement than their pattern: the line must name a host the
    tool reaches by default (owner's check (1) above ``RULES``).
    """

    kind: FailureKind
    name: str
    pattern: re.Pattern[str]
    endpoint: bool = False


def _prose(kind: FailureKind, name: str, pattern: str) -> Rule:
    """A rule over prose: case-insensitive, one line at a time."""
    return Rule(kind, name, re.compile(pattern, re.IGNORECASE | re.MULTILINE))


def _exact(kind: FailureKind, name: str, pattern: str) -> Rule:
    """A rule over a machine-shaped line: case-sensitive, one line at a time."""
    return Rule(kind, name, re.compile(pattern, re.MULTILINE))


def _endpoint(name: str, pattern: str) -> Rule:
    """An ENVIRONMENT rule whose line must also name a known infra host.

    Always ``ENVIRONMENT``: the four rules that read "the endpoint could not
    be reached" are the only ones a misconfigured ADDRESS can forge.
    """
    return Rule(
        FailureKind.ENVIRONMENT,
        name,
        re.compile(pattern, re.IGNORECASE | re.MULTILINE),
        endpoint=True,
    )


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
# Sharpened by the owner on the same day: a CONTAINER-RUNTIME shape is not by
# itself an artifact defect. `exec: "x": executable file not found in $PATH`
# proves that a missing executable was asked for, not that its name came from
# the authored CMD/ENTRYPOINT — the command is overridable at run time — and
# `unable to resolve action` is printed just as faithfully for a reference that
# was valid when the workflow was written and whose upstream tag has since been
# deleted. Where the snapshot cannot tell the causes apart, it observes: both
# shapes live in `SYMPTOMS` now.
#
# Generalised by the owner (2026-09-22, bounded deterministic pass) into the
# rule the whole catalogue now answers to:
#
#   A CLASS IS ESTABLISHED ONLY WHERE THE SHAPE OF THE EVIDENCE TIES THE
#   MESSAGE TO ITS CAUSE. Without sufficient provenance the honest answer is
#   an observation and UNCLASSIFIED — and a classification known to fire on
#   the wrong cause is a WRONG DIAGNOSIS, not a limitation to be pinned and
#   shipped.
#
# So the catalogue is NOT widened to keep a live scenario green: live
# acceptance run 1 (a COPY of a path that is not in the build context) reads
# UNCLASSIFIED now, and the expectation was corrected rather than the rule.
# What the rule costs each kind:
#
# - AUTHORING keeps only what the tool that OWNS the artifact says about the
#   artifact's own text. `dockerfile parse error`: the parser read the
#   Dockerfile's bytes and could not — no environment state, no project code
#   and no invocation can make a well-formed instruction unparseable.
#   `unrecognized named-value`: the same, for the runner's own expression
#   parser over the workflow YAML (a missing secret VALUE is a different
#   message — the expression evaluates to empty; this one says the NAME is
#   not in the language), and it demands GitHub's own framing on the SAME
#   line, because a pytest assertion can quote the phrase without being the
#   validator. `unknown instruction` is a SYMPTOM outright: it is ordinary
#   English about a vocabulary, and `ValueError: unknown instruction:
#   frobnicate` has never seen a Dockerfile. A `dockerfile parse error` line
#   that carries those words therefore establishes nothing either — the words
#   are what an application would be quoting, and the parser's framing beside
#   them proves only that both sentences share a line.
# - ENVIRONMENT matches only a line carrying a TOOL'S OWN framing: apt's
#   `E: `, curl's `curl: (N)`, git's `fatal: unable to access '...':`, uv's
#   error chain, buildkit's `failed to solve:`, the docker daemon's own
#   reply, the registry's `toomanyrequests:`, the runner's own sentence. The
#   BARE phrases — `connection timed out`, `503 Service Unavailable`, `no
#   space left on device`, `temporary failure in name resolution`, `could not
#   resolve host` — are printed verbatim by any application under test that
#   exercises a retry path, so on their own they are symptoms. Each
#   alternative below is named for the tool it was taken from.
# - PROJECT needs PROVENANCE beside the assertion (`_has_project_provenance`).
#   The rules match the assertion shapes exactly as before; `classify_failure`
#   is what refuses to call it a class when the same piece of evidence does
#   not say WHOSE assertion failed.
#
# Sharpened again by the owner on 2026-09-22, in two checks this catalogue
# now answers to, quoted verbatim:
#
#   (1) Tool framing (curl/apt/docker) establishes the SOURCE of a message,
#       not its cause: a wrong address from configuration also yields a
#       network error.
#   (2) A path inside the checkout establishes the LOCATION of an assertion,
#       not project ownership: a CI setup script can live there.
#
# What (1) costs ENVIRONMENT: the four UNREACHABLE-ENDPOINT rules -- `host
# unresolvable`, `name resolution failure`, `connection timed out`, `fetch
# failure` -- keep their tool framing AND additionally require the endpoint
# named on the line to be a KNOWN INFRASTRUCTURE host the tool reaches by
# default (`KNOWN_INFRA_HOSTS`). `curl: (6) Could not resolve host:
# pypi.invalid` is curl saying curl could not resolve it, and a typo in an
# index URL prints it exactly as a broken resolver does; against `pypi.org`
# there is no address left to have got wrong. A custom mirror, a private
# address, `localhost`, an `.invalid` name -- and a line that names no
# endpoint at all -- become `symptom:` observations, and the verdict is
# UNCLASSIFIED. The rules where the infrastructure itself ANSWERED or failed
# keep only their framing, because there is no endpoint to misconfigure:
# `docker daemon unreachable` (the local socket), `runner shutdown` (the
# runner about itself), `disk full` (the daemon's own storage path),
# `registry rate limit` (the registry's own refusal) and `service
# unavailable` (a 503 a tool read, whoever it was talking to). The price is
# stated: a genuinely unreachable custom mirror now goes unnamed, which is
# live acceptance run 2 -- and that is the cost of never naming a
# `sources.list` line as the runner's network.
#
# What (2) costs PROJECT: a frame or node id under a CI-HARNESS directory
# (`.github/`, `.ci/`, `ci/`, `scripts/ci/` as path components) is where the
# assertion RAN, not whose it was -- the runner's own setup script lives in
# the checkout and asserts its preconditions there. A test vendored INSIDE
# `tests/` stays PROJECT: the class names the tree that was built and tested,
# and the project chose to carry that file.
#
# The symptoms these failures share with every other cause live in `SYMPTOMS`
# — including, since this pass, both COPY/ADD shapes. The build context IS
# the checkout the Dockerfile was authored against (spec §6.4 scenario A), so
# the line is a genuine mismatch between the instruction and its context; but
# a path the COPY never had right and a file the project moved AFTER the
# Dockerfile was authored print the identical line, and the snapshot cannot
# say which side moved. Naming AUTHORING there would be right by luck.
_QUOTED_PATH_NOT_FOUND = r'[^\n]*"[^"\n]*": not found[ \t]*$'

# The endpoints an unreachable-endpoint rule may rest on: the infrastructure
# a build tool reaches BY DEFAULT, so that nothing a project configures could
# have made the address wrong. Deliberately short -- every name added here is
# a claim that reaching it is the environment's job, not the artifact's.
KNOWN_INFRA_HOSTS = frozenset(
    {
        "deb.debian.org",
        "security.debian.org",
        "archive.ubuntu.com",
        "security.ubuntu.com",
        "ports.ubuntu.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry-1.docker.io",
        "docker.io",
        "index.docker.io",
        "auth.docker.io",
        "production.cloudflare.docker.com",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "astral.sh",
        "pypi.python.org",
    }
)

# A dotted name as a log line carries it: the host of a URL, or a bare
# `host[:port]`. The lookbehind keeps the tail of a longer name (or of a
# path component) from being read as a host of its own.
_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9._-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
)


def _names_infra_host(line: str) -> bool:
    """Whether the line names an endpoint the tool reaches by default.

    A simple SUFFIX rule over the listed names: `pypi.org` and any subdomain
    of it qualify, while `notgithub.com` and `github.com.elsewhere.test` are
    other hosts entirely. Only subdomains OF A LISTED NAME count, which is
    why the list spells out `objects.githubusercontent.com` rather than
    trusting the parent domain.
    """
    return any(
        host == name or host.endswith(f".{name}")
        for host in (match.group(0).lower() for match in _HOST_RE.finditer(line))
        for name in KNOWN_INFRA_HOSTS
    )


# The tool framings the ENVIRONMENT rules require, each named for the tool
# that prints it. They are line fragments, never anchored to the line's head:
# buildkit frames a step's output as `#N t.ttt ` and then reprints the failing
# lines a second time, under `------`, with a bare `t.ttt ` instead.
_APT_ERROR = r"(?:^|[ \t])E: "
_APT_UNREACHABLE = r"could not connect to \S+?:\d+"
_CURL = r"curl: \(\d+\)"
_GIT_ACCESS = r"fatal: unable to access '[^'\n]*':"
_PIP_ERROR = r"(?:^|[ \t])ERROR: "
_UV_ERROR = r"(?:error: Failed to fetch:|Caused by:)"
_BUILDKIT = r"failed to solve:"
_DAEMON = r"(?:Error response from daemon:|error during connect:)"
_TIMED_OUT = r"(?:connection timed out|operation timed out|i/o timeout)"

RULES: tuple[Rule, ...] = (
    # The Dockerfile parser's own sentence -- no other tool prints it -- but
    # never on a line that also carries `unknown instruction`: those words
    # are a symptom now, and a line holding both proves only that the two
    # sentences share a line.
    _prose(
        FailureKind.AUTHORING,
        "dockerfile parse error",
        r"dockerfile parse error(?![^\n]*unknown instruction)",
    ),
    # Anchored to the validator's own line: GitHub prints "The workflow is
    # not valid." and/or the failing `.github/workflows/...` path on the
    # SAME line as "Unrecognized named-value" -- an assertion or a third
    # party tool that merely mentions the phrase carries neither.
    _prose(
        FailureKind.AUTHORING,
        "unrecognized named-value",
        r"(?:workflow is not valid|\.github/workflows/)[^\n]*unrecognized named-value",
    ),
    # docker's own reply when its socket is not answering.
    _prose(
        FailureKind.ENVIRONMENT,
        "docker daemon unreachable",
        r"(?:cannot connect to the docker daemon|error during connect:)",
    ),
    # curl, git, apt or uv saying DNS failed, about a host they reach by
    # default. The bare sentence is a symptom (an offline-probe step prints
    # it on purpose), and so is the framed sentence about an endpoint the
    # configuration chose: a typo in an index URL does not resolve either.
    _endpoint(
        "host unresolvable",
        rf"(?:{_CURL}|{_GIT_ACCESS}|{_APT_ERROR}|{_UV_ERROR})"
        r"[^\n]*could not resolve host",
    ),
    # apt names the host it could not resolve, which is framing enough; pip
    # and uv need their own error framing, since pip's `WARNING: Retrying`
    # says the same words about a problem it went on to recover from. The
    # named host must still be one of `KNOWN_INFRA_HOSTS`.
    _endpoint(
        "name resolution failure",
        r"(?:temporary failure resolving '[^'\n]+'"
        rf"|(?:{_APT_ERROR}|{_PIP_ERROR})[^\n]*temporary failure in name resolution"
        rf"|{_UV_ERROR}[^\n]*failed to lookup address)",
    ),
    # apt's and uv's own fetch failures, against a default index or mirror.
    # `TypeError: Failed to fetch` -- jest's message, the over-firer recorded
    # in TODO.md -- carries neither framing: no `E: ` at the head of a word,
    # and no trailing colon. `error: Failed to fetch:
    # \`https://mirror.corp/simple/x/\`` carries uv's framing and fails the
    # host check instead.
    _endpoint(
        "fetch failure",
        rf"(?:{_APT_ERROR}Failed to fetch\b|error: Failed to fetch:)",
    ),
    # apt's `Err:` detail line names the host and port it could not reach;
    # the other tools carry their own framing before the phrase. Live
    # acceptance run 2 -- apt against `10.255.255.1` -- is framed and fails
    # the host check: an unroutable private address is what a `sources.list`
    # line chose, and a curl timeout naming no endpoint at all is no better.
    _endpoint(
        "connection timed out",
        rf"(?:{_APT_UNREACHABLE}[^\n]*connection timed out"
        rf"|(?:{_APT_ERROR}|{_CURL}|{_GIT_ACCESS}|{_UV_ERROR}|{_BUILDKIT})"
        rf"[^\n]*{_TIMED_OUT})",
    ),
    # The registry's own refusal, or buildkit reporting it.
    _prose(
        FailureKind.ENVIRONMENT,
        "registry rate limit",
        rf"(?:toomanyrequests:[^\n]*rate limit|{_BUILDKIT}[^\n]*toomanyrequests)",
    ),
    # An upstream 503 as a TOOL read it, not as an app's fixture printed it.
    _prose(
        FailureKind.ENVIRONMENT,
        "service unavailable",
        rf"(?:{_BUILDKIT}|{_DAEMON}|{_CURL}|{_APT_ERROR}|{_UV_ERROR})"
        r"[^\n]*503 service unavailable",
    ),
    # buildkit, the daemon, or the daemon's own storage path: a test writing
    # to a deliberately tiny tmpfs prints the bare sentence and nothing else.
    _prose(
        FailureKind.ENVIRONMENT,
        "disk full",
        rf"(?:{_BUILDKIT}|{_DAEMON}|/var/lib/docker[^\n]*?:)"
        r"[^\n]*no space left on device",
    ),
    # The runner's own sentence about itself.
    _prose(
        FailureKind.ENVIRONMENT,
        "runner shutdown",
        r"the runner has received a shutdown signal",
    ),
    # The assertion shapes. Each matches as it always did; none of them
    # establishes PROJECT unless the SAME piece of evidence also carries a
    # frame or node id pointing into the checkout (`_has_project_provenance`,
    # applied in `classify_failure`).
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
    # buildkit's own shape for a COPY/ADD source missing from the build
    # context (the path it could not find, quoted, at the line's end), and
    # docker's shape for the same. AUTHORING until this pass: the build
    # context IS the checkout, so the line is a real mismatch between the
    # instruction and its context -- but a path the COPY never had right and
    # a file the project moved AFTER the Dockerfile was authored print the
    # identical line. The snapshot cannot say which side moved, so it says
    # what it saw. Listed before `no such file` so the operator reads the
    # builder's own shape first.
    _symptom(
        "copy/add source not found",
        rf"failed to (?:solve|compute cache key){_QUOTED_PATH_NOT_FOUND}",
    ),
    _symptom(
        "copy/add failed in build context",
        r"(?:COPY|ADD) failed:[^\n]*"
        r"(?:no such file or directory|file not found in build context)",
    ),
    # Demoted with them: `unknown instruction` is ordinary English about a
    # vocabulary, and an application says it about its own -- the parser's
    # framing on the same line proves only that both sentences share a line.
    _symptom("unknown instruction", r"unknown instruction"),
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
    # docker's/containerd's own shape when the process it was told to start is
    # not in the image: `exec: "<binary>": executable file not found in $PATH`.
    # It was an AUTHORING rule until the owner's ruling of 2026-09-22: the line
    # names the binary, never WHO named it, and `docker run --entrypoint`, a
    # job's `container.options`, a compose `command:` and `kubectl run --` all
    # override the image's CMD/ENTRYPOINT with a name of their own. Kept as its
    # own symptom, apart from the bare sentence below, because the shape still
    # tells the operator it was the container runtime that failed to start.
    _symptom(
        "entrypoint executable not found",
        r"exec:[^\n]*executable file not found in \$PATH",
    ),
    # The bare sentence, outside the `exec:` shape above: pytest, tox and any
    # process spawner print it about a binary that is nobody's entrypoint.
    _symptom(
        "executable not found",
        r"^(?![^\n]*exec:[^\n]*executable file not found in \$PATH)"
        r"[^\n]*executable file not found",
    ),
    # The runner could not fetch a `uses:` reference. A typo in the authored
    # workflow prints it; so does a tag, branch or whole repository the
    # upstream deleted after the workflow was written, and so does a private
    # action the token may no longer read. Demoted 2026-09-22 with the `exec:`
    # shape above, for the same reason: the snapshot carries nothing that tells
    # a defect in the YAML from a change on the other side of the reference.
    _symptom("unresolvable action", r"unable to resolve action"),
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

    A PROJECT match is demoted the same way when the piece of evidence it
    landed on carries no provenance for the assertion (see
    :func:`_has_project_provenance`): an assertion that does not say whose
    it was establishes nothing, and cannot make an unrelated cause ambiguous
    either. An unreachable-endpoint match against a host outside
    ``KNOWN_INFRA_HOSTS`` is demoted to a ``symptom:`` observation by
    :func:`_matches` for the same reason, and is likewise not a competing
    kind.

    Every observation names the LINE it was read from, and every matched line
    gets one, so a cited block never hides a second match behind its first.
    ``EVIDENCE_UNAVAILABLE`` keeps all four kinds of observation beside the
    note of what could not be read (spec §3: "markers already found are
    preserved as observations") — an unreadable run must not be made to look
    emptier than the part of it that WAS read.
    """
    where = step.ref if step is not None else job.job_id
    pool = _evidence_pool(job, step)
    per_item = [_matches(item) for item in pool]
    matches, unprovenanced = _partition_by_provenance(
        [match for found, _, _ in per_item for match in found]
    )
    demoted = [
        f"{WARNING_SHAPED_NOTE}: {match.describe()}"
        for _, warned, _ in per_item
        for match in warned
    ] + [f"{NO_PROVENANCE_NOTE}: {match.describe()}" for match in unprovenanced]
    exceptions = [
        f"exception: {_for_operator(item, line)}"
        for item in pool
        for line in _EXCEPTION_LINE_RE.findall(item.text)
    ]
    symptoms = [
        f"{SYMPTOM_NOTE}: {match.describe()}"
        for _, _, off_infra in per_item
        for match in off_infra
    ] + [
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
            found + demoted + exceptions + symptoms + _missing(completeness),
        )
    kinds = sorted({match.rule.kind for match in matches}, key=lambda k: k.value)
    if len(kinds) >= 2:
        return FailureVerdict(
            where,
            "UNCLASSIFIED",
            FailureKind.UNKNOWN,
            [],
            [_ambiguity(matches, kinds), *demoted, *exceptions, *symptoms],
        )
    if len(kinds) == 1:
        cited = list(dict.fromkeys(match.evidence for match in matches))
        observations = (
            [match.describe() for match in matches] + demoted + exceptions + symptoms
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
        demoted + exceptions + symptoms or [_NO_RULE_NOTE],
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
#
# Case-insensitive (owner finding, 2026-09-22, final review round): a tool
# that writes `WARNING:` (all caps, the shape several CI actions use) or
# `Warning:` defeated a case-sensitive prefix just as completely as no
# prefix at all, and a recovered problem behind it established a class.
_LINE_WARNING_RE = re.compile(rf"^{_LINE_PREFIX}(?:W|warning|notice): ", re.IGNORECASE)
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


# PROJECT's provenance (owner's evidence rule, 2026-09-22). `AssertionError:
# 1 != 2` is printed by the project's own suite, by a CI setup script the
# runner invokes with `python -c`, and by an installed dependency's doctest
# collected by the same run. What tells them apart is the FILE the failing
# frame names, so a class is established only where the SAME piece of
# evidence carries one of the two shapes that name it: a traceback frame, or
# a pytest node id. The node id is provenance in its own right, which is why
# `FAILED tests/test_x.py::test_y - AssertionError: ...` needs nothing else.
_PROVENANCE_RE = re.compile(
    rf'File "(?P<frame>[^"\n]+)", line \d+|^{_LINE_PREFIX}FAILED (?P<node>\S+?)::',
    re.MULTILINE,
)
# Not the checkout: an installed dependency, the runner's own temp area, or
# a synthetic frame (`<string>`/`<stdin>`, what `python -c` reports).
_NOT_THE_CHECKOUT = ("site-packages", "dist-packages", "/_temp/", "/tmp/")
# Inside the checkout, and still not the project's (owner's check (2)): the
# CI harness' own directories. A setup script the runner invokes lives there
# and asserts ITS preconditions, so the path says where the assertion ran,
# never whose it was. Path COMPONENTS, not substrings: `scripts/ci/` must not
# be found inside `scripts/cinema/`, and a project directory named
# `municipal/` holds no `ci` component. `("scripts", "ci")` is subsumed by
# `("ci",)` and spelled out anyway, because the owner named all four.
_CI_HARNESS_DIRS = ((".github",), (".ci",), ("ci",), ("scripts", "ci"))
# An ABSOLUTE frame is the checkout's only where the path says so. `/app/
# tests/test_greeting.py` qualifies: it is the image's WORKDIR copy of the
# project, which is what live acceptance run 3 really printed.
_CHECKOUT_DIRS = ("/tests/", "/src/")

NO_PROVENANCE_NOTE = "assertion without project provenance"


def _has_project_provenance(item: Evidence) -> bool:
    """Whether this piece of evidence says WHOSE assertion failed."""
    return any(
        _is_project_path(frame or node)
        for frame, node in _PROVENANCE_RE.findall(item.text)
    )


def _is_project_path(path: str) -> bool:
    """Whether a frame's path names a file of the checkout under diagnosis.

    A RELATIVE path is the checkout's by construction — pytest prints node
    ids relative to its rootdir, and a traceback frame is relative when the
    process was started inside the tree. An ABSOLUTE one is the checkout's
    only when it sits under the project's own directories. Either way a path
    through a CI-harness directory is not the project's, however far inside
    the checkout it sits (:func:`_is_ci_harness_path`).
    """
    if path.startswith("<"):
        return False
    if any(part in path for part in _NOT_THE_CHECKOUT):
        return False
    if _is_ci_harness_path(path):
        return False
    if not path.startswith("/"):
        return True
    return any(part in path for part in _CHECKOUT_DIRS)


def _is_ci_harness_path(path: str) -> bool:
    """Whether the path runs through one of the CI harness' own directories.

    A relative path is the checkout's by construction and an absolute one
    under `tests/`/`src/` is too, so this is the check that asks WHOSE file
    it is rather than where it sits.
    """
    parts = tuple(path.split("/"))
    return any(
        parts[index : index + len(harness)] == harness
        for harness in _CI_HARNESS_DIRS
        for index in range(len(parts))
    )


def _partition_by_provenance(
    matches: list[_Match],
) -> tuple[list[_Match], list[_Match]]:
    """Split the matches into (established, PROJECT matches without provenance).

    Only the PROJECT kind is gated: the other kinds are tied to their cause by
    the shape of the line itself (a tool's own framing, a parser's own
    sentence), which is the same requirement read off a different feature.
    """
    established: list[_Match] = []
    unprovenanced: list[_Match] = []
    for match in matches:
        if match.rule.kind is FailureKind.PROJECT and not _has_project_provenance(
            match.evidence
        ):
            unprovenanced.append(match)
        else:
            established.append(match)
    return established, unprovenanced


def _for_operator(item: Evidence, line: str) -> str:
    """One line of evidence as an operator should read it.

    An annotation's level is data, not text (`forge.Evidence`), so it is
    rendered back onto the line here -- at the one place a human reads it,
    and nowhere a rule can trip over it.
    """
    return line if item.level is None else f"{item.level}: {line}"


def _matched_lines(rule: Rule, item: Evidence) -> list[str]:
    """Every line of ``item`` the rule matched: document order, once each.

    ALL of them, not the first: the citation is the block, so a verdict that
    named only the first match reported one missing file out of two and left
    the operator to guess there was a second. Deduplicated by line text —
    a pattern can land twice inside one line, and an operator reads lines,
    not match offsets.
    """
    return list(
        dict.fromkeys(
            _line_at(item.text, hit.start()) for hit in rule.pattern.finditer(item.text)
        )
    )


def _matches(item: Evidence) -> tuple[list[_Match], list[_Match], list[_Match]]:
    """Every rule against one piece of evidence, in three buckets.

    ``(established, warning-shaped, off-infrastructure)``. A rule of ANY kind
    keeps looking past a match it judged warning-shaped. The lines it passed
    over are returned separately, and only when that rule established nothing
    at all, so the verdict can observe a noticed problem without citing it
    and without repeating what it did cite.

    The third bucket is an endpoint rule's match on a line that names no
    known infrastructure host (owner's check (1)): reported as a symptom
    whatever else that rule found, because it is a real thing the run
    printed and the reason it establishes nothing is about the endpoint, not
    about the rule.
    """
    found: list[_Match] = []
    warned: list[_Match] = []
    off_infra: list[_Match] = []
    for rule in RULES:
        established: list[_Match] = []
        skipped: list[_Match] = []
        for line in _matched_lines(rule, item):
            match = _Match(rule, item, line)
            if _is_warning_shaped(item, line):
                skipped.append(match)
            elif rule.endpoint and not _names_infra_host(line):
                off_infra.append(match)
            else:
                established.append(match)
        found.extend(established)
        if not established:
            warned.extend(skipped)
    return found, warned, off_infra


def _symptom_matches(item: Evidence) -> list[_Match]:
    """The symptom markers one piece of evidence carries: observed, not cited.

    Every matched line, as in :func:`_matches`. Unlike those these establish
    nothing, so the warning shape changes nothing about them either: the
    rendered line carries its own ``W: `` prefix or annotation level for the
    operator to read.
    """
    return [
        _Match(rule, item, line)
        for rule in SYMPTOMS
        for line in _matched_lines(rule, item)
    ]


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
