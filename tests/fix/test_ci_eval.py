"""F §7.3–§7.4: CI evidence of an attempt, recurrence, and the evaluation.

The BuildKit "passed" shapes here are synthetic (NOT recordings); the CI
rows are enabled on the C-recordings (``tests/fix/test_ci_recordings.py``).
Every attempt is read against :data:`DOCKERFILE`, the corrected Dockerfile
holding ``FROM`` at line 1 and ``COPY`` at line 11.
"""

from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from deployer.fix import ci_eval, templates
from deployer.fix.ci_eval import (
    AttemptEvidence,
    attempt_evidence,
    build_section,
    evaluate,
    from_qualification,
)
from deployer.fix.qualify import Qualification, Qualified
from deployer.forge import Completeness, FailedJob, _build_job
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.shape import Shape
from tests.fix.conftest import enable_for_test

COPY = "COPY docs/setup.md ./setup.md"
COPY_LINES = (11, 11)
FROM = "FROM python:3.12-slim AS extra"
FROM_LINES = (1, 1)
REF = "a4efb8b6-20f9-46f4-b827-91ac0547be3a::t0jkpmm4mq76x0tyiprh1w5o7"
BUILD_STEP = "Run docker build ."
BUILD = BuildConfig("Dockerfile", (), None, "t")
DOCKERFILE = (
    FROM + "\n" + "RUN true\n" * 5 + "ENV A=1\n" * 4 + COPY + "\n" + "RUN true\n" * 2
).encode()
"""The corrected Dockerfile the attempts are read against: FROM at line 1,
COPY at line 11 (``COPY_LINES``). ``ENV`` is no BuildKit step, so the COPY
is step 7 of 9 of stage ``extra``: ``[extra 7/9]`` (ruling Z)."""

HEADER = f"##[group]{BUILD_STEP}\n##[endgroup]\n"
"""The build step's runner group header and its end: the section starts
after the ``##[endgroup]``."""
TS = "2026-09-25T14:30:21.1506466Z "
"""The runner's timestamp, in the recordings' exact form."""


def stamp(text: str, ts: str = TS) -> str:
    """``text`` with the runner timestamp ``ts`` on every ``\\n`` line, as
    the runner writes a job log (a final newline opens no line)."""
    body, end = (text[:-1], "\n") if text.endswith("\n") else (text, "")
    return "\n".join(ts + line for line in body.split("\n")) + end


_COPY_TEXT = HEADER + (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    f"#5 [extra 7/9] {COPY}\n"
    "#5 DONE 0.1s\n"
)
COPY_OK = stamp(_COPY_TEXT)
FROM_BODY = (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    "#4 [extra 1/2] FROM docker.io/library/python:3.12-slim\n"
    "#4 DONE 1.0s\n"
)
FROM_RECUR_BODY = (
    "#1 [internal] load build definition from Dockerfile\n"
    "ERROR: failed to build: failed to solve: dockerfile parse error on line "
    "{n}: FROM requires either one or three arguments\n"
)
FROM_OK = stamp(HEADER + FROM_BODY)
FROM_RECUR = stamp(HEADER + FROM_RECUR_BODY)


def _copy_recur(line: int = 11, path: str = "docs/setup.md") -> str:
    """A copy-missing/buildkit log whose ``>>>`` block is at ``line``."""
    return stamp(
        HEADER
        + (
            "#1 [internal] load build definition from Dockerfile\n"
            f"#12 [extra 7/9] {COPY}\n"
            f'#12 ERROR: failed to calculate checksum of ref {REF}: "/{path}": '
            "not found\n"
            f"Dockerfile:{line}\n"
            "--------------------\n"
            f"  {line} | >>> {COPY}\n"
            "--------------------\n"
        )
    )


def _job(
    log: str, job_id: int = 11, setup: str = "success", build: str | None = "success"
) -> FailedJob:
    """A job built from ``log`` by forge's own reading: step 2 (``setup``'s
    conclusion) precedes the build step 3 (``build``'s conclusion)."""
    record = {
        "name": "Build image",
        "conclusion": "success" if setup == "success" else "failure",
        "steps": [
            {"number": 1, "name": "Set up job", "conclusion": "success"},
            {"number": 2, "name": "setup", "conclusion": setup},
            {"number": 3, "name": BUILD_STEP, "conclusion": build},
            {"number": 4, "name": "smoke", "conclusion": "failure"},
        ],
    }
    return _build_job(record, job_id, log, [], Completeness("present", "absent"))


def _q(
    log: str = COPY_OK,
    status: Qualification = "qualified",
    reason: str | None = None,
    run_id: int = 7,
    attempt: int = 1,
    setup: str = "success",
    build: str | None = "success",
) -> Qualified:
    """An attempt qualified on the job read from ``log``, build at step 3."""
    if status != "qualified":
        return Qualified(run_id, attempt, "image", status, reason, None, None)
    job = _job(log, setup=setup, build=build)
    shape = Shape(job, "image", 3, BUILD, [])
    return Qualified(run_id, attempt, "image", status, reason, job, shape)


def _copy(log: str, q: Qualified | None = None) -> AttemptEvidence:
    """COPY evidence on the production CI COPY row, the log as qualified."""
    return attempt_evidence(
        q or _q(log),
        "missing_copy_source",
        COPY,
        COPY_LINES,
        log,
        dockerfile=DOCKERFILE,
    )


