# ATP smoke-test seam Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove one executable vertical seam — a deployer-authored Dockerfile is built by L2, and an external ATP suite runs against that image and reports back into deployer's verification report.

**Architecture:** ATP is an optional external tool driven by subprocess, exactly like `hadolint` and `actionlint`: found with `shutil.which`, pinned by version, `SKIPPED` when absent. For a target declaring `smoke`, the L2 branch is `build → ATP smoke` — ATP is itself the runtime check, so `_run_completes` is not invoked. The image stays alive for the length of the run and is removed in the existing `finally`.

**Tech Stack:** Python 3.12, pydantic v2, pytest, uv. External: `atp` CLI 2.1.0 (not a Python dependency of this repo).

**Spec:** `docs/superpowers/specs/2026-09-06-atp-smoke-seam-design.md`

## Global Constraints

- Package management is `uv` only. Never `pip`. **Do not add `atp` as a project dependency** — it is an external binary, like hadolint.
- Line length 88. Type hints on all code. Public APIs get docstrings.
- After every change: `uv run ruff format .`, `uv run ruff check . --fix`, `uv run pyrefly check`.
- Tests: `uv run pytest` (markers `docker` and `llm` are excluded by `addopts`). Container tests go behind `@pytest.mark.docker`.
- `ATP_VERSION = "2.1.0"` — the version pinned by `ai-orchestrators-workspace/workspace-manifest.toml:49`.
- `ATP_REPORT_FORMAT = "1.0"` — ATP's `JSONReporter.FORMAT_VERSION`.
- The check id is exactly `atp_smoke`.
- Non-goals, enforced by the plan: no ATP suite authoring, no http/service adapter path, no remote runtime support, no new `bench compare` axis.
- `schema_version` already exists on the reports (PR #59, merge `bcfeebc`). Fields added here are additive within v1 and must **not** bump it.

---

### Task 1: `SmokeSpec` and the `smoke` target intent

**Files:**
- Modify: `src/deployer/models.py` (add `SmokeSpec` next to `CISpec`; add `smoke` field and a validator to `DeployTarget`)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SmokeSpec(suite: str, timeout_s: int = 300)`; `DeployTarget.smoke: SmokeSpec | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_models.py`:

```python
def test_smoke_target_requires_a_run_intent() -> None:
    """A smoke target is a job; ATP drives it, so the job intent must be declared."""
    with pytest.raises(ValidationError, match="run"):
        DeployTarget(smoke={"suite": "suite.yaml"})


def test_smoke_target_rejects_a_service_intent() -> None:
    """The http-adapter path is a separate seam with a different lifecycle."""
    with pytest.raises(ValidationError, match="service"):
        DeployTarget(
            smoke={"suite": "suite.yaml"},
            service={"port": 8000},
        )


def test_smoke_spec_defaults_and_rejects_unknown_keys() -> None:
    target = DeployTarget(smoke={"suite": "suite.yaml"}, run={})
    assert target.smoke is not None
    assert target.smoke.suite == "suite.yaml"
    assert target.smoke.timeout_s == 300
    with pytest.raises(ValidationError):
        DeployTarget(smoke={"suite": "s.yaml", "kind": "x"}, run={})


def test_smoke_spec_rejects_an_empty_suite_path() -> None:
    with pytest.raises(ValidationError):
        DeployTarget(smoke={"suite": ""}, run={})
```

Check the imports at the top of `tests/test_models.py`; add `DeployTarget` and `ValidationError` if they are not already imported.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -q -k smoke`
Expected: FAIL — `DeployTarget` has no field `smoke` (pydantic raises on the unexpected keyword or silently ignores it, so the `pytest.raises` blocks fail with "DID NOT RAISE").

- [ ] **Step 3: Write the implementation**

In `src/deployer/models.py`, directly after `class CISpec`:

```python
class SmokeSpec(BaseModel):
    """Request an ATP smoke test of the built image. Presence is the request.

    `suite` is a path to an ATP suite YAML, resolved relative to the
    `target.json` that declared it — never to the current directory and never
    to the project directory, so a target document stays portable.
    """

    model_config = ConfigDict(extra="forbid")

    suite: str = Field(min_length=1)
    timeout_s: int = Field(default=300, gt=0)
```

In `class DeployTarget`, add the field after `ci`:

```python
    smoke: SmokeSpec | None = None
```

And add a validator next to the other `DeployTarget` validators:

```python
    @model_validator(mode="after")
    def _smoke_is_a_job_intent(self) -> "DeployTarget":
        """ATP's container adapter talks to a job over stdin/stdout.

        `service` is refused rather than supported: the http adapter needs a
        published port, a readiness wait and guaranteed teardown — a separate
        seam. Loosening this later is backward compatible.
        """
        if self.smoke is None:
            return self
        if self.service is not None:
            raise ValueError(
                "DeployTarget.smoke with a service intent is unsupported: "
                "the ATP container adapter drives a job over stdin/stdout"
            )
        if self.run is None:
            raise ValueError(
                "DeployTarget.smoke requires a run intent: the target must "
                "declare that it is a job"
            )
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -q -k smoke` → PASS
Then: `uv run pytest -q` → all pass.

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/models.py tests/test_models.py
git commit -m "feat(models): SmokeSpec and the smoke target intent"
```

---

### Task 2: Report fields — `atp_available` and the built-image reference

**Files:**
- Modify: `src/deployer/models.py` (add `BuiltImage`; add two fields to `VerificationReport`)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `ContainerRuntime` (already in `models.py`).
- Produces: `BuiltImage(tag: str, runtime: ContainerRuntime, lifecycle: Literal["ephemeral"], cleanup_status: Literal["removed", "failed", "not_attempted"])`; `VerificationReport.atp_available: bool`; `VerificationReport.built_image: BuiltImage | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_built_image_defaults_to_unattempted_cleanup() -> None:
    """cleanup_status records what happened, so it cannot default to success.

    `rmi` is best-effort and may fail; a hardcoded "removed" would be a claim
    the code never checks.
    """
    from deployer.models import BuiltImage

    image = BuiltImage(tag="localhost/x", runtime=ContainerRuntime(tool="docker"))
    assert image.lifecycle == "ephemeral"
    assert image.cleanup_status == "not_attempted"


def test_verification_report_defaults_have_no_atp_and_no_image() -> None:
    report = VerificationReport()
    assert report.atp_available is False
    assert report.built_image is None
    assert report.schema_version == SCHEMA_VERSION
```

Add `SCHEMA_VERSION`, `ContainerRuntime` and `VerificationReport` to the imports of `tests/test_models.py` if missing.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -q -k "built_image or atp"`
Expected: FAIL — `ImportError: cannot import name 'BuiltImage'`.

- [ ] **Step 3: Write the implementation**

In `src/deployer/models.py`, before `class VerificationReport`:

```python
class BuiltImage(BaseModel):
    """The image L2 built, and what became of it.

    `lifecycle` is the contract a consumer reads: `ephemeral` means the image
    does not outlive the run, so the reference answers "what was tested", not
    "what is on your machine".
    """

    tag: str
    runtime: ContainerRuntime
    lifecycle: Literal["ephemeral"] = "ephemeral"
    cleanup_status: Literal["removed", "failed", "not_attempted"] = "not_attempted"
```

In `class VerificationReport`, add beside the other `*_available` flags:

```python
    atp_available: bool = False
    built_image: BuiltImage | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -q` → PASS
Then: `uv run pytest -q` → all pass (the fields are additive; nothing else reads them yet).

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/models.py tests/test_models.py
git commit -m "feat(models): built-image reference and atp_available on the report"
```

---

### Task 3: `_atp_verdict` — the verdict table as a pure function

Keeping the mapping separate from the subprocess call is what makes every row of the spec's §5 table testable without ATP installed.

**Files:**
- Modify: `src/deployer/verify.py` (add `ATP_VERSION`, `ATP_REPORT_FORMAT`, `_atp_verdict`)
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `CheckResult`, `CheckStatus`, `FailureKind` (already imported in `verify.py`).
- Produces: `_atp_verdict(report_path: Path, returncode: int) -> CheckResult`; constants `ATP_VERSION`, `ATP_REPORT_FORMAT`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py`:

```python
def _atp_report(tmp_path: Path, **summary: object) -> Path:
    """Write an ATP JSON report with the given summary fields."""
    import json

    path = tmp_path / "atp-report.json"
    body = {"version": "1.0", "summary": {"total_tests": 1, **summary}}
    path.write_text(json.dumps(body))
    return path


def test_atp_verdict_passes_when_report_and_exit_code_agree(tmp_path: Path) -> None:
    from deployer.verify import _atp_verdict

    result = _atp_verdict(_atp_report(tmp_path, success=True), 0)
    assert result.check_id == "atp_smoke"
    assert result.status is CheckStatus.PASSED


def test_atp_verdict_failed_assertions_are_authoring(tmp_path: Path) -> None:
    """Packaging is what deployer controls, so a failed assertion is on us."""
    from deployer.verify import _atp_verdict

    report = _atp_report(tmp_path, success=False, failed_tests=1)
    result = _atp_verdict(report, 1)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING


def test_atp_verdict_empty_suite_is_environment(tmp_path: Path) -> None:
    """ATP computes success as passed == total, so 0 == 0 reports success.

    A suite that ran nothing says nothing about the artifact, and must never
    be the strongest signal in the seam.
    """
    from deployer.verify import _atp_verdict

    report = _atp_report(tmp_path, total_tests=0, success=True)
    result = _atp_verdict(report, 0)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_atp_verdict_missing_report_is_environment(tmp_path: Path) -> None:
    from deployer.verify import _atp_verdict

    result = _atp_verdict(tmp_path / "absent.json", 0)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_atp_verdict_unparseable_report_is_environment(tmp_path: Path) -> None:
    from deployer.verify import _atp_verdict

    path = tmp_path / "atp-report.json"
    path.write_text("{not json")
    result = _atp_verdict(path, 0)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_atp_verdict_unknown_report_version_is_environment(tmp_path: Path) -> None:
    import json

    from deployer.verify import _atp_verdict

    path = tmp_path / "atp-report.json"
    path.write_text(json.dumps({"version": "9.9", "summary": {"total_tests": 1}}))
    result = _atp_verdict(path, 0)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_atp_verdict_disagreement_between_report_and_exit_code(
    tmp_path: Path,
) -> None:
    """The JSON is authoritative; the exit code is a consistency check."""
    from deployer.verify import _atp_verdict

    result = _atp_verdict(_atp_report(tmp_path, success=True), 1)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT
    assert "disagree" in result.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -q -k atp_verdict`
Expected: FAIL — `ImportError: cannot import name '_atp_verdict'`.

- [ ] **Step 3: Write the implementation**

In `src/deployer/verify.py`, beside `HADOLINT_VERSION` / `ACTIONLINT_VERSION` (around line 36):

```python
ATP_VERSION = "2.1.0"
ATP_REPORT_FORMAT = "1.0"
```

Add `import json` to the imports if it is not already there. Then add the function near the other check helpers:

```python
def _atp_env_failure(message: str) -> CheckResult:
    """An ATP outcome that says nothing about the authored artifact."""
    return CheckResult(
        check_id="atp_smoke",
        status=CheckStatus.FAILED,
        failure_kind=FailureKind.ENVIRONMENT,
        message=message,
    )


def _atp_verdict(report_path: Path, returncode: int) -> CheckResult:
    """Map an ATP run onto a check result; the JSON report is authoritative.

    The exit code is only a consistency check: when the two disagree, neither
    is trusted and the run is environmental. A suite that ran zero tests is
    rejected because ATP computes `summary.success` as
    `passed_tests == total_tests`, so an empty suite reports success by
    arithmetic — the seam's strongest signal would become its cheapest false
    positive.
    """
    if not report_path.is_file():
        return _atp_env_failure(f"atp wrote no JSON report (exit {returncode})")
    try:
        document = json.loads(report_path.read_text())
    except (OSError, ValueError) as exc:
        return _atp_env_failure(f"atp report unreadable: {exc}")
    if not isinstance(document, dict):
        return _atp_env_failure("atp report is not a JSON object")
    version = document.get("version")
    if version != ATP_REPORT_FORMAT:
        return _atp_env_failure(
            f"unsupported atp report format {version!r} "
            f"(this deployer reads {ATP_REPORT_FORMAT})"
        )
    summary = document.get("summary")
    if not isinstance(summary, dict):
        return _atp_env_failure("atp report has no summary")
    total = summary.get("total_tests")
    if not isinstance(total, int) or total < 1:
        return _atp_env_failure(
            "atp suite ran no tests; an empty suite reports success by "
            "arithmetic and proves nothing about the image"
        )
    success = summary.get("success")
    if success is True and returncode == 0:
        return CheckResult(check_id="atp_smoke", status=CheckStatus.PASSED)
    if success is False and returncode != 0:
        failed = summary.get("failed_tests", "some")
        return CheckResult(
            check_id="atp_smoke",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=f"{failed} of {total} ATP test(s) failed against the built image",
        )
    return _atp_env_failure(
        f"atp report and exit code disagree: success={success!r}, exit {returncode}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_static.py -q -k atp_verdict` → PASS (7 tests)
Then: `uv run pytest -q` → all pass.

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat(verify): ATP verdict mapping, JSON authoritative"
```

---

### Task 4: `_check_atp_smoke` — the subprocess wrapper

**Files:**
- Modify: `src/deployer/verify.py`
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `_atp_verdict`, `ATP_VERSION` (Task 3); `ContainerRuntime` (`models.py`).
- Produces: `_check_atp_smoke(suite: Path, runtime: ContainerRuntime, tag: str, timeout: int) -> tuple[CheckResult, bool]` — the bool is `atp_available`, matching `_check_actionlint`'s shape.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py`:

```python
def test_atp_smoke_skips_when_binary_absent(tmp_path: Path, monkeypatch) -> None:
    from deployer.verify import _check_atp_smoke

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    result, available = _check_atp_smoke(
        tmp_path / "suite.yaml", ContainerRuntime(tool="docker"), "localhost/x", 300
    )
    assert result.status is CheckStatus.SKIPPED
    assert available is False
    assert "non-comparable" in result.message


def test_atp_smoke_version_mismatch_skips_without_running(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from deployer.verify import _check_atp_smoke

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/atp")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="atp 9.9.9", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    result, available = _check_atp_smoke(
        tmp_path / "suite.yaml", ContainerRuntime(tool="docker"), "localhost/x", 300
    )
    assert result.status is CheckStatus.SKIPPED
    assert available is False
    assert len(calls) == 1  # only --version; the suite never ran


def test_atp_smoke_passes_runtime_and_tag_explicitly(
    tmp_path: Path, monkeypatch
) -> None:
    """ATP's auto-detection prefers podman, so the runtime we built with must
    be named explicitly, and the tag must be fully qualified."""
    import json
    import subprocess

    from deployer.verify import ATP_VERSION, _check_atp_smoke

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/atp")
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=ATP_VERSION, stderr="")
        seen.append(cmd)
        out = cmd[cmd.index("--output-file") + 1]
        Path(out).write_text(
            json.dumps({"version": "1.0", "summary": {"total_tests": 1, "success": True}})
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    suite = tmp_path / "suite.yaml"
    suite.write_text("test_suite: x\n")
    result, available = _check_atp_smoke(
        suite, ContainerRuntime(tool="docker"), "localhost/deployer-verify-abc", 300
    )

    assert result.status is CheckStatus.PASSED
    assert available is True
    command = seen[0]
    assert "image=localhost/deployer-verify-abc" in command
    assert "runtime=docker" in command
    assert "--no-save" in command
    assert str(suite) in command


def test_atp_smoke_timeout_is_environment(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from deployer.verify import ATP_VERSION, _check_atp_smoke

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/atp")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=ATP_VERSION, stderr="")
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    result, available = _check_atp_smoke(
        tmp_path / "suite.yaml", ContainerRuntime(tool="docker"), "localhost/x", 300
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT
    assert available is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -q -k atp_smoke`
Expected: FAIL — `ImportError: cannot import name '_check_atp_smoke'`.

- [ ] **Step 3: Write the implementation**

In `src/deployer/verify.py`:

```python
def _check_atp_smoke(
    suite: Path,
    runtime: ContainerRuntime,
    tag: str,
    timeout: int,
) -> tuple[CheckResult, bool]:
    """Run an ATP suite against the built image; (result, atp_available).

    The runtime is named explicitly because ATP's `auto` detection prefers
    podman when both are installed, which would look for a docker-built image
    in the wrong engine. `--no-save` plus a temporary cwd keep an external
    verification step from writing into the operator's dashboard database and
    `.atp-runs/checkpoints/`.
    """
    binary = shutil.which("atp")
    if binary is None:
        return (
            CheckResult(
                check_id="atp_smoke",
                status=CheckStatus.SKIPPED,
                message=f"atp {ATP_VERSION} not installed; run is non-comparable",
            ),
            False,
        )
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (_atp_env_failure(f"atp --version failed: {exc}"), False)
    if ATP_VERSION not in version:
        first = version.strip().splitlines()[0] if version.strip() else "?"
        return (
            CheckResult(
                check_id="atp_smoke",
                status=CheckStatus.SKIPPED,
                message=(
                    f"atp version mismatch (want {ATP_VERSION}, got: {first}); "
                    "run is non-comparable"
                ),
            ),
            False,
        )
    with tempfile.TemporaryDirectory(prefix="deployer-atp-") as tmp:
        report_path = Path(tmp) / "atp-report.json"
        try:
            proc = subprocess.run(
                [
                    binary,
                    "test",
                    str(suite),
                    "--adapter",
                    "container",
                    "--adapter-config",
                    f"image={tag}",
                    "--adapter-config",
                    f"runtime={runtime.tool}",
                    "--output",
                    "json",
                    "--output-file",
                    str(report_path),
                    "--no-save",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return (_atp_env_failure(f"atp timed out after {timeout}s"), True)
        except OSError as exc:
            return (_atp_env_failure(f"running atp failed: {exc}"), True)
        return (_atp_verdict(report_path, proc.returncode), True)
```

`tempfile` is already imported in `verify.py`; confirm with `grep -n "^import tempfile" src/deployer/verify.py` and add it if missing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_static.py -q -k atp_smoke` → PASS (4 tests)
Then: `uv run pytest -q` → all pass.

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat(verify): run an ATP suite as an optional external tool"
```

---

### Task 5: `verify_docker` — qualified tag, the smoke branch, cleanup status

**Files:**
- Modify: `src/deployer/verify.py:1437-1473` (`verify_docker`)
- Test: `tests/test_verify_docker.py`

**Interfaces:**
- Consumes: `_check_atp_smoke` (Task 4); `BuiltImage` (Task 2).
- Produces: `verify_docker(..., smoke_suite: Path | None = None) -> tuple[list[CheckResult], int | None, BuiltImage, bool]` — results, image size, the built-image reference, and `atp_available`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_docker.py` (match the file's existing style for faking `container_run`; the fakes below use `monkeypatch` on `deployer.verify.container_run`):

```python
def test_smoke_target_does_not_run_the_job_itself(monkeypatch, tmp_path) -> None:
    """ATP is the runtime check for a smoke target.

    `_run_completes` starts the container with no stdin at all, and a correct
    ATP agent reading stdin would get EOF; running it first would fail a
    healthy image.
    """
    from deployer.models import DeployTarget
    from deployer.verify import verify_docker

    called: list[str] = []
    monkeypatch.setattr(
        "deployer.verify._run_completes",
        lambda *a, **k: called.append("run") or CheckResult(
            check_id="run_completes", status=CheckStatus.PASSED
        ),
    )
    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr("deployer.verify.container_run", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: (
            CheckResult(check_id="atp_smoke", status=CheckStatus.PASSED),
            True,
        ),
    )
    target = DeployTarget(run={}, smoke={"suite": "s.yaml"})

    results, _size, image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        target,
        ContainerRuntime(tool="docker"),
        smoke_suite=tmp_path / "s.yaml",
    )

    assert called == []  # the job check never ran
    assert [r.check_id for r in results] == ["build", "atp_smoke"]
    assert available is True
    assert image.tag.startswith("localhost/deployer-verify-")


