# CI-failure diagnosis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read a real failed GitHub Actions run of the `ci.yml` this repo authored, classify each failure deterministically, and emit a verdict that cites evidence from the run.

**Architecture:** `forge.py` is the single GitHub-API chokepoint and returns a `FailedRun` snapshot of facts; `diagnose.py` is a pure classifier over that snapshot with no network. The taxonomy gains `UNKNOWN` (failure established, cause not) and `PROJECT` (positively evidenced defect in the project under verification), which is a breaking wire-format change and bumps the report schema to 2.0.

**Tech Stack:** Python 3.12, `uv`, pydantic v2, pytest, `gh` CLI as a subprocess.

**Spec:** `docs/superpowers/specs/2026-09-21-ci-failure-diagnosis-design.md` (externally reviewed twice; rev 2 APPROVED WITH NOTES).

## Global Constraints

- Python 3.12+, dependencies managed **exclusively** with `uv`. Never `pip`.
- `uv run ruff format .` and `uv run ruff check . --fix`; line length **88**.
- `uv run pyrefly check` after every change; fix what it reports.
- Tests: `uv run pytest`. Markers `docker` and `llm` are excluded by default
  (`addopts = "-m 'not docker and not llm'"`). New tests in this plan are **unit**
  tests and must stay in the default selection — no network, no tokens.
- **Authoring ≠ execution.** Nothing in this plan makes the agent apply anything.
- Branch → push → `gh pr create`. **Direct commits to `master` are forbidden**, as is
  a local merge bypassing a PR.
- `UNKNOWN` is never a `CLASSIFIED` class; `CLASSIFIED` admits only `AUTHORING`,
  `ENVIRONMENT`, `PROJECT`, always with evidence.
- A class is established **only** together with a citation. A rule that cannot cite
  contributes an observation, not a class.
- `PROJECT` is established on **positive evidence of a cause in the project**, never
  by the absence of other markers.

## PR boundaries

This plan ships in **three PRs**. Each leaves the repo working and tested.

| PR | Tasks | Closes |
|---|---|---|
| **PR-1 — taxonomy & report contract** | 1–5 | `todo://deployer/failure-classification-channel` |
| **PR-2 — bootstrap (preparatory)** | 6–7 | nothing; explicitly does **not** close the slice |
| **PR-3 — diagnosis & acceptance** | 8–12 | `todo://deployer/ci-failure-diagnosis` |

## File structure

| File | Responsibility |
|---|---|
| `src/deployer/models.py` | `FailureKind` +2 members; `SCHEMA_VERSION`; `CISpec.trigger_mode` |
| `src/deployer/verify.py` | two classification holes; L1 trigger validation per mode |
| `src/deployer/author.py` | stop reasons for the new classes |
| `src/deployer/bench.py` | explicit known schema majors |
| `src/deployer/llm.py` | prompt branch for `trigger_mode` |
| `src/deployer/forge.py` | **new** — the only GitHub-API caller; returns `FailedRun` |
| `src/deployer/diagnose.py` | **new** — pure classifier + run summary |
| `src/deployer/cli.py` | **new** `diagnose` subcommand |
| `tests/test_forge.py`, `tests/test_diagnose.py` | **new** |
| `tests/fixtures/runs/` | **new** — snapshot fixtures |

---

# PR-1 — taxonomy & report contract

### Task 1: `FailureKind` gains `UNKNOWN` and `PROJECT`

**Files:**
- Modify: `src/deployer/models.py:354-358`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `FailureKind.UNKNOWN` (value `"unknown"`), `FailureKind.PROJECT`
  (value `"project"`).

- [ ] **Step 1: Write the failing test**

```python
def test_failure_kind_has_unknown_and_project():
    assert FailureKind.UNKNOWN.value == "unknown"
    assert FailureKind.PROJECT.value == "project"


def test_failed_check_may_carry_unknown():
    """"Failed, cause not established" must be expressible: the fallthrough to
    AUTHORING existed only because it was not."""
    result = CheckResult(
        check_id="x", status=CheckStatus.FAILED, failure_kind=FailureKind.UNKNOWN
    )
    assert result.failure_kind is FailureKind.UNKNOWN


def test_failed_check_still_requires_a_kind():
    """The taxonomy invariant is ONE-WAY: FAILED without a class stays forbidden."""
    with pytest.raises(ValidationError):
        CheckResult(check_id="x", status=CheckStatus.FAILED)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -k "unknown or project" -v`
Expected: FAIL — `AttributeError: UNKNOWN`.

- [ ] **Step 3: Write minimal implementation**

```python
class FailureKind(StrEnum):
    """Taxonomy of failure causes."""

    AUTHORING = "authoring"
    ENVIRONMENT = "environment"
    #: Positively evidenced defect in the code/tests of the project under
    #: verification. Never inferred from the absence of other markers.
    PROJECT = "project"
    #: The failure is established; the cause is not. Before this member existed
    #: the only way to express a failure was to name a cause, which is why
    #: unknown failures fell through to AUTHORING.
    UNKNOWN = "unknown"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -k "unknown or project" -v` → PASS
