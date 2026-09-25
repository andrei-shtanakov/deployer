"""F §7.3–§7.4: CI evidence of an attempt, recurrence, and the evaluation.

The BuildKit "passed" shapes here are synthetic (NOT recordings), reached
through the test seam exactly as ``tests/fix/test_templates.py`` does.
"""

from dataclasses import replace
from typing import Any

import pytest

from deployer.fix.ci_eval import (
    AttemptEvidence,
    attempt_evidence,
    evaluate,
    from_qualification,
)
from deployer.fix.qualify import Qualification, Qualified
from deployer.forge import Completeness, FailedJob, _build_job
from deployer.reproduce.shape import job_text
from tests.fix.conftest import enable_for_test

COPY = "COPY docs/setup.md ./setup.md"
COPY_LINES = (11, 11)
FROM = "FROM python:3.12-slim AS extra"
FROM_LINES = (1, 1)
REF = "a4efb8b6-20f9-46f4-b827-91ac0547be3a::t0jkpmm4mq76x0tyiprh1w5o7"
BUILD_STEP = "Run docker build ."

COPY_OK = (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    f"#5 [stage-0 7/9] {COPY}\n"
    "#5 DONE 0.1s\n"
)
FROM_OK = (
    "#1 [internal] load build definition from Dockerfile\n"
    "#1 DONE 0.0s\n"
    "#4 [extra 1/2] FROM docker.io/library/python:3.12-slim\n"
    "#4 DONE 1.0s\n"
)
FROM_RECUR = (
    "#1 [internal] load build definition from Dockerfile\n"
    "ERROR: failed to build: failed to solve: dockerfile parse error on line "
    "{n}: FROM requires either one or three arguments\n"
)


def _copy_recur(line: int = 11, path: str = "docs/setup.md") -> str:
    """A copy-missing/buildkit log whose ``>>>`` block is at ``line``."""
    return (
        "#1 [internal] load build definition from Dockerfile\n"
        f"#12 [stage-0 7/9] {COPY}\n"
        f'#12 ERROR: failed to calculate checksum of ref {REF}: "/{path}": '
        "not found\n"
        f"Dockerfile:{line}\n"
        "--------------------\n"
        f"  {line} | >>> {COPY}\n"
        "--------------------\n"
    )


def _job(log: str, job_id: int = 11) -> FailedJob:
    """A job built from ``log`` by forge's own reading (green build step)."""
    record = {
        "name": "Build image",
        "conclusion": "success",
        "steps": [
            {"number": 1, "name": "Set up job", "conclusion": "success"},
            {"number": 2, "name": BUILD_STEP, "conclusion": "success"},
        ],
    }
    return _build_job(record, job_id, log, [], Completeness("present", "absent"))


def _q(
    log: str = COPY_OK,
    status: Qualification = "qualified",
    reason: str | None = None,
    run_id: int = 7,
    attempt: int = 1,
) -> Qualified:
    """An attempt qualified on the job read from ``log``."""
    job = _job(log) if status == "qualified" else None
    return Qualified(run_id, attempt, "image", status, reason, job, None)


def _copy(log: str, q: Qualified | None = None) -> AttemptEvidence:
    """COPY evidence with the CI COPY row enabled, the log as qualified."""
    with enable_for_test("copy-passed/buildkit"):
        return attempt_evidence(
            q or _q(log), "missing_copy_source", COPY, COPY_LINES, log
        )


def _ev(
    qualification: Qualification = "qualified",
    positive: bool = False,
    recurred: bool = False,
    template: str | None = "not_confirmed",
    run: int = 1,
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
    )


# attempt_evidence -------------------------------------------------------------


def test_key_and_positive_copy() -> None:
    """A passed COPY template is positive, keyed by run, attempt, job key."""
    got = _copy(COPY_OK)
    assert got.key == (7, 1, "image")
    assert (got.qualification, got.positive, got.recurred) == ("qualified", True, False)
    assert got.template == "passed"
    assert got.detail is None
    lines = job_text(_job(COPY_OK)).split("\n")
    assert [lines[n - 1] for n in got.lines] == [
        f"#5 [stage-0 7/9] {COPY}",
        "#5 DONE 0.1s",
    ]


def test_rows_disabled_is_not_enabled() -> None:
    """Production rows are disabled: no positive, ``not_enabled``."""
    got = attempt_evidence(_q(), "missing_copy_source", COPY, COPY_LINES, COPY_OK)
    assert (got.positive, got.template, got.detail) == (
        False,
        "not_enabled",
        "templates not enabled",
    )


def test_positive_from() -> None:
    """A passed FROM template is positive."""
    with enable_for_test("from-parsed/buildkit"):
        got = attempt_evidence(
            _q(FROM_OK), "from_argument_count", FROM, FROM_LINES, FROM_OK
        )
    assert (got.positive, got.recurred, got.template) == (True, False, "passed")


def test_copy_recurrence_same_span() -> None:
    """A copy-missing match at the corrected span recurs."""
    got = _copy(_copy_recur())
    assert (got.positive, got.recurred) == (False, True)
    assert got.detail == "defect recurred at lines 11-11"
    assert got.lines


def test_copy_recurrence_other_object() -> None:
    """A "not found" for a new source still recurs: whatever its object."""
    assert _copy(_copy_recur(path="docs/new.md")).recurred


def test_copy_other_span_is_not_recurrence() -> None:
    """A match at another span is not the corrected instruction."""
    assert not _copy(_copy_recur(line=12)).recurred


def test_ambiguous_match_is_not_recurrence() -> None:
    """``"ambiguous"`` is not a recurrence."""
    log = _copy_recur() + (
        f'#9 ERROR: failed to calculate checksum of ref {REF}: "/x": not found\n'
    )
    assert not _copy(log).recurred


def test_from_recurrence_same_line() -> None:
    """A FROM parse error naming the corrected line recurs as ``(N, N)``."""
    log = FROM_RECUR.format(n=1)
    got = attempt_evidence(_q(log), "from_argument_count", FROM, FROM_LINES, log)
    assert got.recurred
    other = FROM_RECUR.format(n=2)
    got = attempt_evidence(_q(other), "from_argument_count", FROM, FROM_LINES, other)
    assert not got.recurred


def test_recurrence_ignores_other_class() -> None:
    """Only the admitted class's matcher counts."""
    log = FROM_RECUR.format(n=11)
    got = attempt_evidence(_q(log), "missing_copy_source", COPY, COPY_LINES, log)
    assert not got.recurred


@pytest.mark.parametrize("status", ["excluded", "undetermined"])
def test_non_qualified_is_carried(status: Qualification) -> None:
    """A non-qualified attempt keeps its qualification and reason."""
    q = _q(status=status, reason="why")
    got = attempt_evidence(q, "missing_copy_source", COPY, COPY_LINES, COPY_OK)
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
    got = attempt_evidence(_q(), bad, COPY, COPY_LINES, COPY_OK)
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
    )
    assert evaluate([positive, unread], True) == (
        INSUFFICIENT,
        "qualification undetermined",
    )
    assert evaluate([positive], True) == ("ci_confirmed", None)
