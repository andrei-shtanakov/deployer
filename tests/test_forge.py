"""forge.py: the FailedRun snapshot behind the single gh chokepoint."""

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from deployer.forge import (
    GH_TIMEOUT_S,
    AdapterRefusal,
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
    FailedStep,
    GhError,
    RunRef,
    StepRef,
    SubprocessGh,
    dump_snapshot,
    fetch_failed_run,
    load_snapshot,
)

_JOBS_RE = re.compile(r"/attempts/(\d+)/jobs\?per_page=100&page=(\d+)$")
_ANNOTATIONS_RE = re.compile(r"/check-runs/(\d+)/annotations\?per_page=100&page=(\d+)$")
_LOGS_RE = re.compile(r"/actions/jobs/(\d+)/logs$")
_ATTEMPT_RE = re.compile(r"/attempts/(\d+)$")


@dataclass
class Call:
    argv: list[str]
    timeout: float | None
    shell: bool = False


@dataclass
class JobCall:
    attempt: int
    page: int


def step(number: int, name: str, conclusion: str = "failure") -> dict[str, Any]:
    return {"number": number, "name": name, "conclusion": conclusion}


def job(
    job_id: int,
    conclusion: str = "failure",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A job record as the jobs endpoint returns it."""
    if steps is None:
        steps = [step(1, "Checkout", "success"), step(2, "Test")]
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "conclusion": conclusion,
        "steps": steps,
    }


@dataclass
class FakeGh:
    """A GhRunner that serves canned responses and records every call."""

    run: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
            "head_sha": "abc123",
            "html_url": "https://github.com/o/r/actions/runs/1",
        }
    )
    job_pages: list[list[dict[str, Any]]] = field(default_factory=lambda: [[job(1)]])
    annotations: list[dict[str, Any]] | GhError = field(default_factory=list)
    logs: str | GhError | None = "plain job log"
    calls: list[Call] = field(default_factory=list)
    job_calls: list[JobCall] = field(default_factory=list)

    def api(self, argv: list[str], *, timeout: float) -> str:
        self.calls.append(Call(argv=list(argv), timeout=timeout))
        path = argv[-1]  # flags (e.g. --allow-escape-sequences) precede it
        if m := _JOBS_RE.search(path):
            return self._jobs_page(int(m.group(1)), int(m.group(2)))
        if m := _ANNOTATIONS_RE.search(path):
            return self._annotations_page(int(m.group(2)))
        if _LOGS_RE.search(path):
            return self._logs()
        if m := _ATTEMPT_RE.search(path):
            return json.dumps({**self.run, "run_attempt": int(m.group(1))})
        return json.dumps(self.run)

    def _jobs_page(self, attempt: int, page: int) -> str:
        self.job_calls.append(JobCall(attempt=attempt, page=page))
        total = sum(len(p) for p in self.job_pages)
        items = self.job_pages[page - 1] if page <= len(self.job_pages) else []
        return json.dumps({"total_count": total, "jobs": items})

    def _annotations_page(self, page: int) -> str:
        if isinstance(self.annotations, GhError):
            raise self.annotations
        return json.dumps(self.annotations if page == 1 else [])

    def _logs(self) -> str:
        if isinstance(self.logs, GhError):
            raise self.logs
        return self.logs or ""


@pytest.fixture()
def fake_gh() -> FakeGh:
    return FakeGh()


# --- the brief's tests ------------------------------------------------------


def test_unfinished_run_is_refused_not_snapshotted(fake_gh):
    fake_gh.run = {"status": "in_progress", "conclusion": None}
    out = fetch_failed_run(RunRef(repo="o/r", run_id=1), attempt=None, runner=fake_gh)
    assert isinstance(out, AdapterRefusal) and out.reason == "not_finished"


def test_successful_run_is_refused(fake_gh):
    fake_gh.run = {"status": "completed", "conclusion": "success"}
    out = fetch_failed_run(RunRef(repo="o/r", run_id=1), attempt=None, runner=fake_gh)
    assert isinstance(out, AdapterRefusal) and out.reason == "not_failed"


def test_attempt_is_resolved_once_before_reading_jobs(fake_gh):
    """A re-run must not mix evidence from different attempts."""
    fake_gh.run = {"status": "completed", "conclusion": "failure", "run_attempt": 3}
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=None, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.attempt == 3
    assert all(call.attempt == 3 for call in fake_gh.job_calls)


def test_jobs_are_read_with_pagination(fake_gh):
    fake_gh.job_pages = [[job(1)], [job(2)]]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert [j.job_id for j in snapshot.jobs] == [1, 2]


def test_three_completeness_states_are_distinct(fake_gh):
    fake_gh.logs = None
    unavailable = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(unavailable, FailedRun)
    assert unavailable.completeness.logs == "unavailable"
    fake_gh.logs = GhError("404")
    errored = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(errored, FailedRun)
    assert errored.completeness.logs == "error"


def test_unbound_log_line_keeps_source_none(fake_gh):
    """Where the API gives no line→step binding, the adapter does not invent one."""
    fake_gh.logs = "a line with no step header"
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.jobs[0].evidence[0].source is None


def test_gh_is_invoked_without_a_shell_and_with_a_timeout(fake_gh):
    fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert all(
        isinstance(c.argv, list) and c.timeout is not None for c in fake_gh.calls
    )
    assert not any("--web" in c.argv or c.shell for c in fake_gh.calls)


# --- refusal happens before anything else is read ---------------------------


def _paths(fake_gh: FakeGh) -> list[str]:
    return [c.argv[-1] for c in fake_gh.calls]


def test_refusal_reads_nothing_but_the_run(fake_gh):
    fake_gh.run = {"status": "completed", "conclusion": "cancelled"}
    out = fetch_failed_run(RunRef("o/r", 1), attempt=None, runner=fake_gh)
    assert isinstance(out, AdapterRefusal)
    assert "cancelled" in out.detail
    assert _paths(fake_gh) == ["repos/o/r/actions/runs/1"]


def test_unfinished_detail_names_the_status(fake_gh):
    fake_gh.run = {"status": "queued", "conclusion": None}
    out = fetch_failed_run(RunRef("o/r", 1), attempt=None, runner=fake_gh)
    assert isinstance(out, AdapterRefusal) and "queued" in out.detail


def test_timed_out_is_a_supported_conclusion(fake_gh):
    fake_gh.run["conclusion"] = "timed_out"
    out = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(out, FailedRun)


def test_explicit_attempt_reads_the_attempt_endpoint(fake_gh):
    snapshot = fetch_failed_run(RunRef("o/r", 7), attempt=2, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert _paths(fake_gh)[0] == "repos/o/r/actions/runs/7/attempts/2"
    assert snapshot.attempt == 2
    assert all(call.attempt == 2 for call in fake_gh.job_calls)


def test_identity_is_copied_from_the_run(fake_gh):
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert (snapshot.repo, snapshot.run_id) == ("o/r", 1)
    assert snapshot.head_sha == "abc123"
    assert snapshot.url == "https://github.com/o/r/actions/runs/1"


# --- what is kept -----------------------------------------------------------


def test_only_non_green_jobs_and_steps_are_kept(fake_gh):
    fake_gh.job_pages = [
        [
            job(1, "success"),
            job(2, "skipped"),
            job(3, "neutral"),
            job(4, "cancelled"),
            job(
                5,
                steps=[
                    step(1, "Checkout", "success"),
                    step(2, "Optional", "skipped"),
                    step(3, "Lint", "neutral"),
                    step(4, "Test", "failure"),
                    step(5, "Cleanup", "cancelled"),
                ],
            ),
        ]
    ]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert [j.job_id for j in snapshot.jobs] == [4, 5]
    assert [s.ref for s in snapshot.jobs[1].steps] == [StepRef(5, 4), StepRef(5, 5)]
    assert snapshot.jobs[1].steps[0].name == "Test"


def test_logs_and_annotations_are_fetched_only_for_kept_jobs(fake_gh):
    fake_gh.job_pages = [[job(1, "success"), job(2)]]
    fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    paths = _paths(fake_gh)
    assert "repos/o/r/actions/jobs/2/logs" in paths
    assert "repos/o/r/actions/jobs/1/logs" not in paths
    assert not any("/check-runs/1/" in p for p in paths)


def test_jobs_pagination_stops_at_total_count(fake_gh):
    fake_gh.job_pages = [[job(1)], [job(2)], [job(3)]]
    fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert [c.page for c in fake_gh.job_calls] == [1, 2, 3]


# --- evidence binding -------------------------------------------------------

_TS = "2026-09-22T01:02:03.1234567Z "
_LOG = "\n".join(
    [
        f"{_TS}Current runner version: '2.3'",
        f"{_TS}##[group]Run pytest -q",
        f"{_TS}pytest -q",
        f"{_TS}shell: /usr/bin/bash -e {{0}}",
        f"{_TS}##[endgroup]",
        f"{_TS}FAILED tests/test_x.py::test_y",
        f"{_TS}##[error]Process completed with exit code 1.",
        f"{_TS}##[group]Run Test",
        f"{_TS}named step block",
        f"{_TS}##[endgroup]",
        f"{_TS}##[group]Post job cleanup",
        f"{_TS}cleanup",
        f"{_TS}##[endgroup]",
    ]
)


def test_group_titled_after_a_unique_step_is_bound_to_it(fake_gh):
    fake_gh.job_pages = [[job(1, steps=[step(1, "Run pytest -q"), step(2, "Test")])]]
    fake_gh.logs = _LOG
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    (kept,) = snapshot.jobs
    by_step = {s.ref: s for s in kept.steps}
    assert [e.text for e in by_step[StepRef(1, 1)].evidence] == [
        "##[group]Run pytest -q\npytest -q\nshell: /usr/bin/bash -e {0}\n##[endgroup]"
    ]
    assert by_step[StepRef(1, 1)].evidence[0].source == StepRef(1, 1)
    assert [e.text for e in by_step[StepRef(1, 2)].evidence] == [
        "##[group]Run Test\nnamed step block\n##[endgroup]"
    ]
    assert [e.text for e in kept.evidence] == [
        "Current runner version: '2.3'",
        "FAILED tests/test_x.py::test_y\n##[error]Process completed with exit code 1.",
        "##[group]Post job cleanup\ncleanup\n##[endgroup]",
    ]
    assert all(e.source is None for e in kept.evidence)


def test_ambiguous_step_name_is_not_bound(fake_gh):
    fake_gh.job_pages = [[job(1, steps=[step(1, "Test"), step(2, "Test")])]]
    fake_gh.logs = f"{_TS}##[group]Run Test\n{_TS}x\n{_TS}##[endgroup]"
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert all(not s.evidence for s in snapshot.jobs[0].steps)
    assert [e.source for e in snapshot.jobs[0].evidence] == [None]


def test_block_bound_to_a_green_step_stays_as_job_evidence(fake_gh):
    """The step is not kept, but the block and its real binding are."""
    fake_gh.job_pages = [
        [job(1, steps=[step(1, "Checkout", "success"), step(2, "Test")])]
    ]
    fake_gh.logs = (
        f"{_TS}##[group]Run Checkout\n{_TS}with: fetch-depth 1\n{_TS}##[endgroup]"
    )
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert [s.ref for s in snapshot.jobs[0].steps] == [StepRef(1, 2)]
    assert snapshot.jobs[0].evidence == [
        Evidence(
            source=StepRef(1, 1),
            text="##[group]Run Checkout\nwith: fetch-depth 1\n##[endgroup]",
        )
    ]


def test_group_without_endgroup_runs_to_the_end(fake_gh):
    fake_gh.job_pages = [[job(1, steps=[step(1, "Test")])]]
    fake_gh.logs = f"{_TS}##[group]Run Test\n{_TS}one\n{_TS}two"
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert [e.text for e in snapshot.jobs[0].steps[0].evidence] == [
        "##[group]Run Test\none\ntwo"
    ]


def test_annotations_become_job_evidence(fake_gh):
    fake_gh.annotations = [
        {
            "annotation_level": "failure",
            "message": "Process completed with exit code 1.",
        },
        {"annotation_level": "warning", "message": "deprecated"},
    ]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness.annotations == "present"
    tail = snapshot.jobs[0].evidence[-2:]
    assert tail == [
        Evidence(source=1, text="failure: Process completed with exit code 1."),
        Evidence(source=1, text="warning: deprecated"),
    ]


# --- completeness -----------------------------------------------------------


def test_logs_present_when_text_came_back(fake_gh):
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness == Completeness(logs="present", annotations="absent")


def test_expired_logs_are_unavailable_not_error(fake_gh):
    fake_gh.logs = GhError("gh: Gone (HTTP 410)", status=410)
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness.logs == "unavailable"


def test_annotation_error_is_recorded_not_raised(fake_gh):
    fake_gh.annotations = GhError("gh: Forbidden (HTTP 403)", status=403)
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness.annotations == "error"


def test_completeness_is_the_worst_across_kept_jobs(fake_gh):
    """One job's logs failing outranks another job's logs arriving."""
    fake_gh.job_pages = [[job(1), job(2)]]
    logs_by_job = {1: "fine", 2: GhError("boom")}

    def logs_per_job(argv: list[str], *, timeout: float) -> str:
        m = _LOGS_RE.search(argv[-1])
        if m:
            result = logs_by_job[int(m.group(1))]
            if isinstance(result, GhError):
                raise result
            return result
        return FakeGh.api(fake_gh, argv, timeout=timeout)

    class Mixed:
        api = staticmethod(logs_per_job)

    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=Mixed())
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness.logs == "error"


def test_run_level_gh_error_propagates(fake_gh):
    class Broken:
        def api(self, argv: list[str], *, timeout: float) -> str:
            raise GhError("gh: Not Found (HTTP 404)", status=404)

    with pytest.raises(GhError):
        fetch_failed_run(RunRef("o/r", 1), attempt=None, runner=Broken())


# --- ANSI-coloured logs ------------------------------------------------------


def test_logs_call_passes_allow_escape_sequences_other_calls_do_not(fake_gh):
    """gh refuses real build logs (they carry ANSI colour) without this flag."""
    fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    logs_calls = [c for c in fake_gh.calls if _LOGS_RE.search(c.argv[-1])]
    other_calls = [c for c in fake_gh.calls if not _LOGS_RE.search(c.argv[-1])]
    assert logs_calls and all("--allow-escape-sequences" in c.argv for c in logs_calls)
    assert other_calls and not any(
        "--allow-escape-sequences" in c.argv for c in other_calls
    )


def test_ansi_colour_is_stripped_from_log_evidence(fake_gh):
    """The runner echoes the step command in colour; evidence must be plain."""
    fake_gh.job_pages = [[job(1, steps=[step(1, "Run docker build")])]]
    fake_gh.logs = (
        f"{_TS}##[group]Run docker build\n"
        f"{_TS}\x1b[36;1mdocker build --file ./Dockerfile .\x1b[0m\n"
        f"{_TS}##[endgroup]"
    )
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    all_evidence = [e for j in snapshot.jobs for e in j.evidence] + [
        e for j in snapshot.jobs for s in j.steps for e in s.evidence
    ]
    assert all_evidence
    assert not any("\x1b" in e.text for e in all_evidence)
    (kept,) = snapshot.jobs
    assert "docker build --file ./Dockerfile ." in kept.steps[0].evidence[0].text


# --- versioned serialization -------------------------------------------------


def test_snapshot_round_trips_through_versioned_json(fake_gh):
    fake_gh.job_pages = [[job(1, steps=[step(1, "Run pytest -q"), step(2, "Test")])]]
    fake_gh.logs = _LOG
    fake_gh.annotations = [{"annotation_level": "failure", "message": "m"}]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    text = dump_snapshot(snapshot)
    assert json.loads(text)["snapshot_schema_version"] == "1.0"
    restored = load_snapshot(text)
    assert restored == snapshot
    sources = {type(e.source) for j in restored.jobs for e in j.evidence}
    assert sources == {type(None), int}
    assert restored.jobs[0].steps[0].evidence[0].source == StepRef(1, 1)


def test_snapshot_types_construct_positionally():
    assert RunRef("o/r", 1) == RunRef(repo="o/r", run_id=1)
    refusal = AdapterRefusal("not_failed", "conclusion is success")
    assert refusal.reason == "not_failed"
    run = FailedRun("o/r", 1, 1, "sha", "url", [], Completeness("present", "absent"))
    assert run.snapshot_schema_version == "1.0"
    assert FailedJob(1, "j", "failure", [], []).steps == []
    assert FailedStep(StepRef(1, 1), "s", "failure", []).ref.number == 1


# --- the real runner --------------------------------------------------------


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_subprocess_gh_runs_gh_api_without_a_shell(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return _Proc(0, stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = SubprocessGh().api(["repos/o/r/actions/runs/1"], timeout=GH_TIMEOUT_S)
    assert out == '{"ok": true}'
    assert isinstance(seen["cmd"], list)
    assert seen["cmd"][:2] == ["gh", "api"]
    assert seen["cmd"][2:] == ["repos/o/r/actions/runs/1"]
    assert not seen.get("shell", False)
    assert seen["timeout"] == GH_TIMEOUT_S
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["env"]["GH_PROMPT_DISABLED"] == "1"
    assert seen["env"]["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_subprocess_gh_decodes_with_replacement(monkeypatch):
    """Undecodable bytes in a log must not escape as UnicodeDecodeError."""
    seen: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _Proc(0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessGh().api(["x"], timeout=1.0)
    assert seen["text"] is True
    assert seen["errors"] == "replace"


def test_subprocess_gh_nonzero_exit_is_a_gh_error_with_http_status(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: _Proc(1, stderr="gh: Not Found (HTTP 404)\n"),
    )
    with pytest.raises(GhError) as info:
        SubprocessGh().api(["repos/o/r/actions/runs/1"], timeout=1.0)
    assert info.value.status == 404
    assert "Not Found" in str(info.value)


def test_subprocess_gh_nonzero_exit_without_http_status(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _Proc(4, stderr="not logged in")
    )
    with pytest.raises(GhError) as info:
        SubprocessGh().api(["x"], timeout=1.0)
    assert info.value.status is None


def test_subprocess_gh_timeout_is_a_gh_error(monkeypatch):
    def timed_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(GhError):
        SubprocessGh().api(["x"], timeout=1.0)


def test_subprocess_gh_missing_binary_is_a_gh_error(monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(GhError):
        SubprocessGh().api(["x"], timeout=1.0)
