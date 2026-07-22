# Remote Verify (Phase 1 + 1.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** L2 verification (build/run/healthcheck) can run on a remote SSH Docker/Podman host via a first-class `ContainerRuntime`, with build-context hygiene and comparability metadata in all reports.

**Architecture:** A new `src/deployer/runtime.py` owns runtime resolution (flags → deployer env → native env → local) and a single `container_run()` chokepoint that injects `DOCKER_HOST`/`CONTAINER_HOST`; `verify.py` threads a `ContainerRuntime` through all seven container CLI calls and builds from an isolated temp context; `AuthoringRun`/`VerificationReport` record runtime + run metadata.

**Tech Stack:** Python 3.12, pydantic v2, argparse, subprocess, pytest (markers: `docker`), uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-07-21-bench-remote-verify-design.md` (Phases 1 and 1.5 only; corpus/bench/golden are later plans).

## Global Constraints

- Package management: `uv` only, never pip. Tests: `uv run pytest` (unit; docker-marked excluded by default via `addopts = "-m 'not docker and not llm'"`).
- After every task: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check` must be clean.
- Line length 88; type hints everywhere; public APIs get docstrings.
- Branch: all commits go to `feature/bench-remote-verify` (already checked out). Never commit to `master`.
- Backward compat: `VerificationReport.docker_available` JSON field keeps its name. Existing unit tests must keep passing without Docker or SSH.
- Deployer-provided hosts accept **`ssh://` only**; native `DOCKER_HOST`/`CONTAINER_HOST` values are captured as-is.
- `runtime_env()` output must never be logged or printed (may carry secrets).

---

### Task 0: Podman-remote spike (manual, user-run)

No code. Before final remote validation (Task 8), the user runs this matrix on their host and records results in `.superpowers/sdd/progress.md`:

```sh
# docker local (on a machine with docker)
docker version
# docker ssh
DOCKER_HOST=ssh://user@host docker version && DOCKER_HOST=ssh://user@host docker run --rm alpine:3.20 true
# podman local
podman version
# podman remote (needs enabled podman socket on the remote)
CONTAINER_HOST=ssh://user@host/run/user/1000/podman/podman.sock podman --remote version
```

Findings feed Task 8 only; Tasks 1–7 are mock-based and do not block on this.

---

### Task 1: `ContainerRuntime` model + `resolve_runtime`

**Files:**
- Modify: `src/deployer/models.py` (add `ContainerRuntime`; extend `VerificationReport`, `AuthoringRun`)
- Create: `src/deployer/runtime.py`
- Test: `tests/test_runtime.py` (new)

**Interfaces:**
- Produces: `deployer.models.ContainerRuntime(tool, host, host_source)` with property `remote: bool`; `VerificationReport.runtime: ContainerRuntime | None`; `AuthoringRun.runtime: ContainerRuntime | None`; `deployer.runtime.RuntimeConfigError(Exception)`; `deployer.runtime.resolve_runtime(tool_arg: str | None = None, host_arg: str | None = None, env: Mapping[str, str] | None = None) -> ContainerRuntime | None`; `deployer.runtime.NATIVE_HOST_ENV: dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runtime.py`:

```python
"""Runtime resolution matrix: flags -> deployer env -> native env -> local."""

import pytest

from deployer.models import ContainerRuntime
from deployer.runtime import RuntimeConfigError, resolve_runtime


@pytest.fixture()
def all_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.shutil.which", lambda tool: f"/usr/bin/{tool}"
    )


@pytest.fixture()
def no_tools(monkeypatch) -> None:
    monkeypatch.setattr("deployer.runtime.shutil.which", lambda tool: None)


def test_explicit_tool_and_cli_host(all_tools) -> None:
    rt = resolve_runtime("docker", "ssh://u@h", env={})
    assert rt == ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    assert rt.remote


def test_local_default_prefers_podman(all_tools) -> None:
    rt = resolve_runtime(env={})
    assert rt == ContainerRuntime(tool="podman", host=None, host_source="local")
    assert not rt.remote


def test_docker_detected_when_no_podman(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.shutil.which",
        lambda tool: "/usr/bin/docker" if tool == "docker" else None,
    )
    rt = resolve_runtime(env={})
    assert rt is not None and rt.tool == "docker"


def test_no_tools_means_static_only(no_tools) -> None:
    assert resolve_runtime(env={}) is None


def test_deployer_env_tool_and_host(all_tools) -> None:
    env = {
        "DEPLOYER_CONTAINER_TOOL": "docker",
        "DEPLOYER_CONTAINER_HOST": "ssh://u@h",
    }
    rt = resolve_runtime(env=env)
    assert rt == ContainerRuntime(
        tool="docker", host="ssh://u@h", host_source="deployer_env"
    )


def test_cli_flags_beat_deployer_env(all_tools) -> None:
    env = {
        "DEPLOYER_CONTAINER_TOOL": "podman",
        "DEPLOYER_CONTAINER_HOST": "ssh://env@h",
    }
    rt = resolve_runtime("docker", "ssh://cli@h", env=env)
    assert rt is not None
    assert (rt.tool, rt.host, rt.host_source) == ("docker", "ssh://cli@h", "cli")


def test_native_env_captured_for_selected_tool(all_tools) -> None:
    rt = resolve_runtime("docker", env={"DOCKER_HOST": "tcp://old:2375"})
    assert rt is not None
    assert (rt.host, rt.host_source) == ("tcp://old:2375", "native_env")


def test_native_env_of_other_tool_ignored(all_tools) -> None:
    rt = resolve_runtime("podman", env={"DOCKER_HOST": "ssh://u@h"})
    assert rt == ContainerRuntime(tool="podman", host=None, host_source="local")


def test_explicit_tool_missing_raises(no_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime("docker", env={})


def test_env_tool_invalid_value_raises(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime(env={"DEPLOYER_CONTAINER_TOOL": "nerdctl"})


def test_cli_host_must_be_ssh(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime("docker", "tcp://h:2375", env={})


def test_deployer_env_host_must_be_ssh(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime(env={"DEPLOYER_CONTAINER_HOST": "tcp://h:2375"})


def test_explicit_host_without_any_tool_raises(no_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime(host_arg="ssh://u@h", env={})


def test_runtime_round_trips_json() -> None:
    rt = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    assert ContainerRuntime.model_validate_json(rt.model_dump_json()) == rt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deployer.runtime'` (and `ImportError` for `ContainerRuntime`).

