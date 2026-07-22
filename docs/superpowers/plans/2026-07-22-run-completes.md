# Run-completes Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `run` intent to `DeployTarget` so job images are verified to run to completion (exit 0, optional hidden stdout oracle), closing the bench blind spot where an inert `CMD ["python"]` passes build-only verification.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-22-run-completes-design.md`. `RunSpec` becomes the third runtime surface (`service | run | build-only`). A new `run_completes` check in `verify.py` runs the container in the foreground and classifies outcomes (narrow ENVIRONMENT rule). The stdout oracle is redacted from both the LLM prompt (`_context_blocks`) and every FAILED message (verifier-side `_redact_oracle`). Corpus: `no-build-system` becomes the sole job case.

**Tech Stack:** Python 3.12, pydantic v2, pytest (docker-marked tests need podman/docker), uv.

## Global Constraints

- Package management with `uv` only (never pip): `uv run pytest`, `uv run ruff format .`, `uv run ruff check . --fix`, `uv run pyrefly check`.
- Run `uv run pyrefly check` after every code change; fix resulting errors before committing.
- Type hints on all code; line length 88; docstrings on public APIs.
- Docker-dependent tests carry `pytest.mark.docker` (module-level `pytestmark`); unit tests must run without any container runtime.
- Branch `feature/run-completes` (already exists, spec committed). Never commit to `master`.
- The oracle string (`expect_stdout`) must never appear in any prompt or any `run_completes` failure message — this is a spec invariant, not a style choice.
- `corpus/**` is excluded from pyrefly; corpus project files need no type hints.

---

### Task 1: `RunSpec` model + mutual exclusion

**Files:**
- Modify: `src/deployer/models.py` (ServiceSpec is at ~line 14, DeployTarget at ~line 21)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `RunSpec(BaseModel)` with `expect_stdout: str | None = None`; `DeployTarget.run: RunSpec | None = None`; validation error when both `service` and `run` are set. Later tasks import `RunSpec` from `deployer.models`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_models.py` (it already imports `pytest`; extend the existing imports from `deployer.models` with `RunSpec` — keep import sorting, `ServiceSpec` and `DeployTarget` are already imported there; add `from pydantic import ValidationError` if not present):

```python
def test_run_spec_defaults_and_roundtrip() -> None:
    target = DeployTarget(run=RunSpec(expect_stdout="ok"))
    parsed = DeployTarget.model_validate_json(target.model_dump_json())
    assert parsed.run is not None
    assert parsed.run.expect_stdout == "ok"


def test_bare_run_spec_has_no_oracle() -> None:
    target = DeployTarget.model_validate_json('{"run": {}}')
    assert target.run is not None
    assert target.run.expect_stdout is None


def test_service_and_run_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        DeployTarget(
            service=ServiceSpec(port=8000), run=RunSpec(expect_stdout="x")
        )


def test_build_only_target_still_valid() -> None:
    target = DeployTarget()
    assert target.service is None
    assert target.run is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -k "run_spec or mutually or build_only" -v`
Expected: FAIL/ERROR with `ImportError: cannot import name 'RunSpec'`

- [ ] **Step 3: Implement `RunSpec` and the validator**

In `src/deployer/models.py`, after `ServiceSpec` add:

```python
class RunSpec(BaseModel):
    """Job intent: the container must run to completion successfully.

    `expect_stdout` is a verifier-side oracle (substring of stdout); it is
    never shown to the authoring model.
    """

    expect_stdout: str | None = None
```

Extend `DeployTarget`:

```python
class DeployTarget(BaseModel):
    """Declarative deploy intent: what is wanted, never how."""

    base_image: str | None = None
    service: ServiceSpec | None = None
    run: RunSpec | None = None
    env: dict[str, str] = Field(default_factory=dict)
    memory_limit: str = "512m"
    system_packages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _service_and_run_exclusive(self) -> "DeployTarget":
        if self.service is not None and self.run is not None:
            raise ValueError(
                "DeployTarget.service and DeployTarget.run are mutually "
                "exclusive: an artifact is a service or a job, not both"
            )
        return self
```

(`model_validator` is already imported in this module.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: all PASS (new and pre-existing)

- [ ] **Step 5: Format, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/models.py tests/test_models.py
git commit -m "feat: RunSpec job intent on DeployTarget (service|run exclusive)"
```

---

### Task 2: `run_completes` check in verify

