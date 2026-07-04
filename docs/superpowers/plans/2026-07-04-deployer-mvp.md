# Deployer MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A library + thin CLI where a deterministic pipeline drives an LLM step to author a working Dockerfile for a Python project, verified by static checks and a sandboxed `docker build` + run + healthcheck.

**Architecture:** Flat package `src/deployer/` with 6 modules: pydantic contracts (`models.py`), deterministic project scanner (`facts.py`), two-level pluggable verification (`verify.py`), the authoring control loop (`author.py`), a thin Anthropic SDK wrapper (`llm.py`), and argparse CLI (`cli.py`). The LLM never writes files or runs commands — the pipeline does. Spec: `docs/superpowers/specs/2026-07-04-deployer-mvp-design.md`.

**Tech Stack:** Python 3.12, uv (never pip), pydantic v2, anthropic SDK (model `claude-opus-4-8`), pytest (+anyio unused for now — everything is sync), ruff, pyrefly. Container runtime: podman preferred, docker fallback, via subprocess.

## Global Constraints

- Package management: ONLY `uv` (`uv add`, `uv run`); NEVER pip.
- Line length 88; type hints on everything; public APIs get docstrings.
- After every task: `uv run ruff format . && uv run ruff check . --fix` then `pyrefly check` — fix errors before committing.
- LLM model string: `claude-opus-4-8` exactly. No `temperature`/`top_p`/`top_k` (400 on this model). The Dockerfile is returned as plain text (no JSON wrapping) per the spec's open-question resolution.
- Sandbox rules (spec, day-one): never `--privileged`; `--memory` limit + hard timeout on build; healthcheck run stage uses `--network=none`; no secrets in build context or env.
- Failure taxonomy: every failed CheckResult carries `failure_kind` = `authoring` | `environment`. Environment failures never consume a repair iteration.
- Tests run via `uv run pytest`; docker/llm-marked tests are excluded by default via `addopts`.
- CLI target file format is **JSON** (`--target target.json`), not YAML — avoids a pyyaml dependency; the spec's `target.yaml` example is amended accordingly.

---

### Task 1: Project scaffold, fixture service, pytest wiring

**Files:**
- Modify: `pyproject.toml`
- Create: `src/deployer/__init__.py`
- Create: `tests/__init__.py` (empty), `tests/conftest.py`
- Create: `tests/fixtures/hello_service/pyproject.toml`, `tests/fixtures/hello_service/.python-version`, `tests/fixtures/hello_service/main.py`
- Create: `tests/fixtures/hello_service/Dockerfile.good` (known-good Dockerfile used by L2 tests)
- Delete: `main.py` (hello-world scaffold)

**Interfaces:**
- Produces: importable package `deployer`; pytest fixture `hello_service` (returns `Path` to the fixture project); markers `docker`, `llm`; entry point `deployer = "deployer.cli:main"` (module arrives in Task 8).

- [ ] **Step 1: Rewrite pyproject.toml**

```toml
[project]
name = "deployer"
version = "0.1.0"
description = "Deploy-authoring agent: LLM authors artifacts, deterministic code verifies them"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.50",
    "pydantic>=2.7",
]

[project.scripts]
deployer = "deployer.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
markers = [
    "docker: requires a container runtime (podman or docker)",
    "llm: requires ANTHROPIC_API_KEY and spends tokens",
]
addopts = "-m 'not docker and not llm'"

[tool.ruff]
line-length = 88

[tool.ruff.lint]
extend-select = ["I"]
```

- [ ] **Step 2: Create package and fixture files**

`src/deployer/__init__.py`:

```python
"""Deployer: LLM authors deploy artifacts, deterministic code verifies them."""
```

`tests/__init__.py`: empty file.

`tests/conftest.py`:

```python
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def hello_service() -> Path:
    """Path to the tiny stdlib HTTP service fixture project."""
    return FIXTURES / "hello_service"
```

`tests/fixtures/hello_service/pyproject.toml`:

```toml
[project]
name = "hello-service"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
hello-service = "main:main"
```

`tests/fixtures/hello_service/.python-version`:

```
3.12
```

`tests/fixtures/hello_service/main.py`:

```python
"""Tiny stdlib HTTP service with a /health endpoint (test fixture)."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
```

`tests/fixtures/hello_service/Dockerfile.good`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
```

Delete `main.py` at the repo root.

- [ ] **Step 3: Sync and verify collection**

Run: `uv sync && uv run pytest`
Expected: exit 0, "no tests ran" / 0 collected (no test files yet), no import errors.

Run: `uv run python -c "import deployer; print(deployer.__doc__)"`
Expected: prints the docstring.

- [ ] **Step 4: Format, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`
Expected: clean (run `pyrefly init` first if `pyrefly.toml`/config is missing).

```bash
git add -A
git commit -m "chore: scaffold src layout, deps, pytest markers, hello_service fixture"
```

---

### Task 2: models.py — pydantic contracts

**Files:**
- Create: `src/deployer/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces (exact names later tasks rely on):
  - `ServiceSpec(port: int, healthcheck_path: str = "/health")`
  - `DeployTarget(base_image: str | None = None, service: ServiceSpec | None = None, env: dict[str, str] = {}, memory_limit: str = "512m")`
  - `ProjectFacts(name, requires_python, python_version, dependencies, entrypoints, has_uv_lock)`
  - `CheckStatus` (StrEnum: `PASSED/FAILED/WARNING/SKIPPED`), `FailureKind` (StrEnum: `AUTHORING/ENVIRONMENT`)
  - `CheckResult(check_id, status, failure_kind=None, message="")`
  - `VerificationReport(results, hadolint_available=False, docker_available=False)` with `.passed`, `.environment_failures`, `.error_signature()`
  - `IterationRecord(index, dockerfile, report, duration_s)`
  - `StopReason` Literal + `AuthoringRun(project, target, iterations, environment_retries, docker_available, hadolint_available, stopped_reason, success)`

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py`:

