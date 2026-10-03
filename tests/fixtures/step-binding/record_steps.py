"""Dev tool: record one real polygon run for step-level log binding.

Owner-permitted (2026-10-03): one push of ``polygon/steps-1`` and one
dispatch. The output is data for review; no production parser reads it yet.

Run from the repository root (needs ``gh`` logged in with push rights)::

    uv run python tests/fixtures/step-binding/record_steps.py prepare
    uv run python tests/fixtures/step-binding/record_steps.py record

``prepare`` writes ``steps-1/tree`` (the workflow and the composite action)
and ``steps-1/expected.json``: which workflow step each marker and each
diagnostic line belongs to, fixed BEFORE the run and before any parser
exists. It is committed on its own so the history shows it predates the data.

``record``:

1. an **orphan** commit of ``steps-1/tree`` through a temporary index
   (plumbing; the checkout and ``master`` are never touched);
2. pushed to ``polygon/steps-1``; a branch that already exists is refused;
3. the SHA must not head any open PR (diagnosis spec §6.3);
4. ``gh workflow run diagnosis-polygon.yml --ref polygon/steps-1``, then
   polling until the run completed;
5. ``forge.list_runs_for_sha`` and ``forge.read_attempt`` through a runner that
   records every ``gh api`` call verbatim into ``gh-calls.json``; the
   per-attempt log archive is saved once, as downloaded, to ``attempt-1.zip``.

Markers are assembled at run time (``printf '%s-%s\\n' MARK s1-a``), so the
full marker line never appears in the command the runner echoes into the
step's header: an exact-line match finds the output, never the script.
Nothing recorded is trimmed, edited or redacted.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from deployer import forge

HERE = Path(__file__).resolve().parent
CASE = HERE / "steps-1"
REPO = "andrei-shtanakov/deployer"
BRANCH = "polygon/steps-1"
CHECKOUT = "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
WORKFLOW_PATH = ".github/workflows/diagnosis-polygon.yml"
ACTION_PATH = ".github/actions/probe/action.yml"
POLL_SECONDS = 15
WAIT_SECONDS = 1800


def _mark(tag: str) -> str:
    """A step line that prints ``MARK-<tag>`` without spelling it out."""
    return f"printf '%s-%s\\n' MARK {tag}"


def _assertion(tag: str) -> str:
    """A step line that prints ``AssertionError: probe-<tag>`` (a diagnose
    shape) without the line itself appearing in the echoed command."""
    return f"printf '%s: %s\\n' AssertionError probe-{tag}"


EXEC_FORMAT = "printf 'exec %s error\\n' format"
"""Prints ``exec format error`` (a second, distinct diagnose shape)."""


def _run(lines: Sequence[str], *, name: str | None = None, cond: str = "") -> str:
    """One ``run:`` step as YAML, a block scalar so no quoting is needed."""
    head = f"      - name: {name}\n        run: |\n" if name else "      - run: |\n"
    if cond:
        head = head.replace("run: |", f"if: {cond}\n        run: |", 1)
    return head + "".join(f"          {line}\n" for line in lines)


def _job(key: str, steps: Sequence[str]) -> str:
    body = "".join(steps)
    return (
        f"  {key}:\n    runs-on: ubuntu-24.04\n    steps:\n"
        f"      - uses: {CHECKOUT}\n{body}"
    )


JOBS: dict[str, list[str]] = {
    "s1-plain-fail": [
        _run([_mark("s1-a")]),
        _run([_mark("s1-b"), _assertion("s1"), "exit 3"]),
    ],
    "s2-named": [
        _run([_mark("s2-a")]),
        _run([_mark("s2-b"), _assertion("s2"), "exit 1"], name="Custom label s2"),
        _run([_mark("s2-c")], name="Named after s2", cond="always()"),
    ],
    "s3-two-failures": [
        _run([_mark("s3-a"), _assertion("s3"), "exit 1"]),
        _run([_mark("s3-b"), EXEC_FORMAT, "exit 2"], cond="always()"),
    ],
    "s4-composite": [
        _run([_mark("s4-before")]),
        "      - uses: ./.github/actions/probe\n",
        _run([_mark("s4-after")], cond="always()"),
    ],
    "s5-spoof": [
        _run(
            [
                "echo \"::group::Run printf '%s-%s\\n' MARK s5-next\"",
                _mark("s5-spoof"),
                'echo "::endgroup::"',
                _assertion("s5"),
                "exit 1",
            ]
        ),
        _run([_mark("s5-next")], cond="always()"),
    ],
    "s6-dupes": [
        _run([_mark("s6-a")], name="Same name s6"),
        _run([_mark("s6-b"), _assertion("s6"), "exit 1"], name="Same name s6"),
    ],
}

ACTION = (
    "name: probe\n"
    "description: composite probe for step binding\n"
    "runs:\n  using: composite\n  steps:\n"
    "    - shell: bash\n      run: |\n"
    '        echo "::group::inner group s4"\n'
    f"        {_mark('s4-inner-1')}\n"
    '        echo "::endgroup::"\n'
    "    - shell: bash\n      run: |\n"
    f"        {_mark('s4-inner-2')}\n"
    f"        {_assertion('s4')}\n"
    "        exit 1\n"
)

EXPECTED = {
    "note": (
        "Pre-registered before the run: the owner of each exact output line, "
        "as (job key, 0-based index among the job's workflow steps AFTER the "
        "checkout). Composite inner lines belong to the outer API step. "
        "'fallback_ok' marks lines where job-level is an acceptable answer "
        "and only a binding to another step is wrong."
    ),
    "lines": [
        {"job": "s1-plain-fail", "step": 0, "line": "MARK-s1-a"},
        {"job": "s1-plain-fail", "step": 1, "line": "MARK-s1-b"},
        {"job": "s1-plain-fail", "step": 1, "line": "AssertionError: probe-s1"},
        {"job": "s2-named", "step": 0, "line": "MARK-s2-a"},
        {"job": "s2-named", "step": 1, "line": "MARK-s2-b"},
        {"job": "s2-named", "step": 1, "line": "AssertionError: probe-s2"},
        {"job": "s2-named", "step": 2, "line": "MARK-s2-c"},
        {"job": "s3-two-failures", "step": 0, "line": "MARK-s3-a"},
        {"job": "s3-two-failures", "step": 0, "line": "AssertionError: probe-s3"},
        {"job": "s3-two-failures", "step": 1, "line": "MARK-s3-b"},
        {"job": "s3-two-failures", "step": 1, "line": "exec format error"},
        {"job": "s4-composite", "step": 0, "line": "MARK-s4-before"},
        {
            "job": "s4-composite",
            "step": 1,
            "line": "MARK-s4-inner-1",
            "fallback_ok": True,
        },
        {
            "job": "s4-composite",
            "step": 1,
            "line": "MARK-s4-inner-2",
            "fallback_ok": True,
        },
        {
            "job": "s4-composite",
            "step": 1,
            "line": "AssertionError: probe-s4",
            "fallback_ok": True,
        },
        {"job": "s4-composite", "step": 2, "line": "MARK-s4-after"},
        {"job": "s5-spoof", "step": 0, "line": "MARK-s5-spoof"},
        {"job": "s5-spoof", "step": 0, "line": "AssertionError: probe-s5"},
        {"job": "s5-spoof", "step": 1, "line": "MARK-s5-next"},
        {"job": "s6-dupes", "step": 0, "line": "MARK-s6-a", "fallback_ok": True},
        {"job": "s6-dupes", "step": 1, "line": "MARK-s6-b", "fallback_ok": True},
        {
            "job": "s6-dupes",
            "step": 1,
            "line": "AssertionError: probe-s6",
            "fallback_ok": True,
        },
    ],
    "diagnose": {
        "s3-two-failures": {
            "step 0 cites": "AssertionError: probe-s3",
            "step 0 must not cite": "exec format error",
            "step 1 cites": "exec format error",
            "step 1 must not cite": "AssertionError: probe-s3",
        }
    },
}


def workflow() -> str:
    """The ``workflow_dispatch``-only polygon workflow with every case job."""
    jobs = "".join(_job(key, steps) for key, steps in JOBS.items())
    return f"name: step-binding\non:\n  workflow_dispatch:\njobs:\n{jobs}"


def main(argv: Sequence[str] | None = None) -> int:
    """``prepare`` or ``record``; each refuses to overwrite what exists."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "record"))
    args = parser.parse_args(argv)
    if args.phase == "prepare":
        return _prepare()
    return _record()


