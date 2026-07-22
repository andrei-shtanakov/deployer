# Compose Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Multi-service artifact support: the model authors Dockerfile + compose.yaml for app-plus-infra-deps targets; a deterministic verifier checks the wiring statically and via `compose up` with an in-network probe.

**Architecture:** `DeployTarget.dependencies` (pinned infra images) turns the artifact into a sentinel-delimited pair parsed by a new `artifacts` module; the author loop threads raw response text; `verify` gains compose L1 checks (PyYAML `safe_load`, schema slice only) and a compose L2 path (unique project name, isolated context, `compose exec` probe, guaranteed `down -v`). Missing compose provider with a runtime present is FAILED/ENVIRONMENT, never a skip.

**Tech Stack:** Python 3.12, pydantic, PyYAML (new dep), pytest, uv, docker/podman compose.

**Spec:** `docs/superpowers/specs/2026-07-22-compose-support-design.md` — read it first.

## Global Constraints

- Branch: `feature/compose` (exists, holds the spec commit). Never commit to `master`.
- Dependency image pinning mirrors base-image rule: tag allowed, digest preferred; reject untagged and `:latest`.
- YAML parsing ONLY via `yaml.safe_load`; validate only the schema slice deployer relies on (top-level mapping → `services` mapping → service mappings).
- App service name is exactly `app` (reserved; `ServiceDependency.name` must never be `app`).
- **No service may declare `ports`** — compose networking is internal-only for the verifier.
- L2 compose runs under a unique project name `deployer-verify-<uuid8>`; compose files live in (and run from) the isolated temp context; teardown `down -v` happens in `finally` INSIDE the context manager and never clobbers the result.
- Runtime present + dependencies declared + compose provider missing → `compose_available` FAILED ENVIRONMENT. Runtime absent entirely → existing static-only semantics.
- Sentinels verbatim: `=== Dockerfile ===` and `=== compose.yaml ===`.
- After every task: `uv run ruff format . && uv run ruff check . && uv run pyrefly check` clean; full `uv run pytest` green.
- 88-char lines, type hints, docstrings on new public helpers, follow existing file patterns.

---

### Task 1: Contract — `ServiceDependency`, `DeployTarget.dependencies`, `IterationRecord.compose`

**Files:**
- Modify: `src/deployer/models.py` (after `RunSpec`, inside `DeployTarget`, inside `IterationRecord`)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `ServiceDependency(name: str, image: str, env: dict[str, str] = {})`; `DeployTarget.dependencies: list[ServiceDependency] = []`; `IterationRecord.compose: str | None = None`. Later tasks branch on `bool(target.dependencies)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_models.py`)

```python
def test_service_dependency_requires_pinned_image() -> None:
    from deployer.models import ServiceDependency

    ServiceDependency(name="cache", image="redis:7-alpine")
    ServiceDependency(name="db", image="postgres:16-alpine")
    ServiceDependency(name="cache", image="redis@sha256:" + "a" * 64)
    for image in ("redis", "redis:latest"):
        with pytest.raises(ValidationError):
            ServiceDependency(name="cache", image=image)


def test_service_dependency_name_rules() -> None:
    from deployer.models import ServiceDependency

    with pytest.raises(ValidationError):
        ServiceDependency(name="app", image="redis:7-alpine")
    with pytest.raises(ValidationError):
        ServiceDependency(name="Cache!", image="redis:7-alpine")


def test_dependencies_require_service() -> None:
    from deployer.models import ServiceDependency

    dep = ServiceDependency(name="cache", image="redis:7-alpine")
    DeployTarget(service=ServiceSpec(port=8000), dependencies=[dep])
    with pytest.raises(ValidationError):
        DeployTarget(dependencies=[dep])  # no service
    with pytest.raises(ValidationError):
        DeployTarget(run=RunSpec(), dependencies=[dep])  # job with deps


def test_duplicate_dependency_names_rejected() -> None:
    from deployer.models import ServiceDependency

    deps = [
        ServiceDependency(name="cache", image="redis:7-alpine"),
        ServiceDependency(name="cache", image="redis:8-alpine"),
    ]
    with pytest.raises(ValidationError):
        DeployTarget(service=ServiceSpec(port=8000), dependencies=deps)


def test_iteration_record_compose_defaults_none() -> None:
    from deployer.models import IterationRecord, VerificationReport

    rec = IterationRecord(
        index=0, dockerfile="FROM x:1", report=VerificationReport(), duration_s=0.1
    )
    assert rec.compose is None
```

