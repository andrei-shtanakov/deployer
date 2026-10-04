"""Forge: recognition on kept jobs, snapshot 1.6, strict load, the population."""

import json

import pytest
from pydantic import ValidationError

from deployer.forge import (
    ArchiveStatus,
    FailedRun,
    GhError,
    OverCap,
    RunRef,
    StepBinding,
    dump_snapshot,
    fetch_failed_run,
    is_not_executed,
    load_snapshot,
)
from deployer.notexecuted import NotExecuted
from tests import step_binding_data as data
from tests.test_forge import step
from tests.test_forge_archive import LOG, ArchiveFakeGh, _bound_archive

TS = "2026-10-04T09:13:25Z"
EXCLUDED = StepBinding("excluded", "cancelled before execution")


def _never_started(job_id: int) -> dict[str, object]:
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "status": "completed",
        "conclusion": "cancelled",
        "runner_id": 0,
        "runner_name": "",
        "steps": [],
        "created_at": TS,
        "started_at": TS,
    }


def _gh(archive: bytes | OverCap | GhError) -> ArchiveFakeGh:
    gh = ArchiveFakeGh(archive=archive)
    ran = {
        "id": 1,
        "name": "job-1",
        "conclusion": "failure",
        "steps": [step(1, "Set up job", "success"), step(2, "Run a")],
    }
    gh.job_pages = [[ran, _never_started(9)]]
    gh.logs = LOG
    gh.logs_by_job = {9: GhError("gh api … failed: gh: HTTP 404", 404)}
    return gh


def _run(gh: ArchiveFakeGh) -> FailedRun:
    run = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def test_a_recognised_sibling_is_excluded_and_the_other_job_binds() -> None:
    gh = _gh(_bound_archive())
    run = _run(gh)
    ran, sibling = run.jobs
    assert sibling.not_executed == NotExecuted(
        "completed", "cancelled", 0, "", 0, TS, TS, 404
    )
    assert sibling.step_binding == EXCLUDED
    assert sibling.completeness.logs == "error"
    assert is_not_executed(sibling)
    assert ran.step_binding == StepBinding("bound")
    assert run.archive == ArchiveStatus("available")
    assert run.completeness.logs == "present"  # the expected 404 is not in the worst-of
    assert len([c for c in gh.calls if c.argv[-1].endswith("jobs/9/logs")]) == 1


@pytest.mark.parametrize(
    "archive",
    [
        data.zip_of([("0_job-1.txt", LOG.encode())]),  # absent
        b"not a zip",  # refused
        GhError("gh api … failed: Not Found (HTTP 404)", 404),  # unavailable
    ],
    ids=["absent", "refused", "unavailable"],
)
def test_excluded_does_not_depend_on_the_archive(archive: bytes | GhError) -> None:
    run = _run(_gh(archive))
    assert run.jobs[1].step_binding == EXCLUDED
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "no_archive"


def test_excluded_when_no_archive_is_attempted() -> None:
    from tests.test_forge import FakeGh

    gh = FakeGh(job_pages=_gh(b"").job_pages, logs=LOG)

    def api(argv: list[str], *, timeout: float) -> str:
        if argv[-1].endswith("jobs/9/logs"):
            raise GhError("gh api … failed: gh: HTTP 404", 404)
        return FakeGh.api(gh, argv, timeout=timeout)

    gh.api = api  # type: ignore[method-assign]
    run = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=gh)
    assert isinstance(run, FailedRun) and run.archive is None
    assert run.jobs[1].step_binding == EXCLUDED
    assert run.jobs[0].step_binding is None


def test_snapshot_1_6_round_trips_and_1_5_loads_as_not_recognised() -> None:
    run = _run(_gh(_bound_archive()))
    text = dump_snapshot(run)
    assert json.loads(text)["snapshot_schema_version"] == "1.6"
    assert load_snapshot(text) == run
    old = json.loads(text)
    old["snapshot_schema_version"] = "1.5"
    for job in old["jobs"]:
        del job["not_executed"]
    assert all(j.not_executed is None for j in load_snapshot(json.dumps(old)).jobs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runner_id", False),
        ("runner_id", "0"),
        ("steps", True),
        ("log_status", "404"),
        ("runner_name", None),
        ("created_at", 0),
        ("status", "Completed"),
    ],
)
def test_an_ill_typed_stored_basis_fails_to_load(field: str, value: object) -> None:
    document = json.loads(dump_snapshot(_run(_gh(_bound_archive()))))
    document["jobs"][1]["not_executed"][field] = value
    with pytest.raises(ValidationError):
        load_snapshot(json.dumps(document))


def test_other_fields_stay_lax_beside_a_strict_basis() -> None:
    document = json.loads(dump_snapshot(_run(_gh(_bound_archive()))))
    document["jobs"][0]["job_id"] = "1"
    assert load_snapshot(json.dumps(document)).jobs[0].job_id == 1


def test_a_contradicting_stored_basis_is_not_trusted() -> None:
    run = _run(_gh(_bound_archive()))
    document = json.loads(dump_snapshot(run))
    document["jobs"][1]["conclusion"] = "failure"
    assert not is_not_executed(load_snapshot(json.dumps(document)).jobs[1])
