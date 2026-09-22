"""The combination table: source x level x cause context x placement x conflict.

One parametrised test over explicit rows. Each row is one piece of evidence and
the verdict the owner's unified rule (quoted verbatim above ``diagnose.RULES``)
says it must produce:

- the LEVEL of a message decides how evidence is weighed -- ``warning``/
  ``notice``, whether it is an annotation's level or a log line's own prefix,
  is an observation and never a class;
- AUTHORING needs positive evidence of a defect in the AUTHORED ARTIFACT, so
  each artifact shape is a row and each of its NEGATIVE TWINS -- the same words
  in a context with another cause -- is a row of its own expecting no class;
- a symptom establishes nothing at any level, and a conflict between two kinds
  is ambiguity, not a winner;
- a TOOL'S FRAMING establishes the source of a message, not its cause, so an
  unreachable-endpoint rule also needs a KNOWN INFRASTRUCTURE host on the line
  (owner's check (1), 2026-09-22) -- `wrong-configured-address` rows;
- a PATH INSIDE THE CHECKOUT establishes where an assertion ran, not whose it
  was, so a frame under a CI-harness directory is no provenance (check (2)),
  while a test vendored into `tests/` stays PROJECT by repository ownership.

Beside the negative twins, which vary the SHAPE, the table carries CAUSE TWINS,
which keep the message text identical and vary only what produced it. Both
members of such a pair expect the SAME verdict, and since the owner's evidence
rule of 2026-09-22 that verdict is the HONEST one: a class only where the two
causes really are the same failing condition, and UNCLASSIFIED wherever the
line could as easily have been printed by an application saying the words for
itself. No pair reads "a class we know is wrong for one of these" any more --
that was a wrong diagnosis, not a limitation -- and several rules therefore
carry two pairs: the tool-framed one that keeps the class, and the unframed
one that no longer gets it.

The rows are built by helpers from a small table of markers so that the cross
product is exhaustive rather than anecdotal; ``print_table()`` (``python -m``
this module, or run it as a script) renders them as markdown for review. It is
never called from a test: a matrix that prints is a matrix nobody reads.
"""

from dataclasses import dataclass

import pytest

from deployer.diagnose import RULES, Outcome, classify_failure, diagnose_run
from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
    FailedStep,
    StepRef,
)
from deployer.models import FailureKind

JOB_ID = 77
COMPLETE = Completeness(logs="present", annotations="present")

Source = str
Context = str


@dataclass(frozen=True)
class Row:
    """One cell of the table: the evidence, and the verdict it must produce.

    ``level`` is an annotation's ``annotation_level`` VERBATIM for ``source ==
    "annotation"`` -- it is handed to ``Evidence``, so it may carry nothing but
    the level itself -- and the shape of the log line for ``source == "log"``,
    which a log block writes at its own head because it has no level. Anything
    else worth naming in the table, such as where in the block the marker
    sits, goes in ``placement``.
    """

    row_id: str
    source: Source
    level: str | None
    text: str
    expected_outcome: Outcome
    expected_kind: FailureKind
    context: Context
    expects_ambiguity: bool = False
    placement: str = ""
    note: str = ""
    """Why this row reads the way it does, for a reader of ``print_table()``.

    Carried by the cause twins below, where the expected value is a statement
    about what the snapshot can honestly conclude rather than a bare class.
    """


# --- the markers, one per catalogue shape ------------------------------------
# `base` is the class the shape establishes when the evidence is failure
# evidence; `None` means the shape names no cause at all.

ARTIFACT = FailureKind.AUTHORING
PROJECT = FailureKind.PROJECT
ENVIRONMENT = FailureKind.ENVIRONMENT

# What survives as AUTHORING after the owner's evidence rule (2026-09-22):
# the Dockerfile parser's own sentence about the artifact's text, and
# GitHub's own validator framing over the workflow YAML.
ARTIFACT_MARKERS = (
    (
        "dockerfile-parse-error",
        "Dockerfile parse error on line 3: unexpected end of statement",
    ),
    (
        "unrecognized-named-value",
        "The workflow is not valid. .github/workflows/ci.yml (Line: 31, "
        "Col: 9): Unrecognized named-value: 'secret'. Located at position 1 "
        "within expression: secret.TOKEN",
    ),
)

# Shapes the catalogue USED to read as AUTHORING and the owner demoted on
# 2026-09-22: a container-runtime shape does not by itself say the name it
# failed on came from the authored artifact, `unknown instruction` is
# ordinary English about a vocabulary, and both COPY/ADD shapes name a
# mismatch between the instruction and its build context without saying
# which side moved. They establish nothing now, at every source and level,
# and their cause twins are below.
DEMOTED_MARKERS = (
    (
        "entrypoint-missing",
        "docker: Error response from daemon: unable to start container "
        'process: exec: "serve": executable file not found in $PATH: unknown.',
    ),
    (
        "unresolvable-action",
        "Unable to resolve action actions/checkout@v99, unable to find version v99",
    ),
    (
        "unknown-instruction",
        "dockerfile parse error on line 3: unknown instruction: FORM "
        "(did you mean FROM?)",
    ),
    (
        "copy-not-found",
        "ERROR: failed to solve: failed to compute cache key: failed to "
        'calculate checksum of ref abc::def: "/docs/setup.md": not found',
    ),
    (
        "copy-failed",
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory",
    ),
)

# PROJECT needs PROVENANCE in the SAME piece of evidence: a traceback frame
# or a pytest node id naming a file of the checkout. Every marker here
# carries its own, as real tool output does.
_PROJECT_FRAME = '  File "/app/tests/test_greet.py", line 10, in test_greet'
_PROJECT_NODE = "FAILED tests/test_greet.py::test_greet"

PROJECT_MARKERS = (
    (
        "assertion-error",
        f"{_PROJECT_FRAME}\n"
        "E   AssertionError: 'hello from ci_build' != 'hello from ci-build'",
    ),
    ("pytest-failed-assertion", f"{_PROJECT_NODE} - AssertionError: 1 != 2"),
    ("pytest-bare-assert", f"{_PROJECT_NODE} - assert 1 == 2"),
    (
        "pytest-assert",
        f"{_PROJECT_FRAME}\nE       assert 'ci_build' == 'ci-build'",
    ),
)

# The same assertion shapes WITHOUT provenance: the runner's own setup step,
# a vendored dependency's doctest, a synthetic `python -c` frame. The rules
# still match them; `classify_failure` refuses to call any of it a class.
PROVENANCELESS_MARKERS = (
    ("bare-assertion-error", "AssertionError: 1 != 2"),
    # Inside the checkout and still not the project's (owner's check (2),
    # 2026-09-22): the CI harness' own script, which the runner invokes and
    # which asserts ITS preconditions.
    (
        "ci-harness-frame",
        '  File ".github/scripts/setup.py", line 3, in main\n'
        "AssertionError: precondition",
    ),
    (
        "ci-directory-node-id",
        "FAILED ci/test_preflight.py::test_env - AssertionError: 1 != 2",
    ),
    ("runner-setup-assertion", 'File "<string>", line 1, in <module>'),
    (
        "vendored-doctest-assertion",
        '  File "/usr/lib/python3.12/site-packages/vendorlib/check.py", '
        "line 8, in verify\nAssertionError: 1 != 2",
    ),
    (
        "harness-node-id",
        "FAILED /usr/lib/python3/dist-packages/vendorlib/tests/test_a.py"
        "::test_a - AssertionError: 1 != 2",
    ),
)

