# Forge Not-Executed Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A kept job of the recorded "cancelled before execution" form leaves the binding
population, gets no failure verdict, and its expected 404 does not degrade completeness.
The job, its cancellation and its log read all stay visible in the snapshot.

**Architecture:** A new pure module, `notexecuted.py`, holds the `NotExecuted` type (strict
fields), `recognise(record, log_status)` and `basis_holds(ne)`. `forge` carries the HTTP
status of every log read structurally (`LogRead`), records `FailedJob.not_executed`
(snapshot 1.6), excludes recognised jobs from `_bind_steps`' population and from the
run-level logs worst-of, and offers `is_not_executed(job)`. `diagnose` evaluates only the
jobs it does not recognise. The exemption covers the logs dimension only, run-level
explanations follow it, and "nothing left" gets an explicit conservative result.

**Tech Stack:** Python 3.12, pydantic `TypeAdapter` + `Strict` (existing dependency), pytest,
uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-10-04-forge-not-executed-jobs-design.md` (rev 2.1).
Cited as §N. It builds on the archive step-binding design, cited as **B**.

## Global Constraints

- Run everything through `uv`: `uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`. Never pip.
- Line length 88; type hints everywhere; public functions get docstrings.
- **The supported form (§2), all required, with no defaults that turn missing or ill-typed values into matching ones:**
  - `status` is the str `"completed"` and `conclusion` is the str `"cancelled"`;
  - `runner_id` is an `int` (not a bool) equal to `0`;
  - `runner_name` is the str `""` (`null` and absent do not match);
  - `steps` is a present list equal to `[]`;
  - `created_at` and `started_at` are both strs matching `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` that parse as a valid UTC instant, and are equal;
  - the job's own log read failed with HTTP status `404` (an `int`).
- The HTTP status reaches recognition structurally (`LogRead.status`, from `GhError.status`). It is never re-derived from text, and logs are never read twice (§3.1).
- `NotExecuted`'s `int` fields are `Annotated[int, Strict()]` and its `str` fields `Annotated[str, Strict()]`. `status` and `conclusion` are `Literal`, because `Strict()` cannot apply to a `Literal` and a `Literal` is exact already. The strictness is scoped to `NotExecuted`, and every other snapshot field keeps its lax loading (§3.3).
- Diagnose consumes `not_executed` only through `is_not_executed(job)` (§3.3). A stored value that fails the predicate is ignored and the job takes the ordinary path, with the observation `job <id>: stored not_executed contradicts the job; ignored`.
- The exemption covers **only the recognised job's logs dimension**. Annotations are never exempt. Run-level explanations follow the same policy (§6).
- A recognised job's `step_binding` is `StepBinding("excluded", "cancelled before execution")` whatever the archive state, and also when no archive was attempted (§4).
- §6.1 applies only when at least one job was kept and every kept job is recognised. The zero-kept path is unchanged.
- Status-less failures of the main reads stay fatal. The archive-path degradation policy of B is not widened.
- `SNAPSHOT_SCHEMA_VERSION` becomes `"1.6"`, additive. `verdict_schema_version` does not change.
- Unchanged: `read_attempt`'s semantics (it only carries `LogRead` internally), the logic of `reproduce`, `fix` and `admission`, and `precheck`'s order.
- **Ruling (spec §9.1 wording):** "the real failures cite their own step's lines" is provable only if diagnose's rules observe something in those lines, and `MARK-…`/`##[error]Process completed…` may match no rule. The acceptance therefore asserts the provable, equally strict form. Each job's `MARK-<leg>` lies in its own step-3 evidence (`StepRef(job, 3)`), and every block any verdict cites has the source of that verdict's own step. Spec rev 2.2 says the same.
- The work happens on the branch `feat/forge-not-executed-jobs`, which already holds the spec and master @ `0c1152d`.

## Review Focus

1. **A recognised job whose log read is retried and returns 404 twice.** No retries exist, and the log is read once (§3.1). Expected: one read and one `LogRead`. Task 3's wiring test asserts a single log request for the job.
2. **A snapshot from 1.5 or older loaded by the 1.6 code.** `not_executed` loads as `None`, and diagnose behaves exactly as before. Task 4, `test_a_1_5_snapshot_diagnoses_exactly_as_before`.
3. **A matrix where the recognised sibling is also the only failed job's twin by name.** Names prove nothing for recognition: only the record and the read count. Task 2's recogniser ignores `name`, and a test with an identical name passes.
4. **A recognised job carrying annotation evidence.** The evidence stays in `job.evidence`, no verdict cites it, and the predicate still holds, because annotation sources are the job id. Task 4.
5. **A failed run whose only non-green jobs are a recognised job and a job with `steps: []` and a runner** (the dispatcher shape). The dispatcher job is evaluated normally, so §6.1 must not fire. Task 4.

---

### Task 1: `LogRead`, the HTTP status of a log read, structurally

**Files:**
- Modify: `src/deployer/forge.py`: add `LogRead`; `_Gh.logs` returns it; adapt `fetch_failed_run`, `_bind_steps`, `_each` and `_read_all_jobs`.
- Test: `tests/test_forge.py`

**Interfaces:**
- Produces: `forge.LogRead(text: str, state: LogsState, status: int | None = None)`, frozen. `_Gh.logs(job_id) -> LogRead`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_forge.py`):

```python
def test_a_log_read_carries_its_http_status(fake_gh):
    """§3.1: the status of a failed log read reaches the caller as data."""
    from deployer.forge import _Gh

    fake_gh.logs = GhError("gh api … failed: gh: HTTP 404", status=404)
    read = _Gh(fake_gh, "o/r").logs(1)
    assert (read.text, read.state, read.status) == ("", "error", 404)
    fake_gh.logs = GhError("gh: Gone (HTTP 410)", status=410)
    assert _Gh(fake_gh, "o/r").logs(1).status == 410
    fake_gh.logs = "a log\n"
    read = _Gh(fake_gh, "o/r").logs(1)
    assert (read.text, read.state, read.status) == ("a log\n", "present", None)
