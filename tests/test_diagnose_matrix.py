"""The combination table: source x level x context x placement x co-occurrence.

One parametrised test over explicit rows. Each row is one piece of evidence
and what the reading layer must report for it: the outcome (``UNCLASSIFIED``
for every complete read -- the layer asserts no cause) and the SET OF
OBSERVATION NAMES, each prefixed ``warning-shaped: `` where the line was one a
tool marked as noticed rather than fatal. The expectation is stated by hand
per marker, never derived from the module under test.

What the rows pin:

- the LEVEL of a message decides only the label -- ``warning``/``notice``,
  whether an annotation's level or a log line's own prefix, in whatever case;
  the line is still reported, and no level changes the outcome;
- a shape is a LINE SHAPE: the parser's own sentence, a tool's own framing,
  pytest's own summary line. The same words in another context (the twins)
  yield whatever shapes that context really carries, which is often none --
  and never a cause;
- two shapes in one block are two observations, not a conflict.

Beside the negative twins, which vary the SHAPE, the table carries CAUSE
TWINS, which keep the message text identical and vary only what produced it.
Both members of a pair expect the SAME observations, and the pair's ``note``
records why the snapshot cannot tell the causes apart -- which is the reason
this layer asserts none. The notes are the documentation of that decision, and
``print_table()`` (``python -m`` this module, or run it as a script) renders
the whole table as markdown for review. It is never called from a test: a
matrix that prints is a matrix nobody reads.
"""

from dataclasses import dataclass

import pytest

from deployer.diagnose import OBSERVATIONS, Outcome, diagnose_run, read_failure
from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
    FailedStep,
    StepRef,
)

JOB_ID = 77
COMPLETE = Completeness(logs="present", annotations="present")
NO_OBSERVATION = "no observation matched"
WARNING_SHAPED = "warning-shaped: "

Source = str
Context = str
Names = frozenset[str]