ENVIRONMENT_MARKERS = (
    (
        "apt-fetch-timeout",
        "E: Failed to fetch http://deb.debian.org/debian/x.deb  Connection "
        "timed out [IP: 1.2.3.4 80]",
    ),
    ("host-unresolvable", "curl: (6) Could not resolve host: pypi.org"),
)

# The same ENVIRONMENT words with no tool's framing around them: an
# application under test printing what it was written to print.
UNFRAMED_ENVIRONMENT_MARKERS = (
    ("bare-timeout", "requests: connection timed out after 5s"),
    ("bare-503", "error parsing HTTP 503 response body: 503 Service Unavailable"),
    ("bare-disk-full", "tmpfs write failed: no space left on device"),
    ("bare-name-resolution", "Temporary failure in name resolution"),
)

# The tool's OWN framing, at an endpoint nothing reaches by default (owner's
# check (1), 2026-09-22). The rules match the framing exactly as in
# `ENVIRONMENT_MARKERS`; what stops them is the host, so these rows are the
# cross-product proof that the demotion holds at every source and level.
MISCONFIGURED_ENDPOINT_MARKERS = (
    ("curl-invalid-index", "curl: (6) Could not resolve host: pypi.invalid"),
    (
        "apt-unroutable-address",
        "E: Failed to fetch http://10.255.255.1/debian/x.deb  Connection "
        "timed out [IP: 10.255.255.1 80]",
    ),
    ("uv-corporate-mirror", "error: Failed to fetch: `https://mirror.corp/simple/x/`"),
    (
        "git-private-forge",
        "fatal: unable to access 'https://git.internal.example/o/r/': "
        "Could not resolve host: git.internal.example",
    ),
)

SYMPTOM_MARKERS = (
    ("bare-no-such-file", "cp: cannot stat '/app/main.py': No such file or directory"),
    (
        "exec-format-error",
        "standard_init_linux.go:228: exec user process caused: exec format error",
    ),
    ("command-not-found", "/bin/bash: line 12: pytest: command not found"),
)

UNKNOWN_MARKERS = (("exit-code", "Process completed with exit code 1."),)

# The negative twins: the marker text of a kept AUTHORING rule in a context
# with another cause -- NOT AUTHORING, either way. `expected_base` is what
# the row honestly resolves to: `None` for most (no rule matches the bare
# words at all, so `no rule matched`/UNCLASSIFIED); `ambiguous` marks the
# one that still collides with a PROJECT rule because the surviving
# AUTHORING shape (bare `dockerfile parse error`, unanchored on purpose --
# nothing else prints that sentence) reads the same prose.
#
# The three assertion twins carry a pytest node id, as a real failing suite
# does: since the evidence rule of 2026-09-22 a PROJECT class needs that
# provenance, and a twin written as a bare `E   AssertionError:` line would
# resolve to UNCLASSIFIED for the wrong reason -- hiding, rather than
# pinning, what the AUTHORING rule does with the same words.
TWINS = (
    ("shell-stat", "stat app.py: no such file or directory", False, None),
    ("cat-missing-config", "cat: config.yml: No such file or directory", False, None),
    (
        "buildkit-run-exit",
        'ERROR: failed to solve: process "/bin/sh -c uv sync --frozen" did '
        "not complete successfully: exit code: 1",
        False,
        None,
    ),
    # buildkit's other `: not found` ending: an image reference it could not
    # pull. A misspelled tag in the authored FROM and a registry that does not
    # serve that tag print the identical line, so it names no cause -- which
    # is exactly why the kept shape demands the QUOTED path of a build-context
    # source. Loosen the rule back to `...: not found` and this row goes red.
    (
        "image-ref-not-found",
        "ERROR: failed to solve: python:3.12-slim: "
        "docker.io/library/python:3.12-slim: not found",
        False,
        None,
    ),
    ("shell-command-not-found", "bash: foo: command not found", False, None),
    (
        "pytest-executable-not-found",
        "E   RuntimeError: executable file not found in the sandbox",
        False,
        None,
    ),
    (
        "assertion-about-parse-error",
        "FAILED tests/test_build.py::test_parse_error - AssertionError: "
        "expected 'Dockerfile parse error' in captured stderr",
        True,
        None,
    ),
    (
        "assertion-about-unknown-instruction",
        "FAILED tests/test_build.py::test_unknown_instruction - "
        "AssertionError: unknown instruction: FORM was not reported",
        False,
        PROJECT,
    ),
    (
        "assertion-about-named-value",
        "FAILED tests/test_ci.py::test_named_value - AssertionError: "
        "Unrecognized named-value was expected",
        False,
        PROJECT,
    ),
)

# --- the variants ------------------------------------------------------------

# A log line's own severity, at its head: apt's `W: `, the `warning: `/
# `notice: ` compilers and pip write -- in whatever case the tool wrote them
# (final review round, finding 2: `WARNING:`/`Notice:` defeated a
# case-sensitive prefix as completely as no prefix at all) -- and buildkit's
# `#<step> <seconds> ` framing, which is not a severity at all and must not
# read as one.
LOG_SHAPES = (
    ("plain", "plain line", ""),
    ("w", "W: line", "W: "),
    ("warning", "warning: line", "warning: "),
    ("warning-upper", "WARNING: line", "WARNING: "),
    ("notice", "notice: line", "notice: "),
    ("notice-title", "Notice: line", "Notice: "),
    ("buildkit", "#7 1.2 line", "#7 1.2 "),
)
_NOTICED_SHAPES = frozenset({"w", "warning", "warning-upper", "notice", "notice-title"})
_SEVERITY_PREFIXES = ("W: ", "warning: ", "WARNING: ", "notice: ", "Notice: ")

ANNOTATION_LEVELS: tuple[str | None, ...] = (
    "warning",
    "notice",
    "failure",
    "error",
    None,
)
_NOTICED_LEVELS = frozenset({"warning", "notice"})

PLACEMENTS = ("first", "middle", "last", "leading-empty", "buildkit-block")


def _placed(text: str, placement: str) -> str:
    """The marker inside a block, where the block is what GitHub really hands
    over: the whole step log, with the marker anywhere in it."""
    if placement == "first":
        return f"{text}\nStarting checks\nCleaning up orphan processes"
    if placement == "middle":
        return f"Starting checks\n{text}\nCleaning up orphan processes"
    if placement == "last":
        return f"Starting checks\nCleaning up orphan processes\n{text}"
    if placement == "leading-empty":
        return f"\n{text}"
    return (
        f"#12 [stage-0 7/9] COPY docs/setup.md ./setup.md\n#12 0.4 {text}\n#12 CANCELED"
    )


def _shaped(text: str, prefix: str) -> str:
    """Put a log line's own severity at the head of EVERY line of a marker.

    apt writes its severity first, so its `W: ` line is the `E: ` line with
    the severity replaced -- which is exactly the recovered-and-retried line
    a Debian build really prints. Buildkit's framing is not a severity and
    is prepended to whatever the line already said. Both are per LINE: a
    multi-line marker (an assertion under its traceback frame) whose first
    line alone carried the prefix would leave the rest looking unprefixed,
    and the row would pass for the wrong reason.
    """
    return "\n".join(_shaped_line(line, prefix) for line in text.split("\n"))


