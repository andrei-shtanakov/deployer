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

from deployer.fix import templates
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
DOCKERFILE = (FROM + "\n" + "RUN true\n" * 9 + COPY + "\n").encode()
"""The corrected Dockerfile the attempts are read against: FROM at line 1,
COPY at line 11 (``COPY_LINES``)."""

HEADER = f"##[group]{BUILD_STEP}\n##[endgroup]\n"
"""The build step's runner group header: its section starts after it."""

COPY_OK = HEADER + (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    f"#5 [stage-0 7/9] {COPY}\n"
    "#5 DONE 0.1s\n"
)
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
FROM_OK = HEADER + FROM_BODY
FROM_RECUR = HEADER + FROM_RECUR_BODY


def _copy_recur(line: int = 11, path: str = "docs/setup.md") -> str:
    """A copy-missing/buildkit log whose ``>>>`` block is at ``line``."""
    return HEADER + (
        "#1 [internal] load build definition from Dockerfile\n"
        f"#12 [stage-0 7/9] {COPY}\n"
        f'#12 ERROR: failed to calculate checksum of ref {REF}: "/{path}": '
        "not found\n"
        f"Dockerfile:{line}\n"
        "--------------------\n"
        f"  {line} | >>> {COPY}\n"
        "--------------------\n"
    )


def _job(log: str, job_id: int = 11, setup: str = "success") -> FailedJob:
    """A job built from ``log`` by forge's own reading: step 2 (``setup``'s
    conclusion) precedes the build step 3."""
    record = {
        "name": "Build image",
        "conclusion": "success" if setup == "success" else "failure",
        "steps": [
            {"number": 1, "name": "Set up job", "conclusion": "success"},
            {"number": 2, "name": "setup", "conclusion": setup},
            {"number": 3, "name": BUILD_STEP, "conclusion": "success"},
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
) -> Qualified:
    """An attempt qualified on the job read from ``log``, build at step 3."""
    if status != "qualified":
        return Qualified(run_id, attempt, "image", status, reason, None, None)
    job = _job(log, setup=setup)
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
        f"#5 [stage-0 7/9] {COPY}",
        "#5 DONE 0.1s",
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
    raw = "".join(f"2026-09-25T10:00:00.1Z {line}\n" for line in COPY_OK.splitlines())
    assert _copy(raw, q=_q(raw)).positive


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
    q = _q(COPY_OK + "#5 CACHED\n", setup="failure")
    got = _copy(COPY_OK + "#5 CACHED\n", q=q)
    assert (got.template, got.failed_before_build) == ("not_confirmed", True)
    assert evaluate([got], True) == (INSUFFICIENT, "CI failed before the build")


def test_failed_after_build_is_not_before() -> None:
    """The failed smoke step after the build step does not count."""
    got = _copy(COPY_OK + "#5 CACHED\n")
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
    log = HEADER + (
        "#1 [internal] load build definition from Dockerfile\n"
        f"#12 [stage-0 7/9] {COPY}\n"
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


def _forged(log: str) -> AttemptEvidence:
    """The corrected COPY of :data:`_FORGED_DF` judged on ``log``."""
    return attempt_evidence(
        _q(log), "missing_copy_source", _FORGED_COPY, (3, 3), log, dockerfile=_FORGED_DF
    )


def test_c1_forged_header_in_another_step_refused() -> None:
    """Review C1, vector 2: a non-build step prints the corrected header and
    ``#0 DONE``; the build then fails at ``RUN make``, never reaching the
    COPY. Only the build step's section is read: not confirmed."""
    log = (
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
    ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "],
    ids=["cr", "vt", "ff", "fs", "gs", "rs", "nel", "ls", "ps"],
)
def test_c1_forged_header_in_run_output_refused(sep: str) -> None:
    """Review C1, vector 1: RUN output smuggles the header and ``#0 DONE``
    behind a line break other than ``\\n``. It is refused, naming the
    character, never re-split."""
    log = HEADER + (
        _BUILDING + "#1 [internal] load build definition from Dockerfile\n"
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
    body = COPY_OK.removeprefix(HEADER)
    log = HEADER * headers + body
    got = _copy(log, q=_q(log))
    assert (got.positive, got.template) == (False, "binding_ambiguous")
    assert f"{headers} runner group headers" in (got.detail or "")


def test_section_ends_at_next_run_group() -> None:
    """Evidence after the next ``##[group]Run `` header is another step's."""
    body = COPY_OK.removeprefix(HEADER)
    head, done = body.rsplit("#5 DONE", 1)
    log = HEADER + head + "##[group]Run echo later\n##[endgroup]\n#5 DONE" + done
    got = _copy(log, q=_q(log))
    assert (got.positive, got.template) == (False, "binding_ambiguous")


def test_build_section_is_total() -> None:
    """``build_section`` returns a reason, never raises."""
    assert isinstance(build_section("", "Run x"), str)
    assert build_section("##[group]Run x\n#1 a\n", "Run x") == (1, "#1 a\n")
