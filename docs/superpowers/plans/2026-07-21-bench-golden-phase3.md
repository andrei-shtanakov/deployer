# Bench Golden Runs (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `deployer bench promote` writes a normalized golden baseline to `corpus/golden/`, and `deployer bench compare` reports leveled regressions between a raw run and another raw run or the golden.

**Architecture:** Normalization strips everything noisy (wall times, absolute paths, hostnames, check messages) into new `GoldenCase`/`GoldenReport` models; `promote` refuses mismatched runs without `--force`; `compare` produces `CompareFinding`s at three levels (hard / important / advisory) with wall-time comparison allowed only raw-vs-raw. The task list also closes the Phase 3 ledger decisions that live in the same code: expectation-matching semantics (`expected_failure_kind`, `requires_l2: false` offline), external URL+commit propagation, `-dirty` corpus commit, failure-kinds Markdown column, `CloneError`, static-only note.

**Tech Stack:** Python 3.12, pydantic v2, argparse, pytest, uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-07-21-bench-remote-verify-design.md`, section "Phase 3 — Run store, golden, compare". `.deployer-runs/` raw store already exists (Phase 2).

## Global Constraints

- uv only, never pip. After every task: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check` clean; full `uv run pytest` green before each commit. No docker needed for any Phase 3 unit test.
- Line length 88; type hints; docstrings on public APIs. Branch: `feature/bench-golden` (checked out); never commit to master.
- **Golden contains no noise**: no wall-clock durations, no absolute paths, no remote hostnames, no check messages (they may embed paths/hosts). Golden keeps: runtime tool, `remote` flag, platform, timeouts, corpus commit, per-case outcome/stopped_reason/iterations/failure_kinds/image size/check-status list, expected snapshot, final Dockerfile, external URL+commit.
- **Wall-time comparison is raw-vs-raw only** — `run vs golden` never mentions wall time.
- Exit codes: promote 0 written / 1 refused / 2 usage; compare 0 no hard+important regressions / 1 regressions / 2 usage. Advisory findings never affect the exit code.
- Spec deviation (documented): spec's advisory "hadolint warning count increased" becomes "hadolint check status worsened" — the pipeline records one hadolint `CheckResult`, not a per-finding count.
- Semantics decisions this plan implements (were deferred to Phase 3): (a) when `expected_success` is false and `expected_failure_kind` is set, matching additionally requires that kind among the run's `failure_kinds`; (b) for `requires_l2: false` cases, a `static_only` run with a passing report counts as success — `BenchCaseResult.success` means "achieved its expected verification level".

---

### Task 1: Matching semantics + result metadata (ledger closures)

**Files:**
- Modify: `src/deployer/bench.py` (`run_case`, `clone_external`, `render_markdown`, corpus-commit helper), `src/deployer/models.py` (`BenchCase` external fields already absent — add to `BenchCaseResult`)
- Test: `tests/test_bench.py` (append)

**Interfaces:**
- Produces: `BenchCaseResult` gains `external_url: str | None = None`, `external_commit: str | None = None`; `deployer.bench.BenchCase` gains the same two optional fields (set by `clone_external`); `run_case` matching per the semantics decisions above; `render_markdown` table gains a `failure kinds` column; `deployer.bench._corpus_commit() -> str | None` returning the sha with a `-dirty` suffix when `git status --porcelain` is non-empty (used instead of `_deployer_git_sha()` in `_run_bench_cases`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
from deployer.models import FailureKind


def _failed_run(kinds: list[FailureKind]) -> AuthoringRun:
    results = [
        CheckResult(
            check_id=f"c{i}", status=CheckStatus.FAILED, failure_kind=kind
        )
        for i, kind in enumerate(kinds)
    ]
    report = VerificationReport(results=results)
    return AuthoringRun(
        project="x",
        target=DeployTarget(),
        iterations=[
            IterationRecord(
                index=0, dockerfile="FROM x:1\n", report=report, duration_s=0.1
            )
        ],
        stopped_reason="no_progress",
        success=False,
    )


def test_expected_failure_kind_must_match(tmp_path: Path, monkeypatch) -> None:
    _make_case(
        tmp_path,
        "svc",
        expected={
            "requires_l2": False,
            "expected_success": False,
            "expected_failure_kind": "authoring",
        },
    )
    case = load_corpus(tmp_path)[0]
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile",
        lambda *a, **k: _failed_run([FailureKind.ENVIRONMENT]),
    )
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, tmp_path / "out",
        build_timeout=600, health_timeout=30,
    )
    assert result.outcome == "mismatched"  # failed, but with the wrong kind

    monkeypatch.setattr(
        "deployer.bench.author_dockerfile",
        lambda *a, **k: _failed_run([FailureKind.AUTHORING]),
    )
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, tmp_path / "out2",
        build_timeout=600, health_timeout=30,
    )
    assert result.outcome == "matched"