def _shaped_line(line: str, prefix: str) -> str:
    if prefix in _SEVERITY_PREFIXES and line.startswith("E: "):
        line = line[len("E: ") :]
    return prefix + line


def _expected(base: FailureKind | None, noticed: bool) -> tuple[Outcome, FailureKind]:
    """The unified rule as a function: a noticed problem, or a shape that
    names no cause, is an observation; anything else is the class its shape
    establishes."""
    if noticed or base is None:
        return "UNCLASSIFIED", FailureKind.UNKNOWN
    return "CLASSIFIED", base


def _log_rows(
    context: Context,
    marker_id: str,
    text: str,
    base: FailureKind | None,
    ambiguous: bool = False,
    shapes: tuple[tuple[str, str, str], ...] = LOG_SHAPES,
) -> list[Row]:
    rows = []
    for slug, display, prefix in shapes:
        outcome, kind = _expected(base, slug in _NOTICED_SHAPES)
        rows.append(
            Row(
                row_id=f"log-{slug}-{context}-{marker_id}",
                source="log",
                level=display,
                text=_shaped(text, prefix),
                expected_outcome=outcome,
                expected_kind=kind,
                context=context,
                expects_ambiguity=ambiguous and outcome == "UNCLASSIFIED",
            )
        )
    return rows


def _annotation_rows(
    context: Context,
    marker_id: str,
    text: str,
    base: FailureKind | None,
    ambiguous: bool = False,
    levels: tuple[str | None, ...] = ANNOTATION_LEVELS,
) -> list[Row]:
    rows = []
    for level in levels:
        outcome, kind = _expected(base, level in _NOTICED_LEVELS)
        rows.append(
            Row(
                row_id=f"ann-{level or 'absent'}-{context}-{marker_id}",
                source="annotation",
                level=level,
                text=text,
                expected_outcome=outcome,
                expected_kind=kind,
                context=context,
                expects_ambiguity=ambiguous and outcome == "UNCLASSIFIED",
            )
        )
    return rows


def _cross(
    context: Context, markers: tuple[tuple[str, str], ...], base: FailureKind | None
) -> list[Row]:
    """Every marker of one cause context against every source and level."""
    return [
        row
        for marker_id, text in markers
        for row in _log_rows(context, marker_id, text, base)
        + _annotation_rows(context, marker_id, text, base)
    ]


def _placement_rows(
    context: Context,
    marker_id: str,
    text: str,
    base: FailureKind | None,
    levels: tuple[str | None, ...],
) -> list[Row]:
    """The same marker, moved around a multi-line block.

    A log block is judged per matched line, an annotation by its level for
    every line of its message; both must hold wherever the marker sits.
    """
    rows = []
    for placement in PLACEMENTS:
        block = _placed(text, placement)
        outcome, kind = _expected(base, False)
        rows.append(
            Row(
                row_id=f"log-plain-{context}-{marker_id}-at-{placement}",
                source="log",
                level="plain line",
                text=block,
                expected_outcome=outcome,
                expected_kind=kind,
                context=context,
                placement=placement,
            )
        )
        for level in levels:
            outcome, kind = _expected(base, level in _NOTICED_LEVELS)
            rows.append(
                Row(
                    row_id=f"ann-{level}-{context}-{marker_id}-at-{placement}",
                    source="annotation",
                    level=level,
                    text=block,
                    expected_outcome=outcome,
                    expected_kind=kind,
                    context=context,
                    placement=placement,
                )
            )
    return rows


_ARTIFACT_MARKER = ARTIFACT_MARKERS[0][1]
_PROJECT_MARKER = PROJECT_MARKERS[0][1]
_ENVIRONMENT_MARKER = ENVIRONMENT_MARKERS[0][1]
_SYMPTOM_MARKER = SYMPTOM_MARKERS[0][1]
# apt's `Err:` detail line, recovered: it keeps the framing the evidence
# rule requires (host and port), so the rule really does match it and the
# WARNING SHAPE is what stops it -- which is the fact these rows pin.
_RECOVERED_FETCH = (
    "W: Could not connect to deb.debian.org:80 (1.2.3.4), connection "
    "timed out [retrying]"
)

CONFLICT_ROWS = (
    Row(
        row_id="log-plain-conflict-artifact-then-project",
        source="log",
        level="plain line",
        text=f"{_ARTIFACT_MARKER}\n{_PROJECT_MARKER}",
        expected_outcome="UNCLASSIFIED",
        expected_kind=FailureKind.UNKNOWN,
        context="conflict",
        expects_ambiguity=True,
    ),
    Row(
        row_id="log-plain-conflict-project-then-artifact",
        source="log",
        level="plain line",
        text=f"{_PROJECT_MARKER}\n{_ARTIFACT_MARKER}",
        expected_outcome="UNCLASSIFIED",
        expected_kind=FailureKind.UNKNOWN,
        context="conflict",
        expects_ambiguity=True,
    ),
    Row(
        row_id="log-plain-conflict-warning-environment-then-artifact",
        source="log",
        level="plain line",
        text=f"{_RECOVERED_FETCH}\n{_ARTIFACT_MARKER}",
        expected_outcome="CLASSIFIED",
        expected_kind=FailureKind.AUTHORING,
        context="conflict",
    ),
    Row(
        row_id="log-plain-conflict-artifact-then-warning-environment",
        source="log",
        level="plain line",
        text=f"{_ARTIFACT_MARKER}\n{_RECOVERED_FETCH}",
        expected_outcome="CLASSIFIED",
        expected_kind=FailureKind.AUTHORING,
        context="conflict",
    ),
)
"""A warning is not a competing kind: it cannot dilute an established cause
into `ambiguous:`, whichever side of the block it sits on. Two FAILURE kinds
in one pool are the real conflict, and order does not pick a winner."""

# --- cause twins: the SAME message, a different cause ------------------------
# The negative twins above vary the SHAPE: different words, so no class. A
# cause twin keeps the message text IDENTICAL and changes only what produced
# it — the harder question the owner put to the catalogue on 2026-09-22. Both
# members of a pair therefore carry the SAME expected verdict: a pair whose
# members differed would mean the snapshot CAN separate the causes, and the
# rule could then be made to.
#
# What changed in the bounded pass of the same day: a CLASSIFIED pair may no
# longer mean "the class the catalogue answers to both causes, over-firing on
# one of them". That is a wrong diagnosis, and the rule was narrowed instead.
# A pair reads CLASSIFIED only where both causes really are the same failing
# condition — the tool that printed the line could not do the thing it was
# asked to do, whoever arranged that — and its note says what the class does
# NOT claim. Everywhere the old note said "known over-firing" or "known
# limitation", the marker turned out to be separable after all, by the
# feature the rule now requires: a TOOL'S OWN FRAMING for ENVIRONMENT, and
# PROVENANCE for PROJECT. Those pairs are UNCLASSIFIED now, and the rules
# keep a framed/provenanced pair of their own beside them.
#
# The price is stated too, in `service-unavailable-unframed`: a real upstream
# failure reported by a tool this catalogue does not know goes unnamed. That
# is the cost of never naming the fixture that prints the same three words.
#
# The preambles are ordinary log lines from each scenario. They deliberately
# do not encode the cause: a real snapshot does not either, which is the
# finding these rows carry.


