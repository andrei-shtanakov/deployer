"""Dev tool: record real CI runs on the polygon for the CI template rows
(F §7.3, §9, §11 stage 4). Owner-permitted real runs; the output is data for
the owner's review.

Run from the repository root (needs ``gh`` logged in with push rights)::

    uv run python tests/fixtures/recordings/record_ci.py --all
    uv run python tests/fixtures/recordings/record_ci.py --case c3-from-parsed

Per case:

1. an **orphan** commit (like ``polygon/run-*``) holding the project files of
   ``polygon/run-5`` (``937d465``), the case's ``Dockerfile`` and added files,
   and a ``workflow_dispatch``-only ``.github/workflows/diagnosis-polygon.yml``
   whose one job checks out and runs one ``docker build`` line R's
   ``parse_build_line`` accepts. It is written with plumbing into a temporary
   index — the checkout and ``master`` are never touched;
2. pushed to ``polygon/fix-c-<case>``; a branch that already exists is refused;
3. the SHA check (diagnosis spec §6.3): the SHA must not be the head of any
   open PR;
4. ``gh workflow run diagnosis-polygon.yml --ref polygon/fix-c-<case>``, then
   polling until the run is completed; ``c2-copy-rerun`` then re-runs ``c1``'s
   run (``gh run rerun``) and waits for attempt 2;
5. ``forge.list_runs_for_sha`` and ``forge.read_attempt`` for every attempt
   through a runner that records every ``gh api`` call verbatim into
   ``gh-calls.json`` — the replay serves those bytes back to forge's real
   parsing.

A case directory that already exists is refused. Nothing is trimmed, edited
or redacted.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deployer import forge

HERE = Path(__file__).resolve().parent
CI = HERE / "ci"
REPO = "andrei-shtanakov/deployer"
PROJECT_COMMIT = "937d465"
PROJECT_FILES = ("pyproject.toml", "uv.lock", "src", "tests")
CHECKOUT = "actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
WORKFLOW_PATH = ".github/workflows/diagnosis-polygon.yml"
BUILD_LINE = "docker build --file ./Dockerfile ."
POLL_SECONDS = 15
WAIT_SECONDS = 1800

_HEAD = (
    "FROM python:3.12-slim\n\n"
    "COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/\n\n"
    "WORKDIR /app\n\n"
    "COPY pyproject.toml uv.lock ./\n"
    "RUN uv sync --frozen --no-install-project\n\n"
    "COPY src/ci_build ./src/ci_build\n"
)
_TAIL = (
    "\nRUN uv sync --frozen\n\n"
    "RUN useradd --create-home appuser\n"
    "USER appuser\n\n"
    'ENV PATH="/app/.venv/bin:$PATH"\n\n'
    'CMD ["python"]\n'
)
COPY_FIXED = "COPY docs/guide/setup.md ./setup.md"
FROM_FIXED = "FROM python:3.12-slim AS extra"
FROM_BAD = "FROM python:3.12-slim extra"
SETUP_MD = "# Setup\n\nDerived recording input (not historical).\n"
COPY_LINE = 11  # the corrected COPY's line in _HEAD + COPY + _TAIL


@dataclass(frozen=True)
class Check:
    """One question asked of a recording: a kind, the corrected text, lines."""

    kind: str
    corrected: str
    lines: tuple[int, int]


@dataclass(frozen=True)
class Case:
    """A polygon recording: Dockerfile, added files, build steps, checks."""

    name: str
    dockerfile: str
    checks: tuple[Check, ...]
    added: dict[str, str] = field(default_factory=dict)
    builds: int = 1
    rerun_of: str | None = None
    reread_of: str | None = None


def _workflow(builds: int) -> str:
    steps = "".join(f"      - run: {BUILD_LINE}\n" for _ in range(builds))
    return (
        "name: build-image\n"
        "on:\n  workflow_dispatch:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
        f"      - uses: {CHECKOUT}\n{steps}"
    )


def _cases() -> tuple[Case, ...]:
    setup = {"docs/guide/setup.md": SETUP_MD}
    copy_df = _HEAD + f"{COPY_FIXED}\n" + _TAIL
    copy_check = Check("copy", COPY_FIXED, (COPY_LINE, COPY_LINE))
    return (
        Case("c1-copy-done", copy_df, (copy_check,), setup),
        Case("c2-copy-rerun", copy_df, (copy_check,), rerun_of="c1-copy-done"),
        Case(
            "c3-from-parsed",
            _HEAD.replace("FROM python:3.12-slim\n", f"{FROM_FIXED}\n", 1) + _TAIL,
            (Check("from", FROM_FIXED, (1, 1)),),
        ),
        Case(
            "c4-copy-later-failure",
            _HEAD + f"{COPY_FIXED}\nRUN false\n" + _TAIL,
            (copy_check,),
            setup,
        ),
        # The corrected source is still absent: the defect recurs at the span.
        Case("c5-copy-recurred", copy_df, (copy_check,)),
        # Two builds in one job, to capture the ``#k CACHED`` form.
        Case("c6-copy-cached", copy_df, (copy_check,), setup, builds=2),
        # c2's read of attempt 2's log hit a transient TLS timeout (kept as
        # recorded); this reads the same run again, with no new run.
        Case(
            "c2b-copy-rerun-reread", copy_df, (copy_check,), reread_of="c2-copy-rerun"
        ),
        # Ten or more steps: c4 showed BuildKit padding the step number
        # (``[stage-0  7/10]``); this is the same padding on a passing build.
        Case(
            "c7-copy-done-padded",
            copy_df + 'LABEL recording="padded"\n',
            (copy_check,),
            setup,
        ),
        # c7's LABEL turned out not to be a build step (still 9 steps, no
        # padding); a RUN is, so this build has 10 steps and padded numbers.
        Case(
            "c9-copy-done-padded-run",
            copy_df + "RUN true\n",
            (copy_check,),
            setup,
        ),
        # A bad FROM in a stage nothing depends on (local l9 builds it with
        # exit 0): does BuildKit parse the whole file before building?
        Case(
            "c8-from-bad-in-skipped-stage",
            f"{FROM_BAD}\nRUN true\n\nFROM python:3.12-slim AS final\nRUN true\n",
            (Check("from", FROM_BAD, (1, 1)),),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Record the requested cases; refuse any that already exist."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--case", action="append")
    args = parser.parse_args(argv)
    wanted = [c for c in _cases() if args.all or c.name in (args.case or [])]
    existing = [c.name for c in wanted if (CI / c.name).exists()]
    if existing:
        print(f"refusing: already recorded: {existing}", file=sys.stderr)
        return 2
    for case in wanted:
        if case.reread_of is not None:
            _record_reread(case)
        elif case.rerun_of is not None:
            _record_rerun(case)
        else:
            _record_new(case)
    return 0


def _record_new(case: Case) -> None:
    """Commit, push, check, dispatch, wait and record one case."""
    root = CI / case.name
    tree = root / "tree"
    _materialise(case, tree)
    branch = f"polygon/fix-c-{case.name}"
    sha = _commit(tree, f"polygon: fix-c recording {case.name}")
    _push(sha, branch)
    _sha_check(sha)
    _gh(["workflow", "run", Path(WORKFLOW_PATH).name, "--repo", REPO, "--ref", branch])
    run_id = _wait(sha, attempts=1)
    _write_case(case, root, sha, branch, run_id)


def _record_rerun(case: Case) -> None:
    """Re-run the source case's run and record both attempts."""
    assert case.rerun_of is not None
    source = json.loads((CI / case.rerun_of / "environment.json").read_text())
    sha, branch, run_id = source["sha"], source["branch"], source["run_id"]
    root = CI / case.name
    _copy_tree(CI / case.rerun_of / "tree", root / "tree")
    _sha_check(sha)
    _gh(["run", "rerun", str(run_id), "--repo", REPO])
    _wait(sha, attempts=2)
    _write_case(case, root, sha, branch, run_id)


