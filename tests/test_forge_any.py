"""forge.py: reading runs of any outcome by ``head_sha`` (spec §7.1)."""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from deployer.forge import (
    AttemptRead,
    GhError,
    RunSummary,
    list_runs_for_sha,
    read_attempt,
)

_SHA = "f1x0000000000000000000000000000000000000"
_RUNS_RE = re.compile(
    r"^repos/o/r/actions/runs\?head_sha=(\w+)&per_page=100&page=(\d+)$"
)
_JOBS_RE = re.compile(r"/attempts/(\d+)/jobs\?per_page=100&page=(\d+)$")
_LOGS_RE = re.compile(r"/actions/jobs/(\d+)/logs$")
_ATTEMPT_RE = re.compile(r"^repos/o/r/actions/runs/(\d+)/attempts/(\d+)$")

_LOG = "\n".join(
    [
        "##[group]Run docker build .",
        "#5 [2/3] COPY app.py /app/",
        "#5 DONE 0.1s",
        "##[endgroup]",
    ]
)


def run_row(run_id: int = 7, **over: Any) -> dict[str, Any]:
    """A ``workflow_runs`` row as the runs listing returns it."""
    row: dict[str, Any] = {
        "id": run_id,
        "run_attempt": 2,
        "event": "push",
        "head_sha": _SHA,
        "path": ".github/workflows/build.yml",
        "head_branch": "fix/copy",
        "status": "completed",
        "conclusion": "success",
    }
    return {**row, **over}


def job(job_id: int, conclusion: str = "success") -> dict[str, Any]:
    """A job record with one green and one step of ``conclusion``."""
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "conclusion": conclusion,
        "steps": [
            {"number": 1, "name": "Checkout", "conclusion": "success"},
            {"number": 2, "name": "docker build .", "conclusion": conclusion},
        ],
    }


@dataclass
class FakeGh:
    """A GhRunner serving the runs listing, attempts, jobs and logs."""

    run_pages: list[dict[str, Any]] = field(
        default_factory=lambda: [{"total_count": 1, "workflow_runs": [run_row()]}]
    )
    runs_error: GhError | None = None
    attempt: dict[str, Any] = field(
        default_factory=lambda: {"status": "completed", "conclusion": "success"}
    )
    attempt_error: GhError | None = None
    # Identity fields the attempt endpoint leaves out, to test a missing one.
    attempt_drop: tuple[str, ...] = ()
    jobs: list[dict[str, Any]] = field(default_factory=lambda: [job(1), job(2)])
    jobs_error: GhError | None = None
    logs: dict[int, str | GhError] = field(default_factory=dict)
    paths: list[str] = field(default_factory=list)

    def api(self, argv: list[str], *, timeout: float) -> str:
        path = argv[-1]
        self.paths.append(path)
        if m := _RUNS_RE.search(path):
            return self._runs(int(m.group(2)))
        if m := _JOBS_RE.search(path):
            return self._jobs(int(m.group(2)))
        if m := _LOGS_RE.search(path):
            return self._log(int(m.group(1)))
        if m := _ATTEMPT_RE.search(path):
            if self.attempt_error is not None:
                raise self.attempt_error
            body = {
                "id": int(m.group(1)),
                "head_sha": _SHA,
                "run_attempt": int(m.group(2)),
                **self.attempt,
            }
            return json.dumps(
                {k: v for k, v in body.items() if k not in self.attempt_drop}
            )
        raise AssertionError(f"unexpected path {path}")

    def _runs(self, page: int) -> str:
        if self.runs_error is not None:
            raise self.runs_error
        if page > len(self.run_pages):
            return json.dumps({"total_count": 0, "workflow_runs": []})
        return json.dumps(self.run_pages[page - 1])

    def _jobs(self, page: int) -> str:
        if self.jobs_error is not None:
            raise self.jobs_error
        items = self.jobs if page == 1 else []
        return json.dumps({"total_count": len(self.jobs), "jobs": items})

    def _log(self, job_id: int) -> str:
        value = self.logs.get(job_id, _LOG)
        if isinstance(value, GhError):
            raise value
        return value


