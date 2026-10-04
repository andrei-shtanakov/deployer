"""Dev tool: record one real polygon run of fail-fast matrices (``steps-2``).

Owner-permitted (2026-10-04): one push of ``polygon/steps-2`` and one dispatch,
with no automatic repeat. The purpose is to see real shapes of cancelled
matrix siblings: a sibling cancelled mid-execution, a sibling cancelled before
it started, and what the jobs API, the job-log endpoint (``gh``'s stderr) and
the per-attempt archive say about each. This grounds the exclusion criteria of
``step-binding-never-started-jobs`` and the main-path fix of #116.

Run from the repository root (needs ``gh`` with push rights)::

    uv run python tests/fixtures/step-binding/record_steps2.py prepare
    uv run python tests/fixtures/step-binding/record_steps2.py record

``prepare`` writes ``steps-2/tree`` and ``steps-2/expected.json``, the
pre-registered hypotheses, which are committed before the run. ``record``
reuses ``record_steps``' plumbing: an orphan commit, the push (a branch that
already exists is refused), the open-PR SHA check, one dispatch, then waiting.
It then reads the run through ``forge.list_runs_for_sha`` and
``forge.read_attempt`` with a runner that records every ``gh api`` call
verbatim (an error is recorded as its message and status, so ``gh``'s exact
stderr is kept). The archive is downloaded as soon as the run completes.
Nothing is trimmed or edited.
"""

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from record_steps import (
    CHECKOUT,
    REPO,
    WORKFLOW_PATH,
    _commit,
    _gh,
    _git,
    _json,
    _RecordingGh,
    _sha_check,
    _wait,
    _write,
    _write_bytes_differ,
)

from deployer import forge

HERE = Path(__file__).resolve().parent
CASE = HERE / "steps-2"
BRANCH = "polygon/steps-2"


def _mark_step(leg_rule: str) -> str:
    """The one run step: print the leg's marker, then the leg's rule."""
    return (
        "      - run: |\n"
        "          printf '%s-%s\\n' MARK \"${{ matrix.leg }}\"\n"
        f"          {leg_rule}\n"
    )


def workflow() -> str:
    """Two independent fail-fast matrices, each job capped at 5 minutes."""
    return (
        "name: step-binding-2\n"
        "on:\n  workflow_dispatch:\n"
        "jobs:\n"
        "  parallel-legs:\n"
        "    timeout-minutes: 5\n"
        "    strategy:\n      fail-fast: true\n"
        "      matrix:\n        leg: [fail-fast, long-1, long-2]\n"
        "    runs-on: ubuntu-24.04\n    steps:\n"
        f"      - uses: {CHECKOUT}\n"
        + _mark_step(
            'if [ "${{ matrix.leg }}" = fail-fast ]; then sleep 20; exit 1; fi; sleep 240'
        )
        + "  waiting-legs:\n"
        "    timeout-minutes: 5\n"
        "    strategy:\n      fail-fast: true\n      max-parallel: 1\n"
        "      matrix:\n        leg: [first-fails, never-starts]\n"
        "    runs-on: ubuntu-24.04\n    steps:\n"
        f"      - uses: {CHECKOUT}\n"
        + _mark_step(
            'if [ "${{ matrix.leg }}" = first-fails ]; then exit 1; fi; sleep 240'
        )
    )


EXPECTED = {
    "note": (
        "Pre-registered HYPOTHESES, written before the run. They are not "
        "guarantees, and whatever the run returns is recorded unchanged; a "
        "shape that does not appear is a result too."
    ),
    "caveats": [
        "The 20-second delay of parallel-legs/fail-fast does not guarantee "
        "that long-1 and long-2 have started executing before fail-fast "
        "cancels them.",
        "max-parallel: 1 limits parallelism; it does not prove which leg of "
        "waiting-legs starts first.",
    ],
    "hypotheses": [
        {
            "job": "parallel-legs (fail-fast)",
            "conclusion": "failure",
            "shape": "ran: a runner, steps, a log, an archive entry",
        },
        {
            "job": "parallel-legs (long-1), parallel-legs (long-2)",
            "conclusion": "cancelled",
            "shape": "cancelled mid-execution: a runner, steps, a log",
        },
        {
            "job": "waiting-legs (first-fails)",
            "conclusion": "failure",
            "shape": "ran",
        },
        {
            "job": "waiting-legs (never-starts)",
            "conclusion": "cancelled",
            "shape": (
                "never started: runner_id 0, runner_name '', no steps, "
                "started_at == created_at, the job log a bare 'gh: HTTP 404', "
                "no archive entry (an observation only, never proof)"
            ),
        },
    ],
}


def main(argv: Sequence[str] | None = None) -> int:
    """``prepare`` or ``record``; each refuses to overwrite what exists."""
    phase = (list(argv) if argv is not None else sys.argv[1:])[:1]
    if phase == ["prepare"]:
        return _prepare()
    if phase == ["record"]:
        return _record()
    print("usage: record_steps2.py prepare|record", file=sys.stderr)
    return 2


def _prepare() -> int:
    if CASE.exists():
        print(f"refusing: {CASE} exists", file=sys.stderr)
        return 2
    _write(CASE / "tree" / WORKFLOW_PATH, workflow())
    _json(CASE / "expected.json", EXPECTED)
    return 0


def _record() -> int:
    if (CASE / "gh-calls.json").exists():
        print("refusing: already recorded", file=sys.stderr)
        return 2
    if _write_bytes_differ(CASE / "tree" / WORKFLOW_PATH, workflow()):
        print("refusing: tree/ differs from the prepared workflow", file=sys.stderr)
        return 2
    sha = _commit(CASE / "tree", "polygon: step-binding recording steps-2")
    if _git(["ls-remote", "--heads", "origin", BRANCH], None).strip():
        raise SystemExit(f"refusing: {BRANCH} already exists on origin")
    _git(["push", "origin", f"{sha}:refs/heads/{BRANCH}"], None)
    _sha_check(sha)
    _gh(["workflow", "run", Path(WORKFLOW_PATH).name, "--repo", REPO, "--ref", BRANCH])
    run_id = _wait(sha)
    archive = forge.SubprocessGh().api_bytes(
        [f"repos/{REPO}/actions/runs/{run_id}/attempts/1/logs"],
        timeout=forge.ARCHIVE_TIMEOUT_S,
    )
    (CASE / "attempt-1.zip").write_bytes(archive)
    downloaded = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    recorder = _RecordingGh(forge.SubprocessGh())
    runs = forge.list_runs_for_sha(REPO, sha, recorder)
    if isinstance(runs, str):
        raise SystemExit(f"listing failed: {runs}")
    for run in runs:
        for attempt in range(1, run.attempts + 1):
            forge.read_attempt(REPO, run, attempt, recorder)
    _json(CASE / "gh-calls.json", recorder.calls)
    _json(
        CASE / "environment.json",
        {
            "repo": REPO,
            "branch": BRANCH,
            "sha": sha,
            "run_id": run_id,
            "run_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "workflow_path": WORKFLOW_PATH,
            "archive_downloaded_at": downloaded,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    print(sha[:7], run_id, json.dumps({"archive_bytes": len(archive)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