def test_static_only_counts_as_success_when_l2_not_required(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    case = load_corpus(tmp_path)[0]

    def static_only_run(*a, **k) -> AuthoringRun:
        report = VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )
        return AuthoringRun(
            project="x",
            target=DeployTarget(),
            iterations=[
                IterationRecord(
                    index=0, dockerfile="FROM x:1\n", report=report, duration_s=0.1
                )
            ],
            stopped_reason="static_only",
            success=False,
        )

    monkeypatch.setattr("deployer.bench.author_dockerfile", static_only_run)
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, tmp_path / "out",
        build_timeout=600, health_timeout=30,
    )
    assert result.success is True  # achieved its expected verification level
    assert result.outcome == "matched"  # expected_success default True now holds


def test_clone_external_carries_url_and_commit(tmp_path: Path) -> None:
    url, pinned = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit=pinned)
    case = clone_external(ext, tmp_path / "scratch")
    assert case.external_url == url
    assert case.external_commit == pinned


def test_run_case_records_external_identity(tmp_path: Path, monkeypatch) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    case = load_corpus(tmp_path)[0].model_copy(
        update={"external_url": "https://x/y.git", "external_commit": "a" * 40}
    )
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, tmp_path / "out",
        build_timeout=600, health_timeout=30,
    )
    assert result.external_url == "https://x/y.git"
    assert result.external_commit == "a" * 40


def test_corpus_commit_dirty_suffix(monkeypatch) -> None:
    from deployer.bench import _corpus_commit

    monkeypatch.setattr("deployer.bench._deployer_git_sha", lambda: "abc123")

    def fake_run_clean(cmd, **kwargs):
        return _fake_proc(0, stdout="")

    def fake_run_dirty(cmd, **kwargs):
        return _fake_proc(0, stdout=" M src/deployer/bench.py\n")

    monkeypatch.setattr("deployer.bench.subprocess.run", fake_run_clean)
    assert _corpus_commit() == "abc123"
    monkeypatch.setattr("deployer.bench.subprocess.run", fake_run_dirty)
    assert _corpus_commit() == "abc123-dirty"


def test_markdown_includes_failure_kinds_column() -> None:
    report = _report(
        BenchCaseResult(
            case="a", outcome="mismatched", success=False,
            stopped_reason="no_progress", iterations=3,
            failure_kinds=[FailureKind.AUTHORING], wall_time_s=1.0,
        )
    )
    md = render_markdown(report)
    assert "| failure kinds |" in md.splitlines()[-3] or "failure kinds" in md
    assert "authoring" in md
```

(`_fake_proc` already exists in `tests/test_runtime.py`, not here — add a tiny local copy or import it; a three-line local helper is fine.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py -v -k "expected_failure or static_only or external_identity or dirty or failure_kinds_column or carries_url"`
Expected: FAIL (attribute errors / assertion failures on current behavior).

- [ ] **Step 3: Implement**

`src/deployer/models.py` — `BenchCaseResult` gains:

```python
    external_url: str | None = None
    external_commit: str | None = None
```

`src/deployer/bench.py`:

`BenchCase` gains the same two fields (defaults `None`). `clone_external` sets them:

```python
    return BenchCase(
        name=ext.name,
        project_dir=dest,
        target=ext.target,
        expected=ext.expected,
        fixture_dockerfile=None,
        external_url=ext.url,
        external_commit=ext.commit,
    )
```

`run_case` — replace the success/outcome computation:

```python
    achieved_level = run.success or (
        not case.expected.requires_l2
        and run.stopped_reason == "static_only"
        and bool(run.iterations)
        and run.iterations[-1].report.passed
    )
    matched = achieved_level == case.expected.expected_success
    if (
        matched
        and not case.expected.expected_success
        and case.expected.expected_failure_kind is not None
    ):
        matched = case.expected.expected_failure_kind in failure_kinds
    return BenchCaseResult(
        case=case.name,
        outcome="matched" if matched else "mismatched",
        success=achieved_level,
        ...
        external_url=case.external_url,
        external_commit=case.external_commit,
    )
```

(`failure_kinds` is computed above the return already — move its computation before this block.)

Corpus commit helper (module level, near the imports):

```python
def _corpus_commit() -> str | None:
    """Deployer repo sha, '-dirty'-suffixed when the working tree has changes."""
    sha = _deployer_git_sha()
    if sha is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "status",
             "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return sha
    if proc.returncode == 0 and proc.stdout.strip():
        return f"{sha}-dirty"
    return sha
```

In `_run_bench_cases`, replace `corpus_commit=_deployer_git_sha()` with `corpus_commit=_corpus_commit()`.

`render_markdown` — extend the table:

```python
        "| case | outcome | stop reason | iters | image MB | wall s | failure kinds |",
        "|---|---|---|---:|---:|---:|---|",
```

and per row append `f"| {', '.join(k.value for k in c.failure_kinds) or '-'} |"` (adjust the row f-string accordingly).

- [ ] **Step 4: Run tests, full suite, format/lint/typecheck, commit**

