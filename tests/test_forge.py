"""forge.py: the FailedRun snapshot behind the single gh chokepoint."""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
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
    StepInfo,
    StepRef,
    SubprocessGh,
    dump_snapshot,
    fetch_failed_run,
    load_snapshot,
    normalise_workflow_path,
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
    # What the endpoint claims the attempt has; by default the truth (the
    # pages actually served), so a test can make the count lie on purpose.
    job_total: int | None = None
    # Raw page bodies, exactly as returned by the endpoint, for a test that
    # needs a shape `job_pages`/`job_total` cannot express (a missing key, a
    # wrong-typed value). Takes precedence over `job_pages` when set.
    raw_job_pages: list[dict[str, Any]] | None = None
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
        if self.raw_job_pages is not None:
            body = (
                self.raw_job_pages[page - 1] if page <= len(self.raw_job_pages) else {}
            )
            return json.dumps(body)
        total = (
            self.job_total
            if self.job_total is not None
            else sum(len(p) for p in self.job_pages)
        )
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
    fake_gh.logs = GhError("gh: Not Found (HTTP 404)", status=404)
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


def test_a_short_jobs_listing_is_an_error_not_a_partial_snapshot(fake_gh):
    """Spec §2.1: a partially collected snapshot is never presented as complete.

    The endpoint says the attempt has two jobs and then serves an empty second
    page. Snapshotting the one job that did arrive lets `diagnose` reach
    CLASSIFIED — exit 0 — over a run whose other job was never looked at.
    """
    fake_gh.job_pages = [[job(1)], []]
    fake_gh.job_total = 2
    with pytest.raises(GhError) as caught:
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert str(caught.value) == "jobs listing incomplete: 1 of 2"
    assert caught.value.status is None


def test_a_total_count_that_lies_high_is_read_honestly_as_a_short_listing(fake_gh):
    """The adapter cannot tell an inflated `total_count` from a lost page.

    Both say "you have fewer jobs than this attempt has", and only one of the
    two readings is safe, so the short listing is refused either way.
    """
    fake_gh.job_pages = [[job(1)]]
    fake_gh.job_total = 50
    with pytest.raises(GhError) as caught:
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert str(caught.value) == "jobs listing incomplete: 1 of 50"


def test_a_jobs_page_missing_total_count_and_jobs_is_malformed_not_complete(fake_gh):
    """A page 2 body of `{}` must never read as "attempt has 1 job, done".

    Before this reading, `total = int(body.get("total_count", len(collected)))`
    fell back to the jobs collected so far, so a malformed page silently
    looked like a complete listing instead of a broken response.
    """
    fake_gh.raw_job_pages = [{"total_count": 2, "jobs": [job(1)]}, {}]
    with pytest.raises(GhError) as caught:
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert str(caught.value).startswith("jobs listing malformed:")
    assert caught.value.status is None


def test_a_jobs_listing_whose_total_count_changes_mid_listing_is_inconsistent(fake_gh):
    fake_gh.raw_job_pages = [
        {"total_count": 2, "jobs": [job(1)]},
        {"total_count": 3, "jobs": [job(2)]},
    ]
    with pytest.raises(GhError) as caught:
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert str(caught.value) == "jobs listing inconsistent: total_count 2 then 3"
    assert caught.value.status is None


def test_a_jobs_page_whose_jobs_field_is_not_a_list_is_malformed(fake_gh):
    fake_gh.raw_job_pages = [{"total_count": 2, "jobs": "nope"}]
    with pytest.raises(GhError) as caught:
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert str(caught.value).startswith("jobs listing malformed:")


def test_a_jobs_listing_that_meets_its_count_never_asks_for_an_empty_page(fake_gh):
    """The guard against an endless loop is the count being met, not exhaustion."""
    fake_gh.job_pages = [[job(1), job(2)]]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert [c.page for c in fake_gh.job_calls] == [1]


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
        Evidence(
            source=1,
            text="Process completed with exit code 1.",
            level="failure",
        ),
        Evidence(source=1, text="deprecated", level="warning"),
    ]


def test_an_annotation_keeps_its_message_verbatim_and_its_level_as_data(fake_gh):
    """Round 7: the level used to be rendered into the evidence text, so
    every text rule downstream had to see through it -- a message opening
    with an empty line hid the level, and the `failure: ` prefix stood
    between the `no such file` rule and the exception line it must skip.
    The message is now the evidence text verbatim; the level is a field.
    """
    fake_gh.annotations = [
        {"annotation_level": "warning", "message": "a\nb"},
        {"annotation_level": "failure", "message": "\nConnection timed out"},
    ]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.jobs[0].evidence[-2:] == [
        Evidence(source=1, text="a\nb", level="warning"),
        Evidence(source=1, text="\nConnection timed out", level="failure"),
    ]