**Files:**
- Modify: `src/deployer/verify.py` (add `_redact_oracle` + `_run_completes` after `_run_healthcheck` ~line 578; dispatch in `verify_docker` ~line 606)
- Test: Create `tests/test_verify_run.py` (unit, mocked `container_run` — NO docker marker)

**Interfaces:**
- Consumes: `RunSpec`, `DeployTarget.run` (Task 1); existing `container_run(runtime, args, **kwargs)`, `_with_command_feedback(message, runtime, tag)`, `_is_transport_failure(output)`, `_tail(text, lines=15)`, `CheckResult`, `FailureKind`.
- Produces: check id `"run_completes"` in `VerificationReport.results`; `_redact_oracle(message: str, marker: str | None) -> str`. Bench/authoring loop consume it via the report exactly like `run_healthcheck` (no changes needed there).

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_verify_run.py`:

```python
"""Unit matrix for the run_completes job check (mocked container runtime)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

import deployer.verify as verify_mod
from deployer.models import (
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    RunSpec,
)
from deployer.verify import _redact_oracle, _run_completes

RUNTIME = ContainerRuntime(tool="docker")
MARKER = "hello from job"
CMD_FEEDBACK = 'ENTRYPOINT null, CMD ["python"]'


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_container_run(
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    inspect_stdout: str = CMD_FEEDBACK,
) -> None:
    """Route the foreground `run` to `outcome`; keep inspect/rm working."""

    def fake(runtime: ContainerRuntime, args: list[str], **kwargs: Any) -> Any:
        if args[0] == "image":
            return _proc(0, stdout=inspect_stdout)
        if args[0] == "rm":
            return _proc(0)
        assert args[0] == "run"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(verify_mod, "container_run", fake)


def _target(marker: str | None = MARKER) -> DeployTarget:
    return DeployTarget(run=RunSpec(expect_stdout=marker))


def test_exit_zero_without_oracle_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout=""))
    result = _run_completes(_target(marker=None), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.PASSED


def test_exit_zero_with_marker_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout=f"start\n{MARKER}\n"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.PASSED


def test_marker_in_stderr_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout="", stderr=MARKER))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING


def test_inert_cmd_exit_zero_missing_marker_is_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout=""))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "container command" in result.message
    assert MARKER not in result.message


def test_nonzero_exit_is_authoring_with_output_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(
        monkeypatch, _proc(1, stderr="Traceback ...\nValueError: boom")
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "ValueError: boom" in result.message


def test_app_connection_refused_stays_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-case: app output must not trip broad ENVIRONMENT markers."""
    _patch_container_run(
        monkeypatch,
        _proc(1, stderr="ConnectionRefusedError: connection refused"),
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.AUTHORING


def test_cli_transport_failure_is_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(
        monkeypatch,
        _proc(125, stderr="error during connect: ssh tunnel died"),
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_exit_125_without_transport_marker_stays_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(125, stderr="invalid memory limit"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.AUTHORING


def test_timeout_is_authoring_and_names_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(
        monkeypatch, subprocess.TimeoutExpired(cmd="run", timeout=30)
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "did not exit within" in result.message
    assert "container command" in result.message


def test_oserror_is_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_container_run(monkeypatch, OSError("broken pipe"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_marker_printed_then_crash_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle leak path 1: program prints the marker, then fails."""
    _patch_container_run(
        monkeypatch, _proc(1, stdout=f"{MARKER}\n", stderr="boom")
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert MARKER not in result.message
    assert "<redacted>" in result.message


def test_marker_in_command_feedback_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle leak path 2: an echo-CMD carries the marker into feedback."""
    _patch_container_run(
        monkeypatch,
        _proc(0, stdout="wrong output"),
        inspect_stdout=f'ENTRYPOINT null, CMD ["echo", "{MARKER}"]',
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert MARKER not in result.message


def test_redact_oracle_none_marker_is_noop() -> None:
    assert _redact_oracle("msg", None) == "msg"
    assert _redact_oracle("has secret", "secret") == "has <redacted>"


def test_verify_docker_dispatches_run_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a passed build, a run target triggers run_completes (not
    run_healthcheck)."""

    def fake(runtime: ContainerRuntime, args: list[str], **kwargs: Any) -> Any:
        if args[0] == "build":
            return _proc(0)
        if args[0] == "image":
            return _proc(0, stdout="123" if "Size" in args[3] else CMD_FEEDBACK)
        if args[0] == "run":
            return _proc(0, stdout=MARKER)
        return _proc(0)

    monkeypatch.setattr(verify_mod, "container_run", fake)
    results, _ = verify_mod.verify_docker(
        "FROM python:3.12-slim", tmp_path, _target(), RUNTIME
    )
    assert [r.check_id for r in results] == ["build", "run_completes"]
    assert all(r.status is CheckStatus.PASSED for r in results)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_run.py -v`
Expected: FAIL with `ImportError: cannot import name '_redact_oracle'`

- [ ] **Step 3: Implement `_redact_oracle` and `_run_completes`**

In `src/deployer/verify.py`, after `_run_healthcheck` (before `verify_docker`) add:

```python
def _redact_oracle(message: str, marker: str | None) -> str:
    """Strip the run-intent stdout oracle from verifier text.

    Prompt-side redaction alone cannot stop a program that prints the
    marker and then crashes, or an echo-CMD that carries it into command
    feedback — so every FAILED run_completes message passes through here.
    """
    if not marker:
        return message
    return message.replace(marker, "<redacted>")


def _run_completes(
    target: DeployTarget, runtime: ContainerRuntime, tag: str, timeout: int
) -> CheckResult:
    """Job intent: the image's default command must exit 0 within timeout.

    With an `expect_stdout` oracle, stdout must also contain the marker.
    ENVIRONMENT is deliberately narrow: a foreground run interleaves app
    and CLI output, so only an explicit CLI failure (OSError, or exit
    125/126 plus transport markers) counts — an app that prints
    "connection refused" and exits non-zero stays AUTHORING.
    """
    assert target.run is not None
    container = f"deployer-check-{uuid.uuid4().hex[:8]}"
    marker = target.run.expect_stdout

    def _failed(kind: FailureKind, message: str) -> CheckResult:
        if kind is FailureKind.AUTHORING:
            message = _with_command_feedback(message, runtime, tag)
        return CheckResult(
            check_id="run_completes",
            status=CheckStatus.FAILED,
            failure_kind=kind,
            message=_redact_oracle(message, marker),
        )

    try:
        proc = container_run(
            runtime,
            [
                "run",
                "--name",
                container,
                "--network=none",
                "--memory",
                target.memory_limit,
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _failed(
            FailureKind.AUTHORING,
            f"container did not exit within {timeout}s (a run intent means "
            "a job: the default command must run to completion)",
        )
    except OSError as exc:
        return _failed(
            FailureKind.ENVIRONMENT,
            f"container runtime command failed: {exc}",
        )
    finally:
        try:
            container_run(
                runtime, ["rm", "-f", container], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort cleanup; must never clobber the return value

    if proc.returncode == 0:
        if marker is not None and marker not in proc.stdout:
            return _failed(
                FailureKind.AUTHORING,
                "container exited 0 but stdout did not contain the expected "
                f"output\nstdout tail:\n{_tail(proc.stdout)}",
            )
        return CheckResult(check_id="run_completes", status=CheckStatus.PASSED)

    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode in (125, 126) and _is_transport_failure(output):
        return _failed(
            FailureKind.ENVIRONMENT,
            f"container runtime failed to start the job: {_tail(output, 3)}",
        )
    return _failed(
        FailureKind.AUTHORING,
        f"container exited {proc.returncode}\noutput tail:\n{_tail(output)}",
    )
```

In `verify_docker`, replace the service-only dispatch:

```python
            if target.service is not None:
                results.append(_run_healthcheck(target, runtime, tag, health_timeout))
```

with:

```python
            if target.service is not None:
                results.append(_run_healthcheck(target, runtime, tag, health_timeout))
            elif target.run is not None:
                results.append(_run_completes(target, runtime, tag, health_timeout))
```

Also update the `verify_docker` docstring first line to: `"""L2: real sandboxed build; then service healthcheck or job run-completes."""` (keep the loopback-probe paragraph).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_run.py -v`
Expected: all PASS

- [ ] **Step 5: Run the whole unit suite, format, typecheck, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/verify.py tests/test_verify_run.py
git commit -m "feat: run_completes check for job intents (narrow ENV, oracle redaction)"
```

---

### Task 3: Prompt redaction + SYSTEM_PROMPT job rule

**Files:**
- Modify: `src/deployer/llm.py` (`SYSTEM_PROMPT` ~line 14, `_context_blocks` ~line 50)
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `DeployTarget.run` (Task 1).
- Produces: `_context_blocks` renders the intent with `"run": {}` when a run intent exists (oracle stripped); `SYSTEM_PROMPT` contains the job rule. `AnthropicAuthor.info()` picks up the new prompt sha automatically (no change needed).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm.py` (extend its `deployer.models` imports with `RunSpec` as needed; `_context_blocks`, `ProjectFacts`, `DeployTarget` import style — follow the file's existing imports):

```python
def test_run_intent_visible_but_oracle_redacted() -> None:
    target = DeployTarget(run=RunSpec(expect_stdout="secret-oracle-string"))
    rendered = _context_blocks(ProjectFacts(), target)
    assert '"run": {}' in rendered
    assert "secret-oracle-string" not in rendered


def test_build_only_target_renders_null_run() -> None:
    rendered = _context_blocks(ProjectFacts(), DeployTarget())
    assert '"run": null' in rendered


def test_service_target_rendering_unchanged() -> None:
    target = DeployTarget(service=ServiceSpec(port=8000))
    rendered = _context_blocks(ProjectFacts(), target)
    assert '"port": 8000' in rendered


def test_system_prompt_states_job_rule() -> None:
    assert "run" in SYSTEM_PROMPT and "job" in SYSTEM_PROMPT
    assert "exit 0" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -v`
Expected: new tests FAIL (`'"run": {}' in rendered` is False; job-rule assertion fails)

- [ ] **Step 3: Implement redaction and the prompt rule**

In `src/deployer/llm.py` add `import json` to the imports, then add before `_context_blocks`:

```python
def _intent_json(target: DeployTarget) -> str:
    """Deploy intent for the prompt, with the run oracle redacted.

    The model may see that a run intent exists — never the expected
    stdout, or `CMD ["echo", ...]` would game the check.
    """
    data = target.model_dump()
    if data.get("run") is not None:
        data["run"] = {}
    return json.dumps(data, indent=2)
```

In `_context_blocks`, replace:

```python
        f"Deploy intent:\n{target.model_dump_json(indent=2)}",
```

with:

```python
        f"Deploy intent:\n{_intent_json(target)}",
```

In `SYSTEM_PROMPT`, insert after the `script_entrypoint` rule (the bullet ending `...entrypoints is non-empty it wins over script_entrypoint.`):

```text
- A "run" deploy intent means a job image: the CMD must execute the
  project's entrypoint (per the rules above) and exit 0 when the work
  completes. The container's stdout is checked against a held-back
  oracle you cannot see, so the only winning strategy is to actually
  run the project's code — never fake output with echo, never leave a
  bare interpreter, never author a long-running server for a run
  intent.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: all PASS

- [ ] **Step 5: Format, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/llm.py tests/test_llm.py
git commit -m "feat: redact run oracle from prompts; SYSTEM_PROMPT job rule"
```

---

### Task 4: Corpus job case + user-facing docs

**Files:**
- Modify: `corpus/synthetic/no-build-system/project/main.py`
- Create: `corpus/synthetic/no-build-system/target.json`
- Modify: `src/deployer/cli.py` (`--health-timeout` help, ~line 71 — the option is added once in a shared helper, so one edit covers both subcommands; verify with grep)
- Modify: `README.md` (~lines 26-28)
- Test: `tests/test_bench.py`

**Interfaces:**
- Consumes: `RunSpec` (Task 1), `load_corpus(corpus_root, pattern="*") -> list[BenchCase]` from `deployer.bench` (existing).
- Produces: the `no-build-system` case carries a run intent with oracle `"hello from no-build-system"`; its project exposes a `__main__` guard so `script_entrypoint` fires.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bench.py` (it already imports `load_corpus`; add `Path` usage consistent with the file):

```python
def test_no_build_system_is_a_job_case() -> None:
    corpus = Path(__file__).parent.parent / "corpus" / "synthetic"
    case = next(
        c for c in load_corpus(corpus) if c.name == "no-build-system"
    )
    assert case.target.service is None
    assert case.target.run is not None
    assert case.target.run.expect_stdout == "hello from no-build-system"


def test_no_build_system_main_has_guard() -> None:
    from deployer.facts import analyze_project

    project = (
        Path(__file__).parent.parent
        / "corpus"
        / "synthetic"
        / "no-build-system"
        / "project"
    )
    assert analyze_project(project).script_entrypoint == "main.py"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bench.py -k "no_build_system" -v`
Expected: both FAIL (`case.target.run is None`; `script_entrypoint is None`)

- [ ] **Step 3: Update the corpus case**

Overwrite `corpus/synthetic/no-build-system/project/main.py`:

```python
"""Script-style project: no [build-system], must not be pip-installed."""

if __name__ == "__main__":
    print("hello from no-build-system")
```

Create `corpus/synthetic/no-build-system/target.json`:

```json
{"run": {"expect_stdout": "hello from no-build-system"}}
```

Leave `fixture.Dockerfile` alone — its `CMD ["uv", "run", "--no-sync", "python", "main.py"]` still prints the marker under the guard.

- [ ] **Step 4: Update CLI help and README**

In `src/deployer/cli.py`, replace the `--health-timeout` help string:

```python
        help=(
            "seconds allowed for runtime checks (service healthcheck or "
            "run intent); ignored for build-only targets"
        ),
```

In `README.md`, replace the comment block:

```text
# verify checks <project-path>/Dockerfile; --health-timeout is ignored for
# non-service targets. Slow source builds (e.g. llama-cpp-python) need
# --build-timeout well above the 600s default.
```

with:

```text
# verify checks <project-path>/Dockerfile; --health-timeout bounds runtime
# checks (service healthcheck or run intent) and is ignored for build-only
# targets. Slow source builds (e.g. llama-cpp-python) need
# --build-timeout well above the 600s default.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_bench.py tests/test_cli.py tests/test_facts.py -v`
Expected: all PASS

- [ ] **Step 6: Format, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add corpus/synthetic/no-build-system src/deployer/cli.py README.md tests/test_bench.py
git commit -m "feat: no-build-system becomes the job corpus case; timeout docs"
```

---

### Task 5: Docker-marked acceptance tests + full sweep

**Files:**
- Modify: `tests/test_verify_docker.py`
- No production code changes expected.

**Interfaces:**
- Consumes: everything above; corpus job case files (Task 4); existing `runtime` fixture and `_by_id` helper in `tests/test_verify_docker.py`.

- [ ] **Step 1: Write the docker-marked tests**

Append to `tests/test_verify_docker.py` (extend the module's `deployer.models` import with `RunSpec`; `Path` is already imported):

```python
CORPUS_JOB = (
    Path(__file__).parent.parent / "corpus" / "synthetic" / "no-build-system"
)
JOB_TARGET = DeployTarget(run=RunSpec(expect_stdout="hello from no-build-system"))


def test_job_fixture_passes_run_completes(runtime: ContainerRuntime) -> None:
    dockerfile = (CORPUS_JOB / "fixture.Dockerfile").read_text()
    report = verify(dockerfile, CORPUS_JOB / "project", JOB_TARGET, runtime)
    assert _by_id(report, "build").status is CheckStatus.PASSED
    assert _by_id(report, "run_completes").status is CheckStatus.PASSED
    assert report.passed


def test_inert_cmd_fails_run_completes_without_leaking_oracle(
    runtime: ContainerRuntime,
) -> None:
    """The motivating blind spot: bare `python` exits 0 silently — only
    the hidden stdout oracle catches it, and the failure names the CMD
    but never the oracle."""
    dockerfile = (
        (CORPUS_JOB / "fixture.Dockerfile")
        .read_text()
        .replace(
            'CMD ["uv", "run", "--no-sync", "python", "main.py"]',
            'CMD ["python"]',
        )
    )
    report = verify(dockerfile, CORPUS_JOB / "project", JOB_TARGET, runtime)
    check = _by_id(report, "run_completes")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "container command" in check.message
    assert "hello from no-build-system" not in check.message
```

- [ ] **Step 2: Run the docker suite**

Run: `uv run pytest -m docker -v`
Expected: all PASS, including the two new tests and the corpus smoke
(`tests/test_corpus.py` now exercises `run_completes` for
no-build-system via its fixture Dockerfile).

- [ ] **Step 3: Fixture bench acceptance**

Run: `uv run deployer bench run --author fixture --label run-completes-fixture`
Expected: 6/6 matched, success rate 1.0 (fixture Dockerfiles pass the new
check).

- [ ] **Step 4: Full sweep and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
uv run pytest
git add tests/test_verify_docker.py
git commit -m "test: docker-marked run_completes acceptance (job fixture + inert CMD)"
```

- [ ] **Step 5: Record manual follow-ups (not automatable here)**

Remaining acceptance from the spec, to run manually before/at PR time:
manual `--author anthropic` research run → 6/6 expected → `bench promote`
(golden gains the `run_completes` check — change of measured subject, not
a regression) → `bench compare <run> golden` clean. Record the outcome in
`.superpowers/sdd/progress.md` (local ledger).