def summary(**over: Any) -> RunSummary:
    """The RunSummary that ``run_row()`` lists as."""
    base: dict[str, Any] = {
        "run_id": 7,
        "attempts": 2,
        "event": "push",
        "head_sha": _SHA,
        "path": ".github/workflows/build.yml",
        "head_branch": "fix/copy",
    }
    return RunSummary(**{**base, **over})


# --- list_runs_for_sha --------------------------------------------------------


def test_a_complete_listing_yields_one_summary_per_run() -> None:
    fake = FakeGh(
        run_pages=[
            {
                "total_count": 2,
                "workflow_runs": [
                    run_row(7),
                    run_row(
                        8,
                        run_attempt=1,
                        event="workflow_dispatch",
                        path=".github/workflows/build.yml@main",
                        head_branch=None,
                        conclusion="failure",
                    ),
                ],
            }
        ]
    )
    runs = list_runs_for_sha("o/r", _SHA, fake)
    assert runs == [
        summary(),
        summary(
            run_id=8,
            attempts=1,
            event="workflow_dispatch",
            head_branch=None,
        ),
    ]
    assert fake.paths == [f"repos/o/r/actions/runs?head_sha={_SHA}&per_page=100&page=1"]


def test_a_listing_is_paginated_until_total_count_is_met() -> None:
    fake = FakeGh(
        run_pages=[
            {"total_count": 2, "workflow_runs": [run_row(7)]},
            {"total_count": 2, "workflow_runs": [run_row(8)]},
        ]
    )
    runs = list_runs_for_sha("o/r", _SHA, fake)
    assert isinstance(runs, list)
    assert [r.run_id for r in runs] == [7, 8]


def test_no_runs_is_an_empty_complete_listing() -> None:
    fake = FakeGh(run_pages=[{"total_count": 0, "workflow_runs": []}])
    assert list_runs_for_sha("o/r", _SHA, fake) == []


def test_a_run_listed_twice_is_a_reason_not_deduplicated() -> None:
    """Final review M2: a page shift repeats run 7 and hides another run."""
    fake = FakeGh(
        run_pages=[
            {"total_count": 2, "workflow_runs": [run_row(7)]},
            {"total_count": 2, "workflow_runs": [run_row(7)]},
        ]
    )
    assert list_runs_for_sha("o/r", _SHA, fake) == (
        "runs listing incomplete: run 7 listed twice"
    )


def test_more_rows_than_total_count_is_a_reason() -> None:
    """Final review M2: rows past ``total_count`` are not a complete listing."""
    fake = FakeGh(
        run_pages=[{"total_count": 1, "workflow_runs": [run_row(7), run_row(8)]}]
    )
    assert list_runs_for_sha("o/r", _SHA, fake) == (
        "runs listing incomplete: 2 rows exceed total_count 1"
    )


def test_total_count_above_the_rows_delivered_is_a_reason_not_a_shorter_list() -> None:
    """Review Focus 5: count 3, two rows, then an empty page."""
    fake = FakeGh(
        run_pages=[
            {"total_count": 3, "workflow_runs": [run_row(7), run_row(8)]},
            {"total_count": 3, "workflow_runs": []},
        ]
    )
    assert list_runs_for_sha("o/r", _SHA, fake) == "runs listing incomplete: 2 of 3"


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"total_count": 1},
        {"total_count": "1", "workflow_runs": []},
        {"total_count": 1, "workflow_runs": "nope"},
        [],
    ],
)
def test_a_malformed_page_is_a_reason(page: Any) -> None:
    fake = FakeGh(run_pages=[page])
    reason = list_runs_for_sha("o/r", _SHA, fake)
    assert isinstance(reason, str)
    assert reason.startswith("runs listing malformed:")