- [ ] **Step 3: Add `ContainerRuntime` to `src/deployer/models.py`**

Insert after the `SystemDepHint` class (before `CheckStatus`):

```python
class ContainerRuntime(BaseModel):
    """Where and with which CLI the L2 sandbox runs.

    `host_source` records how the host was chosen so reports never lie
    about where a run happened (a pre-set DOCKER_HOST is captured, not
    silently inherited).
    """

    tool: Literal["docker", "podman"]
    host: str | None = None
    host_source: Literal["cli", "deployer_env", "native_env", "local"] = "local"

    @property
    def remote(self) -> bool:
        return self.host is not None
```

In `VerificationReport`, add after `image_size_bytes`:

```python
    runtime: ContainerRuntime | None = None
```

In `AuthoringRun`, add after `hints_offered`:

```python
    runtime: ContainerRuntime | None = None
```

- [ ] **Step 4: Create `src/deployer/runtime.py`**

```python
"""Container runtime resolution and the single subprocess chokepoint."""

import os
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any

from deployer.models import ContainerRuntime

NATIVE_HOST_ENV = {"docker": "DOCKER_HOST", "podman": "CONTAINER_HOST"}
_DETECTION_ORDER = ("podman", "docker")


class RuntimeConfigError(Exception):
    """Explicitly-invalid runtime configuration; the CLI maps this to exit 2."""


def _validate_ssh(host: str, origin: str) -> None:
    if not host.startswith("ssh://"):
        raise RuntimeConfigError(f"{origin} must be an ssh:// URL, got {host!r}")


def _resolve_tool(tool_arg: str | None, env: Mapping[str, str]) -> str | None:
    if tool_arg is not None:
        if shutil.which(tool_arg) is None:
            raise RuntimeConfigError(
                f"--container-tool {tool_arg}: not found on PATH"
            )
        return tool_arg
    env_tool = env.get("DEPLOYER_CONTAINER_TOOL")
    if env_tool:
        if env_tool not in NATIVE_HOST_ENV:
            raise RuntimeConfigError(
                "DEPLOYER_CONTAINER_TOOL must be 'docker' or 'podman', "
                f"got {env_tool!r}"
            )
        if shutil.which(env_tool) is None:
            raise RuntimeConfigError(
                f"DEPLOYER_CONTAINER_TOOL {env_tool}: not found on PATH"
            )
        return env_tool
    for tool in _DETECTION_ORDER:
        if shutil.which(tool):
            return tool
    return None


def resolve_runtime(
    tool_arg: str | None = None,
    host_arg: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ContainerRuntime | None:
    """Resolve the container runtime; None means implicit static-only.

    Raises RuntimeConfigError for explicitly-invalid configuration
    (requested tool missing, malformed host, host without any tool).
    """
    if env is None:
        env = os.environ
    tool = _resolve_tool(tool_arg, env)
    if tool is None:
        if host_arg or env.get("DEPLOYER_CONTAINER_HOST"):
            raise RuntimeConfigError(
                "container host given but no container tool found on PATH"
            )
        return None
    if host_arg:
        _validate_ssh(host_arg, "--container-host")
        return ContainerRuntime(tool=tool, host=host_arg, host_source="cli")
    deployer_host = env.get("DEPLOYER_CONTAINER_HOST")
    if deployer_host:
        _validate_ssh(deployer_host, "DEPLOYER_CONTAINER_HOST")
        return ContainerRuntime(
            tool=tool, host=deployer_host, host_source="deployer_env"
        )
    native_host = env.get(NATIVE_HOST_ENV[tool])
    if native_host:
        return ContainerRuntime(
            tool=tool, host=native_host, host_source="native_env"
        )
    return ContainerRuntime(tool=tool)
```