@dataclass(frozen=True)
class CauseTwin:
    """One message text, two causes, and what the snapshot may conclude."""

    rule: str
    slug: str
    marker: str
    base: FailureKind | None
    note: str
    cause_a: str
    preamble_a: str
    cause_b: str
    preamble_b: str


_BUILD_CONTEXT = "#2 [internal] load build context\n#2 transferring context: 41.09kB"
_SMALL_CONTEXT = "#2 [internal] load build context\n#2 transferring context: 1.21kB"

CAUSE_TWINS: tuple[CauseTwin, ...] = (
    # --- the kept AUTHORING shapes -------------------------------------------
    CauseTwin(
        rule="dockerfile parse error",
        slug="dockerfile-parse-error",
        marker="Dockerfile parse error on line 3: unexpected end of statement",
        base=ARTIFACT,
        note=(
            "hand-written Dockerfile vs one a build script generated: AUTHORING "
            "either way -- the class names the artifact whose text will not "
            "parse, never who typed it. A plain syntax error, with no "
            "`unknown instruction` beside it, is the whole of what the rule "
            "keeps"
        ),
        cause_a="hand-written",
        preamble_a="#1 [internal] load build definition from Dockerfile",
        cause_b="script-generated",
        preamble_b="#1 [internal] load build definition from Dockerfile.gen",
    ),
    CauseTwin(
        rule="unrecognized named-value",
        slug="unrecognized-named-value",
        marker=(
            "The workflow is not valid. .github/workflows/ci.yml (Line: 31, "
            "Col: 9): Unrecognized named-value: 'secret'. Located at "
            "position 1 within expression: secret.TOKEN"
        ),
        base=ARTIFACT,
        note=(
            "the repo's own workflow vs a reusable workflow it calls: AUTHORING "
            "either way -- GitHub's own validator says the NAME is not in the "
            "language, which only workflow YAML can be wrong about; whose YAML "
            "it is the snapshot does not say, and the class does not claim"
        ),
        cause_a="own-workflow",
        preamble_a="##[error].github/workflows/ci.yml (Line: 31, Col: 9)",
        cause_b="called-reusable-workflow",
        preamble_b="##[error]org/ci-workflows/.github/workflows/build.yml@v2",
    ),
    # --- the AUTHORING shapes demoted by the evidence rule --------------------
    CauseTwin(
        rule="unknown instruction",
        slug="unknown-instruction",
        marker=(
            "dockerfile parse error on line 7: unknown instruction: "
            "RUN --mount=type=cache,target=/root/.cache"
        ),
        base=None,
        note=(
            "a typo in the authored file vs an instruction a builder too old to "
            "know it rejects: UNCLASSIFIED for both now. The pair used to read "
            "AUTHORING on the parser's framing sharing the line -- but that is "
            "exactly what an application quoting the parser also prints, so the "
            "framing beside the words proves only that two sentences share a "
            "line. Reported as `symptom: unknown instruction`"
        ),
        cause_a="typo",
        preamble_a="#1 [internal] load build definition from Dockerfile",
        cause_b="builder-too-old",
        preamble_b='#0 building with "default" instance using docker driver',
    ),
    CauseTwin(
        rule="copy/add source not found",
        slug="copy-not-found",
        marker=(
            "ERROR: failed to solve: failed to compute cache key: failed to "
            'calculate checksum of ref abc::def: "/docs/setup.md": not found'
        ),
        base=None,
        note=(
            "a path the COPY never had right vs a file the project removed "
            "AFTER the Dockerfile was authored: UNCLASSIFIED for both now. The "
            "pair used to read AUTHORING with the second written off as a "
            "stated residual -- but the snapshot cannot say which side moved, "
            "so naming the artifact was right by luck. This is live acceptance "
            "run 1, and its expectation was corrected rather than the rule"
        ),
        cause_a="wrong-path",
        preamble_a="#12 [stage-0 7/9] COPY docs/setup.md ./setup.md",
        cause_b="removed-after-authoring",
        preamble_b="#12 [stage-0 7/9] COPY docs/setup.md ./setup.md",
    ),
    CauseTwin(
        rule="copy/add failed in build context",
        slug="copy-failed",
        marker=(
            "COPY failed: file not found in build context or excluded by "
            ".dockerignore: stat app.py: no such file or directory"
        ),
        base=None,
        note=(
            "a source that is not in the repo vs one the .dockerignore excludes: "
            "UNCLASSIFIED for both now, with docker's shape demoted beside "
            "buildkit's -- the class, not the instance"
        ),
        cause_a="absent-from-repo",
        preamble_a=_BUILD_CONTEXT,
        cause_b="excluded-by-dockerignore",
        preamble_b=_SMALL_CONTEXT,
    ),
    # --- ENVIRONMENT, framed: the tool says IT could not ----------------------
    # Both members are a genuine failure of the tool that printed the line, so
    # the class is right for both. What the class claims is the CONDITION the
    # step ran into, never who arranged it. The pairs that used to set a real
    # failure against a test PRINTING the phrase are below instead, unframed:
    # that scenario no longer produces these lines at all.
    CauseTwin(
        rule="docker daemon unreachable",
        slug="docker-daemon-unreachable",
        marker=(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
            " Is the docker daemon running?"
        ),
        base=ENVIRONMENT,
        note=(
            "the runner's daemon stopped vs a self-hosted runner whose socket "
            "was never mounted into the job container: ENVIRONMENT for both -- "
            "docker's own client says IT could not reach a daemon, and both "
            "are that same missing condition"
        ),
        cause_a="daemon-down",
        preamble_a="##[group]Run docker build .",
        cause_b="socket-not-in-the-job-container",
        preamble_b="##[group]Run docker compose up -d",
    ),
    CauseTwin(
        rule="host unresolvable",
        slug="host-unresolvable",
        marker="curl: (6) Could not resolve host: pypi.org",
        base=ENVIRONMENT,
        note=(
            "the runner's resolver down vs an egress proxy refusing the name: "
            "ENVIRONMENT for both -- curl's own exit-code framing says curl "
            "could not resolve it, and the class names that, not the reason "
            "the name did not resolve"
        ),
        cause_a="runner-dns-down",
        preamble_a="##[group]Run curl -sSf https://pypi.org/simple/",
        cause_b="egress-proxy-blocks-the-name",
        preamble_b="##[group]Run curl -sSf https://pypi.org/simple/ --proxy $PROXY",
    ),
    CauseTwin(
        rule="name resolution failure",
        slug="name-resolution-failure",
        marker="E: Temporary failure resolving 'deb.debian.org'",
        base=ENVIRONMENT,
        note=(
            "the runner's resolver down vs a build step run with networking "
            "switched off on purpose: ENVIRONMENT for both -- apt names the "
            "host it could not resolve either way, and a deliberately absent "
            "network is still the environment the step was given"
        ),
        cause_a="resolver-down",
        preamble_a="#9 [stage-0 3/9] RUN apt-get update",
        cause_b="network-disabled-on-purpose",
        preamble_b="#9 [stage-0 3/9] RUN --network=none apt-get update",
    ),
    CauseTwin(
        rule="fetch failure",
        slug="fetch-failure",
        marker=(
            "E: Failed to fetch http://deb.debian.org/debian/pool/main/x.deb  "
            "404  Not Found"
        ),
        base=ENVIRONMENT,
        note=(
            "a mirror that rotated the package out vs a version the Dockerfile "
            "pinned that the suite no longer carries: ENVIRONMENT for both -- "
            "apt says IT could not fetch what it was asked for. RESIDUAL, "
            "stated: the second is arguably an artifact defect, and the "
            "snapshot cannot see the pin"
        ),
        cause_a="mirror-rotated",
        preamble_a="#9 [stage-0 3/9] RUN apt-get install -y libpq5",
        cause_b="pinned-version-withdrawn",
        preamble_b="#9 [stage-0 3/9] RUN apt-get install -y libpq5=13.4-1",
    ),
    CauseTwin(
        rule="connection timed out",
        slug="connection-timed-out",
        marker=(
            "#11 15.43   Could not connect to deb.debian.org:80 (1.2.3.4), "
            "connection timed out"
        ),
        base=ENVIRONMENT,
        note=(
            "the runner's egress firewall vs the mirror itself refusing to "
            "answer: ENVIRONMENT for both -- apt's own detail line names the "
            "host AND the port it could not reach, and the host is the "
            "default Debian mirror, so no address the project configured can "
            "have been the thing that was wrong. The pair used to carry live "
            "acceptance run 2 verbatim; that line named `10.255.255.1` and "
            "now sits in `connection-timed-out-wrong-configured-address`"
        ),
        cause_a="runner-egress-firewall",
        preamble_a="#11 [stage-0 4/9] RUN apt-get update",
        cause_b="mirror-not-answering",
        preamble_b="#11 [stage-0 4/9] RUN apt-get -o Acquire::Retries=0 update",
    ),
    CauseTwin(
        rule="registry rate limit",
        slug="registry-rate-limit",
        marker="toomanyrequests: You have reached your pull rate limit.",
        base=ENVIRONMENT,
        note=(
            "a shared runner IP that exhausted the anonymous quota vs a job "
            "that lost its registry login: ENVIRONMENT for both -- the "
            "registry's own refusal, and in both the pull is the thing that "
            "could not be made"
        ),
        cause_a="quota-exhausted",
        preamble_a="#1 [internal] load metadata for docker.io/library/python:3.12",
        cause_b="registry-login-lost",
        preamble_b="#1 [internal] load metadata for docker.io/acme/base:1.2",
    ),
    CauseTwin(
        rule="service unavailable",
        slug="service-unavailable",
        marker=(
            "ERROR: failed to solve: failed to do request: "
            "docker.io/library/python:3.12-slim: 503 Service Unavailable"
        ),
        base=ENVIRONMENT,
        note=(
            "the registry down vs the registry shedding load: ENVIRONMENT for "
            "both -- buildkit says IT got the 503, which is what separates "
            "this line from an app's fixture printing the same three words"
        ),
        cause_a="registry-down",
        preamble_a="#1 [internal] load metadata for docker.io/library/python:3.12",
        cause_b="registry-shedding-load",
        preamble_b="#1 [internal] load metadata for docker.io/library/python:3.12",
    ),
    CauseTwin(
        rule="disk full",
        slug="disk-full",
        marker="write /var/lib/docker/tmp/x: no space left on device",
        base=ENVIRONMENT,
        note=(
            "the runner's disk filled by earlier jobs vs an image whose layers "
            "do not fit it at all: ENVIRONMENT for both -- the path is the "
            "daemon's own storage, so it is the runner that ran out"
        ),
        cause_a="runner-disk-filled",
        preamble_a="#12 [stage-0 7/9] COPY . /app",
        cause_b="layers-larger-than-the-runner",
        preamble_b="#12 [stage-0 7/9] COPY model-weights/ /app/weights",
    ),
    CauseTwin(
        rule="runner shutdown",
        slug="runner-shutdown",
        marker=(
            "The runner has received a shutdown signal. This can happen when "
            "the runner service is stopped."
        ),
        base=ENVIRONMENT,
        note=(
            "a spot instance reclaimed mid-job vs the runner service restarted "
            "under it: ENVIRONMENT for both -- the runner's own sentence about "
            "itself. The twin that used to sit here, a human cancelling the "
            "run, is GONE: a cancelled run's conclusion is `cancelled`, which "
            "`forge` refuses (`_FAILED_CONCLUSIONS`), so it never reaches the "
            "classifier to be misread"
        ),
        cause_a="instance-reclaimed",
        preamble_a="##[group]Run pytest",
        cause_b="runner-service-restarted",
        preamble_b="##[group]Run pytest -x",
    ),
    # --- ENVIRONMENT, framed, at an endpoint the configuration chose ---------
    # The owner's check (1), 2026-09-22: "Tool framing (curl/apt/docker)
    # establishes the SOURCE of a message, not its cause: a wrong address
    # from configuration also yields a network error." Each pair below is
    # framed by the tool exactly as its ENVIRONMENT sibling above -- and the
    # endpoint is one nothing reaches by default, so the first cause is a
    # misconfigured ADDRESS and the second the network. UNCLASSIFIED for
    # both: the snapshot cannot say which, and naming the environment would
    # be right by luck.
    CauseTwin(
        rule="host unresolvable",
        slug="host-unresolvable-wrong-configured-address",
        marker="curl: (6) Could not resolve host: pypi.invalid",
        base=None,
        note=(
            "a typo in the index URL vs the runner's resolver down: "
            "UNCLASSIFIED for both -- curl's framing says curl could not "
            "resolve it, and `pypi.invalid` is not a name anything reaches "
            "by default, so the address is as likely the defect as the "
            "resolver"
        ),
        cause_a="typo-in-the-index-url",
        preamble_a="##[group]Run curl -sSf https://pypi.invalid/simple/",
        cause_b="runner-dns-down",
        preamble_b="##[group]Run curl -sSf $INDEX_URL/simple/",
    ),
    CauseTwin(
        rule="name resolution failure",
        slug="name-resolution-failure-wrong-configured-address",
        marker="E: Temporary failure resolving 'mirror.corp'",
        base=None,
        note=(
            "a `sources.list` pointing at a mirror that no longer exists vs "
            "the resolver down for one that does: UNCLASSIFIED for both -- "
            "apt names the host either way, and the name is the artifact's "
            "choice"
        ),
        cause_a="mirror-decommissioned",
        preamble_a="#9 [stage-0 3/9] RUN apt-get update",
        cause_b="resolver-down",
        preamble_b="#9 [stage-0 3/9] RUN apt-get update -o Debug::Acquire::http=1",
    ),
    CauseTwin(
        rule="connection timed out",
        slug="connection-timed-out-wrong-configured-address",
        marker=(
            "#11 15.43   Could not connect to 10.255.255.1:80 (10.255.255.1), "
            "connection timed out"
        ),
        base=None,
        note=(
            "a private address the `sources.list` line got wrong vs the "
            "runner's egress firewall blocking a mirror that is really "
            "there: UNCLASSIFIED for both. Verbatim from live acceptance run "
            "2, which read ENVIRONMENT at acceptance and does not any more: "
            "an unroutable RFC1918 address is precisely what a misconfigured "
            "endpoint looks like, and the framing only says apt was the one "
            "that could not reach it"
        ),
        cause_a="unroutable-address-in-sources-list",
        preamble_a="#11 [stage-0 4/9] RUN apt-get update",
        cause_b="runner-egress-firewall",
        preamble_b="#11 [stage-0 4/9] RUN apt-get -o Acquire::Retries=0 update",
    ),
    CauseTwin(
        rule="fetch failure",
        slug="fetch-failure-wrong-configured-address",
        marker="error: Failed to fetch: `https://mirror.corp/simple/anyio/`",
        base=None,
        note=(
            "`UV_INDEX_URL` pointing at a mirror that is gone vs one that is "
            "up and unreachable from this runner: UNCLASSIFIED for both -- "
            "uv's own framing, an endpoint only this project asks for"
        ),
        cause_a="index-url-stale",
        preamble_a="#7 [stage-0 2/9] RUN uv sync --frozen",
        cause_b="mirror-unreachable-from-the-runner",
        preamble_b="#7 [stage-0 2/9] RUN uv sync --frozen --no-cache",
    ),
    # --- ENVIRONMENT, unframed: the words without the tool -------------------
    # Where the old table set a real failure against a test PRINTING the
    # phrase, the two are separable after all -- by the framing. These pairs
    # carry the scenario that used to make the class an over-firing, and both
    # members are UNCLASSIFIED because nothing in the line says who printed it.
    CauseTwin(
        rule="connection timed out",
        slug="connection-timed-out-unframed",
        marker="requests: connection timed out after 5s",
        base=None,
        note=(
            "the runner's network vs the app's OWN retry test printing the "
            "phrase it is testing: UNCLASSIFIED for both -- the pair that used "
            "to pin this as a known over-firing, now answered honestly"
        ),
        cause_a="runner-network",
        preamble_a="##[group]Run python -m app.sync --once",
        cause_b="app-retry-test",
        preamble_b="##[group]Run pytest tests/test_retry.py -k timeout",
    ),
    CauseTwin(
        rule="service unavailable",
        slug="service-unavailable-unframed",
        marker="error parsing HTTP 503 response body: 503 Service Unavailable",
        base=None,
        note=(
            "an upstream really down, reported by a tool this catalogue does "
            "not know, vs the app's HTTP fixture printing a canned response: "
            "UNCLASSIFIED for both. The cost of the rule is stated here -- the "
            "first is a real ENVIRONMENT failure the snapshot now declines to "
            "name, which is the price of never naming the second"
        ),
        cause_a="upstream-down-unknown-tool",
        preamble_a="##[group]Run python -m app.publish",
        cause_b="canned-fixture-response",
        preamble_b="##[group]Run pytest tests/test_upstream.py -k unavailable",
    ),
    CauseTwin(
        rule="host unresolvable",
        slug="host-unresolvable-unframed",
        marker="ConnectionError: Could not resolve host: pypi.org",
        base=None,
        note=(
            "the runner's DNS down vs a step that deliberately probes an "
            "unreachable host to prove the image needs no network: "
            "UNCLASSIFIED for both -- an exception carries no tool's framing"
        ),
        cause_a="runner-dns-down",
        preamble_a="##[group]Run python -m app.sync --once",
        cause_b="deliberate-offline-probe",
        preamble_b="##[group]Run pytest tests/test_offline.py",
    ),
    CauseTwin(
        rule="name resolution failure",
        slug="name-resolution-failure-unframed",
        marker="Temporary failure in name resolution",
        base=None,
        note=(
            "the resolver down vs a DNS test printing what it asserts on: "
            "UNCLASSIFIED for both -- the bare sentence names no tool"
        ),
        cause_a="resolver-down",
        preamble_a="##[group]Run python -m app.sync --once",
        cause_b="app-dns-test",
        preamble_b="##[group]Run pytest tests/test_dns.py -k failure",
    ),
    CauseTwin(
        rule="fetch failure",
        slug="fetch-failure-unframed",
        marker="TypeError: Failed to fetch",
        base=None,
        note=(
            "a browser test whose fetch really failed vs one asserting on the "
            "message: UNCLASSIFIED for both -- and this is the over-firer "
            "`TODO.md` recorded by name, closed by the framing requirement "
            "rather than by a narrower phrase"
        ),
        cause_a="browser-test-offline",
        preamble_a="##[group]Run npx jest",
        cause_b="browser-test-asserting-the-message",
        preamble_b="##[group]Run npx jest --testPathPattern errors",
    ),
    CauseTwin(
        rule="disk full",
        slug="disk-full-unframed",
        marker="tmpfs write failed: no space left on device",
        base=None,
        note=(
            "the runner's disk filled vs a test writing to a deliberately tiny "
            "tmpfs to prove the app survives ENOSPC: UNCLASSIFIED for both"
        ),
        cause_a="runner-disk-filled",
        preamble_a="##[group]Run python -m app.spool",
        cause_b="deliberate-enospc-test",
        preamble_b="##[group]Run pytest tests/test_spool.py -k enospc",
    ),
    CauseTwin(
        rule="registry rate limit",
        slug="registry-rate-limit-unframed",
        marker="assert 'toomanyrequests' in body",
        base=None,
        note=(
            "a test replaying a recorded 429 body vs one asserting the client "
            "handles it: UNCLASSIFIED for both -- the word alone is not the "
            "registry speaking"
        ),
        cause_a="recorded-response-replayed",
        preamble_a="##[group]Run pytest tests/test_registry.py -k ratelimit",
        cause_b="contract-test",
        preamble_b="##[group]Run pytest tests/test_registry.py -k contract",
    ),
    # --- PROJECT: with provenance, and without -------------------------------
    CauseTwin(
        rule="assertion error",
        slug="assertion-error-with-frame",
        marker=(
            '  File "/app/tests/test_greet.py", line 10, in test_greet\n'
            "AssertionError: 1 != 2"
        ),
        base=PROJECT,
        note=(
            "the project's own test vs one the CI harness copied INTO the "
            "checkout: PROJECT for both -- the frame names a file of the tree "
            "that was built and tested, and a file vendored there is part of "
            "it. RESIDUAL, stated: the class names the tree, not the author"
        ),
        cause_a="project-test",
        preamble_a="##[group]Run pytest tests/test_greet.py",
        cause_b="test-vendored-into-the-checkout",
        preamble_b="##[group]Run pytest tests/",
    ),
    CauseTwin(
        rule="assertion error",
        slug="assertion-error",
        marker="AssertionError: 1 != 2",
        base=None,
        note=(
            "the project's own test vs the CI's setup script asserting a "
            "precondition: UNCLASSIFIED for both. The pair used to read "
            "PROJECT and call the confusion a known limitation; without a "
            "frame the snapshot does not say whose assertion failed, and it "
            "now says so -- `assertion without project provenance`"
        ),
        cause_a="project-test",
        preamble_a="##[group]Run pytest tests/test_greet.py",
        cause_b="ci-setup-script",
        preamble_b="##[group]Run python .github/scripts/provision.py",
    ),
    CauseTwin(
        rule="pytest failed with assertion",
        slug="pytest-failed-assertion",
        marker="FAILED tests/test_greet.py::test_greet - AssertionError: 1 != 2",
        base=PROJECT,
        note=(
            "the project's own suite vs a test file vendored into the "
            "checkout: PROJECT for both -- pytest prints the node id relative "
            "to its rootdir, so the path IS the checkout's. The residual is "
            "the same as the frame pair's, and no larger"
        ),
        cause_a="project-suite",
        preamble_a="##[group]Run pytest",
        cause_b="test-vendored-into-the-checkout",
        preamble_b="##[group]Run pytest tests/",
    ),
    CauseTwin(
        rule="pytest failed with assertion",
        slug="pytest-failed-assertion-outside-the-checkout",
        marker=(
            "FAILED /usr/lib/python3/dist-packages/vendorlib/tests/test_a.py"
            "::test_a - AssertionError: 1 != 2"
        ),
        base=None,
        note=(
            "a conftest plugin the CI image installs vs a vendored "
            "dependency's own suite collected by the same run: UNCLASSIFIED "
            "for both -- the node id names a path outside the checkout, which "
            "is the discriminator the old 'known limitation' note said did "
            "not exist"
        ),
        cause_a="ci-installed-plugin",
        preamble_a="##[group]Run pytest -p ci_harness.plugin",
        cause_b="vendored-dependency-suite",
        preamble_b="##[group]Run pytest --pyargs vendorlib",
    ),
    CauseTwin(
        rule="pytest bare assert",
        slug="pytest-bare-assert",
        marker="FAILED tests/test_greet.py::test_greet - assert 1 == 2",
        base=PROJECT,
        note=(
            "the project's own assertion vs one in a conftest plugin that "
            "lives in the checkout: PROJECT for both -- both are files of the "
            "tree under test"
        ),
        cause_a="project-assertion",
        preamble_a="##[group]Run pytest",
        cause_b="checkout-conftest-assertion",
        preamble_b="##[group]Run pytest -p no:cacheprovider",
    ),
    CauseTwin(
        rule="pytest assert",
        slug="pytest-assert",
        marker=(
            '  File "/app/tests/test_greet.py", line 10, in test_greet\n'
            "E       assert 'ci_build' == 'ci-build'"
        ),
        base=PROJECT,
        note=(
            "the project's own test vs a doctest of a module in its own "
            "`src/`: PROJECT for both -- the frame is inside the checkout "
            "either way, which is exactly what the class claims"
        ),
        cause_a="project-test",
        preamble_a="##[group]Run pytest",
        cause_b="own-module-doctest",
        preamble_b="##[group]Run pytest --doctest-modules src/",
    ),
    CauseTwin(
        rule="pytest assert",
        slug="pytest-assert-unprovenanced",
        marker="E       assert 'ci_build' == 'ci-build'",
        base=None,
        note=(
            "the project's own test vs a doctest in a vendored dependency "
            "collected by the same run: UNCLASSIFIED for both -- pytest's `E` "
            "line alone names no file, and the old note called that a known "
            "limitation instead of declining to answer"
        ),
        cause_a="project-test",
        preamble_a="##[group]Run pytest",
        cause_b="vendored-dependency-doctest",
        preamble_b="##[group]Run pytest --doctest-modules vendor/",
    ),
    # --- PROJECT: location inside the checkout is not ownership --------------
    # The owner's check (2), 2026-09-22: "A path inside the checkout
    # establishes the LOCATION of an assertion, not project ownership: a CI
    # setup script can live there."
    CauseTwin(
        rule="assertion error",
        slug="assertion-error-in-the-ci-harness",
        marker=(
            '  File "/home/runner/work/r/r/.github/scripts/setup.py", line 3, '
            "in main\nAssertionError: precondition"
        ),
        base=None,
        note=(
            "the harness asserting a precondition the project broke vs the "
            "harness script itself being wrong: UNCLASSIFIED for both -- the "
            "frame is inside the checkout and under `.github/`, which says "
            "WHERE the assertion ran and nothing about whose it was"
        ),
        cause_a="project-broke-the-precondition",
        preamble_a="##[group]Run python .github/scripts/setup.py",
        cause_b="harness-script-defect",
        preamble_b="##[group]Run python .github/scripts/setup.py --strict",
    ),
    CauseTwin(
        rule="assertion error",
        slug="assertion-error-vendored-under-tests",
        marker=(
            '  File "/app/tests/vendor/test_third_party.py", line 10, in '
            "test_x\nAssertionError: 1 != 2"
        ),
        base=PROJECT,
        note=(
            "a suite the project wrote vs a third-party suite it vendored "
            "into `tests/`: PROJECT for both, by repository OWNERSHIP -- the "
            "project chose to carry that file and to run it, so the tree "
            "under test is what failed. The class names the tree, never the "
            "author, and `tests/` is not a CI-harness directory"
        ),
        cause_a="own-suite",
        preamble_a="##[group]Run pytest tests/",
        cause_b="vendored-third-party-suite",
        preamble_b="##[group]Run pytest tests/vendor/",
    ),
    # --- the shapes demoted on 2026-09-22 ------------------------------------
    CauseTwin(
        rule="entrypoint executable not found",
        slug="entrypoint-executable-not-found",
        marker=(
            "docker: Error response from daemon: unable to start container "
            'process: exec: "app": executable file not found in $PATH: unknown.'
        ),
        base=None,
        note=(
            "the image's authored CMD names the binary vs `--entrypoint app` "
            "overriding it at run time: UNCLASSIFIED for both -- the demotion, "
            "and the reason for it, in one pair"
        ),
        cause_a="authored-cmd",
        preamble_a="##[group]Run docker run --rm app:ci",
        cause_b="entrypoint-overridden",
        preamble_b="##[group]Run docker run --rm --entrypoint app app:ci",
    ),
    CauseTwin(
        rule="unresolvable action",
        slug="unresolvable-action",
        marker=(
            "Unable to resolve action actions/checkout@v99, unable to find version v99"
        ),
        base=None,
        note=(
            "a typo in the authored workflow vs an upstream tag deleted after "
            "the workflow was written: UNCLASSIFIED for both -- the snapshot "
            "carries nothing from the other side of the reference"
        ),
        cause_a="typo",
        preamble_a="##[group]Run actions/checkout@v99",
        cause_b="upstream-tag-deleted",
        preamble_b="Download action repository 'actions/checkout@v99'",
    ),
)