def test_a_malformed_later_page_is_a_reason_not_the_first_page_alone() -> None:
    fake = FakeGh(run_pages=[{"total_count": 2, "workflow_runs": [run_row(7)]}, {}])
    reason = list_runs_for_sha("o/r", _SHA, fake)
    assert isinstance(reason, str)
    assert reason.startswith("runs listing malformed:")


def test_an_inconsistent_total_count_is_a_reason() -> None:
    fake = FakeGh(
        run_pages=[
            {"total_count": 2, "workflow_runs": [run_row(7)]},
            {"total_count": 3, "workflow_runs": [run_row(8)]},
        ]
    )
    assert list_runs_for_sha("o/r", _SHA, fake) == (
        "runs listing inconsistent: total_count 2 then 3"
    )


@pytest.mark.parametrize(
    "row",
    [
        {"id": 7},
        run_row(run_attempt="2"),
        run_row(event=None),
        run_row(path=None),
        run_row(head_branch=5),
        run_row(id=True),
        "not a row",
    ],
)
def test_a_malformed_row_is_a_reason(row: Any) -> None:
    fake = FakeGh(run_pages=[{"total_count": 1, "workflow_runs": [row]}])
    reason = list_runs_for_sha("o/r", _SHA, fake)
    assert isinstance(reason, str)
    assert reason.startswith("runs listing malformed:")


def test_a_row_for_another_commit_is_a_reason() -> None:
    fake = FakeGh(
        run_pages=[{"total_count": 1, "workflow_runs": [run_row(head_sha="other")]}]
    )
    reason = list_runs_for_sha("o/r", _SHA, fake)
    assert isinstance(reason, str)
    assert "other" in reason


def test_the_workflow_path_is_normalised_without_its_ref() -> None:
    fake = FakeGh(
        run_pages=[
            {
                "total_count": 1,
                "workflow_runs": [run_row(path=".github/workflows/b.yml@refs/x")],
            }
        ]
    )
    runs = list_runs_for_sha("o/r", _SHA, fake)
    assert isinstance(runs, list)
    assert runs[0].path == ".github/workflows/b.yml"


@pytest.mark.parametrize(
    "error",
    [
        GhError("gh api ... timed out after 30.0s"),
        GhError("gh api could not start: no such file"),
        GhError("gh api ... failed: HTTP 502 (HTTP 502)", 502),
    ],
)
def test_a_gh_error_on_the_listing_is_a_reason_not_an_exception(
    error: GhError,
) -> None:
    fake = FakeGh(runs_error=error)
    assert list_runs_for_sha("o/r", _SHA, fake) == str(error)


def test_an_unparseable_listing_is_a_reason() -> None:
    class Garbage:
        def api(self, argv: list[str], *, timeout: float) -> str:
            return "<html>not json</html>"

    reason = list_runs_for_sha("o/r", _SHA, Garbage())
    assert isinstance(reason, str)
    assert reason.startswith("runs listing unparseable:")


# --- read_attempt -------------------------------------------------------------


def test_a_successful_attempt_reads_all_jobs_with_their_logs() -> None:
    fake = FakeGh()
    read = read_attempt("o/r", summary(), 1, fake)
    assert isinstance(read, AttemptRead)
    assert read.error is None
    assert read.run == summary()
    assert (read.attempt, read.status, read.conclusion) == (1, "completed", "success")
    assert read.jobs is not None
    assert [j.job_id for j in read.jobs] == [1, 2]
    assert read.logs_state == {1: "present", 2: "present"}
    first = read.jobs[0]
    # No green filter on jobs; `steps` keeps only non-green steps as ever,
    # `all_steps` keeps them all.
    assert first.steps == []
    assert first.all_steps is not None
    assert [s.name for s in first.all_steps] == ["Checkout", "docker build ."]
    bound = [e.text for e in first.evidence]
    assert any("#5 DONE 0.1s" in text for text in bound)
    assert "repos/o/r/actions/runs/7/attempts/1" in fake.paths
    assert any(p.endswith("/attempts/1/jobs?per_page=100&page=1") for p in fake.paths)