```

- [ ] **Step 2: Run it.** `uv run pytest tests/test_forge.py::test_a_log_read_carries_its_http_status -q`. Expected: FAIL, with `AttributeError: 'tuple' object has no attribute 'text'`.

- [ ] **Step 3: Implement.** In `forge.py`, after `LogsState`:

```python
@dataclass(frozen=True)
class LogRead:
    """One job-log read: its text, what its absence means, and the HTTP status
    of a failed read (``None`` when it succeeded). The status is kept as data
    (not-executed spec §3.1), never re-derived from a message."""

    text: str
    state: LogsState
    status: int | None = None
```

`_Gh.logs` returns `LogRead(text, "present")`, `LogRead("", "unavailable")` (for empty text),
or `LogRead("", "unavailable" if exc.status == 410 else "error", exc.status)`. It still
re-raises when `exc.status is None`, and its docstring names the return type. Adapt the
callers mechanically:
- `fetch_failed_run`: `reads: dict[int, LogRead]`, then `read = reads[job_id]` and
  `read.text`/`read.state`.
- `_bind_steps`: change the parameter `reads: dict[int, LogRead]`. Its loops use
  `read.text`/`read.state`, and the green read becomes `read = gh.logs(job_id)`.
- `_each`: `reads: Mapping[int, LogRead]`.
- `_read_all_jobs`: `read = gh.logs(job_id)`.

Behaviour is unchanged.

- [ ] **Step 4: Run** `uv run pytest -q`. Expected: the new test passes and the whole suite passes (pure plumbing).

- [ ] **Step 5: Lint, type-check, commit.**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/forge.py tests/test_forge.py
git commit -m "refactor(forge): a log read carries its HTTP status (LogRead)"
```

---

### Task 2: `notexecuted.py`: the type, recognition and the stored-basis check (pure)

**Files:**
- Create: `src/deployer/notexecuted.py`
- Test: `tests/test_notexecuted.py`

**Interfaces:**
- Produces: `NotExecuted` (frozen dataclass: `status, conclusion, runner_id, runner_name, steps, created_at, started_at, log_status`, strict as in Global Constraints), `recognise(record: object, log_status: int | None) -> NotExecuted | None` and `basis_holds(ne: NotExecuted) -> bool`.

- [ ] **Step 1: Write the failing tests.** `tests/test_notexecuted.py`. The base record is the real `steps-2` `waiting-legs (never-starts)` record, read from the fixture:

```python
"""Recognising a job cancelled before execution (spec §2, §3.2), pure."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from deployer.notexecuted import NotExecuted, basis_holds, recognise

CASE = Path(__file__).parent / "fixtures" / "step-binding" / "steps-2"
TS = "2026-10-04T09:13:25Z"


def _never_starts() -> dict[str, Any]:
    for call in json.loads((CASE / "gh-calls.json").read_text()):
        if "/jobs?" in call["argv"][-1] and "stdout" in call:
            for job in json.loads(call["stdout"])["jobs"]:
                if job["name"] == "waiting-legs (never-starts)":
                    return job
    raise AssertionError("never-starts not recorded")


def test_the_recorded_form_is_recognised_with_every_basis() -> None:
    assert recognise(_never_starts(), 404) == NotExecuted(
        "completed", "cancelled", 0, "", 0, TS, TS, 404
    )


def test_the_name_proves_nothing() -> None:
    record = _never_starts()
    record["name"] = "parallel-legs (fail-fast)"
    assert recognise(record, 404) is not None


def _without(record: dict[str, Any], key: str) -> dict[str, Any]:
    record = copy.deepcopy(record)
    del record[key]
    return record


def _with(key: str, value: object) -> dict[str, Any]:
    record = _never_starts()
    record[key] = value
    return record


@pytest.mark.parametrize(
    ("record", "log_status"),
    [
        (_with("status", "in_progress"), 404),
        (_with("conclusion", "failure"), 404),
        (_with("runner_id", 1), 404),
        (_with("runner_id", "0"), 404),
        (_with("runner_id", False), 404),
        (_with("runner_name", "GitHub Actions 1"), 404),
        (_with("runner_name", None), 404),
        (_without(_never_starts(), "runner_name"), 404),
        (_with("steps", [{"number": 1, "name": "x", "conclusion": None}]), 404),
        (_without(_never_starts(), "steps"), 404),
        (_with("started_at", "2026-10-04T09:13:26Z"), 404),
        (_without(_never_starts(), "started_at"), 404),
        (_without(_never_starts(), "created_at"), 404),
        ({**_never_starts(), "created_at": "", "started_at": ""}, 404),
        ({**_never_starts(), "created_at": "unknown", "started_at": "unknown"}, 404),
        ({**_never_starts(), "created_at": None, "started_at": None}, 404),
        ({**_never_starts(), "created_at": 0, "started_at": 0}, 404),
        (
            {
                **_never_starts(),
                "created_at": "2026-10-04T09:13:25.000Z",
                "started_at": "2026-10-04T09:13:25.000Z",
            },
            404,
        ),
        (
            {
                **_never_starts(),
                "created_at": "2026-10-04T09:13:25+00:00",
                "started_at": "2026-10-04T09:13:25+00:00",
            },
            404,
        ),
        (
            {
                **_never_starts(),
                "created_at": "2026-02-30T09:13:25Z",
                "started_at": "2026-02-30T09:13:25Z",
            },
            404,
        ),
        (_never_starts(), None),
        (_never_starts(), 410),
        (_never_starts(), 502),
        ("not a record", 404),
    ],
)
def test_any_broken_signal_is_not_recognised(record: object, log_status: int | None) -> None:
    assert recognise(record, log_status) is None


def test_a_dispatcher_shape_is_not_recognised() -> None:
    record = _never_starts()
    record["runner_id"] = 1000028882
    record["runner_name"] = "GitHub Actions 1000028882"
    assert recognise(record, 404) is None


def test_basis_holds_on_the_recorded_values_only() -> None:
    good = NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 404)
    assert basis_holds(good)
    for bad in (
        NotExecuted("completed", "cancelled", 1, "", 0, TS, TS, 404),
        NotExecuted("completed", "cancelled", 0, "x", 0, TS, TS, 404),
        NotExecuted("completed", "cancelled", 0, "", 1, TS, TS, 404),
        NotExecuted("completed", "cancelled", 0, "", 0, TS, "2026-10-04T09:13:26Z", 404),
        NotExecuted("completed", "cancelled", 0, "", 0, "", "", 404),
        NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 410),
    ):
        assert not basis_holds(bad), bad
```

