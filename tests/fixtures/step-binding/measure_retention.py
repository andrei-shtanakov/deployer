"""Dev tool: passive measurement of per-step file presence in per-attempt log
archives (TODO step-archive-retention-window). Owner-permitted 2026-10-05:
read-only downloads, no dispatches, at most 40 runs per pass.

Run from the repository root (needs ``gh`` logged in)::

    uv run python tests/fixtures/step-binding/measure_retention.py select  # writes the run list
    uv run python tests/fixtures/step-binding/measure_retention.py read <pass-name>

``select`` picks up to 40 completed runs across the ecosystem's repositories,
spread over age buckets, and writes ``retention/runs.json``. ``read`` downloads
each selected run's attempt archive through ``SubprocessGh.api_bytes_capped``,
capped at 64 MiB, and writes ``retention/<pass-name>.json``. For each run it
records:
- ``(repo, run_id, attempt)``;
- the attempt's completion time and the download time, both UTC;
- the event and the number of jobs;
- the archive's size and SHA-256 and its entry names;
- for each top-level directory, whether it holds per-step files.

HTTP errors, timeouts and an exceeded cap are recorded as such, and none of
them means "no per-step files". No log text is stored: listings and metadata
only.
"""

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deployer import forge

HERE = Path(__file__).resolve().parent / "retention"
OWNER = "andrei-shtanakov"
REPOS = (
    "deployer",
    "maestro",
    "atp-platform",
    "steward",
    "dispatcher",
    "spec-runner",
    "arbiter",
    "libretto",
    "robin-runtime",
)
LIMIT = 40
CAP = 64 * 2**20
BUCKETS_H = ((0, 12), (12, 24), (24, 48), (48, 168), (168, 720), (720, 10**6))


def _gh_json(args: list[str]) -> Any:
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def select() -> int:
    """Up to ``LIMIT`` runs, round-robin over repos within each age bucket."""
    now = _now()
    pool: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(BUCKETS_H))}
    for repo in REPOS:
        runs = _gh_json(
            [
                "run",
                "list",
                "-R",
                f"{OWNER}/{repo}",
                "--status",
                "completed",
                "--limit",
                "100",
                "--json",
                "databaseId,updatedAt,event,attempt",
            ]
        )
        for run in runs:
            age_h = (now - _parse(run["updatedAt"])).total_seconds() / 3600
            for i, (lo, hi) in enumerate(BUCKETS_H):
                if lo <= age_h < hi:
                    pool[i].append({"repo": repo, **run})
                    break
    per_bucket = LIMIT // len(BUCKETS_H) + 1
    chosen: list[dict[str, Any]] = []
    for i in range(len(BUCKETS_H)):
        by_repo: dict[str, list[dict[str, Any]]] = {}
        for run in pool[i]:
            by_repo.setdefault(run["repo"], []).append(run)
        picked: list[dict[str, Any]] = []
        while len(picked) < per_bucket and any(by_repo.values()):
            for repo in list(by_repo):
                if by_repo[repo] and len(picked) < per_bucket:
                    picked.append(by_repo[repo].pop(0))
        chosen.extend(picked)
    chosen = chosen[:LIMIT]
    HERE.mkdir(exist_ok=True)
    (HERE / "runs.json").write_text(
        json.dumps({"selected_at": _ts(now), "runs": chosen}, indent=2, sort_keys=True)
        + "\n"
    )
    print(f"selected {len(chosen)} runs")
    return 0


def _record(repo: str, run_id: int, attempt: int) -> dict[str, Any]:
    meta = _gh_json(
        ["api", f"repos/{OWNER}/{repo}/actions/runs/{run_id}/attempts/{attempt}"]
    )
    jobs = _gh_json(
        [
            "api",
            f"repos/{OWNER}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        ]
    )["jobs"]
    completed = max(
        (j["completed_at"] for j in jobs if j.get("completed_at")), default=None
    )
    row: dict[str, Any] = {
        "repo": repo,
        "run_id": run_id,
        "attempt": attempt,
        "event": meta.get("event"),
        "jobs": len(jobs),
        "attempt_completed_at": completed,
        "run_updated_at": meta.get("updated_at"),
    }
    path = f"repos/{OWNER}/{repo}/actions/runs/{run_id}/attempts/{attempt}/logs"
    row["downloaded_at"] = _ts(_now())
    try:
        blob = forge.SubprocessGh().api_bytes_capped(
            [path], timeout=120.0, max_bytes=CAP
        )
    except forge.GhTimeout as exc:
        row["result"] = {"kind": "timeout", "detail": str(exc)[:200]}
        return row
    except forge.GhError as exc:
        kind = "http_error" if exc.status is not None else "gh_failure"
        row["result"] = {"kind": kind, "status": exc.status, "detail": str(exc)[:200]}
        return row
    if isinstance(blob, forge.OverCap):
        row["result"] = {"kind": "over_cap", "max_bytes": blob.max_bytes}
        return row
    row["size"] = len(blob)
    row["sha256"] = hashlib.sha256(blob).hexdigest()
    try:
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    except zipfile.BadZipFile as exc:
        row["result"] = {"kind": "corrupt", "detail": str(exc)[:200]}
        return row
    row["entries"] = names
    dirs: dict[str, bool] = {}
    for name in names:
        head, sep, rest = name.partition("/")
        if sep:
            dirs.setdefault(head, False)
            if rest and rest != "system.txt" and not name.endswith("/"):
                dirs[head] = True
    row["per_step_files_by_directory"] = dirs
    row["result"] = {"kind": "read", "per_step_files": any(dirs.values())}
    if completed:
        age = (_parse(row["downloaded_at"]) - _parse(completed)).total_seconds() / 3600
        row["age_hours_at_download"] = round(age, 2)
    return row


def read(pass_name: str) -> int:
    out = HERE / f"{pass_name}.json"
    if out.exists():
        print(f"refusing: {out} exists", file=sys.stderr)
        return 2
    runs = json.loads((HERE / "runs.json").read_text())["runs"]
    rows = [_record(r["repo"], r["databaseId"], r["attempt"]) for r in runs]
    out.write_text(
        json.dumps({"pass": pass_name, "rows": rows}, indent=2, sort_keys=True) + "\n"
    )
    kinds: dict[str, int] = {}
    for row in rows:
        key = row["result"]["kind"]
        if key == "read":
            key += "+files" if row["result"]["per_step_files"] else "-files"
        kinds[key] = kinds.get(key, 0) + 1
    print(json.dumps(kinds))
    return 0


def reread(pass_name: str, targets: list[str]) -> int:
    """Re-read only the named runs (``repo:run_id:attempt``): repeated reads of one
    attempt give the interval "last present -> first absent"."""
    out = HERE / f"{pass_name}.json"
    if out.exists():
        print(f"refusing: {out} exists", file=sys.stderr)
        return 2
    rows = []
    for target in targets:
        repo, run_id, attempt = target.split(":")
        rows.append(_record(repo, int(run_id), int(attempt)))
    out.write_text(
        json.dumps({"pass": pass_name, "rows": rows}, indent=2, sort_keys=True) + "\n"
    )
    for row in rows:
        print(
            row["repo"], row["run_id"], row.get("age_hours_at_download"), row["result"]
        )
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["select"]:
        return select()
    if len(argv) == 2 and argv[0] == "read":
        return read(argv[1])
    if len(argv) >= 3 and argv[0] == "reread":
        return reread(argv[1], argv[2:])
    print(
        "usage: measure_retention.py select | read <pass> | reread <pass> <repo:run:attempt>...",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