```python
from deployer.models import (
    AuthoringRun,
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    ServiceSpec,
    VerificationReport,
)


def _failed(check_id: str, kind: FailureKind, message: str = "boom") -> CheckResult:
    return CheckResult(
        check_id=check_id, status=CheckStatus.FAILED, failure_kind=kind, message=message
    )


def test_deploy_target_defaults() -> None:
    target = DeployTarget()
    assert target.base_image is None
    assert target.service is None
    assert target.memory_limit == "512m"


def test_deploy_target_roundtrip_json() -> None:
    target = DeployTarget(service=ServiceSpec(port=8000))
    restored = DeployTarget.model_validate_json(target.model_dump_json())
    assert restored == target


def test_report_passed_ignores_warnings() -> None:
    report = VerificationReport(
        results=[
            CheckResult(check_id="a", status=CheckStatus.PASSED),
            CheckResult(check_id="b", status=CheckStatus.WARNING, message="meh"),
            CheckResult(check_id="c", status=CheckStatus.SKIPPED),
        ]
    )
    assert report.passed


def test_report_failed_and_taxonomy() -> None:
    report = VerificationReport(
        results=[
            _failed("build", FailureKind.AUTHORING),
            _failed("pull", FailureKind.ENVIRONMENT),
        ]
    )
    assert not report.passed
    assert [r.check_id for r in report.environment_failures] == ["pull"]


def test_error_signature_is_stable_and_first_line_only() -> None:
    r1 = VerificationReport(
        results=[_failed("build", FailureKind.AUTHORING, "line one\nline two")]
    )
    r2 = VerificationReport(
        results=[_failed("build", FailureKind.AUTHORING, "line one\nDIFFERENT")]
    )
    assert r1.error_signature() == r2.error_signature()
    assert "line one" in r1.error_signature()


def test_authoring_run_serializes() -> None:
    run = AuthoringRun(
        project="demo",
        target=DeployTarget(),
        iterations=[],
        environment_retries=0,
        docker_available=False,
        hadolint_available=False,
        stopped_reason="static_only",
        success=False,
    )
    assert '"static_only"' in run.model_dump_json()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.models'`

- [ ] **Step 3: Implement models.py**

```python
"""Pydantic contracts shared across the deployer pipeline."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ServiceSpec(BaseModel):
    """Runtime surface the artifact must expose to count as deployed."""

    port: int
    healthcheck_path: str = "/health"


class DeployTarget(BaseModel):
    """Declarative deploy intent: what is wanted, never how."""

    base_image: str | None = None
    service: ServiceSpec | None = None
    env: dict[str, str] = Field(default_factory=dict)
    memory_limit: str = "512m"


class ProjectFacts(BaseModel):
    """Deterministically scanned project facts. Missing facts are None, never guessed."""

    name: str | None = None
    requires_python: str | None = None
    python_version: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    entrypoints: dict[str, str] = Field(default_factory=dict)
    has_uv_lock: bool = False


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class FailureKind(StrEnum):
    AUTHORING = "authoring"
    ENVIRONMENT = "environment"


class CheckResult(BaseModel):
    """Outcome of a single verification check."""

    check_id: str
    status: CheckStatus
    failure_kind: FailureKind | None = None
    message: str = ""


class VerificationReport(BaseModel):
    """Aggregated check results for one Dockerfile candidate."""

    results: list[CheckResult] = Field(default_factory=list)
    hadolint_available: bool = False
    docker_available: bool = False

    @property
    def passed(self) -> bool:
        return all(r.status is not CheckStatus.FAILED for r in self.results)

    @property
    def environment_failures(self) -> list[CheckResult]:
        return [
            r
            for r in self.results
            if r.status is CheckStatus.FAILED
            and r.failure_kind is FailureKind.ENVIRONMENT
        ]

    def error_signature(self) -> str:
        """Normalized fingerprint of failures, for no-progress detection."""
        parts = [
            f"{r.check_id}:{r.message.splitlines()[0] if r.message else ''}"
            for r in self.results
            if r.status is CheckStatus.FAILED
        ]
        return "|".join(sorted(parts))


class IterationRecord(BaseModel):
    """One generate/repair attempt and its verification outcome."""

    index: int
    dockerfile: str
    report: VerificationReport
    duration_s: float


StopReason = Literal[
    "success", "budget_exhausted", "no_progress", "environment_failure", "static_only"
]


class AuthoringRun(BaseModel):
    """Research artifact: the full record of one authoring loop."""

    project: str
    target: DeployTarget
    iterations: list[IterationRecord] = Field(default_factory=list)
    environment_retries: int = 0
    docker_available: bool = False
    hadolint_available: bool = False
    stopped_reason: StopReason
    success: bool
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/models.py tests/test_models.py
git commit -m "feat: pydantic contracts (intent, facts, checks, authoring run)"
```

---

### Task 3: facts.py — deterministic project scanner

**Files:**
- Create: `src/deployer/facts.py`
- Test: `tests/test_facts.py`

**Interfaces:**
- Consumes: `ProjectFacts` from `deployer.models`.
- Produces: `analyze_project(path: Path) -> ProjectFacts`.

- [ ] **Step 1: Write the failing tests**

`tests/test_facts.py`:

```python
from pathlib import Path

from deployer.facts import analyze_project


def test_analyze_hello_service(hello_service: Path) -> None:
    facts = analyze_project(hello_service)
    assert facts.name == "hello-service"
    assert facts.requires_python == ">=3.12"
    assert facts.python_version == "3.12"
    assert facts.dependencies == []
    assert facts.entrypoints == {"hello-service": "main:main"}
    assert facts.has_uv_lock is False


def test_analyze_empty_dir_yields_explicit_nones(tmp_path: Path) -> None:
    facts = analyze_project(tmp_path)
    assert facts.name is None
    assert facts.requires_python is None
    assert facts.python_version is None
    assert facts.dependencies == []
    assert facts.entrypoints == {}
    assert facts.has_uv_lock is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_facts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.facts'`

- [ ] **Step 3: Implement facts.py**

```python
"""Deterministic project scanner. Never guesses: missing facts stay None."""

import tomllib
from pathlib import Path
from typing import Any

from deployer.models import ProjectFacts


def analyze_project(path: Path) -> ProjectFacts:
    """Collect Python-level facts about the project at *path* without any LLM."""
    pyproject: dict[str, Any] = {}
    pyproject_path = path / "pyproject.toml"
    if pyproject_path.is_file():
        pyproject = tomllib.loads(pyproject_path.read_text())
    project: dict[str, Any] = pyproject.get("project") or {}
    if not isinstance(project, dict):
        project = {}

    python_version: str | None = None
    pv_path = path / ".python-version"
    if pv_path.is_file():
        python_version = pv_path.read_text().strip() or None

    return ProjectFacts(
        name=project.get("name"),
        requires_python=project.get("requires-python"),
        python_version=python_version,
        dependencies=list(project.get("dependencies", [])),
        entrypoints=dict(project.get("scripts", {})),
        has_uv_lock=(path / "uv.lock").is_file(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_facts.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/facts.py tests/test_facts.py
git commit -m "feat: deterministic project facts scanner"
```

