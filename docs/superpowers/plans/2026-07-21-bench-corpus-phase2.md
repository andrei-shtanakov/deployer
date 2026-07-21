# Bench Corpus (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A committed corpus of target projects plus `deployer bench run` / `deployer bench verify` — batch authoring over the corpus with aggregated metrics, offline (no-LLM) by default.

**Architecture:** New `src/deployer/bench.py` owns corpus loading (`BenchCase`), the offline `FixtureAuthor`, per-case scratch execution via the existing `author_dockerfile`, and aggregation into `BenchReport` (JSON + Markdown) under `.deployer-runs/<timestamp>-<label>/`. The CLI gains a `bench` subcommand with `run` and `verify`. Corpus lives in `corpus/synthetic/<case>/` (5 cases seeded from the proven `tests/fixtures/*` projects) plus an `external.toml` manifest for pinned real projects.

**Tech Stack:** Python 3.12, pydantic v2, argparse, tomllib, pytest (`docker` marker), uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-07-21-bench-remote-verify-design.md`, section "Phase 2 — Corpus + bench". Phase 3 (promote/compare) is NOT in scope; `bench run` only writes raw runs into `.deployer-runs/`.

## Global Constraints

- uv only, never pip. After every task: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check` clean; full `uv run pytest` green before each commit.
- Line length 88; type hints everywhere; public APIs get docstrings.
- Branch: all commits to `feature/bench-corpus` (checked out). Never commit to `master`.
- **Offline path is the default**: `--author fixture` needs no API key and no network to an LLM; the LLM author must be selected explicitly (`--author anthropic`). No LLM calls in any test.
- **The corpus is never mutated by a run**: cases are copied to scratch (reusing `deployer.verify.CONTEXT_IGNORE` as the ignore list) before authoring.
- `fixture.Dockerfile` lives in the case dir, NOT inside `project/` (must never enter the build context).
- Raw bench runs go to `.deployer-runs/<timestamp>-<label>/` which is gitignored.
- Comparability metadata recorded per bench run: author backend, corpus commit (= deployer git sha), deployer version, runtime (+ versions), effective timeouts. Per-case `authoring-run.json` files already carry the Phase 1.5 fields.
- Known plan deviation from the spec's corpus listing: the `slow-build` case is deferred to Phase 4 (system-deps hardening) — it is a native-build timeout case and belongs with that work. Record in the ledger.

## Deviation note (spec listing → this plan)

Spec lists 6 synthetic cases; this plan ships 5: `uv-minimal`, `pip-requirements`, `service-healthcheck`, `no-build-system`, `system-deps-psycopg2`. `slow-build` deferred (see Global Constraints).

---

### Task 1: Bench models + `FixtureAuthor`

**Files:**
- Modify: `src/deployer/models.py` (add `ExpectedOutcome`, `BenchCaseResult`, `BenchReport`)
- Create: `src/deployer/bench.py`
- Test: `tests/test_bench.py` (new)

**Interfaces:**
- Consumes: `FailureKind`, `StopReason`, `ContainerRuntime`, `RuntimeVersions`, `AuthorInfo`, `DeployTarget`, `ProjectFacts`, `VerificationReport` from `deployer.models`.
- Produces: `deployer.models.ExpectedOutcome(expected_success: bool = True, max_iterations: int = 3, requires_l2: bool = True, expected_failure_kind: FailureKind | None = None, capabilities: list[str], notes: str = "")`; `deployer.models.BenchCaseResult(case: str, outcome: Literal["matched","mismatched","skipped"], success: bool = False, stopped_reason: StopReason | None = None, iterations: int = 0, image_size_bytes: int | None = None, wall_time_s: float = 0.0, skip_reason: str = "")`; `deployer.models.BenchReport(label, author_backend, corpus_commit, deployer_version, runtime, runtime_versions, build_timeout_s, health_timeout_s, cases)` with properties `success_rate: float | None` and `all_matched: bool`; `deployer.bench.FixtureAuthor(dockerfile: str)` implementing the `DockerfileAuthor` protocol plus `info() -> AuthorInfo`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bench.py`:

```python
"""Bench: models, offline fixture author, corpus loading, orchestration."""

from deployer.bench import FixtureAuthor
from deployer.models import (
    BenchCaseResult,
    BenchReport,
    DeployTarget,
    ExpectedOutcome,
    ProjectFacts,
)


def test_expected_outcome_defaults() -> None:
    expected = ExpectedOutcome()
    assert expected.expected_success is True
    assert expected.max_iterations == 3
    assert expected.requires_l2 is True
    assert expected.expected_failure_kind is None
    assert expected.capabilities == []


def test_bench_report_success_rate_ignores_skipped() -> None:
    report = _report(
        BenchCaseResult(case="a", outcome="matched", success=True),
        BenchCaseResult(case="b", outcome="mismatched", success=False),
        BenchCaseResult(case="c", outcome="skipped", skip_reason="no runtime"),
    )
    assert report.success_rate == 0.5
    assert report.all_matched is False


def test_bench_report_all_skipped_has_no_rate() -> None:
    report = _report(BenchCaseResult(case="a", outcome="skipped"))
    assert report.success_rate is None
    assert report.all_matched is True


def test_bench_report_round_trips_json() -> None:
    report = _report(BenchCaseResult(case="a", outcome="matched", success=True))
    assert BenchReport.model_validate_json(report.model_dump_json()) == report


def _report(*cases: BenchCaseResult) -> BenchReport:
    return BenchReport(
        label="t",
        author_backend="fixture",
        build_timeout_s=600,
        health_timeout_s=30,
        cases=list(cases),
    )


def test_fixture_author_replays_dockerfile_verbatim() -> None:
    author = FixtureAuthor("FROM python:3.12-slim\n")
    facts = ProjectFacts()
    target = DeployTarget()
    generated = author.generate(facts, target)
    assert generated == "FROM python:3.12-slim\n"
    repaired = author.repair(facts, target, generated, _passing_report())
    assert repaired == generated


def test_fixture_author_info() -> None:
    info = FixtureAuthor("FROM x:1\n").info()
    assert info.backend == "fixture"
    assert info.model_id is None
    assert info.prompt_sha256 is not None and len(info.prompt_sha256) == 64


def _passing_report():
    from deployer.models import VerificationReport

    return VerificationReport()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bench.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.bench'` / ImportError for `ExpectedOutcome`.

