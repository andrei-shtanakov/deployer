"""The ``steps-2`` recording: real cancelled matrix siblings (PROVENANCE.md).

The shape facts are re-read from the raw data, not through forge. The replay
test pins what forge and diagnose do with it now that never-started siblings
are recognised (forge-not-executed-jobs spec). ``never-starts`` still yields a
snapshot and an ``error`` log (#116), but it is recognised as cancelled before
execution: its binding is ``excluded`` and the four jobs that ran are ``bound``;
diagnose gives it no verdict, exempts its expected 404, and the run reads
``UNCLASSIFIED`` with one verdict per failure of the jobs that ran.
"""

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from deployer.diagnose import diagnose_run
from deployer.forge import (
    FailedRun,
    GhError,
    OverCap,
    RunRef,
    StepBinding,
    fetch_failed_run,
)

CASE = Path(__file__).parent / "fixtures" / "step-binding" / "steps-2"
_LOGS_RE = re.compile(r"actions/jobs/(\d+)/logs$")


def _calls() -> list[dict[str, Any]]:
    return json.loads((CASE / "gh-calls.json").read_text())


def _jobs() -> dict[str, dict[str, Any]]:
    return {
        job["name"]: job
        for call in _calls()
        if "/jobs?" in call["argv"][-1] and "stdout" in call
        for job in json.loads(call["stdout"])["jobs"]
    }


def test_the_never_started_sibling_has_no_runner_steps_log_or_archive_entry() -> None:
    job = _jobs()["waiting-legs (never-starts)"]
    assert job["conclusion"] == "cancelled"
    assert (job["runner_id"], job["runner_name"], job["steps"]) == (0, "", [])
    assert job["started_at"] == job["created_at"]
    (read,) = [
        c
        for c in _calls()
        if (m := _LOGS_RE.search(c["argv"][-1])) and int(m.group(1)) == job["id"]
    ]
    assert read["status"] == 404
    assert read["error"].endswith("gh: HTTP 404")
    with zipfile.ZipFile(CASE / "attempt-1.zip") as archive:
        assert not [n for n in archive.namelist() if "never-starts" in n]


def test_the_siblings_cancelled_mid_execution_ran() -> None:
    jobs = _jobs()
    for name in ("parallel-legs (long-1)", "parallel-legs (long-2)"):
        job = jobs[name]
        assert job["conclusion"] == "cancelled"
        assert job["runner_id"] != 0 and job["steps"]
        assert job["started_at"] != job["created_at"]


class _Replay:
    """Serves the recorded calls (a recorded error is re-raised with its status),
    annotations as ``[]`` (not recorded), and the recorded archive."""

    def __init__(self) -> None:
        self._by_path = {c["argv"][-1]: c for c in _calls()}
        self._archive = (CASE / "attempt-1.zip").read_bytes()

    def api(self, argv: list[str], *, timeout: float) -> str:
        path = argv[-1]
        if "/check-runs/" in path:
            return "[]"
        call = self._by_path[path]
        if "error" in call:
            raise GhError(call["error"], call["status"])
        return call["stdout"]

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        return self._archive


def test_forge_and_diagnose_today_on_steps_2() -> None:
    env = json.loads((CASE / "environment.json").read_text())
    run = fetch_failed_run(
        RunRef(env["repo"], env["run_id"]), attempt=1, runner=_Replay()
    )
    assert isinstance(run, FailedRun)
    logs = {j.name: j.completeness.logs for j in run.jobs}
    assert logs.pop("waiting-legs (never-starts)") == "error"
    assert set(logs.values()) == {"present"}
    bindings = {j.name: j.step_binding for j in run.jobs}
    assert bindings.pop("waiting-legs (never-starts)") == StepBinding(
        "excluded", "cancelled before execution"
    )
    assert len(bindings) == 4
    assert all(b is not None and b.state == "bound" for b in bindings.values())
    diagnosis = diagnose_run(run)
    assert len(diagnosis.failures) == 4
    assert diagnosis.outcome == "UNCLASSIFIED"
