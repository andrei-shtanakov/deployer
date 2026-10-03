"""Archive states and the wiring in ``fetch_failed_run`` (spec §1, §4.1, §6, §7.4)."""

import json
from dataclasses import dataclass, field

import pytest

from deployer.forge import (
    ArchiveStatus,
    FailedRun,
    GhError,
    OverCap,
    RunRef,
    StepBinding,
    StepRef,
    dump_snapshot,
    fetch_failed_run,
    load_snapshot,
)
from deployer.reproduce.shape import job_text
from tests import step_binding_data as data
from tests.test_forge import _LOGS_RE, Call, FakeGh, job, step

TS = "2026-10-03T10:08:54.1234567Z "
LOG = f"\ufeff{TS}setup\n{TS}##[group]Run a\n{TS}a-cmd\n{TS}##[endgroup]\n{TS}a-out\n"
ARCHIVE_PATH = "repos/o/r/actions/runs/1/attempts/1/logs"


@dataclass
class ArchiveFakeGh(FakeGh):
    """``FakeGh`` plus a per-job log map and the capped archive endpoint."""

    archive: bytes | OverCap | GhError = b""
    logs_by_job: dict[int, str | GhError] = field(default_factory=dict)
    archive_calls: list[list[str]] = field(default_factory=list)

    def api(self, argv: list[str], *, timeout: float) -> str:
        match = _LOGS_RE.search(argv[-1])
        if match and int(match.group(1)) in self.logs_by_job:
            self.calls.append(Call(argv=list(argv), timeout=timeout))
            value = self.logs_by_job[int(match.group(1))]
            if isinstance(value, GhError):
                raise value
            return value
        return super().api(argv, timeout=timeout)

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        self.archive_calls.append(list(argv))
        if isinstance(self.archive, GhError):
            raise self.archive
        return self.archive


def _bound_archive() -> bytes:
    """A step directory whose two files add up to ``LOG``: step 1, then step 2."""
    return data.zip_of(
        [
            ("0_job-1.txt", LOG.encode()),
            ("job-1/system.txt", b"runner\n"),
            ("job-1/1_Set up job.txt", f"\ufeff{TS}setup\n".encode()),
            (
                "job-1/2_Run a.txt",
                f"\ufeff{TS}##[group]Run a\n{TS}a-cmd\n{TS}##[endgroup]\n{TS}a-out\n".encode(),
            ),
        ]
    )


def _gh(archive: bytes | OverCap | GhError) -> ArchiveFakeGh:
    gh = ArchiveFakeGh(archive=archive)
    gh.job_pages = [
        [job(1, steps=[step(1, "Set up job", "success"), step(2, "Run a")])]
    ]
    gh.logs = LOG
    return gh


def _run(gh: FakeGh, attempt: int | None = 1) -> FailedRun:
    run = fetch_failed_run(RunRef("o/r", 1), attempt=attempt, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def test_a_runner_without_the_capped_download_attempts_nothing() -> None:
    run = _run(FakeGh())
    assert run.archive is None and run.jobs[0].step_binding is None


def test_a_bound_job_cites_its_step_and_keeps_its_job_text() -> None:
    gh = _gh(_bound_archive())
    run = _run(gh)
    assert run.archive == ArchiveStatus("available")
    assert run.jobs[0].step_binding == StepBinding("bound")
    by_text = {e.text: e.source for e in run.jobs[0].evidence if e.source != 1}
    assert by_text["a-out"] == StepRef(1, 2)
    plain = _run(FakeGh(job_pages=gh.job_pages, logs=LOG))
    assert job_text(run.jobs[0]) == job_text(plain.jobs[0])
    assert gh.archive_calls == [[ARCHIVE_PATH]]


def test_the_archive_request_names_the_resolved_attempt() -> None:
    gh = _gh(_bound_archive())
    gh.run = {**gh.run, "run_attempt": 2}
    run = _run(gh, attempt=None)
    assert run.attempt == 2
    assert gh.archive_calls == [["repos/o/r/actions/runs/1/attempts/2/logs"]]


def test_an_http_error_makes_the_archive_unavailable() -> None:
    run = _run(_gh(GhError("gh api … failed: Not Found (HTTP 404)", 404)))
    assert run.archive is not None and run.archive.state == "unavailable"
    assert (run.archive.reason or "").startswith("HTTP 404")
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "no_archive"


def test_a_status_less_error_propagates() -> None:
    with pytest.raises(GhError):
        _run(_gh(GhError("gh api … timed out after 120.0s")))


def test_an_archive_over_the_download_cap_is_refused() -> None:
    run = _run(_gh(OverCap(64 * 2**20)))
    assert run.archive is not None and run.archive.state == "refused"
    assert str(64 * 2**20) in (run.archive.reason or "")


def test_a_corrupt_archive_is_refused() -> None:
    run = _run(_gh(b"not a zip"))
    assert run.archive is not None and run.archive.state == "refused"
    assert run.jobs[0].step_binding == StepBinding(
        "no_archive", "the log archive was refused"
    )


def test_an_archive_without_step_files_is_absent_and_costs_no_extra_reads() -> None:
    gh = _gh(data.zip_of([("0_job-1.txt", LOG.encode()), ("job-1/system.txt", b"x\n")]))
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    run = _run(gh)
    assert run.archive is not None and run.archive.state == "absent"
    assert not [c for c in gh.calls if c.argv[-1].endswith("jobs/2/logs")]


def test_a_green_job_is_read_for_the_population_but_not_kept() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    gh.logs_by_job = {2: f"{TS}other job\n"}
    run = _run(gh)
    assert [j.job_id for j in run.jobs] == [1]
    assert [c for c in gh.calls if c.argv[-1].endswith("jobs/2/logs")]
    assert run.jobs[0].step_binding == StepBinding("bound")


def test_a_skipped_job_is_not_read() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(3, conclusion="skipped")]]
    _run(gh)
    assert not [c for c in gh.calls if c.argv[-1].endswith("jobs/3/logs")]


def test_an_unreadable_population_log_makes_binding_unverifiable() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    gh.logs_by_job = {2: GhError("gh api … failed (HTTP 502)", 502)}
    run = _run(gh)
    assert run.archive == ArchiveStatus("available")
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "unverifiable"
    assert all(e.source is None or e.source == 1 for e in run.jobs[0].evidence)


def test_a_job_that_never_started_makes_binding_unverifiable() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="cancelled")]]
    gh.logs_by_job = {2: GhError("gh api … failed (HTTP 404)", 404)}
    run = _run(gh)
    assert {j.job_id: j.step_binding.state for j in run.jobs if j.step_binding} == {
        1: "unverifiable",
        2: "unverifiable",
    }


def test_snapshot_1_5_round_trips_and_1_4_loads_as_not_attempted() -> None:
    run = _run(_gh(_bound_archive()))
    text = dump_snapshot(run)
    assert json.loads(text)["snapshot_schema_version"] == "1.5"
    assert load_snapshot(text) == run
    old = json.loads(text)
    old["snapshot_schema_version"] = "1.4"
    del old["archive"]
    for job_doc in old["jobs"]:
        del job_doc["step_binding"]
    restored = load_snapshot(json.dumps(old))
    assert restored.archive is None and restored.jobs[0].step_binding is None