def _cause_twin_rows(twin: CauseTwin) -> list[Row]:
    """The pair as two rows; the expected verdict is one value for both."""
    outcome, kind = _expected(twin.base, False)
    return [
        Row(
            row_id=f"cause-twin-{twin.slug}-{cause}",
            source="log",
            level="plain line",
            text=f"{preamble}\n{twin.marker}",
            expected_outcome=outcome,
            expected_kind=kind,
            context="cause-twin",
            note=twin.note,
        )
        for cause, preamble in (
            (twin.cause_a, twin.preamble_a),
            (twin.cause_b, twin.preamble_b),
        )
    ]


ROWS: tuple[Row, ...] = (
    *_cross("artifact", ARTIFACT_MARKERS, ARTIFACT),
    *_cross("project", PROJECT_MARKERS, PROJECT),
    *_cross("environment", ENVIRONMENT_MARKERS, ENVIRONMENT),
    *_cross("symptom-only", SYMPTOM_MARKERS, None),
    *_cross("demoted", DEMOTED_MARKERS, None),
    *_cross("unprovenanced", PROVENANCELESS_MARKERS, None),
    *_cross("unframed-environment", UNFRAMED_ENVIRONMENT_MARKERS, None),
    *_cross("wrong-configured-address", MISCONFIGURED_ENDPOINT_MARKERS, None),
    *_cross("unknown", UNKNOWN_MARKERS, None),
    *[
        row
        for marker_id, text, ambiguous, expected_base in TWINS
        for row in _log_rows(
            "twin",
            marker_id,
            text,
            expected_base,
            ambiguous,
            shapes=(LOG_SHAPES[0],),
        )
        + _annotation_rows(
            "twin", marker_id, text, expected_base, ambiguous, levels=("failure",)
        )
    ],
    *_placement_rows(
        "artifact",
        "dockerfile-parse-error",
        _ARTIFACT_MARKER,
        ARTIFACT,
        ("failure", "warning"),
    ),
    *_placement_rows(
        "project", "assertion-error", _PROJECT_MARKER, PROJECT, ("failure",)
    ),
    *_placement_rows(
        "environment",
        "apt-fetch-timeout",
        _ENVIRONMENT_MARKER,
        ENVIRONMENT,
        ("failure",),
    ),
    *_placement_rows(
        "symptom-only", "bare-no-such-file", _SYMPTOM_MARKER, None, ("failure",)
    ),
    *CONFLICT_ROWS,
    *[row for twin in CAUSE_TWINS for row in _cause_twin_rows(twin)],
)