---

### Task 4: verify.py L1 — static checks

**Files:**
- Create: `src/deployer/verify.py`
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `CheckResult`, `CheckStatus`, `FailureKind`, `VerificationReport` from `deployer.models`.
- Produces:
  - `parse_dockerfile(text: str) -> list[tuple[str, str]]` — `(INSTRUCTION, args)` pairs, comments stripped, `\`-continuations joined.
  - `verify_static(dockerfile: str, project_path: Path) -> VerificationReport` — runs: `parses`, `copy_sources`, `base_pinned`, `hadolint` checks; sets `hadolint_available`.
  - `HADOLINT_VERSION: str = "2.12.0"` (pinned; a run without a matching hadolint is non-comparable).

- [ ] **Step 1: Write the failing tests**

`tests/test_verify_static.py`:

```python
from pathlib import Path

from deployer.models import CheckStatus
from deployer.verify import parse_dockerfile, verify_static

GOOD = """\
FROM python:3.12-slim
WORKDIR /app
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
"""


def _by_id(report, check_id: str):
    return next(r for r in report.results if r.check_id == check_id)


def test_parse_joins_continuations_and_skips_comments() -> None:
    text = "# comment\nFROM python:3.12-slim\nRUN echo a \\\n    && echo b\n"
    instructions = parse_dockerfile(text)
    assert instructions[0] == ("FROM", "python:3.12-slim")
    assert instructions[1][0] == "RUN"
    assert "echo b" in instructions[1][1]


def test_good_dockerfile_passes_static(hello_service: Path) -> None:
    report = verify_static(GOOD, hello_service)
    assert report.passed
    assert _by_id(report, "parses").status is CheckStatus.PASSED
    assert _by_id(report, "copy_sources").status is CheckStatus.PASSED


def test_missing_from_fails_as_authoring(hello_service: Path) -> None:
    report = verify_static("RUN echo hi\n", hello_service)
    check = _by_id(report, "parses")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_copy_of_nonexistent_file_fails(hello_service: Path) -> None:
    bad = GOOD.replace("COPY main.py .", "COPY nope.py .")
    report = verify_static(bad, hello_service)
    check = _by_id(report, "copy_sources")
    assert check.status is CheckStatus.FAILED
    assert "nope.py" in check.message


def test_copy_from_stage_is_ignored(hello_service: Path) -> None:
    multi = (
        "FROM python:3.12-slim AS build\n"
        "COPY main.py .\n"
        "FROM python:3.12-slim\n"
        "COPY --from=build /app/main.py .\n"
    )
    report = verify_static(multi, hello_service)
    assert _by_id(report, "copy_sources").status is CheckStatus.PASSED


def test_unpinned_base_image_warns(hello_service: Path) -> None:
    for base in ("FROM python\n", "FROM python:latest\n"):
        report = verify_static(base + "COPY main.py .\n", hello_service)
        assert _by_id(report, "base_pinned").status is CheckStatus.WARNING


def test_pinned_base_image_passes(hello_service: Path) -> None:
    report = verify_static(GOOD, hello_service)
    assert _by_id(report, "base_pinned").status is CheckStatus.PASSED


def test_hadolint_skipped_marks_non_comparable(
    hello_service: Path, monkeypatch
) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    report = verify_static(GOOD, hello_service)
    assert _by_id(report, "hadolint").status is CheckStatus.SKIPPED
    assert report.hadolint_available is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.verify'`

- [ ] **Step 3: Implement the L1 half of verify.py**

