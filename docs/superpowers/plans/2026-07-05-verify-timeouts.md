# Verify Timeout Forwarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators override the L2 build/healthcheck timeouts (600s/30s) from the CLI, threaded as keyword args through `verify()` and `author_dockerfile()`.

**Architecture:** Constants `DEFAULT_BUILD_TIMEOUT`/`DEFAULT_HEALTH_TIMEOUT` live in `src/deployer/verify.py` (single source of truth). `verify()` gains keyword-only params and forwards them to `verify_docker()`; `author_dockerfile()` gains the same params and forwards to both of its `verify()` call sites; `--build-timeout`/`--health-timeout` flags are added to both CLI subcommands with `< 1` → exit 2 validation.

**Tech Stack:** Python 3.12, pydantic v2, pytest (uv-managed: `uv run pytest`), ruff, pyrefly.

Spec: `docs/superpowers/specs/2026-07-05-verify-timeouts-design.md`.

## Global Constraints

- Defaults unchanged: 600s build, 30s health — a run without the new flags must behave byte-identically to today.
- Timeout constants are defined ONLY in `verify.py`; every other reference imports them (no duplicated numbers).
- Timeouts do NOT go into `DeployTarget` (target = what, never how).
- Validation: timeout values `< 1` exit with code 2 on **both** subcommands. No upper bound (deliberate).
- `--health-timeout` help text must say it is ignored for non-service targets.
- Package management via `uv` only. Line length 88. Run `uv run ruff format .`, `uv run ruff check .`, and `pyrefly check` before each commit.
- Default pytest run excludes docker/llm marks (`addopts = "-m 'not docker and not llm'"`), so all new tests must be mock-based and must NOT live in `tests/test_verify_docker.py` (module-level `pytestmark = pytest.mark.docker` would exclude them). The spec names that file for the `verify()` forwarding test; the plan relocates it to `tests/test_verify_static.py` for this reason.

## Preamble: create the feature branch

- [ ] From a clean `master`, run:

```bash
git checkout -b feature/verify-timeouts
```

---

### Task 1: Timeout constants + `verify()` forwarding

**Files:**
- Modify: `src/deployer/verify.py` (constants near line 24; `verify_docker` at line 456; `verify` at line 488)
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (later tasks rely on these exact names):
  - `deployer.verify.DEFAULT_BUILD_TIMEOUT: int = 600`
  - `deployer.verify.DEFAULT_HEALTH_TIMEOUT: int = 30`
  - `verify(dockerfile: str, project_path: Path, target: DeployTarget, tool: str | None, facts: ProjectFacts | None = None, *, build_timeout: int = DEFAULT_BUILD_TIMEOUT, health_timeout: int = DEFAULT_HEALTH_TIMEOUT) -> VerificationReport`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py` (it currently imports `parse_dockerfile, verify_static`; extend the imports at the top of the file):

```python
# at top of file, extend existing imports:
from deployer.models import CheckResult, CheckStatus, DeployTarget, ProjectFacts
from deployer.verify import parse_dockerfile, verify, verify_static
```

Append at the end of the file:

```python
def _spy_docker(captured: dict):
    """verify_docker replacement that records the timeout kwargs it got."""

    def spy(dockerfile, project_path, target, tool, *, build_timeout, health_timeout):
        captured["build_timeout"] = build_timeout
        captured["health_timeout"] = health_timeout
        return [CheckResult(check_id="build", status=CheckStatus.PASSED)], None

    return spy


def _skip_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