def test_built_image_records_a_failed_cleanup(monkeypatch, tmp_path) -> None:
    """rmi is best-effort, so cleanup_status must report what happened."""
    from deployer.models import DeployTarget
    from deployer.verify import verify_docker

    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)

    def boom(*a, **k):
        raise OSError("no daemon")

    monkeypatch.setattr("deployer.verify.container_run", boom)

    _results, _size, image, _available = verify_docker(
        "FROM x:1\n", tmp_path, DeployTarget(), ContainerRuntime(tool="docker")
    )

    assert image.cleanup_status == "failed"


def test_smoke_on_a_remote_runtime_is_skipped(monkeypatch, tmp_path) -> None:
    """ATP's container adapter has no remote-host support, so it must not
    silently test whatever image happens to be local."""
    from deployer.models import DeployTarget
    from deployer.verify import verify_docker

    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr("deployer.verify.container_run", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: pytest.fail("ATP must not run against a remote build"),
    )
    remote = ContainerRuntime(tool="docker", host="ssh://box", host_source="cli")

    results, _size, _image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        DeployTarget(run={}, smoke={"suite": "s.yaml"}),
        remote,
        smoke_suite=tmp_path / "s.yaml",
    )

    smoke = [r for r in results if r.check_id == "atp_smoke"][0]
    assert smoke.status is CheckStatus.SKIPPED
    assert "remote" in smoke.message
    assert available is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_docker.py -q -k "smoke or cleanup"`
Expected: FAIL — `verify_docker() got an unexpected keyword argument 'smoke_suite'` and a 2-tuple unpack error.

- [ ] **Step 3: Write the implementation**

Replace the body of `verify_docker`:

```python
def verify_docker(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
    smoke_suite: Path | None = None,
) -> tuple[list[CheckResult], int | None, BuiltImage, bool]:
    """L2: real sandboxed build; then service healthcheck, ATP smoke, or job run.

    The healthcheck probes over the container's loopback via `exec python -c`,
    so `--network=none` still works. This assumes a Python base image — true
    for every artifact this MVP authors.

    For a `smoke` target ATP is the runtime check and `_run_completes` is not
    invoked: that helper starts the container with no stdin, and an ATP agent
    reading stdin would legitimately fail on EOF.

    The tag is fully qualified with `localhost/` so no consumer of it — ATP
    included — treats a short name as something to pull.
    """
    tag = f"localhost/deployer-verify-{uuid.uuid4().hex[:8]}"
    results: list[CheckResult] = []
    image_size: int | None = None
    atp_available = False
    built = BuiltImage(tag=tag, runtime=runtime)
    try:
        with _isolated_context(project_path) as context:
            build_result = _build(
                dockerfile, context, target, runtime, tag, build_timeout
            )
        results.append(build_result)
        if build_result.status is CheckStatus.PASSED:
            image_size = _image_size(runtime, tag)
            if target.service is not None:
                results.append(_run_healthcheck(target, runtime, tag, health_timeout))
            elif target.smoke is not None and smoke_suite is not None:
                if runtime.remote:
                    results.append(
                        CheckResult(
                            check_id="atp_smoke",
                            status=CheckStatus.SKIPPED,
                            message=(
                                "ATP's container adapter has no remote-host "
                                "support; the image built on a remote host is "
                                "not visible to it. Run is non-comparable"
                            ),
                        )
                    )
                else:
                    smoke_result, atp_available = _check_atp_smoke(
                        smoke_suite, runtime, tag, target.smoke.timeout_s
                    )
                    results.append(smoke_result)
            elif target.run is not None:
                results.append(_run_completes(target, runtime, tag, health_timeout))
    finally:
        # Best-effort cleanup that must never clobber the return value — but
        # `container_run` does not pass `check=True`, so a failed `rmi` returns
        # non-zero silently. Record the actual outcome: claiming "removed"
        # without reading the return code would be exactly the unchecked claim
        # `cleanup_status` exists to prevent.
        try:
            removal = container_run(
                runtime, ["rmi", "-f", tag], capture_output=True, timeout=60
            )
            built.cleanup_status = (
                "removed" if removal.returncode == 0 else "failed"
            )
        except (subprocess.TimeoutExpired, OSError):
            built.cleanup_status = "failed"
    return results, image_size, built, atp_available