Run: `uv run pytest && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Note: `tests/test_cli.py::_make_corpus` sets `expected_success: requires_l2` as a workaround — semantics change (b) makes the workaround unnecessary; revert the helper to plain `{"requires_l2": requires_l2}` and delete its explanatory comment (the offline run now legitimately matches the default `expected_success: true`). Update any test that asserted `success is False` for static-only offline cases.

```bash
git add src/deployer tests
git commit -m "feat: expectation semantics (failure kind, static-only) and result metadata"
```

---

### Task 2: Golden models + normalization

**Files:**
- Modify: `src/deployer/models.py` (add `GoldenCheck`, `GoldenCase`, `GoldenReport`)
- Modify: `src/deployer/bench.py` (add `normalize_run`)
- Test: `tests/test_bench.py` (append)

**Interfaces:**
- Produces: `deployer.models.GoldenCheck(check_id: str, status: CheckStatus, failure_kind: FailureKind | None = None)`; `GoldenCase(case: str, success: bool, stopped_reason: StopReason | None, iterations: int, failure_kinds: list[FailureKind], image_size_bytes: int | None, hadolint_status: CheckStatus | None, checks: list[GoldenCheck], expected: ExpectedOutcome, external_url: str | None = None, external_commit: str | None = None)`; `GoldenReport(promoted_from_label: str, corpus_commit: str | None, deployer_version: str | None, author_backend: str, runtime_tool: str | None, runtime_remote: bool, runtime_platform: str | None, build_timeout_s: int, health_timeout_s: int, cases: list[GoldenCase])`; `deployer.bench.normalize_run(run_dir: Path) -> GoldenReport` (reads `bench-report.json` + per-case `authoring-run.json`; skipped cases are excluded; raises `ValueError` if `bench-report.json` is missing).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
from deployer.bench import normalize_run
from deployer.models import GoldenReport


def _bench_run_on_disk(tmp_path: Path, monkeypatch) -> Path:
    """Produce a real raw run dir via run_bench with a mocked author loop."""
    _make_case(tmp_path, "a-ok", expected={"requires_l2": False})
    _make_case(tmp_path, "b-skip")  # requires_l2 True -> skipped offline
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    _, run_dir = run_bench(
        tmp_path,
        lambda case: FixtureAuthor("FROM x:1\n"),
        None,
        label="norm",
        author_backend="fixture",
        runs_root=tmp_path / "runs",
    )
    return run_dir


def test_normalize_run_strips_noise(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    golden = normalize_run(run_dir)
    assert golden.promoted_from_label == "norm"
    assert [c.case for c in golden.cases] == ["a-ok"]  # skipped excluded
    case = golden.cases[0]
    assert case.success is True
    assert case.checks and case.checks[0].check_id == "parses"
    payload = golden.model_dump_json()
    assert "wall_time_s" not in payload
    assert str(tmp_path) not in payload  # no absolute paths anywhere
    assert "message" not in payload  # check messages stripped


def test_normalize_run_requires_report(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="bench-report.json"):
        normalize_run(tmp_path / "empty")


def test_normalize_run_records_runtime_facts_without_host(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    report_file = run_dir / "bench-report.json"
    report = BenchReport.model_validate_json(report_file.read_text())
    report.runtime = ContainerRuntime(
        tool="docker", host="ssh://secret-host", host_source="cli"
    )
    report_file.write_text(report.model_dump_json(indent=2))
    golden = normalize_run(run_dir)
    assert golden.runtime_tool == "docker"
    assert golden.runtime_remote is True
    assert "secret-host" not in golden.model_dump_json()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py -v -k normalize`
Expected: FAIL — ImportError `normalize_run` / `GoldenReport`.

- [ ] **Step 3: Add models to `src/deployer/models.py`** (after `BenchReport`)

```python
class GoldenCheck(BaseModel):
    """One check outcome in a golden baseline; messages are stripped as noise."""

    check_id: str
    status: CheckStatus
    failure_kind: FailureKind | None = None


class GoldenCase(BaseModel):
    """Normalized per-case baseline: comparable facts only, no noise."""

    case: str
    success: bool
    stopped_reason: StopReason | None = None
    iterations: int = 0
    failure_kinds: list[FailureKind] = Field(default_factory=list)
    image_size_bytes: int | None = None
    hadolint_status: CheckStatus | None = None
    checks: list[GoldenCheck] = Field(default_factory=list)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    external_url: str | None = None
    external_commit: str | None = None


class GoldenReport(BaseModel):
    """Committed golden baseline. Never stores hostnames or wall-clock data."""

    promoted_from_label: str
    corpus_commit: str | None = None
    deployer_version: str | None = None
    author_backend: str
    runtime_tool: str | None = None
    runtime_remote: bool = False
    runtime_platform: str | None = None
    build_timeout_s: int
    health_timeout_s: int
    cases: list[GoldenCase] = Field(default_factory=list)
```

- [ ] **Step 4: Implement `normalize_run` in `src/deployer/bench.py`**