@dataclass(frozen=True)
class Row:
    """One cell of the table: the evidence, and what must be reported for it.

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
    expected_observations: Names
    context: Context
    placement: str = ""
    note: str = ""
    """Why this row reads the way it does, for a reader of ``print_table()``.

    Carried by the cause twins below, where the expected value is a statement
    about what the snapshot can honestly report rather than a cause.
    """


@dataclass(frozen=True)
class Marker:
    """One shape's text, and the observation names it carries.

    ``names`` is what the text reports when it is failure evidence -- a plain
    log line, a buildkit-framed one, or an annotation at any level (a noticed
    level labels the same names). ``prefixed`` is what it reports under a log
    line's OWN severity prefix (``W: ``, ``warning: ``, ...): ``None`` means
    the same names, labelled; the shapes anchored to the line's head (the
    assertion and exception shapes, apt's ``E: ``) do not match behind such a
    prefix at all, and say so with an empty set.
    """

    marker_id: str
    text: str
    names: Names
    prefixed: Names | None = None

    def under_severity(self) -> Names:
        if self.prefixed is not None:
            return self.prefixed
        return frozenset(f"{WARNING_SHAPED}{name}" for name in self.names)


def _names(*names: str) -> Names:
    return frozenset(names)


ANCHORED: Names = frozenset()

# --- the markers, one per catalogue shape ------------------------------------

# The tool that owns an artifact, about the artifact's own text: the
# Dockerfile parser's sentence, and GitHub's validator framing over the
# workflow YAML.
PARSER_FRAMED_MARKERS = (
    Marker(
        "dockerfile-parse-error",
        "Dockerfile parse error on line 3: unexpected end of statement",
        _names("dockerfile parse error"),
    ),
    Marker(
        "unrecognized-named-value",
        "The workflow is not valid. .github/workflows/ci.yml (Line: 31, "
        "Col: 9): Unrecognized named-value: 'secret'. Located at position 1 "
        "within expression: secret.TOKEN",
        _names("unrecognized named-value"),
    ),
)

# The container runtime's and the runner's own shapes, and the two COPY/ADD
# shapes: each names what the tool could not do, never who arranged it.
RUNTIME_SHAPE_MARKERS = (
    Marker(
        "entrypoint-missing",
        "docker: Error response from daemon: unable to start container "
        'process: exec: "serve": executable file not found in $PATH: unknown.',
        _names("entrypoint executable not found"),
    ),
    Marker(
        "unresolvable-action",
        "Unable to resolve action actions/checkout@v99, unable to find version v99",
        _names("unresolvable action"),
    ),
    Marker(
        "unknown-instruction",
        "dockerfile parse error on line 3: unknown instruction: FORM "
        "(did you mean FROM?)",
        _names("unknown instruction"),
    ),
    Marker(
        "copy-not-found",
        "ERROR: failed to solve: failed to compute cache key: failed to "
        'calculate checksum of ref abc::def: "/docs/setup.md": not found',
        _names("copy/add source not found"),
    ),
    Marker(
        "copy-failed",
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory",
        _names("copy/add failed in build context", "no such file"),
    ),
)

# An assertion under its traceback frame or pytest node id, as real tool
# output prints it. The frame is not a shape and decides nothing; the
# assertion line is anchored to the line's head, so a severity prefix on it
# is not the shape any more.
_PROJECT_FRAME = '  File "/app/tests/test_greet.py", line 10, in test_greet'
_PROJECT_NODE = "FAILED tests/test_greet.py::test_greet"

FRAMED_ASSERTION_MARKERS = (
    Marker(
        "assertion-error",
        f"{_PROJECT_FRAME}\n"
        "E   AssertionError: 'hello from ci_build' != 'hello from ci-build'",
        _names("assertion error"),
        ANCHORED,
    ),
    Marker(
        "pytest-failed-assertion",
        f"{_PROJECT_NODE} - AssertionError: 1 != 2",
        _names("pytest failed with assertion"),
        ANCHORED,
    ),
    Marker(
        "pytest-bare-assert",
        f"{_PROJECT_NODE} - assert 1 == 2",
        _names("pytest bare assert"),
        ANCHORED,
    ),
    Marker(
        "pytest-assert",
        f"{_PROJECT_FRAME}\nE       assert 'ci_build' == 'ci-build'",
        _names("pytest assert"),
        ANCHORED,
    ),
)

# The same assertion shapes without a frame into the checkout: the runner's
# own setup step, a vendored dependency's doctest, a synthetic `python -c`
# frame. They read exactly as the framed ones -- the frame decides nothing.
BARE_ASSERTION_MARKERS = (
    Marker(
        "bare-assertion-error",
        "AssertionError: 1 != 2",
        _names("assertion error"),
        ANCHORED,
    ),
    Marker(
        "runner-setup-assertion",
        'File "<string>", line 1, in <module>',
        _names(),
    ),
    Marker(
        "vendored-doctest-assertion",
        '  File "/usr/lib/python3.12/site-packages/vendorlib/check.py", '
        "line 8, in verify\nAssertionError: 1 != 2",
        _names("assertion error"),
        ANCHORED,
    ),
    Marker(
        "harness-node-id",
        "FAILED /usr/lib/python3/dist-packages/vendorlib/tests/test_a.py"
        "::test_a - AssertionError: 1 != 2",
        _names("pytest failed with assertion"),
        ANCHORED,
    ),
)
# A tool's own report of the network. apt's `E: ` line carries two shapes
# (its fetch failure, and the timeout it names); a severity prefix REPLACES
# apt's `E: `, and the bare words behind `W: ` are no tool's framing.
TOOL_FRAMED_MARKERS = (
    Marker(
        "apt-fetch-timeout",
        "E: Failed to fetch http://deb.debian.org/debian/x.deb  Connection "
        "timed out [IP: 1.2.3.4 80]",
        _names("fetch failure", "connection timed out"),
        ANCHORED,
    ),
    Marker(
        "host-unresolvable",
        "curl: (6) Could not resolve host: pypi.org",
        _names("host unresolvable"),
    ),
)

# The same network words with no tool's framing around them: an application
# under test printing what it was written to print. No shape at all.
BARE_WORDS_MARKERS = (
    Marker("bare-timeout", "requests: connection timed out after 5s", _names()),
    Marker(
        "bare-503",
        "error parsing HTTP 503 response body: 503 Service Unavailable",
        _names(),
    ),
    Marker("bare-disk-full", "tmpfs write failed: no space left on device", _names()),
    Marker("bare-name-resolution", "Temporary failure in name resolution", _names()),
)

BARE_SHAPE_MARKERS = (
    Marker(
        "bare-no-such-file",
        "cp: cannot stat '/app/main.py': No such file or directory",
        _names("no such file"),
    ),
    Marker(
        "exec-format-error",
        "standard_init_linux.go:228: exec user process caused: exec format error",
        _names("exec format error"),
    ),
    Marker(
        "command-not-found",
        "/bin/bash: line 12: pytest: command not found",
        _names(),
    ),
)

UNKNOWN_MARKERS = (
    Marker("exit-code", "Process completed with exit code 1.", _names()),
)

# The negative twins: the words of a parser-framed shape in a context with
# another source. What each row honestly carries is the shapes its line has
# -- a shell's `no such file`, a pytest summary line, sometimes nothing -- and
# an unanchored prose shape whose words the line quotes (`dockerfile parse
# error`, `unknown instruction`) is reported as the words it is. None of it
# is a cause.
TWINS = (
    Marker(
        "shell-stat", "stat app.py: no such file or directory", _names("no such file")
    ),
    Marker(
        "cat-missing-config",
        "cat: config.yml: No such file or directory",
        _names("no such file"),
    ),
    Marker(
        "buildkit-run-exit",
        'ERROR: failed to solve: process "/bin/sh -c uv sync --frozen" did '
        "not complete successfully: exit code: 1",
        _names(),
    ),
    # buildkit's other `: not found` ending: an image reference it could not
    # pull. The COPY/ADD shape demands the QUOTED path of a build-context
    # source; loosen it back to `...: not found` and this row goes red.
    Marker(
        "image-ref-not-found",
        "ERROR: failed to solve: python:3.12-slim: "
        "docker.io/library/python:3.12-slim: not found",
        _names(),
    ),
    Marker("shell-command-not-found", "bash: foo: command not found", _names()),
    Marker(
        "pytest-executable-not-found",
        "E   RuntimeError: executable file not found in the sandbox",
        _names("executable not found", "exception"),
    ),
    Marker(
        "assertion-about-parse-error",
        "FAILED tests/test_build.py::test_parse_error - AssertionError: "
        "expected 'Dockerfile parse error' in captured stderr",
        _names("pytest failed with assertion", "dockerfile parse error"),
    ),
    Marker(
        "assertion-about-unknown-instruction",
        "FAILED tests/test_build.py::test_unknown_instruction - "
        "AssertionError: unknown instruction: FORM was not reported",
        _names("pytest failed with assertion", "unknown instruction"),
    ),
    Marker(
        "assertion-about-named-value",
        "FAILED tests/test_ci.py::test_named_value - AssertionError: "
        "Unrecognized named-value was expected",
        _names("pytest failed with assertion"),
    ),
)

# --- the variants ------------------------------------------------------------

# A log line's own severity, at its head: apt's `W: `, the `warning: `/
# `notice: ` compilers and pip write -- in whatever case the tool wrote them
# -- and buildkit's `#<step> <seconds> ` framing, which is not a severity at
# all and must not read as one.
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


def _labelled(names: Names, noticed: bool) -> Names:
    """An annotation's noticed level labels every name; a failing one none."""
    if not noticed:
        return names
    return frozenset(f"{WARNING_SHAPED}{name}" for name in names)