- [ ] **Step 3: Add models to `src/deployer/models.py`**

Insert after `AuthorInfo`:

```python
class ExpectedOutcome(BaseModel):
    """What a corpus case is expected to do under the authoring loop."""

    expected_success: bool = True
    max_iterations: int = 3
    requires_l2: bool = True
    expected_failure_kind: FailureKind | None = None
    capabilities: list[str] = Field(default_factory=list)
    notes: str = ""


class BenchCaseResult(BaseModel):
    """One corpus case's outcome within a bench run."""

    case: str
    outcome: Literal["matched", "mismatched", "skipped"]
    success: bool = False
    stopped_reason: StopReason | None = None
    iterations: int = 0
    image_size_bytes: int | None = None
    wall_time_s: float = 0.0
    skip_reason: str = ""
    failure_kinds: list[FailureKind] = Field(default_factory=list)


class BenchReport(BaseModel):
    """Aggregate research artifact for one bench run over the corpus."""

    label: str
    author_backend: str
    corpus_commit: str | None = None
    deployer_version: str | None = None
    runtime: ContainerRuntime | None = None
    runtime_versions: RuntimeVersions | None = None
    build_timeout_s: int
    health_timeout_s: int
    cases: list[BenchCaseResult] = Field(default_factory=list)

    @property
    def success_rate(self) -> float | None:
        ran = [c for c in self.cases if c.outcome != "skipped"]
        if not ran:
            return None
        return round(sum(1 for c in ran if c.success) / len(ran), 3)

    @property
    def all_matched(self) -> bool:
        return all(c.outcome != "mismatched" for c in self.cases)
```

Note: `StopReason` is defined AFTER the report models in the current file — place `BenchCaseResult`/`BenchReport` after the `StopReason` alias (bottom of file, after `AuthoringRun`) to avoid a forward reference.

- [ ] **Step 4: Create `src/deployer/bench.py`**

```python
"""Corpus loading, offline fixture author, and bench orchestration."""

import hashlib

from deployer.models import (
    AuthorInfo,
    DeployTarget,
    ProjectFacts,
    VerificationReport,
)


class FixtureAuthor:
    """Offline DockerfileAuthor replaying a case's known-good Dockerfile.

    generate() and repair() both return the fixture verbatim: the bench's
    offline mode measures the verification pipeline, not authoring skill,
    so there is nothing to "repair" — a failing fixture is corpus rot.
    """

    def __init__(self, dockerfile: str) -> None:
        self._dockerfile = dockerfile

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        return self._dockerfile

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        return self._dockerfile

    def info(self) -> AuthorInfo:
        """Comparability metadata: fixture hash stands in for a prompt hash."""
        return AuthorInfo(
            backend="fixture",
            prompt_sha256=hashlib.sha256(self._dockerfile.encode()).hexdigest(),
        )
```

- [ ] **Step 5: Run tests, format/lint/typecheck**

Run: `uv run pytest tests/test_bench.py tests/test_models.py -v && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add src/deployer/models.py src/deployer/bench.py tests/test_bench.py
git commit -m "feat: bench models and offline FixtureAuthor"
```

---

### Task 2: `BenchCase` + `load_corpus`

**Files:**
- Modify: `src/deployer/bench.py`
- Test: `tests/test_bench.py` (append)

**Interfaces:**
- Produces: `deployer.bench.BenchCase(name: str, project_dir: Path, target: DeployTarget, expected: ExpectedOutcome, fixture_dockerfile: Path | None)` (pydantic model; `fixture_dockerfile` is None when the file is absent); `deployer.bench.load_corpus(corpus_root: Path, pattern: str = "*") -> list[BenchCase]` — sorted by case name, `fnmatch` filter on the case dir name, raises `FileNotFoundError` when `corpus_root/synthetic` is missing and `ValueError` when a case dir has no `project/`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
import json
from pathlib import Path

import pytest

from deployer.bench import load_corpus


def _make_case(
    root: Path,
    name: str,
    *,
    target: dict | None = None,
    expected: dict | None = None,
    fixture: str | None = "FROM python:3.12-slim\n",
) -> Path:
    case = root / "synthetic" / name
    (case / "project").mkdir(parents=True)
    (case / "project" / "main.py").write_text("print('hi')\n")
    if target is not None:
        (case / "target.json").write_text(json.dumps(target))
    if expected is not None:
        (case / "expected.json").write_text(json.dumps(expected))
    if fixture is not None:
        (case / "fixture.Dockerfile").write_text(fixture)
    return case


def test_load_corpus_reads_case_files(tmp_path: Path) -> None:
    _make_case(
        tmp_path,
        "svc",
        target={"service": {"port": 8000, "healthcheck_path": "/health"}},
        expected={"capabilities": ["service"], "max_iterations": 2},
    )
    cases = load_corpus(tmp_path)
    assert len(cases) == 1
    case = cases[0]
    assert case.name == "svc"
    assert case.target.service is not None and case.target.service.port == 8000
    assert case.expected.max_iterations == 2
    assert case.fixture_dockerfile is not None


def test_load_corpus_defaults_when_files_absent(tmp_path: Path) -> None:
    _make_case(tmp_path, "bare", fixture=None)
    case = load_corpus(tmp_path)[0]
    assert case.target == DeployTarget()
    assert case.expected == ExpectedOutcome()
    assert case.fixture_dockerfile is None


def test_load_corpus_sorted_and_filtered(tmp_path: Path) -> None:
    _make_case(tmp_path, "b-two")
    _make_case(tmp_path, "a-one")
    assert [c.name for c in load_corpus(tmp_path)] == ["a-one", "b-two"]
    assert [c.name for c in load_corpus(tmp_path, "a-*")] == ["a-one"]


def test_load_corpus_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path / "nope")


def test_load_corpus_case_without_project_raises(tmp_path: Path) -> None:
    (tmp_path / "synthetic" / "broken").mkdir(parents=True)
    with pytest.raises(ValueError, match="broken"):
        load_corpus(tmp_path)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py -v -k corpus`
Expected: FAIL — ImportError `load_corpus`.

- [ ] **Step 3: Implement in `src/deployer/bench.py`**

Add imports `import fnmatch`, `from pathlib import Path`, and `ExpectedOutcome` to the models import; then:

```python
class BenchCase(BaseModel):
    """One corpus case: a target project plus intent and expectations."""

    name: str
    project_dir: Path
    target: DeployTarget = Field(default_factory=DeployTarget)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    fixture_dockerfile: Path | None = None


