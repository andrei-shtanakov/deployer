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
  is ambiguity, not a winner.

The rows are built by helpers from a small table of markers so that the cross
product is exhaustive rather than anecdotal; ``print_table()`` (``python -m``
this module, or run it as a script) renders them as markdown for review. It is
never called from a test: a matrix that prints is a matrix nobody reads.
"""

from dataclasses import dataclass

import pytest

from deployer.diagnose import Outcome, classify_failure, diagnose_run
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


# --- the markers, one per catalogue shape ------------------------------------
# `base` is the class the shape establishes when the evidence is failure
# evidence; `None` means the shape names no cause at all.

ARTIFACT = FailureKind.AUTHORING
PROJECT = FailureKind.PROJECT
ENVIRONMENT = FailureKind.ENVIRONMENT

ARTIFACT_MARKERS = (
    (
        "dockerfile-parse-error",
        "Dockerfile parse error on line 3: unexpected end of statement",
    ),
    ("unknown-instruction", "ERROR: unknown instruction: FORM (did you mean FROM?)"),
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
    (
        "unresolvable-action",
        "Unable to resolve action actions/checkout@v99, unable to find version v99",
    ),
    (
        "unrecognized-named-value",
        "Unrecognized named-value: 'secret'. Located at position 1 within "
        "expression: secret.TOKEN",
    ),
    (
        "entrypoint-missing",
        "docker: Error response from daemon: unable to start container "
        'process: exec: "serve": executable file not found in $PATH: unknown.',
    ),
)

PROJECT_MARKERS = (
    (
        "assertion-error",
        "E   AssertionError: 'hello from ci_build' != 'hello from ci-build'",
    ),
    (
        "pytest-failed-assertion",
        "FAILED tests/test_greet.py::test_greet - AssertionError: 1 != 2",
    ),
    ("pytest-bare-assert", "FAILED tests/test_greet.py::test_greet - assert 1 == 2"),
    ("pytest-assert", "E       assert 'ci_build' == 'ci-build'"),
)

ENVIRONMENT_MARKERS = (
    (
        "apt-fetch-timeout",
        "E: Failed to fetch http://deb.debian.org/debian/x.deb  Connection "
        "timed out [IP: 1.2.3.4 80]",
    ),
    ("host-unresolvable", "curl: (6) Could not resolve host: pypi.org"),
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
# with another cause. `ambiguous` marks the twins that collide with a PROJECT
# rule -- the catalogue reads those shapes as prose, so the honest verdict is
# the conflict, not the class. Either way: NOT AUTHORING.
TWINS = (
    ("shell-stat", "stat app.py: no such file or directory", False),
    ("cat-missing-config", "cat: config.yml: No such file or directory", False),
    (
        "buildkit-run-exit",
        'ERROR: failed to solve: process "/bin/sh -c uv sync --frozen" did '
        "not complete successfully: exit code: 1",
        False,
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
    ),
    ("shell-command-not-found", "bash: foo: command not found", False),
    (
        "pytest-executable-not-found",
        "E   RuntimeError: executable file not found in the sandbox",
        False,
    ),
    (
        "assertion-about-parse-error",
        "E   AssertionError: expected 'Dockerfile parse error' in captured stderr",
        True,
    ),
    (
        "assertion-about-unknown-instruction",
        "E   AssertionError: unknown instruction: FORM was not reported",
        True,
    ),
    (
        "assertion-about-unresolvable-action",
        "E   AssertionError: log should mention Unable to resolve action",
        True,
    ),
    (
        "assertion-about-named-value",
        "E   AssertionError: Unrecognized named-value was expected",
        True,
    ),
)

# --- the variants ------------------------------------------------------------

# A log line's own severity, at its head: apt's `W: `, the `warning: `/
# `notice: ` compilers and pip write, and buildkit's `#<step> <seconds> `
# framing, which is not a severity at all and must not read as one.
LOG_SHAPES = (
    ("plain", "plain line", ""),
    ("w", "W: line", "W: "),
    ("warning", "warning: line", "warning: "),
    ("notice", "notice: line", "notice: "),
    ("buildkit", "#7 1.2 line", "#7 1.2 "),
)
_NOTICED_SHAPES = frozenset({"w", "warning", "notice"})
_SEVERITY_PREFIXES = ("W: ", "warning: ", "notice: ")

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
    """Put a log line's own severity at its head.

    apt writes its severity first, so its `W: ` line is the `E: ` line with
    the severity replaced -- which is exactly the recovered-and-retried line
    a Debian build really prints. Buildkit's framing is not a severity and
    is prepended to whatever the line already said.
    """
    if prefix in _SEVERITY_PREFIXES and text.startswith("E: "):
        text = text[len("E: ") :]
    return prefix + text


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


_ARTIFACT_MARKER = ARTIFACT_MARKERS[2][1]
_PROJECT_MARKER = PROJECT_MARKERS[0][1]
_ENVIRONMENT_MARKER = ENVIRONMENT_MARKERS[0][1]
_SYMPTOM_MARKER = SYMPTOM_MARKERS[0][1]
_RECOVERED_FETCH = (
    "W: Failed to fetch http://deb.debian.org/debian/x.deb  Connection "
    "timed out [IP: 1.2.3.4 80] [retrying]"
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

ROWS: tuple[Row, ...] = (
    *_cross("artifact", ARTIFACT_MARKERS, ARTIFACT),
    *_cross("project", PROJECT_MARKERS, PROJECT),
    *_cross("environment", ENVIRONMENT_MARKERS, ENVIRONMENT),
    *_cross("symptom-only", SYMPTOM_MARKERS, None),
    *_cross("unknown", UNKNOWN_MARKERS, None),
    *[
        row
        for marker_id, text, ambiguous in TWINS
        for row in _log_rows(
            "twin", marker_id, text, None, ambiguous, shapes=(LOG_SHAPES[0],)
        )
        + _annotation_rows(
            "twin", marker_id, text, None, ambiguous, levels=("failure",)
        )
    ],
    *_placement_rows(
        "artifact", "copy-not-found", _ARTIFACT_MARKER, ARTIFACT, ("failure", "warning")
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
    assert len({marker_id for marker_id, _, _ in TWINS}) >= len(ARTIFACT_MARKERS)


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
    print("| row_id | source | level | context | expected |")
    print("| --- | --- | --- | --- | --- |")
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
        print(f"| {row.row_id} | {row.source} | {level} | {row.context} | {expected} |")


if __name__ == "__main__":
    print_table()