```python
"""Two-level deterministic verification of Dockerfile candidates.

L1 (this file's static half): parse, COPY-source existence, base-image pinning,
hadolint at a pinned version. L2 (docker half, Task 5): sandboxed build + run.
"""

import json
import shutil
import subprocess
from pathlib import Path

from deployer.models import (
    CheckResult,
    CheckStatus,
    FailureKind,
    VerificationReport,
)

HADOLINT_VERSION = "2.12.0"


def parse_dockerfile(text: str) -> list[tuple[str, str]]:
    """Split a Dockerfile into (INSTRUCTION, args) pairs.

    Joins backslash line-continuations and drops comments/blank lines.
    """
    logical_lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical_lines.append(buffer + line)
        buffer = ""
    if buffer:
        logical_lines.append(buffer.strip())

    instructions: list[tuple[str, str]] = []
    for line in logical_lines:
        head, _, rest = line.partition(" ")
        instructions.append((head.upper(), rest.strip()))
    return instructions


def _check_parses(instructions: list[tuple[str, str]]) -> CheckResult:
    if not instructions:
        return CheckResult(
            check_id="parses",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="empty Dockerfile",
        )
    if instructions[0][0] != "FROM" or not instructions[0][1]:
        return CheckResult(
            check_id="parses",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="Dockerfile must start with a FROM instruction",
        )
    return CheckResult(check_id="parses", status=CheckStatus.PASSED)


def _check_copy_sources(
    instructions: list[tuple[str, str]], project_path: Path
) -> CheckResult:
    missing: list[str] = []
    for name, args in instructions:
        if name not in ("COPY", "ADD"):
            continue
        tokens = args.split()
        if any(t.startswith("--from=") for t in tokens):
            continue  # copies from a build stage, not the context
        sources = [t for t in tokens if not t.startswith("--")][:-1]
        for src in sources:
            if src.startswith(("http://", "https://")):
                continue
            if any(ch in src for ch in "*?["):
                if not list(project_path.glob(src)):
                    missing.append(src)
            elif not (project_path / src).exists():
                missing.append(src)
    if missing:
        return CheckResult(
            check_id="copy_sources",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=f"COPY/ADD sources not found in project: {', '.join(missing)}",
        )
    return CheckResult(check_id="copy_sources", status=CheckStatus.PASSED)


def _check_base_pinned(instructions: list[tuple[str, str]]) -> CheckResult:
    for name, args in instructions:
        if name != "FROM":
            continue
        image = args.split()[0]
        if "@sha256:" in image:
            continue
        _, _, tag = image.partition(":")
        if not tag or tag == "latest":
            return CheckResult(
                check_id="base_pinned",
                status=CheckStatus.WARNING,
                message=f"base image '{image}' has no pinned tag; "
                "reproducible builds need a tag (ideally a digest)",
            )
    return CheckResult(check_id="base_pinned", status=CheckStatus.PASSED)


def _check_hadolint(dockerfile: str) -> tuple[CheckResult, bool]:
    """Run hadolint at the pinned version; (result, available_and_comparable)."""
    binary = shutil.which("hadolint")
    if binary is None:
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.SKIPPED,
                message=f"hadolint {HADOLINT_VERSION} not installed; "
                "run is non-comparable",
            ),
            False,
        )
    version = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=10
    ).stdout
    if HADOLINT_VERSION not in version:
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.SKIPPED,
                message=f"hadolint version mismatch (want {HADOLINT_VERSION}, "
                f"got: {version.strip()}); run is non-comparable",
            ),
            False,
        )
    proc = subprocess.run(
        [binary, "--no-color", "-f", "json", "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=30,
    )
    findings = json.loads(proc.stdout) if proc.stdout.strip() else []
    errors = [f for f in findings if f.get("level") == "error"]
    if errors:
        lines = "; ".join(f"{f['code']}: {f['message']}" for f in errors)
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message=lines,
            ),
            True,
        )
    if findings:
        lines = "; ".join(f"{f['code']}: {f['message']}" for f in findings)
        return (
            CheckResult(
                check_id="hadolint", status=CheckStatus.WARNING, message=lines
            ),
            True,
        )
    return (CheckResult(check_id="hadolint", status=CheckStatus.PASSED), True)


def verify_static(dockerfile: str, project_path: Path) -> VerificationReport:
    """Run all L1 static checks against a Dockerfile candidate."""
    instructions = parse_dockerfile(dockerfile)
    results = [_check_parses(instructions)]
    if results[0].status is CheckStatus.PASSED:
        results.append(_check_copy_sources(instructions, project_path))
        results.append(_check_base_pinned(instructions))
    hadolint_result, hadolint_available = _check_hadolint(dockerfile)
    results.append(hadolint_result)
    return VerificationReport(
        results=results, hadolint_available=hadolint_available
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_static.py -v`
Expected: all PASS. (If hadolint 2.12.0 is installed locally, the good-Dockerfile test still passes: hadolint findings on GOOD are at most warnings — e.g. DL3006-adjacent style notes — and `passed` ignores warnings. If a finding is level "error", fix the fixture Dockerfile, not the check.)

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: L1 static verification (parse, copy sources, base pin, hadolint)"
```

---

### Task 5: verify.py L2 — sandboxed docker build + run + healthcheck

**Files:**
- Modify: `src/deployer/verify.py` (append the docker half)
- Test: `tests/test_verify_docker.py` (all tests marked `docker`)

**Interfaces:**
- Consumes: `DeployTarget`, `ServiceSpec` from models; `verify_static` from Task 4.
- Produces:
  - `detect_container_tool() -> str | None` — podman preferred, docker fallback.
  - `verify_docker(dockerfile: str, project_path: Path, target: DeployTarget, tool: str, *, build_timeout: int = 600, health_timeout: int = 30) -> list[CheckResult]` — `build` check + (when `target.service`) `run_healthcheck` check.
  - `verify(dockerfile, project_path, target, tool) -> VerificationReport` — full pipeline: static first; docker only when static passed and `tool` is not None; sets `docker_available`.
  - `ENVIRONMENT_MARKERS` — stderr substrings classifying a failure as environment.

- [ ] **Step 1: Write the failing tests**

`tests/test_verify_docker.py`:

```python
from pathlib import Path

import pytest

from deployer.models import CheckStatus, DeployTarget, ServiceSpec
from deployer.verify import detect_container_tool, verify

pytestmark = pytest.mark.docker

TARGET = DeployTarget(service=ServiceSpec(port=8000, healthcheck_path="/health"))


@pytest.fixture(scope="module")
def tool() -> str:
    found = detect_container_tool()
    if found is None:
        pytest.skip("no container runtime available")
    return found


def _by_id(report, check_id: str):
    return next(r for r in report.results if r.check_id == check_id)


def test_good_dockerfile_builds_runs_and_healthchecks(
    hello_service: Path, tool: str
) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, TARGET, tool)
    assert report.docker_available
    assert _by_id(report, "build").status is CheckStatus.PASSED
    assert _by_id(report, "run_healthcheck").status is CheckStatus.PASSED
    assert report.passed


def test_broken_run_instruction_fails_build_as_authoring(
    hello_service: Path, tool: str
) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text().replace(
        "WORKDIR /app", "WORKDIR /app\nRUN definitely-not-a-command"
    )
    report = verify(dockerfile, hello_service, TARGET, tool)
    check = _by_id(report, "build")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_wrong_port_fails_healthcheck(hello_service: Path, tool: str) -> None:
    bad_target = DeployTarget(service=ServiceSpec(port=9999))
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, bad_target, tool)
    check = _by_id(report, "run_healthcheck")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_no_tool_degrades_to_static_only(hello_service: Path) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, TARGET, tool=None)
    assert report.docker_available is False
    assert all(r.check_id not in ("build", "run_healthcheck") for r in report.results)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_docker.py -m docker -v`
Expected: FAIL — `ImportError: cannot import name 'detect_container_tool'`

- [ ] **Step 3: Append the L2 half to verify.py**

Add imports at the top: `import time`, `import uuid` — and note `subprocess`, `shutil` are already imported. Append:

```python
ENVIRONMENT_MARKERS = (
    "tls handshake",
    "connection refused",
    "connection reset",
    "temporary failure",
    "i/o timeout",
    "toomanyrequests",
    "network is unreachable",
    "no route to host",
    "service unavailable",
)


def detect_container_tool() -> str | None:
    """Prefer rootless-friendly podman; fall back to docker."""
    for tool in ("podman", "docker"):
        if shutil.which(tool):
            return tool
    return None