(Ensure `ValidationError` is imported from `pydantic` and `RunSpec`/`ServiceSpec` from `deployer.models` at the top of the test file — they may already be.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -k "dependency or dependencies or compose_defaults" -v`
Expected: FAIL (ImportError: ServiceDependency)

- [ ] **Step 3: Implement in `src/deployer/models.py`**

After `RunSpec`:

```python
_DEP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ServiceDependency(BaseModel):
    """A pinned infra dependency the app service needs next to it."""

    name: str
    image: str
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_service_name(cls, value: str) -> str:
        if value == "app":
            raise ValueError(
                'ServiceDependency.name "app" is reserved for the app service'
            )
        if not _DEP_NAME_RE.fullmatch(value):
            raise ValueError(
                "ServiceDependency.name must match [a-z][a-z0-9_-]*, "
                f"got {value!r}"
            )
        return value

    @field_validator("image")
    @classmethod
    def _pinned_image(cls, value: str) -> str:
        """Same rule as base images: tag allowed, digest preferred."""
        if "@sha256:" in value:
            return value
        _, _, tag = value.partition(":")
        if not tag or tag == "latest":
            raise ValueError(
                "ServiceDependency.image must be pinned (a tag or digest, "
                f"never :latest): {value!r}"
            )
        return value
```

In `DeployTarget`, add the field after `entrypoint`:

```python
    dependencies: list[ServiceDependency] = Field(default_factory=list)
```

and extend the model validator (add a second validator after `_service_and_run_exclusive`):

```python
    @model_validator(mode="after")
    def _dependencies_require_service(self) -> "DeployTarget":
        if self.dependencies:
            if self.service is None:
                raise ValueError(
                    "DeployTarget.dependencies require a service intent "
                    "(jobs with dependencies are unsupported)"
                )
            names = [d.name for d in self.dependencies]
            if len(names) != len(set(names)):
                raise ValueError(
                    "DeployTarget.dependencies names must be unique"
                )
        return self
```

In `IterationRecord`, after `dockerfile: str`:

```python
    compose: str | None = None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_models.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/models.py tests/test_models.py
git commit -m "feat: ServiceDependency contract + DeployTarget.dependencies"
```

---

### Task 2: `artifacts` module — sentinel parse/render

**Files:**
- Create: `src/deployer/artifacts.py`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Produces: `DOCKERFILE_SENTINEL = "=== Dockerfile ==="`; `COMPOSE_SENTINEL = "=== compose.yaml ==="`; `class ArtifactParseError(ValueError)`; `parse_artifact_response(text: str, expects_compose: bool) -> tuple[str, str | None]`; `render_artifact_response(dockerfile: str, compose: str | None) -> str`. Tasks 5–6 use all of these.

- [ ] **Step 1: Write the failing tests** (create `tests/test_artifacts.py`)

```python
import pytest

from deployer.artifacts import (
    COMPOSE_SENTINEL,
    DOCKERFILE_SENTINEL,
    ArtifactParseError,
    parse_artifact_response,
    render_artifact_response,
)

RESPONSE = (
    f"{DOCKERFILE_SENTINEL}\nFROM python:3.12-slim\n"
    f"{COMPOSE_SENTINEL}\nservices:\n  app:\n    build: .\n"
)


def test_parse_both_sections() -> None:
    dockerfile, compose = parse_artifact_response(RESPONSE, expects_compose=True)
    assert dockerfile == "FROM python:3.12-slim"
    assert compose == "services:\n  app:\n    build: ."


def test_parse_no_deps_passthrough() -> None:
    dockerfile, compose = parse_artifact_response(
        "FROM python:3.12-slim\n", expects_compose=False
    )
    assert dockerfile == "FROM python:3.12-slim"
    assert compose is None


def test_parse_missing_compose_section_raises() -> None:
    with pytest.raises(ArtifactParseError):
        parse_artifact_response("FROM python:3.12-slim\n", expects_compose=True)


def test_parse_missing_dockerfile_section_raises() -> None:
    text = f"{COMPOSE_SENTINEL}\nservices: {{}}\n"
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(text, expects_compose=True)


def test_parse_duplicated_sentinel_raises() -> None:
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(RESPONSE + RESPONSE, expects_compose=True)


def test_render_round_trips() -> None:
    text = render_artifact_response("FROM x:1", "services: {}")
    assert parse_artifact_response(text, expects_compose=True) == (
        "FROM x:1",
        "services: {}",
    )
    assert render_artifact_response("FROM x:1", None) == "FROM x:1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_artifacts.py -v`
Expected: FAIL (ModuleNotFoundError: deployer.artifacts)

- [ ] **Step 3: Implement `src/deployer/artifacts.py`**

```python
"""Sentinel-delimited multi-artifact responses: parse and render.

Deterministic pipeline code: the model returns raw text; this module
splits it. A malformed response raises ArtifactParseError, which the
authoring loop converts into an authoring finding — never a crash.
"""

DOCKERFILE_SENTINEL = "=== Dockerfile ==="
COMPOSE_SENTINEL = "=== compose.yaml ==="


class ArtifactParseError(ValueError):
    """The response does not match the required sentinel format."""


def parse_artifact_response(
    text: str, expects_compose: bool
) -> tuple[str, str | None]:
    """Split a raw author response into (dockerfile, compose).

    Without compose expectation the whole text is the Dockerfile —
    the single-artifact contract is unchanged.
    """
    if not expects_compose:
        return text.strip(), None
    for sentinel in (DOCKERFILE_SENTINEL, COMPOSE_SENTINEL):
        count = text.count(sentinel)
        if count != 1:
            raise ArtifactParseError(
                f"response must contain the line {sentinel!r} exactly once "
                f"(found {count}); reply with both sections under "
                f"{DOCKERFILE_SENTINEL!r} and {COMPOSE_SENTINEL!r}"
            )
    head, _, rest = text.partition(DOCKERFILE_SENTINEL)
    if COMPOSE_SENTINEL in head:
        raise ArtifactParseError(
            f"{DOCKERFILE_SENTINEL!r} must come before {COMPOSE_SENTINEL!r}"
        )
    dockerfile, _, compose = rest.partition(COMPOSE_SENTINEL)
    if not dockerfile.strip() or not compose.strip():
        raise ArtifactParseError("both artifact sections must be non-empty")
    return dockerfile.strip(), compose.strip()


def render_artifact_response(dockerfile: str, compose: str | None) -> str:
    """Inverse of parse: the format fixture authors and prompts use."""
    if compose is None:
        return dockerfile
    return (
        f"{DOCKERFILE_SENTINEL}\n{dockerfile}\n{COMPOSE_SENTINEL}\n{compose}"
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_artifacts.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/artifacts.py tests/test_artifacts.py
git commit -m "feat: sentinel artifact parse/render module"
```

---

### Task 3: Compose L1 checks + verify() compose plumbing

**Files:**
- Modify: `pyproject.toml` (via `uv add pyyaml`)
- Modify: `src/deployer/verify.py` (new check functions; `verify_static` untouched; `verify` gains `compose` kwarg)
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `DeployTarget.dependencies` (Task 1).
- Produces: `_compose_l1_checks(compose: str | None, target: DeployTarget) -> list[CheckResult]` with check ids `compose_present`, `compose_parses`, `compose_services`, `compose_wiring`; `verify(..., compose: str | None = None)` appends them when `target.dependencies` is non-empty. Task 4 extends `verify` further; Tasks 5–6 pass `compose` through.

- [ ] **Step 1: Add the dependency**

Run: `uv add pyyaml`
Expected: pyyaml appears in `pyproject.toml` dependencies and `uv.lock`. If `uv run pyrefly check` later complains about missing stubs, also run `uv add --dev types-PyYAML`.

- [ ] **Step 2: Write the failing tests** (append to `tests/test_verify_static.py`)

```python
COMPOSE_TARGET = DeployTarget(
    service=ServiceSpec(port=8000),
    env={"REDIS_URL": "redis://cache:6379/0"},
    dependencies=[ServiceDependency(name="cache", image="redis:7-alpine")],
)

COMPOSE_GOOD = """\
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      REDIS_URL: redis://cache:6379/0
    depends_on:
      cache:
        condition: service_healthy
  cache:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
"""


def _compose_checks(compose: str | None):
    from deployer.verify import _compose_l1_checks

    return {r.check_id: r for r in _compose_l1_checks(compose, COMPOSE_TARGET)}


def test_compose_good_passes_all_l1() -> None:
    checks = _compose_checks(COMPOSE_GOOD)
    for check_id in (
        "compose_present",
        "compose_parses",
        "compose_services",
        "compose_wiring",
    ):
        assert checks[check_id].status is CheckStatus.PASSED, check_id


def test_compose_missing_artifact_fails_present() -> None:
    checks = _compose_checks(None)
    check = checks["compose_present"]
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "compose_parses" not in checks  # later checks not attempted


def test_compose_unparseable_yaml_fails() -> None:
    checks = _compose_checks("services: [unclosed")
    assert checks["compose_parses"].status is CheckStatus.FAILED


def test_compose_non_mapping_shapes_fail() -> None:
    for text in ("- a\n- b", "services: []", "services:\n  app: []"):
        checks = _compose_checks(text)
        assert checks["compose_parses"].status is CheckStatus.FAILED, text


def test_compose_wrong_service_set_fails() -> None:
    missing_dep = COMPOSE_GOOD.replace("  cache:\n    image: redis:7-alpine\n", "")
    extra = COMPOSE_GOOD + "  rogue:\n    image: nginx:1.27\n"
    for text in (missing_dep, extra):
        checks = _compose_checks(text)
        assert checks["compose_services"].status is CheckStatus.FAILED, text


def test_compose_image_mismatch_fails() -> None:
    checks = _compose_checks(COMPOSE_GOOD.replace("redis:7-alpine", "redis:6"))
    assert checks["compose_services"].status is CheckStatus.FAILED


def test_compose_app_build_shapes() -> None:
    short_form = COMPOSE_GOOD.replace(
        "    build:\n      context: .\n      dockerfile: Dockerfile\n",
        "    build: .\n",
    )
    assert _compose_checks(short_form)["compose_services"].status is (
        CheckStatus.PASSED
    )
    wrong_context = COMPOSE_GOOD.replace("context: .", "context: ./src")
    assert _compose_checks(wrong_context)["compose_services"].status is (
        CheckStatus.FAILED
    )


def test_compose_missing_healthcheck_fails_wiring() -> None:
    text = COMPOSE_GOOD.replace(
        "    healthcheck:\n      test: [\"CMD\", \"redis-cli\", \"ping\"]\n"
        "      interval: 2s\n",
        "",
    )
    assert _compose_checks(text)["compose_wiring"].status is CheckStatus.FAILED


def test_compose_depends_on_needs_condition() -> None:
    list_form = COMPOSE_GOOD.replace(
        "    depends_on:\n      cache:\n        condition: service_healthy\n",
        "    depends_on: [cache]\n",
    )
    assert _compose_checks(list_form)["compose_wiring"].status is CheckStatus.FAILED


def test_compose_missing_env_key_fails_wiring() -> None:
    text = COMPOSE_GOOD.replace(
        "    environment:\n      REDIS_URL: redis://cache:6379/0\n", ""
    )
    assert _compose_checks(text)["compose_wiring"].status is CheckStatus.FAILED


def test_compose_env_list_form_accepted() -> None:
    text = COMPOSE_GOOD.replace(
        "    environment:\n      REDIS_URL: redis://cache:6379/0\n",
        "    environment:\n      - REDIS_URL=redis://cache:6379/0\n",
    )
    assert _compose_checks(text)["compose_wiring"].status is CheckStatus.PASSED


def test_compose_ports_forbidden_everywhere() -> None:
    on_app = COMPOSE_GOOD.replace(
        "    environment:", '    ports:\n      - "8000:8000"\n    environment:'
    )
    on_dep = COMPOSE_GOOD.replace(
        "    image: redis:7-alpine",
        '    image: redis:7-alpine\n    ports:\n      - "6379:6379"',
    )
    for text in (on_app, on_dep):
        checks = _compose_checks(text)
        assert checks["compose_wiring"].status is CheckStatus.FAILED, text


def test_verify_appends_compose_checks_for_deps_target(hello_service: Path) -> None:
    report = verify(GOOD, hello_service, COMPOSE_TARGET, None, compose=COMPOSE_GOOD)
    ids = [r.check_id for r in report.results]
    assert "compose_parses" in ids and "compose_wiring" in ids

    plain = verify(GOOD, hello_service, DeployTarget(), None)
    assert "compose_present" not in [r.check_id for r in plain.results]
```

(Add `ServiceDependency` to the `deployer.models` import at the top of the test file.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -k compose -v`
Expected: FAIL (ImportError: `_compose_l1_checks`)

- [ ] **Step 4: Implement in `src/deployer/verify.py`**

Add `import yaml` to the imports. Add after `_check_entrypoint_in_command`:

```python
def _failed(check_id: str, message: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=CheckStatus.FAILED,
        failure_kind=FailureKind.AUTHORING,
        message=message,
    )


def _passed(check_id: str) -> CheckResult:
    return CheckResult(check_id=check_id, status=CheckStatus.PASSED)


def _env_keys(service: dict) -> set[str]:
    """Keys of a compose `environment` block, mapping or KEY=VALUE list."""
    raw = service.get("environment")
    if isinstance(raw, dict):
        return {k for k in raw if isinstance(k, str)}
    if isinstance(raw, list):
        return {
            e.split("=", 1)[0] for e in raw if isinstance(e, str) and "=" in e
        }
    return set()


def _compose_l1_checks(
    compose: str | None, target: DeployTarget
) -> list[CheckResult]:
    """Static checks for the compose artifact of a dependencies target.

    Validates only the schema slice deployer relies on (services
    mapping, service mappings) — never the full Compose spec.
    """
    if compose is None:
        return [
            _failed(
                "compose_present",
                "deploy target declares dependencies but no compose.yaml "
                "artifact was provided",
            )
        ]
    results = [_passed("compose_present")]

    try:
        doc = yaml.safe_load(compose)
    except yaml.YAMLError as exc:
        results.append(_failed("compose_parses", f"compose.yaml: {exc}"))
        return results
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not all(
        isinstance(v, dict) for v in services.values()
    ):
        results.append(
            _failed(
                "compose_parses",
                "compose.yaml must be a mapping with a `services` mapping "
                "whose values are mappings",
            )
        )
        return results
    results.append(_passed("compose_parses"))

    problems: list[str] = []
    expected = {"app"} | {d.name for d in target.dependencies}
    actual = set(services)
    if actual != expected:
        problems.append(
            f"services must be exactly {sorted(expected)}, got {sorted(actual)}"
        )
    app = services.get("app", {})
    build = app.get("build")
    build_ok = build == "." or (
        isinstance(build, dict)
        and build.get("context") == "."
        and build.get("dockerfile", "Dockerfile") == "Dockerfile"
    )
    if "app" in services and not build_ok:
        problems.append(
            "app must build from the project Dockerfile "
            '(build: "." or {context: ".", dockerfile: "Dockerfile"})'
        )
    for dep in target.dependencies:
        svc = services.get(dep.name)
        if svc is not None and svc.get("image") != dep.image:
            problems.append(
                f"service {dep.name} must use image {dep.image!r} verbatim, "
                f"got {svc.get('image')!r}"
            )
    results.append(
        _failed("compose_services", "; ".join(problems))
        if problems
        else _passed("compose_services")
    )

    wiring: list[str] = []
    depends = app.get("depends_on")
    for dep in target.dependencies:
        svc = services.get(dep.name)
        if svc is not None and not isinstance(svc.get("healthcheck"), dict):
            wiring.append(f"service {dep.name} must define a healthcheck")
        condition = (
            depends.get(dep.name, {}).get("condition")
            if isinstance(depends, dict)
            and isinstance(depends.get(dep.name), dict)
            else None
        )
        if condition != "service_healthy":
            wiring.append(
                f"app must depend on {dep.name} with "
                "condition: service_healthy"
            )
    missing_env = set(target.env) - _env_keys(app)
    if missing_env:
        wiring.append(
            "app environment must carry the deploy intent env keys: "
            f"missing {sorted(missing_env)}"
        )
    for name, svc in services.items():
        if isinstance(svc, dict) and "ports" in svc:
            wiring.append(
                f"service {name} declares ports; compose networking is "
                "internal-only for the verifier"
            )
    results.append(
        _failed("compose_wiring", "; ".join(wiring))
        if wiring
        else _passed("compose_wiring")
    )
    return results
```

In `verify()`, add the keyword `compose: str | None = None` to the signature and insert right after `report = verify_static(...)`:

```python
    if target.dependencies:
        report.results.extend(_compose_l1_checks(compose, target))
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_verify_static.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 6: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add pyproject.toml uv.lock src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: compose L1 checks (present/parses/services/wiring) + PyYAML"
```

---

### Task 4: Compose L2 — provider probe, up/probe/down, verify() wiring

**Files:**
- Modify: `src/deployer/runtime.py` (add `compose_available`)
- Modify: `src/deployer/verify.py` (add `_verify_compose`; extend `verify`)
- Test: `tests/test_runtime.py`, `tests/test_verify_docker.py`

**Interfaces:**
- Consumes: `_compose_l1_checks` (Task 3), `container_run` chokepoint, `_isolated_context`, `_classify`/`_is_transport_failure`/`_tail`, `_fake_container_run` test helper (exists in `tests/test_verify_static.py` — import or replicate per that file's pattern).
- Produces: `compose_available(runtime: ContainerRuntime) -> bool`; `_verify_compose(dockerfile: str, compose: str, project_path: Path, target: DeployTarget, runtime: ContainerRuntime, *, build_timeout: int, health_timeout: int) -> list[CheckResult]` with check ids `compose_up`, `compose_healthcheck`; `verify` emits `compose_available` FAILED/ENVIRONMENT when provider missing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runtime.py`:

```python
def test_compose_available_true_and_false(monkeypatch) -> None:
    import subprocess

    from deployer.models import ContainerRuntime
    from deployer.runtime import compose_available

    runtime = ContainerRuntime(tool="podman")

    def ok(rt, args, **kwargs):
        assert args == ["compose", "version"]
        return subprocess.CompletedProcess(args, 0, stdout="v2", stderr="")

    monkeypatch.setattr("deployer.runtime.container_run", ok)
    assert compose_available(runtime) is True

    def missing(rt, args, **kwargs):
        return subprocess.CompletedProcess(args, 125, stdout="", stderr="no provider")

    monkeypatch.setattr("deployer.runtime.container_run", missing)
    assert compose_available(runtime) is False

    def boom(rt, args, **kwargs):
        raise OSError("gone")

    monkeypatch.setattr("deployer.runtime.container_run", boom)
    assert compose_available(runtime) is False
```

Append to `tests/test_verify_docker.py` (follow that file's existing fake-container_run idiom; `COMPOSE_TARGET`/`COMPOSE_GOOD` as in Task 3 — define locally):

```python
def test_verify_compose_up_probe_down_sequence(monkeypatch, tmp_path: Path) -> None:
    from deployer.verify import _verify_compose

    calls: list[list[str]] = []

    def fake(runtime, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    results = _verify_compose(
        "FROM python:3.12-slim",
        COMPOSE_GOOD,
        tmp_path,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        build_timeout=60,
        health_timeout=5,
    )
    by_id = {r.check_id: r for r in results}
    assert by_id["compose_up"].status is CheckStatus.PASSED
    assert by_id["compose_healthcheck"].status is CheckStatus.PASSED
    flat = ["\x00".join(c) for c in calls]
    assert any("up" in c for c in calls if c[0] == "compose")
    project_flags = {c[c.index("-p") + 1] for c in calls if "-p" in c}
    assert len(project_flags) == 1  # one unique project name throughout
    assert next(iter(project_flags)).startswith("deployer-verify-")
    assert calls[-1][:1] == ["compose"] and "down" in calls[-1]  # teardown last
    assert "-v" in calls[-1]


def test_verify_compose_up_failure_classifies_and_still_tears_down(
    monkeypatch, tmp_path: Path
) -> None:
    from deployer.verify import _verify_compose

    calls: list[list[str]] = []

    def fake(runtime, args, **kwargs):
        calls.append(args)
        if "up" in args:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="build failed: syntax error"
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    results = _verify_compose(
        "FROM python:3.12-slim",
        COMPOSE_GOOD,
        tmp_path,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        build_timeout=60,
        health_timeout=5,
    )
    by_id = {r.check_id: r for r in results}
    assert by_id["compose_up"].status is CheckStatus.FAILED
    assert by_id["compose_up"].failure_kind == "authoring"
    assert "compose_healthcheck" not in by_id
    assert "down" in calls[-1]  # teardown ran despite failure


def test_verify_compose_probe_failure_collects_logs(
    monkeypatch, tmp_path: Path
) -> None:
    from deployer.verify import _verify_compose

    def fake(runtime, args, **kwargs):
        if "exec" in args:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="urlopen error"
            )
        if "logs" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="app exploded here", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    results = _verify_compose(
        "FROM python:3.12-slim",
        COMPOSE_GOOD,
        tmp_path,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        build_timeout=60,
        health_timeout=2,
    )
    by_id = {r.check_id: r for r in results}
    check = by_id["compose_healthcheck"]
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "app exploded here" in check.message


def test_verify_missing_compose_provider_is_environment_failure(
    monkeypatch, hello_service: Path
) -> None:
    monkeypatch.setattr("deployer.verify.compose_available", lambda rt: False)
    report = verify(
        GOOD,
        hello_service,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        compose=COMPOSE_GOOD,
    )
    check = next(r for r in report.results if r.check_id == "compose_available")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "environment"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runtime.py tests/test_verify_docker.py -k compose -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implement**

`src/deployer/runtime.py`, after `probe_runtime_versions`:

```python
def compose_available(runtime: ContainerRuntime) -> bool:
    """Whether `<tool> compose` resolves to a working provider."""
    try:
        proc = container_run(
            runtime,
            ["compose", "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0
```

`src/deployer/verify.py`: import `compose_available` from `deployer.runtime`; add after `_run_completes`:

```python
def _verify_compose(
    dockerfile: str,
    compose: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    *,
    build_timeout: int,
    health_timeout: int,
) -> list[CheckResult]:
    """L2 for a dependencies target: compose up, in-network probe, down.

    The candidate Dockerfile and compose.yaml are written INTO the
    isolated context and compose runs against that copy, so the
    CONTEXT_IGNORE invariant holds and `app.build.context: "."`
    resolves inside the sandbox. The unique project name keeps parallel
    runs and stale containers from colliding. Teardown (`down -v`)
    runs in `finally` inside the context manager and never clobbers
    the result. No ports are ever published; the probe runs inside the
    app container. memory_limit is not enforced on this path
    (provider support is inconsistent).
    """
    assert target.service is not None
    project = f"deployer-verify-{uuid.uuid4().hex[:8]}"
    url = (
        f"http://127.0.0.1:{target.service.port}"
        f"{target.service.healthcheck_path}"
    )
    probe = f"import urllib.request; urllib.request.urlopen('{url}', timeout=2)"
    results: list[CheckResult] = []
    with _isolated_context(project_path) as context:
        (context / "Dockerfile").write_text(dockerfile + "\n")
        (context / "compose.yaml").write_text(compose + "\n")
        base = ["compose", "-p", project, "-f", str(context / "compose.yaml")]
        try:
            up = container_run(
                runtime,
                [*base, "up", "--build", "-d"],
                capture_output=True,
                text=True,
                timeout=build_timeout,
            )
            if up.returncode != 0:
                results.append(
                    CheckResult(
                        check_id="compose_up",
                        status=CheckStatus.FAILED,
                        failure_kind=_classify(up.stdout + "\n" + up.stderr),
                        message=_tail(up.stderr or up.stdout),
                    )
                )
                return results
            results.append(_passed("compose_up"))

            deadline = time.monotonic() + health_timeout
            last_error = ""
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    probe_proc = container_run(
                        runtime,
                        [*base, "exec", "-T", "app", "python", "-c", probe],
                        capture_output=True,
                        text=True,
                        timeout=max(1.0, remaining),
                    )
                except subprocess.TimeoutExpired:
                    break
                if probe_proc.returncode == 0:
                    results.append(_passed("compose_healthcheck"))
                    return results
                last_error = probe_proc.stdout + "\n" + probe_proc.stderr
                time.sleep(1)
            logs = container_run(
                runtime, [*base, "logs"], capture_output=True, text=True, timeout=30
            )
            log_text = (logs.stdout + "\n" + logs.stderr).strip()
            if _is_transport_failure(last_error):
                results.append(
                    CheckResult(
                        check_id="compose_healthcheck",
                        status=CheckStatus.FAILED,
                        failure_kind=FailureKind.ENVIRONMENT,
                        message=(
                            f"healthcheck {url}: daemon became unreachable "
                            f"mid-poll: {_tail(last_error, 3)}"
                        ),
                    )
                )
                return results
            results.append(
                CheckResult(
                    check_id="compose_healthcheck",
                    status=CheckStatus.FAILED,
                    failure_kind=FailureKind.AUTHORING,
                    message=(
                        f"healthcheck {url} failed within {health_timeout}s: "
                        f"{_tail(last_error, 3)}\ncompose logs:\n{_tail(log_text)}"
                    ),
                )
            )
            return results
        except (subprocess.TimeoutExpired, OSError) as exc:
            message = (
                "compose command timed out"
                if isinstance(exc, subprocess.TimeoutExpired)
                else f"compose command failed: {exc}"
            )
            results.append(
                CheckResult(
                    check_id="compose_up" if not results else "compose_healthcheck",
                    status=CheckStatus.FAILED,
                    failure_kind=FailureKind.ENVIRONMENT,
                    message=message,
                )
            )
            return results
        finally:
            try:
                container_run(
                    runtime,
                    [*base, "down", "-v", "--timeout", "10"],
                    capture_output=True,
                    timeout=60,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass  # best-effort cleanup; must never clobber the result
```

Extend `verify()` — replace the `if runtime is not None:` block body:

```python
    if runtime is not None:
        report.docker_available = True
        if target.dependencies:
            if not compose_available(runtime):
                report.results.append(
                    CheckResult(
                        check_id="compose_available",
                        status=CheckStatus.FAILED,
                        failure_kind=FailureKind.ENVIRONMENT,
                        message=(
                            f"{runtime.tool} compose provider is not "
                            "available; a dependencies target cannot be "
                            "L2-verified"
                        ),
                    )
                )
            elif report.passed and compose is not None:
                report.results.extend(
                    _verify_compose(
                        dockerfile,
                        compose,
                        project_path,
                        target,
                        runtime,
                        build_timeout=build_timeout,
                        health_timeout=health_timeout,
                    )
                )
        elif report.passed:
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
```

(`image_size_bytes` stays `None` on the compose path — compose image naming is provider-dependent; documented limitation.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_runtime.py tests/test_verify_docker.py tests/test_verify_static.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/runtime.py src/deployer/verify.py tests/test_runtime.py tests/test_verify_docker.py
git commit -m "feat: compose L2 — provider probe, up/exec-probe/down, environment semantics"
```

---

### Task 5: Author loop + prompt + CLI artifact IO

**Files:**
- Modify: `src/deployer/author.py` (loop threads raw text; parses artifacts)
- Modify: `src/deployer/llm.py` (compose prompt section; repair label)
- Modify: `src/deployer/cli.py` (`_cmd_verify` reads compose.yaml; `_cmd_author` writes it)
- Test: `tests/test_author.py`, `tests/test_llm.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `parse_artifact_response`/`ArtifactParseError`/`render_artifact_response` (Task 2), `verify(..., compose=...)` (Tasks 3–4), `IterationRecord.compose` (Task 1).
- Produces: `DockerfileAuthor.generate`/`repair` semantics: return RAW response text — plain Dockerfile without dependencies, sentinel format with. `repair`'s third parameter is the previous raw response (rename to `artifact_text`). `author_dockerfile` records `IterationRecord(dockerfile=..., compose=...)`; a parse failure records `dockerfile=<raw response>`, `compose=None` and a report with the single FAILED check `artifact_format`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_author.py` (reuse that file's existing stub-author pattern):

```python
COMPOSE_TARGET = DeployTarget(
    service=ServiceSpec(port=8000),
    dependencies=[ServiceDependency(name="cache", image="redis:7-alpine")],
)


class _ComposeAuthor:
    """Returns a valid sentinel response; repair returns it unchanged."""

    def __init__(self, dockerfile: str, compose: str) -> None:
        self.response = render_artifact_response(dockerfile, compose)

    def generate(self, facts, target):
        return self.response

    def repair(self, facts, target, artifact_text, report):
        return self.response


def test_author_records_compose_artifact(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    author = _ComposeAuthor("FROM python:3.12-slim\nCOPY main.py .", "services: {}")
    run = author_dockerfile(
        tmp_path, COMPOSE_TARGET, author, runtime=None
    )
    assert run.iterations[0].compose == "services: {}"
    assert run.iterations[0].dockerfile.startswith("FROM python:3.12-slim")


def test_author_parse_failure_becomes_artifact_format_finding(
    tmp_path: Path,
) -> None:
    class _Broken:
        def generate(self, facts, target):
            return "FROM python:3.12-slim"  # no sentinels despite deps

        def repair(self, facts, target, artifact_text, report):
            return "FROM python:3.12-slim"  # still broken -> no_progress

    run = author_dockerfile(tmp_path, COMPOSE_TARGET, _Broken(), runtime=None)
    first = run.iterations[0].report
    assert [r.check_id for r in first.results] == ["artifact_format"]
    assert first.results[0].failure_kind == "authoring"
    assert run.stopped_reason == "no_progress"
    assert run.iterations[0].compose is None


def test_author_single_artifact_contract_unchanged(tmp_path: Path) -> None:
    class _Plain:
        def generate(self, facts, target):
            return "FROM python:3.12-slim"

        def repair(self, facts, target, artifact_text, report):
            return "FROM python:3.12-slim"

    run = author_dockerfile(tmp_path, DeployTarget(), _Plain(), runtime=None)
    assert run.iterations[0].compose is None
    assert run.stopped_reason == "static_only"
```

Append to `tests/test_llm.py`:

```python
def test_system_prompt_carries_compose_rules() -> None:
    from deployer.artifacts import COMPOSE_SENTINEL, DOCKERFILE_SENTINEL

    assert DOCKERFILE_SENTINEL in SYSTEM_PROMPT
    assert COMPOSE_SENTINEL in SYSTEM_PROMPT
    assert "service_healthy" in SYSTEM_PROMPT
    assert "never declare ports" in SYSTEM_PROMPT
    assert 'named exactly "app"' in SYSTEM_PROMPT
```

Append to `tests/test_cli.py` (follow that file's project-setup helpers):

```python
def test_cli_verify_reads_compose_for_deps_target(tmp_path, capsys) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY main.py .\n")
    target = {
        "service": {"port": 8000},
        "dependencies": [{"name": "cache", "image": "redis:7-alpine"}],
    }
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target))
    # no compose.yaml -> compose_present FAILED -> exit 1
    code = main(
        ["verify", str(tmp_path), "--target", str(target_path), "--no-docker"]
        if _verify_supports_no_docker()
        else ["verify", str(tmp_path), "--target", str(target_path)]
    )
    assert code == 1
    assert "compose_present" in capsys.readouterr().out
```

Note for the implementer: `deployer verify` has no `--no-docker` flag — drop the conditional and run plain `verify`; on machines with a runtime this may attempt L2, so monkeypatch `deployer.cli.resolve_runtime` (or `_resolve_runtime_or_error`) to return `None` per the existing test_cli idiom for runtime-free tests. Write the test the way test_cli.py already writes runtime-free verify tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_author.py tests/test_llm.py -k "compose or artifact" -v`
Expected: FAIL

- [ ] **Step 3: Implement `src/deployer/author.py`**

- Import `ArtifactParseError, parse_artifact_response` from `deployer.artifacts` and `CheckResult, CheckStatus, FailureKind` from `deployer.models`.
- Rename the Protocol's `repair` third param to `artifact_text: str` and update both docstrings: authors return RAW response text (sentinel format when the target declares dependencies).
- Rework the loop body: thread `response: str | None` (raw text). Each iteration:

```python
            expects_compose = bool(target.dependencies)
            try:
                dockerfile, compose = parse_artifact_response(
                    response, expects_compose
                )
            except ArtifactParseError as exc:
                report = VerificationReport(
                    results=[
                        CheckResult(
                            check_id="artifact_format",
                            status=CheckStatus.FAILED,
                            failure_kind=FailureKind.AUTHORING,
                            message=str(exc),
                        )
                    ]
                )
                dockerfile, compose = response, None
            else:
                report = verify(
                    dockerfile,
                    project_path,
                    target,
                    runtime,
                    facts,
                    compose=compose,
                    build_timeout=build_timeout,
                    health_timeout=health_timeout,
                )
                if report.environment_failures and environment_retries == 0:
                    environment_retries += 1
                    report = verify(
                        dockerfile,
                        project_path,
                        target,
                        runtime,
                        facts,
                        compose=compose,
                        build_timeout=build_timeout,
                        health_timeout=health_timeout,
                    )
            iterations.append(
                IterationRecord(
                    index=index,
                    dockerfile=dockerfile,
                    compose=compose,
                    report=report,
                    duration_s=time.monotonic() - start,
                )
            )
```

  The rest of the loop (environment break, success/static_only, no_progress signature, repair call `author.repair(facts, target, response, report)`) keeps its current structure — only the threaded variable is `response` instead of `dockerfile`.

`src/deployer/llm.py`:

- Import the sentinels: `from deployer.artifacts import COMPOSE_SENTINEL, DOCKERFILE_SENTINEL`.
- Append a compose section to `SYSTEM_PROMPT` (inside the f-string, after the last rule):

```
- When the deploy intent declares "dependencies", you author TWO
  artifacts and reply with exactly two sections:
  {DOCKERFILE_SENTINEL}
  <the Dockerfile>
  {COMPOSE_SENTINEL}
  <the compose.yaml>
  Compose rules: the buildable service is named exactly "app" and
  builds from the project Dockerfile (build: {{context: ".",
  dockerfile: "Dockerfile"}}). Each dependency becomes a service using
  the intent's name and image verbatim, with a healthcheck you choose
  for that image. "app" must declare depends_on with
  condition: service_healthy for every dependency. Deploy-intent env
  goes into the app service environment; per-dependency env into that
  dependency's environment. Services never declare ports — compose
  networking is internal-only here; ingress is not this artifact's
  job. Without "dependencies", reply with only the Dockerfile as
  before — no sentinels.
```

  NOTE: the prompt is an f-string — literal `{` `}` in this section must be doubled (`{{context: ...}}` as shown).
- In `repair()`, rename the parameter to `artifact_text` and change the prompt line `f"Dockerfile:\n{dockerfile}\n\n"` to `f"Current artifacts:\n{artifact_text}\n\n"`.

`src/deployer/cli.py`:

- `_cmd_verify`: after reading the Dockerfile, add:

```python
    compose_path = project / "compose.yaml"
    compose = compose_path.read_text() if compose_path.is_file() else None
```

  and pass `compose=compose` to `verify(...)`. (A dependencies target without the file fails `compose_present` → exit 1; a plain target ignores it.)
- `_cmd_author`: after writing the Dockerfile from the last iteration, add:

```python
        last = run.iterations[-1]
        if last.compose is not None:
            (project / "compose.yaml").write_text(last.compose + "\n")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_author.py tests/test_llm.py tests/test_cli.py -v && uv run pytest`
Expected: all PASS (existing author tests keep passing — the single-artifact path is behavior-identical)

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/author.py src/deployer/llm.py src/deployer/cli.py tests/test_author.py tests/test_llm.py tests/test_cli.py
git commit -m "feat: two-artifact authoring loop, compose prompt rules, CLI compose IO"
```

---

### Task 6: Bench plumbing + `compose-redis` corpus case + README

**Files:**
- Modify: `src/deployer/bench.py` (`BenchCase.fixture_compose`, loader, `FixtureAuthor`, `run_case` skip, `verify_corpus`, `promote_run`)
- Create: `corpus/synthetic/compose-redis/{project/requirements.txt,project/main.py,fixture.Dockerfile,fixture.compose.yaml,target.json,expected.json}`
- Modify: `tests/test_corpus.py` (`EXPECTED_CASES`), `tests/test_bench.py`, `README.md`

**Interfaces:**
- Consumes: `render_artifact_response` (Task 2); author-loop raw-text contract (Task 5).
- Produces: `BenchCase.fixture_compose: Path | None`; `FixtureAuthor(dockerfile: str, compose: str | None = None)` returning `render_artifact_response(dockerfile, compose)` from both `generate` and `repair`; `verify_corpus` passes the fixture compose to `verify`; `promote_run` copies `compose.yaml` next to each promoted `Dockerfile`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_corpus.py`: insert `"compose-redis"` into `EXPECTED_CASES` between `"aaa"`-sorted neighbors — directory sort puts it FIRST (before `"entrypoint-override"`).

Append to `tests/test_bench.py` (follow its `_make_corpus` helper idiom):

```python
def test_fixture_author_renders_sentinels_when_compose_present() -> None:
    from deployer.artifacts import COMPOSE_SENTINEL
    from deployer.bench import FixtureAuthor

    author = FixtureAuthor("FROM x:1", compose="services: {}")
    out = author.generate(ProjectFacts(), DeployTarget())
    assert COMPOSE_SENTINEL in out
    plain = FixtureAuthor("FROM x:1")
    assert COMPOSE_SENTINEL not in plain.generate(ProjectFacts(), DeployTarget())


def test_run_case_skips_deps_case_without_fixture_compose(tmp_path: Path) -> None:
    # build a minimal corpus case with dependencies but no fixture.compose.yaml,
    # using this file's _make_corpus-style setup, then:
    #   result = run_case(case, FixtureAuthor(case.fixture_dockerfile.read_text()),
    #                     None, out_dir, build_timeout=60, health_timeout=5)
    # assert result.outcome == "skipped"
    # assert "fixture.compose.yaml" in result.skip_reason
    ...
```

(The second test's body follows the file's existing corpus-fabrication helper — write it concretely against `_make_corpus` when editing the file; the assertion pair above is the requirement.)

- [ ] **Step 2: Implement `src/deployer/bench.py`**

- `BenchCase`: add `fixture_compose: Path | None = None`.
- `load_corpus`: after the `fixture` line add:

```python
        fixture_compose = case_dir / "fixture.compose.yaml"
```

  and pass `fixture_compose=fixture_compose if fixture_compose.is_file() else None`.
- `FixtureAuthor`:

```python
    def __init__(self, dockerfile: str, compose: str | None = None) -> None:
        self._response = render_artifact_response(dockerfile, compose)
```

  `generate`/`repair` return `self._response`; `repair`'s third param renamed `artifact_text`; `info()` hashes `self._response`.
- `run_case`: after the existing `author is None` skip, add:

```python
    if case.target.dependencies and case.fixture_compose is None and isinstance(
        author, FixtureAuthor
    ):
        return BenchCaseResult(
            case=case.name,
            outcome="skipped",
            skip_reason="dependencies target has no fixture.compose.yaml",
            expected=case.expected,
        )
```

  and wherever the caller constructs `FixtureAuthor` from a case (see `run_bench`/CLI), pass `compose=case.fixture_compose.read_text()` when present.
- `verify_corpus`: pass `compose=case.fixture_compose.read_text() if case.fixture_compose else None` into `verify`.
- `run_case` artifact persistence: where the case out-dir writes `Dockerfile` from the last iteration, also write `compose.yaml` when `iteration.compose is not None`.
- `promote_run`: next to the `Dockerfile` copy, add:

```python
        compose_src = run_dir / "cases" / case.case / "compose.yaml"
        if compose_src.is_file():
            shutil.copyfile(compose_src, dest / "compose.yaml")
```

  (guard: `dest` may need creating even when only compose exists — keep the existing mkdir logic shared.)

- [ ] **Step 3: Create the corpus case**

`corpus/synthetic/compose-redis/project/requirements.txt`:

```
flask
redis
```

`corpus/synthetic/compose-redis/project/main.py`:

```python
"""Compose fixture: Flask service whose /health proves Redis wiring."""

import os

import redis
from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health() -> str:
    redis.Redis.from_url(os.environ["REDIS_URL"]).ping()
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

`corpus/synthetic/compose-redis/target.json`:

```json
{"service": {"port": 8000, "healthcheck_path": "/health"},
 "env": {"REDIS_URL": "redis://cache:6379/0"},
 "dependencies": [{"name": "cache", "image": "redis:7-alpine"}]}
```

`corpus/synthetic/compose-redis/expected.json`:

```json
{"capabilities": ["compose", "service"], "notes": "app + redis dep; /health does redis.ping() so a green healthcheck proves env wiring, depends_on and the dep healthcheck — not merely app startup"}
```

`corpus/synthetic/compose-redis/fixture.Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
CMD ["python", "main.py"]
```

`corpus/synthetic/compose-redis/fixture.compose.yaml`:

```yaml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      REDIS_URL: redis://cache:6379/0
    depends_on:
      cache:
        condition: service_healthy
  cache:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 15
```

- [ ] **Step 4: Update README**

In the artifact/facts paragraph, extend with one sentence:

```
A deploy target may declare pinned infra `dependencies` (redis,
postgres, ...): the model then authors a compose.yaml next to the
Dockerfile and verification runs `compose up` with an in-network
healthcheck probe — no host ports are ever published.
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_corpus.py -k "not docker" -v && uv run pytest`
Expected: all PASS — in particular `test_corpus_static_checks_pass_for_every_fixture` proves the fixture pair passes all compose L1 checks. (It calls `verify(...)`; make sure it passes the fixture compose — that call site was updated in Step 2's `verify_corpus`/test path. If `test_corpus.py`'s own `verify(...)` call needs the compose kwarg, add `compose=case.fixture_compose.read_text() if case.fixture_compose else None` there too.)

- [ ] **Step 6: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/bench.py corpus/synthetic/compose-redis tests/test_corpus.py tests/test_bench.py README.md
git commit -m "feat: compose-redis corpus case + bench compose plumbing"
```

---

### Task 7: End-to-end gates and PR

**Files:** none new (verification + PR only)

- [ ] **Step 1: Full unit suite and gates**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
```

Expected: all PASS, no diffs, no findings

- [ ] **Step 2: Docker-marked tests** (local podman has a compose provider — verified: podman-compose via `podman compose`)

Run: `uv run pytest -m docker`
Expected: PASS including the compose-redis e2e; afterwards verify teardown left nothing:

```bash
podman ps -a --format '{{.Names}}' | grep deployer-verify- || echo clean
```

Expected: `clean`

- [ ] **Step 3: Fixture bench 10/10**

```bash
uv run deployer bench run --author fixture --label compose-support
```

Expected: 10/10 matched, success rate 1.0

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feature/compose
gh pr create --title "feat: compose (multi-service) artifact — dependencies contract, two-artifact authoring, deterministic compose verifier" --body "Implements docs/superpowers/specs/2026-07-22-compose-support-design.md ..."
```

PR body must cover: contract, sentinel authoring, L1 trio, L2 semantics (unique project name, isolated context, in-network probe, FAILED/ENVIRONMENT provider rule, no ports), corpus case, evidence (test counts, bench, teardown check). End the body with the standard generation footer. Then follow the repo git workflow: read Copilot review, fix valid findings, reply to invalid ones; do NOT merge.

- [ ] **Step 5 (post-green follow-up, separate chore commit):** LLM acceptance + golden re-promote:

```bash
uv run deployer bench run --author anthropic --label llm-compose
uv run deployer bench compare .deployer-runs/<run-dir> golden
uv run deployer bench promote .deployer-runs/<run-dir>
```

Run the bench detached/no-timeout (a 10-case LLM+docker run exceeds 10 minutes).

---

## Self-review notes

- Spec coverage: D1 → Task 1; D2 → Tasks 2+5 (parser, prompt, repair-sees-both via raw text); D3 → Task 3; D4 → Task 4 (probe semantics, project name, isolated context, teardown, memory_limit note); D5 → Task 6; D6 → Task 5 (CLI) + Task 6 (promote); acceptance → Tasks 6–7 (README, gates, bench, teardown check, LLM golden).
- Type consistency: `parse_artifact_response(text, expects_compose) -> tuple[str, str | None]` used in Tasks 2/5/6; `_compose_l1_checks(compose, target)` Tasks 3/4; `_verify_compose(dockerfile, compose, project_path, target, runtime, *, build_timeout, health_timeout)` Task 4; `FixtureAuthor(dockerfile, compose=None)` Task 6; `repair(..., artifact_text, report)` Tasks 5/6.
- Known deliberate gaps: `image_size_bytes` None on compose path (documented); `run_case` FixtureAuthor-skip uses isinstance (LLM authors run deps cases without fixtures); Task 6's second bench test body is written against the file's `_make_corpus` helper at edit time (assertions specified).