- [ ] **Step 2: Run.** `uv run pytest tests/test_notexecuted.py -q`. Expected: `ModuleNotFoundError: No module named 'deployer.notexecuted'`.

- [ ] **Step 3: Implement** `src/deployer/notexecuted.py`:

```python
"""Recognising a job cancelled before execution.

Design: ``docs/superpowers/specs/2026-10-04-forge-not-executed-jobs-design.md``
§2–§3. Pure: no I/O. The recognised form is the one recorded on a real run
(``steps-2``); it is the supported basis for treating a job as cancelled
before execution, not a universal proof. Anything else is not recognised.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Strict

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class NotExecuted:
    """Every basis of the decision, as checked (snapshot 1.6, §3.2).

    The ``int``/``str`` fields are strict on load (§3.3): lax coercion would
    turn ``false`` into ``0`` and ``"404"`` into ``404`` before any check.
    """

    status: Literal["completed"]
    conclusion: Literal["cancelled"]
    runner_id: Annotated[int, Strict()]
    runner_name: Annotated[str, Strict()]
    steps: Annotated[int, Strict()]
    created_at: Annotated[str, Strict()]
    started_at: Annotated[str, Strict()]
    log_status: Annotated[int, Strict()]


def recognise(record: object, log_status: int | None) -> NotExecuted | None:
    """The job's ``NotExecuted`` when its listing record and its own log read
    show the supported form (§2), else ``None``. Missing or ill-typed values
    never match."""
    if not isinstance(record, dict):
        return None
    created, started = record.get("created_at"), record.get("started_at")
    steps = record.get("steps")
    if not (
        record.get("status") == "completed"
        and record.get("conclusion") == "cancelled"
        and _is_int(record.get("runner_id"), 0)
        and record.get("runner_name") == ""
        and isinstance(steps, list)
        and steps == []
        and _same_instant(created, started)
        and _is_int(log_status, 404)
    ):
        return None
    assert isinstance(created, str) and isinstance(started, str)
    return NotExecuted("completed", "cancelled", 0, "", 0, created, started, 404)


def basis_holds(ne: NotExecuted) -> bool:
    """A stored ``NotExecuted`` satisfies §2 on its own values (§3.3)."""
    return (
        ne.status == "completed"
        and ne.conclusion == "cancelled"
        and _is_int(ne.runner_id, 0)
        and ne.runner_name == ""
        and _is_int(ne.steps, 0)
        and _same_instant(ne.created_at, ne.started_at)
        and _is_int(ne.log_status, 404)
    )


def _is_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _same_instant(created: object, started: object) -> bool:
    """Both valid timestamps in the recorded form, and equal (§2)."""
    if not (isinstance(created, str) and isinstance(started, str)):
        return False
    if not (_TIMESTAMP_RE.fullmatch(created) and _TIMESTAMP_RE.fullmatch(started)):
        return False
    try:
        datetime.strptime(created, _TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return created == started
```

- [ ] **Step 4: Run** `uv run pytest tests/test_notexecuted.py -q`. Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit.**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/notexecuted.py tests/test_notexecuted.py
git commit -m "feat(notexecuted): recognise the recorded cancelled-before-execution form"
```

---

### Task 3: Snapshot 1.6, strict loading, forge wiring and the binding population

**Files:**
- Modify: `src/deployer/forge.py`: `FailedJob.not_executed`, `is_not_executed`, `SNAPSHOT_SCHEMA_VERSION = "1.6"`, the `build_failed_job` parameter, `fetch_failed_run` and `_bind_steps`.
- Modify: `src/deployer/stepbinding.py`: add `"excluded"` to `StepBindingState`.
- Modify: `tests/test_forge.py`, `tests/test_diagnose.py`: current-version literals `"1.5"` → `"1.6"` only where they pin the current version.
- Modify: `tests/test_steps2_recording.py`: the binding assertion of `test_forge_and_diagnose_today_on_steps_2` (see Step 5).
- Test: `tests/test_forge_not_executed.py`

**Interfaces:**
- Consumes: `LogRead` (Task 1); `NotExecuted`, `recognise`, `basis_holds` (Task 2).
- Produces:
  - `FailedJob.not_executed: NotExecuted | None = None` (the last field);
  - `forge.is_not_executed(job: FailedJob) -> bool` (§3.3);
  - `build_failed_job(..., not_executed: NotExecuted | None = None)`;
  - `StepBinding("excluded", "cancelled before execution")` on recognised jobs.

- [ ] **Step 1: Write the failing tests.** `tests/test_forge_not_executed.py`. It uses `ArchiveFakeGh` and `_bound_archive` from `tests/test_forge_archive.py`, and builds kept-job records in the recorded form:

```python
"""Forge: recognition on kept jobs, snapshot 1.6, strict load, the population."""

import json

import pytest
from pydantic import ValidationError