```

Add `BuiltImage` to the `deployer.models` import list at the top of `verify.py`.

Then update the single call site inside `verify()` (around line 1535):

```python
        elif report.passed:
            docker_results, image_size, built, atp_available = verify_docker(
                dockerfile,
                project_path,
                target,
                runtime,
                build_timeout=build_timeout,
                health_timeout=health_timeout,
                smoke_suite=smoke_suite,
            )
            report.results.extend(docker_results)
            report.image_size_bytes = image_size
            report.built_image = built
            report.atp_available = atp_available
```

`smoke_suite` reaches `verify()` in Task 6; until then add the parameter to `verify()`'s signature as `smoke_suite: Path | None = None` so this task compiles and its tests run.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_docker.py -q` → PASS
Then: `uv run pytest -q` → all pass. If other tests unpack `verify_docker`'s old 2-tuple, update them to the 4-tuple — that is an intended contract change, not a test workaround.

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/verify.py tests/test_verify_docker.py
git commit -m "feat(verify): ATP smoke replaces the job check for smoke targets"
```

---

### Task 6: Thread the resolved suite path from the CLI and the bench

The suite is resolved against the **`target.json` file**, because `_load_target` discards the file's origin (`cli.py:79`) and the bench copies only `project/` into its scratch directory (`bench.py:231-237`). The serialized `DeployTarget` keeps the relative path.

**Files:**
- Modify: `src/deployer/verify.py` (`verify` signature — already added in Task 5)
- Modify: `src/deployer/author.py:70-79` and its two `verify(...)` calls at `:134` and `:147`
- Modify: `src/deployer/cli.py` (`_cmd_verify`, `_cmd_author`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `verify(..., smoke_suite: Path | None = None)` (Task 5).
- Produces: `author_dockerfile(..., smoke_suite: Path | None = None)`; CLI helper `_resolve_smoke_suite(target: DeployTarget, target_path: str | None) -> Path | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_smoke_suite_resolves_against_the_target_file(tmp_path: Path) -> None:
    """Not the cwd and not project/: a target document must stay portable."""
    from deployer.cli import _resolve_smoke_suite
    from deployer.models import DeployTarget

    target_dir = tmp_path / "case"
    target_dir.mkdir()
    (target_dir / "suite.yaml").write_text("test_suite: x\n")
    target = DeployTarget(run={}, smoke={"suite": "suite.yaml"})

    resolved = _resolve_smoke_suite(target, str(target_dir / "target.json"))

    assert resolved == target_dir / "suite.yaml"