(`subprocess` and `Any` imports are used in Task 2; keep them out until then if ruff flags them — add in Task 2.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_runtime.py tests/test_models.py -v`
Expected: all PASS.

- [ ] **Step 6: Format, lint, typecheck**

Run: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/deployer/models.py src/deployer/runtime.py tests/test_runtime.py
git commit -m "feat: ContainerRuntime model and resolve_runtime resolution matrix"
```

---

### Task 2: `runtime_env` + `container_run` chokepoint

**Files:**
- Modify: `src/deployer/runtime.py`
- Test: `tests/test_runtime.py` (append)

**Interfaces:**
- Consumes: `ContainerRuntime`, `NATIVE_HOST_ENV` from Task 1.
- Produces: `deployer.runtime.runtime_env(runtime: ContainerRuntime) -> dict[str, str]`; `deployer.runtime.container_run(runtime: ContainerRuntime, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runtime.py`:

```python
from deployer.runtime import container_run, runtime_env


def test_runtime_env_overlays_docker_host_for_cli_source(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DOCKER_HOST", "tcp://stale:2375")
    rt = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    env = runtime_env(rt)
    assert env["DOCKER_HOST"] == "ssh://u@h"
    assert env["PATH"] == "/usr/bin"  # full os.environ copy, not a minimal dict


def test_runtime_env_overlays_container_host_for_podman(monkeypatch) -> None:
    rt = ContainerRuntime(
        tool="podman", host="ssh://u@h", host_source="deployer_env"
    )
    assert runtime_env(rt)["CONTAINER_HOST"] == "ssh://u@h"


def test_runtime_env_untouched_for_native_and_local(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://pre@set")
    native = ContainerRuntime(
        tool="docker", host="ssh://pre@set", host_source="native_env"
    )
    assert runtime_env(native)["DOCKER_HOST"] == "ssh://pre@set"
    monkeypatch.delenv("DOCKER_HOST")
    local = ContainerRuntime(tool="docker")
    assert "DOCKER_HOST" not in runtime_env(local)


def test_container_run_prepends_tool_and_injects_env(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return "sentinel"

    monkeypatch.setattr("deployer.runtime.subprocess.run", fake_run)
    rt = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    result = container_run(rt, ["build", "-t", "x", "."], capture_output=True)
    assert result == "sentinel"
    assert seen["cmd"] == ["docker", "build", "-t", "x", "."]
    assert seen["env"]["DOCKER_HOST"] == "ssh://u@h"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runtime.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'runtime_env'`.

- [ ] **Step 3: Implement in `src/deployer/runtime.py`**

Append (and ensure `import subprocess` and `from typing import Any` are present):

```python
def runtime_env(runtime: ContainerRuntime) -> dict[str, str]:
    """Process env for container CLI calls. Never log the result.

    Starts from a full os.environ copy (PATH, HOME, SSH_AUTH_SOCK and
    docker/podman config vars must survive or SSH agent auth breaks) and
    overlays the tool-native host var only for deployer-chosen hosts.
    """
    env = os.environ.copy()
    if runtime.host_source in ("cli", "deployer_env") and runtime.host is not None:
        env[NATIVE_HOST_ENV[runtime.tool]] = runtime.host
    return env


def container_run(
    runtime: ContainerRuntime, args: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """The single chokepoint for every container CLI invocation."""
    return subprocess.run([runtime.tool, *args], env=runtime_env(runtime), **kwargs)
```

- [ ] **Step 4: Run tests, then format/lint/typecheck**

Run: `uv run pytest tests/test_runtime.py -v && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/deployer/runtime.py tests/test_runtime.py
git commit -m "feat: runtime_env overlay and container_run chokepoint"
```

---

### Task 3: Thread `ContainerRuntime` through verify, author, CLI

**Files:**
- Modify: `src/deployer/verify.py` (signatures + all seven subprocess calls)
- Modify: `src/deployer/author.py` (`run_docker: bool` → `runtime: ContainerRuntime | None`)
- Modify: `src/deployer/cli.py` (resolve runtime, map `RuntimeConfigError` → exit 2)
- Test: `tests/test_verify_docker.py`, `tests/test_author.py`, `tests/test_cli.py`, `tests/test_verify_static.py` (mechanical updates)

**Interfaces:**
- Consumes: `resolve_runtime`, `container_run`, `RuntimeConfigError` (Tasks 1–2).
- Produces: `verify(dockerfile, project_path, target, runtime: ContainerRuntime | None, facts=None, *, build_timeout, health_timeout) -> VerificationReport` (report has `.runtime` set); `verify_docker(dockerfile, project_path, target, runtime: ContainerRuntime, *, ...)`; `author_dockerfile(project_path, target, author, *, max_iterations=3, runtime: ContainerRuntime | None = None, build_timeout, health_timeout) -> AuthoringRun` (run has `.runtime` set). `detect_container_tool()` is **deleted**.

- [ ] **Step 1: Update `src/deployer/verify.py`**

Replace the import block addition and `detect_container_tool`:

```python
from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    VerificationReport,
)
from deployer.runtime import container_run
```

Delete `detect_container_tool()` entirely.

Change every L2 function to take `runtime: ContainerRuntime` and route through `container_run`. The full new bodies (replacing `tool: str` versions):

```python
def _build(
    dockerfile: str,
    context_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    tag: str,
    timeout: int,
) -> CheckResult:
    try:
        proc = container_run(
            runtime,
            [
                "build",
                "--memory",
                target.memory_limit,
                "-t",
                tag,
                "-f",
                "-",
                str(context_path),
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
```

(`context_path` rename is cosmetic here; Task 5 makes it a real temp context. `_classify` still takes stderr here; Task 4 changes that.)

`_image_size(runtime, tag)`: same body with `container_run(runtime, ["image", "inspect", ...], ...)`.

`_run_healthcheck(target, runtime, tag, timeout)`: replace the four `subprocess.run([tool, ...])` calls with `container_run(runtime, ["run", "-d", ...])`, `container_run(runtime, ["exec", container, "python", "-c", probe], ...)`, `container_run(runtime, ["logs", container], ...)`, and in `finally`: `container_run(runtime, ["rm", "-f", container], capture_output=True, timeout=30)`.

`verify_docker(dockerfile, project_path, target, runtime, *, build_timeout, health_timeout)`: pass `runtime` down; `finally` cleanup becomes `container_run(runtime, ["rmi", "-f", tag], capture_output=True, timeout=60)`.

`verify(...)`: parameter `tool: str | None` becomes `runtime: ContainerRuntime | None`; body:

```python
    report = verify_static(dockerfile, project_path, facts)
    report.runtime = runtime
    if runtime is None:
        return report
    report.docker_available = True
    if report.passed:
        docker_results, image_size = verify_docker(
            dockerfile,
            project_path,
            target,
            runtime,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        report.results.extend(docker_results)
        report.image_size_bytes = image_size
    return report
```

- [ ] **Step 2: Update `src/deployer/author.py`**

Signature and body: replace `run_docker: bool = True` with `runtime: ContainerRuntime | None = None`; delete the `detect_container_tool` import and the `tool = detect_container_tool() if run_docker else None` line; pass `runtime` to both `verify(...)` calls; `stopped_reason` check becomes `"success" if runtime is not None else "static_only"`; the returned `AuthoringRun` gains `runtime=runtime` and `docker_available=runtime is not None`.

- [ ] **Step 3: Update `src/deployer/cli.py`**

Replace the `detect_container_tool` import with:

```python
from deployer.runtime import RuntimeConfigError, resolve_runtime
```

In `_cmd_verify`, replace `detect_container_tool()` with:

```python
    try:
        runtime = resolve_runtime()
    except RuntimeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

and pass `runtime` to `verify(...)`. In `_cmd_author`, resolve the same way (before calling `author_dockerfile`) and call with `runtime=None if args.no_docker else runtime`. (CLI flags come in Task 6; env vars already work through `resolve_runtime`.)

- [ ] **Step 4: Update tests mechanically**

`tests/test_verify_docker.py` — fixture and call sites:

```python
from deployer.models import ContainerRuntime
from deployer.runtime import resolve_runtime


@pytest.fixture(scope="module")
def runtime() -> ContainerRuntime:
    found = resolve_runtime()
    if found is None:
        pytest.skip("no container runtime available")
    return found
```

Every `verify(dockerfile, project, TARGET, tool)` becomes `verify(dockerfile, project, TARGET, runtime)`; `test_no_tool_degrades_to_static_only` passes `None` (unchanged semantics).

`tests/test_author.py` — wherever a test monkeypatched `deployer.author.detect_container_tool` (e.g. `lambda: "docker"`), instead pass `runtime=ContainerRuntime(tool="docker")` to `author_dockerfile(...)`; spy `verify` doubles take `runtime` positionally where they took `tool` (the argument position is unchanged: 4th positional).

`tests/test_cli.py` and `tests/test_verify_static.py` — update any `detect_container_tool` monkeypatch to `monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)` (static-only) or a `ContainerRuntime(...)` return.

- [ ] **Step 5: Run the unit suite**

Run: `uv run pytest`
Expected: all PASS (docker-marked excluded by default addopts).

- [ ] **Step 6: Run docker-marked suite locally (if a runtime exists)**

Run: `uv run pytest -m docker`
Expected: PASS on podman, as before.

- [ ] **Step 7: Format, lint, typecheck; commit**

Run: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer tests
git commit -m "refactor: thread ContainerRuntime through verify/author/cli"
```