def _log_rows(
    context: Context,
    marker: Marker,
    shapes: tuple[tuple[str, str, str], ...] = LOG_SHAPES,
) -> list[Row]:
    rows = []
    for slug, display, prefix in shapes:
        expected = marker.under_severity() if slug in _NOTICED_SHAPES else marker.names
        rows.append(
            Row(
                row_id=f"log-{slug}-{context}-{marker.marker_id}",
                source="log",
                level=display,
                text=_shaped(marker.text, prefix),
                expected_outcome="UNCLASSIFIED",
                expected_observations=expected,
                context=context,
            )
        )
    return rows


def _annotation_rows(
    context: Context,
    marker: Marker,
    levels: tuple[str | None, ...] = ANNOTATION_LEVELS,
) -> list[Row]:
    rows = []
    for level in levels:
        rows.append(
            Row(
                row_id=f"ann-{level or 'absent'}-{context}-{marker.marker_id}",
                source="annotation",
                level=level,
                text=marker.text,
                expected_outcome="UNCLASSIFIED",
                expected_observations=_labelled(marker.names, level in _NOTICED_LEVELS),
                context=context,
            )
        )
    return rows


def _cross(context: Context, markers: tuple[Marker, ...]) -> list[Row]:
    """Every marker of one context against every source and level."""
    return [
        row
        for marker in markers
        for row in _log_rows(context, marker) + _annotation_rows(context, marker)
    ]