def _job(row: Row) -> FailedJob:
    """One failed job carrying exactly the row's piece of evidence."""
    if row.source == "log":
        evidence = [Evidence(source=None, text=row.text)]
    else:
        evidence = [Evidence(source=JOB_ID, text=row.text, level=row.level)]
    return FailedJob(
        job_id=JOB_ID,
        name="build",
        conclusion="failure",
        steps=[],
        evidence=evidence,
        completeness=COMPLETE,
    )


@pytest.mark.parametrize("row", ROWS, ids=[row.row_id for row in ROWS])
def test_combination(row: Row) -> None:
    """Every cell of the table, against the unified rule."""
    verdict = classify_failure(_job(row), step=None, completeness=COMPLETE)
    assert (verdict.outcome, verdict.kind) == (
        row.expected_outcome,
        row.expected_kind,
    ), verdict.observations
    if row.expected_outcome == "CLASSIFIED":
        assert verdict.evidence, "a class must cite the evidence it rests on"
    else:
        assert verdict.evidence == [], "nothing established, nothing cited"
    if row.expects_ambiguity:
        assert any(o.startswith("ambiguous: ") for o in verdict.observations)


def test_every_row_is_addressable() -> None:
    """A duplicate id would silently hide a cell behind another."""
    assert len({row.row_id for row in ROWS}) == len(ROWS)