def _ev(
    qualification: Qualification = "qualified",
    positive: bool = False,
    recurred: bool = False,
    template: str | None = "not_confirmed",
    run: int = 1,
    before: bool = False,
    ambiguous: bool = False,
) -> AttemptEvidence:
    """Hand-built evidence for the evaluation table."""
    return AttemptEvidence(
        (run, 1, "image"),
        qualification,
        positive,
        recurred,
        None,
        (),
        template,  # type: ignore[arg-type]
        before,
        ambiguous,
    )


# attempt_evidence -------------------------------------------------------------


def test_key_and_positive_copy() -> None:
    """A passed COPY template is positive, keyed by run, attempt, job key."""
    got = _copy(COPY_OK)
    assert got.key == (7, 1, "image")
    assert (got.qualification, got.positive, got.recurred) == ("qualified", True, False)
    assert got.template == "passed"
    assert got.detail is None
    assert got.lines == ()
    log = COPY_OK.split("\n")
    assert [log[n - 1] for n in got.log_lines] == [
        f"{TS}#5 [extra 7/9] {COPY}",
        f"{TS}#5 DONE 0.1s",
    ]


def _ci_disabled() -> Any:
    """The production table with both CI rows disabled (no recording)."""
    rows = tuple(
        replace(row, recording=None) if row.side == "ci" else row
        for row in templates.ROWS
    )
    return patch.object(templates, "ROWS", rows)


def test_rows_disabled_is_not_enabled() -> None:
    """A CI row without a recording: no positive, ``not_enabled``."""
    with _ci_disabled():
        got = attempt_evidence(
            _q(),
            "missing_copy_source",
            COPY,
            COPY_LINES,
            COPY_OK,
            dockerfile=DOCKERFILE,
        )
    assert (got.positive, got.template, got.detail) == (
        False,
        "not_enabled",
        "templates not enabled",
    )


def test_positive_from() -> None:
    """A passed FROM template is positive."""
    with enable_for_test("from-parsed/buildkit"):
        got = attempt_evidence(
            _q(FROM_OK),
            "from_argument_count",
            FROM,
            FROM_LINES,
            FROM_OK,
            dockerfile=DOCKERFILE,
        )
    assert (got.positive, got.recurred, got.template) == (True, False, "passed")


def test_copy_recurrence_same_span() -> None:
    """A copy-missing match at the corrected span recurs."""
    got = _copy(_copy_recur())
    assert (got.positive, got.recurred) == (False, True)
    assert got.detail == "defect recurred at lines 11-11"
    # Admission, in the job text: header 2, ``#12 ERROR`` 3, block 4-7.
    assert got.lines == (2, 3, 4, 5, 6, 7)
    # Template, in the log as read: header 4, ``#12 ERROR`` 5.
    assert got.log_lines == (4, 5)


def test_copy_recurrence_other_object() -> None:
    """A "not found" for a new source still recurs: whatever its object."""
    assert _copy(_copy_recur(path="docs/new.md")).recurred


def test_copy_other_span_is_not_recurrence() -> None:
    """A match at another span is not the corrected instruction."""
    assert not _copy(_copy_recur(line=12)).recurred


AMBIGUOUS_COPY = _copy_recur() + (
    f'#9 ERROR: failed to calculate checksum of ref {REF}: "/x": not found\n'
)


def test_ambiguous_copy_blocks_but_does_not_recur() -> None:
    """Ruling AB: an ``"ambiguous"`` COPY match is not a recurrence, but
    it is flagged and blocks the claim beside a positive attempt."""
    got = _copy(AMBIGUOUS_COPY)
    assert (got.recurred, got.ambiguous_recurrence) == (False, True)
    assert got.detail == "admission match ambiguous"
    positive = _copy(COPY_OK, q=_q(COPY_OK, attempt=2))
    assert evaluate([positive, got], True) == (INSUFFICIENT, "binding ambiguous")
    assert evaluate([got, positive], True) == (INSUFFICIENT, "binding ambiguous")


def test_ambiguous_from_blocks() -> None:
    """Parse errors naming two lines are ambiguous for FROM: flagged."""
    log = FROM_RECUR.format(n=1) + FROM_RECUR.format(n=2).split("\n", 1)[1]
    got = attempt_evidence(
        _q(log), "from_argument_count", FROM, FROM_LINES, log, dockerfile=DOCKERFILE
    )
    assert (got.recurred, got.ambiguous_recurrence) == (False, True)


def test_recurrence_outranks_ambiguous() -> None:
    """A proven recurrence elsewhere ranks above an ambiguous match."""
    ambiguous = _copy(AMBIGUOUS_COPY)
    recurred = _copy(_copy_recur(), q=_q(_copy_recur(), attempt=2))
    assert evaluate([ambiguous, recurred], True) == (INSUFFICIENT, "defect recurred")


def test_from_recurrence_same_line() -> None:
    """A FROM parse error naming the corrected line recurs as ``(N, N)``."""
    log = FROM_RECUR.format(n=1)
    got = attempt_evidence(
        _q(log), "from_argument_count", FROM, FROM_LINES, log, dockerfile=DOCKERFILE
    )
    assert got.recurred
    other = FROM_RECUR.format(n=2)
    got = attempt_evidence(
        _q(other), "from_argument_count", FROM, FROM_LINES, other, dockerfile=DOCKERFILE
    )
    assert not got.recurred


def test_recurrence_ignores_other_class() -> None:
    """Only the admitted class's matcher counts."""
    log = FROM_RECUR.format(n=11)
    got = attempt_evidence(
        _q(log), "missing_copy_source", COPY, COPY_LINES, log, dockerfile=DOCKERFILE
    )
    assert not got.recurred