```python
def normalize_run(run_dir: Path) -> GoldenReport:
    """Normalize a raw bench run into a committable golden baseline.

    Strips wall-clock data, absolute paths, hostnames and check messages;
    skipped cases are excluded (a golden only asserts about cases that ran).
    """
    report_file = run_dir / "bench-report.json"
    if not report_file.is_file():
        raise ValueError(f"not a bench run dir: missing {report_file}")
    report = BenchReport.model_validate_json(report_file.read_text())
    golden_cases: list[GoldenCase] = []
    for result in report.cases:
        if result.outcome == "skipped":
            continue
        checks: list[GoldenCheck] = []
        hadolint_status: CheckStatus | None = None
        run_file = run_dir / "cases" / result.case / "authoring-run.json"
        if run_file.is_file():
            authoring = AuthoringRun.model_validate_json(run_file.read_text())
            if authoring.iterations:
                last_report = authoring.iterations[-1].report
                checks = [
                    GoldenCheck(
                        check_id=r.check_id,
                        status=r.status,
                        failure_kind=r.failure_kind,
                    )
                    for r in last_report.results
                ]
                for r in last_report.results:
                    if r.check_id == "hadolint":
                        hadolint_status = r.status
        golden_cases.append(
            GoldenCase(
                case=result.case,
                success=result.success,
                stopped_reason=result.stopped_reason,
                iterations=result.iterations,
                failure_kinds=result.failure_kinds,
                image_size_bytes=result.image_size_bytes,
                hadolint_status=hadolint_status,
                checks=checks,
                expected=_case_expected(run_dir, result.case),
                external_url=result.external_url,
                external_commit=result.external_commit,
            )
        )
    runtime = report.runtime
    platform = (
        report.runtime_versions.platform
        if report.runtime_versions is not None
        else None
    )
    return GoldenReport(
        promoted_from_label=report.label,
        corpus_commit=report.corpus_commit,
        deployer_version=report.deployer_version,
        author_backend=report.author_backend,
        runtime_tool=runtime.tool if runtime is not None else None,
        runtime_remote=runtime.remote if runtime is not None else False,
        runtime_platform=platform,
        build_timeout_s=report.build_timeout_s,
        health_timeout_s=report.health_timeout_s,
        cases=golden_cases,
    )
```

The `expected` snapshot: the raw run doesn't store `ExpectedOutcome`. Simplest correct source is the authoring run's sibling — but it isn't there either. Store the snapshot at *bench time* instead: in Task 1's scope `BenchCaseResult` did not get `expected`; add it here — extend `BenchCaseResult` with `expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)` populated by `run_case` (`expected=case.expected`), and use `expected=result.expected` in `normalize_run` (delete the `_case_expected` placeholder above — it does not exist). Update one Task-1 test to assert `result.expected == case.expected`.

- [ ] **Step 5: Run tests, full suite, format/lint/typecheck, commit**

```bash
git add src/deployer tests
git commit -m "feat: golden models and raw-run normalization"
```

---

### Task 3: `bench promote` CLI

**Files:**
- Modify: `src/deployer/bench.py` (add `promote_run`), `src/deployer/cli.py`
- Test: `tests/test_bench.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `deployer.bench.promote_run(run_dir: Path, corpus_root: Path, *, force: bool = False) -> Path` — normalizes, refuses (raises `ValueError`) when any case is `mismatched` unless `force`, writes `corpus_root/golden/golden.json` and `corpus_root/golden/cases/<name>/Dockerfile` (copied from the raw run), replaces any existing golden atomically enough for a research bench (delete old `golden/` tree first), returns the golden dir. CLI: `deployer bench promote RUN_DIR [--corpus corpus] [--force]`; exit 0 written / 1 refused / 2 usage (missing run dir).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
from deployer.bench import promote_run


def test_promote_writes_golden_tree(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    golden_dir = promote_run(run_dir, tmp_path)
    assert golden_dir == tmp_path / "golden"
    golden = GoldenReport.model_validate_json(
        (golden_dir / "golden.json").read_text()
    )
    assert golden.promoted_from_label == "norm"
    assert (golden_dir / "cases" / "a-ok" / "Dockerfile").read_text().startswith(
        "FROM x:1"
    )


def test_promote_refuses_mismatch_without_force(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "bad", expected={"requires_l2": False,
                                          "expected_success": False})
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    _, run_dir = run_bench(
        tmp_path, lambda c: FixtureAuthor("FROM x:1\n"), None,
        label="bad", author_backend="fixture", runs_root=tmp_path / "runs",
    )
    with pytest.raises(ValueError, match="mismatch"):
        promote_run(run_dir, tmp_path)
    promote_run(run_dir, tmp_path, force=True)  # force overrides
    assert (tmp_path / "golden" / "golden.json").is_file()


def test_promote_replaces_previous_golden(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    stale = tmp_path / "golden" / "cases" / "ghost" / "Dockerfile"
    stale.parent.mkdir(parents=True)
    stale.write_text("FROM ghost:1\n")
    promote_run(run_dir, tmp_path)
    assert not stale.exists()
```

Append to `tests/test_cli.py`:

```python
def test_bench_promote_cli(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    code = main(["bench", "promote", str(run_dir), "--corpus", str(corpus)])
    assert code == 0
    assert (corpus / "golden" / "golden.json").is_file()
    assert "golden" in capsys.readouterr().out


def test_bench_promote_missing_run_dir_exits_2(tmp_path, capsys):
    assert main(["bench", "promote", str(tmp_path / "nope")]) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py tests/test_cli.py -v -k promote`
Expected: FAIL — ImportError / `invalid choice: 'promote'`.

- [ ] **Step 3: Implement `promote_run`**