def _placement_rows(
    context: Context, marker: Marker, levels: tuple[str | None, ...]
) -> list[Row]:
    """The same marker, moved around a multi-line block.

    A log block is judged per matched line, an annotation by its level for
    every line of its message; both must hold wherever the marker sits.
    """
    rows = []
    for placement in PLACEMENTS:
        block = _placed(marker.text, placement)
        rows.append(
            Row(
                row_id=f"log-plain-{context}-{marker.marker_id}-at-{placement}",
                source="log",
                level="plain line",
                text=block,
                expected_outcome="UNCLASSIFIED",
                expected_observations=marker.names,
                context=context,
                placement=placement,
            )
        )
        for level in levels:
            rows.append(
                Row(
                    row_id=f"ann-{level}-{context}-{marker.marker_id}-at-{placement}",
                    source="annotation",
                    level=level,
                    text=block,
                    expected_outcome="UNCLASSIFIED",
                    expected_observations=_labelled(
                        marker.names, level in _NOTICED_LEVELS
                    ),
                    context=context,
                    placement=placement,
                )
            )
    return rows


_PARSER_MARKER = PARSER_FRAMED_MARKERS[0]
_ASSERTION_MARKER = FRAMED_ASSERTION_MARKERS[0]
_TOOL_MARKER = TOOL_FRAMED_MARKERS[0]
_BARE_MARKER = BARE_SHAPE_MARKERS[0]
# apt's `Err:` detail line, recovered: it keeps the framing the timeout shape
# requires (host and port), so the shape really does match it and the
# WARNING LABEL is what these rows pin.
_RECOVERED_FETCH = (
    "W: Could not connect to deb.debian.org:80 (1.2.3.4), connection "
    "timed out [retrying]"
)

TWO_SHAPE_ROWS = (
    Row(
        row_id="log-plain-two-shapes-parser-then-assertion",
        source="log",
        level="plain line",
        text=f"{_PARSER_MARKER.text}\n{_ASSERTION_MARKER.text}",
        expected_outcome="UNCLASSIFIED",
        expected_observations=_names("dockerfile parse error", "assertion error"),
        context="two-shapes",
    ),
    Row(
        row_id="log-plain-two-shapes-assertion-then-parser",
        source="log",
        level="plain line",
        text=f"{_ASSERTION_MARKER.text}\n{_PARSER_MARKER.text}",
        expected_outcome="UNCLASSIFIED",
        expected_observations=_names("dockerfile parse error", "assertion error"),
        context="two-shapes",
    ),
    Row(
        row_id="log-plain-two-shapes-warning-timeout-then-parser",
        source="log",
        level="plain line",
        text=f"{_RECOVERED_FETCH}\n{_PARSER_MARKER.text}",
        expected_outcome="UNCLASSIFIED",
        expected_observations=_names(
            f"{WARNING_SHAPED}connection timed out", "dockerfile parse error"
        ),
        context="two-shapes",
    ),
    Row(
        row_id="log-plain-two-shapes-parser-then-warning-timeout",
        source="log",
        level="plain line",
        text=f"{_PARSER_MARKER.text}\n{_RECOVERED_FETCH}",
        expected_outcome="UNCLASSIFIED",
        expected_observations=_names(
            f"{WARNING_SHAPED}connection timed out", "dockerfile parse error"
        ),
        context="two-shapes",
    ),
)
"""Two shapes in one pool are two observations, whichever order they sit in;
a recovered line keeps its label beside a failing one. Nothing is
`ambiguous:` because nothing competes for a cause."""

# --- cause twins: the SAME message, a different cause ------------------------
# The negative twins above vary the SHAPE: different words, so different (or
# no) observations. A cause twin keeps the message text IDENTICAL and changes
# only what produced it -- the question the owner put to the catalogue on
# 2026-09-22, and the one that closed it: for every shape below there is a
# pair of causes the snapshot cannot tell apart, or a pair it can tell apart
# only by the tool's framing, which establishes the SOURCE of a message and
# not its cause. Both members of every pair therefore expect the same
# observations and no cause, and the note records why. Where an older note
# said "residual, stated" the residual is now the whole answer: the layer
# reports the shape and asserts nothing about who arranged it.
#
# The preambles are ordinary log lines from each scenario. They deliberately
# do not encode the cause: a real snapshot does not either, which is the
# finding these rows carry.


@dataclass(frozen=True)
class CauseTwin:
    """One message text, two causes, and what the snapshot may report."""

    rule: str
    slug: str
    marker: str
    names: Names
    note: str
    cause_a: str
    preamble_a: str
    cause_b: str
    preamble_b: str


_BUILD_CONTEXT = "#2 [internal] load build context\n#2 transferring context: 41.09kB"
_SMALL_CONTEXT = "#2 [internal] load build context\n#2 transferring context: 1.21kB"