Run: `uv run pyrefly check` → no new errors

- [ ] **Step 5: Commit**

```bash
git add src/deployer/models.py tests/test_models.py
git commit -m "feat(models): FailureKind gains UNKNOWN and PROJECT"
```

---

### Task 2: `_classify` stops inventing AUTHORING

**Files:**
- Modify: `src/deployer/verify.py:976-980`
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `FailureKind.UNKNOWN` (Task 1).
- Produces: `_classify(output: str) -> FailureKind` — returns `UNKNOWN` instead of
  `AUTHORING` when no marker matches.

- [ ] **Step 1: Write the failing test**

```python
def test_unknown_output_is_unknown_not_authoring():
    """Negative case: an exit-1 failure carrying no markers."""
    assert _classify("exit status 1") is FailureKind.UNKNOWN


def test_environment_marker_still_classifies():
    """Positive twin: a known cause keeps its justified classification."""
    assert _classify("cannot connect to the docker daemon") is FailureKind.ENVIRONMENT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verify_static.py -k classify -v`
Expected: the first FAILs (`AUTHORING is not UNKNOWN`); the second PASSes.

- [ ] **Step 3: Write minimal implementation**

```python
def _classify(output: str) -> FailureKind:
    lowered = output.lower()
    if any(marker in lowered for marker in ENVIRONMENT_MARKERS):
        return FailureKind.ENVIRONMENT
    # No marker matched. An exit code alone does not prove a root cause, so the
    # honest answer is UNKNOWN — the old fallthrough to AUTHORING asserted a
    # cause that nothing in the output supports.
    return FailureKind.UNKNOWN
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_verify_static.py -v` → PASS
Run: `uv run pytest` → note any corpus/golden expectation failures; they are
expected here and are fixed in Task 5, **not** by weakening this rule.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "fix(verify): unknown failure classifies UNKNOWN, not AUTHORING"
```

---

### Task 3: exit 125/126 without other positive evidence is UNKNOWN

**Files:**
- Modify: `src/deployer/verify.py:1437-1444`
- Test: `tests/test_verify_run.py`

**Interfaces:**
- Consumes: `FailureKind.UNKNOWN` (Task 1).
- Produces: the 125/126 branch returns `UNKNOWN` only when **no other positive
  evidence** identifies the cause.

- [ ] **Step 1: Write the failing test**

```python
def test_125_without_transport_marker_and_without_evidence_is_unknown():
    """Negative case: the exit code alone does not establish a class."""
    result = _run_completes_result(returncode=125, output="something went wrong")
    assert result.failure_kind is FailureKind.UNKNOWN


def test_125_with_transport_marker_is_environment():
    """Positive twin A: a known transport cause keeps ENVIRONMENT."""
    result = _run_completes_result(
        returncode=125, output="cannot connect to the docker daemon"
    )
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_125_with_other_positive_evidence_keeps_its_class():
    """Positive twin B: other positive evidence still classifies. The rule is
    "no OTHER positive evidence", not "no transport marker"."""
    result = _run_completes_result(
        returncode=125, output="exec: \"/app/start\": stat /app/start: no such file"
    )
    assert result.failure_kind is FailureKind.AUTHORING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verify_run.py -k 125 -v`
Expected: the first FAILs (`AUTHORING is not UNKNOWN`).

- [ ] **Step 3: Write minimal implementation**

```python
#: Positive evidence that the image's own entrypoint/command is wrong — an
#: authoring cause that can be cited, unlike a bare exit code.
AUTHORING_MARKERS = (
    "no such file or directory",
    "executable file not found",
    "exec format error",
)


def _classify_exit(returncode: int, output: str) -> FailureKind:
    lowered = output.lower()
    if returncode in (125, 126) and _is_transport_failure(output):
        return FailureKind.ENVIRONMENT
    if any(marker in lowered for marker in AUTHORING_MARKERS):
        return FailureKind.AUTHORING
    return FailureKind.UNKNOWN
```

Replace the tail of the 125/126 branch so both the transport case and the
fallthrough go through `_classify_exit`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_verify_run.py -v` → PASS
Run: `uv run pyrefly check` → clean

- [ ] **Step 5: Commit**

```bash
git add src/deployer/verify.py tests/test_verify_run.py
git commit -m "fix(verify): 125/126 without positive evidence is UNKNOWN"
```

---

### Task 4: the authoring loop stops instead of repairing an unknown cause

**Files:**
- Modify: `src/deployer/author.py:175-188`
- Test: `tests/test_author.py`