```python
def promote_run(run_dir: Path, corpus_root: Path, *, force: bool = False) -> Path:
    """Promote a raw run to the committed golden baseline in corpus/golden."""
    golden = normalize_run(run_dir)
    mismatched = [
        c.case
        for c in BenchReport.model_validate_json(
            (run_dir / "bench-report.json").read_text()
        ).cases
        if c.outcome == "mismatched"
    ]
    if mismatched and not force:
        raise ValueError(
            "refusing to promote a run with mismatched cases "
            f"({', '.join(mismatched)}); use --force to override"
        )
    golden_dir = corpus_root / "golden"
    if golden_dir.exists():
        shutil.rmtree(golden_dir)
    golden_dir.mkdir(parents=True)
    (golden_dir / "golden.json").write_text(golden.model_dump_json(indent=2))
    for case in golden.cases:
        src = run_dir / "cases" / case.case / "Dockerfile"
        if src.is_file():
            dest = golden_dir / "cases" / case.case
            dest.mkdir(parents=True)
            shutil.copyfile(src, dest / "Dockerfile")
    return golden_dir
```

(Avoid double-reading `bench-report.json`: refactor `normalize_run` to expose the parsed report — simplest is an internal `_load_bench_report(run_dir) -> BenchReport` used by both; keep the public signatures as specified.)

- [ ] **Step 4: Wire the CLI**

`_cmd_bench_promote` follows the existing `_cmd_bench_*` pattern: validate `RUN_DIR` is a directory (else exit 2), call `promote_run(Path(args.run_dir), Path(args.corpus), force=args.force)`, `except (FileNotFoundError, ValueError) as exc` → if the message starts with "refusing" print it and return 1, else return 2; on success print the golden dir path. Parser:

```python
    p_bench_promote = bench_sub.add_parser(
        "promote", help="promote a raw run to corpus/golden"
    )
    p_bench_promote.add_argument("run_dir")
    p_bench_promote.add_argument("--corpus", default="corpus")
    p_bench_promote.add_argument("--force", action="store_true")
    p_bench_promote.set_defaults(func=_cmd_bench_promote)
```

Distinguishing exit 1 vs 2: raise a dedicated `PromoteRefusedError(ValueError)` in `bench.py` instead of string matching — catch it before `ValueError` in the CLI and return 1. (Cleaner than message inspection; add it next to the models imports.)

- [ ] **Step 5: Run tests, full suite, format/lint/typecheck, commit**

```bash
git add src/deployer tests
git commit -m "feat: bench promote with normalized golden output"
```

---

### Task 4: Compare core

**Files:**
- Modify: `src/deployer/models.py` (add `CompareFinding`), `src/deployer/bench.py` (add `load_baseline`, `compare_runs`)
- Test: `tests/test_bench.py` (append)

**Interfaces:**
- Produces: `deployer.models.CompareFinding(level: Literal["hard","important","advisory"], case: str, metric: str, detail: str)`; `deployer.bench.load_baseline(source: Path | str, corpus_root: Path) -> BenchReport | GoldenReport` (the literal string `"golden"` loads `corpus_root/golden/golden.json`, else treats source as a raw run dir; raises `ValueError` when missing); `deployer.bench.compare_runs(candidate: BenchReport, baseline: BenchReport | GoldenReport, *, image_threshold_pct: float = 10.0, wall_threshold_pct: float = 25.0, iteration_threshold: int = 0) -> list[CompareFinding]`.
- Comparison rules (candidate measured against baseline):
  - hard: baseline case success, candidate case not success.
  - important: candidate iterations − baseline iterations > iteration_threshold; failure_kinds flipped between authoring-only and environment-only; case present in baseline but absent (or skipped) in candidate.
  - advisory: image size grew > image_threshold_pct; hadolint status worsened (PASSED→WARNING/FAILED — only when both sides carry a hadolint status; raw `BenchCaseResult` has none, so this fires only golden-baseline compares); wall time grew > wall_threshold_pct — **only when baseline is a `BenchReport`** (raw-vs-raw); case present only in candidate ("new case").
  - Findings sorted: hard, important, advisory; within level by case name.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
from deployer.bench import compare_runs, load_baseline
from deployer.models import CompareFinding, GoldenCase


def _golden(*cases: GoldenCase) -> GoldenReport:
    return GoldenReport(
        promoted_from_label="base",
        author_backend="fixture",
        build_timeout_s=600,
        health_timeout_s=30,
        cases=list(cases),
    )


def _gcase(name: str, **overrides) -> GoldenCase:
    defaults = dict(case=name, success=True, stopped_reason="success",
                    iterations=1, image_size_bytes=100_000_000)
    defaults.update(overrides)
    return GoldenCase(**defaults)


def _rcase(name: str, **overrides) -> BenchCaseResult:
    defaults = dict(case=name, outcome="matched", success=True,
                    stopped_reason="success", iterations=1,
                    image_size_bytes=100_000_000, wall_time_s=10.0)
    defaults.update(overrides)
    return BenchCaseResult(**defaults)


def test_compare_green_to_red_is_hard() -> None:
    findings = compare_runs(
        _report(_rcase("a", outcome="mismatched", success=False,
                       stopped_reason="no_progress")),
        _golden(_gcase("a")),
    )
    assert [f.level for f in findings][0] == "hard"
    assert findings[0].case == "a"