---

### Task 4: SSH failure markers + classify combined output

**Files:**
- Modify: `src/deployer/verify.py` (`ENVIRONMENT_MARKERS`, `_classify`, call sites)
- Test: `tests/test_verify_static.py` (append; `_classify` unit tests live here with the other verify unit tests)

**Interfaces:**
- Produces: `_classify(output: str) -> FailureKind` where `output` is `stdout + "\n" + stderr`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py`:

```python
import pytest

from deployer.models import FailureKind
from deployer.verify import _classify


@pytest.mark.parametrize(
    "line",
    [
        "u@host: Permission denied (publickey).",
        "Host key verification failed.",
        "ssh: Could not resolve hostname bench: nodename nor servname known",
        "ssh: connect to host 10.0.0.5 port 22: Operation timed out",
        "ssh: connect to host bench port 22: Connection refused",
        "Cannot connect to the Docker daemon at ssh://u@host. Is it running?",
        "error during connect: Get \"http://docker.example\": EOF",
        "Error: context deadline exceeded",
        "connection timed out",
    ],
)
def test_ssh_and_daemon_errors_are_environment(line: str) -> None:
    assert _classify(line) is FailureKind.ENVIRONMENT


def test_classify_sees_stdout_side_of_combined_output() -> None:
    combined = "error during connect: dial tcp: timeout\n" + ""
    assert _classify(combined) is FailureKind.ENVIRONMENT


def test_ordinary_build_error_stays_authoring() -> None:
    assert _classify("E: Unable to locate package libfoo") is FailureKind.AUTHORING