**Interfaces:**
- Consumes: `FailureKind.UNKNOWN`, `FailureKind.PROJECT` (Task 1).
- Produces: stop reasons `"unknown_failure"` and `"project_failure"`; helper
  `AuthoringRun.repairable(report) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
def test_unknown_failure_stops_without_repair(spy_author):
    """The loop broke only on ENVIRONMENT, so UNKNOWN fell through to repair."""
    run = author_loop(facts, target, author=spy_author, reports=[report_with(UNKNOWN)])
    assert run.stopped_reason == "unknown_failure"
    assert spy_author.repair_calls == 0


def test_project_failure_stops_without_repair(spy_author):
    run = author_loop(facts, target, author=spy_author, reports=[report_with(PROJECT)])
    assert run.stopped_reason == "project_failure"
    assert spy_author.repair_calls == 0


def test_authoring_beside_unknown_does_not_permit_repair(spy_author):
    """Editing the shared artifact can also affect the unestablished cause, so
    "that failure was not addressed" cannot be promised."""
    run = author_loop(
        facts, target, author=spy_author, reports=[report_with(AUTHORING, UNKNOWN)]
    )
    assert run.stopped_reason == "unknown_failure"
    assert spy_author.repair_calls == 0


def test_all_authoring_still_repairs(spy_author):
    run = author_loop(
        facts, target, author=spy_author, reports=[report_with(AUTHORING), passing()]
    )
    assert spy_author.repair_calls == 1


def test_stop_priority_environment_before_unknown_before_project(spy_author):
    run = author_loop(
        facts, target, author=spy_author,
        reports=[report_with(ENVIRONMENT, UNKNOWN, PROJECT)],
    )
    assert run.stopped_reason == "environment_failure"


def test_all_failures_are_kept_in_the_report(spy_author):
    run = author_loop(
        facts, target, author=spy_author, reports=[report_with(AUTHORING, UNKNOWN)]
    )
    kinds = {r.failure_kind for r in run.iterations[-1].report.results if r.failure_kind}
    assert kinds == {FailureKind.AUTHORING, FailureKind.UNKNOWN}
```

`spy_author` is a fixture wrapping the author double with a `repair_calls` counter.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_author.py -k "unknown or project or priority" -v`
Expected: FAIL — `repair_calls == 1`, `stopped_reason` is `None`.

- [ ] **Step 3: Write minimal implementation**

```python
def _stop_reason(report: VerificationReport) -> str | None:
    """Priority: ENVIRONMENT → UNKNOWN → PROJECT. All failures stay in the
    report regardless of which reason was chosen."""
    kinds = {r.failure_kind for r in report.results if r.status is CheckStatus.FAILED}
    if FailureKind.ENVIRONMENT in kinds:
        return "environment_failure"
    if FailureKind.UNKNOWN in kinds:
        return "unknown_failure"
    if FailureKind.PROJECT in kinds:
        return "project_failure"
    return None
```

In the loop, replace `if report.environment_failures:` with:

```python
            reason = _stop_reason(report)
            if reason is not None:
                stopped_reason = reason
                break
```

Repair is therefore reached only when every remaining failed check is
`AUTHORING` — the condition the owner ruled on.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_author.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/deployer/author.py tests/test_author.py
git commit -m "fix(author): stop on UNKNOWN/PROJECT instead of repairing a guess"
```

---

### Task 5: report schema 2.0, with the known-majors set made explicit

**Files:**
- Modify: `src/deployer/models.py:17`, `src/deployer/bench.py:527-528`
- Modify: `README.md` (the versioning policy paragraph, around line 78-84)
- Test: `tests/test_bench.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `SCHEMA_VERSION = "2.0"`; `_KNOWN_SCHEMA_MAJORS = frozenset({"0","1","2"})`.

**Why this is not a field addition:** `README.md:80-81` says that within a major only
**added fields** are compatible. Widening the value set of the existing
`failure_kind` field makes `CheckResult.model_validate` fail with a pydantic enum
error in an old reader — a breaking change to an existing field.

**Trap to avoid:** `_KNOWN_SCHEMA_MAJORS` is currently *derived*
(`{LEGACY_SCHEMA_VERSION, SCHEMA_VERSION.partition(".")[0]}`). Bumping
`SCHEMA_VERSION` to `"2.0"` would silently turn it into `{"0","2"}` and **drop
support for major 1**. It must become an explicit set.

- [ ] **Step 1: Write the failing test**

```python
def test_new_reader_accepts_majors_0_1_2():
    for version in ("0", "1.0", "2.0"):
        assert _schema_major_supported(version)


def test_major_1_is_not_dropped_by_the_bump():
    """Regression: the known-majors set used to be derived from SCHEMA_VERSION,
    so bumping to 2.0 would have silently dropped 1."""
    assert "1" in _KNOWN_SCHEMA_MAJORS