def test_compare_iteration_growth_is_important() -> None:
    findings = compare_runs(
        _report(_rcase("a", iterations=3)), _golden(_gcase("a"))
    )
    assert any(
        f.level == "important" and f.metric == "iterations" for f in findings
    )


def test_compare_missing_case_is_important_new_case_advisory() -> None:
    findings = compare_runs(
        _report(_rcase("b")), _golden(_gcase("a"))
    )
    levels = {(f.level, f.metric, f.case) for f in findings}
    assert ("important", "missing_case", "a") in levels
    assert ("advisory", "new_case", "b") in levels


def test_compare_image_growth_threshold() -> None:
    grown = _report(_rcase("a", image_size_bytes=115_000_000))
    assert any(
        f.level == "advisory" and f.metric == "image_size"
        for f in compare_runs(grown, _golden(_gcase("a")))
    )
    small = _report(_rcase("a", image_size_bytes=105_000_000))
    assert not any(
        f.metric == "image_size" for f in compare_runs(small, _golden(_gcase("a")))
    )


def test_compare_wall_time_raw_vs_raw_only() -> None:
    slow = _report(_rcase("a", wall_time_s=20.0))
    raw_baseline = _report(_rcase("a", wall_time_s=10.0))
    assert any(
        f.metric == "wall_time" for f in compare_runs(slow, raw_baseline)
    )
    assert not any(
        f.metric == "wall_time" for f in compare_runs(slow, _golden(_gcase("a")))
    )


def test_compare_clean_run_has_no_findings() -> None:
    assert compare_runs(_report(_rcase("a")), _golden(_gcase("a"))) == []