def _prepare() -> int:
    if CASE.exists():
        print(f"refusing: {CASE} exists", file=sys.stderr)
        return 2
    _write(CASE / "tree" / WORKFLOW_PATH, workflow())
    _write(CASE / "tree" / ACTION_PATH, ACTION)
    _json(CASE / "expected.json", EXPECTED)
    return 0


def _record() -> int:
    if (CASE / "gh-calls.json").exists():
        print("refusing: already recorded", file=sys.stderr)
        return 2
    if _write_bytes_differ(CASE / "tree" / WORKFLOW_PATH, workflow()):
        print("refusing: tree/ differs from the prepared workflow", file=sys.stderr)
        return 2
    sha = _commit(CASE / "tree", "polygon: step-binding recording steps-1")
    _push(sha)
    _sha_check(sha)
    _gh(["workflow", "run", Path(WORKFLOW_PATH).name, "--repo", REPO, "--ref", BRANCH])
    run_id = _wait(sha)
    recorder = _RecordingGh(forge.SubprocessGh())
    runs = forge.list_runs_for_sha(REPO, sha, recorder)
    if isinstance(runs, str):
        raise SystemExit(f"listing failed: {runs}")
    for run in runs:
        for attempt in range(1, run.attempts + 1):
            forge.read_attempt(REPO, run, attempt, recorder)
    _json(CASE / "gh-calls.json", recorder.calls)
    archive = forge.SubprocessGh().api_bytes(
        [f"repos/{REPO}/actions/runs/{run_id}/attempts/1/logs"],
        timeout=forge.ARCHIVE_TIMEOUT_S,
    )
    (CASE / "attempt-1.zip").write_bytes(archive)
    _json(
        CASE / "environment.json",
        {
            "repo": REPO,
            "branch": BRANCH,
            "sha": sha,
            "run_id": run_id,
            "run_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "workflow_path": WORKFLOW_PATH,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    print(sha[:7], run_id)
    return 0


class _RecordingGh:
    """A ``GhRunner`` that records every call and its stdout (or error)."""

    def __init__(self, inner: forge.SubprocessGh) -> None:
        self.inner = inner
        self.calls: list[dict[str, object]] = []

    def api(self, argv: list[str], *, timeout: float) -> str:
        try:
            out = self.inner.api(argv, timeout=timeout)
        except forge.GhError as exc:
            self.calls.append({"argv": argv, "error": str(exc), "status": exc.status})
            raise
        self.calls.append({"argv": argv, "stdout": out})
        return out


def _write_bytes_differ(path: Path, text: str) -> bool:
    return not path.is_file() or path.read_bytes() != text.encode()


def _commit(tree: Path, message: str) -> str:
    """An orphan commit of ``tree`` through a temporary index (plumbing)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for path in sorted(p for p in tree.rglob("*") if p.is_file()):
            blob = _git(["hash-object", "-w", str(path)], env).strip()
            rel = path.relative_to(tree).as_posix()
            _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}"], env)
        tree_sha = _git(["write-tree"], env).strip()
        return _git(["commit-tree", tree_sha, "-m", message], env).strip()


def _push(sha: str) -> None:
    if _git(["ls-remote", "--heads", "origin", BRANCH], None).strip():
        raise SystemExit(f"refusing: {BRANCH} already exists on origin")
    _git(["push", "origin", f"{sha}:refs/heads/{BRANCH}"], None)


def _sha_check(sha: str) -> None:
    """Diagnosis spec §6.3: the SHA must not head any open PR."""
    heads = _gh(
        ["pr", "list", "--repo", REPO, "--state", "open", "--json", "headRefOid"]
    )
    if any(row["headRefOid"] == sha for row in json.loads(heads)):
        raise SystemExit(f"refusing to dispatch: {sha} heads an open PR")


def _wait(sha: str) -> int:
    """Poll until the run for ``sha`` completed; return its id."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        rows = json.loads(_gh(["api", f"repos/{REPO}/actions/runs?head_sha={sha}"]))[
            "workflow_runs"
        ]
        for row in rows:
            if row["status"] == "completed":
                return int(row["id"])
    raise SystemExit(f"timed out waiting for the run of {sha}")


def _git(args: list[str], env: dict[str, str] | None) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env
    ).stdout


def _gh(args: list[str]) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def _json(path: Path, data: object) -> None:
    _write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