def test_old_reader_refuses_v2_explicitly(tmp_path):
    """An old reader must fail loudly, not degrade."""
    doc = {"schema_version": "2.0", "results": [
        {"check_id": "x", "status": "failed", "failure_kind": "unknown"}]}
    with pytest.raises(ValidationError):
        CheckResultV1.model_validate(doc["results"][0])
```

`CheckResultV1` is a test-local pydantic model with the two-member enum, standing
in for a reader pinned to the old contract.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bench.py -k schema -v`
Expected: FAIL — `"2.0"` unsupported; `_KNOWN_SCHEMA_MAJORS` is `{"0","1"}`.

- [ ] **Step 3: Write minimal implementation**

```python
# models.py
SCHEMA_VERSION = "2.0"
```

```python
# bench.py — explicit, not derived: deriving it from SCHEMA_VERSION drops the
# previous major on every bump.
_KNOWN_SCHEMA_MAJORS = frozenset({"0", "1", "2"})
```

Update the README paragraph to say that **2.0 widened the value set of
`failure_kind`**, that readers pinned to v1 refuse v2 by design, and that deployer
reads majors 0, 1 and 2.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest` → PASS (corpus expectations updated here if Task 2/3 moved them)
Run: `uv run pyrefly check` → clean

- [ ] **Step 5: Commit**

```bash
git add src/deployer/models.py src/deployer/bench.py README.md tests/
git commit -m "feat(models): report schema 2.0; known majors made explicit"
```

- [ ] **Step 6: Open PR-1**

```bash
git push -u origin fix/failure-taxonomy-unknown-project
gh pr create --title "fix(taxonomy): UNKNOWN/PROJECT, the two classification holes, schema 2.0"
```

Move `todo://deployer/failure-classification-channel` to `## Shipped` in the same PR.

---

# PR-2 — bootstrap (preparatory; does not close the slice)

### Task 6: `CISpec.trigger_mode`, validated per mode

**Files:**
- Modify: `src/deployer/models.py:107-116`, `src/deployer/verify.py:613-615`,
  `src/deployer/llm.py:113-118`
- Test: `tests/test_models.py`, `tests/test_verify_static.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: nothing from PR-1.
- Produces: `CISpec.trigger_mode: Literal["default", "manual"] = "default"`.

- [ ] **Step 1: Write the failing test**

```python
def test_empty_ci_spec_keeps_default_mode():
    """{"ci": {}} must keep EXACTLY its current behaviour."""
    assert CISpec().trigger_mode == "default"


def test_unknown_trigger_mode_is_rejected():
    with pytest.raises(ValidationError):
        CISpec(trigger_mode="cron")


def test_default_mode_still_requires_push_and_pull_request():
    problems = _check_ci_triggers({"on": {"push": None}}, trigger_mode="default")
    assert "workflow must trigger on pull_request" in problems


def test_manual_mode_requires_dispatch_and_forbids_push():
    assert _check_ci_triggers(
        {"on": {"workflow_dispatch": None}}, trigger_mode="manual"
    ) == []
    problems = _check_ci_triggers(
        {"on": {"push": None, "workflow_dispatch": None}}, trigger_mode="manual"
    )
    assert "manual trigger_mode forbids push" in problems


def test_manual_mode_keeps_every_other_constraint():
    """pull_request_target stays forbidden in both modes."""
    problems = _check_ci_triggers(
        {"on": {"workflow_dispatch": None, "pull_request_target": None}},
        trigger_mode="manual",
    )
    assert "pull_request_target is forbidden (security)" in problems
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py tests/test_verify_static.py -k trigger -v`
Expected: FAIL — `CISpec` forbids `trigger_mode`; `_check_ci_triggers` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
class CISpec(BaseModel):
    """Request for a build-image CI workflow. Presence is the request.

    `trigger_mode` varies HOW the workflow is triggered, not what it builds:
    `manual` is still the build-image workflow. Unknown keys stay rejected.
    """

    model_config = ConfigDict(extra="forbid")

    trigger_mode: Literal["default", "manual"] = "default"
```

```python
def _check_ci_triggers(workflow: dict, trigger_mode: str) -> list[str]:
    problems: list[str] = []
    triggers = _ci_triggers(workflow)
    if triggers is None:
        raise RuntimeError("ci_parses must guarantee an unambiguous trigger key")
    if trigger_mode == "manual":
        if "workflow_dispatch" not in triggers:
            problems.append("manual trigger_mode requires workflow_dispatch")
        for forbidden in ("push", "pull_request"):
            if forbidden in triggers:
                problems.append(f"manual trigger_mode forbids {forbidden}")
    else:
        for wanted in ("push", "pull_request"):
            if wanted not in triggers:
                problems.append(f"workflow must trigger on {wanted}")
    if "pull_request_target" in triggers:
        problems.append("pull_request_target is forbidden (security)")
    return problems
```