@pytest.mark.parametrize("status", ["excluded", "undetermined"])
def test_non_qualified_is_carried(status: Qualification) -> None:
    """A non-qualified attempt keeps its qualification and reason."""
    q = _q(status=status, reason="why")
    got = attempt_evidence(
        q, "missing_copy_source", COPY, COPY_LINES, COPY_OK, dockerfile=DOCKERFILE
    )
    assert got == from_qualification(q)
    assert got == AttemptEvidence((7, 1, "image"), status, False, False, "why", ())


def test_blank_log_is_undetermined() -> None:
    """A blank log is undetermined, not raised."""
    got = _copy(" \n", q=_q(COPY_OK))
    assert (got.qualification, got.positive) == ("undetermined", False)
    assert got.detail == "log of job 11 is empty"


def test_other_log_is_undetermined() -> None:
    """A log that is not the one qualified is undetermined."""
    got = _copy(COPY_OK + "extra\n", q=_q(COPY_OK))
    assert got.qualification == "undetermined"
    assert got.detail == "log of job 11 is not the one it was qualified on"


def test_job_without_steps_is_undetermined() -> None:
    """A qualified job with no step listing cannot be re-read."""
    q = _q()
    assert q.job is not None
    q = replace(q, job=replace(q.job, all_steps=None))
    assert _copy(COPY_OK, q=q).qualification == "undetermined"


def test_failure_is_undetermined() -> None:
    """An unknown class is a reason, never an exception."""
    bad: Any = "nope"
    got = attempt_evidence(_q(), bad, COPY, COPY_LINES, COPY_OK, dockerfile=DOCKERFILE)
    assert got.qualification == "undetermined"
    assert (got.detail or "").startswith("evidence failed: KeyError")


def test_timestamped_log_reads_as_qualified() -> None:
    """The raw log with runner timestamps rebuilds to the qualified text."""
    raw = stamp(_COPY_TEXT, "2026-09-25T10:00:00.1000000Z ")
    assert _copy(raw, q=_q(raw)).positive


def test_section_timestamp_must_be_the_runner_form() -> None:
    """Ruling Y: a section line whose timestamp is not the recordings' form
    (seven fraction digits) is refused."""
    raw = stamp(_COPY_TEXT, "2026-09-25T10:00:00.1Z ")
    got = _copy(raw, q=_q(raw))
    assert (got.positive, got.template) == (False, "binding_ambiguous")
    assert got.detail == "log line 3: section line without a runner timestamp"


# evaluate ---------------------------------------------------------------------

INSUFFICIENT = "ci_confirmation_insufficient"

TABLE: list[tuple[str, list[AttemptEvidence], bool, str, str | None]] = [
    ("confirmed", [_ev(positive=True, template="passed")], True, "ci_confirmed", None),
    (
        "later-fail",
        [_ev(positive=True, template="passed"), _ev(template="not_confirmed", run=2)],
        True,
        "ci_confirmed",
        None,
    ),
    (
        "excluded-kept",
        [_ev(positive=True, template="passed"), _ev("excluded", template=None, run=2)],
        True,
        "ci_confirmed",
        None,
    ),
    (
        "incomplete",
        [_ev(positive=True, template="passed")],
        False,
        INSUFFICIENT,
        "qualification undetermined",
    ),
    (
        "pos+undet",
        [_ev(positive=True, template="passed"), _ev("undetermined", template=None)],
        True,
        INSUFFICIENT,
        "qualification undetermined",
    ),
    (
        "contradict",
        [_ev(positive=True, template="passed"), _ev(recurred=True, run=2)],
        True,
        INSUFFICIENT,
        "contradictory runs",
    ),
    (
        "contradict-1",
        [_ev(positive=True, recurred=True, template="passed")],
        True,
        INSUFFICIENT,
        "contradictory runs",
    ),
    ("recurred", [_ev(recurred=True)], True, INSUFFICIENT, "defect recurred"),
    ("none", [], True, INSUFFICIENT, "no qualifying run"),
    (
        "excluded-only",
        [_ev("excluded", template=None)],
        True,
        INSUFFICIENT,
        "no qualifying run",
    ),
    (
        "not-enabled",
        [_ev(template="not_enabled"), _ev(template="not_enabled", run=2)],
        True,
        INSUFFICIENT,
        "templates not enabled",
    ),
    (
        "not-reached",
        [_ev(template="not_confirmed"), _ev(template="not_enabled", run=2)],
        True,
        INSUFFICIENT,
        "build step not reached",
    ),
    (
        "unknown",
        [_ev(template="unknown_format"), _ev(template="not_enabled", run=2)],
        True,
        INSUFFICIENT,
        "unknown format",
    ),
    (
        "ambiguous",
        [_ev(template="binding_ambiguous"), _ev(template="not_confirmed", run=2)],
        True,
        INSUFFICIENT,
        "binding ambiguous",
    ),
    (
        "failed-before",
        [_ev(template="not_confirmed", before=True), _ev(template="unknown_format")],
        True,
        INSUFFICIENT,
        "CI failed before the build",
    ),
    (
        "ambig>before",
        [_ev(template="binding_ambiguous"), _ev(template="not_enabled", before=True)],
        True,
        INSUFFICIENT,
        "binding ambiguous",
    ),
    (
        "pos+ambig",
        [_ev(positive=True, template="passed"), _ev(ambiguous=True, run=2)],
        True,
        INSUFFICIENT,
        "binding ambiguous",
    ),
    (
        "recur+ambig",
        [_ev(recurred=True), _ev(ambiguous=True, run=2)],
        True,
        INSUFFICIENT,
        "defect recurred",
    ),
    (
        "contra+ambig",
        [
            _ev(positive=True, template="passed"),
            _ev(recurred=True, run=2),
            _ev(ambiguous=True, run=3),
        ],
        True,
        INSUFFICIENT,
        "contradictory runs",
    ),
    (
        "excl-recur",
        [_ev("excluded", recurred=True, template=None)],
        True,
        INSUFFICIENT,
        "no qualifying run",
    ),
]