```

- [ ] **Step 2: Run to verify the new markers fail**

Run: `uv run pytest tests/test_verify_static.py -v -k "environment or authoring or combined"`
Expected: `permission denied (publickey)`, `host key`, `resolve hostname`, `ssh: connect`, `docker daemon`, `error during connect`, `context deadline` cases FAIL (classified AUTHORING today).

- [ ] **Step 3: Implement**

In `src/deployer/verify.py` extend the tuple:

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
    "permission denied (publickey)",
    "host key verification failed",
    "could not resolve hostname",
    "ssh: connect to host",
    "connection timed out",
    "cannot connect to the docker daemon",
    "error during connect",
    "context deadline exceeded",
)
```

Rename `_classify`'s parameter to `output` (behavior identical — lowercase substring match). Update the two call sites to pass combined output:

- in `_build`: `failure_kind=_classify(proc.stdout + "\n" + proc.stderr)`, and `message=_tail(proc.stderr or proc.stdout)`;
- in `_run_healthcheck` (container start failure): `failure_kind=_classify(started.stdout + "\n" + started.stderr)`, `message=_tail(started.stderr or started.stdout)`.

- [ ] **Step 4: Run tests, format/lint/typecheck, commit**

Run: `uv run pytest && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: SSH/daemon environment markers, classify combined output"
```

---

### Task 5: Build-context hygiene (isolated temp context)

**Files:**
- Modify: `src/deployer/verify.py` (`CONTEXT_IGNORE`, `_isolated_context`, `verify_docker` uses it)
- Test: `tests/test_verify_static.py` (context unit tests), `tests/test_verify_docker.py` (one integration test)

**Interfaces:**
- Produces: `deployer.verify.CONTEXT_IGNORE: tuple[str, ...]`; `deployer.verify._isolated_context(project_path: Path) -> Iterator[Path]` (contextmanager). Task 6+ and the future bench reuse `CONTEXT_IGNORE`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py`:

```python
from deployer.verify import _isolated_context


def test_isolated_context_excludes_secrets_and_junk(tmp_path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "mod.py").write_text("x = 1\n")
    for junk in (".env", ".env.local"):
        (tmp_path / junk).write_text("SECRET=1\n")
    for junk_dir in (".git", ".venv", ".deployer", "__pycache__"):
        (tmp_path / junk_dir).mkdir()
        (tmp_path / junk_dir / "f").write_text("x")
    with _isolated_context(tmp_path) as ctx:
        assert ctx != tmp_path
        assert (ctx / "app.py").read_text() == "print('hi')\n"
        assert (ctx / "nested" / "mod.py").exists()
        assert not (ctx / ".env").exists()
        assert not (ctx / ".env.local").exists()
        for junk_dir in (".git", ".venv", ".deployer", "__pycache__"):
            assert not (ctx / junk_dir).exists()
    assert not ctx.exists()  # cleaned up on exit
```

Append to `tests/test_verify_docker.py`:

```python
def test_build_context_excludes_dotenv(
    hello_service: Path, runtime, tmp_path: Path
) -> None:
    import shutil as _shutil

    project = tmp_path / "proj"
    _shutil.copytree(hello_service, project)
    (project / ".env").write_text("SECRET=do-not-ship\n")
    dockerfile = (
        (project / "Dockerfile.good").read_text()
        + "\nRUN test ! -e /app/.env\nRUN test ! -e .env\n"
    )
    report = verify(dockerfile, project, TARGET, runtime)
    assert _by_id(report, "build").status is CheckStatus.PASSED
```

(If `Dockerfile.good` COPYs selectively rather than `COPY . .`, the `RUN test ! -e` guards still validate the context because a `COPY .env` would fail — keep the test as written; it asserts the built image never saw the file.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_verify_static.py -v -k isolated_context`
Expected: FAIL — `ImportError: cannot import name '_isolated_context'`.

- [ ] **Step 3: Implement in `src/deployer/verify.py`**

Add imports `import tempfile` and `from collections.abc import Iterator`, `from contextlib import contextmanager`. Add near the L2 section:

```python
CONTEXT_IGNORE = (
    ".git",
    ".venv",
    ".deployer",
    ".env",
    ".env.*",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
)


@contextmanager
def _isolated_context(project_path: Path) -> Iterator[Path]:
    """Deterministic temp build context: the project minus CONTEXT_IGNORE.

    Restores the MVP invariant "no secrets in the build context, ever" —
    with a remote daemon the context leaves the machine entirely.
    """
    with tempfile.TemporaryDirectory(prefix="deployer-context-") as tmp:
        context = Path(tmp) / "context"
        shutil.copytree(
            project_path, context, ignore=shutil.ignore_patterns(*CONTEXT_IGNORE)
        )
        yield context
```

In `verify_docker`, wrap the build:

```python
    try:
        with _isolated_context(project_path) as context:
            build_result = _build(
                dockerfile, context, target, runtime, tag, build_timeout
            )
        results.append(build_result)
        ...
```

- [ ] **Step 4: Run tests, format/lint/typecheck, commit**

Run: `uv run pytest && uv run pytest -m docker && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer/verify.py tests
git commit -m "feat: isolated temp build context excluding secrets and junk"
```

---

### Task 6: CLI flags `--container-tool` / `--container-host` + README