In `llm.py`, branch the workflow rules line: for `manual`, instruct
`trigger on workflow_dispatch only (never push, pull_request or
pull_request_target)`; the default text is unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -v` → PASS. `uv run pyrefly check` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/models.py src/deployer/verify.py src/deployer/llm.py tests/
git commit -m "feat(ci): trigger_mode on CISpec, L1 validated per mode"
```

---

### Task 7: the safe polygon workflow on the default branch

**Files:**
- Create: `.github/workflows/diagnosis-polygon.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: a dispatchable workflow at a path distinct from `.github/workflows/ci.yml`,
  job name `polygon`, distinct from the required contexts `test` and `governance / gate`.

**Why a separate PR:** GitHub requires a `workflow_dispatch` workflow to exist on the
default branch to be dispatchable, which inverts the usual rhythm. This PR lands the
*safe* workflow; the failing content arrives later on the experiment commit.

- [ ] **Step 1: Write the workflow**

```yaml
# Polygon for CI-failure diagnosis acceptance (todo://deployer/ci-failure-diagnosis).
# workflow_dispatch ONLY: it never runs on push or pull_request, so it never
# becomes a PR check. The watcher excludes it by this path and by the trigger,
# not by the displayed name. Job name is deliberately NOT `test`.
name: diagnosis-polygon

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  polygon:
    runs-on: ubuntu-24.04
    steps:
      - run: echo "safe default; failure scenarios live on the experiment ref"
```

- [ ] **Step 2: Verify isolation before merging**

```bash
# no push/pull_request triggers
grep -E '^\s+(push|pull_request):' .github/workflows/diagnosis-polygon.yml && exit 1
# job name does not collide with a required context
gh api repos/andrei-shtanakov/deployer/rulesets --jq '.[].id' | while read -r id; do
  gh api "repos/andrei-shtanakov/deployer/rulesets/$id" \
    --jq '[.rules[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context]'
done
```

Expected: the grep finds nothing; the required contexts are `test` and
`governance / gate`, neither of which is `polygon`.

- [ ] **Step 3: Commit and open PR-2**

```bash
git add .github/workflows/diagnosis-polygon.yml
git commit -m "ci: dispatch-only polygon workflow for diagnosis acceptance"
git push -u origin ci/diagnosis-polygon
gh pr create --title "ci: dispatch-only polygon workflow (preparatory, does not close the slice)"
```

- [ ] **Step 4: Verify the untested assumption, once merged**

```bash
gh workflow run diagnosis-polygon.yml --ref master
gh run list --workflow diagnosis-polygon.yml --limit 1
```

Expected: the dispatch is accepted. If it is refused, the default-branch
requirement is different from the documented behaviour and Task 12 must be
re-planned **before** anything is built on it.

---

# PR-3 — diagnosis & acceptance

### Task 8: `forge.py` — the snapshot of facts

**Files:**
- Create: `src/deployer/forge.py`
- Test: `tests/test_forge.py`

**Interfaces:**
- Consumes: nothing from PR-1/PR-2.
- Produces:
  - `StepRef(job_id: int, number: int)` — the composite key; the API gives steps no id.
  - `Evidence(source: StepRef | int | None, text: str)` — `int` is a job id for
    job-level evidence, `None` means the binding is absent and was not invented.
  - `FailedStep(ref: StepRef, name: str, conclusion: str, evidence: list[Evidence])`
  - `FailedJob(job_id: int, name: str, conclusion: str, steps: list[FailedStep], evidence: list[Evidence])`
  - `Completeness(logs: Literal["present","unavailable","error"], annotations: Literal["present","absent","error"])`
  - `FailedRun(repo: str, run_id: int, attempt: int, head_sha: str, url: str, jobs: list[FailedJob], completeness: Completeness)`
  - `AdapterRefusal(reason: Literal["not_finished","not_failed"], detail: str)`
  - `fetch_failed_run(ref: RunRef, *, attempt: int | None, runner=...) -> FailedRun | AdapterRefusal`

- [ ] **Step 1: Write the failing test**

```python
def test_unfinished_run_is_refused_not_snapshotted(fake_gh):
    fake_gh.run = {"status": "in_progress", "conclusion": None}
    out = fetch_failed_run(RunRef(repo="o/r", run_id=1), attempt=None, runner=fake_gh)
    assert isinstance(out, AdapterRefusal) and out.reason == "not_finished"


def test_successful_run_is_refused(fake_gh):
    fake_gh.run = {"status": "completed", "conclusion": "success"}
    out = fetch_failed_run(RunRef(repo="o/r", run_id=1), attempt=None, runner=fake_gh)
    assert isinstance(out, AdapterRefusal) and out.reason == "not_failed"


def test_attempt_is_resolved_once_before_reading_jobs(fake_gh):
    """A re-run must not mix evidence from different attempts."""
    fake_gh.run = {"status": "completed", "conclusion": "failure", "run_attempt": 3}
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=None, runner=fake_gh)
    assert snapshot.attempt == 3
    assert all(call.attempt == 3 for call in fake_gh.job_calls)