from deployer.forge import (
    ArchiveStatus,
    FailedRun,
    GhError,
    OverCap,
    RunRef,
    StepBinding,
    dump_snapshot,
    fetch_failed_run,
    is_not_executed,
    load_snapshot,
)
from deployer.notexecuted import NotExecuted
from tests import step_binding_data as data
from tests.test_forge import step
from tests.test_forge_archive import LOG, ArchiveFakeGh, _bound_archive

TS = "2026-10-04T09:13:25Z"
EXCLUDED = StepBinding("excluded", "cancelled before execution")


def _never_started(job_id: int) -> dict[str, object]:
    return {
        "id": job_id, "name": f"job-{job_id}", "status": "completed",
        "conclusion": "cancelled", "runner_id": 0, "runner_name": "",
        "steps": [], "created_at": TS, "started_at": TS,
    }


def _gh(archive: bytes | OverCap | GhError) -> ArchiveFakeGh:
    gh = ArchiveFakeGh(archive=archive)
    ran = {"id": 1, "name": "job-1", "conclusion": "failure",
           "steps": [step(1, "Set up job", "success"), step(2, "Run a")]}
    gh.job_pages = [[ran, _never_started(9)]]
    gh.logs = LOG
    gh.logs_by_job = {9: GhError("gh api … failed: gh: HTTP 404", 404)}
    return gh


def _run(gh: ArchiveFakeGh) -> FailedRun:
    run = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def test_a_recognised_sibling_is_excluded_and_the_other_job_binds() -> None:
    gh = _gh(_bound_archive())
    run = _run(gh)
    ran, sibling = run.jobs
    assert sibling.not_executed == NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 404)
    assert sibling.step_binding == EXCLUDED
    assert sibling.completeness.logs == "error"
    assert is_not_executed(sibling)
    assert ran.step_binding == StepBinding("bound")
    assert run.archive == ArchiveStatus("available")
    assert run.completeness.logs == "present"  # the expected 404 is not in the worst-of
    assert len([c for c in gh.calls if c.argv[-1].endswith("jobs/9/logs")]) == 1


@pytest.mark.parametrize(
    "archive",
    [
        data.zip_of([("0_job-1.txt", LOG.encode())]),  # absent
        b"not a zip",  # refused
        GhError("gh api … failed: Not Found (HTTP 404)", 404),  # unavailable
    ],
    ids=["absent", "refused", "unavailable"],
)
def test_excluded_does_not_depend_on_the_archive(archive: bytes | GhError) -> None:
    run = _run(_gh(archive))
    assert run.jobs[1].step_binding == EXCLUDED
    assert run.jobs[0].step_binding is not None
    assert run.jobs[0].step_binding.state == "no_archive"


def test_excluded_when_no_archive_is_attempted() -> None:
    from tests.test_forge import FakeGh

    gh = FakeGh(job_pages=_gh(b"").job_pages, logs=LOG)

    def api(argv: list[str], *, timeout: float) -> str:
        if argv[-1].endswith("jobs/9/logs"):
            raise GhError("gh api … failed: gh: HTTP 404", 404)
        return FakeGh.api(gh, argv, timeout=timeout)

    gh.api = api  # type: ignore[method-assign]
    run = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=gh)
    assert isinstance(run, FailedRun) and run.archive is None
    assert run.jobs[1].step_binding == EXCLUDED
    assert run.jobs[0].step_binding is None


def test_snapshot_1_6_round_trips_and_1_5_loads_as_not_recognised() -> None:
    run = _run(_gh(_bound_archive()))
    text = dump_snapshot(run)
    assert json.loads(text)["snapshot_schema_version"] == "1.6"
    assert load_snapshot(text) == run
    old = json.loads(text)
    old["snapshot_schema_version"] = "1.5"
    for job in old["jobs"]:
        del job["not_executed"]
    assert all(j.not_executed is None for j in load_snapshot(json.dumps(old)).jobs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runner_id", False), ("runner_id", "0"), ("steps", True),
        ("log_status", "404"), ("runner_name", None), ("created_at", 0),
        ("status", "Completed"),
    ],
)
def test_an_ill_typed_stored_basis_fails_to_load(field: str, value: object) -> None:
    document = json.loads(dump_snapshot(_run(_gh(_bound_archive()))))
    document["jobs"][1]["not_executed"][field] = value
    with pytest.raises(ValidationError):
        load_snapshot(json.dumps(document))


def test_other_fields_stay_lax_beside_a_strict_basis() -> None:
    document = json.loads(dump_snapshot(_run(_gh(_bound_archive()))))
    document["jobs"][0]["job_id"] = "1"
    assert load_snapshot(json.dumps(document)).jobs[0].job_id == 1


def test_a_contradicting_stored_basis_is_not_trusted() -> None:
    run = _run(_gh(_bound_archive()))
    document = json.loads(dump_snapshot(run))
    document["jobs"][1]["conclusion"] = "failure"
    assert not is_not_executed(load_snapshot(json.dumps(document)).jobs[1])
```

- [ ] **Step 2: Run.** `uv run pytest tests/test_forge_not_executed.py -q`. Expected: `ImportError: cannot import name 'is_not_executed'`.

- [ ] **Step 3: Implement the model.** In `stepbinding.py`, add `"excluded"` to `StepBindingState`. In `forge.py`:
  - import `from deployer.notexecuted import NotExecuted, basis_holds, recognise`;
  - set `SNAPSHOT_SCHEMA_VERSION = "1.6"`;
  - append `not_executed: NotExecuted | None = None` to `FailedJob`, and extend its docstring with one line for §3.2;
  - add the `build_failed_job` keyword `not_executed: NotExecuted | None = None`, passed through;
  - extend `dump_snapshot`'s docstring: "Schema 1.6 adds each job's ``not_executed`` (not-executed spec §3.2), additive; older documents load it as ``None``."
  - add the predicate:

```python
def is_not_executed(job: FailedJob) -> bool:
    """Whether a job is recognised as cancelled before execution (§3.3):
    its stored basis holds on its own values and agrees with the job."""
    ne = job.not_executed
    return (
        ne is not None
        and basis_holds(ne)
        and job.conclusion == "cancelled"
        and job.all_steps == []
        and not job.steps
        and job.completeness.logs == "error"
        and all(e.source == job.job_id for e in job.evidence)
    )