**Files:**
- Modify: `src/deployer/cli.py`
- Modify: `README.md`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `resolve_runtime(tool_arg, host_arg)`, `RuntimeConfigError`.
- Produces: CLI surface `deployer verify|author ... [--container-tool {docker,podman}] [--container-host ssh://...]`; env defaults `DEPLOYER_CONTAINER_TOOL` / `DEPLOYER_CONTAINER_HOST`; invalid config exits 2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (follow the file's existing project-scaffolding helper pattern; `_make_project` below stands for the existing helper that creates a valid project dir with a Dockerfile):

```python
from deployer.cli import main
from deployer.models import ContainerRuntime


def test_verify_passes_cli_runtime_flags_through(tmp_path, monkeypatch, capsys):
    project = _make_project(tmp_path)
    seen: dict = {}

    def fake_resolve(tool_arg=None, host_arg=None, env=None):
        seen["args"] = (tool_arg, host_arg)
        return None  # static-only keeps the test docker-free

    monkeypatch.setattr("deployer.cli.resolve_runtime", fake_resolve)
    main(
        [
            "verify",
            str(project),
            "--container-tool",
            "docker",
            "--container-host",
            "ssh://u@h",
        ]
    )
    assert seen["args"] == ("docker", "ssh://u@h")


def test_verify_invalid_runtime_config_exits_2(tmp_path, monkeypatch, capsys):
    project = _make_project(tmp_path)
    from deployer.runtime import RuntimeConfigError

    def boom(tool_arg=None, host_arg=None, env=None):
        raise RuntimeConfigError("--container-host must be an ssh:// URL")

    monkeypatch.setattr("deployer.cli.resolve_runtime", boom)
    code = main(["verify", str(project), "--container-host", "tcp://h:1"])
    assert code == 2
    assert "ssh://" in capsys.readouterr().err


def test_author_no_docker_skips_runtime_resolution(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    monkeypatch.setattr(
        "deployer.cli.resolve_runtime",
        lambda *a, **k: pytest.fail("resolve_runtime must not be called"),
    )
    monkeypatch.setattr(
        "deployer.cli.author_dockerfile",
        lambda *a, **k: pytest.skip("reached author"),
    )
    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: object())
    main(["author", str(project), "--no-docker"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -v -k "runtime or no_docker"`
Expected: FAIL — `error: unrecognized arguments: --container-tool`.

- [ ] **Step 3: Implement in `src/deployer/cli.py`**

Add next to `_add_timeout_flags`:

```python
def _add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--container-tool",
        choices=("docker", "podman"),
        default=None,
        help="container CLI to use (default: DEPLOYER_CONTAINER_TOOL or detection)",
    )
    parser.add_argument(
        "--container-host",
        default=None,
        metavar="ssh://user@host",
        help="remote engine over SSH (default: DEPLOYER_CONTAINER_HOST or local)",
    )
```

Call `_add_runtime_flags(p_verify)` and `_add_runtime_flags(p_author)` in `main`. Add one shared resolver used by both commands:

```python
def _resolve_runtime_or_error(args: argparse.Namespace):
    try:
        return resolve_runtime(args.container_tool, args.container_host)
    except RuntimeConfigError as exc:
        return f"{exc}"
```

`_cmd_verify`: `runtime = _resolve_runtime_or_error(args)`; if `isinstance(runtime, str)` → print `error:` to stderr, return 2; else pass to `verify(...)`.

`_cmd_author`: when `args.no_docker` is true, skip resolution entirely and pass `runtime=None`; otherwise resolve the same way (exit 2 on error) and pass `runtime=runtime`.

- [ ] **Step 4: Update `README.md` Usage section**

Extend the usage block and add the canonical remote invocation:

```markdown
uv run deployer author <project-path> [--target target.json] [--no-docker] \
    [--container-tool docker|podman] [--container-host ssh://user@host] \
    [--build-timeout 600] [--health-timeout 30]
uv run deployer verify <project-path> [same flags]

Remote verification (the L2 sandbox on another machine over SSH):

    DEPLOYER_CONTAINER_TOOL=docker \
    DEPLOYER_CONTAINER_HOST=ssh://user@host \
    uv run pytest -m docker

`--container-host` / `DEPLOYER_CONTAINER_HOST` accept `ssh://` URLs only;
a pre-existing `DOCKER_HOST`/`CONTAINER_HOST` is honored and recorded in
reports as `host_source: "native_env"`. The build context is copied to a
temp dir minus `.git`, `.venv`, `.deployer`, `.env*`, caches — secrets
never reach the daemon, local or remote. Invalid runtime configuration
(missing requested tool, non-ssh host) exits 2.
```

- [ ] **Step 5: Run tests, format/lint/typecheck, commit**

Run: `uv run pytest && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer/cli.py README.md tests/test_cli.py
git commit -m "feat: --container-tool/--container-host flags with exit-2 validation"
```

---

### Task 7 (Phase 1.5): Run-metadata hardening

**Files:**
- Modify: `src/deployer/models.py` (`RuntimeVersions`, `AuthorInfo`, report/run fields)
- Modify: `src/deployer/runtime.py` (`probe_runtime_versions`)
- Modify: `src/deployer/llm.py` (`AnthropicAuthor.info()`)
- Modify: `src/deployer/author.py` (record metadata)
- Modify: `src/deployer/cli.py` (verify path records versions)
- Test: `tests/test_runtime.py`, `tests/test_llm.py`, `tests/test_author.py` (append)

**Interfaces:**
- Produces: `RuntimeVersions(client_version, server_version, platform, probe_warning)`; `AuthorInfo(backend, model_id, prompt_sha256)`; `probe_runtime_versions(runtime) -> RuntimeVersions` (never raises); `AnthropicAuthor.info() -> AuthorInfo`; `AuthoringRun` fields `build_timeout_s: int | None`, `health_timeout_s: int | None`, `max_iterations: int | None`, `runtime_versions: RuntimeVersions | None`, `author_info: AuthorInfo | None`, `deployer_version: str | None`, `deployer_git_sha: str | None`; `VerificationReport.runtime_versions: RuntimeVersions | None` (populated by the CLI verify path only).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runtime.py`:

```python
import json

from deployer.models import RuntimeVersions
from deployer.runtime import probe_runtime_versions


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    class P:
        pass

    p = P()
    p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
    return p


def test_probe_parses_version_json(monkeypatch) -> None:
    payload = json.dumps(
        {
            "Client": {"Version": "27.0.1"},
            "Server": {"Version": "27.0.1", "Os": "linux", "Arch": "amd64"},
        }
    )
    monkeypatch.setattr(
        "deployer.runtime.container_run",
        lambda *a, **k: _fake_proc(0, stdout=payload),
    )
    versions = probe_runtime_versions(ContainerRuntime(tool="docker"))
    assert versions.client_version == "27.0.1"
    assert versions.server_version == "27.0.1"
    assert versions.platform == "linux/amd64"
    assert versions.probe_warning is None


def test_probe_is_best_effort_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.container_run",
        lambda *a, **k: _fake_proc(1, stderr="cannot connect"),
    )
    versions = probe_runtime_versions(ContainerRuntime(tool="docker"))
    assert versions.probe_warning is not None
    assert versions.client_version is None


def test_probe_never_raises_on_garbage(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.container_run",
        lambda *a, **k: _fake_proc(0, stdout="not json"),
    )
    assert probe_runtime_versions(ContainerRuntime(tool="podman")).probe_warning
```

Append to `tests/test_llm.py`:

```python
import hashlib

from deployer.llm import SYSTEM_PROMPT, AnthropicAuthor


def test_author_info_exposes_model_and_prompt_hash() -> None:
    info = AnthropicAuthor(client=object()).info()
    assert info.backend == "anthropic"
    assert info.model_id == "claude-opus-4-8"
    assert info.prompt_sha256 == hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
```

Append to `tests/test_author.py` (uses the file's existing stub-author pattern; a stub without `.info()` must not break):

```python
def test_run_records_effective_config(hello_service: Path) -> None:
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        _StubAuthor(),          # the file's existing always-good stub
        max_iterations=2,
        runtime=None,
        build_timeout=123,
        health_timeout=7,
    )
    assert run.build_timeout_s == 123
    assert run.health_timeout_s == 7
    assert run.max_iterations == 2
    assert run.author_info is None  # stub has no .info()
    assert run.deployer_version  # installed package metadata
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_runtime.py tests/test_llm.py tests/test_author.py -v`
Expected: FAIL — missing `RuntimeVersions`, `probe_runtime_versions`, `info`, new fields.

- [ ] **Step 3: Add models**

In `src/deployer/models.py` after `ContainerRuntime`:

```python
class RuntimeVersions(BaseModel):
    """Best-effort engine/CLI versions; failures are warnings, never fatal."""

    client_version: str | None = None
    server_version: str | None = None
    platform: str | None = None
    probe_warning: str | None = None


class AuthorInfo(BaseModel):
    """Which author produced a run — required for comparable bench data."""

    backend: str
    model_id: str | None = None
    prompt_sha256: str | None = None
```

`VerificationReport` gains `runtime_versions: RuntimeVersions | None = None`.
`AuthoringRun` gains:

```python
    build_timeout_s: int | None = None
    health_timeout_s: int | None = None
    max_iterations: int | None = None
    runtime_versions: RuntimeVersions | None = None
    author_info: AuthorInfo | None = None
    deployer_version: str | None = None
    deployer_git_sha: str | None = None
```

- [ ] **Step 4: Implement `probe_runtime_versions` in `src/deployer/runtime.py`**

```python
import json

from deployer.models import ContainerRuntime, RuntimeVersions


def probe_runtime_versions(runtime: ContainerRuntime) -> RuntimeVersions:
    """Best-effort `<tool> version` probe; never raises, never blocks a run."""
    try:
        proc = container_run(
            runtime,
            ["version", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return RuntimeVersions(
                probe_warning=detail[-1] if detail else "version probe failed"
            )
        data = json.loads(proc.stdout)
        client = (data.get("Client") or {}).get("Version")
        server_block = data.get("Server") or {}
        server = server_block.get("Version")
        os_name = server_block.get("Os") or ""
        arch = server_block.get("Arch") or ""
        platform = f"{os_name}/{arch}" if os_name and arch else None
        return RuntimeVersions(
            client_version=client, server_version=server, platform=platform
        )
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        return RuntimeVersions(probe_warning=f"{exc.__class__.__name__}: {exc}")
```

(Podman's JSON nests differently on some versions — the `.get` chains degrade to `None`s plus no warning, which is acceptable best-effort. A wrong-shape non-dict payload raises `AttributeError`; add it to the except tuple: `(subprocess.TimeoutExpired, OSError, json.JSONDecodeError, AttributeError)`.)

- [ ] **Step 5: Implement `AnthropicAuthor.info()` in `src/deployer/llm.py`**

```python
import hashlib

from deployer.models import AuthorInfo


class AnthropicAuthor:
    ...
    def info(self) -> AuthorInfo:
        """Comparability metadata for run reports."""
        return AuthorInfo(
            backend="anthropic",
            model_id=self._model,
            prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        )
```

- [ ] **Step 6: Record metadata in `src/deployer/author.py`**

Add helpers (top-level, above `author_dockerfile`):

```python
import importlib.metadata
import subprocess


def _deployer_version() -> str | None:
    try:
        return importlib.metadata.version("deployer")
    except importlib.metadata.PackageNotFoundError:
        return None


def _deployer_git_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None
```

In `author_dockerfile`, before the return: probe once (`runtime_versions = probe_runtime_versions(runtime) if runtime is not None else None`), pull author metadata defensively:

```python
    info_method = getattr(author, "info", None)
    author_info = info_method() if callable(info_method) else None
```

and extend the returned `AuthoringRun(...)` with `runtime=runtime`, `build_timeout_s=build_timeout`, `health_timeout_s=health_timeout`, `max_iterations=max_iterations`, `runtime_versions=runtime_versions`, `author_info=author_info`, `deployer_version=_deployer_version()`, `deployer_git_sha=_deployer_git_sha()`.

- [ ] **Step 7: CLI verify path records versions**

In `_cmd_verify` after a successful `verify(...)` call with a non-None runtime:

```python
    if runtime is not None:
        report.runtime_versions = probe_runtime_versions(runtime)
```

(import `probe_runtime_versions` from `deployer.runtime`).

- [ ] **Step 8: Run everything, format/lint/typecheck, commit**

Run: `uv run pytest && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`

```bash
git add src/deployer tests
git commit -m "feat: run-metadata hardening (runtime versions, author info, timeouts)"
```

Deliberately deferred vs the spec's 1.5 list (record in progress.md as backlog):
hadolint *version* recording (`hadolint_available` already marks runs
non-comparable, and `_check_hadolint` pins the version) and hashing the
prompt *renderer* (v1 hashes `SYSTEM_PROMPT` only; renderer changes also
show up as deployer_git_sha changes).

---

### Task 8: Full validation sweep + remote acceptance

**Files:**
- Modify: `.superpowers/sdd/progress.md` (record results)

- [ ] **Step 1: Full local sweep**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest && uv run pytest -m docker`
Expected: all clean/PASS on the local podman.

- [ ] **Step 2: Remote acceptance (needs Task 0 spike results + a reachable host)**

```sh
DEPLOYER_CONTAINER_TOOL=docker \
DEPLOYER_CONTAINER_HOST=ssh://user@host \
uv run pytest -m docker
```

Expected: same suite PASSES against the remote engine. Then one CLI acceptance run:

```sh
uv run deployer verify tests/fixtures/hello_service \
    --container-tool docker --container-host ssh://user@host
```

Expected: exit 0; `verify-report.json` contains `"runtime": {"tool": "docker", "host": "ssh://user@host", "host_source": "cli"}` and non-null `runtime_versions`.

- [ ] **Step 3: Negative acceptance**

```sh
uv run deployer verify tests/fixtures/hello_service --container-host tcp://h:2375; echo $?
```

Expected: `error: --container-host must be an ssh:// URL...` and exit `2`.

- [ ] **Step 4: Record in progress.md and commit**

```bash
git add .superpowers/sdd/progress.md
git commit -m "chore: record phase-1 remote acceptance results"
```

If no remote host is reachable yet, record Steps 2–3 as pending in progress.md — the PR can still go up with local validation, and remote acceptance completes before merge.

### Remote acceptance results (2026-07-22, post-merge)

Host: `ssh://user@host` ("homelab", LAN address redacted; Ubuntu 24.04, docker server
29.6.1; local client 29.6.2 via `brew install docker` — the dev machine is
podman-only). All steps PASSED:

- **Task 0 spike**: `DOCKER_HOST=ssh://…` version + `alpine:3.20 run` OK.
  Podman branch not exercised — no podman on the host; the
  podman-remote transport-marker watch item stays open.
- **Step 2**: 16 docker-marked tests (verify + corpus smoke) passed
  against the remote engine in 148s, unchanged. CLI acceptance: exit 0,
  report round-trips `runtime` (`host_source: "cli"`) and
  `runtime_versions` with **non-null platform** `linux/amd64` (the
  podman-machine null-platform limitation is absent on remote docker).
  Note: the verbatim command fails earlier with "Dockerfile not found" —
  the fixture ships `Dockerfile.good`; run it on a copy renamed to
  `Dockerfile`.
- **Step 3**: exact error + exit 2. Caveat: project-path validation runs
  before host validation, so the fixture-as-shipped hits the Dockerfile
  error (exit 1) first — cosmetic ordering.
- **Watch items**: `build --memory` accepted without warnings *because
  the remote daemon runs the legacy builder* (BuildKit disabled);
  BuildKit `--memory` behavior remains unexercised. Legacy-builder litter
  confirmed: the failed-RUN test leaves an intermediate container +
  dangling intermediate images that deployer's own rm/rmi cannot track —
  bench hosts want periodic `docker system prune`. Remote cleaned back to
  baseline after the run.
- **Local prereq gotcha**: a stale `~/.docker/config.json`
  (`credsStore: "desktop"` from a removed Docker Desktop) breaks pulls;
  worked around with a clean `DOCKER_CONFIG` dir.

Detailed record: `.superpowers/sdd/progress.md` (local ledger; this
section is the tracked summary — the ledger itself is gitignored, so
Step 4's verbatim `git add` does not apply).
