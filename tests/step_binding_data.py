"""The ``steps-1`` recording, read plainly, and the named derivations of it.

Synthetic cases are built from the recording at test time by the functions
here; no derived binary is committed (``tests/fixtures/step-binding/
PROVENANCE.md``, "Synthetic cases"). This module parses the archive without
``deployer.logarchive`` so that the ownership tests do not depend on the reader.
"""

import io
import json
import re
import warnings
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from deployer.stepbinding import StepDirectory, StepFile

STEPS_1 = Path(__file__).parent / "fixtures" / "step-binding" / "steps-1"
Entries = list[tuple[str, bytes]]
_LOGS_RE = re.compile(r"actions/jobs/(\d+)/logs$")
_SETUP = ("Set up job", "Complete job")


def calls() -> list[dict[str, Any]]:
    """Every recorded ``gh api`` call of ``steps-1``."""
    return json.loads((STEPS_1 / "gh-calls.json").read_text())


def job_records() -> dict[str, dict[str, Any]]:
    """The attempt's jobs listing, by job name."""
    return {
        job["name"]: job
        for call in calls()
        if "/jobs?" in call["argv"][-1] and "stdout" in call
        for job in json.loads(call["stdout"])["jobs"]
    }


def job_logs() -> dict[str, str]:
    """Each job's log text exactly as recorded (leading BOM included)."""
    by_id = {job["id"]: name for name, job in job_records().items()}
    logs: dict[str, str] = {}
    for call in calls():
        match = _LOGS_RE.search(call["argv"][-1])
        if match and "stdout" in call:
            logs[by_id[int(match.group(1))]] = call["stdout"]
    return logs


def archive_entries() -> Entries:
    """``attempt-1.zip``'s entries in archive order."""
    with zipfile.ZipFile(STEPS_1 / "attempt-1.zip") as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


def zip_of(
    entries: Iterable[tuple[str, bytes]], method: int = zipfile.ZIP_DEFLATED
) -> bytes:
    """A ZIP of ``entries`` in order; duplicate names are written as given."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns on a duplicate name
        with zipfile.ZipFile(buffer, "w", method) as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return buffer.getvalue()


def directories(entries: Entries | None = None) -> list[StepDirectory]:
    """Step directories read plainly: ``<dir>/<N>_….txt``, ``system.txt`` skipped."""
    groups: dict[str, list[StepFile]] = {}
    for name, data in archive_entries() if entries is None else entries:
        head, sep, rest = name.partition("/")
        if not sep or rest == "system.txt":
            continue
        number = int(rest.split("_", 1)[0])
        groups.setdefault(head, []).append(StepFile(number, data.decode("utf-8")))
    return [
        StepDirectory(name, tuple(sorted(files, key=lambda f: f.number)))
        for name, files in sorted(groups.items())
    ]


def renamed(entries: Entries, old: str, new: str) -> Entries:
    """Every entry whose name starts with ``old`` renamed to start with ``new``."""
    return [
        (new + name[len(old) :] if name.startswith(old) else name, data)
        for name, data in entries
    ]


def without(entries: Entries, prefix: str) -> Entries:
    """``entries`` minus those whose name starts with ``prefix``."""
    return [(name, data) for name, data in entries if not name.startswith(prefix)]


def edited(entries: Entries, prefix: str, change: Callable[[bytes], bytes]) -> Entries:
    """``change`` applied to every entry whose name starts with ``prefix``."""
    return [
        (name, change(data) if name.startswith(prefix) else data)
        for name, data in entries
    ]


_WORKER_RE = re.compile(rb"Worker ID: \{[0-9a-f-]+\}")
_TEMP_RE = re.compile(rb"_temp/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def foreign_runner(entries: Entries) -> Entries:
    """Every step file with another runner's ``Worker ID`` and temporary ``HOME``
    lines: the lines that differ between real runs. A labelled stand-in for
    another attempt's archive (spec §9.2, "attempt mixing")."""
    zeros = b"00000000-0000-0000-0000-000000000000"

    def change(data: bytes) -> bytes:
        data = _WORKER_RE.sub(b"Worker ID: {" + zeros + b"}", data)
        return _TEMP_RE.sub(b"_temp/" + zeros, data)

    return [
        (
            name,
            change(data) if "/" in name and not name.endswith("system.txt") else data,
        )
        for name, data in entries
    ]


def preregistered() -> list[tuple[str, str, int]]:
    """``(job, exact line, API step number)`` for every ``expected.json`` line."""
    records = job_records()
    expected = json.loads((STEPS_1 / "expected.json").read_text())["lines"]
    owners: list[tuple[str, str, int]] = []
    for row in expected:
        steps = [
            s
            for s in records[row["job"]]["steps"]
            if s["name"] not in _SETUP and not s["name"].startswith("Post ")
        ]
        owners.append((row["job"], row["line"], steps[1 + row["step"]]["number"]))
    return owners