def test_load_baseline_golden_and_raw(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    golden = load_baseline("golden", tmp_path)
    assert isinstance(golden, GoldenReport)
    raw = load_baseline(run_dir, tmp_path)
    assert isinstance(raw, BenchReport)
    with pytest.raises(ValueError):
        load_baseline("golden", tmp_path / "nowhere")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py -v -k compare`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement**

`src/deployer/models.py`:

```python
class CompareFinding(BaseModel):
    """One regression (or notice) from comparing two bench runs."""

    level: Literal["hard", "important", "advisory"]
    case: str
    metric: str
    detail: str
```

`src/deployer/bench.py`:

```python
_LEVEL_ORDER = {"hard": 0, "important": 1, "advisory": 2}


def load_baseline(
    source: Path | str, corpus_root: Path
) -> BenchReport | GoldenReport:
    """Load a comparison baseline: the literal 'golden' or a raw run dir."""
    if source == "golden":
        golden_file = corpus_root / "golden" / "golden.json"
        if not golden_file.is_file():
            raise ValueError(f"no golden baseline at {golden_file}")
        return GoldenReport.model_validate_json(golden_file.read_text())
    run_dir = Path(source)
    report_file = run_dir / "bench-report.json"
    if not report_file.is_file():
        raise ValueError(f"not a bench run dir: missing {report_file}")
    return BenchReport.model_validate_json(report_file.read_text())


def _baseline_cases(
    baseline: BenchReport | GoldenReport,
) -> dict[str, BenchCaseResult | GoldenCase]:
    if isinstance(baseline, BenchReport):
        return {c.case: c for c in baseline.cases if c.outcome != "skipped"}
    return {c.case: c for c in baseline.cases}


def compare_runs(
    candidate: BenchReport,
    baseline: BenchReport | GoldenReport,
    *,
    image_threshold_pct: float = 10.0,
    wall_threshold_pct: float = 25.0,
    iteration_threshold: int = 0,
) -> list[CompareFinding]:
    """Regressions of `candidate` measured against `baseline`, by level."""
    findings: list[CompareFinding] = []
    base = _baseline_cases(baseline)
    cand = {c.case: c for c in candidate.cases if c.outcome != "skipped"}
    raw_baseline = isinstance(baseline, BenchReport)

    for name, b in base.items():
        c = cand.get(name)
        if c is None:
            findings.append(
                CompareFinding(
                    level="important",
                    case=name,
                    metric="missing_case",
                    detail="present in baseline but absent or skipped in candidate",
                )
            )
            continue
        if b.success and not c.success:
            findings.append(
                CompareFinding(
                    level="hard",
                    case=name,
                    metric="success",
                    detail=f"green in baseline, now {c.stopped_reason}",
                )
            )
        if c.iterations - b.iterations > iteration_threshold:
            findings.append(
                CompareFinding(
                    level="important",
                    case=name,
                    metric="iterations",
                    detail=f"{b.iterations} -> {c.iterations}",
                )
            )
        b_kinds, c_kinds = set(b.failure_kinds), set(c.failure_kinds)
        if b_kinds and c_kinds and b_kinds != c_kinds:
            findings.append(
                CompareFinding(
                    level="important",
                    case=name,
                    metric="failure_kind",
                    detail=f"{sorted(k.value for k in b_kinds)} -> "
                    f"{sorted(k.value for k in c_kinds)}",
                )
            )
        if (
            b.image_size_bytes
            and c.image_size_bytes
            and c.image_size_bytes
            > b.image_size_bytes * (1 + image_threshold_pct / 100)
        ):
            findings.append(
                CompareFinding(
                    level="advisory",
                    case=name,
                    metric="image_size",
                    detail=f"{b.image_size_bytes} -> {c.image_size_bytes} bytes "
                    f"(>{image_threshold_pct:g}%)",
                )
            )
        b_hadolint = getattr(b, "hadolint_status", None)
        if (
            b_hadolint is CheckStatus.PASSED
            and _candidate_hadolint(candidate, name) 
            not in (None, CheckStatus.PASSED)
        ):
            findings.append(
                CompareFinding(
                    level="advisory",
                    case=name,
                    metric="hadolint",
                    detail="hadolint status worsened vs baseline",
                )
            )
        if (
            raw_baseline
            and b.wall_time_s > 0
            and c.wall_time_s
            > b.wall_time_s * (1 + wall_threshold_pct / 100)
        ):
            findings.append(
                CompareFinding(
                    level="advisory",
                    case=name,
                    metric="wall_time",
                    detail=f"{b.wall_time_s:.1f}s -> {c.wall_time_s:.1f}s "
                    f"(>{wall_threshold_pct:g}%)",
                )
            )

    for name in sorted(set(cand) - set(base)):
        findings.append(
            CompareFinding(
                level="advisory",
                case=name,
                metric="new_case",
                detail="present in candidate but not in baseline",
            )
        )
    findings.sort(key=lambda f: (_LEVEL_ORDER[f.level], f.case, f.metric))
    return findings
```

Type-narrowing note: `b` is `BenchCaseResult | GoldenCase`, and `wall_time_s` exists only on `BenchCaseResult` — pyrefly will reject the `raw_baseline and b.wall_time_s` guard as-is. Narrow explicitly: `if raw_baseline and isinstance(b, BenchCaseResult) and ...` (the flag and the isinstance are equivalent at runtime; the isinstance is for the type checker).

`_candidate_hadolint`: the raw `BenchCaseResult` doesn't carry hadolint status. Two honest options: (1) drop the hadolint advisory entirely this phase, or (2) add `hadolint_status: CheckStatus | None = None` to `BenchCaseResult` (populated in `run_case` from the last report, exactly like `normalize_run` does) so both sides carry it, and simplify the code above to `b_hadolint is PASSED and c.hadolint_status not in (None, PASSED)`. **Take option 2** — it also removes the `getattr` for `wall_time_s` asymmetry concern (only `GoldenCase` lacks wall time, which the `raw_baseline` gate already handles). Add the field in this task, populate in `run_case`, delete the `_candidate_hadolint` helper from the code above, and assert in the golden-normalization test that the two `hadolint_status` sources agree.

- [ ] **Step 4: Run tests, full suite, format/lint/typecheck, commit**

```bash
git add src/deployer tests
git commit -m "feat: compare core with leveled regression findings"
```

---

### Task 5: `bench compare` CLI + README

**Files:**
- Modify: `src/deployer/cli.py`, `README.md`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: CLI `deployer bench compare CANDIDATE BASELINE [--corpus corpus] [--image-threshold PCT] [--wall-threshold PCT] [--iteration-threshold N]` where CANDIDATE is a raw run dir and BASELINE is a raw run dir or the literal `golden`. Prints findings grouped by level (or "no regressions"); exit 0 no hard+important / 1 regressions / 2 usage.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_bench_compare_cli_regression_exits_1(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "base"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    assert main(
        ["bench", "promote", str(run_dir), "--corpus", str(corpus)]
    ) == 0

    import json as _json

    report_file = run_dir / "bench-report.json"
    data = _json.loads(report_file.read_text())
    data["cases"][0]["success"] = False
    data["cases"][0]["outcome"] = "mismatched"
    data["cases"][0]["stopped_reason"] = "no_progress"
    report_file.write_text(_json.dumps(data))

    code = main(
        ["bench", "compare", str(run_dir), "golden", "--corpus", str(corpus)]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "hard" in out and "case-one" in out


def test_bench_compare_clean_exits_0(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "base"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    assert main(["bench", "promote", str(run_dir), "--corpus", str(corpus)]) == 0
    code = main(
        ["bench", "compare", str(run_dir), "golden", "--corpus", str(corpus)]
    )
    assert code == 0
    assert "no regressions" in capsys.readouterr().out


def test_bench_compare_bad_baseline_exits_2(tmp_path, capsys):
    assert main(
        ["bench", "compare", str(tmp_path), str(tmp_path / "nope")]
    ) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -v -k compare`
Expected: FAIL — `invalid choice: 'compare'`.

- [ ] **Step 3: Implement `_cmd_bench_compare`**

```python
def _cmd_bench_compare(args: argparse.Namespace) -> int:
    candidate_dir = Path(args.candidate)
    try:
        candidate = load_baseline(candidate_dir, Path(args.corpus))
        baseline = load_baseline(
            args.baseline if args.baseline == "golden" else Path(args.baseline),
            Path(args.corpus),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(candidate, BenchReport):
        print("error: candidate must be a raw run dir", file=sys.stderr)
        return 2
    findings = compare_runs(
        candidate,
        baseline,
        image_threshold_pct=args.image_threshold,
        wall_threshold_pct=args.wall_threshold,
        iteration_threshold=args.iteration_threshold,
    )
    if not findings:
        print("no regressions")
        return 0
    for finding in findings:
        print(
            f"[{finding.level:>9}] {finding.case}: "
            f"{finding.metric} — {finding.detail}"
        )
    blocking = any(f.level in ("hard", "important") for f in findings)
    return 1 if blocking else 0
```

Parser:

```python
    p_bench_compare = bench_sub.add_parser(
        "compare", help="compare a raw run against another run or the golden"
    )
    p_bench_compare.add_argument("candidate")
    p_bench_compare.add_argument("baseline", help="raw run dir or 'golden'")
    p_bench_compare.add_argument("--corpus", default="corpus")
    p_bench_compare.add_argument(
        "--image-threshold", type=float, default=10.0, metavar="PCT"
    )
    p_bench_compare.add_argument(
        "--wall-threshold", type=float, default=25.0, metavar="PCT"
    )
    p_bench_compare.add_argument(
        "--iteration-threshold", type=int, default=0, metavar="N"
    )
    p_bench_compare.set_defaults(func=_cmd_bench_compare)
```

Imports: `load_baseline`, `compare_runs`, `promote_run`, `PromoteRefusedError` from `deployer.bench`; `BenchReport` from `deployer.models`.

- [ ] **Step 4: README — extend the Bench section**

```markdown
### Golden baseline

    uv run deployer bench promote .deployer-runs/<ts>-<label> [--corpus corpus] [--force]
    uv run deployer bench compare .deployer-runs/<ts>-<label> golden
    uv run deployer bench compare <runA> <runB>   # raw-vs-raw

`promote` normalizes a raw run (no wall times, paths, hostnames, or check
messages) into `corpus/golden/` (committed) and refuses runs with
mismatched cases unless `--force`. `compare` reports regressions by level:
hard (green→red), important (iteration growth, failure-kind flip, missing
case), advisory (image size, hadolint status, new case; wall time only for
raw-vs-raw). Exit 1 on hard/important findings, 0 otherwise.
```

- [ ] **Step 5: Run tests, full suite, format/lint/typecheck, commit**

```bash
git add src/deployer/cli.py README.md tests/test_cli.py
git commit -m "feat: bench compare CLI with leveled exit codes"
```

---

### Task 6: Error-shape cleanup (CloneError, static-only note, README filter note)

**Files:**
- Modify: `src/deployer/bench.py`, `src/deployer/cli.py`, `README.md`
- Test: `tests/test_bench.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `deployer.bench.CloneError(RuntimeError)` raised by `clone_external` instead of bare `RuntimeError`; `_cmd_bench_run`'s except tuple narrows from `RuntimeError` to `CloneError` (keeping `subprocess.TimeoutExpired`); `_cmd_bench_verify` prints `note: no container runtime found; static-only verification` on success when runtime is None; README documents that `--filter` applies to synthetic cases only.

- [ ] **Step 1: Write the failing tests**

`tests/test_bench.py`:

```python
def test_clone_external_raises_clone_error(tmp_path: Path) -> None:
    from deployer.bench import CloneError

    url, _ = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit="0" * 40)
    with pytest.raises(CloneError):
        clone_external(ext, tmp_path / "scratch")