CAUSE_TWINS: tuple[CauseTwin, ...] = (
    # --- the parser-framed shapes ----------------------------------------------
    CauseTwin(
        rule="dockerfile parse error",
        slug="dockerfile-parse-error",
        marker="Dockerfile parse error on line 3: unexpected end of statement",
        names=_names("dockerfile parse error"),
        note=(
            "hand-written Dockerfile vs one a build script generated: the "
            "parser's own sentence names the artifact whose text will not "
            "parse, never who typed it or whether the text the parser saw is "
            "the text that was authored -- reported as the parser's shape"
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
        names=_names("unrecognized named-value"),
        note=(
            "the repo's own workflow vs a reusable workflow it calls: GitHub's "
            "own validator says the NAME is not in the language; whose YAML it "
            "is the snapshot does not say -- reported as the validator's shape"
        ),
        cause_a="own-workflow",
        preamble_a="##[error].github/workflows/ci.yml (Line: 31, Col: 9)",
        cause_b="called-reusable-workflow",
        preamble_b="##[error]org/ci-workflows/.github/workflows/build.yml@v2",
    ),
    # --- the words the parser frames, and an application also says -----------
    CauseTwin(
        rule="unknown instruction",
        slug="unknown-instruction",
        marker=(
            "dockerfile parse error on line 7: unknown instruction: "
            "RUN --mount=type=cache,target=/root/.cache"
        ),
        names=_names("unknown instruction"),
        note=(
            "a typo in the authored file vs an instruction a builder too old to "
            "know it rejects: the parser's framing sharing the line is exactly "
            "what an application quoting the parser also prints, so the framing "
            "beside the words proves only that two sentences share a line -- "
            "reported as `unknown instruction`, once"
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
        names=_names("copy/add source not found"),
        note=(
            "a path the COPY never had right vs a file the project removed "
            "AFTER the Dockerfile was authored: the snapshot cannot say which "
            "side moved. This is live acceptance run 1; reported as buildkit's "
            "own shape and nothing more"
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
        names=_names("copy/add failed in build context", "no such file"),
        note=(
            "a source that is not in the repo vs one the .dockerignore "
            "excludes: docker's shape beside buildkit's, and the bare `no such "
            "file` the line also carries -- two observations, no cause"
        ),
        cause_a="absent-from-repo",
        preamble_a=_BUILD_CONTEXT,
        cause_b="excluded-by-dockerignore",
        preamble_b=_SMALL_CONTEXT,
    ),
    # --- the network, framed: the tool says IT could not ----------------------
    # Both members are a genuine failure of the tool that printed the line.
    # The framing establishes the SOURCE of the message -- which is why the
    # shape is reported under the tool's name -- and never who arranged the
    # condition the tool ran into; the live run-2 (an apt source at a custom
    # unroutable address, i.e. the project's own configuration) prints the
    # same apt line as an unreachable mirror. The pairs that set a real
    # failure against a test PRINTING the phrase are below instead, unframed.
    CauseTwin(
        rule="docker daemon unreachable",
        slug="docker-daemon-unreachable",
        marker=(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
            " Is the docker daemon running?"
        ),
        names=_names("docker daemon unreachable"),
        note=(
            "the runner's daemon stopped vs a self-hosted runner whose socket "
            "was never mounted into the job container: docker's own client "
            "says IT could not reach a daemon; why, the line does not say"
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
        names=_names("host unresolvable"),
        note=(
            "the runner's resolver down vs an egress proxy refusing the name: "
            "curl's own exit-code framing says curl could not resolve it, not "
            "the reason the name did not resolve"
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
        names=_names("name resolution failure"),
        note=(
            "the runner's resolver down vs a build step run with networking "
            "switched off on purpose: apt names the host it could not resolve "
            "either way, and whether the absent network was the runner's "
            "fault or the step's design the line does not carry"
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
        names=_names("fetch failure"),
        note=(
            "a mirror that rotated the package out vs a version the Dockerfile "
            "pinned that the suite no longer carries: apt says IT could not "
            "fetch what it was asked for; the second is arguably an artifact "
            "defect, and the snapshot cannot see the pin"
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
            "#11 15.43   Could not connect to 10.255.255.1:80 (10.255.255.1), "
            "connection timed out"
        ),
        names=_names("connection timed out"),
        note=(
            "an unroutable mirror vs the runner's egress firewall: apt's own "
            "detail line names the host AND the port it could not reach, and "
            "nothing about why. Verbatim from live acceptance run 2, whose "
            "unroutable address was the project's own apt source"
        ),
        cause_a="unroutable-mirror",
        preamble_a="#11 [stage-0 4/9] RUN apt-get update",
        cause_b="runner-egress-firewall",
        preamble_b="#11 [stage-0 4/9] RUN apt-get -o Acquire::Retries=0 update",
    ),
    CauseTwin(
        rule="registry rate limit",
        slug="registry-rate-limit",
        marker="toomanyrequests: You have reached your pull rate limit.",
        names=_names("registry rate limit"),
        note=(
            "a shared runner IP that exhausted the anonymous quota vs a job "
            "that lost its registry login: the registry's own refusal, and in "
            "both the pull is the thing that could not be made"
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
        names=_names("service unavailable"),
        note=(
            "the registry down vs the registry shedding load: buildkit says IT "
            "got the 503, which is what separates this line from an app's "
            "fixture printing the same three words"
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
        names=_names("disk full"),
        note=(
            "the runner's disk filled by earlier jobs vs an image whose layers "
            "do not fit it at all: the path is the daemon's own storage, so it "
            "is the daemon that ran out; whose fault, the line does not say"
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
        names=_names("runner shutdown"),
        note=(
            "a spot instance reclaimed mid-job vs the runner service restarted "
            "under it: the runner's own sentence about itself. The twin that "
            "used to sit here, a human cancelling the run, is GONE: a cancelled "
            "run's conclusion is `cancelled`, which `forge` refuses "
            "(`_FAILED_CONCLUSIONS`), so it never reaches this layer"
        ),
        cause_a="instance-reclaimed",
        preamble_a="##[group]Run pytest",
        cause_b="runner-service-restarted",
        preamble_b="##[group]Run pytest -x",
    ),
    # --- the network, unframed: the words without the tool -------------------
    # A real failure against a test PRINTING the phrase: nothing in the line
    # says who printed it, so the tool-framed shapes do not match, and only
    # what the line otherwise is (an exception, or nothing) is reported.
    CauseTwin(
        rule="connection timed out",
        slug="connection-timed-out-unframed",
        marker="requests: connection timed out after 5s",
        names=_names(),
        note=(
            "the runner's network vs the app's OWN retry test printing the "
            "phrase it is testing: no shape, nothing reported"
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
        names=_names(),
        note=(
            "an upstream really down, reported by a tool this catalogue does "
            "not know, vs the app's HTTP fixture printing a canned response: no "
            "shape, nothing reported. The cost is stated here -- the first is a "
            "real upstream failure the layer does not name, which is the price "
            "of never naming the second"
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
        names=_names("exception"),
        note=(
            "the runner's DNS down vs a step that deliberately probes an "
            "unreachable host to prove the image needs no network: an exception "
            "carries no tool's framing, and is reported as the exception it is"
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
        names=_names(),
        note=(
            "the resolver down vs a DNS test printing what it asserts on: the "
            "bare sentence names no tool, nothing reported"
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
        names=_names("exception"),
        note=(
            "a browser test whose fetch really failed vs one asserting on the "
            "message: jest's own exception, reported as one -- the over-firer "
            "`TODO.md` recorded by name, closed by the framing requirement"
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
        names=_names(),
        note=(
            "the runner's disk filled vs a test writing to a deliberately tiny "
            "tmpfs to prove the app survives ENOSPC: no shape, nothing reported"
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
        names=_names(),
        note=(
            "a test replaying a recorded 429 body vs one asserting the client "
            "handles it: the word alone is not the registry speaking, nothing "
            "reported"
        ),
        cause_a="recorded-response-replayed",
        preamble_a="##[group]Run pytest tests/test_registry.py -k ratelimit",
        cause_b="contract-test",
        preamble_b="##[group]Run pytest tests/test_registry.py -k contract",
    ),
    # --- assertions: with a frame, and without ---------------------------------
    # A frame or node id inside the checkout says where an assertion RAN, not
    # whose it is; one outside says the same of another place. Neither is a
    # cause, so every pair reads as the assertion shape and nothing more.
    CauseTwin(
        rule="assertion error",
        slug="assertion-error-with-frame",
        marker=(
            '  File "/app/tests/test_greet.py", line 10, in test_greet\n'
            "AssertionError: 1 != 2"
        ),
        names=_names("assertion error"),
        note=(
            "the project's own test vs one the CI harness copied INTO the "
            "checkout: the frame names a file of the tree that was tested, and "
            "a file vendored there is part of it -- the assertion is reported, "
            "the frame decides nothing"
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
        names=_names("assertion error"),
        note=(
            "the project's own test vs the CI's setup script asserting a "
            "precondition: without a frame the snapshot does not say whose "
            "assertion failed, and with one it would say only where it ran"
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
        names=_names("pytest failed with assertion"),
        note=(
            "the project's own suite vs a test file vendored into the "
            "checkout: pytest prints the node id relative to its rootdir, so "
            "the path is the checkout's either way -- reported as pytest's "
            "summary shape"
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
        names=_names("pytest failed with assertion"),
        note=(
            "a conftest plugin the CI image installs vs a vendored "
            "dependency's own suite collected by the same run: the node id "
            "names a path outside the checkout, and the summary line is "
            "reported exactly as one inside it would be"
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
        names=_names("pytest bare assert"),
        note=(
            "the project's own assertion vs one in a conftest plugin that "
            "lives in the checkout: both are files of the tree under test, "
            "and the summary line is what is reported"
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
        names=_names("pytest assert"),
        note=(
            "the project's own test vs a doctest of a module in its own "
            "`src/`: the frame is inside the checkout either way, and the `E` "
            "line is what is reported"
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
        names=_names("pytest assert"),
        note=(
            "the project's own test vs a doctest in a vendored dependency "
            "collected by the same run: pytest's `E` line alone names no file, "
            "and is reported as the shape it is"
        ),
        cause_a="project-test",
        preamble_a="##[group]Run pytest",
        cause_b="vendored-dependency-doctest",
        preamble_b="##[group]Run pytest --doctest-modules vendor/",
    ),
    # --- the container runtime and the runner --------------------------------
    CauseTwin(
        rule="entrypoint executable not found",
        slug="entrypoint-executable-not-found",
        marker=(
            "docker: Error response from daemon: unable to start container "
            'process: exec: "app": executable file not found in $PATH: unknown.'
        ),
        names=_names("entrypoint executable not found"),
        note=(
            "the image's authored CMD names the binary vs `--entrypoint app` "
            "overriding it at run time: the line names the binary, never WHO "
            "named it -- reported as the runtime's shape"
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
        names=_names("unresolvable action"),
        note=(
            "a typo in the authored workflow vs an upstream tag deleted after "
            "the workflow was written: the snapshot carries nothing from the "
            "other side of the reference -- reported as the runner's shape"
        ),
        cause_a="typo",
        preamble_a="##[group]Run actions/checkout@v99",
        cause_b="upstream-tag-deleted",
        preamble_b="Download action repository 'actions/checkout@v99'",
    ),
)


def _cause_twin_rows(twin: CauseTwin) -> list[Row]:
    """The pair as two rows; the expected observations are one set for both."""
    return [
        Row(
            row_id=f"cause-twin-{twin.slug}-{cause}",
            source="log",
            level="plain line",
            text=f"{preamble}\n{twin.marker}",
            expected_outcome="UNCLASSIFIED",
            expected_observations=twin.names,
            context="cause-twin",
            note=twin.note,
        )
        for cause, preamble in (
            (twin.cause_a, twin.preamble_a),
            (twin.cause_b, twin.preamble_b),
        )
    ]


ROWS: tuple[Row, ...] = (
    *_cross("parser-framed", PARSER_FRAMED_MARKERS),
    *_cross("framed-assertion", FRAMED_ASSERTION_MARKERS),
    *_cross("tool-framed", TOOL_FRAMED_MARKERS),
    *_cross("bare-shape", BARE_SHAPE_MARKERS),
    *_cross("runtime-shape", RUNTIME_SHAPE_MARKERS),
    *_cross("bare-assertion", BARE_ASSERTION_MARKERS),
    *_cross("bare-words", BARE_WORDS_MARKERS),
    *_cross("unknown", UNKNOWN_MARKERS),
    *[
        row
        for marker in TWINS
        for row in _log_rows("twin", marker, shapes=(LOG_SHAPES[0],))
        + _annotation_rows("twin", marker, levels=("failure",))
    ],
    *_placement_rows("parser-framed", _PARSER_MARKER, ("failure", "warning")),
    *_placement_rows("framed-assertion", _ASSERTION_MARKER, ("failure",)),
    *_placement_rows("tool-framed", _TOOL_MARKER, ("failure",)),
    *_placement_rows("bare-shape", _BARE_MARKER, ("failure",)),
    *TWO_SHAPE_ROWS,
    *[row for twin in CAUSE_TWINS for row in _cause_twin_rows(twin)],
)

_SHAPE_NAMES = frozenset(shape.name for shape in OBSERVATIONS)


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


def _observed_names(observations: list[str]) -> Names:
    """The observation NAMES a verdict reported, with their warning label.

    Every observation of a complete job-level read is `<name>: <line>` or
    `warning-shaped: <name>: <line>`, or the one note that nothing matched;
    anything else here is a shape of observation this table does not know.
    """
    if observations == [NO_OBSERVATION]:
        return frozenset()
    names = set()
    for observation in observations:
        label = ""
        if observation.startswith(WARNING_SHAPED):
            label = WARNING_SHAPED
            observation = observation[len(WARNING_SHAPED) :]
        name = observation.split(": ", 1)[0]
        assert name in _SHAPE_NAMES, observation
        names.add(label + name)
    return frozenset(names)


@pytest.mark.parametrize("row", ROWS, ids=[row.row_id for row in ROWS])
def test_combination(row: Row) -> None:
    """Every cell of the table: the outcome, the observation names, and
    that a citation exists exactly when something was observed."""
    verdict = read_failure(_job(row), step=None, completeness=COMPLETE)
    assert verdict.outcome == row.expected_outcome
    assert verdict.kind is None
    assert _observed_names(verdict.observations) == row.expected_observations, (
        verdict.observations
    )
    if row.expected_observations:
        assert verdict.evidence, "an observation must cite the block it was read from"
    else:
        assert verdict.evidence == [], "nothing observed, nothing cited"


def test_every_row_is_addressable() -> None:
    """A duplicate id would silently hide a cell behind another."""
    assert len({row.row_id for row in ROWS}) == len(ROWS)


def test_every_observation_shape_is_expected_by_some_row() -> None:
    """The mutation guard, counted rather than trusted: drop any shape from
    `OBSERVATIONS` and at least one row reddens, because every shape's name
    is stated as expected somewhere in this table."""
    expected = {
        name.removeprefix(WARNING_SHAPED)
        for row in ROWS
        for name in row.expected_observations
    }
    assert _SHAPE_NAMES <= expected, sorted(_SHAPE_NAMES - expected)
    assert expected <= _SHAPE_NAMES, sorted(expected - _SHAPE_NAMES)


def test_every_cause_twin_names_a_real_shape() -> None:
    """A twin written against a shape that no longer exists is a claim nobody
    rechecks; and the pairs are unique."""
    assert {twin.rule for twin in CAUSE_TWINS} <= _SHAPE_NAMES
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
    """The pair's whole point: two causes, one reading, no cause asserted.

    If these ever diverge, the layer has found a discriminator the note
    says does not exist -- and then the note is what must change, not this
    assertion."""
    first, second = _cause_twin_rows(twin)
    a = read_failure(_job(first), step=None, completeness=COMPLETE)
    b = read_failure(_job(second), step=None, completeness=COMPLETE)
    assert a.kind is b.kind is None
    assert _observed_names(a.observations) == _observed_names(b.observations), (
        a.observations,
        b.observations,
    )


# --- the run summary: two independent failures, two readings (spec §4) ------
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


def test_two_independent_jobs_keep_both_readings_and_no_cause() -> None:
    """A parser error in job 1 and an assertion in job 2 are two readings,
    each with its own observation; the run asserts no cause over either."""
    diagnosis = diagnose_run(
        _run(_job_with(1, _PARSER_MARKER.text), _job_with(2, _ASSERTION_MARKER.text))
    )
    assert diagnosis.outcome == "UNCLASSIFIED"
    assert diagnosis.causes == []
    assert [_observed_names(v.observations) for v in diagnosis.failures] == [
        _names("dockerfile parse error"),
        _names("assertion error"),
    ]


def test_two_independent_jobs_do_not_depend_on_their_order() -> None:
    forward = diagnose_run(
        _run(_job_with(1, _PARSER_MARKER.text), _job_with(2, _ASSERTION_MARKER.text))
    )
    backward = diagnose_run(
        _run(_job_with(2, _ASSERTION_MARKER.text), _job_with(1, _PARSER_MARKER.text))
    )
    assert forward.outcome == backward.outcome
    assert forward.causes == backward.causes == []
    assert {v.where: v.observations for v in forward.failures} == {
        v.where: v.observations for v in backward.failures
    }


def print_table() -> None:
    """Render the table as markdown, for review outside the test run."""
    print(
        "| row_id | source | level | context | expected outcome | observations | note |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for row in ROWS:
        level = row.level if row.level is not None else "absent"
        if row.placement:
            level = f"{level}, marker {row.placement}"
        observations = ", ".join(sorted(row.expected_observations)) or "-"
        print(
            f"| {row.row_id} | {row.source} | {level} | {row.context} "
            f"| {row.expected_outcome} | {observations} | {row.note} |"
        )


if __name__ == "__main__":
    print_table()