def test_every_artifact_shape_has_a_negative_twin() -> None:
    """The third bullet, counted rather than trusted: as many twin markers as
    kept AUTHORING shapes."""
    twins = {row.row_id for row in ROWS if row.context == "twin"}
    assert twins
    assert not [
        row
        for row in ROWS
        if row.context == "twin" and row.expected_kind is FailureKind.AUTHORING
    ]
    assert len({marker_id for marker_id, _, _, _ in TWINS}) >= len(ARTIFACT_MARKERS)


def test_every_catalogue_rule_has_a_cause_twin_pair() -> None:
    """The class, not the instance: coverage is asserted against `RULES`
    itself, so a rule added later has no cause twin and says so here rather
    than shipping unexamined."""
    covered = {twin.rule for twin in CAUSE_TWINS}
    assert {rule.name for rule in RULES} <= covered, (
        f"rules without a cause twin: {sorted({r.name for r in RULES} - covered)}"
    )
    # The demoted shapes keep their pairs: a demotion with no twin is a
    # claim nobody rechecks.
    assert {
        "entrypoint executable not found",
        "unresolvable action",
        "unknown instruction",
        "copy/add source not found",
        "copy/add failed in build context",
    } <= covered
    assert len({twin.slug for twin in CAUSE_TWINS}) == len(CAUSE_TWINS)