```

- [ ] **Step 4: Wire `fetch_failed_run` and `_bind_steps`.**
  - In `fetch_failed_run`, after the reads loop:

```python
    recognised = {
        int(r["id"]): ne
        for r in kept
        if (ne := recognise(r, reads[int(r["id"])].status)) is not None
    }
    archive, bindings = _bind_steps(
        runner, gh, ref, resolved, listing, kept, reads, set(recognised)
    )
```

  - In the job loop, a recognised job gets `step_binding=StepBinding("excluded", "cancelled
    before execution")` and `not_executed=recognised[job_id]`, and no spans. Other jobs are
    unchanged.
  - The run-level logs worst-of runs over `[j for j in jobs if j.job_id not in recognised]`.
    With none left, `_worst` returns its existing empty value (`"unavailable"`, "nothing
    read"); the annotations worst-of still covers every job.
  - In `_bind_steps`, add the parameter `excluded: set[int]` and compute
    `active = {job_id: read for job_id, read in reads.items() if job_id not in excluded}`.
    Every use of `reads` after the capability check becomes `active`: the early return
    `if not isinstance(...) or not active: return None, {}`, the kept-state loop, `_each(...)`,
    and the returned `{job_id: bound[job_id] for job_id in active}`. The population skips
    excluded jobs: `population = [r for r in listing if r.get("conclusion") != "skipped" and
    r.get("id") not in excluded]`. The malformed-record loop skips them too.

- [ ] **Step 5: Update the pinned versions and the `steps-2` binding pin.**
  - Change `"1.5"` to `"1.6"` where a test pins the CURRENT snapshot version (`tests/test_forge.py`'s round-trip, positional and missing-version tests; `tests/test_diagnose.py`'s run version). Tests that pin older versions stay.
  - In `tests/test_steps2_recording.py::test_forge_and_diagnose_today_on_steps_2`, replace the `{"unverifiable"}` assertion with: `never-starts`' binding is `StepBinding("excluded", "cancelled before execution")`, and every other job's binding is `bound`. The outcome assertion (`EVIDENCE_UNAVAILABLE`) stays until Task 4.
  - Update the README snapshot paragraph: `"1.5"` → `"1.6"`, and add one sentence on `not_executed`.

- [ ] **Step 6: Run** `uv run pytest tests/test_forge_not_executed.py tests/test_steps2_recording.py -q`, then `uv run pytest -q`. Expected: all pass.

- [ ] **Step 7: Lint, type-check, commit.**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat(forge): recognise not-executed kept jobs; snapshot 1.6; excluded from binding"
```

---

### Task 4: Diagnose: no verdict, a logs-only exemption, explanations, nothing left

**Files:**
- Modify: `src/deployer/diagnose.py`: `diagnose_run`, `_run_missing`, new `_run_lost`, `_job_incomplete`, `_job_missing`, notes.
- Modify: `tests/test_steps2_recording.py`: the outcome assertion (Step 5).
- Test: `tests/test_diagnose_not_executed.py`

**Interfaces:**
- Consumes: `forge.is_not_executed`, `FailedJob.not_executed` (Task 3).

- [ ] **Step 1: Write the failing tests.** `tests/test_diagnose_not_executed.py`, on hand-built snapshots:

```python
"""Diagnose with recognised jobs (spec §5, §6, §6.1, §3.3)."""

from dataclasses import replace

from deployer.diagnose import diagnose_run
from deployer.forge import Completeness, Evidence, FailedJob, FailedRun, FailedStep, StepInfo, StepRef
from deployer.notexecuted import NotExecuted

TS = "2026-10-04T09:13:25Z"
NE = NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 404)
CANCELLED = "job 9 (job-9) was cancelled before execution (runner 0, no steps, log 404)"
ALL = "every non-green job was cancelled before execution; no job log holds the failure"


def _recognised(annotations: str = "absent", evidence: list[Evidence] | None = None) -> FailedJob:
    return FailedJob(
        9, "job-9", "cancelled", [], evidence or [],
        Completeness("error", annotations), all_steps=[], not_executed=NE,
    )


def _ran(logs: str = "present") -> FailedJob:
    ref = StepRef(1, 2)
    step = FailedStep(ref, "Run a", "failure", [])
    text = "AssertionError: boom"
    return FailedJob(
        1, "job-1", "failure", [step], [Evidence(ref, text)] if logs == "present" else [],
        Completeness(logs, "absent"),
        all_steps=[StepInfo(1, "Set up job", "success"), StepInfo(2, "Run a", "failure")],
    )


def _run(*jobs: FailedJob, logs: str = "present", annotations: str = "absent") -> FailedRun:
    return FailedRun("o/r", 1, 1, "sha", "url", list(jobs), Completeness(logs, annotations))


def test_a_recognised_job_gets_no_verdict_and_does_not_degrade_the_run() -> None:
    d = diagnose_run(_run(_recognised(), _ran()))
    assert [v.where for v in d.failures] == [StepRef(1, 2)]
    assert d.outcome == "UNCLASSIFIED"
    assert CANCELLED in d.observations
    assert not any("logs fetch error" in o for o in d.observations)


def test_a_real_lost_log_beside_an_exclusion_is_reported_alone() -> None:
    d = diagnose_run(_run(_recognised(), _ran(logs="error"), logs="error"))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert CANCELLED in d.observations
    assert "job 1: logs fetch error" in d.observations
    assert "job 9: logs fetch error" not in d.observations


def test_a_recognised_job_s_annotations_error_still_counts() -> None:
    d = diagnose_run(_run(_recognised(annotations="error"), _ran(), annotations="error"))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert [v.where for v in d.failures] == [StepRef(1, 2)]
    assert "job 9: annotations fetch error" in d.observations
    assert CANCELLED in d.observations


def test_annotation_evidence_of_a_recognised_job_is_kept_but_not_cited() -> None:
    job = _recognised(evidence=[Evidence(9, "The job was cancelled", "failure")])
    d = diagnose_run(_run(job, _ran()))
    assert d.run.jobs[0].evidence == job.evidence
    assert all(e.source != 9 for v in d.failures for e in v.evidence)


def test_every_kept_job_recognised_is_explicitly_unavailable() -> None:
    d = diagnose_run(_run(_recognised(), logs="unavailable"))
    assert d.failures == []
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations[:2] == [CANCELLED, ALL]
    assert "logs unavailable" not in d.observations


def test_zero_kept_jobs_is_unchanged() -> None:
    d = diagnose_run(_run(logs="unavailable"))
    assert d.outcome == "UNCLASSIFIED"
    assert ALL not in d.observations


def test_a_dispatcher_shaped_sibling_is_evaluated_normally() -> None:
    dispatcher = replace(_recognised(), not_executed=None)
    d = diagnose_run(_run(dispatcher, _ran(), logs="error"))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert ALL not in d.observations
    assert {v.where for v in d.failures} == {9, StepRef(1, 2)}


def test_a_contradicting_stored_basis_is_ignored_with_a_note() -> None:
    contradicted = replace(_recognised(), conclusion="failure")
    d = diagnose_run(_run(contradicted, _ran(), logs="error"))
    assert "job 9: stored not_executed contradicts the job; ignored" in d.observations
    assert 9 in {v.where for v in d.failures}


def test_a_1_5_snapshot_diagnoses_exactly_as_before() -> None:
    plain = diagnose_run(_run(_ran()))
    assert plain.outcome == "UNCLASSIFIED" and plain.observations == []
```

- [ ] **Step 2: Run.** `uv run pytest tests/test_diagnose_not_executed.py -q`. Expected: failures, because the recognised job still gets a verdict and degrades the run.

- [ ] **Step 3: Implement.** In `diagnose.py`, import `is_not_executed` from `deployer.forge`, and add:

```python
_NOT_EXECUTED_NOTE = (
    "job {job_id} ({name}) was cancelled before execution (runner 0, no steps, log 404)"
)
_ALL_NOT_EXECUTED_NOTE = (
    "every non-green job was cancelled before execution; no job log holds the failure"
)
_CONTRADICTED_NOTE = "job {job_id}: stored not_executed contradicts the job; ignored"
```

Rewrite `diagnose_run`'s body. With no job carrying `not_executed`, it must take exactly the
old path and produce exactly the old observations:

```python
    recognised = {j.job_id for j in snapshot.jobs if is_not_executed(j)}
    notes = [
        _NOT_EXECUTED_NOTE.format(job_id=j.job_id, name=j.name)
        for j in snapshot.jobs
        if j.job_id in recognised
    ] + [
        _CONTRADICTED_NOTE.format(job_id=j.job_id)
        for j in snapshot.jobs
        if j.not_executed is not None and j.job_id not in recognised
    ]
    evaluated = [j for j in snapshot.jobs if j.job_id not in recognised]
    failures = [
        read_failure(job, step, job.completeness)
        for job in evaluated
        for step in (job.steps or [None])
    ]
    missing = _run_missing(snapshot, recognised)
    if snapshot.jobs and not evaluated:
        return RunDiagnosis(
            snapshot, [], "EVIDENCE_UNAVAILABLE", [],
            notes + [_ALL_NOT_EXECUTED_NOTE] + missing,
        )
    if _run_lost(snapshot, recognised) or any(
        v.outcome == "EVIDENCE_UNAVAILABLE" for v in failures
    ):
        return RunDiagnosis(snapshot, failures, "EVIDENCE_UNAVAILABLE", [], notes + missing)
    observations = notes + ([] if failures else [_EMPTY_SET_NOTE])
    return RunDiagnosis(snapshot, failures, "UNCLASSIFIED", [], observations)
```

with:

```python
def _run_lost(snapshot: FailedRun, recognised: set[int]) -> bool:
    """Whether evidence was lost, exempting only recognised jobs' log reads (§6)."""
    if not snapshot.jobs:
        return _lost(snapshot.completeness)
    if not recognised:
        return _incomplete(snapshot.completeness)
    return any(_job_incomplete(j, j.job_id in recognised) for j in snapshot.jobs)


def _job_incomplete(job: FailedJob, exempt: bool) -> bool:
    if exempt:
        return job.completeness.annotations == "error"
    return _incomplete(job.completeness)


def _job_missing(job: FailedJob, exempt: bool) -> list[str]:
    if exempt:
        return ["annotations fetch error"] if job.completeness.annotations == "error" else []
    return _missing(job.completeness)
```

`_run_missing(snapshot, recognised)` builds `per_job` with
`_job_missing(job, job.job_id in recognised)`, and returns
`per_job if (per_job or recognised) else _missing(snapshot.completeness)`. With no
recognised jobs the fallback is the same as today. Update its single caller. Keep the
`diagnose_run` docstring accurate: add one paragraph on recognised jobs (§5, §6, §6.1).

- [ ] **Step 4: Run** `uv run pytest tests/test_diagnose_not_executed.py tests/test_diagnose.py tests/test_diagnose_matrix.py -q`. Expected: all pass. The existing diagnose tests are unchanged.

- [ ] **Step 5: Update the `steps-2` outcome pin.** In `tests/test_steps2_recording.py`, change the outcome assertion of `test_forge_and_diagnose_today_on_steps_2` to `UNCLASSIFIED` and `len(diagnosis.failures) == 4`, and update its docstring: the spec has now revised both outcomes. Then run `uv run pytest -q`.

- [ ] **Step 6: Lint, type-check, commit.**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat(diagnose): no verdict for a recognised job; logs-only exemption; nothing-left result"
```

---

### Task 5: Acceptance on `steps-2`, the end-to-end cases, the boundary, docs

**Files:**
- Modify: `tests/step_binding_data.py`: add the `Steps2Replay` class.
- Modify: `tests/test_steps2_recording.py`: use `Steps2Replay` in place of its private `_Replay`.
- Test: `tests/test_not_executed_acceptance.py`
- Modify: `tests/fixtures/step-binding/PROVENANCE.md` (the derivations named for this spec) and `CHECKSUMS.sha256` (re-hash); `TODO.md`.

**Interfaces:**
- Produces: `step_binding_data.with_assertion_in_fail_fast() -> Steps2Replay` (a labelled synthetic derivation: the runner-timestamped line `AssertionError: probe-fail-fast` inserted right after the `MARK-fail-fast` line in both `parallel-legs (fail-fast)`'s served log and its step-3 file of the served archive, the archive re-zipped with `zip_of`; the timestamp copied from the `MARK` line so the comparison still matches), `step_binding_data.STEPS_2` (the `steps-2` fixture directory), `step_binding_data.only_the_recognised_job_kept(records)` (every listed job except `waiting-legs (never-starts)` turned `"conclusion": "success"`, so the recognised job is the only kept one; a labelled derivation), and `step_binding_data.Steps2Replay(logs: dict[int, str | GhError] | None = None, annotations: dict[int, GhError] | None = None, archive: bytes | None = <recorded>, capped: bool = True, records: Callable[[list[dict]], list[dict]] | None = None)`. It serves the recorded calls, applies overrides, and serves annotations `[]` unless overridden. When `capped` is false it has no `api_bytes_capped` (implement this as a subclass, not as an attribute set to `None`).

- [ ] **Step 1: Add `Steps2Replay` and switch `test_steps2_recording.py` to it.** Keep that file's assertions unchanged. Run `uv run pytest tests/test_steps2_recording.py -q`; it passes.

- [ ] **Step 2: Write the acceptance tests.** `tests/test_not_executed_acceptance.py`:

```python
"""Acceptance on the real steps-2 recording (spec §9.1), end-to-end synthetic
cases (§9.2) and the reproduce/fix boundary (§8, §9.3)."""

import copy
import json
from dataclasses import replace

from deployer.diagnose import diagnose_run
from deployer.forge import FailedRun, GhError, RunRef, StepBinding, StepRef, fetch_failed_run
from deployer.notexecuted import NotExecuted
from deployer.reproduce.shape import job_text, precheck
from tests.step_binding_data import (
    STEPS_2,
    Steps2Replay,
    only_the_recognised_job_kept,
    with_assertion_in_fail_fast,
)

ENV = json.loads((STEPS_2 / "environment.json").read_text())
TS = "2026-10-04T09:13:25Z"
NEVER = "waiting-legs (never-starts)"
RAN = {
    "parallel-legs (fail-fast)": "MARK-fail-fast",
    "parallel-legs (long-1)": "MARK-long-1",
    "parallel-legs (long-2)": "MARK-long-2",
    "waiting-legs (first-fails)": "MARK-first-fails",
}


def _fetch(gh: Steps2Replay) -> FailedRun:
    run = fetch_failed_run(RunRef(ENV["repo"], ENV["run_id"]), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def test_the_never_started_sibling_is_recognised_and_excluded() -> None:
    by = {j.name: j for j in _fetch(Steps2Replay()).jobs}
    never = by[NEVER]
    assert never.not_executed == NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 404)
    assert never.step_binding == StepBinding("excluded", "cancelled before execution")
    assert never.completeness.logs == "error"


def test_the_four_jobs_that_ran_are_bound_at_their_run_step() -> None:
    run = _fetch(Steps2Replay())
    assert run.archive is not None and run.archive.state == "available"
    for job in run.jobs:
        if job.name == NEVER:
            continue
        assert job.step_binding == StepBinding("bound")
        holders = [e for e in job.evidence if RAN[job.name] in e.text.split("\n")]
        assert [e.source for e in holders] == [StepRef(job.job_id, 3)]


def test_the_verdicts_are_exactly_the_four_run_steps() -> None:
    run = _fetch(Steps2Replay())
    d = diagnose_run(run)
    ids = {j.name: j.job_id for j in run.jobs}
    assert len(d.failures) == 4  # a set alone would hide a duplicated verdict
    assert {v.where for v in d.failures} == {StepRef(ids[n], 3) for n in RAN}
    for verdict in d.failures:  # no verdict cites another job's or step's evidence
        assert all(e.source == verdict.where for e in verdict.evidence)
    assert d.outcome == "UNCLASSIFIED"
    assert any("was cancelled before execution" in o for o in d.observations)
    assert not any("logs fetch error" in o for o in d.observations)


def test_a_recognised_diagnostic_line_is_cited_from_its_own_step() -> None:
    """Labelled synthetic (owner, 2026-10-04): every-cited-block-is-own-step passes
    vacuously when nothing is cited, so one leg gets a line an existing rule surely
    observes. It is `AssertionError: probe-fail-fast`, inserted after
    `MARK-fail-fast` in BOTH that job's log and its step-3 archive file, so binding
    still holds."""
    gh = with_assertion_in_fail_fast()
    run = _fetch(gh)
    ids = {j.name: j.job_id for j in run.jobs}
    d = diagnose_run(run)
    assert len(d.failures) == 4
    (verdict,) = [v for v in d.failures if v.where == StepRef(ids["parallel-legs (fail-fast)"], 3)]
    assert verdict.evidence, "the rule must cite something"
    assert all(e.source == verdict.where for e in verdict.evidence)
    assert any("AssertionError: probe-fail-fast" in e.text for e in verdict.evidence)
    others = [v for v in d.failures if v is not verdict]
    assert not any("probe-fail-fast" in e.text for v in others for e in v.evidence)


def test_job_text_is_unchanged_by_recognition() -> None:
    run = _fetch(Steps2Replay())
    plain = _fetch(Steps2Replay(capped=False))
    assert {j.job_id: job_text(j) for j in run.jobs} == {
        j.job_id: job_text(j) for j in plain.jobs
    }


def test_a_real_error_beside_the_exclusion_stays_visible() -> None:
    ids = {r["name"]: r["id"] for r in Steps2Replay().records()}
    gh = Steps2Replay(logs={ids["parallel-legs (long-1)"]: GhError("x (HTTP 502)", 502)})
    run = _fetch(gh)
    d = diagnose_run(run)
    assert {j.name: j.step_binding for j in run.jobs}[NEVER].state == "excluded"
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert f"job {ids['parallel-legs (long-1)']}: logs fetch error" in d.observations
    assert f"job {ids[NEVER]}: logs fetch error" not in d.observations


def test_the_recognised_job_s_annotations_error_is_reported() -> None:
    ids = {r["name"]: r["id"] for r in Steps2Replay().records()}
    d = diagnose_run(_fetch(Steps2Replay(annotations={ids[NEVER]: GhError("y (HTTP 502)", 502)})))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert f"job {ids[NEVER]}: annotations fetch error" in d.observations


def test_every_kept_job_recognised() -> None:
    gh = Steps2Replay(records=only_the_recognised_job_kept)
    run = _fetch(gh)
    assert [j.name for j in run.jobs] == [NEVER]
    d = diagnose_run(run)
    assert d.failures == [] and d.outcome == "EVIDENCE_UNAVAILABLE"
    assert any(o.startswith("every non-green job was cancelled before execution") for o in d.observations)


def test_excluded_under_every_archive_state() -> None:
    absent = Steps2Replay(archive=b"PK\x05\x06" + b"\x00" * 18)  # an empty ZIP: absent
    refused = Steps2Replay(archive=b"not a zip")
    unavailable = Steps2Replay(archive=GhError("z (HTTP 404)", 404))
    not_attempted = Steps2Replay(capped=False)
    for gh in (absent, refused, unavailable, not_attempted):
        never = {j.name: j for j in _fetch(gh).jobs}[NEVER]
        assert never.step_binding == StepBinding("excluded", "cancelled before execution")


def test_reproduce_precheck_refuses_the_same_with_and_without_the_field() -> None:
    run = _fetch(Steps2Replay())
    stripped = replace(run, jobs=[replace(j, not_executed=None) for j in run.jobs])
    assert precheck(run) == precheck(stripped)
    assert getattr(precheck(run), "reason", None) == "checkout SHA not established"
```

Exact helper shapes are the implementer's call: `records()` returns the recorded listing,
and `records=` transforms the listing served for the jobs request. One rule holds: an
`archive=GhError(...)` must raise from `api_bytes_capped`.

- [ ] **Step 3: The fix boundary.** Append to `tests/fix/test_author.py`:

```python
def test_a_recognised_sibling_does_not_change_r_s_rebuilt_binding(unique: Case) -> None:
    """Not-executed spec §8: a never-started sibling with `not_executed`, added
    to the stored run that `_build` reads, leaves the re-bound configuration
    identical (a labelled synthetic derivation of the basename-unique case)."""
    from deployer.fix import author as author_mod
    from deployer.reproduce.model import ReproductionSection

    document = unique.s.document()
    section = ReproductionSection.model_validate(document["reproduction"])
    assert section.try_dir is not None
    source_dir = (unique.s.r.root / section.try_dir).parent.parent / "source"
    before = author_mod._build(document, section, source_dir)
    sibling = {
        **copy.deepcopy(document["run"]["jobs"][0]),
        "job_id": 999_999, "name": "never-starts", "conclusion": "cancelled",
        "steps": [], "evidence": [], "all_steps": [],
        "completeness": {"logs": "error", "annotations": "absent"},
        "step_binding": {"state": "excluded", "reason": "cancelled before execution"},
        "not_executed": {
            "status": "completed", "conclusion": "cancelled", "runner_id": 0,
            "runner_name": "", "steps": 0, "created_at": "2026-10-04T09:13:25Z",
            "started_at": "2026-10-04T09:13:25Z", "log_status": 404,
        },
    }
    changed = copy.deepcopy(document)
    changed["run"]["jobs"].append(sibling)
    assert author_mod._build(changed, section, source_dir) == before
```

Check the import paths of `ReproductionSection` and `copy` against the file. If the stored
run's job objects have different keys, build the sibling from the same dataclass dump as the
run, and say so in the report.

- [ ] **Step 4: Run** `uv run pytest tests/test_not_executed_acceptance.py tests/fix/test_author.py -q`. Expected: all pass.

- [ ] **Step 5: PROVENANCE, checksums, TODO.**
  - **PROVENANCE:** append a short "Not-executed jobs" note. It lists each derivation from `steps-2` used by the acceptance tests (a log served as 502, annotations served as 502, the archive replaced, the listing reduced to the recognised job, no capped download). Each is a labelled synthetic case, not a recording.
  - **Checksums:** re-hash with `cd tests/fixtures/step-binding && find . -type f ! -name CHECKSUMS.sha256 ! -name 'record_steps*.py' ! -path '*__pycache__*' | sed 's|^\./||' | LC_ALL=C sort | xargs shasum -a 256 > CHECKSUMS.sha256 && cd -`, then run `uv run pytest tests/test_step_binding_recording.py -q`.
  - **TODO:** move `step-binding-never-started-jobs` to Shipped with a `Fixed:` line naming the spec, snapshot 1.6 and the acceptance. Open no new item unless the work found one.

- [ ] **Step 6: Full verification and commit.**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q
git add -A
git commit -m "test(not-executed): acceptance on steps-2, end-to-end cases, reproduce/fix boundary"
```