def test_smoke_suite_is_none_without_a_smoke_intent() -> None:
    from deployer.cli import _resolve_smoke_suite
    from deployer.models import DeployTarget

    assert _resolve_smoke_suite(DeployTarget(), "/anywhere/target.json") is None


def test_smoke_suite_without_a_target_file_is_an_error() -> None:
    """A smoke intent can only arrive through a --target file, so a missing
    path is a broken invocation, not a silent skip."""
    from deployer.cli import _resolve_smoke_suite
    from deployer.models import DeployTarget

    with pytest.raises(ValueError, match="--target"):
        _resolve_smoke_suite(DeployTarget(run={}, smoke={"suite": "s.yaml"}), None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k smoke_suite`
Expected: FAIL — `ImportError: cannot import name '_resolve_smoke_suite'`.

- [ ] **Step 3: Write the implementation**

In `src/deployer/cli.py`, next to `_load_target`:

```python
def _resolve_smoke_suite(target: DeployTarget, target_path: str | None) -> Path | None:
    """Absolute path of the ATP suite, resolved against the target document.

    Resolving against the cwd would make a target non-portable, and against
    the project directory would put a test suite inside the build context.
    """
    if target.smoke is None:
        return None
    if target_path is None:
        raise ValueError(
            "a smoke intent requires --target: the suite path is resolved "
            "relative to the target file"
        )
    return (Path(target_path).parent / target.smoke.suite).resolve()
```

In `_cmd_verify` and `_cmd_author`, after the target loads successfully and before calling `verify` / `author_dockerfile`:

```python
    try:
        smoke_suite = _resolve_smoke_suite(target, args.target)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

Then pass `smoke_suite=smoke_suite` to the `verify(...)` call in `_cmd_verify` and to `author_dockerfile(...)` in `_cmd_author`.

In `src/deployer/author.py`, add the keyword-only parameter to `author_dockerfile`:

```python
    smoke_suite: Path | None = None,
```

and pass `smoke_suite=smoke_suite` to **both** `verify(...)` calls (`author.py:134` and `author.py:147` — the second is the environment-failure retry; missing it would make a retried run skip the seam).

In `src/deployer/verify.py`, thread the parameter from `verify` into `verify_docker` (the `smoke_suite=smoke_suite` argument added in Task 5).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q -k smoke_suite` → PASS
Then: `uv run pytest -q` → all pass.

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/cli.py src/deployer/author.py src/deployer/verify.py tests/test_cli.py
git commit -m "feat(cli): resolve the ATP suite against the target document"
```

---

### Task 7: Bench — suite resolution, skip semantics, acceptance gate

`VerificationReport.passed` ignores every status but `FAILED` (`models.py:309-311`), so without this task a `SKIPPED` smoke check would let the bench count a run in which the seam never executed as a success.

**Files:**
- Modify: `src/deployer/bench.py` (`BenchCase`, `load_corpus`, `run_case`)
- Modify: `src/deployer/cli.py` (`bench run` gains `--require-atp`)
- Test: `tests/test_bench.py`

**Interfaces:**
- Consumes: `DeployTarget.smoke` (Task 1); `author_dockerfile(..., smoke_suite=...)` (Task 6).
- Produces: `BenchCase.smoke_suite: Path | None`; `run_case` returning `outcome="skipped"` for an unexecuted smoke; `deployer bench run --require-atp`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`:

```python
def test_load_corpus_resolves_the_smoke_suite_beside_target_json(
    tmp_path: Path,
) -> None:
    case = _make_case(
        tmp_path, "agent", target={"run": {}, "smoke": {"suite": "suite.yaml"}}
    )
    (case / "suite.yaml").write_text("test_suite: x\n")

    loaded = load_corpus(tmp_path)[0]

    assert loaded.smoke_suite == case / "suite.yaml"


def test_skipped_smoke_makes_the_case_skipped_not_successful(
    tmp_path: Path, monkeypatch
) -> None:
    """A run where the seam never executed must not read as a pass."""
    case = _make_case(
        tmp_path, "agent", target={"run": {}, "smoke": {"suite": "suite.yaml"}}
    )
    (case / "suite.yaml").write_text("test_suite: x\n")
    run = _fake_run(True)
    run.iterations[-1].report.results.append(
        CheckResult(
            check_id="atp_smoke",
            status=CheckStatus.SKIPPED,
            message="atp 2.1.0 not installed; run is non-comparable",
        )
    )
    monkeypatch.setattr("deployer.bench.author_dockerfile", lambda *a, **k: run)

    result = run_case(
        load_corpus(tmp_path)[0],
        FixtureAuthor("FROM x:1\n"),
        ContainerRuntime(tool="docker"),
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )

    assert result.outcome == "skipped"
    assert "atp" in result.skip_reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bench.py -q -k "smoke_suite or skipped_smoke"`
Expected: FAIL — `BenchCase` has no attribute `smoke_suite`; the second test reports `outcome == "matched"`.

- [ ] **Step 3: Write the implementation**

In `src/deployer/bench.py`, add the field to `BenchCase`:

```python
    smoke_suite: Path | None = None
```

In `load_corpus`, after `target` is parsed, inside the `cases.append(BenchCase(...))` call add:

```python
                smoke_suite=(
                    case_dir / target.smoke.suite if target.smoke is not None else None
                ),
```

In `run_case`, pass the suite through to the author — the call at
`bench.py:243-251` becomes:

```python
        run = author_dockerfile(
            scratch,
            case.target,
            author,
            max_iterations=case.expected.max_iterations,
            runtime=runtime,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
            smoke_suite=case.smoke_suite,
        )
```

Then, after the run completes and before the outcome is decided, add:

```python
    if case.target.smoke is not None and run.iterations:
        smoke = [
            r
            for r in run.iterations[-1].report.results
            if r.check_id == "atp_smoke"
        ]
        if smoke and smoke[0].status is CheckStatus.SKIPPED:
            return BenchCaseResult(
                case=case.name,
                outcome="skipped",
                skip_reason=f"atp_smoke skipped: {smoke[0].message}",
                expected=case.expected,
            )
```

Place it immediately before the code that computes `achieved_level` / `matched`, so a case whose seam never ran is never scored.

In `src/deployer/cli.py`, add the flag to the `bench run` parser:

```python
    p_bench_run.add_argument(
        "--require-atp",
        action="store_true",
        help=(
            "fail if a case declaring a smoke intent was skipped for a missing "
            "or mismatched atp; acceptance of the ATP seam requires it"
        ),
    )
```

and in `_cmd_bench_run`, after the report is built and before returning:

```python
    if args.require_atp:
        # Match the marker `run_case` writes, not the prose after it: a
        # substring test against a free-text message would drift silently as
        # the message is reworded.
        unexecuted = [
            c
            for c in report.cases
            if c.outcome == "skipped"
            and (c.skip_reason or "").startswith("atp_smoke skipped:")
        ]
        if unexecuted:
            names = ", ".join(c.case for c in unexecuted)
            print(
                f"error: --require-atp: smoke never executed for: {names}",
                file=sys.stderr,
            )
            return 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_bench.py -q` → PASS
Then: `uv run pytest -q` → all pass.

- [ ] **Step 5: Format, lint, type check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/bench.py src/deployer/cli.py tests/test_bench.py
git commit -m "feat(bench): a skipped smoke is a skipped case, not a pass"
```

---

### Task 8: The `atp-agent` corpus case, the integration test, and the docs

**Files:**
- Create: `corpus/synthetic/atp-agent/project/agent.py`
- Create: `corpus/synthetic/atp-agent/project/pyproject.toml`
- Create: `corpus/synthetic/atp-agent/target.json`
- Create: `corpus/synthetic/atp-agent/expected.json`
- Create: `corpus/synthetic/atp-agent/fixture.Dockerfile`
- Create: `corpus/synthetic/atp-agent/suite.yaml`
- Modify: `README.md`, `CLAUDE.md` (the corpus-case count and the artifact/verification description)
- Test: `tests/test_corpus.py`, and a `@pytest.mark.docker` test in `tests/test_verify_docker.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a corpus case named `atp-agent`.

- [ ] **Step 1: Write the fixture agent and its case files**

`corpus/synthetic/atp-agent/project/agent.py` — the whole ATP protocol this seam needs, deterministic, no network and no model:

```python
"""Minimal ATP-compatible agent: one request in, one response out."""

import json
import sys


def main() -> int:
    """Read an ATPRequest on stdin, write an ATPResponse on stdout."""
    raw = sys.stdin.read()
    if not raw.strip():
        print("empty request on stdin", file=sys.stderr)
        return 1
    request = json.loads(raw)
    task = request.get("task") or {}
    json.dump(
        {
            "request_id": request.get("request_id", "unknown"),
            "status": "completed",
            "output": f"handled: {task.get('description', '')}",
            "artifacts": [],
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`corpus/synthetic/atp-agent/project/pyproject.toml`:

```toml
[project]
name = "atp-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []
```

`corpus/synthetic/atp-agent/target.json`:

```json
{
  "run": {},
  "smoke": {"suite": "suite.yaml"},
  "base_image": "python:3.12-slim"
}
```

`corpus/synthetic/atp-agent/expected.json`:

```json
{
  "requires_l2": true,
  "expected_success": true,
  "max_iterations": 2,
  "capabilities": ["run", "smoke"],
  "notes": "ATP smoke seam: the packaged agent must still answer an ATPRequest"
}
```

`corpus/synthetic/atp-agent/fixture.Dockerfile` — the known-good artifact the offline author replays:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY agent.py ./

CMD ["python", "agent.py"]
```

`corpus/synthetic/atp-agent/suite.yaml` — fixture-owned, never authored:

```yaml
test_suite: atp_agent_smoke
description: The packaged agent answers an ATPRequest
version: "1.0"

defaults:
  timeout_seconds: 30

tests:
  - id: answers
    name: Agent answers a request
    tags: [smoke]
    task:
      description: "say hello"
    assertions:
      - type: no_errors
```

- [ ] **Step 2: Write the failing corpus test**

Append to `tests/test_corpus.py` (follow the file's existing helper for locating the corpus root):

```python
def test_atp_agent_case_declares_a_smoke_intent_and_ships_its_suite() -> None:
    """The suite is fixture-owned input and lives beside target.json."""
    from deployer.bench import load_corpus

    corpus_root = Path(__file__).resolve().parent.parent / "corpus"
    case = [c for c in load_corpus(corpus_root) if c.name == "atp-agent"][0]

    assert case.target.smoke is not None
    assert case.target.run is not None
    assert case.smoke_suite is not None and case.smoke_suite.is_file()
```

- [ ] **Step 3: Run the test to verify it fails, then passes**

Run: `uv run pytest tests/test_corpus.py -q -k atp_agent`
Expected before the files exist: FAIL with `IndexError` (no such case).
After Step 1's files are in place: PASS.

- [ ] **Step 4: Add the container-marked integration test**

Append to `tests/test_verify_docker.py`:

```python
@pytest.mark.docker
def test_atp_agent_case_builds_and_answers(tmp_path: Path) -> None:
    """End-to-end on a real runtime: build the fixture, then have ATP drive it.

    Skips when `atp` is absent — the acceptance bench run is where a skip is
    rejected, not here.
    """
    import shutil as _shutil

    from deployer.bench import load_corpus
    from deployer.models import DeployTarget
    from deployer.runtime import resolve_runtime
    from deployer.verify import verify_docker

    if _shutil.which("atp") is None:
        pytest.skip("atp not installed")
    corpus_root = Path(__file__).resolve().parent.parent / "corpus"
    case = [c for c in load_corpus(corpus_root) if c.name == "atp-agent"][0]
    assert case.fixture_dockerfile is not None
    dockerfile = case.fixture_dockerfile.read_text()
    runtime = resolve_runtime()
    if runtime is None:
        pytest.skip("no container runtime resolved")

    results, _size, image, available = verify_docker(
        dockerfile,
        case.project_dir,
        case.target,
        runtime,
        smoke_suite=case.smoke_suite,
    )

    assert available is True
    smoke = [r for r in results if r.check_id == "atp_smoke"][0]
    assert smoke.status is CheckStatus.PASSED, smoke.message
    assert image.cleanup_status == "removed"
```

`resolve_runtime(tool_arg=None, host_arg=None, env=None)` returns
`ContainerRuntime | None` (`src/deployer/runtime.py:54-58`); `None` means
static-only, hence the skip. `DeployTarget` is imported above only if the
surrounding file needs it — drop the unused import if ruff flags it.

- [ ] **Step 5: Update the docs**

In `README.md`, in the corpus section, add `atp-agent` to the case list and document the intent:

```markdown
`smoke` in a target requests an ATP smoke test of the built image:

```json
{"run": {}, "smoke": {"suite": "suite.yaml", "timeout_s": 300}}
```

The suite path is resolved relative to the `target.json` that declares it.
The check id is `atp_smoke`; it needs `atp` 2.1.0 on `PATH` and a local
container runtime (ATP's container adapter has no remote-host support), and
reports `SKIPPED` otherwise. `deployer bench run --require-atp` turns such a
skip into a failure, which is how the seam is accepted.

#### Installing `atp` 2.1.0 (temporary source-install workaround)

The published release cannot be installed: `atp-platform==2.1.0` requires
`atp-adapters`, which was never published to PyPI, and its CLI additionally
imports `fastapi` and `atp_sdk` without declaring them. Tracked as
atp-platform#320 (`publish-installable-container-cli`). **Until that closes,
install from the tagged source.** Do not assume a checkout of atp-platform is
already on disk:

```bash
tmp=$(mktemp -d)
git clone --depth 1 --branch v2.1.0 \
    git@github.com:andrei-shtanakov/atp-platform.git "$tmp/atp"
cd "$tmp/atp" && uv tool install '.[dashboard]' --with ./packages/atp-sdk
```

Verify both, not just the first:

```bash
atp --version                        # atp, version 2.1.0
atp plugins list --type=adapter      # must list `container`
```
```

In `CLAUDE.md`, update the sentence that lists what verification does, so the L2 description mentions the ATP smoke level, and update the corpus case count from 11 to 12.

In `TODO.md`, the `install-atp` item gets the same install procedure by
reference (`README.md`, "Installing `atp` 2.1.0"), plus the note that it is a
temporary workaround pending atp-platform#320. **Do not tick `install-atp`
until the README instructions have been reproduced from a clean temporary
directory** — not from an existing atp-platform checkout and not from a
leftover scratch tree. An instruction that only works on the machine that
wrote it is not an instruction.

- [ ] **Step 6: Run everything and commit**

```bash
uv run pytest -q
uv run pytest -m docker -q        # requires a container runtime; atp-marked test skips without atp
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add corpus/synthetic/atp-agent README.md CLAUDE.md tests/
git commit -m "feat(corpus): atp-agent case proving the ATP smoke seam"
```

---

## Acceptance (run by the owner, not by a task)

The seam is closed only by an end-to-end bench run reporting `atp_smoke: PASSED`.
`SKIPPED` keeps an ordinary run portable but does not close it.

```bash
uv run deployer bench run --filter 'atp-agent' --require-atp
uv run deployer bench compare .deployer-runs/<ts>-<label> golden
uv run deployer bench promote .deployer-runs/<ts>-<label>   # after reviewing the diff
```

This requires `atp` 2.1.0 on the bench machine — `todo://deployer/install-atp`.
The new corpus case moves the golden baseline, so the diff is reviewed before
promoting, per the usual rhythm.

## Follow-ups this plan deliberately leaves open

- No `atp_smoke` axis in `bench compare`: one case measures one example, the
  same reasoning that parks `todo://deployer/second-ci-corpus-case`.
- No http/service adapter path, no remote runtime support — both are separate
  seams named as non-goals in the spec.
