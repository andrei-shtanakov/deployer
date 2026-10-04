"""Archive states and the wiring in ``fetch_failed_run``.

Spec §1, §4.1, §6, §7.4.
"""

import json
from dataclasses import dataclass, field

import pytest

from deployer.diagnose import diagnose_run
from deployer.forge import (
    ArchiveStatus,
    FailedRun,
    GhError,
    GhTimeout,
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
_STEP_2 = f"\ufeff{TS}##[group]Run a\n{TS}a-cmd\n{TS}##[endgroup]\n{TS}a-out\n"
ARCHIVE_PATH = "repos/o/r/actions/runs/1/attempts/1/logs"


@dataclass
class ArchiveFakeGh(FakeGh):
    """``FakeGh`` plus a per-job log map and the capped archive endpoint."""

    archive: bytes | OverCap | GhError = b""
    logs_by_job: dict[int, str | GhError] = field(default_factory=dict)
    archive_calls: list[list[str]] = field(default_factory=list)
    archive_error: Exception | None = None

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
        if self.archive_error is not None:
            raise self.archive_error
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
            ("job-1/2_Run a.txt", _STEP_2.encode()),
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
    reason = run.archive.reason or ""
    assert reason.startswith("HTTP 404") and reason.count("404") == 1
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "no_archive"


def test_no_kept_job_means_no_archive_attempt() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[job(2, conclusion="success")]]
    gh.logs_by_job = {2: f"{TS}other\n"}
    run = _run(gh)
    assert run.archive is None and run.jobs == []
    assert gh.archive_calls == []


def _assert_diagnosed_without_steps(run: FailedRun) -> None:
    assert all(not isinstance(e.source, StepRef) for j in run.jobs for e in j.evidence)
    diagnosis = diagnose_run(run)
    assert diagnosis.failures
    assert all(
        not isinstance(e.source, StepRef)
        for f in diagnosis.failures
        for e in f.evidence
    )


def test_a_malformed_green_job_record_makes_binding_unverifiable() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [
        [*gh.job_pages[0], job(2, conclusion="success", steps=[{"name": "x"}])]
    ]
    run = _run(gh)
    assert run.archive == ArchiveStatus("available")
    binding = run.jobs[0].step_binding
    assert binding is not None and binding.state == "unverifiable"
    reason = binding.reason or ""
    assert len(reason) < 200 and "green job 2" in reason
    assert "a step without an int `number`" in reason
    _assert_diagnosed_without_steps(run)


def test_a_malformed_kept_job_record_still_raises_status_less() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[job(1, steps=[{"name": "x"}])]]
    with pytest.raises(GhError) as caught:
        _run(gh)
    assert caught.value.status is None


def test_kept_job_states_are_checked_before_any_green_log_is_read() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[job(2, conclusion="success"), *gh.job_pages[0]]]
    gh.logs_by_job = {1: GhError("gh api … failed (HTTP 404)", 404)}
    run = _run(gh)
    assert [j.step_binding.state for j in run.jobs if j.step_binding] == [
        "unverifiable"
    ]
    assert not [c for c in gh.calls if c.argv[-1].endswith("jobs/2/logs")]


def _with_green_log(error: GhError) -> ArchiveFakeGh:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    gh.logs_by_job = {2: error}
    return gh


def test_a_timeout_on_a_green_log_makes_binding_unverifiable() -> None:
    run = _run(_with_green_log(GhTimeout("gh api … timed out after 30.0s")))
    assert run.archive == ArchiveStatus("available")
    binding = run.jobs[0].step_binding
    assert binding is not None and binding.state == "unverifiable"
    reason = binding.reason or ""
    assert len(reason) < 200 and "job 2" in reason and "timed out" in reason
    _assert_diagnosed_without_steps(run)


def test_another_status_less_error_on_a_green_log_propagates() -> None:
    with pytest.raises(GhError) as caught:
        _run(_with_green_log(GhError("gh api could not start: no gh")))
    assert not isinstance(caught.value, GhTimeout)


def test_a_timeout_on_a_kept_log_propagates() -> None:
    gh = _gh(_bound_archive())
    gh.logs_by_job = {1: GhTimeout("gh api … timed out after 30.0s")}
    with pytest.raises(GhTimeout):
        _run(gh)


def test_a_long_green_record_id_gives_a_short_reason() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [
        [*gh.job_pages[0], {**job(2, conclusion="success"), "id": "9" * 10_000}]
    ]
    run = _run(gh)
    binding = run.jobs[0].step_binding
    assert binding is not None and binding.state == "unverifiable"
    assert len(binding.reason or "") < 300 and "`id` is not an int" in (
        binding.reason or ""
    )


def test_a_download_timeout_is_recorded_and_the_diagnosis_is_still_made() -> None:
    plain = _run(FakeGh(job_pages=_gh(b"").job_pages, logs=LOG))
    run = _run(_gh(GhTimeout("gh api … timed out after 120.0s")))
    assert run.archive is not None and run.archive.state == "unavailable"
    assert "timed out" in (run.archive.reason or "")
    assert run.jobs
    for kept in run.jobs:
        assert kept.step_binding is not None
        assert kept.step_binding.state == "no_archive"
    assert [j.completeness for j in run.jobs] == [j.completeness for j in plain.jobs]
    assert run.jobs[0].completeness.logs == "present"
    _assert_diagnosed_without_steps(run)


def test_another_status_less_archive_error_propagates() -> None:
    with pytest.raises(GhError) as caught:
        _run(_gh(GhError("gh api could not start: no gh")))
    assert not isinstance(caught.value, GhTimeout)


def test_an_unexpected_archive_exception_propagates() -> None:
    gh = _gh(b"")
    gh.archive_error = ValueError("boom")
    with pytest.raises(ValueError):
        _run(gh)


def test_a_status_less_error_on_a_kept_log_propagates() -> None:
    gh = _gh(_bound_archive())
    gh.logs_by_job = {1: GhError("gh api … timed out after 30.0s", None)}
    with pytest.raises(GhError):
        _run(gh)


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