def test_a_failed_attempt_keeps_its_failed_steps() -> None:
    fake = FakeGh(
        attempt={"status": "completed", "conclusion": "failure"},
        jobs=[job(1), job(2, "failure")],
    )
    read = read_attempt("o/r", summary(), 2, fake)
    assert read.conclusion == "failure"
    assert read.jobs is not None
    assert [s.name for s in read.jobs[1].steps] == ["docker build ."]


def test_an_expired_log_is_unavailable_per_job() -> None:
    fake = FakeGh(logs={2: GhError("gone (HTTP 410)", 410)})
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error is None
    assert read.logs_state == {1: "present", 2: "unavailable"}
    assert read.jobs is not None
    assert read.jobs[1].completeness.logs == "unavailable"


def test_another_http_status_on_a_log_is_error_per_job() -> None:
    fake = FakeGh(logs={1: GhError("boom (HTTP 500)", 500)})
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error is None
    assert read.logs_state == {1: "error", 2: "present"}


def test_a_status_less_error_on_a_log_is_recorded_not_raised() -> None:
    fake = FakeGh(logs={2: GhError("gh api ... timed out after 30.0s")})
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error == "gh api ... timed out after 30.0s"
    assert read.jobs is None


@pytest.mark.parametrize(
    "error",
    [
        GhError("gh api ... timed out after 30.0s"),
        GhError("gh api could not start: no such file"),
        GhError("not found (HTTP 404)", 404),
    ],
)
def test_a_gh_error_on_the_attempt_metadata_is_recorded_not_raised(
    error: GhError,
) -> None:
    fake = FakeGh(attempt_error=error)
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error == str(error)
    assert read.jobs is None
    assert read.status == "unknown"
    assert read.conclusion is None
    assert not any("/jobs" in p for p in fake.paths)


def test_a_gh_error_on_the_jobs_listing_is_recorded_not_raised() -> None:
    fake = FakeGh(jobs_error=GhError("gh api ... timed out after 30.0s"))
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error == "gh api ... timed out after 30.0s"
    assert read.jobs is None
    assert read.status == "completed"


def test_an_incomplete_jobs_listing_is_recorded_as_error() -> None:
    class Short(FakeGh):
        def _jobs(self, page: int) -> str:
            items = [job(1)] if page == 1 else []
            return json.dumps({"total_count": 2, "jobs": items})

    read = read_attempt("o/r", summary(), 1, Short())
    assert read.error == "jobs listing incomplete: 1 of 2"
    assert read.jobs is None


@pytest.mark.parametrize(
    "attempt",
    [
        {"conclusion": "success"},
        {"status": 3, "conclusion": "success"},
        {"status": "completed", "conclusion": 1},
    ],
)
def test_malformed_attempt_metadata_is_recorded_as_error(
    attempt: dict[str, Any],
) -> None:
    fake = FakeGh(attempt=attempt)
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error is not None
    assert read.error.startswith("attempt metadata malformed:")
    assert read.jobs is None


def test_a_malformed_job_record_is_recorded_as_error() -> None:
    fake = FakeGh(jobs=[{"name": "no id"}])
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error is not None
    assert read.error.startswith("job record malformed:")
    assert read.jobs is None


def test_unparseable_attempt_metadata_is_recorded_as_error() -> None:
    class Garbage:
        def api(self, argv: list[str], *, timeout: float) -> str:
            return "not json"

    read = read_attempt("o/r", summary(), 1, Garbage())
    assert read.error is not None
    assert read.error.startswith("attempt metadata unparseable:")