def load_corpus(corpus_root: Path, pattern: str = "*") -> list[BenchCase]:
    """Load synthetic corpus cases whose directory name matches `pattern`."""
    synthetic = corpus_root / "synthetic"
    if not synthetic.is_dir():
        raise FileNotFoundError(f"no synthetic corpus at {synthetic}")
    cases: list[BenchCase] = []
    for case_dir in sorted(p for p in synthetic.iterdir() if p.is_dir()):
        if not fnmatch.fnmatch(case_dir.name, pattern):
            continue
        project_dir = case_dir / "project"
        if not project_dir.is_dir():
            raise ValueError(f"corpus case {case_dir.name} has no project/ dir")
        target_file = case_dir / "target.json"
        target = (
            DeployTarget.model_validate_json(target_file.read_text())
            if target_file.is_file()
            else DeployTarget()
        )
        expected_file = case_dir / "expected.json"
        expected = (
            ExpectedOutcome.model_validate_json(expected_file.read_text())
            if expected_file.is_file()
            else ExpectedOutcome()
        )
        fixture = case_dir / "fixture.Dockerfile"
        cases.append(
            BenchCase(
                name=case_dir.name,
                project_dir=project_dir,
                target=target,
                expected=expected,
                fixture_dockerfile=fixture if fixture.is_file() else None,
            )
        )
    return cases
```

(`BaseModel` and `Field` come from `pydantic`; add the import.)

- [ ] **Step 4: Run tests, format/lint/typecheck, commit**

Run: `uv run pytest tests/test_bench.py -v && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer/bench.py tests/test_bench.py
git commit -m "feat: BenchCase and load_corpus with glob filtering"
```

---

### Task 3: Synthetic corpus content (5 cases) + gitignore + docker corpus smoke

**Files:**
- Create: `corpus/synthetic/{uv-minimal,pip-requirements,service-healthcheck,no-build-system,system-deps-psycopg2}/...`
- Modify: `.gitignore` (add `.deployer-runs/`)
- Test: `tests/test_corpus.py` (new)

**Interfaces:**
- Consumes: `load_corpus`, `BenchCase` (Task 2); `deployer.facts.analyze_project`, `deployer.verify.verify`, and the `runtime` fixture pattern from `tests/test_verify_docker.py`.
- Produces: the committed corpus; every case has `project/`, `expected.json`, `fixture.Dockerfile`; service cases have `target.json`.

- [ ] **Step 1: Seed the three service cases from proven test fixtures**

```bash
mkdir -p corpus/synthetic
for pair in "service-healthcheck hello_service" "pip-requirements pip_service" "system-deps-psycopg2 sysdep_service"; do
  set -- $pair
  mkdir -p "corpus/synthetic/$1"
  cp -R "tests/fixtures/$2" "corpus/synthetic/$1/project"
  mv "corpus/synthetic/$1/project/Dockerfile.good" "corpus/synthetic/$1/fixture.Dockerfile"