def test_jobs_are_read_with_pagination(fake_gh):
    fake_gh.job_pages = [[job(1)], [job(2)]]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert [j.job_id for j in snapshot.jobs] == [1, 2]


def test_three_completeness_states_are_distinct(fake_gh):
    fake_gh.logs = None
    assert fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh).completeness.logs == "unavailable"
    fake_gh.logs = GhError("404")
    assert fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh).completeness.logs == "error"


def test_unbound_log_line_keeps_source_none(fake_gh):
    """Where the API gives no line→step binding, the adapter does not invent one."""
    fake_gh.logs = "a line with no step header"
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert snapshot.jobs[0].evidence[0].source is None


def test_gh_is_invoked_without_a_shell_and_with_a_timeout(fake_gh):
    fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert all(isinstance(c.argv, list) and c.timeout is not None for c in fake_gh.calls)
    assert not any("--web" in c.argv or c.shell for c in fake_gh.calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_forge.py -v`
Expected: FAIL — `ModuleNotFoundError: deployer.forge`.

- [ ] **Step 3: Write minimal implementation**

Create `src/deployer/forge.py` with the dataclasses above and
`fetch_failed_run` structured as: resolve attempt **first** → refuse if not a
finished failure → read jobs with pagination → read logs/annotations, recording a
`Completeness` state per source → build `Evidence` with `source=None` where the
binding is absent. All `gh` calls go through one private `_gh(argv, timeout)`
helper using `subprocess.run(argv, capture_output=True, text=True, timeout=...)`
with no `shell=True`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_forge.py -v` → PASS. `uv run pyrefly check` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/forge.py tests/test_forge.py
git commit -m "feat(forge): FailedRun snapshot behind the single gh chokepoint"
```

---

### Task 9: `diagnose.py` — per-failure classification

**Files:**
- Create: `src/deployer/diagnose.py`
- Test: `tests/test_diagnose.py`

**Interfaces:**
- Consumes: `FailedRun`, `FailedJob`, `FailedStep`, `Evidence` (Task 8);
  `FailureKind` (Task 1).
- Produces:
  - `Outcome = Literal["CLASSIFIED","UNCLASSIFIED","EVIDENCE_UNAVAILABLE"]`
  - `FailureVerdict(where: StepRef | int, outcome: Outcome, kind: FailureKind | None, evidence: list[Evidence], observations: list[str])`
  - `classify_failure(job, step, completeness) -> FailureVerdict`

- [ ] **Step 1: Write the failing test**

```python
def test_class_requires_a_citation():
    """A rule that cannot cite contributes an observation, not a class."""
    v = classify_failure(job_with(evidence=[]), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_project_needs_positive_evidence_not_absence_of_markers():
    v = classify_failure(job_with(text="FAILED test_x - AssertionError: 1 != 2"),
                         step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT
    assert v.evidence


def test_similar_message_different_cause_does_not_reuse_the_class():
    """Rule soundness: a citation is necessary, not sufficient."""
    v = classify_failure(job_with(text="AssertionError in the runner's own setup"),
                         step=None, completeness=COMPLETE)
    assert v.kind is not FailureKind.PROJECT


def test_incomplete_evidence_wins_over_a_found_marker():
    v = classify_failure(job_with(text="AssertionError: 1 != 2"),
                         step=None, completeness=LOGS_UNAVAILABLE)
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert "AssertionError" in " ".join(v.observations)


def test_conflicting_causes_for_one_failure_are_unclassified():
    """Not resolved by iteration order."""
    v = classify_failure(
        job_with(text="cannot connect to the docker daemon\nAssertionError: 1 != 2"),
        step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED"
    assert "ambiguous" in " ".join(v.observations).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diagnose.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`classify_failure` collects **all** matching rules first (never returning on the
first match), then decides: incomplete completeness → `EVIDENCE_UNAVAILABLE`;
two or more incompatible classes → `UNCLASSIFIED` with an `ambiguous` observation;
exactly one class **with** evidence → `CLASSIFIED`; otherwise `UNCLASSIFIED` with
`kind=FailureKind.UNKNOWN`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diagnose.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/diagnose.py tests/test_diagnose.py
git commit -m "feat(diagnose): per-failure classification with evidence discipline"
```

---

### Task 10: the run summary

**Files:**
- Modify: `src/deployer/diagnose.py`
- Test: `tests/test_diagnose.py`

**Interfaces:**
- Consumes: `FailureVerdict` (Task 9).
- Produces: `RunDiagnosis(run: FailedRun, failures: list[FailureVerdict], outcome: Outcome, causes: list[FailureKind])`,
  `diagnose_run(snapshot: FailedRun) -> RunDiagnosis`.

- [ ] **Step 1: Write the failing test**

```python
def test_two_independent_failures_are_not_a_conflict():
    d = diagnose_run(run_with(job_project(), job_environment()))
    assert d.outcome == "CLASSIFIED"
    assert set(d.causes) == {FailureKind.PROJECT, FailureKind.ENVIRONMENT}


def test_job_order_does_not_change_the_result():
    a = diagnose_run(run_with(job_project(), job_unclassified()))
    b = diagnose_run(run_with(job_unclassified(), job_project()))
    assert a.outcome == b.outcome and set(a.causes) == set(b.causes)


def test_precedence_evidence_unavailable_beats_unclassified():
    d = diagnose_run(run_with(job_unclassified(), completeness=LOGS_UNAVAILABLE))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"


def test_unclassified_summary_keeps_established_causes():
    d = diagnose_run(run_with(job_project(), job_unclassified()))
    assert FailureKind.PROJECT in d.causes


def test_empty_diagnosable_set_is_never_classified():
    """A failed run with no diagnosable failed job/step must not satisfy
    "every element is classified" vacuously."""
    d = diagnose_run(run_with())
    assert d.outcome == "UNCLASSIFIED"
    assert d.failures == [] and d.observations


def test_empty_set_with_lost_data_is_evidence_unavailable():
    d = diagnose_run(run_with(completeness=LOGS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"


def test_failed_job_without_failed_steps_keeps_a_job_level_failure():
    d = diagnose_run(run_with(job_failed_no_steps()))
    assert [v.where for v in d.failures] == [JOB_ID]


def test_itemised_steps_do_not_duplicate_the_job_level_error():
    d = diagnose_run(run_with(job_failed_with_steps()))
    assert all(isinstance(v.where, StepRef) for v in d.failures)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diagnose.py -k run -v` → `diagnose_run` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
def diagnose_run(snapshot: FailedRun) -> RunDiagnosis:
    failures = [
        classify_failure(job, step, snapshot.completeness)
        for job in snapshot.jobs
        for step in (job.steps or [None])          # job-level when not itemised
    ]
    if snapshot.completeness.logs in ("unavailable", "error") or any(
        v.outcome == "EVIDENCE_UNAVAILABLE" for v in failures
    ):
        outcome = "EVIDENCE_UNAVAILABLE"
    elif not failures or any(v.outcome == "UNCLASSIFIED" for v in failures):
        # The empty set is NOT vacuously classified.
        outcome = "UNCLASSIFIED"
    else:
        outcome = "CLASSIFIED"
    causes = sorted({v.kind for v in failures if v.outcome == "CLASSIFIED" and v.kind})
    return RunDiagnosis(snapshot, failures, outcome, causes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diagnose.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/diagnose.py tests/test_diagnose.py
git commit -m "feat(diagnose): run summary with explicit precedence and no vacuous pass"
```

---

### Task 11: `deployer diagnose`

**Files:**
- Modify: `src/deployer/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `fetch_failed_run` (Task 8), `diagnose_run` (Task 10).
- Produces: subcommand `diagnose`; verdict document with its **own**
  `verdict_schema_version = "1.0"`.

**Exit codes — the numeric table:**

| Code | Meaning |
|---|---|
| `0` | `CLASSIFIED` |
| `3` | `UNCLASSIFIED` |
| `4` | `EVIDENCE_UNAVAILABLE` |
| `5` | adapter refusal (run not finished / not failed) |
| `2` | argument or metadata-fetch error |

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("outcome,code", [
    ("CLASSIFIED", 0), ("UNCLASSIFIED", 3), ("EVIDENCE_UNAVAILABLE", 4)])
def test_exit_code_per_outcome(outcome, code, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis(outcome))
    out = tmp_path / "v.json"
    assert cli.main(["diagnose", RUN_URL, "--output-file", str(out)]) == code
    assert json.loads(out.read_text())["outcome"] == outcome


def test_adapter_refusal_has_its_own_code(monkeypatch):
    monkeypatch.setattr(cli, "fetch_failed_run",
                        lambda *a, **k: AdapterRefusal("not_failed", "run succeeded"))
    assert cli.main(["diagnose", RUN_URL]) == 5


def test_url_and_repo_run_id_are_mutually_exclusive():
    assert cli.main(["diagnose", RUN_URL, "--repo", "o/r", "--run-id", "1"]) == 2


def test_attempt_must_be_a_positive_int():
    assert cli.main(["diagnose", RUN_URL, "--attempt", "0"]) == 2


def test_verdict_document_carries_its_own_schema_version(tmp_path):
    out = tmp_path / "v.json"
    cli.main(["diagnose", RUN_URL, "--output-file", str(out)])
    assert json.loads(out.read_text())["verdict_schema_version"] == "1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -k diagnose -v` → unknown subcommand.

- [ ] **Step 3: Write minimal implementation**

Add `p_diagnose = sub.add_parser("diagnose", ...)` with a positional `run_url`
(optional), `--repo`, `--run-id`, `--attempt` (positive int), `--output-file`;
`_cmd_diagnose` validates mutual exclusion, calls `fetch_failed_run`, maps
`AdapterRefusal` to `5`, otherwise calls `diagnose_run` and maps the outcome to
the table above. stdout gets the human summary; stderr the diagnostics.

**Fixture input is a test affordance, not a user contract** — no `--from-fixture`
flag is added.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v` → PASS. `uv run pyrefly check` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/cli.py tests/test_cli.py
git commit -m "feat(cli): deployer diagnose with per-outcome exit codes"
```

---

### Task 12: live acceptance and the paid benchmark

**Files:**
- Create: `tests/fixtures/runs/{authoring,environment,project}.json`
- Modify: `TODO.md`

**Interfaces:**
- Consumes: everything above.
- Produces: fixtures; acceptance evidence.

**Scenario origin.** The artifact is authored by `deployer author` with
`trigger_mode: manual`, then placed at `.github/workflows/diagnosis-polygon.yml`
on the experiment commit. The **only** permitted post-generation edit is the
controlled failure injection, named per scenario:

| Scenario | Injection | Expected class |
|---|---|---|
| A | a build step referencing a path that does not exist | `AUTHORING` |
| B | a dependency fetch pointed at a controlled-unavailable host | `ENVIRONMENT` |
| C | the project's own test made to fail with an explicit assertion, run **inside the image build** | `PROJECT` |
| C-control | scenario C with the defect removed | must pass |

`trigger_mode` does not license arbitrary CI steps: C runs the test during the
image build, inside the build-only contract.

- [ ] **Step 1: Verify SHA isolation before every dispatch**

```bash
SHA=$(git rev-parse HEAD)
gh pr list --repo andrei-shtanakov/deployer --state open --json headRefOid \
  --jq '.[].headRefOid' | grep -qx "$SHA" && { echo "SHA is an open PR head — stop"; exit 1; }
```

- [ ] **Step 2: Run the four dispatches**

```bash
gh workflow run diagnosis-polygon.yml --ref "$EXPERIMENT_REF"
gh run list --workflow diagnosis-polygon.yml --limit 1 --json databaseId,url,headSha
```

The expected class is recorded **before** each dispatch and is not present in the
scenario name or any comment the diagnosis can read.

- [ ] **Step 3: Diagnose each run through the CLI**

```bash
uv run deployer diagnose <run-url> --output-file evidence/<scenario>.json; echo "exit=$?"
```

Expected: A → `AUTHORING` exit 0; B → `ENVIRONMENT` exit 0; C → `PROJECT` exit 0;
C-control → adapter refusal `not_failed`, exit 5.

- [ ] **Step 4: Freeze the snapshots as fixtures**

Export each snapshot anonymised, preserving relations and diagnostic markers, into
`tests/fixtures/runs/`. Re-run `uv run pytest` — the offline suite now replays the
real runs.

- [ ] **Step 5: Paid benchmark, in this order**

```bash
uv run deployer bench run --author anthropic
uv run deployer bench compare      # explain EVERY class change before promoting
uv run deployer bench promote
```

Refreshing the baseline does not by itself prove correctness.

- [ ] **Step 6: Close the item and open PR-3**

Move `todo://deployer/ci-failure-diagnosis` to `## Shipped` with the run URLs,
SHAs and the golden-diff explanation. `todo://deployer/ci-fix-authoring` stays
open.

```bash
gh pr create --title "feat(diagnose): deterministic CI-failure diagnosis"
```

---

## Self-review

**Spec coverage.** §1 → Task 12 scenarios; §2.1 → Task 8; §2.2 → Task 9; §2.3 →
Tasks 8/9 (`AdapterRefusal` is a separate type); §3 → Task 9; §4 → Task 10; §5 →
Tasks 1–5; §5.3 → Task 4; §5.4 → Task 5; §6.1 → Task 6; §6.2 → Tasks 7 and 12;
§6.3 → Task 12 step 1; §6.4 → Task 12; §7 → Task 11; §8.1 → Tasks 2,3,4,5,9,10,11;
§8.2 → Task 12 step 2; §8.3 → Task 12 step 5; §9 → Task 7 step 4.

**Types.** `StepRef`, `Evidence`, `Completeness`, `FailedStep`, `FailedJob`,
`FailedRun`, `AdapterRefusal` are defined in Task 8 and used unchanged in Tasks
9–11. `FailureVerdict` (Task 9) and `RunDiagnosis` (Task 10) keep their names in
Task 11.

**Known trap carried into Task 5:** `_KNOWN_SCHEMA_MAJORS` is derived today and
would silently drop major 1 on the bump.