def _classify(stderr: str) -> FailureKind:
    lowered = stderr.lower()
    if any(marker in lowered for marker in ENVIRONMENT_MARKERS):
        return FailureKind.ENVIRONMENT
    return FailureKind.AUTHORING


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _build(
    dockerfile: str,
    project_path: Path,
    target: "DeployTarget",
    tool: str,
    tag: str,
    timeout: int,
) -> CheckResult:
    try:
        proc = subprocess.run(
            [
                tool,
                "build",
                "--memory",
                target.memory_limit,
                "-t",
                tag,
                "-f",
                "-",
                str(project_path),
            ],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message=f"build timed out after {timeout}s",
        )
    if proc.returncode != 0:
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=_classify(proc.stderr),
            message=_tail(proc.stderr),
        )
    return CheckResult(check_id="build", status=CheckStatus.PASSED)


def _run_healthcheck(
    target: "DeployTarget", tool: str, tag: str, timeout: int
) -> CheckResult:
    assert target.service is not None
    container = f"deployer-check-{uuid.uuid4().hex[:8]}"
    url = f"http://127.0.0.1:{target.service.port}{target.service.healthcheck_path}"
    probe = (
        "import urllib.request; "
        f"urllib.request.urlopen('{url}', timeout=2)"
    )
    try:
        started = subprocess.run(
            [
                tool,
                "run",
                "-d",
                "--rm",
                "--name",
                container,
                "--network=none",
                "--memory",
                target.memory_limit,
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if started.returncode != 0:
            return CheckResult(
                check_id="run_healthcheck",
                status=CheckStatus.FAILED,
                failure_kind=_classify(started.stderr),
                message=_tail(started.stderr),
            )
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            probe_proc = subprocess.run(
                [tool, "exec", container, "python", "-c", probe],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if probe_proc.returncode == 0:
                return CheckResult(
                    check_id="run_healthcheck", status=CheckStatus.PASSED
                )
            last_error = probe_proc.stderr
            time.sleep(1)
        logs = subprocess.run(
            [tool, "logs", container], capture_output=True, text=True, timeout=10
        )
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=(
                f"healthcheck {url} failed within {timeout}s: "
                f"{_tail(last_error, 3)}\ncontainer logs:\n{_tail(logs.stdout)}"
            ),
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message="container runtime command timed out",
        )
    finally:
        subprocess.run(
            [tool, "rm", "-f", container], capture_output=True, timeout=30
        )


def verify_docker(
    dockerfile: str,
    project_path: Path,
    target: "DeployTarget",
    tool: str,
    *,
    build_timeout: int = 600,
    health_timeout: int = 30,
) -> list[CheckResult]:
    """L2: real sandboxed build; for service intents, run + loopback healthcheck.

    The healthcheck probes over the container's loopback via `exec python -c`,
    so `--network=none` still works. This assumes a Python base image — true
    for every artifact this MVP authors.
    """
    tag = f"deployer-verify-{uuid.uuid4().hex[:8]}"
    results: list[CheckResult] = []
    try:
        build_result = _build(
            dockerfile, project_path, target, tool, tag, build_timeout
        )
        results.append(build_result)
        if build_result.status is CheckStatus.PASSED and target.service is not None:
            results.append(_run_healthcheck(target, tool, tag, health_timeout))
    finally:
        subprocess.run([tool, "rmi", "-f", tag], capture_output=True, timeout=60)
    return results


def verify(
    dockerfile: str,
    project_path: Path,
    target: "DeployTarget",
    tool: str | None,
) -> VerificationReport:
    """Full verification: L1 static always; L2 docker when available and L1 passed."""
    report = verify_static(dockerfile, project_path)
    if tool is None:
        return report
    report.docker_available = True
    if report.passed:
        report.results.extend(
            verify_docker(dockerfile, project_path, target, tool)
        )
    return report
```

Also add `DeployTarget` to the imports from `deployer.models` at the top of the file (replacing the string annotations `"DeployTarget"` above with the real name).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_docker.py -m docker -v`
Expected: all PASS (requires podman or docker locally; first run pulls `python:3.12-slim`).
Also run: `uv run pytest` — default suite still green, docker tests excluded.

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/verify.py tests/test_verify_docker.py
git commit -m "feat: L2 sandboxed docker build + network-none run healthcheck"
```

---

### Task 6: author.py — the control loop

**Files:**
- Create: `src/deployer/author.py`
- Test: `tests/test_author.py`

**Interfaces:**
- Consumes: `analyze_project`, `verify`, `detect_container_tool`, models.
- Produces:
  - `DockerfileAuthor` Protocol: `generate(facts: ProjectFacts, target: DeployTarget) -> str`; `repair(facts: ProjectFacts, target: DeployTarget, dockerfile: str, report: VerificationReport) -> str`.
  - `author_dockerfile(project_path: Path, target: DeployTarget, author: DockerfileAuthor, *, max_iterations: int = 3, run_docker: bool = True) -> AuthoringRun`.

- [ ] **Step 1: Write the failing tests**

`tests/test_author.py`. The fake author is deterministic; docker is disabled (`run_docker=False`) so only L1 drives the loop. hadolint noise is silenced by monkeypatching it as SKIPPED-unavailable so tests behave identically with or without a local hadolint.

```python
from pathlib import Path

import pytest

from deployer.author import author_dockerfile
from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    ProjectFacts,
    VerificationReport,
)

GOOD = "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\nCMD [\"python\", \"main.py\"]\n"
BAD_COPY = "FROM python:3.12-slim\nCOPY nope.py .\n"
NO_FROM = "RUN echo broken\n"


@pytest.fixture(autouse=True)
def _no_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


class ScriptedAuthor:
    """Returns queued Dockerfiles: first for generate(), rest for repair()."""

    def __init__(self, *dockerfiles: str) -> None:
        self._queue = list(dockerfiles)
        self.repair_calls = 0

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        return self._queue.pop(0)

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        self.repair_calls += 1
        return self._queue.pop(0)


def test_success_on_first_iteration(hello_service: Path) -> None:
    run = author_dockerfile(
        hello_service, DeployTarget(), ScriptedAuthor(GOOD), run_docker=False
    )
    assert run.stopped_reason == "static_only"
    assert run.success is False  # static-only never counts as full success
    assert len(run.iterations) == 1
    assert run.iterations[0].dockerfile == GOOD


def test_repair_path_fixes_bad_copy(hello_service: Path) -> None:
    author = ScriptedAuthor(BAD_COPY, GOOD)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, run_docker=False
    )
    assert author.repair_calls == 1
    assert len(run.iterations) == 2
    assert run.stopped_reason == "static_only"