done
```

Confirm each `project/` now contains no Dockerfile (`ls corpus/synthetic/*/project`). Read each `main.py` to confirm the serving port (tests use 8000).

- [ ] **Step 2: Write target.json + expected.json for the seeded cases**

`corpus/synthetic/service-healthcheck/target.json` (and identically for `pip-requirements`):

```json
{"service": {"port": 8000, "healthcheck_path": "/health"}}
```

`corpus/synthetic/system-deps-psycopg2/target.json`:

```json
{"service": {"port": 8000, "healthcheck_path": "/health"}}
```

`corpus/synthetic/service-healthcheck/expected.json`:

```json
{"capabilities": ["service"], "notes": "stdlib HTTP service; the simplest green path"}
```

`corpus/synthetic/pip-requirements/expected.json`:

```json
{"capabilities": ["pip", "service"], "notes": "requirements.txt-only project, no pyproject"}
```

`corpus/synthetic/system-deps-psycopg2/expected.json`:

```json
{"capabilities": ["pip", "service", "system-deps"], "notes": "psycopg2 source build; needs apt build+runtime packages from the hints table"}
```

If a seeded `main.py` serves on a different port or path, fix target.json to match the code (the code is ground truth), and note it in the report.

- [ ] **Step 3: Author the two uv cases**

`corpus/synthetic/uv-minimal/project/pyproject.toml`:

```toml
[project]
name = "uv-minimal"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

`corpus/synthetic/uv-minimal/project/src/uv_minimal/__init__.py`:

```python
"""Smallest possible uv project with a build system."""

GREETING = "hello from uv-minimal"
```

`corpus/synthetic/no-build-system/project/pyproject.toml`:

```toml
[project]
name = "no-build-system"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []
```

`corpus/synthetic/no-build-system/project/main.py`:

```python
"""Script-style project: no [build-system], must not be pip-installed."""

print("hello from no-build-system")
```

Generate lockfiles (committed — they are facts the scanner reads):

```bash
(cd corpus/synthetic/uv-minimal/project && uv lock)
(cd corpus/synthetic/no-build-system/project && uv lock)
```

`corpus/synthetic/uv-minimal/expected.json`:

```json
{"capabilities": ["uv"], "notes": "uv.lock + build-system; build-only target (no service)"}
```

`corpus/synthetic/no-build-system/expected.json`:

```json
{"capabilities": ["uv", "no-build-system"], "notes": "must not install the project as a package"}
```

No `target.json` for either (build-only intent, `DeployTarget()` defaults).

`corpus/synthetic/uv-minimal/fixture.Dockerfile`:

```dockerfile
FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen
CMD ["uv", "run", "python", "-c", "import uv_minimal; print(uv_minimal.GREETING)"]
```

`corpus/synthetic/no-build-system/fixture.Dockerfile`:

```dockerfile
FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY main.py ./
CMD ["uv", "run", "--no-sync", "python", "main.py"]
```

(If `uv sync --frozen` fails for `uv-minimal` in the docker smoke because hatchling needs the src tree earlier, adjust COPY order — the smoke test is the arbiter; keep the base images pinned.)

- [ ] **Step 4: Add `.deployer-runs/` to `.gitignore`**

Append the line `.deployer-runs/` to `.gitignore`.

- [ ] **Step 5: Write the corpus tests**

Create `tests/test_corpus.py`:

```python
"""The committed corpus itself: parses, and every fixture verifies green."""

from pathlib import Path

import pytest

from deployer.bench import load_corpus
from deployer.facts import analyze_project
from deployer.models import ContainerRuntime
from deployer.runtime import resolve_runtime
from deployer.verify import verify

CORPUS = Path(__file__).parent.parent / "corpus"
EXPECTED_CASES = [
    "no-build-system",
    "pip-requirements",
    "service-healthcheck",
    "system-deps-psycopg2",
    "uv-minimal",
]


def test_corpus_parses_and_is_complete() -> None:
    cases = load_corpus(CORPUS)
    assert [c.name for c in cases] == EXPECTED_CASES
    for case in cases:
        assert case.fixture_dockerfile is not None, case.name
        assert not (case.project_dir / "Dockerfile").exists(), case.name
        assert not (case.project_dir / "fixture.Dockerfile").exists(), case.name


def test_corpus_static_checks_pass_for_every_fixture() -> None:
    for case in load_corpus(CORPUS):
        assert case.fixture_dockerfile is not None
        report = verify(
            case.fixture_dockerfile.read_text(),
            case.project_dir,
            case.target,
            None,
            analyze_project(case.project_dir),
        )
        assert report.passed, f"{case.name}: {report.model_dump_json(indent=2)}"


@pytest.fixture(scope="module")
def runtime() -> ContainerRuntime:
    found = resolve_runtime()
    if found is None:
        pytest.skip("no container runtime available")
    return found


@pytest.mark.docker
@pytest.mark.parametrize("name", EXPECTED_CASES)
def test_corpus_fixture_verifies_end_to_end(name: str, runtime) -> None:
    case = {c.name: c for c in load_corpus(CORPUS)}[name]
    assert case.fixture_dockerfile is not None
    report = verify(
        case.fixture_dockerfile.read_text(),
        case.project_dir,
        case.target,
        runtime,
        analyze_project(case.project_dir),
    )
    assert report.passed, f"{name}: {report.model_dump_json(indent=2)}"
```

- [ ] **Step 6: Run tests (unit, then docker), iterate on fixtures until green**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: unit tests PASS.
Run: `uv run pytest tests/test_corpus.py -m docker -v` (long: psycopg2 source build)
Expected: 5/5 PASS. If the uv-case fixtures fail, fix the Dockerfile (COPY order, uv image tag) — the project files and targets are the fixed points.

- [ ] **Step 7: Format/lint/typecheck, commit**

```bash
git add corpus .gitignore tests/test_corpus.py
git commit -m "feat: synthetic corpus (5 cases) with verified fixture Dockerfiles"
```

---

### Task 4: `run_case` + `run_bench` + report writers

**Files:**
- Modify: `src/deployer/bench.py`
- Test: `tests/test_bench.py` (append)

**Interfaces:**
- Consumes: `author_dockerfile` and `_deployer_version`/`_deployer_git_sha` from `deployer.author`; `CONTEXT_IGNORE`, `DEFAULT_BUILD_TIMEOUT`, `DEFAULT_HEALTH_TIMEOUT` from `deployer.verify`; `probe_runtime_versions` from `deployer.runtime`; `DockerfileAuthor` protocol from `deployer.author`.
- Produces: `run_case(case: BenchCase, author: DockerfileAuthor | None, runtime: ContainerRuntime | None, case_out_dir: Path, *, build_timeout: int, health_timeout: int) -> BenchCaseResult`; `run_bench(corpus_root: Path, make_author: Callable[[BenchCase], DockerfileAuthor | None], runtime: ContainerRuntime | None, *, label: str, author_backend: str, pattern: str = "*", runs_root: Path = Path(".deployer-runs"), build_timeout: int = DEFAULT_BUILD_TIMEOUT, health_timeout: int = DEFAULT_HEALTH_TIMEOUT) -> tuple[BenchReport, Path]`; `render_markdown(report: BenchReport) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
from deployer.bench import BenchCase, render_markdown, run_bench, run_case
from deployer.models import (
    AuthoringRun,
    CheckResult,
    CheckStatus,
    IterationRecord,
    VerificationReport,
)


def _fake_run(success: bool) -> AuthoringRun:
    report = VerificationReport(
        results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)],
        image_size_bytes=123_000_000 if success else None,
    )
    return AuthoringRun(
        project="x",
        target=DeployTarget(),
        iterations=[
            IterationRecord(index=0, dockerfile="FROM x:1\n", report=report, duration_s=0.1)
        ],
        stopped_reason="success" if success else "no_progress",
        success=success,
    )


def test_run_case_skips_l2_case_without_runtime(tmp_path: Path) -> None:
    _make_case(tmp_path, "svc")
    case = load_corpus(tmp_path)[0]
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, tmp_path / "out",
        build_timeout=600, health_timeout=30,
    )
    assert result.outcome == "skipped"
    assert "runtime" in result.skip_reason


def test_run_case_skips_when_author_missing(tmp_path: Path) -> None:
    _make_case(tmp_path, "svc", fixture=None)
    case = load_corpus(tmp_path)[0]
    result = run_case(
        case, None, ContainerRuntime(tool="docker"), tmp_path / "out",
        build_timeout=600, health_timeout=30,
    )
    assert result.outcome == "skipped"
    assert "fixture" in result.skip_reason


def test_run_case_runs_in_scratch_and_writes_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    (tmp_path / "synthetic" / "svc" / "project" / ".env").write_text("S=1\n")
    case = load_corpus(tmp_path)[0]
    seen: dict = {}

    def fake_author_dockerfile(project_path, target, author, **kwargs):
        seen["project_path"] = Path(project_path)
        seen["kwargs"] = kwargs
        return _fake_run(True)

    monkeypatch.setattr("deployer.bench.author_dockerfile", fake_author_dockerfile)
    out = tmp_path / "out"
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, out,
        build_timeout=99, health_timeout=9,
    )
    assert seen["project_path"] != case.project_dir  # scratch copy, not corpus
    assert not (seen["project_path"] / ".env").exists()  # CONTEXT_IGNORE applied
    assert seen["kwargs"]["max_iterations"] == case.expected.max_iterations
    assert seen["kwargs"]["build_timeout"] == 99
    assert result.outcome == "matched" and result.success
    assert result.iterations == 1
    assert result.image_size_bytes == 123_000_000
    assert (out / "authoring-run.json").is_file()
    assert (out / "Dockerfile").read_text() == "FROM x:1\n\n"
    assert not (case.project_dir / ".deployer").exists()  # corpus untouched


def test_run_case_mismatch_when_expectation_violated(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    case = load_corpus(tmp_path)[0]
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile",
        lambda *a, **k: _fake_run(False),
    )
    result = run_case(
        case, FixtureAuthor("FROM x:1\n"), None, tmp_path / "out",
        build_timeout=600, health_timeout=30,
    )
    assert result.outcome == "mismatched"
    assert result.stopped_reason == "no_progress"


def test_run_bench_aggregates_and_writes_reports(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "a-ok", expected={"requires_l2": False})
    _make_case(tmp_path, "b-l2")  # requires_l2 default True -> skipped (no runtime)
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    report, run_dir = run_bench(
        tmp_path,
        lambda case: FixtureAuthor("FROM x:1\n"),
        None,
        label="unit",
        author_backend="fixture",
        runs_root=tmp_path / "runs",
    )
    assert [c.outcome for c in report.cases] == ["matched", "skipped"]
    assert report.label == "unit"
    assert run_dir.name.endswith("-unit")
    assert (run_dir / "bench-report.json").is_file()
    md = (run_dir / "bench-report.md").read_text()
    assert "a-ok" in md and "skipped" in md


def test_run_bench_no_matching_cases_raises(tmp_path: Path) -> None:
    _make_case(tmp_path, "only")
    with pytest.raises(ValueError, match="no corpus cases"):
        run_bench(
            tmp_path, lambda c: None, None,
            label="x", author_backend="fixture",
            pattern="zzz*", runs_root=tmp_path / "runs",
        )


def test_render_markdown_has_table_and_metadata() -> None:
    report = _report(
        BenchCaseResult(
            case="a", outcome="matched", success=True,
            stopped_reason="success", iterations=2,
            image_size_bytes=45_600_000, wall_time_s=12.5,
        )
    )
    md = render_markdown(report)
    assert "| a | matched | success | 2 | 45.6 | 12.5 |" in md
    assert "author: fixture" in md
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py -v -k "run_case or run_bench or markdown"`
Expected: FAIL — ImportError `run_case`.

- [ ] **Step 3: Implement in `src/deployer/bench.py`**

Add imports:

```python
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import datetime

from deployer.author import (
    DockerfileAuthor,
    _deployer_git_sha,
    _deployer_version,
    author_dockerfile,
)
from deployer.models import (
    BenchCaseResult,
    BenchReport,
    ContainerRuntime,
)
from deployer.runtime import probe_runtime_versions
from deployer.verify import CONTEXT_IGNORE, DEFAULT_BUILD_TIMEOUT, DEFAULT_HEALTH_TIMEOUT
```

Implementation:

```python
def run_case(
    case: BenchCase,
    author: DockerfileAuthor | None,
    runtime: ContainerRuntime | None,
    case_out_dir: Path,
    *,
    build_timeout: int,
    health_timeout: int,
) -> BenchCaseResult:
    """Author one corpus case in a scratch copy; never mutates the corpus."""
    if case.expected.requires_l2 and runtime is None:
        return BenchCaseResult(
            case=case.name,
            outcome="skipped",
            skip_reason="case requires L2 but no container runtime resolved",
        )
    if author is None:
        return BenchCaseResult(
            case=case.name,
            outcome="skipped",
            skip_reason="no fixture.Dockerfile for the offline fixture author",
        )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"deployer-bench-{case.name}-") as tmp:
        scratch = Path(tmp) / "project"
        shutil.copytree(
            case.project_dir,
            scratch,
            symlinks=True,
            ignore=shutil.ignore_patterns(*CONTEXT_IGNORE),
        )
        run = author_dockerfile(
            scratch,
            case.target,
            author,
            max_iterations=case.expected.max_iterations,
            runtime=runtime,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
    wall = time.monotonic() - started
    case_out_dir.mkdir(parents=True, exist_ok=True)
    (case_out_dir / "authoring-run.json").write_text(run.model_dump_json(indent=2))
    last = run.iterations[-1] if run.iterations else None
    if last is not None:
        (case_out_dir / "Dockerfile").write_text(last.dockerfile + "\n")
    failure_kinds = sorted(
        {
            r.failure_kind
            for it in run.iterations
            for r in it.report.results
            if r.failure_kind is not None
        }
    )
    return BenchCaseResult(
        case=case.name,
        outcome="matched" if run.success == case.expected.expected_success
        else "mismatched",
        success=run.success,
        stopped_reason=run.stopped_reason,
        iterations=len(run.iterations),
        image_size_bytes=last.report.image_size_bytes if last else None,
        wall_time_s=round(wall, 3),
        failure_kinds=failure_kinds,
    )


def run_bench(
    corpus_root: Path,
    make_author: Callable[[BenchCase], DockerfileAuthor | None],
    runtime: ContainerRuntime | None,
    *,
    label: str,
    author_backend: str,
    pattern: str = "*",
    runs_root: Path = Path(".deployer-runs"),
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> tuple[BenchReport, Path]:
    """Run the authoring loop over every matching corpus case and aggregate."""
    cases = load_corpus(corpus_root, pattern)
    if not cases:
        raise ValueError(f"no corpus cases match pattern {pattern!r}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = runs_root / f"{stamp}-{label}"
    run_dir.mkdir(parents=True, exist_ok=False)
    results = [
        run_case(
            case,
            make_author(case),
            runtime,
            run_dir / "cases" / case.name,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        for case in cases
    ]
    report = BenchReport(
        label=label,
        author_backend=author_backend,
        corpus_commit=_deployer_git_sha(),
        deployer_version=_deployer_version(),
        runtime=runtime,
        runtime_versions=(
            probe_runtime_versions(runtime) if runtime is not None else None
        ),
        build_timeout_s=build_timeout,
        health_timeout_s=health_timeout,
        cases=results,
    )
    (run_dir / "bench-report.json").write_text(report.model_dump_json(indent=2))
    (run_dir / "bench-report.md").write_text(render_markdown(report))
    return report, run_dir


def render_markdown(report: BenchReport) -> str:
    """Human-readable summary table for one bench run."""
    if report.runtime is None:
        runtime_line = "static-only"
    elif report.runtime.host:
        runtime_line = f"{report.runtime.tool} @ {report.runtime.host}"
    else:
        runtime_line = f"{report.runtime.tool} (local)"
    rate = report.success_rate
    lines = [
        f"# Bench run: {report.label}",
        "",
        f"- author: {report.author_backend}",
        f"- corpus commit: {report.corpus_commit or 'unknown'}",
        f"- deployer: {report.deployer_version or 'unknown'}",
        f"- runtime: {runtime_line}",
        f"- timeouts: build {report.build_timeout_s}s / health {report.health_timeout_s}s",
        f"- success rate: {rate if rate is not None else 'n/a'}",
        "",
        "| case | outcome | stop reason | iters | image MB | wall s |",
        "|---|---|---|---:|---:|---:|",
    ]
    for c in report.cases:
        size = f"{c.image_size_bytes / 1e6:.1f}" if c.image_size_bytes else "-"
        lines.append(
            f"| {c.case} | {c.outcome} | {c.stopped_reason or '-'} "
            f"| {c.iterations} | {size} | {c.wall_time_s:.1f} |"
        )
    return "\n".join(lines) + "\n"
```

Note on private imports: `_deployer_version`/`_deployer_git_sha` are package-internal helpers in `deployer.author` — importing them inside the same distribution is deliberate; do not copy-paste their bodies (DRY).

- [ ] **Step 4: Run tests, format/lint/typecheck, commit**

Run: `uv run pytest tests/test_bench.py -v && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer/bench.py tests/test_bench.py
git commit -m "feat: bench orchestration (run_case/run_bench) and report writers"
```

---

### Task 5: `verify_corpus` + CLI `bench run` / `bench verify` + README

**Files:**
- Modify: `src/deployer/bench.py` (add `verify_corpus`)
- Modify: `src/deployer/cli.py`
- Modify: `README.md`
- Test: `tests/test_cli.py` (append), `tests/test_corpus.py` (append one docker e2e)

**Interfaces:**
- Consumes: everything from Tasks 1–4; `_resolve_runtime_or_error`, `_add_runtime_flags`, `_add_timeout_flags`, `_timeout_error` in `cli.py`; `AnthropicAuthor` from `deployer.llm`; `analyze_project`, `verify`.
- Produces: `verify_corpus(corpus_root: Path, runtime: ContainerRuntime | None, *, pattern: str = "*", build_timeout: int, health_timeout: int) -> list[tuple[str, VerificationReport]]` (raises `ValueError` when a matched case lacks `fixture.Dockerfile`); CLI surface `deployer bench run [--corpus PATH] [--filter GLOB] [--label NAME] [--author fixture|anthropic] [runtime flags] [timeout flags]` and `deployer bench verify [--corpus PATH] [--filter GLOB] [runtime flags] [timeout flags]`. Exit codes: bench run — 0 all matched, 1 any mismatch, 2 usage; bench verify — 0 all fixtures pass, 1 any fail, 2 usage. Label must match `[A-Za-z0-9._-]+`.

- [ ] **Step 1: Write the failing CLI tests**

Append to `tests/test_cli.py` (reuse the file's existing project-scaffold/mocking patterns; `_make_corpus` builds a minimal corpus in tmp_path exactly like `tests/test_bench.py::_make_case` — small local copy here is acceptable test scaffolding):

```python
def _make_corpus(tmp_path, name="case-one", requires_l2=False):
    import json as _json

    case = tmp_path / "corpus" / "synthetic" / name
    (case / "project").mkdir(parents=True)
    (case / "project" / "main.py").write_text("print('hi')\n")
    (case / "expected.json").write_text(
        _json.dumps({"requires_l2": requires_l2})
    )
    (case / "fixture.Dockerfile").write_text("FROM python:3.12-slim\n")
    return tmp_path / "corpus"


def test_bench_run_offline_exits_0_on_match(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    code = main(["bench", "run", "--corpus", str(corpus), "--label", "t"])
    assert code == 0
    out = capsys.readouterr().out
    assert "case-one" in out and "bench-report" in out


def test_bench_run_bad_label_exits_2(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    code = main(["bench", "run", "--corpus", str(corpus), "--label", "a/b"])
    assert code == 2
    assert "label" in capsys.readouterr().err


def test_bench_run_missing_corpus_exits_2(tmp_path, capsys):
    code = main(["bench", "run", "--corpus", str(tmp_path / "nope")])
    assert code == 2


def test_bench_run_anthropic_requires_explicit_flag(tmp_path, monkeypatch):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.AnthropicAuthor",
        lambda: pytest.fail("AnthropicAuthor must not be constructed by default"),
    )
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0


def test_bench_verify_static_only_pass_exits_0(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    code = main(["bench", "verify", "--corpus", str(corpus)])
    assert code == 0
    assert "case-one" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -v -k bench`
Expected: FAIL — `invalid choice: 'bench'`.

- [ ] **Step 3: Add `verify_corpus` to `src/deployer/bench.py`**

```python
def verify_corpus(
    corpus_root: Path,
    runtime: ContainerRuntime | None,
    *,
    pattern: str = "*",
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> list[tuple[str, VerificationReport]]:
    """Verify each case's committed fixture.Dockerfile. No LLM, no authoring."""
    from deployer.facts import analyze_project
    from deployer.verify import verify

    results: list[tuple[str, VerificationReport]] = []
    for case in load_corpus(corpus_root, pattern):
        if case.fixture_dockerfile is None:
            raise ValueError(f"corpus case {case.name} has no fixture.Dockerfile")
        report = verify(
            case.fixture_dockerfile.read_text(),
            case.project_dir,
            case.target,
            runtime,
            analyze_project(case.project_dir),
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        results.append((case.name, report))
    return results
```

(Move the two imports to module top with the rest — shown local here only to keep the fragment self-contained.)

- [ ] **Step 4: Wire the CLI in `src/deployer/cli.py`**

Imports:

```python
from deployer.bench import FixtureAuthor, run_bench, verify_corpus
```

Command implementations (place near the other `_cmd_*`):

```python
_LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")


def _cmd_bench_run(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"error: {corpus} is not a directory", file=sys.stderr)
        return 2
    if not _LABEL_RE.fullmatch(args.label):
        print("error: --label must match [A-Za-z0-9._-]+", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    runtime = _resolve_runtime_or_error(args)
    if isinstance(runtime, str):
        print(f"error: {runtime}", file=sys.stderr)
        return 2
    if args.author == "anthropic":
        shared = AnthropicAuthor()
        make_author = lambda case: shared  # noqa: E731
    else:
        make_author = lambda case: (  # noqa: E731
            FixtureAuthor(case.fixture_dockerfile.read_text())
            if case.fixture_dockerfile is not None
            else None
        )
    try:
        report, run_dir = run_bench(
            corpus,
            make_author,
            runtime,
            label=args.label,
            author_backend=args.author,
            pattern=args.filter_pattern,
            build_timeout=args.build_timeout,
            health_timeout=args.health_timeout,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for case in report.cases:
        line = f"[{case.outcome:>10}] {case.case}"
        if case.skip_reason:
            line += f": {case.skip_reason}"
        print(line)
    rate = report.success_rate
    print(f"success rate: {rate if rate is not None else 'n/a'}")
    print(f"bench-report: {run_dir / 'bench-report.json'}")
    print(f"markdown: {run_dir / 'bench-report.md'}")
    return 0 if report.all_matched else 1


def _cmd_bench_verify(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"error: {corpus} is not a directory", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    runtime = _resolve_runtime_or_error(args)
    if isinstance(runtime, str):
        print(f"error: {runtime}", file=sys.stderr)
        return 2
    try:
        results = verify_corpus(
            corpus,
            runtime,
            pattern=args.filter_pattern,
            build_timeout=args.build_timeout,
            health_timeout=args.health_timeout,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failed = False
    for name, report in results:
        status = "ok" if report.passed else "FAIL"
        print(f"[{status:>4}] {name}")
        if not report.passed:
            failed = True
            _print_report(report)
    return 1 if failed else 0
```

Parser wiring in `main()` (after the `author` subparser):

```python
    p_bench = sub.add_parser("bench", help="corpus bench operations")
    bench_sub = p_bench.add_subparsers(dest="bench_command", required=True)

    p_bench_run = bench_sub.add_parser(
        "run", help="author every corpus case and aggregate metrics"
    )
    p_bench_run.add_argument("--corpus", default="corpus")
    p_bench_run.add_argument(
        "--filter", default="*", dest="filter_pattern", metavar="GLOB"
    )
    p_bench_run.add_argument("--label", default="run")
    p_bench_run.add_argument(
        "--author", choices=("fixture", "anthropic"), default="fixture",
        help="fixture (offline, default) or anthropic (real LLM, costs money)",
    )
    _add_runtime_flags(p_bench_run)
    _add_timeout_flags(p_bench_run)
    p_bench_run.set_defaults(func=_cmd_bench_run)

    p_bench_verify = bench_sub.add_parser(
        "verify", help="verify each case's committed fixture.Dockerfile"
    )
    p_bench_verify.add_argument("--corpus", default="corpus")
    p_bench_verify.add_argument(
        "--filter", default="*", dest="filter_pattern", metavar="GLOB"
    )
    _add_runtime_flags(p_bench_verify)
    _add_timeout_flags(p_bench_verify)
    p_bench_verify.set_defaults(func=_cmd_bench_verify)
```

(`import re` and `from pathlib import Path` are already present or trivially added.)

- [ ] **Step 5: Add one docker-marked CLI e2e to `tests/test_corpus.py`**

```python
@pytest.mark.docker
def test_bench_run_offline_single_case_end_to_end(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    from deployer.cli import main

    monkeypatch.chdir(tmp_path)
    code = main(
        [
            "bench", "run",
            "--corpus", str(CORPUS),
            "--filter", "service-healthcheck",
            "--label", "smoke",
        ]
    )
    assert code == 0
    runs = list((tmp_path / ".deployer-runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "cases" / "service-healthcheck" / "authoring-run.json").is_file()
```

- [ ] **Step 6: README — Bench section**

Add after the Usage section:

```markdown
## Bench

The corpus (`corpus/synthetic/`) is a set of small target projects with
declared intent (`target.json`) and expectations (`expected.json`).

    uv run deployer bench run [--corpus corpus] [--filter GLOB] [--label NAME] \
        [--author fixture|anthropic] [runtime/timeout flags]
    uv run deployer bench verify [--corpus corpus] [--filter GLOB]

`bench run` authors every case in a scratch copy and writes the raw run
(per-case `authoring-run.json` + final Dockerfile, aggregate
`bench-report.json` + `bench-report.md`) under `.deployer-runs/<ts>-<label>/`
(gitignored). The default author is `fixture` — it replays each case's
committed `fixture.Dockerfile`, needs no API key, and measures the
verification pipeline. `--author anthropic` runs the real LLM and spends
money; select it explicitly. `bench verify` just verifies the committed
fixtures (corpus smoke). Exit codes: 0 all matched/passed, 1 mismatch/fail,
2 invalid invocation. Cases with `requires_l2: true` are skipped (not
failed) when no container runtime is available.
```

- [ ] **Step 7: Run everything, format/lint/typecheck, commit**

Run: `uv run pytest && uv run pytest -m docker && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: all green (docker corpus suite is slow — psycopg2 source build).

```bash
git add src/deployer/bench.py src/deployer/cli.py README.md tests/test_cli.py tests/test_corpus.py
git commit -m "feat: deployer bench run/verify CLI with offline default"
```

---

### Task 6: External manifest (`external.toml`) + clone-at-pin

**Files:**
- Modify: `src/deployer/bench.py`, `src/deployer/models.py` (add `ExternalTarget`)
- Create: `corpus/external.toml` (documented, zero entries)
- Modify: `src/deployer/cli.py` (`--include-external` on `bench run`)
- Test: `tests/test_bench.py` (append)

**Interfaces:**
- Produces: `deployer.models.ExternalTarget(name: str, url: str, commit: str, target: DeployTarget, expected: ExpectedOutcome)`; `deployer.bench.load_external(corpus_root: Path) -> list[ExternalTarget]` (tolerates a missing file → `[]`); `deployer.bench.clone_external(ext: ExternalTarget, dest_root: Path) -> BenchCase` (clones at the pinned commit via `git init/fetch --depth 1/checkout FETCH_HEAD`; raises `RuntimeError` on git failure; resulting `BenchCase` has `fixture_dockerfile=None`); `run_bench(..., include_external: bool = False)` appends external cases after synthetic ones.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
import subprocess

from deployer.bench import clone_external, load_external
from deployer.models import ExternalTarget


def _make_local_git_repo(root: Path) -> tuple[str, str]:
    repo = root / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "main.py").write_text("print('v1')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    env_commit = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        ["git", *env_commit, "commit", "-qm", "v1"], cwd=repo, check=True
    )
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    (repo / "main.py").write_text("print('v2')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", *env_commit, "commit", "-qm", "v2"], cwd=repo, check=True
    )
    return str(repo), pinned


def test_load_external_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_external(tmp_path) == []


def test_load_external_parses_entries(tmp_path: Path) -> None:
    (tmp_path / "external.toml").write_text(
        '[[targets]]\n'
        'name = "demo"\n'
        'url = "https://example.invalid/demo.git"\n'
        'commit = "abc123"\n'
        '[targets.expected]\n'
        'expected_success = false\n'
    )
    targets = load_external(tmp_path)
    assert len(targets) == 1
    assert targets[0].name == "demo"
    assert targets[0].expected.expected_success is False


def test_clone_external_checks_out_pinned_commit(tmp_path: Path) -> None:
    url, pinned = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit=pinned)
    case = clone_external(ext, tmp_path / "scratch")
    assert case.name == "demo"
    assert (case.project_dir / "main.py").read_text() == "print('v1')\n"
    assert case.fixture_dockerfile is None


def test_clone_external_bad_commit_raises(tmp_path: Path) -> None:
    url, _ = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit="0" * 40)
    with pytest.raises(RuntimeError, match="demo"):
        clone_external(ext, tmp_path / "scratch")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_bench.py -v -k external`
Expected: FAIL — ImportError `load_external`.

- [ ] **Step 3: Implement**

`src/deployer/models.py`, after `ExpectedOutcome` (note: `ExpectedOutcome` must be defined before it):

```python
class ExternalTarget(BaseModel):
    """A pinned real-world project consumed by the bench via cloning."""

    name: str
    url: str
    commit: str
    target: DeployTarget = Field(default_factory=DeployTarget)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
```

`src/deployer/bench.py` (add `import subprocess`, `import tomllib`, `ExternalTarget` import):

```python
def load_external(corpus_root: Path) -> list[ExternalTarget]:
    """Parse corpus/external.toml; a missing file means no external targets."""
    manifest = corpus_root / "external.toml"
    if not manifest.is_file():
        return []
    data = tomllib.loads(manifest.read_text())
    return [ExternalTarget.model_validate(t) for t in data.get("targets", [])]


def clone_external(ext: ExternalTarget, dest_root: Path) -> BenchCase:
    """Clone an external target at its pinned commit into dest_root/<name>."""
    dest = dest_root / ext.name
    dest.mkdir(parents=True, exist_ok=True)
    commands = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", ext.url],
        ["git", "fetch", "-q", "--depth", "1", "origin", ext.commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for command in commands:
        proc = subprocess.run(
            command, cwd=dest, capture_output=True, text=True, timeout=300
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"cloning external target {ext.name} failed at "
                f"{' '.join(command)}: {proc.stderr.strip()}"
            )
    return BenchCase(
        name=ext.name,
        project_dir=dest,
        target=ext.target,
        expected=ext.expected,
        fixture_dockerfile=None,
    )
```

`run_bench` change — new keyword `include_external: bool = False`; after loading synthetic cases:

```python
    if include_external:
        externals = load_external(corpus_root)
        with tempfile.TemporaryDirectory(prefix="deployer-external-") as ext_tmp:
            cases = cases + [
                clone_external(ext, Path(ext_tmp)) for ext in externals
            ]
            ...  # the whole run loop moves inside this `with` when externals exist
```

Implementation note: restructure so the case loop runs inside the temp dir's lifetime. Simplest correct shape — extract the loop body into a closure or run the external tempdir around the whole existing body:

```python
    if not include_external:
        return _run_bench_cases(cases, ...)
    with tempfile.TemporaryDirectory(prefix="deployer-external-") as ext_tmp:
        cases += [clone_external(e, Path(ext_tmp)) for e in load_external(corpus_root)]
        return _run_bench_cases(cases, ...)
```

where `_run_bench_cases` is the existing body (case loop + report + writers) factored into a private helper taking `(cases, runtime, make_author, label, author_backend, runs_root, build_timeout, health_timeout)`.

`corpus/external.toml`:

```toml
# Pinned real-world targets for `deployer bench run --include-external`.
# No entries yet: candidates (locallogai) enter after Phase 4 system-deps
# hardening. Format:
#
# [[targets]]
# name = "some-project"
# url = "https://github.com/owner/repo.git"
# commit = "<full sha>"
# [targets.target]        # optional DeployTarget fields
# [targets.expected]      # optional ExpectedOutcome fields
# expected_success = false
```

CLI: `p_bench_run.add_argument("--include-external", action="store_true")` and pass `include_external=args.include_external` to `run_bench`. External cases under the fixture author are skipped by the existing `author is None` path ("no fixture.Dockerfile") — that is the intended offline behavior.

- [ ] **Step 4: Run tests, format/lint/typecheck, commit**

Run: `uv run pytest tests/test_bench.py tests/test_cli.py -v && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer/models.py src/deployer/bench.py src/deployer/cli.py corpus/external.toml tests/test_bench.py
git commit -m "feat: external.toml manifest with clone-at-pin support"
```

---

### Task 7: Full validation sweep

**Files:**
- Modify: `.superpowers/sdd/progress.md` (record; gitignored — no commit needed unless code changed)

- [ ] **Step 1: Full sweep**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest && uv run pytest -m docker`
Expected: all clean/green (docker suite now includes the 5-case corpus verification and the bench CLI smoke — expect several minutes; psycopg2 dominates).

- [ ] **Step 2: Manual acceptance (local)**

```sh
uv run deployer bench verify                       # 5/5 ok, exit 0
uv run deployer bench run --label accept           # 5 matched, exit 0
cat .deployer-runs/*-accept/bench-report.md
uv run deployer bench run --label x --filter zzz   ; echo $?   # exit 2
```

- [ ] **Step 3: Record results in the ledger**

Append outcomes (including the slow-build deferral note) to `.superpowers/sdd/progress.md`.