def test_log_block_evidence_carries_no_level(fake_gh):
    """A log block has no annotation level, so `level` is None and
    `diagnose` judges it line by line instead of by a whole-block level."""
    fake_gh.job_pages = [[job(1, steps=[step(1, "Test")])]]
    fake_gh.logs = f"{_TS}##[group]Run Test\n{_TS}one\n{_TS}##[endgroup]\n{_TS}two"
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.jobs[0].steps[0].evidence[0].level is None
    assert [e.level for e in snapshot.jobs[0].evidence] == [None]


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


def _logs_per_job(fake_gh: FakeGh, logs_by_job: dict[int, str | GhError]) -> Any:
    """A runner that answers each job's log endpoint differently."""

    def answer(argv: list[str], *, timeout: float) -> str:
        m = _LOGS_RE.search(argv[-1])
        if m:
            result = logs_by_job[int(m.group(1))]
            if isinstance(result, GhError):
                raise result
            return result
        return FakeGh.api(fake_gh, argv, timeout=timeout)

    class Mixed:
        api = staticmethod(answer)

    return Mixed()


def _one_readable_one_not(fake_gh: FakeGh) -> FailedRun:
    """The fail-fast matrix shape: job 1 logs fine, job 2's log endpoint 404s."""
    fake_gh.job_pages = [[job(1), job(2)]]
    runner = _logs_per_job(
        fake_gh, {1: "fine", 2: GhError("gh: Not Found (HTTP 404)", status=404)}
    )
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=runner)
    assert isinstance(snapshot, FailedRun)
    return snapshot


def test_completeness_is_the_worst_across_kept_jobs(fake_gh):
    """One job's logs failing outranks another job's logs arriving."""
    assert _one_readable_one_not(fake_gh).completeness.logs == "error"


def test_each_job_carries_the_state_of_its_own_read(fake_gh):
    """The run aggregate is the worst-of; a job says only what happened to it.

    Without this, the sibling whose log 404s speaks for the job that was
    read completely, and diagnose drops that job's established cause.
    """
    snapshot = _one_readable_one_not(fake_gh)
    assert [j.completeness.logs for j in snapshot.jobs] == ["present", "error"]
    assert [j.completeness.annotations for j in snapshot.jobs] == ["absent", "absent"]


def test_a_hand_built_job_is_complete_by_construction():
    """The default is what a job built in a test or a fixture asserts."""
    assert FailedJob(1, "j", "failure", [], []).completeness == Completeness(
        logs="present", annotations="absent"
    )


def test_a_gh_failure_with_no_http_status_propagates(fake_gh):
    """A `gh` that never reached GitHub — unknown flag, timeout, missing
    binary — is a broken instrument, not missing data. Filing it under
    `logs="error"` reports a tooling defect to the operator as an
    unreadable log and turns every run into EVIDENCE_UNAVAILABLE.
    """
    fake_gh.logs = GhError("unknown flag: --allow-escape-sequences")
    with pytest.raises(GhError):
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)


def test_an_http_status_error_on_logs_is_still_recorded_as_data(fake_gh):
    """The twin: GitHub answered, so the answer is a fact about the run."""
    fake_gh.logs = GhError("gh: Internal Server Error (HTTP 500)", status=500)
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness.logs == "error"


def test_a_status_less_gh_failure_on_annotations_propagates_too(fake_gh):
    """The same class as the logs path: a `gh` that never reached GitHub is a
    broken instrument, not an annotations endpoint that answered badly."""
    fake_gh.annotations = GhError("unknown flag: --paginate")
    with pytest.raises(GhError):
        fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)


def test_a_status_error_on_annotations_keeps_the_partial_list(fake_gh):
    """The twin: GitHub answered, so the state is data — and the pages that
    did arrive before the bad one are kept as evidence."""
    page_one = [{"annotation_level": "failure", "message": f"m{i}"} for i in range(100)]

    def annotations_then_fail(argv: list[str], *, timeout: float) -> str:
        m = _ANNOTATIONS_RE.search(argv[-1])
        if m:
            if int(m.group(2)) == 1:
                return json.dumps(page_one)
            raise GhError("gh: Internal Server Error (HTTP 500)", status=500)
        return FakeGh.api(fake_gh, argv, timeout=timeout)

    class Paged:
        api = staticmethod(annotations_then_fail)

    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=Paged())
    assert isinstance(snapshot, FailedRun)
    assert snapshot.completeness.annotations == "error"
    annotation_evidence = [e for e in snapshot.jobs[0].evidence if e.source == 1]
    assert len(annotation_evidence) == 100


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
    document = json.loads(text)
    assert document["snapshot_schema_version"] == "1.3"
    assert document["jobs"][0]["completeness"] == {
        "logs": "present",
        "annotations": "present",
    }
    restored = load_snapshot(text)
    assert restored == snapshot
    sources = {type(e.source) for j in restored.jobs for e in j.evidence}
    assert sources == {type(None), int}
    assert restored.jobs[0].steps[0].evidence[0].source == StepRef(1, 1)
    assert restored.jobs[0].evidence[-1].level == "failure"