def test_logs_carry_the_fetched_text_and_omit_unread_jobs() -> None:
    """``logs`` holds the exact text ``gh.logs`` returned, keyed by job id;
    an unread log (410, another status, blank) has no key at all."""
    raw = "2026-09-25T10:00:00.1Z \x1b[1m" + _LOG
    fake = FakeGh(
        jobs=[job(1), job(2), job(3), job(4)],
        logs={
            1: raw,
            2: GhError("gone (HTTP 410)", 410),
            3: GhError("boom (HTTP 500)", 500),
            4: "  \n",
        },
    )
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.logs == {1: raw}
    assert read.logs_state == {
        1: "present",
        2: "unavailable",
        3: "error",
        4: "unavailable",
    }


def test_an_attempt_not_read_has_no_logs() -> None:
    unfinished = FakeGh(attempt={"status": "in_progress", "conclusion": None})
    assert read_attempt("o/r", summary(), 2, unfinished).logs == {}
    broken = FakeGh(jobs_error=GhError("boom (HTTP 500)", 500))
    assert read_attempt("o/r", summary(), 1, broken).logs == {}


def test_an_unfinished_attempt_records_its_status_and_reads_no_jobs() -> None:
    """Only completed attempts count; qualification marks this undetermined."""
    fake = FakeGh(attempt={"status": "in_progress", "conclusion": None})
    read = read_attempt("o/r", summary(), 2, fake)
    assert (read.status, read.conclusion) == ("in_progress", None)
    assert read.jobs is None
    assert read.error is None
    assert read.logs_state == {}
    assert not any("/jobs" in p for p in fake.paths)


# --- attempt identity against the RunSummary ----------------------------------


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {"head_sha": "other"},
            f"attempt metadata mismatch: head_sha other, not {_SHA}",
        ),
        ({"head_sha": None}, "attempt metadata malformed: head_sha=None"),
        ({"head_sha": 5}, "attempt metadata malformed: head_sha=5"),
        ({"run_attempt": 2}, "attempt metadata mismatch: run_attempt 2, not 1"),
        ({"run_attempt": "1"}, "attempt metadata malformed: run_attempt='1'"),
        ({"run_attempt": True}, "attempt metadata malformed: run_attempt=True"),
        ({"id": 8}, "attempt metadata mismatch: id 8, not 7"),
        ({"id": "7"}, "attempt metadata malformed: id='7'"),
        ({"id": None}, "attempt metadata malformed: id=None"),
    ],
)
def test_attempt_identity_must_match_the_run_summary(
    override: dict[str, Any], expected: str
) -> None:
    fake = FakeGh(attempt={"status": "completed", "conclusion": "success", **override})
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error == expected
    assert read.jobs is None
    assert (read.status, read.conclusion) == ("unknown", None)
    assert not any("/jobs" in p for p in fake.paths)


@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        ("head_sha", "attempt metadata malformed: head_sha=None"),
        ("run_attempt", "attempt metadata malformed: run_attempt=None"),
    ],
)
def test_a_missing_identity_field_is_an_error(dropped: str, expected: str) -> None:
    fake = FakeGh(attempt_drop=(dropped,))
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error == expected
    assert read.jobs is None


def test_an_absent_run_id_is_accepted() -> None:
    fake = FakeGh(attempt_drop=("id",))
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error is None
    assert read.jobs is not None


def test_an_unfinished_attempt_is_identity_checked_too() -> None:
    fake = FakeGh(attempt={"status": "in_progress", "conclusion": None, "id": 9})
    read = read_attempt("o/r", summary(), 1, fake)
    assert read.error == "attempt metadata mismatch: id 9, not 7"


def test_non_object_attempt_metadata_is_malformed() -> None:
    class ListGh:
        def api(self, argv: list[str], *, timeout: float) -> str:
            return "[]"

    read = read_attempt("o/r", summary(), 1, ListGh())
    assert read.error == "attempt metadata malformed: not an object (list)"
