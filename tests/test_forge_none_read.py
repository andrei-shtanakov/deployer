"""``none_read``: a run-level dimension that no job's result counts toward
(TODO forge-nothing-fetched-state). It is never a job's own state."""

import json

import pytest
from pydantic import ValidationError

from deployer.cli import _print_diagnosis
from deployer.diagnose import diagnose_run
from deployer.forge import (
    Completeness,
    FailedJob,
    FailedRun,
    GhError,
    RunRef,
    dump_snapshot,
    fetch_failed_run,
    load_snapshot,
)
from tests.test_forge import FakeGh, job
from tests.test_forge_not_executed import _never_started

EMPTY = "failed run exposes no failed job or step"
CANCELLED = "job 9 (job-9) was cancelled before execution (runner 0, no steps, log 404)"
ALL = "every non-green job was cancelled before execution; no job log holds the failure"


class _NeverStartedGh(FakeGh):
    """One kept job, cancelled before execution; its log reads 404."""

    def api(self, argv: list[str], *, timeout: float) -> str:
        if argv[-1].endswith("jobs/9/logs"):
            raise GhError("gh api … failed: gh: HTTP 404", 404)
        return super().api(argv, timeout=timeout)


def _fetch(gh: FakeGh) -> FailedRun:
    run = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def test_zero_kept_jobs_read_none_read_in_both_dimensions() -> None:
    run = _fetch(FakeGh(job_pages=[[job(1, conclusion="success")]]))
    assert run.jobs == []
    assert run.completeness == Completeness("none_read", "none_read")
    d = diagnose_run(run)
    assert (d.outcome, d.observations) == ("UNCLASSIFIED", [EMPTY])


def test_every_kept_job_recognised_reads_none_read_logs() -> None:
    run = _fetch(_NeverStartedGh(job_pages=[[_never_started(9)]]))
    assert run.completeness == Completeness("none_read", "absent")
    d = diagnose_run(run)
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == [CANCELLED, ALL]


def test_every_kept_job_recognised_with_an_annotations_error() -> None:
    gh = _NeverStartedGh(
        job_pages=[[_never_started(9)]],
        annotations=GhError("gh: Bad Gateway (HTTP 502)", status=502),
    )
    run = _fetch(gh)
    assert run.completeness == Completeness("none_read", "error")
    d = diagnose_run(run)
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == [CANCELLED, ALL, "job 9: annotations fetch error"]


def test_a_normal_run_is_unchanged() -> None:
    run = _fetch(FakeGh())
    assert run.completeness == Completeness("present", "absent")


def test_an_older_empty_snapshot_loads_and_diagnoses_as_before() -> None:
    run = _fetch(FakeGh(job_pages=[[job(1, conclusion="success")]]))
    document = json.loads(dump_snapshot(run))
    document["snapshot_schema_version"] = "1.6"
    document["completeness"] = {"logs": "unavailable", "annotations": "absent"}
    old = load_snapshot(json.dumps(document))
    d = diagnose_run(old)
    assert (d.outcome, d.observations) == ("UNCLASSIFIED", [EMPTY])


@pytest.mark.parametrize("dimension", ["logs", "annotations"])
def test_a_job_can_never_carry_none_read(dimension: str) -> None:
    run = _fetch(FakeGh())
    document = json.loads(dump_snapshot(run))
    document["jobs"][0]["completeness"][dimension] = "none_read"
    with pytest.raises(ValidationError):
        load_snapshot(json.dumps(document))
    good = run.jobs[0]
    with pytest.raises(ValueError):
        FailedJob(
            good.job_id,
            good.name,
            good.conclusion,
            good.steps,
            good.evidence,
            Completeness(
                "none_read" if dimension == "logs" else "present",
                "none_read" if dimension == "annotations" else "absent",
            ),
        )


def test_the_cli_explains_none_read_without_claiming_nothing_was_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_diagnosis(
        diagnose_run(_fetch(_NeverStartedGh(job_pages=[[_never_started(9)]])))
    )
    err = capsys.readouterr().err
    assert "completeness: logs=none_read annotations=absent" in err
    assert "none_read: no job's result counts toward this dimension" in err
    assert "not read" not in err and "nothing was read" not in err