```

`tests/test_cli.py`:

```python
def test_bench_verify_static_only_prints_note(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert main(["bench", "verify", "--corpus", str(corpus)]) == 0
    assert "static-only" in capsys.readouterr().out
```

- [ ] **Step 2: Implement**

`bench.py`: `class CloneError(RuntimeError): """Cloning an external target failed."""` — `clone_external` raises it (message unchanged). `cli.py`: import it; in `_cmd_bench_run` replace `RuntimeError` with `CloneError` in the except tuple. `_cmd_bench_verify`: after the loop, if `runtime is None` print the note before returning. README Bench section: add the sentence "`--filter` applies to synthetic cases only; external targets are included wholesale via `--include-external`."

(The existing `test_clone_external_bad_commit_raises` uses `pytest.raises(RuntimeError...)` — it still passes since `CloneError` subclasses it; leave it, the new test pins the subtype.)

- [ ] **Step 3: Run tests, full suite, format/lint/typecheck, commit**

```bash
git add src/deployer tests README.md
git commit -m "refactor: CloneError, static-only verify note, filter docs"
```

---

### Task 7: Full sweep + acceptance

- [ ] **Step 1: Full sweep**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest && uv run pytest -m docker`
Expected: all clean/green (docker suite unchanged by Phase 3 — it must still pass).

- [ ] **Step 2: Manual acceptance (local podman)**

```sh
uv run deployer bench run --label golden-candidate          # 5 matched
uv run deployer bench promote .deployer-runs/*-golden-candidate
git status corpus/golden                                    # new files to commit
uv run deployer bench run --label check
uv run deployer bench compare .deployer-runs/*-check golden ; echo $?   # 0, "no regressions" or advisory-only
uv run deployer bench compare .deployer-runs/*-check .deployer-runs/*-golden-candidate ; echo $?  # raw-vs-raw, wall time advisory allowed
```

Commit the promoted golden:

```bash
git add corpus/golden
git commit -m "feat: initial golden baseline from fixture-author run"
```

- [ ] **Step 3: Record results + deferred notes in `.superpowers/sdd/progress.md`** (gitignored, no commit).
