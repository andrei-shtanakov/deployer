# Forge Archive Step Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind a failed run's job-log lines to their `StepRef` when, and only when, the
per-attempt log archive's per-step files prove which step each line belongs to. In every
other case, keep today's job-level evidence unchanged.

**Architecture:** Two new pure modules hold the logic. `stepbinding.py` normalises and
compares texts and proves ownership. `logarchive.py` is a bounded, in-memory ZIP reader.
`forge.py` gains a capped streaming download (`SubprocessGh.api_bytes_capped`), an
overlay that cuts forge's unchanged blocks at step boundaries, the snapshot 1.5 states,
and the wiring in `fetch_failed_run`. The evidence text stays the job log's, and
`job_text` is byte-identical whether or not anything binds.

**Tech Stack:** Python 3.12, stdlib `zipfile`/`zlib`/`subprocess`/`threading` (no new
dependency), pydantic `TypeAdapter` (existing), pytest, uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-10-03-forge-archive-step-binding-design.md` (rev 2.1,
approved for planning). Cited below as §N.

## Global Constraints

- Run everything through `uv`: `uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`. Never pip.
- Line length 88; type hints everywhere; public functions get docstrings.
- Limits are constants with no CLI flag: download 64 MiB; entries 4096; one entry uncompressed 64 MiB; all read entries uncompressed 256 MiB (§7.1).
- Comparison timestamp: `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ` (exactly one space), removed only where present. Forge's own looser `RUNNER_LOG_TIMESTAMP_RE` is **not** used for comparison (§3.2).
- One BOM (U+FEFF) is dropped at the start of each compared text only. Evidence text, `_split_blocks`' splitting and the separate BOM item `forge-log-leading-bom` are untouched (§4.3).
- `job_text` must be byte-identical for one job log whether the job is bound, the archive is absent, refused or unavailable, or no archive was attempted (§5.3).
- `SNAPSHOT_SCHEMA_VERSION` becomes `"1.5"`. Its new fields are additive and default to `None`, meaning "not attempted" (§6).
- Unchanged by this plan: `read_attempt`, `fetch_archive`, `fix/*`, `reproduce/*`, `admission/*`, `diagnose.py`'s logic (only its docstrings' version), `verdict_schema_version`.
- An HTTP-status `GhError` is data (an archive state). A status-less `GhError` propagates (§7.4).
- Production `SubprocessGh` reads the archive. A runner without `api_bytes_capped` (every existing test fake) attempts no archive, which leaves the states `None`.
- The work happens on the branch `feat/forge-archive-step-binding`, which already holds the spec.
- **Deviation from §9.2's wording:** the synthetic cases are derived at test time by named functions in `tests/step_binding_data.py`; no derived ZIPs are committed under `tests/fixtures/step-binding/synthetic/`. The derivation and its labels live in PROVENANCE's "Synthetic cases" section (Task 6). Derivations stay reviewable as code, and no binary escapes the checksums.

## Review Focus

1. **A job log with lines that carry no timestamp.** An `env:` value with newlines prints continuation lines without a runner timestamp. Expected: such lines compare as they are and the job still binds (Task 2, `test_an_untimestamped_line_is_compared_as_is`).
2. **A directory name that has nothing to do with the job's name.** Matrix names get sanitised, and names are never proof. Expected: the job binds by content alone (Task 2, `test_the_directory_name_proves_nothing`).
3. **A cancelled run with a job that never started.** Its conclusion is `cancelled` and its log is not `present`. Expected: it is in the population, so every kept job is `unverifiable` and nothing binds (Task 5, `test_a_job_that_never_started_makes_binding_unverifiable`).
4. **`--attempt` omitted.** Expected: the archive request names the **resolved** attempt (`attempts/2`), never `None` or the latest by accident (Task 5, `test_the_archive_request_names_the_resolved_attempt`).
5. **A ZIP that carries explicit directory entries (`dir/`).** Expected: ignored, never counted as a malformed step entry (Task 3, `test_directory_entries_are_ignored`).

---

### Task 1: Blocks with line indices, and the step overlay that keeps `job_text`

**Files:**
- Create: `src/deployer/stepbinding.py` (only `StepSpan` in this task)
- Modify: `src/deployer/forge.py` (`_split_blocks`, `_log_evidence`, `build_failed_job`)
- Test: `tests/test_forge_overlay.py`

**Interfaces:**
- Produces: `deployer.stepbinding.StepSpan(number: int, start: int, end: int)`. These are job-log line indices `[start, end)`, 0-based as `str.splitlines` numbers them.
- Produces: `forge.build_failed_job(record, job_id, log_text, annotations, completeness, *, spans: Sequence[StepSpan] = ()) -> FailedJob`. With no spans the behaviour is exactly today's.

- [ ] **Step 1: Write the failing tests**

`tests/test_forge_overlay.py`:

```python
"""The step overlay over forge's blocks (spec §5): ``job_text`` never moves."""

from typing import Any

import pytest

from deployer.forge import COMPLETE_BY_CONSTRUCTION, StepRef, build_failed_job
from deployer.reproduce.shape import job_text
from deployer.stepbinding import StepSpan

TS = "2026-10-03T10:08:54.1234567Z "
JOB = 7


def _log(
    lines: list[str], *, newline: str = "\n", final: bool = True, bom: bool = False
) -> str:
    body = newline.join(TS + line for line in lines) + (newline if final else "")
    return ("\ufeff" if bom else "") + body


BOUNDARY_BLANKS = [
    "##[group]Run a",
    "a-cmd",
    "##[endgroup]",
    "a-out",
    "",
    "",
    "##[group]Run b",
    "b-cmd",
    "##[endgroup]",
    "b-out",
]
BOUNDARY_SPANS = (StepSpan(3, 0, 5), StepSpan(4, 5, 10))

EMPTY_OUTPUT = [
    "##[group]Run a",
    "a-cmd",
    "##[endgroup]",
    "a-out",
    "##[group]Run b",
    "b-cmd",
    "##[endgroup]",
    "##[group]Run c",
    "c-cmd",
    "##[endgroup]",
    "c-out",
]
EMPTY_SPANS = (StepSpan(3, 0, 4), StepSpan(4, 4, 7), StepSpan(5, 7, 11))

UNCLOSED = ["##[group]Run a", "a-cmd", "a-out", "b-out-1", "b-out-2"]
UNCLOSED_SPANS = (StepSpan(3, 0, 3), StepSpan(4, 3, 5))

BLANK_BLOCK = ["##[group]Run a", "a-cmd", "##[endgroup]", "", "  ", "##[group]Run b"]
BLANK_SPANS = (StepSpan(3, 0, 4), StepSpan(4, 4, 6))

CASES = {
    "blank lines at a boundary": (_log(BOUNDARY_BLANKS), BOUNDARY_SPANS),
    "a step with empty output": (_log(EMPTY_OUTPUT), EMPTY_SPANS),
    "an unclosed group across a boundary": (_log(UNCLOSED), UNCLOSED_SPANS),
    "a blank-only block the splitter drops": (_log(BLANK_BLOCK), BLANK_SPANS),
    "no final newline": (_log(BOUNDARY_BLANKS, final=False), BOUNDARY_SPANS),
    "a CRLF log": (_log(BOUNDARY_BLANKS, newline="\r\n"), BOUNDARY_SPANS),
    "a leading BOM": (_log(BOUNDARY_BLANKS, bom=True), BOUNDARY_SPANS),
}


def _record(spans: tuple[StepSpan, ...]) -> dict[str, Any]:
    steps = [
        {"number": s.number, "name": f"s{s.number}", "conclusion": "failure"}
        for s in spans
    ]
    return {"name": "j", "conclusion": "failure", "steps": steps}


@pytest.mark.parametrize("case", sorted(CASES))
def test_job_text_is_the_same_bound_and_unbound(case: str) -> None:
    log, spans = CASES[case]
    unbound = build_failed_job(_record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION)
    bound = build_failed_job(
        _record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION, spans=spans
    )
    assert job_text(bound) == job_text(unbound)
    assert all(e.source is None for e in unbound.evidence)
    assert {e.source for e in bound.evidence} <= {StepRef(JOB, s.number) for s in spans}
    assert all(not s.evidence for s in bound.steps)


def _sources_of(line: str, log: str, spans: tuple[StepSpan, ...]) -> list[object]:
    bound = build_failed_job(
        _record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION, spans=spans
    )
    return [e.source for e in bound.evidence if line in e.text.split("\n")]


def test_a_block_cut_at_a_boundary_keeps_every_line_with_its_step() -> None:
    log, spans = CASES["blank lines at a boundary"]
    assert _sources_of("a-out", log, spans) == [StepRef(JOB, 3)]
    assert _sources_of("b-out", log, spans) == [StepRef(JOB, 4)]
    bound = build_failed_job(
        _record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION, spans=spans
    )
    texts = [(e.source, e.text) for e in bound.evidence]
    assert (StepRef(JOB, 3), "a-out\n") in texts
    assert (StepRef(JOB, 4), "") in texts  # the second blank line, kept


def test_an_unclosed_group_is_cut_between_its_steps() -> None:
    log, spans = CASES["an unclosed group across a boundary"]
    assert _sources_of("a-out", log, spans) == [StepRef(JOB, 3)]
    assert _sources_of("b-out-1", log, spans) == [StepRef(JOB, 4)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_forge_overlay.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'deployer.stepbinding'`.

- [ ] **Step 3: Create `stepbinding.py` with `StepSpan`**

`src/deployer/stepbinding.py`:

```python
"""Step binding from the per-attempt log archive.

Design: ``docs/superpowers/specs/2026-10-03-forge-archive-step-binding-design.md``.
Pure functions over texts already read: no I/O and no ``gh``. ``forge`` reads
the archive and the logs, and asks this module which job-log lines the
runner's own per-step files place under which step.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StepSpan:
    """Job-log lines ``[start, end)`` the runner filed under API step ``number``.

    Indices are 0-based, as ``str.splitlines`` numbers the job log's lines.
    """

    number: int
    start: int
    end: int
```

- [ ] **Step 4: Make `_split_blocks` report indices and add the overlay**

In `src/deployer/forge.py`, add `from itertools import groupby` and
`from collections.abc import Sequence` to the imports, and add
`from deployer.stepbinding import StepSpan`. Replace `_split_blocks` and `_log_evidence`:

```python
def _log_evidence(
    log_text: str, job_id: int, spans: Sequence[StepSpan] = ()
) -> list[Evidence]:
    """A job log's blocks, in log order, as evidence.

    A ``##[group]<title>`` header is no ground for binding a block to a step
    (snapshot 1.4): a step's own output can print a group whose title is
    byte-identical to the next step's runner header (recording ``steps-1``,
    job ``s5-spoof``). Only ``spans`` bind: boundaries the per-attempt
    archive proved (spec §4). A block that crosses a boundary is cut at it;
    every piece keeps all its lines, blank ones included, so the pieces of a
    block join back to the block and ``job_text`` never depends on binding
    (spec §5.3). Lines outside every span stay ``source=None``.
    """
    owner = {i: span.number for span in spans for i in range(span.start, span.end)}
    evidence: list[Evidence] = []
    for block in _split_blocks(log_text):
        for number, piece in groupby(block, key=lambda item: owner.get(item[0])):
            source = None if number is None else StepRef(job_id, number)
            text = "\n".join(line for _, line in piece)
            evidence.append(Evidence(source=source, text=text))
    return evidence


def _split_blocks(log_text: str) -> list[list[tuple[int, str]]]:
    """Blocks of ``(splitlines index, normalised line)``, split at
    ``##[group]`` and after ``##[endgroup]``; a block with no non-blank line
    is dropped."""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []

    def flush() -> None:
        if any(line.strip() for _, line in current):
            blocks.append(current)

    for index, raw in enumerate(log_text.splitlines()):
        line = RUNNER_LOG_TIMESTAMP_RE.sub("", raw, count=1)
        line = ANSI_CSI_RE.sub("", line)
        if line.startswith(RUNNER_GROUP_PREFIX):
            flush()
            current = [(index, line)]
        elif line == _ENDGROUP:
            current.append((index, line))
            flush()
            current = []
        else:
            current.append((index, line))
    flush()
    return blocks
```

In `build_failed_job`, add the keyword parameter `spans: Sequence[StepSpan] = ()`
after `completeness` (signature
`def build_failed_job(record, job_id, log_text, annotations, completeness, *, spans=())`).
Replace `job_evidence = _log_evidence(log_text)` with
`job_evidence = _log_evidence(log_text, job_id, spans)`, and add one sentence to its
docstring: "``spans`` (spec §4) bind log lines to steps; without them every log block
is job-level."

- [ ] **Step 5: Run the new tests and the whole suite**

Run: `uv run pytest tests/test_forge_overlay.py -v`
Expected: all 9 PASS.
Run: `uv run pytest -q`
Expected: everything passes (no spans anywhere yet, so nothing else changes).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/stepbinding.py src/deployer/forge.py tests/test_forge_overlay.py
git commit -m "feat(forge): blocks carry line indices; spans overlay keeps job_text"
```

---

### Task 2: Ownership — comparison, matching first, structure after (pure)

**Files:**
- Modify: `src/deployer/stepbinding.py`
- Create: `tests/step_binding_data.py` (the `steps-1` readers and named derivations, shared by Tasks 2–6)
- Test: `tests/test_stepbinding.py`

**Interfaces:**
- Consumes: `StepSpan` (Task 1).
- Produces, in `deployer.stepbinding`:
  - `COMPARE_TIMESTAMP_RE: re.Pattern[str]`
  - `comparison_lines(text: str) -> list[str]`
  - `StepFile(number: int, text: str)`
  - `StepDirectory(name: str, files: tuple[StepFile, ...])`: files in increasing `number`, numbers unique, text strictly decoded (`logarchive` guarantees all three)
  - `StepBindingState = Literal["bound", "no_archive", "unverifiable", "unmatched", "ambiguous", "malformed"]`
  - `JobBinding(state: StepBindingState, reason: str | None = None, spans: tuple[StepSpan, ...] = ())`
  - `bind_jobs(logs: Mapping[int, str], api_steps: Mapping[int, Sequence[int]], directories: Sequence[StepDirectory]) -> dict[int, JobBinding]`. `logs` is the whole population (§4.1) by `job_id`. The result has one entry per job in `logs`.
- Produces, in `tests/step_binding_data.py`: `STEPS_1`, `calls()`, `job_records()`, `job_logs()`, `archive_entries()`, `zip_of()`, `directories()`, `renamed()`, `without()`, `edited()`, `foreign_runner()`, `preregistered()`.

- [ ] **Step 1: Write the shared test data module**

`tests/step_binding_data.py`:

```python
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


def zip_of(entries: Iterable[tuple[str, bytes]], method: int = zipfile.ZIP_DEFLATED) -> bytes:
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
        (name, change(data) if "/" in name and not name.endswith("system.txt") else data)
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
```

- [ ] **Step 2: Write the failing ownership tests**

`tests/test_stepbinding.py`:

```python
"""Ownership by content (spec §3, §4) on ``steps-1`` and its derivations."""

import json

from deployer.stepbinding import (
    StepDirectory,
    StepFile,
    bind_jobs,
    comparison_lines,
)
from tests import step_binding_data as data

TS = "2026-10-03T10:08:54.1234567Z "


def _steps1() -> tuple[dict[int, str], dict[int, list[int]], dict[str, int]]:
    records = data.job_records()
    ids = {name: record["id"] for name, record in records.items()}
    logs = {ids[name]: text for name, text in data.job_logs().items()}
    numbers = {r["id"]: [s["number"] for s in r["steps"]] for r in records.values()}
    return logs, numbers, ids


def test_comparison_lines_drop_one_bom_split_on_newline_and_strip_timestamps() -> None:
    text = f"\ufeff{TS}a\n{TS}\ufeffb\r\n{TS}c"
    assert comparison_lines(text) == ["a", "\ufeffb\r", "c"]
    assert comparison_lines(f"{TS}a\n") == ["a"]
    assert comparison_lines("") == []
    assert comparison_lines("2026-10-03T10:08:54Z a\n") == ["2026-10-03T10:08:54Z a"]


def test_every_recorded_job_is_bound_and_every_line_lands_at_its_owner() -> None:
    logs, numbers, ids = _steps1()
    result = bind_jobs(logs, numbers, data.directories())
    assert {r.state for r in result.values()} == {"bound"}
    for job_id, binding in result.items():
        assert binding.spans[0].start == 0
        assert all(a.end == b.start for a, b in zip(binding.spans, binding.spans[1:]))
        assert binding.spans[-1].end == len(comparison_lines(logs[job_id]))
    observed = json.loads((data.STEPS_1 / "observed.json").read_text())["lines"]
    for row in observed:
        index = row["job_log_line"] - 1
        spans = result[ids[row["job"]]].spans
        (owner,) = [s.number for s in spans if s.start <= index < s.end]
        assert owner == row["zip_step"], row


def test_renumbering_that_reorders_the_files_is_unmatched() -> None:
    logs, numbers, ids = _steps1()
    entries = data.renamed(data.archive_entries(), "s1-plain-fail/3_", "s1-plain-fail/7_")
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "unmatched"
    assert result[ids["s2-named"]].state == "bound"


def test_a_number_outside_the_api_listing_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    entries = data.renamed(data.archive_entries(), "s1-plain-fail/9_", "s1-plain-fail/99_")
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "malformed"
    assert "99" in (result[ids["s1-plain-fail"]].reason or "")


def test_a_repeated_api_step_number_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    numbers[ids["s1-plain-fail"]] = [*numbers[ids["s1-plain-fail"]], 4]
    result = bind_jobs(logs, numbers, data.directories())
    assert result[ids["s1-plain-fail"]].state == "malformed"


def test_identical_job_logs_make_both_jobs_ambiguous() -> None:
    logs, numbers, ids = _steps1()
    logs[ids["s5-spoof"]] = logs[ids["s6-dupes"]]
    result = bind_jobs(logs, numbers, data.directories())
    assert result[ids["s5-spoof"]].state == "ambiguous"
    assert result[ids["s6-dupes"]].state == "ambiguous"
    assert result[ids["s1-plain-fail"]].state == "bound"


def test_two_directories_matching_one_job_make_it_ambiguous() -> None:
    logs, numbers, ids = _steps1()
    entries = data.archive_entries()
    copy = [
        ("copy-of-s1/" + name.partition("/")[2], body)
        for name, body in entries
        if name.startswith("s1-plain-fail/")
    ]
    result = bind_jobs(logs, numbers, data.directories(entries + copy))
    assert result[ids["s1-plain-fail"]].state == "ambiguous"


def test_a_missing_step_file_is_unmatched() -> None:
    logs, numbers, ids = _steps1()
    entries = data.without(data.archive_entries(), "s2-named/3_")
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s2-named"]].state == "unmatched"
    assert result[ids["s3-two-failures"]].state == "bound"


def test_a_step_file_without_a_final_newline_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    entries = data.edited(
        data.archive_entries(), "s1-plain-fail/9_", lambda b: b.removesuffix(b"\n")
    )
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "malformed"


def test_a_foreign_line_break_in_the_job_log_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    job = ids["s1-plain-fail"]
    logs[job] = logs[job].replace("MARK-s1-b", "MARK-s1-b\x85x", 1)
    entries = data.edited(
        data.archive_entries(),
        "s1-plain-fail/4_",
        lambda b: b.replace(b"MARK-s1-b", "MARK-s1-b\x85x".encode(), 1),
    )
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[job].state == "malformed"


def test_an_archive_of_another_runner_matches_nothing() -> None:
    logs, numbers, _ = _steps1()
    result = bind_jobs(logs, numbers, data.directories(data.foreign_runner(data.archive_entries())))
    assert {r.state for r in result.values()} == {"unmatched"}


def test_an_untimestamped_line_is_compared_as_is() -> None:
    log = f"{TS}##[group]Run a\n  continuation\n{TS}##[endgroup]\n{TS}out\n"
    files = (StepFile(3, log),)
    result = bind_jobs({1: log}, {1: [1, 3]}, [StepDirectory("any", files)])
    assert result[1].state == "bound"


def test_the_directory_name_proves_nothing() -> None:
    logs, numbers, ids = _steps1()
    entries = data.renamed(data.archive_entries(), "s1-plain-fail/", "zz/")
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "bound"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_stepbinding.py -v`
Expected: `ImportError: cannot import name 'bind_jobs' from 'deployer.stepbinding'`.

- [ ] **Step 4: Implement comparison and ownership**

Append to `src/deployer/stepbinding.py`, and extend its imports to
`import re`, `from collections.abc import Mapping, Sequence`,
`from typing import Literal`:

```python
COMPARE_TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ")
"""The runner timestamp removed for comparison only (spec §3.2): exactly one
space after it, and only where a line starts with one."""
_BOM = "\ufeff"

StepBindingState = Literal[
    "bound", "no_archive", "unverifiable", "unmatched", "ambiguous", "malformed"
]


@dataclass(frozen=True)
class StepFile:
    """One per-step file: its API step number and its strict UTF-8 text."""

    number: int
    text: str


@dataclass(frozen=True)
class StepDirectory:
    """One archive directory's step files, in increasing ``number``, unique."""

    name: str
    files: tuple[StepFile, ...]


@dataclass(frozen=True)
class JobBinding:
    """What binding decided for one job; ``spans`` only when ``bound``."""

    state: StepBindingState
    reason: str | None = None
    spans: tuple[StepSpan, ...] = ()


def comparison_lines(text: str) -> list[str]:
    """The text as compared (spec §3.1–§3.2): one leading BOM dropped, split on
    ``\\n`` only (a final ``\\n`` opens no line), and each line's runner
    timestamp removed where it starts with one. Nothing else is normalised."""
    body = text.removeprefix(_BOM)
    if not body:
        return []
    lines = body.split("\n")
    if body.endswith("\n"):
        lines.pop()
    return [COMPARE_TIMESTAMP_RE.sub("", line, count=1) for line in lines]


def bind_jobs(
    logs: Mapping[int, str],
    api_steps: Mapping[int, Sequence[int]],
    directories: Sequence[StepDirectory],
) -> dict[int, JobBinding]:
    """Bind each population job's log to at most one step directory (spec §4).

    The full match relation is computed first, by content. A pair counts only
    when the directory matches this one job and the job this one directory;
    any other match makes every job it touches ``ambiguous``. Only then is the
    structure of each candidate checked; a failure is ``malformed`` and frees
    nothing for another job. Names and order prove nothing.
    """
    by_text: dict[tuple[str, ...], list[int]] = {}
    for job_id, text in logs.items():
        by_text.setdefault(tuple(comparison_lines(text)), []).append(job_id)
    jobs_of = [
        by_text.get(
            tuple(line for f in d.files for line in comparison_lines(f.text)), []
        )
        for d in directories
    ]
    result: dict[int, JobBinding] = {}
    for job_id, text in logs.items():
        found = [i for i, jobs in enumerate(jobs_of) if job_id in jobs]
        if not found:
            result[job_id] = JobBinding(
                "unmatched", "no step directory's text equals this job's log"
            )
        elif len(found) > 1:
            result[job_id] = JobBinding(
                "ambiguous", f"{len(found)} step directories match this job's log"
            )
        elif len(jobs_of[found[0]]) > 1:
            result[job_id] = JobBinding(
                "ambiguous", "the matching step directory matches another job too"
            )
        else:
            result[job_id] = _structure(
                text, api_steps.get(job_id, ()), directories[found[0]]
            )
    return result


def _structure(
    log: str, numbers: Sequence[int], directory: StepDirectory
) -> JobBinding:
    """Spec §4.3 on a matched pair: ``bound`` with spans, or ``malformed``."""
    if len(set(numbers)) != len(numbers):
        return JobBinding("malformed", "the jobs listing repeats a step number")
    unknown = [f.number for f in directory.files if f.number not in set(numbers)]
    if unknown:
        return JobBinding("malformed", f"step file number(s) {unknown} not in the listing")
    open_ended = [f.number for f in directory.files if not f.text.endswith("\n")]
    if open_ended:
        return JobBinding("malformed", f"step file(s) {open_ended} lack a final newline")
    if not _breaks_agree(log):
        return JobBinding("malformed", "a line break other than \\n inside the job log")
    spans: list[StepSpan] = []
    start = 0
    for step_file in directory.files:
        end = start + len(comparison_lines(step_file.text))
        spans.append(StepSpan(step_file.number, start, end))
        start = end
    return JobBinding("bound", None, tuple(spans))


def _breaks_agree(log: str) -> bool:
    """Spec §4.3: one BOM-dropped view, split by ``str.splitlines`` and on
    ``\\n`` (one trailing ``\\r`` off each line), gives the same lines, so
    comparison line ``i`` is forge's block line ``i``."""
    view = log.removeprefix(_BOM)
    lines = view.split("\n")
    if view.endswith("\n"):
        lines.pop()
    return view.splitlines() == [line.removesuffix("\r") for line in lines]
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_stepbinding.py -v`
Expected: all 14 PASS. If `test_every_recorded_job_is_bound…` fails on the BOM, the
`_breaks_agree` view is one-sided again (review R1). Fix the code, not the test.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/stepbinding.py tests/step_binding_data.py tests/test_stepbinding.py
git commit -m "feat(stepbinding): ownership by content, matching before structure"
```

---

### Task 3: The bounded ZIP reader

**Files:**
- Create: `src/deployer/logarchive.py`
- Test: `tests/test_logarchive.py`

**Interfaces:**
- Consumes: `StepFile`, `StepDirectory` (Task 2).
- Produces, in `deployer.logarchive`:
  - `ArchiveLimits(download: int = 64 * MIB, entries: int = 4096, entry: int = 64 * MIB, total: int = 256 * MIB)`
  - `ArchiveRefused(reason: str)`
  - `read_step_directories(blob: bytes, limits: ArchiveLimits = ArchiveLimits()) -> tuple[StepDirectory, ...] | ArchiveRefused`. An empty tuple means no step directory (`absent`).

- [ ] **Step 1: Write the failing tests**

`tests/test_logarchive.py`:

```python
"""The archive reader (spec §4.2, §7.1, §7.3): bounded, in memory, all or nothing."""

import zipfile

import pytest

from deployer.logarchive import ArchiveLimits, ArchiveRefused, read_step_directories
from tests import step_binding_data as data


def _patch_central(blob: bytes, name: str, *, flag_or: int = 0, method: int | None = None) -> bytes:
    """Set bits in, or the method of, ``name``'s central-directory record."""
    raw = bytearray(blob)
    target = name.encode()
    at = raw.find(b"PK\x01\x02")
    while at != -1:
        size = int.from_bytes(raw[at + 28 : at + 30], "little")
        if bytes(raw[at + 46 : at + 46 + size]) == target:
            flags = int.from_bytes(raw[at + 8 : at + 10], "little") | flag_or
            raw[at + 8 : at + 10] = flags.to_bytes(2, "little")
            if method is not None:
                raw[at + 10 : at + 12] = method.to_bytes(2, "little")
            return bytes(raw)
        at = raw.find(b"PK\x01\x02", at + 4)
    raise AssertionError(name)


def _refused(blob: bytes, limits: ArchiveLimits = ArchiveLimits()) -> str:
    result = read_step_directories(blob, limits)
    assert isinstance(result, ArchiveRefused), result
    return result.reason


S2_FILE = "s2-named/3_Run printf '%s-%s_n' MARK s2-a.txt"


def test_the_recording_reads_as_six_step_directories() -> None:
    result = read_step_directories((data.STEPS_1 / "attempt-1.zip").read_bytes())
    assert not isinstance(result, ArchiveRefused)
    numbers = {d.name: [f.number for f in d.files] for d in result}
    assert numbers["s1-plain-fail"] == [1, 2, 3, 4, 8, 9]
    assert numbers["s4-composite"] == [1, 2, 3, 4, 5, 10, 11]
    assert len(result) == 6


def test_an_archive_without_step_files_is_empty_not_refused() -> None:
    kept = [(n, b) for n, b in data.archive_entries() if "/" not in n or n.endswith("/system.txt")]
    assert read_step_directories(data.zip_of(kept)) == ()


def test_directory_entries_are_ignored() -> None:
    entries = [("s1-plain-fail/", b""), *data.archive_entries()]
    result = read_step_directories(data.zip_of(entries))
    assert not isinstance(result, ArchiveRefused) and len(result) == 6


def test_a_duplicate_entry_name_refuses_the_archive() -> None:
    entries = data.archive_entries()
    assert "duplicate" in _refused(data.zip_of([*entries, entries[3]]))


def test_a_truncated_archive_is_corrupt() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "corrupt" in _refused(blob[: len(blob) - 100])


def test_a_crc_mismatch_is_corrupt() -> None:
    entries = [(S2_FILE, b"MARK-unique-payload\n")]
    blob = bytearray(data.zip_of(entries, method=zipfile.ZIP_STORED))
    at = blob.find(b"MARK-unique-payload")
    blob[at] ^= 0x01
    assert "corrupt" in _refused(bytes(blob))


def test_an_encrypted_entry_refuses_the_archive() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "encrypted" in _refused(_patch_central(blob, S2_FILE, flag_or=0x1))


def test_an_unsupported_method_refuses_the_archive() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "method" in _refused(_patch_central(blob, S2_FILE, method=12))


def test_a_step_file_that_is_not_utf8_refuses_the_archive() -> None:
    entries = data.edited(data.archive_entries(), "s3-two-failures/4_", lambda b: b"\xff" + b)
    assert "UTF-8" in _refused(data.zip_of(entries))


def test_an_unreadable_copy_beside_a_match_still_refuses() -> None:
    entries = data.archive_entries()
    copy = [
        ("copy/" + n.partition("/")[2], b"\xff" + b)
        for n, b in entries
        if n.startswith("s1-plain-fail/") and not n.endswith("system.txt")
    ]
    assert "UTF-8" in _refused(data.zip_of(entries + copy))


@pytest.mark.parametrize("bad", ["03_x.txt", "+3_x.txt", "x.txt", "3_x.log", "sub/3_x.txt"])
def test_a_malformed_step_entry_name_refuses_the_archive(bad: str) -> None:
    entries = [*data.archive_entries(), (f"s2-named/{bad}", b"x\n")]
    assert "not <N>_<name>.txt" in _refused(data.zip_of(entries))


def test_a_directory_of_only_malformed_names_refuses_rather_than_vanishes() -> None:
    entries = [*data.archive_entries(), ("extra/+1_a.txt", b"a\n"), ("extra/x.txt", b"b\n")]
    assert "not <N>_<name>.txt" in _refused(data.zip_of(entries))


def test_a_repeated_number_in_one_directory_refuses_the_archive() -> None:
    entries = [*data.archive_entries(), ("s2-named/4_another name.txt", b"x\n")]
    assert "repeats" in _refused(data.zip_of(entries))


def test_the_entry_count_limit() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "entries" in _refused(blob, ArchiveLimits(entries=10))


def test_the_one_entry_limit() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "exceeds" in _refused(blob, ArchiveLimits(entry=1000))


def test_the_total_limit() -> None:
    blob = data.zip_of(data.archive_entries())
    assert "total" in _refused(blob, ArchiveLimits(total=20_000))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_logarchive.py -v`
Expected: `ModuleNotFoundError: No module named 'deployer.logarchive'`.

- [ ] **Step 3: Implement the reader**

`src/deployer/logarchive.py`:

```python
"""Reading the per-attempt log archive: in memory, bounded, all or nothing.

Design: ``docs/superpowers/specs/2026-10-03-forge-archive-step-binding-design.md``
§4.2, §7.1, §7.3. Discovery is separate from validation: every top-level
directory's entries other than ``system.txt`` are its step entries, whatever
their names, and any one that cannot be read before ownership is known —
a malformed name, a repeated number, text that is not UTF-8 — refuses the
whole archive. Excluding it could make another directory look unique.
"""

import io
import re
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass

from deployer.stepbinding import StepDirectory, StepFile

MIB = 2**20
_CHUNK = 64 * 1024
_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_STEP_NAME_RE = re.compile(r"(0|[1-9][0-9]*)_[^/]*\.txt", re.DOTALL)
_SYSTEM = "system.txt"


@dataclass(frozen=True)
class ArchiveLimits:
    """Spec §7.1: constants, lowered only by tests."""

    download: int = 64 * MIB
    entries: int = 4096
    entry: int = 64 * MIB
    total: int = 256 * MIB


@dataclass(frozen=True)
class ArchiveRefused:
    """The archive cannot be read whole; ``reason`` says why (state ``refused``)."""

    reason: str


def read_step_directories(
    blob: bytes, limits: ArchiveLimits = ArchiveLimits()
) -> tuple[StepDirectory, ...] | ArchiveRefused:
    """The archive's step directories, or why the archive is refused.

    An empty tuple means the archive holds no step directory (``absent``).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            return _read(archive, limits)
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as exc:
        return ArchiveRefused(f"corrupt archive: {exc}")


def _read(
    archive: zipfile.ZipFile, limits: ArchiveLimits
) -> tuple[StepDirectory, ...] | ArchiveRefused:
    infos = archive.infolist()
    if len(infos) > limits.entries:
        return ArchiveRefused(f"{len(infos)} entries exceed the limit of {limits.entries}")
    repeated = sorted(n for n, c in Counter(i.filename for i in infos).items() if c > 1)
    if repeated:
        return ArchiveRefused(f"duplicate entry name(s): {repeated}")
    for info in infos:
        if info.flag_bits & 0x1:
            return ArchiveRefused(f"encrypted entry: {info.filename!r}")
        if info.compress_type not in _METHODS:
            return ArchiveRefused(
                f"unsupported compression method {info.compress_type}: {info.filename!r}"
            )
        if info.file_size > limits.entry:
            return ArchiveRefused(
                f"entry {info.filename!r} exceeds {limits.entry} bytes uncompressed"
            )
    groups: dict[str, list[zipfile.ZipInfo]] = {}
    for info in infos:
        head, sep, rest = info.filename.partition("/")
        if sep and rest and rest != _SYSTEM and not info.is_dir():
            groups.setdefault(head, []).append(info)
    directories: list[StepDirectory] = []
    budget = limits.total
    for name in sorted(groups):
        files: list[StepFile] = []
        for info in groups[name]:
            match = _STEP_NAME_RE.fullmatch(info.filename.partition("/")[2])
            if match is None:
                return ArchiveRefused(f"step entry {info.filename!r} is not <N>_<name>.txt")
            data = _read_entry(archive, info, limits.entry, budget)
            if isinstance(data, ArchiveRefused):
                return data
            budget -= len(data)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return ArchiveRefused(f"step file {info.filename!r} is not valid UTF-8")
            files.append(StepFile(int(match.group(1)), text))
        numbers = Counter(f.number for f in files)
        twice = sorted(n for n, c in numbers.items() if c > 1)
        if twice:
            return ArchiveRefused(f"directory {name!r} repeats step number(s) {twice}")
        directories.append(StepDirectory(name, tuple(sorted(files, key=lambda f: f.number))))
    return tuple(directories)


def _read_entry(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, entry_limit: int, budget: int
) -> bytes | ArchiveRefused:
    """One entry, decompressed in bounded chunks against both limits."""
    chunks: list[bytes] = []
    size = 0
    with archive.open(info) as stream:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > entry_limit:
                return ArchiveRefused(
                    f"entry {info.filename!r} exceeds {entry_limit} bytes uncompressed"
                )
            if size > budget:
                return ArchiveRefused("entries exceed the total uncompressed limit")
            chunks.append(chunk)
    return b"".join(chunks)
```

The directory-entry case: `info.is_dir()` is true for `s1-plain-fail/`, and its `rest`
is empty, so it is skipped.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_logarchive.py -v`
Expected: all PASS. For `sub/3_x.txt`, `_STEP_NAME_RE` (no `/` allowed) refuses it.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/logarchive.py tests/test_logarchive.py
git commit -m "feat(logarchive): bounded in-memory reader; unreadable before ownership refuses"
```

---

### Task 4: `api_bytes_capped`, a download limit that limits the download

**Files:**
- Modify: `src/deployer/forge.py` (`OverCap`, `GhCappedBytesRunner`, `SubprocessGh.__init__`, `SubprocessGh.api_bytes_capped`, reader threads)
- Test: `tests/test_forge_capped.py`

**Interfaces:**
- Produces, in `deployer.forge`:
  - `OverCap(max_bytes: int)`
  - `@runtime_checkable class GhCappedBytesRunner(GhRunner, Protocol): def api_bytes_capped(self, argv: list[str], *, timeout: float, max_bytes: int) -> bytes | OverCap`
  - `SubprocessGh(command: Sequence[str] = ("gh",))`, where `command` is the program prefix before `api`. All three methods use it.
  - `STDERR_TAIL_BYTES = 64 * 1024`

- [ ] **Step 1: Write the failing tests (real child processes)**

`tests/test_forge_capped.py`:

```python
"""``SubprocessGh.api_bytes_capped`` against real child processes (spec §7.2).

No network: the "gh" is ``python -c <script>``. Each child records its pid in
``$PIDFILE`` first, so the test can prove it was reaped, not left a zombie.
"""

import os
import sys
import time
from pathlib import Path

import pytest

from deployer.forge import GhCappedBytesRunner, GhError, OverCap, SubprocessGh

PRELUDE = "import os, sys, time, signal\nopen(os.environ['PIDFILE'], 'w').write(str(os.getpid()))\n"


def _gh(script: str) -> SubprocessGh:
    return SubprocessGh(command=(sys.executable, "-c", PRELUDE + script))


@pytest.fixture()
def pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pid"
    monkeypatch.setenv("PIDFILE", str(path))
    return path


def _assert_reaped(pidfile: Path) -> None:
    pid = int(pidfile.read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # already reaped: not our child any more


def test_subprocess_gh_is_a_capped_runner() -> None:
    assert isinstance(SubprocessGh(), GhCappedBytesRunner)


def test_bytes_come_back_whole(pidfile: Path) -> None:
    gh = _gh("sys.stdout.buffer.write(b'PK' + b'x' * 1000)")
    assert gh.api_bytes_capped(["x"], timeout=10, max_bytes=2000) == b"PK" + b"x" * 1000
    _assert_reaped(pidfile)


def test_exactly_the_cap_is_not_over_it(pidfile: Path) -> None:
    gh = _gh("sys.stdout.buffer.write(b'x' * 5000)")
    assert gh.api_bytes_capped(["x"], timeout=10, max_bytes=5000) == b"x" * 5000


def test_an_endless_stdout_stops_at_the_cap(pidfile: Path) -> None:
    gh = _gh("while True:\n    sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush()")
    start = time.monotonic()
    assert gh.api_bytes_capped(["x"], timeout=30, max_bytes=200_000) == OverCap(200_000)
    assert time.monotonic() - start < 10
    _assert_reaped(pidfile)


def test_a_fast_exit_over_the_cap_is_still_over_it(pidfile: Path) -> None:
    gh = _gh("sys.stdout.buffer.write(b'x' * 100_001)")
    assert gh.api_bytes_capped(["x"], timeout=10, max_bytes=100_000) == OverCap(100_000)
    _assert_reaped(pidfile)


def test_a_stderr_flood_neither_blocks_nor_grows(pidfile: Path) -> None:
    gh = _gh("sys.stderr.buffer.write(b'e' * 2_000_000); sys.stdout.buffer.write(b'ok')")
    assert gh.api_bytes_capped(["x"], timeout=10, max_bytes=100) == b"ok"
    _assert_reaped(pidfile)


def test_a_failing_exit_maps_its_status_from_the_stderr_tail(pidfile: Path) -> None:
    gh = _gh(
        "sys.stderr.buffer.write(b'e' * 2_000_000 + b' (HTTP 404)'); sys.exit(1)"
    )
    with pytest.raises(GhError) as caught:
        gh.api_bytes_capped(["x"], timeout=10, max_bytes=100)
    assert caught.value.status == 404
    assert len(str(caught.value)) < 70_000


def test_a_silent_child_times_out(pidfile: Path) -> None:
    gh = _gh("time.sleep(60)")
    start = time.monotonic()
    with pytest.raises(GhError) as caught:
        gh.api_bytes_capped(["x"], timeout=0.5, max_bytes=100)
    assert caught.value.status is None and "timed out" in str(caught.value)
    assert time.monotonic() - start < 0.5 + 2 + 3
    _assert_reaped(pidfile)


def test_a_child_ignoring_sigterm_is_killed(pidfile: Path) -> None:
    gh = _gh("signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)")
    start = time.monotonic()
    with pytest.raises(GhError):
        gh.api_bytes_capped(["x"], timeout=0.5, max_bytes=100)
    assert time.monotonic() - start < 0.5 + 2 + 3
    _assert_reaped(pidfile)


def test_a_missing_program_is_a_status_less_error() -> None:
    gh = SubprocessGh(command=("/nonexistent/gh-binary",))
    with pytest.raises(GhError) as caught:
        gh.api_bytes_capped(["x"], timeout=5, max_bytes=100)
    assert caught.value.status is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_forge_capped.py -v`
Expected: `ImportError: cannot import name 'GhCappedBytesRunner'`.

- [ ] **Step 3: Implement**

In `src/deployer/forge.py`, add the imports `import threading`, `import time`, and
`runtime_checkable` to the `typing` import. Then add the following after
`GhBytesRunner`:

```python
@dataclass(frozen=True)
class OverCap:
    """``api_bytes_capped`` stopped reading: more than ``max_bytes`` arrived."""

    max_bytes: int


@runtime_checkable
class GhCappedBytesRunner(GhRunner, Protocol):
    """A runner whose binary download stops at a byte cap (spec §7.2)."""

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        """Stdout bytes, or :class:`OverCap`; raise :class:`GhError` on failure."""
        ...


STDERR_TAIL_BYTES = 64 * 1024
"""How much of a capped call's stderr is kept: its tail, for the error message."""
_CAPPED_CHUNK = 64 * 1024
_TERMINATE_GRACE_S = 2.0
_READER_JOIN_S = 5.0
_POLL_S = 0.05


class _StdoutReader(threading.Thread):
    """Reads stdout in chunks until EOF or until more than ``max_bytes`` arrived."""

    def __init__(self, fd: int, max_bytes: int) -> None:
        super().__init__(daemon=True)
        self._fd = fd
        self._max = max_bytes
        self.chunks: list[bytes] = []
        self.size = 0
        self.over = threading.Event()
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            while chunk := os.read(self._fd, _CAPPED_CHUNK):
                self.size += len(chunk)
                self.chunks.append(chunk)  # at most the cap plus one chunk
                if self.size > self._max:
                    self.over.set()
                    return
        except Exception as exc:  # surfaced by the caller, never swallowed
            self.error = exc


class _StderrTail(threading.Thread):
    """Drains stderr to EOF, keeping only its last ``STDERR_TAIL_BYTES``."""

    def __init__(self, fd: int) -> None:
        super().__init__(daemon=True)
        self._fd = fd
        self.tail = bytearray()
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            while chunk := os.read(self._fd, _CAPPED_CHUNK):
                self.tail += chunk
                del self.tail[:-STDERR_TAIL_BYTES]
        except Exception as exc:
            self.error = exc
```

In `SubprocessGh`, add

```python
    def __init__(self, command: Sequence[str] = ("gh",)) -> None:
        """``command`` is the program run before ``api`` (``gh`` in production)."""
        self._command = tuple(command)
```

and change `cmd = ["gh", "api", *argv]` to `cmd = [*self._command, "api", *argv]` in both
`api` and `api_bytes`. Then add:

```python
    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        """``gh api *argv`` stdout, stopping once more than ``max_bytes`` arrived.

        The whole lifecycle is owned here (spec §7.2): two reader threads, one
        deadline in the calling thread, terminate → grace → kill → reap, and
        both readers finished before any result is chosen, so a child that
        exits before its last bytes are read can neither hide an exceeded cap
        nor return partial output.
        """
        cmd = [*self._command, "api", *argv]
        what = " ".join(argv)
        deadline = time.monotonic() + timeout
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_gh_env(),
            )
        except OSError as exc:
            raise GhError(f"gh api could not start: {exc}") from exc
        assert proc.stdout is not None and proc.stderr is not None
        out = _StdoutReader(proc.stdout.fileno(), max_bytes)
        err = _StderrTail(proc.stderr.fileno())
        out.start()
        err.start()
        try:
            ended = _await_exit(proc, out.over, deadline)
            if ended != "exited":
                _stop(proc)
            for reader in (out, err):
                reader.join(_READER_JOIN_S)
            if out.is_alive() or err.is_alive():
                raise GhError(f"gh api {what}: an output reader did not finish")
            if out.over.is_set():
                return OverCap(max_bytes)
            if ended == "deadline":
                raise GhError(f"gh api {what} timed out after {timeout}s")
            for failure in (out.error, err.error):
                if failure is not None:
                    raise GhError(f"gh api {what}: reading output failed: {failure}")
            if proc.returncode != 0:
                stderr = bytes(err.tail).decode("utf-8", errors="replace")
                raise _gh_failure(what, proc.returncode, stderr)
            return b"".join(out.chunks)
        finally:
            if proc.poll() is None:
                _stop(proc)
            proc.stdout.close()
            proc.stderr.close()
```

And module-level helpers next to `_gh_env`:

```python
def _await_exit(
    proc: subprocess.Popen[bytes], over: threading.Event, deadline: float
) -> Literal["exited", "over", "deadline"]:
    """Wait for the child, an exceeded cap or the deadline, whichever is first."""
    while True:
        if over.is_set():
            return "over"
        left = deadline - time.monotonic()
        if left <= 0:
            return "deadline"
        try:
            proc.wait(timeout=min(left, _POLL_S))
            return "exited"
        except subprocess.TimeoutExpired:
            continue


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Terminate, wait a grace period, then kill; always reap."""
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
```

- [ ] **Step 4: Run the tests (and the forge suite, for the `command` change)**

Run: `uv run pytest tests/test_forge_capped.py -v`
Expected: all 11 PASS within a few seconds each.
Run: `uv run pytest tests/test_forge.py -q`
Expected: PASS. The existing `SubprocessGh` tests monkeypatch `subprocess.run` and
still see `["gh", "api", …]`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/forge.py tests/test_forge_capped.py
git commit -m "feat(forge): api_bytes_capped — a download cap that stops the download"
```

---

### Task 5: Wiring, states and snapshot 1.5

**Files:**
- Modify: `src/deployer/forge.py` (`ArchiveStatus`, `StepBinding`, `FailedJob.step_binding`, `FailedRun.archive`, `LOG_ARCHIVE_TIMEOUT_S`, `_bind_steps`, `fetch_failed_run`, `build_failed_job(..., step_binding=None)`, `SNAPSHOT_SCHEMA_VERSION = "1.5"`, `dump_snapshot` docstring)
- Modify: `src/deployer/diagnose.py` (two docstring mentions of the nested snapshot's version: 1.4 → 1.5)
- Modify: `README.md` (the snapshot-version paragraph: `"1.4"` → `"1.5"`, plus one sentence about 1.5)
- Modify: `tests/test_forge.py` (the version literals `"1.4"` → `"1.5"` in the round-trip, positional and missing-version tests), `tests/test_diagnose.py` (its `"1.4"`)
- Test: `tests/test_forge_archive.py`

**Interfaces:**
- Consumes: `bind_jobs`, `JobBinding`, `StepBindingState` (Task 2); `read_step_directories`, `ArchiveLimits`, `ArchiveRefused` (Task 3); `GhCappedBytesRunner`, `OverCap` (Task 4).
- Produces: `forge.ArchiveStatus(state: Literal["available","absent","unavailable","refused"], reason: str | None = None)`, `forge.StepBinding(state: StepBindingState, reason: str | None = None)`, `FailedJob.step_binding: StepBinding | None = None`, `FailedRun.archive: ArchiveStatus | None = None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_forge_archive.py`:

```python
"""Archive states and the wiring in ``fetch_failed_run`` (spec §1, §4.1, §6, §7.4)."""

import json
from dataclasses import dataclass, field

import pytest

from deployer.forge import (
    ArchiveStatus,
    FailedRun,
    GhError,
    OverCap,
    RunRef,
    StepBinding,
    StepRef,
    dump_snapshot,
    fetch_failed_run,
    load_snapshot,
)
from deployer.reproduce.shape import job_text
from tests import step_binding_data as data
from tests.test_forge import _LOGS_RE, Call, FakeGh, job, step

TS = "2026-10-03T10:08:54.1234567Z "
LOG = f"\ufeff{TS}setup\n{TS}##[group]Run a\n{TS}a-cmd\n{TS}##[endgroup]\n{TS}a-out\n"
ARCHIVE_PATH = "repos/o/r/actions/runs/1/attempts/1/logs"


@dataclass
class ArchiveFakeGh(FakeGh):
    """``FakeGh`` plus a per-job log map and the capped archive endpoint."""

    archive: bytes | OverCap | GhError = b""
    logs_by_job: dict[int, str | GhError] = field(default_factory=dict)
    archive_calls: list[list[str]] = field(default_factory=list)

    def api(self, argv: list[str], *, timeout: float) -> str:
        match = _LOGS_RE.search(argv[-1])
        if match and int(match.group(1)) in self.logs_by_job:
            self.calls.append(Call(argv=list(argv), timeout=timeout))
            value = self.logs_by_job[int(match.group(1))]
            if isinstance(value, GhError):
                raise value
            return value
        return super().api(argv, timeout=timeout)

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        self.archive_calls.append(list(argv))
        if isinstance(self.archive, GhError):
            raise self.archive
        return self.archive


def _bound_archive() -> bytes:
    """A step directory whose two files add up to ``LOG``: step 1, then step 2."""
    return data.zip_of(
        [
            ("0_job-1.txt", LOG.encode()),
            ("job-1/system.txt", b"runner\n"),
            ("job-1/1_Set up job.txt", f"\ufeff{TS}setup\n".encode()),
            ("job-1/2_Run a.txt", f"\ufeff{TS}##[group]Run a\n{TS}a-cmd\n{TS}##[endgroup]\n{TS}a-out\n".encode()),
        ]
    )


def _gh(archive: bytes | OverCap | GhError) -> ArchiveFakeGh:
    gh = ArchiveFakeGh(archive=archive)
    gh.job_pages = [[job(1, steps=[step(1, "Set up job", "success"), step(2, "Run a")])]]
    gh.logs = LOG
    return gh


def _run(gh: FakeGh, attempt: int | None = 1) -> FailedRun:
    run = fetch_failed_run(RunRef("o/r", 1), attempt=attempt, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def test_a_runner_without_the_capped_download_attempts_nothing() -> None:
    run = _run(FakeGh())
    assert run.archive is None and run.jobs[0].step_binding is None


def test_a_bound_job_cites_its_step_and_keeps_its_job_text() -> None:
    gh = _gh(_bound_archive())
    run = _run(gh)
    assert run.archive == ArchiveStatus("available")
    assert run.jobs[0].step_binding == StepBinding("bound")
    by_text = {e.text: e.source for e in run.jobs[0].evidence if e.source != 1}
    assert by_text["a-out"] == StepRef(1, 2)
    plain = _run(FakeGh(job_pages=gh.job_pages, logs=LOG))
    assert job_text(run.jobs[0]) == job_text(plain.jobs[0])
    assert gh.archive_calls == [[ARCHIVE_PATH]]


def test_the_archive_request_names_the_resolved_attempt() -> None:
    gh = _gh(_bound_archive())
    gh.run = {**gh.run, "run_attempt": 2}
    run = _run(gh, attempt=None)
    assert run.attempt == 2
    assert gh.archive_calls == [["repos/o/r/actions/runs/1/attempts/2/logs"]]


def test_an_http_error_makes_the_archive_unavailable() -> None:
    run = _run(_gh(GhError("gh api … failed: Not Found (HTTP 404)", 404)))
    assert run.archive is not None and run.archive.state == "unavailable"
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "no_archive"


def test_a_status_less_error_propagates() -> None:
    with pytest.raises(GhError):
        _run(_gh(GhError("gh api … timed out after 120.0s")))


def test_an_archive_over_the_download_cap_is_refused() -> None:
    run = _run(_gh(OverCap(64 * 2**20)))
    assert run.archive is not None and run.archive.state == "refused"
    assert str(64 * 2**20) in (run.archive.reason or "")


def test_a_corrupt_archive_is_refused() -> None:
    run = _run(_gh(b"not a zip"))
    assert run.archive is not None and run.archive.state == "refused"
    assert run.jobs[0].step_binding == StepBinding("no_archive", "the log archive was refused")


def test_an_archive_without_step_files_is_absent_and_costs_no_extra_reads() -> None:
    gh = _gh(data.zip_of([("0_job-1.txt", LOG.encode()), ("job-1/system.txt", b"x\n")]))
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    run = _run(gh)
    assert run.archive is not None and run.archive.state == "absent"
    assert not [c for c in gh.calls if c.argv[-1].endswith("jobs/2/logs")]


def test_a_green_job_is_read_for_the_population_but_not_kept() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    gh.logs_by_job = {2: f"{TS}other job\n"}
    run = _run(gh)
    assert [j.job_id for j in run.jobs] == [1]
    assert [c for c in gh.calls if c.argv[-1].endswith("jobs/2/logs")]
    assert run.jobs[0].step_binding == StepBinding("bound")


def test_a_skipped_job_is_not_read() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(3, conclusion="skipped")]]
    _run(gh)
    assert not [c for c in gh.calls if c.argv[-1].endswith("jobs/3/logs")]


def test_an_unreadable_population_log_makes_binding_unverifiable() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="success")]]
    gh.logs_by_job = {2: GhError("gh api … failed (HTTP 502)", 502)}
    run = _run(gh)
    assert run.archive == ArchiveStatus("available")
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "unverifiable"
    assert all(e.source is None or e.source == 1 for e in run.jobs[0].evidence)


def test_a_job_that_never_started_makes_binding_unverifiable() -> None:
    gh = _gh(_bound_archive())
    gh.job_pages = [[*gh.job_pages[0], job(2, conclusion="cancelled")]]
    gh.logs_by_job = {2: GhError("gh api … failed (HTTP 404)", 404)}
    run = _run(gh)
    assert {j.job_id: j.step_binding.state for j in run.jobs if j.step_binding} == {
        1: "unverifiable",
        2: "unverifiable",
    }


def test_snapshot_1_5_round_trips_and_1_4_loads_as_not_attempted() -> None:
    run = _run(_gh(_bound_archive()))
    text = dump_snapshot(run)
    assert json.loads(text)["snapshot_schema_version"] == "1.5"
    assert load_snapshot(text) == run
    old = json.loads(text)
    old["snapshot_schema_version"] = "1.4"
    del old["archive"]
    for job_doc in old["jobs"]:
        del job_doc["step_binding"]
    restored = load_snapshot(json.dumps(old))
    assert restored.archive is None and restored.jobs[0].step_binding is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_forge_archive.py -v`
Expected: `ImportError: cannot import name 'ArchiveStatus'`.

- [ ] **Step 3: Add the models and bump the version**

In `src/deployer/forge.py`: import
`from deployer.logarchive import ArchiveLimits, ArchiveRefused, read_step_directories`
and extend the `stepbinding` import to
`from deployer.stepbinding import JobBinding, StepBindingState, StepSpan, bind_jobs`.
Set `SNAPSHOT_SCHEMA_VERSION = "1.5"`. Add
`LOG_ARCHIVE_TIMEOUT_S = 120.0` with the docstring
`"""Wall-clock budget for downloading one per-attempt log archive."""`. Above `FailedJob`,
add:

```python
ArchiveState = Literal["available", "absent", "unavailable", "refused"]


@dataclass(frozen=True)
class ArchiveStatus:
    """How the per-attempt log archive was read (snapshot 1.5, spec §6).

    ``None`` on a run means no archive was attempted: an older snapshot, or a
    runner without the capped download.
    """

    state: ArchiveState
    reason: str | None = None


@dataclass(frozen=True)
class StepBinding:
    """What step binding decided for one job (snapshot 1.5, spec §6)."""

    state: StepBindingState
    reason: str | None = None
```

Append `step_binding: StepBinding | None = None` as the last field of `FailedJob`. Add
`archive: ArchiveStatus | None = None` to `FailedRun` just before
`snapshot_schema_version`. Extend `dump_snapshot`'s docstring with: "Schema 1.5 adds
the run's ``archive`` and each job's ``step_binding`` (spec §6); additive, so a 1.4 or
older document loads with both ``None``, which means not attempted."

Add `step_binding: StepBinding | None = None` to `build_failed_job`'s keyword
parameters and pass it into `FailedJob(..., step_binding=step_binding)`.

- [ ] **Step 4: Wire `_bind_steps` into `fetch_failed_run`**

Replace the body of `fetch_failed_run` from `kept = [...]` up to the
`return FailedRun(` call with:

```python
    listing = gh.jobs(ref.run_id, resolved)
    kept = [job for job in listing if job.get("conclusion") not in _GREEN_CONCLUSIONS]
    reads: dict[int, tuple[str, LogsState]] = {}
    noted: dict[int, tuple[list[dict[str, Any]], AnnotationsState]] = {}
    for record in kept:
        job_id = int(record["id"])
        reads[job_id] = gh.logs(job_id)
        noted[job_id] = gh.annotations(job_id)
    archive, bindings = _bind_steps(runner, gh, ref, resolved, listing, reads)
    jobs: list[FailedJob] = []
    for record in kept:
        job_id = int(record["id"])
        log_text, logs_state = reads[job_id]
        annotations, annotations_state = noted[job_id]
        binding = bindings.get(job_id)
        jobs.append(
            build_failed_job(
                record,
                job_id,
                log_text,
                annotations,
                Completeness(logs=logs_state, annotations=annotations_state),
                spans=binding.spans if binding is not None else (),
                step_binding=(
                    StepBinding(binding.state, binding.reason)
                    if binding is not None
                    else None
                ),
            )
        )
```

Pass `archive=archive` into `FailedRun(...)`. `runner` is the resolved runner, so bind
`runner = runner if runner is not None else SubprocessGh()` first and build
`gh = _Gh(runner, ref.repo)` from it. Then add:

```python
def _bind_steps(
    runner: GhRunner,
    gh: "_Gh",
    ref: RunRef,
    attempt: int,
    listing: list[dict[str, Any]],
    reads: dict[int, tuple[str, LogsState]],
) -> tuple[ArchiveStatus | None, dict[int, JobBinding]]:
    """The archive's state and each kept job's binding (spec §4, §6, §7).

    A runner without the capped download attempts nothing. Only an
    ``available`` archive costs the population's extra log reads (§4.1).
    """
    if not isinstance(runner, GhCappedBytesRunner):
        return None, {}
    limits = ArchiveLimits()
    path = f"repos/{ref.repo}/actions/runs/{ref.run_id}/attempts/{attempt}/logs"
    try:
        blob = runner.api_bytes_capped(
            [path], timeout=LOG_ARCHIVE_TIMEOUT_S, max_bytes=limits.download
        )
    except GhError as exc:
        if exc.status is None:
            raise
        return ArchiveStatus("unavailable", str(exc)), _each(
            reads, "no_archive", "the log archive is unavailable"
        )
    if isinstance(blob, OverCap):
        return ArchiveStatus(
            "refused", f"the download exceeds {blob.max_bytes} bytes"
        ), _each(reads, "no_archive", "the log archive was refused")
    found = read_step_directories(blob, limits)
    if isinstance(found, ArchiveRefused):
        return ArchiveStatus("refused", found.reason), _each(
            reads, "no_archive", "the log archive was refused"
        )
    if not found:
        return ArchiveStatus("absent", "the archive holds no per-step files"), _each(
            reads, "no_archive", "the log archive holds no per-step files"
        )
    population = [r for r in listing if r.get("conclusion") != "skipped"]
    logs: dict[int, str] = {}
    for record in population:
        job_id = int(record["id"])
        text, state = reads[job_id] if job_id in reads else gh.logs(job_id)
        if state != "present":
            return ArchiveStatus("available"), _each(
                reads, "unverifiable", f"the log of job {job_id} is {state}"
            )
        logs[job_id] = text
    numbers = {
        int(r["id"]): [int(s["number"]) for s in r.get("steps") or []]
        for r in population
    }
    bound = bind_jobs(logs, numbers, found)
    return ArchiveStatus("available"), {job_id: bound[job_id] for job_id in reads}


def _each(
    reads: dict[int, tuple[str, LogsState]], state: StepBindingState, reason: str
) -> dict[int, JobBinding]:
    """The same binding state for every kept job."""
    return {job_id: JobBinding(state, reason) for job_id in reads}
```

In the cancelled-job test, the cancelled job is not green, so it is kept: its own read
(404) is not `present`, and every kept job reads `unverifiable`. That is the expected
outcome of Review Focus item 3.

- [ ] **Step 5: Update the version literals and the docs**

In `tests/test_forge.py`, change `"1.4"` to `"1.5"` in
`test_snapshot_round_trips_through_versioned_json`,
`test_snapshot_types_construct_positionally` and
`test_a_document_without_a_version_loads_as_the_current_one`. Leave
`test_a_1_3_snapshot_keeps_its_recorded_step_bindings` alone. In `tests/test_diagnose.py`,
change `document["run"]["snapshot_schema_version"] == "1.4"` to `"1.5"`. In
`src/deployer/diagnose.py`, change both "``run`` snapshot is schema 1.4 either way" to
1.5. In `README.md`, change `` `snapshot_schema_version` (`"1.4"`) `` and "schema 1.4
either way" to 1.5, and append after the 1.4 sentence: "1.5 adds the run's `archive`
(how the per-attempt log archive was read: `available`, `absent`, `unavailable` or
`refused`) and each job's `step_binding` (`bound`, `no_archive`, `unverifiable`,
`unmatched`, `ambiguous` or `malformed`). Where a job is `bound`, its log blocks carry
the `StepRef` the runner's own per-step files prove. Both fields are `null` on older
snapshots: not attempted."

- [ ] **Step 6: Run the tests and the whole suite**

Run: `uv run pytest tests/test_forge_archive.py -v`
Expected: all PASS.
Run: `uv run pytest -q`
Expected: all pass. Fakes without `api_bytes_capped` attempt no archive, so every
existing test is unaffected apart from the version literals.

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/forge.py src/deployer/diagnose.py README.md tests/test_forge.py tests/test_diagnose.py tests/test_forge_archive.py
git commit -m "feat(forge): bind steps from the per-attempt archive; snapshot 1.5 states"
```

---

### Task 6: Acceptance on `steps-1`, the end-to-end synthetic cases, provenance and TODO

**Files:**
- Test: `tests/test_step_binding_acceptance.py`
- Modify: `tests/fixtures/step-binding/PROVENANCE.md` (a "Synthetic cases" section), `tests/fixtures/step-binding/CHECKSUMS.sha256` (re-hash, because PROVENANCE is checksummed)
- Modify: `TODO.md`

**Interfaces:**
- Consumes: everything above, plus `diagnose.diagnose_run` and `reproduce.shape.job_text`.

- [ ] **Step 1: Write the acceptance tests**

`tests/test_step_binding_acceptance.py`:

```python
"""Acceptance on the real ``steps-1`` recording (spec §9.1) and end-to-end
synthetic cases derived from it (§9.2), through ``fetch_failed_run``."""

from deployer.diagnose import diagnose_run
from deployer.forge import FailedRun, OverCap, RunRef, StepRef, fetch_failed_run
from deployer.reproduce.shape import job_text
from tests import step_binding_data as data

REPO = "andrei-shtanakov/deployer"
RUN = 37115427715
JOB_LEVEL_NOTE = "cited evidence is job-level (no step binding)"


class PlainReplay:
    """Serves the recorded ``gh api`` calls by path. No capped download, so
    ``fetch_failed_run`` attempts no archive. Annotations were not recorded
    (``read_attempt`` does not read them), so they are served empty: a
    labelled gap."""

    def __init__(self, archive: bytes, logs: dict[int, str] | None = None) -> None:
        self._by_path = {c["argv"][-1]: c["stdout"] for c in data.calls() if "stdout" in c}
        self._logs = logs or {}
        self._archive = archive
        self.paths: list[str] = []

    def api(self, argv: list[str], *, timeout: float) -> str:
        path = argv[-1]
        self.paths.append(path)
        if "/check-runs/" in path:
            return "[]"
        job_id = path.rsplit("/", 2)[-2] if path.endswith("/logs") else None
        if job_id is not None and int(job_id) in self._logs:
            return self._logs[int(job_id)]
        return self._by_path[path]


class Replay(PlainReplay):
    """``PlainReplay`` plus ``archive`` as the per-attempt log archive."""

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        self.paths.append(argv[-1])
        return self._archive


def _fetch(gh: PlainReplay) -> FailedRun:
    run = fetch_failed_run(RunRef(REPO, RUN), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def _recorded() -> bytes:
    return (data.STEPS_1 / "attempt-1.zip").read_bytes()


def test_every_preregistered_line_lands_at_its_step() -> None:
    run = _fetch(Replay(_recorded()))
    assert run.archive is not None and run.archive.state == "available"
    by_name = {j.name: j for j in run.jobs}
    assert {j.step_binding.state for j in run.jobs if j.step_binding} == {"bound"}
    for job_name, line, number in data.preregistered():
        kept = by_name[job_name]
        holders = [e for e in kept.evidence if line in e.text.split("\n")]
        assert len(holders) == 1, (job_name, line)
        assert holders[0].source == StepRef(kept.job_id, number), (job_name, line)


def test_job_text_is_unchanged_by_binding() -> None:
    bound = {j.job_id: job_text(j) for j in _fetch(Replay(_recorded())).jobs}
    plain = {j.job_id: job_text(j) for j in _fetch(PlainReplay(_recorded())).jobs}
    assert bound == plain


def test_each_failed_step_of_s3_cites_only_its_own_line() -> None:
    run = _fetch(Replay(_recorded()))
    diagnosis = diagnose_run(run)
    (s3,) = [j for j in run.jobs if j.name == "s3-two-failures"]
    verdicts = {
        v.where.number: v
        for v in diagnosis.failures
        if isinstance(v.where, StepRef) and v.where.job_id == s3.job_id
    }
    cited = {n: "\n".join(e.text for e in v.evidence) for n, v in verdicts.items()}
    assert "AssertionError: probe-s3" in cited[3] and "exec format error" not in cited[3]
    assert "exec format error" in cited[4] and "AssertionError: probe-s3" not in cited[4]
    assert all(JOB_LEVEL_NOTE not in v.observations for v in verdicts.values())


def test_every_request_names_the_chosen_attempt() -> None:
    gh = Replay(_recorded())
    _fetch(gh)
    runs = [p for p in gh.paths if "/actions/runs/" in p]
    assert runs and all(f"/runs/{RUN}/attempts/1" in p for p in runs)


def test_another_runners_archive_binds_nothing() -> None:
    blob = data.zip_of(data.foreign_runner(data.archive_entries()))
    run = _fetch(Replay(blob))
    assert {j.step_binding.state for j in run.jobs if j.step_binding} == {"unmatched"}
    assert all(not isinstance(e.source, StepRef) for j in run.jobs for e in j.evidence)


def test_a_missing_step_file_unbinds_only_its_job() -> None:
    blob = data.zip_of(data.without(data.archive_entries(), "s2-named/3_"))
    states = {j.name: j.step_binding.state for j in _fetch(Replay(blob)).jobs if j.step_binding}
    assert states.pop("s2-named") == "unmatched"
    assert set(states.values()) == {"bound"}


def test_identical_job_logs_bind_neither() -> None:
    ids = {name: r["id"] for name, r in data.job_records().items()}
    logs = data.job_logs()
    run = _fetch(Replay(_recorded(), {ids["s5-spoof"]: logs["s6-dupes"]}))
    states = {j.name: j.step_binding.state for j in run.jobs if j.step_binding}
    assert states["s5-spoof"] == "ambiguous" and states["s6-dupes"] == "ambiguous"
    assert states["s1-plain-fail"] == "bound"
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_step_binding_acceptance.py -v`
Expected: all PASS. `PlainReplay` has no `api_bytes_capped`, so it is not a
`GhCappedBytesRunner` and attempts no archive.

- [ ] **Step 3: Record the synthetic cases in PROVENANCE and re-hash**

Append to `tests/fixtures/step-binding/PROVENANCE.md`:

```markdown
## Synthetic cases

The step-binding tests also run on cases derived from `steps-1`. They are built at test
time by named functions in `tests/step_binding_data.py` (`renamed`, `without`, `edited`,
`foreign_runner`, `zip_of`), and no derived file is committed. Every derived case is a
transformation of the recorded entries or logs, named by its test. None of them is a
recording. `foreign_runner` replaces the `Worker ID` and temporary `HOME` lines, the
lines that differ between real runs, as a stand-in for another attempt's archive; it is
not one. Annotations were not recorded, because `read_attempt` does not read them, so
the acceptance replay serves them empty.
```

Then re-hash, from the repository root:

```bash
cd tests/fixtures/step-binding && find . -type f ! -name CHECKSUMS.sha256 ! -name record_steps.py ! -path '*__pycache__*' | sed 's|^\./||' | LC_ALL=C sort | xargs shasum -a 256 > CHECKSUMS.sha256 && cd -
uv run pytest tests/test_step_binding_recording.py -q
```

Expected: PASS.

- [ ] **Step 4: TODO**

In `TODO.md`, move `forge-step-level-log-binding` to `## Shipped`, as `- [x]`, with a
`Fixed:` line naming the spec, snapshot 1.5 and the acceptance. Keep its two sibling
sentences as new open items under the original section:

```markdown
- [ ] "Nothing was fetched" as a `Completeness` state of its own instead of being inferred from `jobs == []` in `diagnose_run` @owner:repo:deployer @id:forge-nothing-fetched-state @epic:eco.dark-factory
- [ ] Record a real repeated attempt of the step-binding polygon run (`steps-1b`, spec §9.3) @owner:github:andrei-shtanakov @id:step-binding-rerun-recording @trigger:"the owner permits one rerun of 37115427715" @epic:eco.dark-factory
  Without it, the repeated-attempt check is synthetic only (spec §9.2, "attempt mixing").
```

- [ ] **Step 5: Full verification and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q
git add tests/test_step_binding_acceptance.py tests/fixtures/step-binding/PROVENANCE.md tests/fixtures/step-binding/CHECKSUMS.sha256 TODO.md
git commit -m "test(step-binding): acceptance on steps-1 and end-to-end synthetic cases"
```

Expected: the full suite passes; record the passed count in the PR.