def test_every_cause_twin_carries_its_reason() -> None:
    """A row whose expected value is "both, today" is unreadable without the
    reason; the note is part of the row, not of a comment that can drift."""
    assert all(twin.note for twin in CAUSE_TWINS)
    pairs = [row for row in ROWS if row.context == "cause-twin"]
    assert len(pairs) == 2 * len(CAUSE_TWINS)
    assert all(row.note for row in pairs)


@pytest.mark.parametrize("twin", CAUSE_TWINS, ids=[t.slug for t in CAUSE_TWINS])
def test_cause_twin_members_are_indistinguishable(twin: CauseTwin) -> None:
    """The pair's whole point: two causes, one verdict.

    If these ever diverge, the catalogue has found a discriminator the note
    says does not exist — and then the note is what must change, not this
    assertion."""
    first, second = _cause_twin_rows(twin)
    a = classify_failure(_job(first), step=None, completeness=COMPLETE)
    b = classify_failure(_job(second), step=None, completeness=COMPLETE)
    assert (a.outcome, a.kind) == (b.outcome, b.kind), (
        a.observations,
        b.observations,
    )


# --- the run summary: two independent failures are not a conflict (spec §4) --
# Not a row of the table above: a row is one evidence pool and one verdict,
# while this is two jobs, two verdicts and the run summary over them.


def _run(*jobs: FailedJob) -> FailedRun:
    return FailedRun(
        repo="example/project",
        run_id=4242,
        attempt=1,
        head_sha="deadbeef",
        url="https://github.com/example/project/actions/runs/4242",
        jobs=list(jobs),
        completeness=COMPLETE,
    )


def _job_with(job_id: int, text: str) -> FailedJob:
    ref = StepRef(job_id, 1)
    return FailedJob(
        job_id=job_id,
        name=f"job-{job_id}",
        conclusion="failure",
        steps=[
            FailedStep(
                ref=ref,
                name="run",
                conclusion="failure",
                evidence=[Evidence(source=ref, text=text)],
            )
        ],
        evidence=[],
        completeness=COMPLETE,
    )


def test_two_independent_jobs_keep_both_causes() -> None:
    """An artifact defect in job 1 and a project defect in job 2 are two
    answers, not a contradiction: the run is CLASSIFIED and carries both."""
    diagnosis = diagnose_run(
        _run(_job_with(1, _ARTIFACT_MARKER), _job_with(2, _PROJECT_MARKER))
    )
    assert diagnosis.outcome == "CLASSIFIED"
    assert diagnosis.causes == [FailureKind.AUTHORING, FailureKind.PROJECT]


def test_two_independent_jobs_do_not_depend_on_their_order() -> None:
    forward = diagnose_run(
        _run(_job_with(1, _ARTIFACT_MARKER), _job_with(2, _PROJECT_MARKER))
    )
    backward = diagnose_run(
        _run(_job_with(2, _PROJECT_MARKER), _job_with(1, _ARTIFACT_MARKER))
    )
    assert forward.outcome == backward.outcome
    assert forward.causes == backward.causes


def print_table() -> None:
    """Render the table as markdown, for review outside the test run."""
    print("| row_id | source | level | context | expected | note |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in ROWS:
        expected = row.expected_outcome
        if row.expected_outcome == "CLASSIFIED":
            expected = f"CLASSIFIED / {row.expected_kind.value}"
        elif row.expects_ambiguity:
            expected = "UNCLASSIFIED / unknown (ambiguous)"
        else:
            expected = "UNCLASSIFIED / unknown"
        level = row.level if row.level is not None else "absent"
        if row.placement:
            level = f"{level}, marker {row.placement}"
        print(
            f"| {row.row_id} | {row.source} | {level} | {row.context} "
            f"| {expected} | {row.note} |"
        )


if __name__ == "__main__":
    print_table()
