"""Dev tool: build a reproduction bundle from a real run (spec §8.A).

    uv run python tests/fixtures/reproduction/make_bundle.py snapshot \
        --run-id 35680991093 --out tests/fixtures/reproduction/run-1
    uv run python tests/fixtures/reproduction/make_bundle.py tree \
        --ref refs/keep/polygon-run-1 --out tests/fixtures/reproduction/run-1

Read-only: the snapshot is re-fetched through forge (no dispatch); the tree
and its listing come from local git objects of the pinned commit.
"""

import argparse
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

from deployer.forge import FailedRun, RunRef, dump_snapshot, fetch_failed_run

REPO = "andrei-shtanakov/deployer"
ANON = "example/project"
PATHS = ("/home/runner/work/deployer/deployer", "/home/runner/work/project/project")


def snapshot(run_id: int, out: Path) -> None:
    """Re-fetch the run read-only and write it anonymised as ``snapshot.json``."""
    run = fetch_failed_run(RunRef(REPO, run_id), attempt=None)
    if not isinstance(run, FailedRun):
        sys.exit(f"refused: {run}")
    text = dump_snapshot(run).replace(*PATHS).replace(REPO, ANON)
    out.mkdir(parents=True, exist_ok=True)
    (out / "snapshot.json").write_text(text + "\n")


def tree(ref: str, out: Path) -> None:
    """Vendor the tree of ``ref`` into ``tree/`` and its Git listing."""
    archive = _git_bytes("archive", "--format=tar", ref)
    dest = out / "tree"
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    entries = []
    for line in _git("ls-tree", "-r", "-t", "--full-tree", ref).splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        entries.append({"path": path, "mode": mode, "type": kind, "sha": sha})
    sha = _git("rev-parse", f"{ref}^{{commit}}").strip()
    listing = {"sha": sha, "truncated": False, "tree": entries}
    (out / "tree-listing.json").write_text(json.dumps(listing, indent=2) + "\n")


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _git(*args: str) -> str:
    return _git_bytes(*args).decode()


def main() -> None:
    """Parse the sub-command and run it."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--run-id", type=int, required=True)
    s.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("tree")
    t.add_argument("--ref", required=True)
    t.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "snapshot":
        snapshot(args.run_id, args.out)
    else:
        tree(args.ref, args.out)


if __name__ == "__main__":
    main()
