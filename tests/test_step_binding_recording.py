"""Integrity of the step-binding recording (``tests/fixtures/step-binding``).

Every file matches ``CHECKSUMS.sha256``; ``observed.json`` answers every line
``expected.json`` pre-registered, at the owner it pre-registered; and the facts
``PROVENANCE.md`` states about the data are re-read from the data itself, not
from any forge parser.
"""

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent / "fixtures" / "step-binding"
CASE = ROOT / "steps-1"
NOT_CHECKSUMMED = {
    "CHECKSUMS.sha256",
    "record_steps.py",
    "record_steps2.py",
    "measure_retention.py",
}
TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ", re.MULTILINE)
BOM = "\ufeff"
SETUP_STEPS = ("Set up job", "Complete job")


def _json(name: str) -> Any:
    return json.loads((CASE / name).read_text())


def _jobs_and_logs() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The jobs listing by job name and each job log, from ``gh-calls.json``."""
    calls = _json("gh-calls.json")
    jobs: dict[str, dict[str, Any]] = {}
    for call in calls:
        if "/jobs?" in call["argv"][-1] and "stdout" in call:
            for job in json.loads(call["stdout"])["jobs"]:
                jobs[job["name"]] = job
    by_id = {job["id"]: name for name, job in jobs.items()}
    logs: dict[str, str] = {}
    for call in calls:
        match = re.search(r"actions/jobs/(\d+)/logs", call["argv"][-1])
        if match and "stdout" in call:
            logs[by_id[int(match.group(1))]] = call["stdout"].removeprefix(BOM)
    return jobs, logs


def _step_files(archive: zipfile.ZipFile, job: str) -> list[tuple[int, str]]:
    """``(API step number, member name)`` of a job's per-step files, in order."""
    found = []
    for name in archive.namelist():
        head, _, base = name.partition("/")
        if head == job and base != "system.txt":
            match = re.match(r"(\d+)_", base)
            assert match is not None, name
            found.append((int(match.group(1)), name))
    return sorted(found)


def test_every_file_matches_its_checksum() -> None:
    listed = {}
    for row in (ROOT / "CHECKSUMS.sha256").read_text().splitlines():
        digest, rel = row.split("  ", 1)
        listed[rel] = digest
    present = {
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*")
        if p.is_file()
        and p.name not in NOT_CHECKSUMMED
        and "__pycache__" not in p.parts
    }
    assert present == set(listed)
    for rel, digest in listed.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == digest, rel


def test_observed_answers_every_preregistered_line_at_its_owner() -> None:
    jobs, logs = _jobs_and_logs()
    expected = _json("expected.json")["lines"]
    observed = _json("observed.json")["lines"]
    assert [(e["job"], e["line"]) for e in expected] == [
        (o["job"], o["line"]) for o in observed
    ]
    for want, got in zip(expected, observed, strict=True):
        steps = [
            s
            for s in jobs[want["job"]]["steps"]
            if s["name"] not in SETUP_STEPS and not s["name"].startswith("Post ")
        ]
        owner = steps[1 + want["step"]]["number"]  # steps[0] is the checkout
        assert got["zip_step"] == owner, got
        lines = [
            TIMESTAMP_RE.sub("", line, count=1)
            for line in logs[want["job"]].splitlines()
        ]
        hits = [i + 1 for i, text in enumerate(lines) if text == want["line"]]
        assert hits == [got["job_log_line"]], got


def test_each_line_sits_in_exactly_the_observed_step_file() -> None:
    archive = zipfile.ZipFile(CASE / "attempt-1.zip")
    for row in _json("observed.json")["lines"]:
        holders = [
            number
            for number, name in _step_files(archive, row["job"])
            if row["line"]
            in TIMESTAMP_RE.sub("", archive.read(name).decode("utf-8-sig")).splitlines()
        ]
        assert holders == [row["zip_step"]], row


def test_step_files_add_up_to_the_job_log_as_text() -> None:
    jobs, logs = _jobs_and_logs()
    archive = zipfile.ZipFile(CASE / "attempt-1.zip")
    assert set(jobs) == set(logs)
    for job, log in logs.items():
        files = _step_files(archive, job)
        joined = "".join(archive.read(name).decode("utf-8-sig") for _, name in files)
        assert TIMESTAMP_RE.sub("", joined) == TIMESTAMP_RE.sub("", log), job
        assert {n for n, _ in files} <= {s["number"] for s in jobs[job]["steps"]}


def test_the_spoofed_header_is_byte_identical_to_the_real_one() -> None:
    _, logs = _jobs_and_logs()
    lines = [
        TIMESTAMP_RE.sub("", line, count=1) for line in logs["s5-spoof"].splitlines()
    ]
    header = "##[group]Run printf '%s-%s\\n' MARK s5-next"
    assert [i + 1 for i, text in enumerate(lines) if text == header] == [113, 118]


RETENTION = ROOT / "retention"
TRACKED = (37255937674, 37255938526)


def _rows(name: str) -> list[dict[str, Any]]:
    return json.loads((RETENTION / f"{name}.json").read_text())["rows"]


def test_the_retention_facts_are_re_derived_from_the_data() -> None:
    """PROVENANCE's retention section, re-read from ``retention/`` itself."""
    rows = _rows("pass-1")
    assert len(rows) == 40
    assert {r["result"]["kind"] for r in rows} == {"read"}
    present = [r for r in rows if r["result"]["per_step_files"]]
    absent = [r for r in rows if not r["result"]["per_step_files"]]
    assert sorted(r["run_id"] for r in present) == sorted(TRACKED)
    assert all(r["age_hours_at_download"] < 0.05 for r in present)
    assert len(absent) == 38
    assert min(r["age_hours_at_download"] for r in absent) >= 8.3
    first = {r["run_id"]: r for r in present}
    for name in ("track-15m", "track-30m"):
        for row in _rows(name):
            assert row["result"]["per_step_files"], (name, row["run_id"])
            assert row["sha256"] == first[row["run_id"]]["sha256"], (
                name,
                row["run_id"],
            )
    for row in _rows("track-60m"):
        assert not row["result"]["per_step_files"]
        assert len(row["entries"]) == 2