def test_verify_forwards_timeouts_to_verify_docker(
    hello_service: Path, monkeypatch
) -> None:
    _skip_hadolint(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("deployer.verify.verify_docker", _spy_docker(captured))
    report = verify(
        GOOD,
        hello_service,
        DeployTarget(),
        "podman",
        build_timeout=1200,
        health_timeout=45,
    )
    assert captured == {"build_timeout": 1200, "health_timeout": 45}
    assert report.docker_available


def test_verify_defaults_match_module_constants(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.verify import DEFAULT_BUILD_TIMEOUT, DEFAULT_HEALTH_TIMEOUT

    _skip_hadolint(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("deployer.verify.verify_docker", _spy_docker(captured))
    verify(GOOD, hello_service, DeployTarget(), "podman")
    assert captured == {
        "build_timeout": DEFAULT_BUILD_TIMEOUT,
        "health_timeout": DEFAULT_HEALTH_TIMEOUT,
    }
    assert DEFAULT_BUILD_TIMEOUT == 600
    assert DEFAULT_HEALTH_TIMEOUT == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -v -k timeout`
Expected: both tests FAIL — `ImportError` (no `DEFAULT_BUILD_TIMEOUT`) or `TypeError: verify() got an unexpected keyword argument 'build_timeout'`.

- [ ] **Step 3: Implement in `src/deployer/verify.py`**

Add constants next to `HADOLINT_VERSION` (line 24):

```python
HADOLINT_VERSION = "2.12.0"
DEFAULT_BUILD_TIMEOUT = 600
DEFAULT_HEALTH_TIMEOUT = 30
```

Change `verify_docker`'s signature defaults (line 456-463) from literals to the constants:

```python
def verify_docker(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    tool: str,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> tuple[list[CheckResult], int | None]:
```

Change `verify` (line 488) to accept and forward the kwargs:

```python
def verify(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    tool: str | None,
    facts: ProjectFacts | None = None,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> VerificationReport:
    """Full verification: L1 static always; L2 docker when available and L1 passed."""
    report = verify_static(dockerfile, project_path, facts)
    if tool is None:
        return report
    report.docker_available = True
    if report.passed:
        docker_results, image_size = verify_docker(
            dockerfile,
            project_path,
            target,
            tool,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        report.results.extend(docker_results)
        report.image_size_bytes = image_size
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_static.py -v`
Expected: all PASS (including pre-existing tests).

- [ ] **Step 5: Full check + commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check && uv run pytest
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: verify() forwards build/health timeouts to verify_docker"
```

---

### Task 2: `author_dockerfile()` forwarding (both call sites)

**Files:**
- Modify: `src/deployer/author.py` (signature at line 34; `verify()` calls at lines 69 and 72)
- Test: `tests/test_author.py`

**Interfaces:**
- Consumes (from Task 1): `deployer.verify.DEFAULT_BUILD_TIMEOUT`, `DEFAULT_HEALTH_TIMEOUT`; `verify(..., *, build_timeout, health_timeout)`.
- Produces (Task 3 relies on): `author_dockerfile(project_path, target, author, *, max_iterations: int = 3, run_docker: bool = True, build_timeout: int = DEFAULT_BUILD_TIMEOUT, health_timeout: int = DEFAULT_HEALTH_TIMEOUT) -> AuthoringRun`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_author.py` (module already defines `ScriptedAuthor`, `GOOD`, and imports `CheckResult`, `CheckStatus`, `DeployTarget`, `VerificationReport`):

```python
def test_author_forwards_timeouts_to_both_verify_calls(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import FailureKind

    captured: list[dict] = []

    def spy_verify(
        dockerfile,
        project_path,
        target,
        tool,
        facts=None,
        *,
        build_timeout,
        health_timeout,
    ):
        captured.append(
            {"build_timeout": build_timeout, "health_timeout": health_timeout}
        )
        if len(captured) == 1:  # first call: environment flake -> triggers retry
            return VerificationReport(
                results=[
                    CheckResult(
                        check_id="build",
                        status=CheckStatus.FAILED,
                        failure_kind=FailureKind.ENVIRONMENT,
                        message="connection reset",
                    )
                ]
            )
        return VerificationReport(
            results=[CheckResult(check_id="build", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.author.verify", spy_verify)
    monkeypatch.setattr("deployer.author.detect_container_tool", lambda: "podman")
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        ScriptedAuthor(GOOD),
        build_timeout=1200,
        health_timeout=45,
    )
    assert len(captured) == 2  # main call + environment-retry call
    assert all(
        c == {"build_timeout": 1200, "health_timeout": 45} for c in captured
    )
    assert run.stopped_reason == "success"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_author.py::test_author_forwards_timeouts_to_both_verify_calls -v`
Expected: FAIL with `TypeError: author_dockerfile() got an unexpected keyword argument 'build_timeout'`.

- [ ] **Step 3: Implement in `src/deployer/author.py`**

Extend the import from `deployer.verify` (line 17):

```python
from deployer.verify import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_HEALTH_TIMEOUT,
    detect_container_tool,
    verify,
)
```

Extend the signature (line 34):

```python
def author_dockerfile(
    project_path: Path,
    target: DeployTarget,
    author: DockerfileAuthor,
    *,
    max_iterations: int = 3,
    run_docker: bool = True,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> AuthoringRun:
```

Forward at BOTH call sites (lines 69 and 72):

```python
            report = verify(
                dockerfile,
                project_path,
                target,
                tool,
                facts,
                build_timeout=build_timeout,
                health_timeout=health_timeout,
            )
            if report.environment_failures and environment_retries == 0:
                environment_retries += 1
                report = verify(
                    dockerfile,
                    project_path,
                    target,
                    tool,
                    facts,
                    build_timeout=build_timeout,
                    health_timeout=health_timeout,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_author.py -v`
Expected: all PASS.

- [ ] **Step 5: Full check + commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check && uv run pytest
git add src/deployer/author.py tests/test_author.py
git commit -m "feat: author_dockerfile forwards timeouts to both verify call sites"
```

---

### Task 3: CLI flags on both subcommands + validation + README

**Files:**
- Modify: `src/deployer/cli.py` (`_cmd_verify` at line 38; `_cmd_author` at line 56; parsers at lines 88-100)
- Modify: `README.md` (Usage block at lines 19-22)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes (Tasks 1-2): `deployer.verify.DEFAULT_BUILD_TIMEOUT`, `DEFAULT_HEALTH_TIMEOUT`; `verify(..., *, build_timeout, health_timeout)`; `author_dockerfile(..., build_timeout=, health_timeout=)`.
- Produces: `deployer verify|author ... --build-timeout N --health-timeout N`; values `< 1` → exit 2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_verify_rejects_nonpositive_timeouts(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    assert cli.main(["verify", str(tmp_path), "--build-timeout", "0"]) == 2
    assert cli.main(["verify", str(tmp_path), "--health-timeout", "0"]) == 2


def test_author_rejects_nonpositive_timeouts(tmp_path: Path) -> None:
    assert cli.main(["author", str(tmp_path), "--build-timeout", "0"]) == 2
    assert cli.main(["author", str(tmp_path), "--health-timeout", "-5"]) == 2


def test_verify_flags_reach_library(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    from deployer.models import VerificationReport

    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text(
        (hello_service / "Dockerfile.good").read_text()
    )
    captured = {}

    def spy_verify(
        dockerfile, project_path, target, tool, facts=None, *, build_timeout,
        health_timeout,
    ):
        captured["timeouts"] = (build_timeout, health_timeout)
        return VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.cli.verify", spy_verify)
    exit_code = cli.main(
        [
            "verify",
            str(project),
            "--build-timeout",
            "1200",
            "--health-timeout",
            "45",
        ]
    )
    assert exit_code == 0
    assert captured["timeouts"] == (1200, 45)


def test_author_flags_reach_library(tmp_path: Path, monkeypatch) -> None:
    from deployer.models import AuthoringRun, DeployTarget

    captured = {}

    def spy_author(
        project_path, target, author, *, max_iterations, run_docker,
        build_timeout, health_timeout,
    ):
        captured["timeouts"] = (build_timeout, health_timeout)
        return AuthoringRun(
            project="p",
            target=DeployTarget(),
            stopped_reason="static_only",
            success=False,
        )

    monkeypatch.setattr("deployer.cli.author_dockerfile", spy_author)
    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: object())
    exit_code = cli.main(
        [
            "author",
            str(tmp_path),
            "--no-docker",
            "--build-timeout",
            "1200",
            "--health-timeout",
            "45",
        ]
    )
    assert exit_code == 0
    assert captured["timeouts"] == (1200, 45)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k "timeouts or flags"`
Expected: 4 FAIL — argparse exits with `SystemExit: 2` on the unknown `--build-timeout` flag (argparse errors raise SystemExit, which pytest reports as an error/failure for these tests).

- [ ] **Step 3: Implement in `src/deployer/cli.py`**

Extend the import from `deployer.verify` (line 11):

```python
from deployer.verify import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_HEALTH_TIMEOUT,
    detect_container_tool,
    verify,
)
```

Add two helpers after `_load_target` (line 25):

```python
def _add_timeout_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--build-timeout",
        type=int,
        default=DEFAULT_BUILD_TIMEOUT,
        help="seconds allowed for the container build",
    )
    parser.add_argument(
        "--health-timeout",
        type=int,
        default=DEFAULT_HEALTH_TIMEOUT,
        help="seconds allowed for the healthcheck; ignored for non-service targets",
    )


def _timeout_error(args: argparse.Namespace) -> str | None:
    if args.build_timeout < 1:
        return "--build-timeout must be >= 1"
    if args.health_timeout < 1:
        return "--health-timeout must be >= 1"
    return None
```

In `_cmd_verify` (line 38), validate FIRST (before the Dockerfile-existence check — argument errors are exit 2, missing files exit 1), then forward:

```python
def _cmd_verify(args: argparse.Namespace) -> int:
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    project = Path(args.path)
    dockerfile_path = project / "Dockerfile"
    if not dockerfile_path.is_file():
        print(f"error: {dockerfile_path} not found", file=sys.stderr)
        return 1
    target = _load_target(args.target)
    report = verify(
        dockerfile_path.read_text(),
        project,
        target,
        detect_container_tool(),
        analyze_project(project),
        build_timeout=args.build_timeout,
        health_timeout=args.health_timeout,
    )
    _print_report(report)
    return 0 if report.passed else 1
```

In `_cmd_author` (line 56), add validation next to the existing `--max-iterations` check and forward:

```python
    if args.max_iterations < 1:
        print("error: --max-iterations must be >= 1", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    run = author_dockerfile(
        project,
        target,
        AnthropicAuthor(),
        max_iterations=args.max_iterations,
        run_docker=not args.no_docker,
        build_timeout=args.build_timeout,
        health_timeout=args.health_timeout,
    )
```

In `main()`, register the flags on BOTH subparsers (after each parser's existing arguments, lines 88-100):

```python
    _add_timeout_flags(p_verify)
    ...
    _add_timeout_flags(p_author)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 5: Update README Usage block (lines 19-22)**

```sh
uv run deployer author <project-path> [--target target.json] [--no-docker] \
    [--build-timeout 600] [--health-timeout 30]
uv run deployer verify <project-path> [--build-timeout 600] [--health-timeout 30]
# verify checks <project-path>/Dockerfile; --health-timeout is ignored for
# non-service targets. Slow source builds (e.g. llama-cpp-python) need
# --build-timeout well above the 600s default.
```

- [ ] **Step 6: Full check + commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check && uv run pytest
git add src/deployer/cli.py tests/test_cli.py README.md
git commit -m "feat: --build-timeout/--health-timeout flags on verify and author"
```

---

### Task 4: Docker-marked smoke check + wrap-up

**Files:**
- No source changes expected; runs the opt-in suites and finishes the branch.

- [ ] **Step 1: Run the docker-marked tests locally (podman present on this machine)**

Run: `uv run pytest -m docker -v`
Expected: all PASS (behavior with default timeouts is unchanged).

- [ ] **Step 2: Final full sweep**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check && uv run pytest
```

Expected: clean.

- [ ] **Step 3: Hand off** — implementation complete; proceed per superpowers:finishing-a-development-branch (PR to master, as with PR #1/#2).