def _record_reread(case: Case) -> None:
    """Read the source case's run again: no dispatch, no re-run."""
    assert case.reread_of is not None
    source = json.loads((CI / case.reread_of / "environment.json").read_text())
    root = CI / case.name
    _copy_tree(CI / case.reread_of / "tree", root / "tree")
    _write_case(case, root, source["sha"], source["branch"], source["run_id"])


def _write_case(case: Case, root: Path, sha: str, branch: str, run_id: int) -> None:
    """Read the runs through forge with a recording runner; write the case."""
    recorder = _RecordingGh(forge.SubprocessGh())
    runs = forge.list_runs_for_sha(REPO, sha, recorder)
    if isinstance(runs, str):
        raise SystemExit(f"{case.name}: listing failed: {runs}")
    for run in runs:
        for attempt in range(1, run.attempts + 1):
            forge.read_attempt(REPO, run, attempt, recorder)
    _json(root / "gh-calls.json", recorder.calls)
    _json(
        root / "checks.json",
        [
            {"kind": c.kind, "corrected": c.corrected, "lines": list(c.lines)}
            for c in case.checks
        ],
    )
    _json(
        root / "environment.json",
        {
            "repo": REPO,
            "branch": branch,
            "sha": sha,
            "run_id": run_id,
            "run_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "workflow_path": WORKFLOW_PATH,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    print(case.name, sha[:7], run_id)


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


def _materialise(case: Case, tree: Path) -> None:
    tree.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", PROJECT_COMMIT, *PROJECT_FILES],
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    for rel, text in case.added.items():
        _write(tree / rel, text)
    _write(tree / "Dockerfile", case.dockerfile)
    _write(tree / WORKFLOW_PATH, _workflow(case.builds))


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


def _push(sha: str, branch: str) -> None:
    if not branch.startswith("polygon/"):
        raise SystemExit(f"refusing to push outside polygon/: {branch}")
    remote = _git(["ls-remote", "--heads", "origin", branch], None)
    if remote.strip():
        raise SystemExit(f"refusing: {branch} already exists on origin")
    _git(["push", "origin", f"{sha}:refs/heads/{branch}"], None)


def _sha_check(sha: str) -> None:
    """Diagnosis spec §6.3: the SHA must not head any open PR."""
    heads = _gh(
        ["pr", "list", "--repo", REPO, "--state", "open", "--json", "headRefOid"]
    )
    if any(row["headRefOid"] == sha for row in json.loads(heads)):
        raise SystemExit(f"refusing to dispatch: {sha} heads an open PR")


def _wait(sha: str, attempts: int) -> int:
    """Poll until one run for ``sha`` reached ``attempts`` and completed."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        rows = json.loads(_gh(["api", f"repos/{REPO}/actions/runs?head_sha={sha}"]))[
            "workflow_runs"
        ]
        for row in rows:
            done = row["status"] == "completed"
            if done and row["run_attempt"] >= attempts:
                return int(row["id"])
    raise SystemExit(f"timed out waiting for the run of {sha}")


def _copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


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