@pytest.mark.parametrize(
    ("evidence", "complete", "outcome", "reason"),
    [row[1:] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_evaluate_table(
    evidence: list[AttemptEvidence], complete: bool, outcome: str, reason: str | None
) -> None:
    """F §7.4: each condition yields its outcome and exact reason."""
    assert evaluate(evidence, complete) == (outcome, reason)


@pytest.mark.parametrize(
    ("evidence", "complete"),
    [row[1:3] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_evaluate_order_independent(
    evidence: list[AttemptEvidence], complete: bool
) -> None:
    """Reversing the evidence gives the same result."""
    assert evaluate(evidence[::-1], complete) == evaluate(evidence, complete)


def test_evaluate_never_raises() -> None:
    """Malformed input is ``qualification undetermined``, not an exception."""
    assert evaluate([None], True) == (  # type: ignore[list-item]
        INSUFFICIENT,
        "qualification undetermined",
    )


def test_pipeline_positive_and_undetermined() -> None:
    """End to end: a positive attempt beside an unread one is insufficient."""
    positive = _copy(COPY_OK)
    unread = attempt_evidence(
        _q(status="undetermined", reason="log of job 12 is unavailable", attempt=2),
        "missing_copy_source",
        COPY,
        COPY_LINES,
        "",
        dockerfile=DOCKERFILE,
    )
    assert evaluate([positive, unread], True) == (
        INSUFFICIENT,
        "qualification undetermined",
    )
    assert evaluate([positive], True) == ("ci_confirmed", None)


# Fix round 1 regressions ------------------------------------------------------


def test_multiline_from_recurs_at_first_line() -> None:
    """A §4.2: a FROM spanning lines 3-4 recurs at its first line only."""
    at3 = FROM_RECUR.format(n=3)
    got = attempt_evidence(
        _q(at3), "from_argument_count", FROM, (3, 4), at3, dockerfile=DOCKERFILE
    )
    assert got.recurred
    at4 = FROM_RECUR.format(n=4)
    got = attempt_evidence(
        _q(at4), "from_argument_count", FROM, (3, 4), at4, dockerfile=DOCKERFILE
    )
    assert not got.recurred


def test_positive_and_multiline_from_recurrence_contradict() -> None:
    """A positive attempt and a multi-line FROM recurrence contradict."""
    with enable_for_test("from-parsed/buildkit"):
        a = attempt_evidence(
            _q(FROM_OK),
            "from_argument_count",
            FROM,
            (3, 4),
            FROM_OK,
            dockerfile=DOCKERFILE,
        )
    b_log = FROM_RECUR.format(n=3)
    b = attempt_evidence(
        _q(b_log, attempt=2),
        "from_argument_count",
        FROM,
        (3, 4),
        b_log,
        dockerfile=DOCKERFILE,
    )
    assert (a.positive, b.recurred) == (True, True)
    assert evaluate([a, b], True) == (INSUFFICIENT, "contradictory runs")
    assert evaluate([b, a], True) == (INSUFFICIENT, "contradictory runs")


def test_copy_other_text_is_not_recurrence() -> None:
    """A §4.2: the block text must be the corrected instruction."""
    got = attempt_evidence(
        _q(_copy_recur()),
        "missing_copy_source",
        "COPY docs/other.md ./setup.md",
        COPY_LINES,
        _copy_recur(),
        dockerfile=DOCKERFILE,
    )
    assert not got.recurred


def test_failed_before_build_reason() -> None:
    """A failed step before the build step, template not passed."""
    q = _q(COPY_OK + stamp("#5 CACHED\n"), setup="failure")
    got = _copy(COPY_OK + stamp("#5 CACHED\n"), q=q)
    assert (got.template, got.failed_before_build) == ("not_confirmed", True)
    assert evaluate([got], True) == (INSUFFICIENT, "CI failed before the build")


def test_failed_after_build_is_not_before() -> None:
    """The failed smoke step after the build step does not count."""
    got = _copy(COPY_OK + stamp("#5 CACHED\n"))
    assert not got.failed_before_build
    assert evaluate([got], True) == (INSUFFICIENT, "build step not reached")


def test_failed_before_but_passed_is_positive() -> None:
    """A passed template is positive whatever failed before the build."""
    got = _copy(COPY_OK, q=_q(COPY_OK, setup="failure"))
    assert (got.positive, got.failed_before_build) == (True, False)


# Fix round 2 regressions ------------------------------------------------------


def test_from_qualification_of_qualified_is_undetermined() -> None:
    """I-1: a qualified attempt not read for evidence is undetermined and
    blocks a positive attempt elsewhere."""
    unread = from_qualification(_q(_copy_recur(), attempt=2))
    assert unread.qualification == "undetermined"
    assert unread.detail == "qualified attempt not evaluated"
    positive = _copy(COPY_OK)
    assert evaluate([positive, unread], True) == (
        INSUFFICIENT,
        "qualification undetermined",
    )


def test_malformed_attempt_never_raises() -> None:
    """M-1: even reading ``q.status`` is inside the totality guard."""
    bad: Any = None
    got = attempt_evidence(
        bad, "missing_copy_source", COPY, COPY_LINES, COPY_OK, dockerfile=DOCKERFILE
    )
    assert (got.key, got.qualification) == ((0, 0, ""), "undetermined")


def test_lines_as_list_still_recurs() -> None:
    """M-2: spans compare as tuples whatever sequence the caller passes."""
    lines: Any = [11, 11]
    log = _copy_recur()
    got = attempt_evidence(
        _q(log), "missing_copy_source", COPY, lines, log, dockerfile=DOCKERFILE
    )
    assert got.recurred


def test_multiline_copy_recurs() -> None:
    """M-4: a COPY spanning lines 11-12 recurs at its whole span."""
    log = stamp(
        HEADER + "#1 [internal] load build definition from Dockerfile\n"
        f"#12 [extra 7/9] {COPY}\n"
        f'#12 ERROR: failed to calculate checksum of ref {REF}: "/docs/setup.md": '
        "not found\n"
        "Dockerfile:11\n"
        "--------------------\n"
        "  11 | >>> COPY docs/setup.md \\\n"
        "  12 | >>>     ./setup.md\n"
        "--------------------\n"
    )
    corrected = "COPY docs/setup.md \\\n    ./setup.md"
    got = attempt_evidence(
        _q(log), "missing_copy_source", corrected, (11, 12), log, dockerfile=DOCKERFILE
    )
    assert got.recurred
    got = attempt_evidence(
        _q(log), "missing_copy_source", corrected, (11, 11), log, dockerfile=DOCKERFILE
    )
    assert not got.recurred


def test_copy_recurrence_with_rows_disabled() -> None:
    """M-4: CI rows disabled — recurrence uses the admission matchers, so
    the result is still ``defect recurred``."""
    log = _copy_recur()
    with _ci_disabled():
        got = attempt_evidence(
            _q(log), "missing_copy_source", COPY, COPY_LINES, log, dockerfile=DOCKERFILE
        )
    assert (got.template, got.recurred) == ("not_enabled", True)
    assert evaluate([got], True) == (INSUFFICIENT, "defect recurred")


# Ruling T: the bound build step's own section, split on \n only ---------------

_FORGED_COPY = "COPY a.txt ./a.txt"
_FORGED_DF = f"FROM python:3.12-slim\nRUN make\n{_FORGED_COPY}\n".encode()
_FORGED = f"#0 [stage-0 3/3] {_FORGED_COPY}"
_BUILDING = '#0 building with "default" instance using docker driver\n'
"""BuildKit's first line (every C-recording): ``#0`` is then already seen,
so a forged ``#0`` header dodges the numbering-restart check."""


def _forged(log: str, build: str | None = "failure") -> AttemptEvidence:
    """The corrected COPY of :data:`_FORGED_DF` judged on ``log``; the build
    step concluded ``build`` (``failure``: the forgery logs all fail it)."""
    q = _q(log, build=build)
    return attempt_evidence(
        q, "missing_copy_source", _FORGED_COPY, (3, 3), log, dockerfile=_FORGED_DF
    )


def test_c1_forged_header_in_another_step_refused() -> None:
    """Review C1, vector 2: a non-build step prints the corrected header and
    ``#0 DONE``; the build then fails at ``RUN make``, never reaching the
    COPY. Only the build step's section is read: not confirmed."""
    log = stamp(
        "##[group]Run make lint\n##[endgroup]\n"
        f"{_FORGED}\n#0 DONE 0.0s\n"
        + HEADER
        + _BUILDING
        + "#1 [internal] load build definition from Dockerfile\n"
        "#6 [stage-0 2/3] RUN make\n"
        '#6 ERROR: process "/bin/sh -c make" did not complete successfully\n'
    )
    got = _forged(log)
    assert (got.positive, got.template) == (False, "not_confirmed")
    assert got.detail == "corrected step header absent"
    assert evaluate([got], True) == (INSUFFICIENT, "build step not reached")


@pytest.mark.parametrize(
    "sep",
    ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=["cr", "vt", "ff", "fs", "gs", "rs", "nel", "ls", "ps"],
)
def test_c1_forged_header_in_run_output_refused(sep: str) -> None:
    """Review C1, vector 1: RUN output smuggles the header and ``#0 DONE``
    behind a line break other than ``\\n``. It is refused, naming the
    character, never re-split."""
    log = stamp(
        HEADER + _BUILDING + "#1 [internal] load build definition from Dockerfile\n"
        "#6 [stage-0 2/3] RUN make\n"
        f"#6 0.100 x{sep}{_FORGED}{sep}#0 DONE 0.0s\n"
        '#6 ERROR: process "/bin/sh -c make" did not complete successfully\n'
    )
    got = _forged(log)
    assert (got.positive, got.template) == (False, "binding_ambiguous")
    assert got.detail == (
        f"log line 6: line break U+{ord(sep):04X} inside the build step's section"
    )
    assert evaluate([got], True) == (INSUFFICIENT, "binding ambiguous")


def test_crlf_lines_are_accepted() -> None:
    """A CRLF ending is one ``\\n`` line break: the section still reads."""
    got = _copy(COPY_OK.replace("\n", "\r\n"), q=_q(COPY_OK.replace("\n", "\r\n")))
    assert got.positive


@pytest.mark.parametrize("headers", [0, 2], ids=["none", "two"])
def test_build_header_not_once_is_ambiguous(headers: int) -> None:
    """No runner group header for the build step, or several."""
    body = COPY_OK.removeprefix(stamp(HEADER))
    log = stamp(HEADER) * headers + body
    got = _copy(log, q=_q(log))
    assert (got.positive, got.template) == (False, "binding_ambiguous")
    assert f"{headers} runner group headers" in (got.detail or "")


def test_section_ends_at_next_run_group() -> None:
    """Evidence after the next ``##[group]Run `` header is another step's."""
    head, done = _COPY_TEXT.rsplit("#5 DONE", 1)
    log = stamp(head + "##[group]Run echo later\n##[endgroup]\n#5 DONE" + done)
    got = _copy(log, q=_q(log))
    assert (got.positive, got.template) == (False, "binding_ambiguous")


def test_build_section_is_total() -> None:
    """``build_section`` returns a reason, never raises."""
    assert isinstance(build_section("", "Run x"), str)
    assert isinstance(build_section("##[group]Run x\n#1 a\n", "Run x"), str)
    log = stamp("##[group]Run x\n##[endgroup]\n#1 a\n")
    assert build_section(log, "Run x") == (2, "#1 a")


@pytest.mark.parametrize(
    "boundary",
    [
        "Post job cleanup.",
        "Post Run actions/checkout@v4",
        "##[group]Some group",
    ],
    ids=["post-job", "post-step", "group"],
)
def test_forged_header_after_the_build_step_refused(boundary: str) -> None:
    """Round 2: a forged header and ``#0 DONE`` printed after the build step
    (the post phase, a post step, any runner group) are not the build's."""
    log = stamp(
        HEADER + _BUILDING + "#1 [internal] load build definition from Dockerfile\n"
        "#6 [stage-0 2/3] RUN make\n"
        '#6 ERROR: process "/bin/sh -c make" did not complete successfully\n'
        "##[error]Process completed with exit code 1.\n"
        f"{boundary}\n{_FORGED}\n#0 DONE 0.0s\n"
    )
    job = _job(log)
    assert job.all_steps is not None
    post = {"number": 5, "name": "Post Run actions/checkout@v4", "conclusion": None}
    steps = [
        {"number": s.number, "name": s.name, "conclusion": s.conclusion}
        for s in job.all_steps
    ] + [post]
    record = {"name": job.name, "conclusion": job.conclusion, "steps": steps}
    job = _build_job(record, job.job_id, log, [], Completeness("present", "absent"))
    q = replace(_q(log), job=job)
    got = attempt_evidence(
        q, "missing_copy_source", _FORGED_COPY, (3, 3), log, dockerfile=_FORGED_DF
    )
    assert (got.positive, got.template) == (False, "not_confirmed")
    assert got.detail == "corrected step header absent"


# Round 3, ruling X (N1): a failure must provably follow the corrected COPY ---

_NOT_AFTER = "failure not provably after the corrected step"


def _n1(run: str = "#6 [stage-0 2/3] RUN make", extra_error: str = "") -> str:
    """The reviewer's N1 log: ``RUN make`` prints ``x\\r<header>\\n\\r#0
    DONE``, which the runner stores as clean timestamped lines, then fails;
    the real COPY never runs. ``run`` is the failing vertex's header;
    ``extra_error`` adds a second failing vertex."""
    filler = "\n".join(f"#6 0.{200 + i} filler {i}" for i in range(5))
    return stamp(
        HEADER + _BUILDING + "\n#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE 0.0s\n\n"
        f"{run}\n#6 0.100 x\n{_FORGED}\n"
        f"#6 0.101 \n#0 DONE 0.0s\n{filler}\n{extra_error}"
        '#6 ERROR: process "/bin/sh -c make" did not complete successfully: '
        "exit code: 2\n"
        'ERROR: failed to build: failed to solve: process "/bin/sh -c make"\n'
        "##[error]Process completed with exit code 1.\nPost job cleanup.\n"
    )


def test_n1_runner_split_forgery_refused() -> None:
    """Review N1: the failing ``#6`` is ``[stage-0 2/3]``, before the COPY's
    ``3/3``, so the forged pair cannot pass."""
    got = _forged(_n1())
    assert (got.positive, got.template, got.detail) == (
        False,
        "binding_ambiguous",
        _NOT_AFTER,
    )
    assert evaluate([got], True) == (INSUFFICIENT, "binding ambiguous")


def test_masked_real_error_refused() -> None:
    """Round-5 re-review: ``::add-mask::#6 ERROR`` makes the runner log the
    real failure as ``***``; with a forged later vertex the log would show
    one failing vertex after the COPY. Any ``***`` refuses (ruling AC)."""
    log = _n1("#6 [stage-0 2/3] RUN make", "#9 [stage-0 4/4] RUN y\n#9 ERROR: x\n")
    log = log.replace("#6 ERROR:", "***:")
    got = _forged(log)
    assert (got.positive, got.template, got.detail) == (
        False,
        "binding_ambiguous",
        "runner-masked text in the log",
    )


@pytest.mark.parametrize(
    ("run", "extra"),
    [
        ("#6 [other 4/5] RUN make", ""),
        ("#6 [stage-0 3/3] RUN make", ""),
        ("#6 RUN make", ""),
        (
            "#6 [stage-0 4/4] RUN make",
            '#7 ERROR: process "x" did not complete successfully\n',
        ),
        ("#6 [stage-0 4/4] RUN make", "#9 CANCELED\n"),
    ],
    ids=["other-stage", "same-step", "unmapped", "two-errors", "canceled"],
)
def test_n1_failure_not_after_refused(run: str, extra: str) -> None:
    """The failing vertex in another stage, at the COPY's own step, with no
    stage header, or a second failing vertex: not provably after the COPY."""
    got = _forged(_n1(run, extra))
    assert (got.positive, got.template, got.detail) == (
        False,
        "binding_ambiguous",
        _NOT_AFTER,
    )


@pytest.mark.parametrize(
    ("failing", "passed"),
    [
        ("#9 [stage-0 4/4] RUN false", True),
        ("#9 [other 4/4] RUN false", False),
        ("#9 [stage-0 3/4] RUN false", False),
        ("#9 [stage-0 2/4] RUN false", False),
    ],
    ids=["after", "other-stage", "same-step", "before"],
)
def test_failure_after_the_copy_only(failing: str, passed: bool) -> None:
    """Only a failing vertex of the COPY's stage at a later step leaves the
    COPY passed (``c4``'s shape); a successful build is unchanged."""
    df = f"FROM python:3.12-slim\nRUN make\n{_FORGED_COPY}\nRUN false\n".encode()
    log = stamp(
        HEADER + _BUILDING + f"#8 [stage-0 3/4] {_FORGED_COPY}\n#8 DONE 0.0s\n"
        f"{failing}\n#9 ERROR: process did not complete successfully\n"
    )
    got = attempt_evidence(
        _q(log, build="failure"),
        "missing_copy_source",
        _FORGED_COPY,
        (3, 3),
        log,
        dockerfile=df,
    )
    assert got.positive is passed
    if not passed:
        assert got.detail == _NOT_AFTER


# Round 3, ruling Y (N2): the section starts after the endgroup, timestamped --


@pytest.mark.parametrize("stamped", [False, True], ids=["bare", "stamped"])
def test_n2_env_continuation_refused(stamped: bool) -> None:
    """Review N2: an ``env:`` value with newlines echoes continuation lines
    inside the runner's group, before ``##[endgroup]``; with or without a
    timestamp they are not the build's section."""
    forged = f"{_FORGED}\n#0 DONE 0.0s\n"
    echo = stamp(
        f"##[group]{BUILD_STEP}\n\x1b[36;1mdocker build .\x1b[0m\n"
        "shell: /usr/bin/bash -e {0}\nenv:\n  X: a\n"
    )
    rest = stamp(
        "##[endgroup]\n" + _BUILDING + "#6 [stage-0 2/3] RUN make\n"
        '#6 ERROR: process "/bin/sh -c make" did not complete successfully\n'
        "##[error]Process completed with exit code 1.\nPost job cleanup.\n"
    )
    log = echo + (stamp(forged) if stamped else forged) + rest
    got = _forged(log)
    assert (got.positive, got.template) == (False, "not_confirmed")


def test_n2_forged_endgroup_in_env_refused() -> None:
    """An ``env:`` value that ends the group early leaves the runner's real
    ``##[endgroup]`` inside the section, or an untimestamped line: refused."""
    log = stamp(
        f"##[group]{BUILD_STEP}\nenv:\n  X: a\n##[endgroup]\n{_FORGED}\n"
        "#0 DONE 0.0s\n##[endgroup]\n" + _BUILDING
    )
    got = _forged(log)
    assert (got.positive, got.template) == (False, "binding_ambiguous")
    assert got.detail == (
        "log line 7: a second ##[endgroup] inside the build step's section"
    )
    bare = log.replace(TS + _FORGED, _FORGED)
    got = _forged(bare)
    assert got.detail == "log line 5: section line without a runner timestamp"


# Round 4, rulings Z (R1) and AA (R2) ------------------------------------------


def _r(forged: str, end: str = "") -> str:
    """The round-4 logs: ``RUN make`` (step 2/3) prints, split by the runner,
    ``forged`` and ``#0 DONE``, then ``end`` (a section-end marker) on its own
    line, then fails; the real COPY at 3/3 never runs."""
    return stamp(
        HEADER + _BUILDING + "\n#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE 0.0s\n\n"
        "#5 [stage-0 1/3] FROM docker.io/library/python:3.12-slim\n#5 DONE 1.0s\n\n"
        f"#6 [stage-0 2/3] RUN make\n#6 0.100 x\n{forged}\n#0 DONE 0.0s\n"
        + (f"{end}\n" if end else "")
        + "#6 0.101 \n"
        '#6 ERROR: process "/bin/sh -c make" did not complete successfully: '
        "exit code: 2\n"
        "------\n > [stage-0 2/3] RUN make:\n------\n"
        "##[error]Process completed with exit code 1.\nPost job cleanup.\n"
    )


@pytest.mark.parametrize(
    ("step", "detail"),
    [
        ("1/3", "step header [stage-0 1/3] is not the Dockerfile's [stage-0 3/3]"),
        ("2/3", "step header [stage-0 2/3] is not the Dockerfile's [stage-0 3/3]"),
        ("0/3", "corrected step header absent"),
    ],
    ids=["1of3", "2of3", "0of3"],
)
def test_r1_forged_step_number_refused(step: str, detail: str) -> None:
    """Review R1: the forged header cannot pick its own step number; the
    COPY's position is derived from the Dockerfile (``3/3``), and step 0 is
    no stage header."""
    got = _forged(_r(f"#0 [stage-0 {step}] {_FORGED_COPY}"))
    assert (got.positive, got.detail) == (False, detail)
    assert evaluate([got], True)[0] == INSUFFICIENT


@pytest.mark.parametrize(
    "end", ["Post job cleanup.", "##[group]x"], ids=["post-job", "group"]
)
def test_r2_forged_section_end_refused(end: str) -> None:
    """Review R2: a forged pair at the true step followed by a forged
    section end; the real ``#6 ERROR`` after it is still found."""
    got = _forged(_r(_FORGED, end))
    assert (got.positive, got.template, got.detail) == (
        False,
        "binding_ambiguous",
        _NOT_AFTER,
    )
    assert evaluate([got], True) == (INSUFFICIENT, "binding ambiguous")


def test_r2_failure_after_the_section_counts() -> None:
    """Ruling AA: a failing vertex anywhere after the section start counts,
    even after the post phase; a success with nothing after is unchanged."""
    ok = stamp(HEADER + _BUILDING + f"#7 [stage-0 3/3] {_FORGED_COPY}\n#7 DONE 0.0s\n")
    assert _forged(ok, build="success").positive
    got = _forged(ok + stamp("Post job cleanup.\n#6 ERROR: x\n"))
    assert (got.positive, got.detail) == (False, _NOT_AFTER)


# Round 5, ruling AB: COPY evidence bound to the build step's API conclusion --


def _hang(vid: str, tail: str) -> str:
    """Review t25r4: ``RUN make`` (2/3) prints, split by the runner, a forged
    header at the derived ``3/3`` and its ``DONE``, then hangs; the job is
    cancelled or times out and the killed client prints no ``#k ERROR``."""
    return stamp(
        HEADER + _BUILDING + "\n#1 [internal] load build definition from Dockerfile\n"
        "#1 DONE 0.0s\n\n"
        "#5 [stage-0 1/3] FROM docker.io/library/python:3.12-slim\n#5 DONE 1.0s\n\n"
        f"#6 [stage-0 2/3] RUN make\n#6 0.100 x\n{vid} [stage-0 3/3] {_FORGED_COPY}\n"
        f"#6 0.101 \n{vid} DONE 0.0s\n{tail}\nPost job cleanup.\n"
    )


_CANCELED = "##[error]The operation was canceled."
_TIMED_OUT = "##[error]The action has timed out."


@pytest.mark.parametrize(
    ("vid", "tail", "build"),
    [
        ("#7", _CANCELED, "cancelled"),
        ("#0", _CANCELED, "cancelled"),
        ("#7", _TIMED_OUT, "timed_out"),
        ("#0", _TIMED_OUT, "timed_out"),
        ("#7", _TIMED_OUT, "failure"),
    ],
    ids=["killed-7", "killed-0", "timeout-7", "timeout-0", "timeout-failure"],
)
def test_ab_killed_build_refused(vid: str, tail: str, build: str) -> None:
    """A killed or timed-out build prints no failing vertex; its step's API
    conclusion is not ``success``, so the forged pair cannot pass."""
    got = _forged(_hang(vid, tail), build=build)
    assert (got.positive, got.template) == (False, "binding_ambiguous")
    assert evaluate([got], True) == (INSUFFICIENT, "binding ambiguous")


@pytest.mark.parametrize(
    ("build", "detail"),
    [
        ("skipped", "build step conclusion is 'skipped'"),
        (None, "build step conclusion is None"),
        ("neutral", "build step conclusion is 'neutral'"),
        ("failure", "build step concluded failure without a failing vertex"),
    ],
    ids=["skipped", "missing", "unknown", "failure-no-vertex"],
)
def test_ab_copy_needs_a_consistent_conclusion(build: str | None, detail: str) -> None:
    """A clean COPY pass refuses unless the build step concluded
    ``success``."""
    got = _copy(COPY_OK, q=_q(COPY_OK, build=build))
    assert (got.positive, got.template, got.detail) == (
        False,
        "binding_ambiguous",
        detail,
    )


def test_ab_success_with_a_failing_vertex_refused() -> None:
    """A step concluded ``success`` with a ``#k ERROR`` is inconsistent,
    even when the error follows the COPY."""
    df = f"FROM python:3.12-slim\nRUN make\n{_FORGED_COPY}\nRUN false\n".encode()
    log = stamp(
        HEADER + _BUILDING + f"#8 [stage-0 3/4] {_FORGED_COPY}\n#8 DONE 0.0s\n"
        "#9 [stage-0 4/4] RUN false\n#9 ERROR: process did not complete\n"
    )
    got = attempt_evidence(
        _q(log), "missing_copy_source", _FORGED_COPY, (3, 3), log, dockerfile=df
    )
    assert (got.positive, got.template, got.detail) == (
        False,
        "binding_ambiguous",
        "failing vertex in a build step concluded success",
    )


def test_ab_build_step_not_unique_refused() -> None:
    """Two listed steps with the build step's number: no conclusion binds."""
    job = _job(COPY_OK)
    assert job.all_steps is not None
    twin = replace(job.all_steps[2], conclusion="success")
    q = replace(_q(COPY_OK), job=replace(job, all_steps=[*job.all_steps, twin]))
    assert ci_eval._build_step(q) is None
    got = _copy(COPY_OK, q=q)
    assert not got.positive
    assert evaluate([got], True)[0] == INSUFFICIENT