def test_a_schema_1_0_snapshot_still_loads(fake_gh):
    """`completeness` on a job is additive: 1.0 documents predate it.

    A stored 1.0 snapshot says nothing about how each job was read, and the
    default reads it as complete — which is what 1.0's whole-run
    `completeness` already implied for every job it kept.
    """
    old = json.dumps(
        {
            "repo": "o/r",
            "run_id": 1,
            "attempt": 1,
            "head_sha": "abc123",
            "url": "https://github.com/o/r/actions/runs/1",
            "jobs": [
                {
                    "job_id": 1,
                    "name": "job-1",
                    "conclusion": "failure",
                    "steps": [],
                    "evidence": [{"source": None, "text": "a line"}],
                }
            ],
            "completeness": {"logs": "present", "annotations": "absent"},
            "snapshot_schema_version": "1.0",
        }
    )
    restored = load_snapshot(old)
    assert restored.snapshot_schema_version == "1.0"
    assert restored.jobs[0].completeness == Completeness("present", "absent")
    assert restored.jobs[0].evidence[0].level is None


def test_a_schema_1_1_snapshot_without_evidence_levels_still_loads():
    """`level` on a piece of evidence is additive too: 1.1 documents predate
    it, and a snapshot written before round 7 carries the level inside the
    annotation's text. It reads back as level-less evidence — which is what
    a log block is — rather than failing to load."""
    old = json.dumps(
        {
            "repo": "o/r",
            "run_id": 1,
            "attempt": 1,
            "head_sha": "abc123",
            "url": "https://github.com/o/r/actions/runs/1",
            "jobs": [
                {
                    "job_id": 1,
                    "name": "job-1",
                    "conclusion": "failure",
                    "steps": [],
                    "evidence": [{"source": 1, "text": "failure: a line"}],
                    "completeness": {"logs": "present", "annotations": "present"},
                }
            ],
            "completeness": {"logs": "present", "annotations": "present"},
            "snapshot_schema_version": "1.1",
        }
    )
    restored = load_snapshot(old)
    assert restored.snapshot_schema_version == "1.1"
    assert restored.jobs[0].evidence[0] == Evidence(1, "failure: a line", None)


def test_snapshot_types_construct_positionally():
    assert RunRef("o/r", 1) == RunRef(repo="o/r", run_id=1)
    refusal = AdapterRefusal("not_failed", "conclusion is success")
    assert refusal.reason == "not_failed"
    run = FailedRun("o/r", 1, 1, "sha", "url", [], Completeness("present", "absent"))
    assert run.snapshot_schema_version == "1.3"
    assert FailedJob(1, "j", "failure", [], []).steps == []
    assert FailedStep(StepRef(1, 1), "s", "failure", []).ref.number == 1
    assert Evidence(None, "a line").level is None
    assert Evidence(1, "a line", "warning") == Evidence(1, "a line", level="warning")


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


# --- schema 1.3: workflow path, event, all steps -----------------------------


def test_snapshot_carries_workflow_path_event_and_all_steps(fake_gh):
    fake_gh.run = {
        **fake_gh.run,
        "path": ".github/workflows/build.yml@main",
        "event": "workflow_dispatch",
    }
    fake_gh.job_pages = [
        [job(1, steps=[step(1, "Set up job", "success"), step(2, "Build")])]
    ]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.workflow_ref_path == ".github/workflows/build.yml@main"
    assert snapshot.workflow_path == ".github/workflows/build.yml"
    assert snapshot.event == "workflow_dispatch"
    assert snapshot.jobs[0].all_steps == [
        StepInfo(1, "Set up job", "success"),
        StepInfo(2, "Build", "failure"),
    ]
    # the kept failed steps are unchanged: only the non-green one
    assert [s.name for s in snapshot.jobs[0].steps] == ["Build"]


def test_run_without_path_or_event_records_none(fake_gh):
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.workflow_ref_path is None
    assert snapshot.workflow_path is None
    assert snapshot.event is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".github/workflows/a.yml", ".github/workflows/a.yml"),
        (".github/workflows/a.yml@main", ".github/workflows/a.yml"),
        (
            ".github/workflows/a.yml@refs/heads/x@y",
            ".github/workflows/a.yml@refs/heads/x",
        ),
    ],
)
def test_normalise_workflow_path_strips_the_last_ref_suffix(raw, expected):
    assert normalise_workflow_path(raw) == expected


def test_a_1_2_snapshot_loads_with_the_new_fields_absent():
    old = json.loads(
        (Path(__file__).parent / "fixtures" / "runs" / "authoring.json").read_text()
    )
    snapshot = load_snapshot(json.dumps(old))
    assert snapshot.snapshot_schema_version == "1.2"
    assert snapshot.workflow_path is None and snapshot.event is None
    assert snapshot.jobs[0].all_steps is None