def test_no_progress_early_stop(hello_service: Path) -> None:
    author = ScriptedAuthor(BAD_COPY, BAD_COPY, GOOD)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, max_iterations=5, run_docker=False
    )
    assert run.stopped_reason == "no_progress"
    assert len(run.iterations) == 2  # third (good) candidate never attempted


def test_budget_exhausted_returns_failed_run(hello_service: Path) -> None:
    author = ScriptedAuthor(NO_FROM, BAD_COPY, NO_FROM)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, max_iterations=3, run_docker=False
    )
    assert run.stopped_reason == "budget_exhausted"
    assert run.success is False
    assert len(run.iterations) == 3


def test_environment_failure_retries_once_without_consuming_iteration(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import FailureKind

    calls = {"n": 0}

    def flaky_verify(dockerfile, project_path, target, tool):
        calls["n"] += 1
        if calls["n"] == 1:
            return VerificationReport(
                results=[
                    CheckResult(
                        check_id="build",
                        status=CheckStatus.FAILED,
                        failure_kind=FailureKind.ENVIRONMENT,
                        message="toomanyrequests: rate limited",
                    )
                ]
            )
        return VerificationReport(
            results=[CheckResult(check_id="build", status=CheckStatus.PASSED)],
            docker_available=True,
        )

    monkeypatch.setattr("deployer.author.verify", flaky_verify)
    monkeypatch.setattr("deployer.author.detect_container_tool", lambda: "docker")
    run = author_dockerfile(hello_service, DeployTarget(), ScriptedAuthor(GOOD))
    assert run.environment_retries == 1
    assert len(run.iterations) == 1
    assert run.stopped_reason == "success"
    assert run.success is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_author.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.author'`

- [ ] **Step 3: Implement author.py**

```python
"""The authoring control loop: deterministic pipeline, LLM inside one step."""

import time
from pathlib import Path
from typing import Protocol

from deployer.facts import analyze_project
from deployer.models import (
    AuthoringRun,
    DeployTarget,
    IterationRecord,
    ProjectFacts,
    StopReason,
    VerificationReport,
)
from deployer.verify import detect_container_tool, verify


class DockerfileAuthor(Protocol):
    """Anything that can draft and repair Dockerfiles from facts + intent."""

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str: ...

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str: ...


def author_dockerfile(
    project_path: Path,
    target: DeployTarget,
    author: DockerfileAuthor,
    *,
    max_iterations: int = 3,
    run_docker: bool = True,
) -> AuthoringRun:
    """Generate -> verify -> repair until success, budget, or no progress.

    The LLM (author) only ever sees facts and reports and returns text;
    this function owns files, subprocesses, and control flow.
    """
    facts = analyze_project(project_path)
    tool = detect_container_tool() if run_docker else None

    iterations: list[IterationRecord] = []
    environment_retries = 0
    hadolint_available = False
    stopped_reason: StopReason = "budget_exhausted"
    dockerfile = author.generate(facts, target)
    prev_signature: str | None = None

    for index in range(max_iterations):
        start = time.monotonic()
        report = verify(dockerfile, project_path, target, tool)
        if report.environment_failures and environment_retries == 0:
            environment_retries += 1
            report = verify(dockerfile, project_path, target, tool)
        iterations.append(
            IterationRecord(
                index=index,
                dockerfile=dockerfile,
                report=report,
                duration_s=time.monotonic() - start,
            )
        )
        hadolint_available = report.hadolint_available

        if report.environment_failures:
            stopped_reason = "environment_failure"
            break
        if report.passed:
            stopped_reason = "success" if tool is not None else "static_only"
            break
        signature = report.error_signature()
        if signature == prev_signature:
            stopped_reason = "no_progress"
            break
        prev_signature = signature
        if index < max_iterations - 1:
            dockerfile = author.repair(facts, target, dockerfile, report)

    return AuthoringRun(
        project=facts.name or project_path.name,
        target=target,
        iterations=iterations,
        environment_retries=environment_retries,
        docker_available=tool is not None,
        hadolint_available=hadolint_available,
        stopped_reason=stopped_reason,
        success=stopped_reason == "success",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_author.py -v`
Expected: all PASS. Then `uv run pytest` — whole default suite green.

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/author.py tests/test_author.py
git commit -m "feat: authoring loop with repair, early-stop, env-failure retry"
```

---

### Task 7: llm.py — AnthropicAuthor

**Files:**
- Create: `src/deployer/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `DockerfileAuthor` protocol shape (structural — no import needed), models.
- Produces: `AnthropicAuthor(client: anthropic.Anthropic | None = None, model: str = "claude-opus-4-8")` satisfying `DockerfileAuthor`; `_extract_dockerfile(text: str) -> str` (fence-stripping helper).

- [ ] **Step 1: Write the failing tests**

`tests/test_llm.py` — the Anthropic client is replaced with a minimal stub; no network.

```python
from deployer.llm import AnthropicAuthor, _extract_dockerfile
from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    VerificationReport,
)


class _Block:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs) -> _Response:
        self.calls.append(kwargs)
        return _Response(self._reply)


class _StubClient:
    def __init__(self, reply: str) -> None:
        self.messages = _Messages(reply)


def test_extract_strips_markdown_fences() -> None:
    fenced = "```dockerfile\nFROM python:3.12-slim\n```\n"
    assert _extract_dockerfile(fenced) == "FROM python:3.12-slim"
    assert _extract_dockerfile("FROM x\n") == "FROM x"


def test_generate_sends_facts_and_returns_dockerfile() -> None:
    client = _StubClient("FROM python:3.12-slim\n")
    author = AnthropicAuthor(client=client)
    facts = ProjectFacts(name="demo", python_version="3.12")
    result = author.generate(facts, DeployTarget())
    assert result == "FROM python:3.12-slim"
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert "demo" in call["messages"][0]["content"]
    assert "temperature" not in call


def test_repair_includes_previous_dockerfile_and_failures() -> None:
    client = _StubClient("FROM python:3.12-slim\nCOPY main.py .\n")
    author = AnthropicAuthor(client=client)
    report = VerificationReport(
        results=[
            CheckResult(
                check_id="copy_sources",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message="COPY/ADD sources not found in project: nope.py",
            )
        ]
    )
    author.repair(ProjectFacts(), DeployTarget(), "FROM x\nCOPY nope.py .\n", report)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "COPY nope.py ." in prompt
    assert "nope.py" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.llm'`

- [ ] **Step 3: Implement llm.py**

```python
"""Thin Anthropic SDK wrapper implementing the DockerfileAuthor protocol."""

from typing import Any

import anthropic

from deployer.models import DeployTarget, ProjectFacts, VerificationReport

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 8192

SYSTEM_PROMPT = """\
You are a deployment artifact author. You write production-quality Dockerfiles
for Python projects managed with uv.

Rules:
- Reply with ONLY the Dockerfile content. No prose, no markdown fences.
- Only COPY files that exist in the project facts you are given. Never invent
  files.
- Pin the base image to a specific tag (never :latest, never untagged).
- The project facts are deterministic ground truth; missing facts are None —
  do not guess values for them.
- Prefer slim base images and a non-root user where practical.
"""


def _extract_dockerfile(text: str) -> str:
    """Strip optional markdown fences the model might add despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop opening fence (with optional language tag)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


class AnthropicAuthor:
    """DockerfileAuthor backed by the Anthropic Messages API."""

    def __init__(
        self, client: Any | None = None, model: str = DEFAULT_MODEL
    ) -> None:
        self._client = client if client is not None else anthropic.Anthropic()
        self._model = model

    def _complete(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        return _extract_dockerfile(text)

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        prompt = (
            "Write a Dockerfile for this project.\n\n"
            f"Project facts (deterministic scan):\n{facts.model_dump_json(indent=2)}\n\n"
            f"Deploy intent:\n{target.model_dump_json(indent=2)}\n"
        )
        return self._complete(prompt)

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        failures = [
            r for r in report.results if r.status.value in ("failed", "warning")
        ]
        findings = "\n".join(f"- [{r.check_id}] {r.message}" for r in failures)
        prompt = (
            "The following Dockerfile failed verification. Fix it.\n\n"
            f"Dockerfile:\n{dockerfile}\n\n"
            f"Verification findings:\n{findings}\n\n"
            f"Project facts (deterministic scan):\n{facts.model_dump_json(indent=2)}\n\n"
            f"Deploy intent:\n{target.model_dump_json(indent=2)}\n"
        )
        return self._complete(prompt)
```

Note: `client` is typed `Any | None` deliberately so tests can inject a stub; the runtime default is a real `anthropic.Anthropic()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/llm.py tests/test_llm.py
git commit -m "feat: AnthropicAuthor with plain-text Dockerfile prompts"
```

---

### Task 8: cli.py + end-to-end docker test

**Files:**
- Create: `src/deployer/cli.py`
- Test: `tests/test_cli.py`, plus one docker-marked e2e test appended to `tests/test_verify_docker.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv: list[str] | None = None) -> int` with subcommands:
  - `deployer verify <path> [--target target.json]` — verify `<path>/Dockerfile` (no LLM), print report, exit 0/1.
  - `deployer author <path> [--target target.json] [--max-iterations N] [--no-docker]` — run the loop with `AnthropicAuthor`, write `<path>/Dockerfile` (best candidate even on failure — never silently succeed), write `<path>/.deployer/authoring-run.json`, exit 0 on `success` (or `static_only` under `--no-docker`), else 1.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import json
from pathlib import Path

import pytest

from deployer import cli
from deployer.models import CheckResult, CheckStatus


@pytest.fixture(autouse=True)
def _no_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


def test_verify_command_passes_on_good_dockerfile(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text(
        (hello_service / "Dockerfile.good").read_text()
    )
    monkeypatch.setattr("deployer.cli.detect_container_tool", lambda: None)
    assert cli.main(["verify", str(project)]) == 0


def test_verify_command_fails_without_dockerfile(tmp_path: Path) -> None:
    assert cli.main(["verify", str(tmp_path)]) == 1


def test_author_command_writes_dockerfile_and_report(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())

    good = (hello_service / "Dockerfile.good").read_text()

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    exit_code = cli.main(["author", str(project), "--no-docker"])
    assert exit_code == 0
    assert (project / "Dockerfile").read_text().rstrip() == good.rstrip()
    run_data = json.loads(
        (project / ".deployer" / "authoring-run.json").read_text()
    )
    assert run_data["stopped_reason"] == "static_only"


def test_author_reads_target_json(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    target_file = tmp_path / "target.json"
    target_file.write_text('{"service": {"port": 8000}}')

    captured = {}

    class FakeAuthor:
        def generate(self, facts, target):
            captured["target"] = target
            return (hello_service / "Dockerfile.good").read_text()

        def repair(self, facts, target, dockerfile, report):
            return dockerfile

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    cli.main(["author", str(project), "--no-docker", "--target", str(target_file)])
    assert captured["target"].service.port == 8000
```

Append to `tests/test_verify_docker.py` (e2e: full loop, fake author, real docker):

```python
def test_e2e_author_loop_with_real_docker(hello_service: Path, tool: str) -> None:
    from deployer.author import author_dockerfile

    good = (hello_service / "Dockerfile.good").read_text()

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    run = author_dockerfile(hello_service, TARGET, FakeAuthor())
    assert run.success is True
    assert run.stopped_reason == "success"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.cli'` (or missing `AnthropicAuthor` attr).

- [ ] **Step 3: Implement cli.py**

```python
"""Thin argparse CLI over the deployer library."""

import argparse
import sys
from pathlib import Path

from deployer.author import author_dockerfile
from deployer.llm import AnthropicAuthor
from deployer.models import CheckStatus, DeployTarget, VerificationReport
from deployer.verify import detect_container_tool, verify

_STATUS_ICONS = {
    CheckStatus.PASSED: "ok",
    CheckStatus.FAILED: "FAIL",
    CheckStatus.WARNING: "warn",
    CheckStatus.SKIPPED: "skip",
}


def _load_target(path: str | None) -> DeployTarget:
    if path is None:
        return DeployTarget()
    return DeployTarget.model_validate_json(Path(path).read_text())


def _print_report(report: VerificationReport) -> None:
    for result in report.results:
        icon = _STATUS_ICONS[result.status]
        line = f"[{icon:>4}] {result.check_id}"
        if result.message:
            line += f": {result.message.splitlines()[0]}"
        print(line)
    if not report.docker_available:
        print("note: no container runtime found; static-only verification")


def _cmd_verify(args: argparse.Namespace) -> int:
    project = Path(args.path)
    dockerfile_path = project / "Dockerfile"
    if not dockerfile_path.is_file():
        print(f"error: {dockerfile_path} not found", file=sys.stderr)
        return 1
    target = _load_target(args.target)
    report = verify(
        dockerfile_path.read_text(), project, target, detect_container_tool()
    )
    _print_report(report)
    return 0 if report.passed else 1


def _cmd_author(args: argparse.Namespace) -> int:
    project = Path(args.path)
    target = _load_target(args.target)
    run = author_dockerfile(
        project,
        target,
        AnthropicAuthor(),
        max_iterations=args.max_iterations,
        run_docker=not args.no_docker,
    )
    if run.iterations:
        (project / "Dockerfile").write_text(run.iterations[-1].dockerfile + "\n")
        _print_report(run.iterations[-1].report)
    report_dir = project / ".deployer"
    report_dir.mkdir(exist_ok=True)
    (report_dir / "authoring-run.json").write_text(run.model_dump_json(indent=2))
    print(
        f"stopped: {run.stopped_reason} after {len(run.iterations)} iteration(s); "
        f"run report: {report_dir / 'authoring-run.json'}"
    )
    accepted = ("success", "static_only") if args.no_docker else ("success",)
    return 0 if run.stopped_reason in accepted else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `deployer` CLI."""
    parser = argparse.ArgumentParser(prog="deployer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="verify an existing Dockerfile")
    p_verify.add_argument("path")
    p_verify.add_argument("--target", default=None, help="DeployTarget JSON file")
    p_verify.set_defaults(func=_cmd_verify)

    p_author = sub.add_parser("author", help="author a Dockerfile with the LLM")
    p_author.add_argument("path")
    p_author.add_argument("--target", default=None, help="DeployTarget JSON file")
    p_author.add_argument("--max-iterations", type=int, default=3)
    p_author.add_argument(
        "--no-docker", action="store_true", help="static-only verification"
    )
    p_author.set_defaults(func=_cmd_author)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS.
Run: `uv run pytest -m docker -v` (with a container runtime)
Expected: docker suite + e2e loop test PASS.
Run: `uv run pytest`
Expected: full default suite green.
Smoke: `uv run deployer verify tests/fixtures/hello_service` — exits 1 (no `Dockerfile` there — `Dockerfile.good` is deliberately not named `Dockerfile`); prints the error.

- [ ] **Step 5: Lint, typecheck, commit**

Run: `uv run ruff format . && uv run ruff check . --fix && pyrefly check`

```bash
git add src/deployer/cli.py tests/test_cli.py tests/test_verify_docker.py
git commit -m "feat: deployer CLI (author/verify) + e2e docker loop test"
```

---

### Task 9: Dogfood run + README

**Files:**
- Modify: `README.md`
- No new source files. This task validates the real LLM path once, manually.

- [ ] **Step 1: Live smoke test (requires ANTHROPIC_API_KEY or `ant auth login`, and docker/podman)**

```bash
uv run deployer author tests/fixtures/hello_service --target /dev/stdin <<'EOF'
{"service": {"port": 8000, "healthcheck_path": "/health"}}
EOF
```

Expected: exit 0, `tests/fixtures/hello_service/Dockerfile` written, `.deployer/authoring-run.json` shows `"success": true`. Inspect the run report — iterations count and check results are the research payload.
Then clean up so fixtures stay pristine:

```bash
git status --short tests/fixtures/  # review
rm -f tests/fixtures/hello_service/Dockerfile
rm -rf tests/fixtures/hello_service/.deployer
```

If the run fails on an authoring error, that is data, not necessarily a bug: read `.deployer/authoring-run.json`, check whether the loop repaired sensibly, and only fix code if the pipeline (not the model) misbehaved.

Optional second dogfood (spec: "and on deployer itself"): `uv run deployer author . ` with no target — deployer is a library, so no service is declared and the gate is build-only. Review, then `rm -f Dockerfile && rm -rf .deployer` (don't commit these artifacts).

- [ ] **Step 2: Write README.md**

```markdown
# deployer

Research bench for deploy-authoring agents: an LLM authors a Dockerfile from
deterministic project facts + a declarative `deploy_target` intent; a
deterministic pipeline verifies it (static checks, then a sandboxed
`docker build` + run + healthcheck) and feeds failures back for repair.
**Authoring ≠ execution**: the model only ever sees facts and reports and
returns text — files, docker, and control flow belong to the pipeline.

Design: `docs/superpowers/specs/2026-07-04-deployer-mvp-design.md`.

## Usage

```sh
uv run deployer author <project-path> [--target target.json] [--no-docker]
uv run deployer verify <project-path>   # checks <project-path>/Dockerfile
```

`target.json` is a `DeployTarget`: e.g.
`{"service": {"port": 8000, "healthcheck_path": "/health"}}`.
Every `author` run writes `.deployer/authoring-run.json` — iteration count,
per-check outcomes, authoring-vs-environment failure taxonomy. That file is
the research output.

## Development

```sh
uv sync
uv run pytest              # unit tests (no docker, no LLM)
uv run pytest -m docker    # + sandboxed docker build/run tests
uv run ruff format . && uv run ruff check . --fix && pyrefly check
```

Optional: `hadolint` 2.12.0 on PATH enables the lint check; runs without it
are marked non-comparable in the run report.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README with usage and research-output description"
```

---

## Deferred (explicitly not in this plan)

Backlog from the spec, in rough priority order: fixture with a system dependency (libpq/psycopg2) probing the hard case; baseline arm vs the official uv Dockerfile; agent-with-tools phase-2 comparison arm; CI/Helm/Terraform artifact types; MCP wrapper; arbiter/ATP seam wiring.
