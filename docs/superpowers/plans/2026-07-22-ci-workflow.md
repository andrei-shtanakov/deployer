# CI Workflow Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Third artifact type — a build-image GitHub Actions workflow authored atomically with the Dockerfile and verified by five static L1 checks (incl. pinned actionlint).

**Architecture:** `DeployTarget.ci: CISpec | None` requests the workflow; the sentinel parser generalizes to a third section `=== ci.yml ===`; `verify` gains a `ci` kwarg and an explicit check cascade (`ci_present → ci_parses → {ci_wiring, ci_pinned, actionlint}`) with SKIPPED dependents; the prompt carries a verified `ACTIONS_CHECKOUT_PIN` SHA so the model never invents pins. No CI L2 — workflows are never executed.

**Tech Stack:** Python 3.12, pydantic, PyYAML, actionlint 1.7.12 (optional binary, hadolint pattern), pytest, uv.

**Spec:** `docs/superpowers/specs/2026-07-22-ci-workflow-design.md` — read it first.

## Global Constraints

- Branch: `feature/ci-workflow` (exists, holds the spec commit). Never commit to `master`.
- `CISpec` has NO fields (presence is the request); `ci` + `dependencies` rejected in the `DeployTarget` model validator.
- Sentinel verbatim: `=== ci.yml ===`; section order Dockerfile → compose → ci. Canonical path `<project>/.github/workflows/ci.yml`.
- Cascade: a failed prerequisite gives dependents SKIPPED, never cascading FAILED; `ci_wiring` ⊥ `ci_pinned`; actionlint needs only `ci_parses`.
- `ci_pinned`: ONLY remote `owner/repo@<40-hex-sha>`; `@vN`/`@main`/no-`@`/local `./`/`docker://` all FAILED.
- actionlint: `ACTIONLINT_VERSION = "1.7.12"`; missing binary OR version mismatch → SKIPPED + run non-comparable (mismatched version is NEVER executed); runs against a real temp `.github/workflows/ci.yml` file, not stdin.
- `verify` signature is exactly the spec's: positional params unchanged, keyword-only `compose` and `ci`.
- Runner: fixed label `ubuntu-24.04` (deterministic label, not an immutable pin) — prompt/fixture only, not L1-enforced.
- All five CI checks classify failures as `failure_kind="authoring"`.
- Checkout pin: `actions/checkout` v5.0.1, candidate SHA `93cb6efe18208431cddfb8368fd83d5badbf9bfd` — MUST be verified against the tag deref before use (Task 4 Step 1).
- After every task: `uv run ruff format . && uv run ruff check . && uv run pyrefly check` clean; full `uv run pytest` green.
- 88-char lines, type hints, docstrings on new public helpers, follow existing file patterns.

---

### Task 1: Contract — `CISpec`, `DeployTarget.ci`, `IterationRecord.ci`

**Files:**
- Modify: `src/deployer/models.py` (new model after `ServiceDependency`; `DeployTarget` field + validator; `IterationRecord`)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `CISpec(BaseModel)` (no fields); `DeployTarget.ci: CISpec | None = None`; `IterationRecord.ci: str | None = None`. Later tasks branch on `target.ci is not None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_models.py`)

```python
def test_ci_spec_presence_is_the_request() -> None:
    from deployer.models import CISpec

    target = DeployTarget.model_validate_json('{"ci": {}}')
    assert isinstance(target.ci, CISpec)
    assert DeployTarget().ci is None


def test_ci_composes_with_service_and_run() -> None:
    from deployer.models import CISpec

    DeployTarget(ci=CISpec(), service=ServiceSpec(port=8000))
    DeployTarget(ci=CISpec(), run=RunSpec())
    DeployTarget(ci=CISpec())  # build-only


def test_ci_with_dependencies_rejected() -> None:
    from deployer.models import CISpec, ServiceDependency

    with pytest.raises(ValidationError):
        DeployTarget(
            ci=CISpec(),
            service=ServiceSpec(port=8000),
            dependencies=[ServiceDependency(name="cache", image="redis:7-alpine")],
        )


def test_iteration_record_ci_defaults_none() -> None:
    from deployer.models import IterationRecord, VerificationReport

    rec = IterationRecord(
        index=0, dockerfile="FROM x:1", report=VerificationReport(), duration_s=0.1
    )
    assert rec.ci is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -k "ci_" -v`
Expected: FAIL (ImportError: CISpec)

- [ ] **Step 3: Implement in `src/deployer/models.py`**

After `ServiceDependency`:

```python
class CISpec(BaseModel):
    """Request for a build-image CI workflow. Presence is the request.

    Deliberately empty: no kind/registry/triggers until a second
    implemented workflow kind exists — a discriminator now would be
    false extensibility.
    """
```

In `DeployTarget`: add `ci: CISpec | None = None` after `dependencies`, and a validator after `_dependencies_require_service`:

```python
    @model_validator(mode="after")
    def _ci_incompatible_with_dependencies(self) -> "DeployTarget":
        if self.ci is not None and self.dependencies:
            raise ValueError(
                "DeployTarget.ci with dependencies is unsupported: "
                "compose-aware CI is a later iteration"
            )
        return self
```

In `IterationRecord`: add `ci: str | None = None` after `compose`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_models.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/models.py tests/test_models.py
git commit -m "feat: CISpec contract — DeployTarget.ci, ci x dependencies rejected"
```

---

### Task 2: Artifacts — third sentinel section, 3-tuple parse

**Files:**
- Modify: `src/deployer/artifacts.py`
- Modify: `src/deployer/author.py:117` (mechanical call-site update only)
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Consumes: existing line-anchored `_sentinel_line_indices`.
- Produces: `CI_SENTINEL = "=== ci.yml ==="`; `parse_artifact_response(text: str, expects_compose: bool, expects_ci: bool = False) -> tuple[str, str | None, str | None]`; `render_artifact_response(dockerfile: str, compose: str | None = None, ci: str | None = None) -> str`. Tasks 4–5 rely on these exact signatures.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_artifacts.py`; also mechanically update the existing tests' expected tuples from 2-tuple to 3-tuple)

```python
CI_RESPONSE = (
    f"{DOCKERFILE_SENTINEL}\nFROM python:3.12-slim\n"
    f"{CI_SENTINEL}\nname: ci\n"
)


def test_parse_ci_only_section() -> None:
    dockerfile, compose, ci = parse_artifact_response(
        CI_RESPONSE, expects_compose=False, expects_ci=True
    )
    assert dockerfile == "FROM python:3.12-slim"
    assert compose is None
    assert ci == "name: ci"


def test_parse_all_three_sections() -> None:
    text = (
        f"{DOCKERFILE_SENTINEL}\nFROM x:1\n"
        f"{COMPOSE_SENTINEL}\nservices: {{}}\n"
        f"{CI_SENTINEL}\nname: ci\n"
    )
    assert parse_artifact_response(text, True, True) == (
        "FROM x:1",
        "services: {}",
        "name: ci",
    )


def test_parse_ci_out_of_order_raises() -> None:
    text = (
        f"{CI_SENTINEL}\nname: ci\n{DOCKERFILE_SENTINEL}\nFROM x:1\n"
    )
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(text, False, True)


def test_parse_missing_ci_section_raises() -> None:
    with pytest.raises(ArtifactParseError):
        parse_artifact_response("FROM x:1", False, True)


def test_render_three_sections_round_trips() -> None:
    text = render_artifact_response("FROM x:1", "services: {}", "name: ci")
    assert parse_artifact_response(text, True, True) == (
        "FROM x:1",
        "services: {}",
        "name: ci",
    )
    ci_only = render_artifact_response("FROM x:1", ci="name: ci")
    assert parse_artifact_response(ci_only, False, True) == (
        "FROM x:1",
        None,
        "name: ci",
    )
    assert render_artifact_response("FROM x:1") == "FROM x:1"
```

(Import `CI_SENTINEL` in the test file's import block. Existing tests: every `parse_artifact_response(...)` expectation gains a trailing `None`; the no-deps passthrough becomes `("FROM python:3.12-slim", None, None)`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_artifacts.py -v`
Expected: FAIL (ImportError: CI_SENTINEL)

- [ ] **Step 3: Implement — generalize to an expected-section list**

Replace `parse_artifact_response` and `render_artifact_response` in `src/deployer/artifacts.py` (keep `_sentinel_line_indices`, the module docstring, `ArtifactParseError`); add `CI_SENTINEL = "=== ci.yml ==="` below `COMPOSE_SENTINEL`:

```python
def parse_artifact_response(
    text: str, expects_compose: bool, expects_ci: bool = False
) -> tuple[str, str | None, str | None]:
    """Split a raw author response into (dockerfile, compose, ci).

    With no extra sections expected the whole text is the Dockerfile —
    the single-artifact contract is unchanged. Otherwise sentinels are
    matched line-anchored (a line counts only when its stripped content
    equals the sentinel exactly), sections must appear in the order
    Dockerfile -> compose -> ci, each exactly once and non-empty.
    Prose before the Dockerfile sentinel is dropped as chatter.
    """
    expected = [DOCKERFILE_SENTINEL]
    if expects_compose:
        expected.append(COMPOSE_SENTINEL)
    if expects_ci:
        expected.append(CI_SENTINEL)
    if len(expected) == 1:
        return text.strip(), None, None
    lines = text.splitlines()
    listed = ", ".join(repr(s) for s in expected)
    positions: list[int] = []
    for sentinel in expected:
        idxs = _sentinel_line_indices(lines, sentinel)
        if len(idxs) != 1:
            raise ArtifactParseError(
                f"response must contain the line {sentinel!r} exactly once "
                f"(found {len(idxs)}); reply with one section per expected "
                f"sentinel ({listed}), each sentinel on its own line"
            )
        positions.append(idxs[0])
    if positions != sorted(positions):
        order = " -> ".join(repr(s) for s in expected)
        raise ArtifactParseError(f"sections must appear in order: {order}")
    bounds = positions[1:] + [len(lines)]
    contents: list[str] = []
    for start, end in zip(positions, bounds):
        section = "\n".join(lines[start + 1 : end]).strip()
        if not section:
            raise ArtifactParseError("every artifact section must be non-empty")
        contents.append(section)
    dockerfile = contents[0]
    compose = contents[1] if expects_compose else None
    ci = contents[-1] if expects_ci else None
    return dockerfile, compose, ci


def render_artifact_response(
    dockerfile: str, compose: str | None = None, ci: str | None = None
) -> str:
    """Inverse of parse: the format fixture authors and prompts use."""
    if compose is None and ci is None:
        return dockerfile
    parts = [DOCKERFILE_SENTINEL, dockerfile]
    if compose is not None:
        parts.extend([COMPOSE_SENTINEL, compose])
    if ci is not None:
        parts.extend([CI_SENTINEL, ci])
    return "\n".join(parts)
```

Mechanical call-site update in `src/deployer/author.py:117`:

```python
                dockerfile, compose, _ci = parse_artifact_response(
                    response, expects_compose
                )
```

(`_ci` is unused until Task 4 threads it; name it `_ci` so ruff accepts the unused binding — or use `dockerfile, compose, _ = ...`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_artifacts.py tests/test_author.py -v && uv run pytest`
Expected: all PASS (author loop behavior unchanged)

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/artifacts.py src/deployer/author.py tests/test_artifacts.py
git commit -m "feat: third sentinel section === ci.yml === (3-tuple parse/render)"
```

---

### Task 3: Verifier — five CI checks with explicit cascade

**Files:**
- Modify: `src/deployer/verify.py` (constants, `_ci_l1_checks` + helpers, `_check_actionlint`, `verify` signature + wiring; `VerificationReport.actionlint_available` in `src/deployer/models.py`)
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `target.ci` (Task 1), `_check_failed`/`_check_passed` helpers, `yaml`, `_tail`.
- Produces: `_ci_l1_checks(ci: str | None) -> tuple[list[CheckResult], bool]` (results, actionlint_available) with check ids `ci_present`, `ci_parses`, `ci_wiring`, `ci_pinned`, `actionlint`; `ACTIONLINT_VERSION = "1.7.12"`; `verify(..., *, compose=None, ci=None, ...)` appends them when `target.ci is not None` and sets `report.actionlint_available`. `VerificationReport.actionlint_available: bool = False`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_verify_static.py`)

```python
GOOD_SHA = "a" * 40

CI_GOOD = f"""\
name: ci
on:
  push:
  pull_request:
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@{GOOD_SHA}
      - run: docker build --file ./Dockerfile .
"""


def _ci_checks(ci: str | None):
    from deployer.verify import _ci_l1_checks

    results, _ = _ci_l1_checks(ci)
    return {r.check_id: r for r in results}


def test_ci_good_passes_own_checks(monkeypatch) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    checks = _ci_checks(CI_GOOD)
    for check_id in ("ci_present", "ci_parses", "ci_wiring", "ci_pinned"):
        assert checks[check_id].status is CheckStatus.PASSED, check_id
    assert checks["actionlint"].status is CheckStatus.SKIPPED


def test_ci_missing_artifact_fails_present_and_skips_rest() -> None:
    checks = _ci_checks(None)
    assert checks["ci_present"].status is CheckStatus.FAILED
    assert checks["ci_present"].failure_kind == "authoring"
    for dep in ("ci_parses", "ci_wiring", "ci_pinned", "actionlint"):
        assert checks[dep].status is CheckStatus.SKIPPED, dep


def test_ci_unparseable_fails_parses_and_skips_dependents() -> None:
    checks = _ci_checks("on: [unclosed")
    assert checks["ci_parses"].status is CheckStatus.FAILED
    for dep in ("ci_wiring", "ci_pinned", "actionlint"):
        assert checks[dep].status is CheckStatus.SKIPPED, dep


def test_ci_on_true_key_normalized(monkeypatch) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    # unquoted `on:` -> YAML 1.1 boolean True key; must still parse+wire
    checks = _ci_checks(CI_GOOD)  # CI_GOOD's `on:` IS the True key
    assert checks["ci_parses"].status is CheckStatus.PASSED
    assert checks["ci_wiring"].status is CheckStatus.PASSED


def test_ci_both_on_keys_ambiguous_fails() -> None:
    text = CI_GOOD.replace("name: ci", 'name: ci\n"on":\n  push:')
    checks = _ci_checks(text)
    assert checks["ci_parses"].status is CheckStatus.FAILED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("  pull_request:\n", ""),  # missing trigger
        lambda t: t.replace("pull_request", "pull_request_target"),
        lambda t: t.replace("--file ./Dockerfile", "--file /abs/Dockerfile"),
        lambda t: t.replace("--file ./Dockerfile", "--file other.Dockerfile"),
        lambda t: t.replace(
            "      - uses: actions/checkout@" + GOOD_SHA + "\n"
            "      - run: docker build --file ./Dockerfile .",
            "      - run: docker build --file ./Dockerfile .\n"
            "      - uses: actions/checkout@" + GOOD_SHA,
        ),  # checkout after build
        lambda t: t + "      - run: docker push ghcr.io/x/y\n",
        lambda t: t.replace(
            "docker build --file ./Dockerfile .",
            "docker buildx build --push --file ./Dockerfile .",
        ),
        lambda t: t + "      - run: docker login ghcr.io\n",
        lambda t: t + f"      - uses: docker/login-action@{GOOD_SHA}\n",
        lambda t: t + "      - run: echo ${{ secrets.TOKEN }}\n",
    ],
)
def test_ci_wiring_negatives(mutate) -> None:
    checks = _ci_checks(mutate(CI_GOOD))
    assert checks["ci_wiring"].status is CheckStatus.FAILED


@pytest.mark.parametrize(
    "build_cmd",
    [
        "docker build .",
        "docker build -f Dockerfile .",
        "docker build -f ./Dockerfile .",
        "docker build --file=./Dockerfile .",
    ],
)
def test_ci_wiring_accepts_build_forms(build_cmd: str) -> None:
    text = CI_GOOD.replace("docker build --file ./Dockerfile .", build_cmd)
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.PASSED


def test_ci_push_trigger_does_not_trip_push_rule() -> None:
    assert _ci_checks(CI_GOOD)["ci_wiring"].status is CheckStatus.PASSED


def test_ci_multiline_run_scalar_parsed() -> None:
    text = CI_GOOD.replace(
        "      - run: docker build --file ./Dockerfile .",
        "      - run: |\n"
        "          # build the image\n"
        "          docker build --file ./Dockerfile .",
    )
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.PASSED


def test_ci_checkout_and_build_must_share_a_job() -> None:
    text = CI_GOOD.replace(
        "      - run: docker build --file ./Dockerfile .",
        "",
    ) + (
        "  build2:\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n"
        "      - run: docker build --file ./Dockerfile .\n"
    )
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.FAILED


@pytest.mark.parametrize(
    "uses",
    [
        "actions/checkout@v5",
        "actions/checkout@main",
        "actions/checkout",
        "./local-action",
        "docker://alpine:3.20",
    ],
)
def test_ci_pinned_rejects_non_sha_refs(uses: str) -> None:
    text = CI_GOOD.replace(f"actions/checkout@{GOOD_SHA}", uses)
    assert _ci_checks(text)["ci_pinned"].status is CheckStatus.FAILED


def test_ci_pinned_accepts_remote_sha() -> None:
    assert _ci_checks(CI_GOOD)["ci_pinned"].status is CheckStatus.PASSED


def test_actionlint_version_mismatch_skips_without_running(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        "deployer.verify.shutil.which", lambda _: "/usr/bin/actionlint"
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="9.9.9", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    checks = _ci_checks(CI_GOOD)
    assert checks["actionlint"].status is CheckStatus.SKIPPED
    assert len(calls) == 1  # only --version; the linter itself never ran


def test_actionlint_runs_against_real_workflow_path(monkeypatch) -> None:
    import subprocess

    from deployer.verify import ACTIONLINT_VERSION

    monkeypatch.setattr(
        "deployer.verify.shutil.which", lambda _: "/usr/bin/actionlint"
    )
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        if "--version" in cmd or "-version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=ACTIONLINT_VERSION, stderr=""
            )
        seen.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    results, available = __import__(
        "deployer.verify", fromlist=["_ci_l1_checks"]
    )._ci_l1_checks(CI_GOOD)
    assert available is True
    assert seen and seen[0].endswith(".github/workflows/ci.yml")


def test_verify_appends_ci_checks_only_for_ci_target(hello_service: Path) -> None:
    from deployer.models import CISpec

    target = DeployTarget(ci=CISpec())
    report = verify(GOOD, hello_service, target, None, ci=CI_GOOD)
    ids = [r.check_id for r in report.results]
    assert "ci_wiring" in ids and "ci_pinned" in ids

    plain = verify(GOOD, hello_service, DeployTarget(), None)
    assert "ci_present" not in [r.check_id for r in plain.results]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -k "ci_ or actionlint" -v`
Expected: FAIL (ImportError: `_ci_l1_checks`)

- [ ] **Step 3: Implement in `src/deployer/verify.py` (+ one field in models)**

`src/deployer/models.py`: add to `VerificationReport` after `hadolint_available`:

```python
    actionlint_available: bool = False
```

`src/deployer/verify.py` — constants near `HADOLINT_VERSION`:

```python
ACTIONLINT_VERSION = "1.7.12"
_USES_REMOTE_PIN = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$"
)
_DOCKER_BUILD = re.compile(r"^docker\s+(?:buildx\s+)?build\b")
_DOCKER_PUSH = re.compile(r"\bdocker\s+push\b")
_DOCKER_LOGIN = re.compile(r"\bdocker\s+login\b")
_SECRETS_REF = re.compile(r"\bsecrets\.")
```

Helpers + checks (after the compose L1 block):

```python
def _ci_skipped(reason: str, *check_ids: str) -> list[CheckResult]:
    return [
        CheckResult(check_id=cid, status=CheckStatus.SKIPPED, message=reason)
        for cid in check_ids
    ]


def _ci_triggers(workflow: dict) -> set[str] | None:
    """Trigger names, normalizing the YAML 1.1 `on` -> True key.

    Returns None when BOTH "on" and True are present (ambiguous) or
    when neither is — callers turn that into a ci_parses failure.
    """
    keys = [k for k in ("on", True) if k in workflow]
    if len(keys) != 1:
        return None
    value = workflow[keys[0]]
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {v for v in value if isinstance(v, str)}
    if isinstance(value, dict):
        return {k for k in value if isinstance(k, str)}
    return set()


def _run_lines_of(step: dict) -> list[str]:
    """Non-empty, non-comment lines of a step's run scalar."""
    run = step.get("run")
    if not isinstance(run, str):
        return []
    lines = []
    for raw in run.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _build_line_ok(line: str) -> bool:
    """Accept docker build forms targeting the project Dockerfile.

    `docker build .` (default ./Dockerfile), `-f Dockerfile`,
    `-f ./Dockerfile`, `--file=./Dockerfile` — flag with space or `=`.
    Any other path (absolute, renamed) is rejected.
    """
    if not _DOCKER_BUILD.match(line) or "--push" in line.split():
        return False
    tokens = line.split()
    file_value: str | None = None
    for i, tok in enumerate(tokens):
        if tok in ("-f", "--file"):
            if i + 1 < len(tokens):
                file_value = tokens[i + 1]
        elif tok.startswith(("--file=", "-f=")):
            file_value = tok.split("=", 1)[1]
    if file_value is None:
        return tokens[-1] == "."
    return file_value in ("Dockerfile", "./Dockerfile") and tokens[-1] == "."


def _ci_wiring_problems(workflow: dict) -> list[str]:
    problems: list[str] = []
    triggers = _ci_triggers(workflow)
    assert triggers is not None  # ci_parses guarantees unambiguous `on`
    for wanted in ("push", "pull_request"):
        if wanted not in triggers:
            problems.append(f"workflow must trigger on {wanted}")
    if "pull_request_target" in triggers:
        problems.append("pull_request_target is forbidden (security)")
    jobs = workflow.get("jobs", {})
    paired = False
    for job in jobs.values():
        steps = job.get("steps") or []
        checkout_at: int | None = None
        build_at: int | None = None
        for i, step in enumerate(steps):
            uses = step.get("uses")
            if (
                isinstance(uses, str)
                and uses.split("@")[0] == "actions/checkout"
                and checkout_at is None
            ):
                checkout_at = i
            if build_at is None and any(
                _build_line_ok(line) for line in _run_lines_of(step)
            ):
                build_at = i
        if checkout_at is not None and build_at is not None:
            if checkout_at < build_at:
                paired = True
            else:
                problems.append(
                    "checkout must precede docker build in the same job"
                )
    if not paired and not any(
        "checkout must precede" in p for p in problems
    ):
        problems.append(
            "no job pairs an actions/checkout step with a "
            "`docker build` of the project Dockerfile"
        )
    for job in jobs.values():
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if isinstance(uses, str) and "login-action" in uses:
                problems.append(f"login action is out of the MVP: {uses}")
            with_block = step.get("with")
            if isinstance(with_block, dict) and with_block.get("push") is True:
                problems.append("push: true input is out of the MVP")
            for line in _run_lines_of(step):
                if _DOCKER_PUSH.search(line):
                    problems.append("docker push is out of the MVP")
                if _DOCKER_LOGIN.search(line):
                    problems.append("docker login is out of the MVP")
                if _DOCKER_BUILD.match(line) and "--push" in line.split():
                    problems.append("buildx --push is out of the MVP")
    return problems


def _ci_pinned_problems(workflow: dict) -> list[str]:
    problems: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            if uses.startswith("./"):
                problems.append(f"local action is out of the MVP: {uses}")
            elif uses.startswith("docker://"):
                problems.append(f"docker action is out of the MVP: {uses}")
            elif not _USES_REMOTE_PIN.fullmatch(uses):
                problems.append(
                    f"uses must be pinned to a 40-hex commit SHA: {uses}"
                )
    return problems


def _check_actionlint(ci: str) -> tuple[CheckResult, bool]:
    """actionlint at the pinned version; (result, available_and_comparable).

    Runs against a real temporary .github/workflows/ci.yml (actionlint
    applies path-based context); a mismatched version is never executed.
    """
    binary = shutil.which("actionlint")
    if binary is None:
        return (
            CheckResult(
                check_id="actionlint",
                status=CheckStatus.SKIPPED,
                message=f"actionlint {ACTIONLINT_VERSION} not installed; "
                "run is non-comparable",
            ),
            False,
        )
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        if ACTIONLINT_VERSION not in version:
            return (
                CheckResult(
                    check_id="actionlint",
                    status=CheckStatus.SKIPPED,
                    message=(
                        f"actionlint version mismatch (want "
                        f"{ACTIONLINT_VERSION}, got: "
                        f"{version.strip().splitlines()[0] if version.strip() else '?'}"
                        "); run is non-comparable"
                    ),
                ),
                False,
            )
        with tempfile.TemporaryDirectory(prefix="deployer-ci-") as tmp:
            wf_dir = Path(tmp) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            wf_path = wf_dir / "ci.yml"
            wf_path.write_text(ci + "\n")
            proc = subprocess.run(
                [binary, "-no-color", str(wf_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        if proc.returncode == 0:
            return (CheckResult(check_id="actionlint", status=CheckStatus.PASSED), True)
        return (
            CheckResult(
                check_id="actionlint",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message=_tail(proc.stdout or proc.stderr),
            ),
            True,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (
            CheckResult(
                check_id="actionlint",
                status=CheckStatus.SKIPPED,
                message=f"actionlint execution failed ({exc.__class__.__name__}); "
                "run is non-comparable",
            ),
            False,
        )


def _ci_l1_checks(ci: str | None) -> tuple[list[CheckResult], bool]:
    """Static checks for the CI artifact of a ci-target.

    Cascade: ci_present -> ci_parses -> {ci_wiring, ci_pinned,
    actionlint}; a failed prerequisite gives dependents SKIPPED,
    never a cascading FAILED. No CI L2 exists: workflows are never
    executed.
    """
    if ci is None:
        return (
            [
                _check_failed(
                    "ci_present",
                    "deploy target requests a CI workflow but no ci.yml "
                    "artifact was provided",
                ),
                *_ci_skipped(
                    "skipped: ci_present failed",
                    "ci_parses",
                    "ci_wiring",
                    "ci_pinned",
                    "actionlint",
                ),
            ],
            False,
        )
    results = [_check_passed("ci_present")]

    parse_problem: str | None = None
    workflow: dict = {}
    try:
        doc = yaml.safe_load(ci)
    except yaml.YAMLError as exc:
        parse_problem = f"ci.yml: {exc}"
    else:
        if not isinstance(doc, dict):
            parse_problem = "ci.yml must be a mapping"
        elif _ci_triggers(doc) is None:
            parse_problem = (
                "workflow must have exactly one trigger key "
                '("on"; YAML 1.1 may parse it as boolean True — both at '
                "once is ambiguous)"
            )
        else:
            jobs = doc.get("jobs")
            if not isinstance(jobs, dict) or not jobs or not all(
                isinstance(j, dict) and isinstance(j.get("steps"), list)
                and all(isinstance(s, dict) for s in j.get("steps") or [])
                for j in jobs.values()
            ):
                parse_problem = (
                    "workflow must define a `jobs` mapping whose jobs "
                    "carry `steps` lists of mappings"
                )
            else:
                workflow = doc
    if parse_problem is not None:
        results.append(_check_failed("ci_parses", parse_problem))
        results.extend(
            _ci_skipped(
                "skipped: ci_parses failed",
                "ci_wiring",
                "ci_pinned",
                "actionlint",
            )
        )
        return results, False
    results.append(_check_passed("ci_parses"))

    wiring = _ci_wiring_problems(workflow)
    results.append(
        _check_failed("ci_wiring", "; ".join(wiring))
        if wiring
        else _check_passed("ci_wiring")
    )
    pinned = _ci_pinned_problems(workflow)
    results.append(
        _check_failed("ci_pinned", "; ".join(pinned))
        if pinned
        else _check_passed("ci_pinned")
    )
    lint_result, lint_available = _check_actionlint(ci)
    results.append(lint_result)
    return results, lint_available
```

Also scan the raw text for secrets (covers expressions outside parsed values): in `_ci_wiring_problems`, add as the first line:

```python
    problems: list[str] = []
    # raw-text sweep: secrets must not appear anywhere, incl. ${{ }}
```

and pass the raw text in — change the signature to `_ci_wiring_problems(workflow: dict, raw: str)` and append at the end:

```python
    if _SECRETS_REF.search(raw):
        problems.append("secrets.* references are out of the MVP")
    return problems
```

(call it as `_ci_wiring_problems(workflow, ci)`).

`verify()`: add `ci: str | None = None` keyword to the exact spec signature, and after the compose-L1 insertion:

```python
    if target.ci is not None:
        ci_results, actionlint_available = _ci_l1_checks(ci)
        report.results.extend(ci_results)
        report.actionlint_available = actionlint_available
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_verify_static.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/verify.py src/deployer/models.py tests/test_verify_static.py
git commit -m "feat: CI L1 checks — present/parses/wiring/pinned + pinned actionlint"
```

---

### Task 4: Author loop threading, prompt pin, CLI IO

**Files:**
- Modify: `src/deployer/author.py` (thread `ci` through parse/verify/record)
- Modify: `src/deployer/llm.py` (`ACTIONS_CHECKOUT_PIN`, CI prompt section)
- Modify: `src/deployer/cli.py` (verify reads / author writes `.github/workflows/ci.yml`)
- Test: `tests/test_author.py`, `tests/test_llm.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `parse_artifact_response(response, expects_compose, expects_ci)` (Task 2), `verify(..., ci=ci)` (Task 3), `IterationRecord.ci` (Task 1).
- Produces: `ACTIONS_CHECKOUT_PIN: str` in `llm.py` (Task 5's fixture pin-drift test imports it); author loop records `IterationRecord(ci=...)`; CLI canonical path `<project>/.github/workflows/ci.yml`.

- [ ] **Step 1: Verify the checkout pin SHA (annotated-tag deref)**

```bash
git ls-remote https://github.com/actions/checkout.git 'v5.0.1' 'v5.0.1^{}'
```

Expected: one or two lines; use the `^{}` (dereferenced commit) SHA if present, else the plain tag SHA. Candidate from planning: `93cb6efe18208431cddfb8368fd83d5badbf9bfd` (v5.0.1). Use whatever this command outputs as the commit SHA — do not trust the plan's candidate blindly.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_llm.py`:

```python
def test_system_prompt_carries_ci_rules() -> None:
    from deployer.artifacts import CI_SENTINEL
    from deployer.llm import ACTIONS_CHECKOUT_PIN, SYSTEM_PROMPT

    assert CI_SENTINEL in SYSTEM_PROMPT
    assert ACTIONS_CHECKOUT_PIN in SYSTEM_PROMPT
    assert "ubuntu-24.04" in SYSTEM_PROMPT
    assert "pull_request" in SYSTEM_PROMPT
    assert "never push" in SYSTEM_PROMPT


def test_actions_checkout_pin_shape() -> None:
    import re

    from deployer.llm import ACTIONS_CHECKOUT_PIN

    assert re.fullmatch(
        r"actions/checkout@[0-9a-f]{40}", ACTIONS_CHECKOUT_PIN
    )
```

Append to `tests/test_author.py` (reuse the file's stub-author idiom):

```python
CI_TARGET = DeployTarget.model_validate_json('{"ci": {}}')


def test_author_records_ci_artifact(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")

    class _CIAuthor:
        def generate(self, facts, target):
            return render_artifact_response(
                "FROM python:3.12-slim\nCOPY main.py .", ci="name: ci"
            )

        def repair(self, facts, target, artifact_text, report, /):
            return self.generate(facts, target)

    run = author_dockerfile(tmp_path, CI_TARGET, _CIAuthor(), runtime=None)
    assert run.iterations[0].ci == "name: ci"
    assert run.iterations[0].compose is None


def test_author_ci_parse_failure_is_artifact_format(tmp_path: Path) -> None:
    class _Broken:
        def generate(self, facts, target):
            return "FROM python:3.12-slim"  # no ci section despite target.ci

        def repair(self, facts, target, artifact_text, report, /):
            return "FROM python:3.12-slim"

    run = author_dockerfile(tmp_path, CI_TARGET, _Broken(), runtime=None)
    assert [r.check_id for r in run.iterations[0].report.results] == [
        "artifact_format"
    ]
    assert run.iterations[0].ci is None
```

Append to `tests/test_cli.py` (follow the file's runtime-free verify idiom and its author-stub idiom):

```python
def test_cli_verify_reads_ci_workflow(tmp_path, monkeypatch, capsys) -> None:
    # project with Dockerfile but no .github/workflows/ci.yml and a ci target
    # -> ci_present FAILED -> exit 1, "ci_present" printed.
    # Follow this file's existing runtime-free pattern (monkeypatch the
    # runtime resolution to None) and target-file helper.
    ...


def test_cli_author_writes_ci_workflow(tmp_path, monkeypatch) -> None:
    # monkeypatch AnthropicAuthor with a stub returning a ci-bearing
    # sentinel response (this file already stubs AnthropicAuthor for
    # author tests); assert (project/".github/workflows/ci.yml") exists
    # with the section content + trailing newline after `main([...])`.
    ...
```

(The two CLI test bodies follow existing idioms in `tests/test_cli.py` — write them concretely against those helpers when editing; the assertions above are the requirement.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm.py tests/test_author.py -k "ci" -v`
Expected: FAIL (ImportError: ACTIONS_CHECKOUT_PIN / wrong tuple)

- [ ] **Step 4: Implement**

`src/deployer/author.py` — in the loop:

```python
            expects_compose = bool(target.dependencies)
            expects_ci = target.ci is not None
            try:
                dockerfile, compose, ci = parse_artifact_response(
                    response, expects_compose, expects_ci
                )
            except ArtifactParseError as exc:
                ...  # unchanged, but record dockerfile=response, compose=None, ci=None
            else:
                report = verify(
                    dockerfile,
                    project_path,
                    target,
                    runtime,
                    facts,
                    compose=compose,
                    ci=ci,
                    build_timeout=build_timeout,
                    health_timeout=health_timeout,
                )
                ...  # environment retry passes compose=compose, ci=ci too
            iterations.append(
                IterationRecord(
                    index=index,
                    dockerfile=dockerfile,
                    compose=compose,
                    ci=ci,
                    report=report,
                    duration_s=time.monotonic() - start,
                )
            )
```

(In the except branch set `dockerfile, compose, ci = response, None, None`.)

`src/deployer/llm.py` — constant below `POETRY_VERSION` (SHA from Step 1):

```python
ACTIONS_CHECKOUT_PIN = "actions/checkout@<sha-from-step-1>"
```

Prompt: append a CI section to `SYSTEM_PROMPT` (f-string; no literal braces needed — do NOT write `${{{{ }}}}` examples into the prompt):

```
- When the deploy intent sets "ci", you also author a GitHub Actions
  build-image workflow and add a third section to your reply:
  {CI_SENTINEL}
  <the workflow YAML>
  (order: Dockerfile section first, compose section if any, ci last).
  Workflow rules: trigger on push and pull_request (never
  pull_request_target); one job on the fixed runner label
  ubuntu-24.04; steps: `uses: {ACTIONS_CHECKOUT_PIN}` (use this exact
  pinned reference), then `run: docker build --file ./Dockerfile .`.
  Pin every action to a full commit SHA. The workflow only builds:
  never push images, never docker login, never reference secrets.
  Without "ci", do not emit a ci section.
```

Import `CI_SENTINEL` alongside the other sentinels.

`src/deployer/cli.py`:

- `_cmd_verify`, next to the compose read:

```python
    ci_path = project / ".github" / "workflows" / "ci.yml"
    ci = ci_path.read_text() if ci_path.is_file() else None
```

  and pass `ci=ci` to `verify(...)`.
- `_cmd_author`, next to the compose write:

```python
        if last.ci is not None:
            wf_dir = project / ".github" / "workflows"
            wf_dir.mkdir(parents=True, exist_ok=True)
            (wf_dir / "ci.yml").write_text(last.ci + "\n")
```

(Transactionality holds structurally: writes happen only from a successfully parsed `IterationRecord`, never from raw text.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_author.py tests/test_llm.py tests/test_cli.py -v && uv run pytest`
Expected: all PASS

- [ ] **Step 6: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/author.py src/deployer/llm.py src/deployer/cli.py tests/test_author.py tests/test_llm.py tests/test_cli.py
git commit -m "feat: ci artifact threading, ACTIONS_CHECKOUT_PIN prompt rules, CLI ci.yml IO"
```

---

### Task 5: Bench plumbing + `ci-build` corpus case + README

**Files:**
- Modify: `src/deployer/bench.py` (`BenchCase.fixture_ci`, loader, `FixtureAuthor(ci=)`, `run_case` skip + persist, `verify_corpus`, `promote_run`), `src/deployer/cli.py` (FixtureAuthor call site)
- Create: `corpus/synthetic/ci-build/{project/pyproject.toml,project/uv.lock,project/src/ci_build/__init__.py,fixture.Dockerfile,fixture.ci.yml,target.json,expected.json}`
- Modify: `tests/test_corpus.py` (`EXPECTED_CASES`, verify call sites, pin-drift test), `tests/test_bench.py`, `README.md`

**Interfaces:**
- Consumes: `render_artifact_response(dockerfile, compose, ci)` (Task 2), `ACTIONS_CHECKOUT_PIN` (Task 4), `verify(..., ci=...)` (Task 3).
- Produces: `BenchCase.fixture_ci: Path | None`; `FixtureAuthor(dockerfile, compose=None, ci=None)`; golden/raw runs persist `ci.yml`.

- [ ] **Step 1: Write the failing tests**

`tests/test_corpus.py`: insert `"ci-build"` into `EXPECTED_CASES` FIRST (`"ci-build" < "compose-redis"` in directory sort), and append:

```python
def test_checkout_pin_matches_llm_constant() -> None:
    from deployer.llm import ACTIONS_CHECKOUT_PIN

    fixture = CORPUS / "synthetic" / "ci-build" / "fixture.ci.yml"
    assert ACTIONS_CHECKOUT_PIN in fixture.read_text()
```

`tests/test_bench.py`, following its existing idioms:

```python
def test_fixture_author_renders_ci_section() -> None:
    from deployer.artifacts import CI_SENTINEL
    from deployer.bench import FixtureAuthor

    author = FixtureAuthor("FROM x:1", ci="name: ci")
    assert CI_SENTINEL in author.generate(ProjectFacts(), DeployTarget())


def test_run_case_skips_ci_case_without_fixture_ci(tmp_path: Path) -> None:
    # fabricate a ci-target case (target {"ci": {}}, requires_l2 false)
    # WITHOUT fixture.ci.yml via this file's corpus-fabrication helper;
    # run_case with FixtureAuthor -> outcome "skipped",
    # "fixture.ci.yml" in skip_reason.
    ...
```

(Second test body: write concretely against the file's `_make_corpus`-style helper at edit time; the assertion pair is the requirement.)

- [ ] **Step 2: Implement `src/deployer/bench.py` (+ cli call site)**

- `BenchCase.fixture_ci: Path | None = None`; loader picks up `case_dir / "fixture.ci.yml"`.
- `FixtureAuthor.__init__(self, dockerfile: str, compose: str | None = None, ci: str | None = None)` → `self._response = render_artifact_response(dockerfile, compose, ci)`.
- `run_case`: extend the existing deps-skip with a ci-skip:

```python
    if (
        case.target.ci is not None
        and case.fixture_ci is None
        and isinstance(author, FixtureAuthor)
    ):
        return BenchCaseResult(
            case=case.name,
            outcome="skipped",
            skip_reason="ci target has no fixture.ci.yml",
            expected=case.expected,
        )
```

- Everywhere a `FixtureAuthor` is constructed from a case (grep `bench.py` + `cli.py`), pass `ci=case.fixture_ci.read_text()` when present.
- `run_case` out-dir persistence: write `ci.yml` when `last.ci is not None` (next to `compose.yaml`).
- `verify_corpus` and both `verify(...)` call sites in `tests/test_corpus.py`: pass `ci=case.fixture_ci.read_text() if case.fixture_ci else None`.
- `promote_run`: copy `ci.yml` like `compose.yaml` (extend the `or`-guard for mkdir).

- [ ] **Step 3: Create the corpus case**

`corpus/synthetic/ci-build/project/pyproject.toml`:

```toml
[project]
name = "ci-build"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

`corpus/synthetic/ci-build/project/src/ci_build/__init__.py`:

```python
"""Build-only project whose CI workflow builds the Dockerfile."""

GREETING = "hello from ci-build"
```

Generate the lock: `cd corpus/synthetic/ci-build/project && uv lock && cd -` (minimal editable-only lock, mirrors `uv-minimal`).

`corpus/synthetic/ci-build/fixture.Dockerfile` (mirror of uv-minimal's proven pattern):

```dockerfile
FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen
CMD ["uv", "run", "python", "-c", "import ci_build; print(ci_build.GREETING)"]
```

`corpus/synthetic/ci-build/fixture.ci.yml` (`<PIN>` = the exact `ACTIONS_CHECKOUT_PIN` value from Task 4):

```yaml
name: ci
on:
  push:
  pull_request:
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: <PIN>
      - run: docker build --file ./Dockerfile .
```

`corpus/synthetic/ci-build/target.json`:

```json
{"ci": {}}
```

`corpus/synthetic/ci-build/expected.json`:

```json
{"capabilities": ["ci"], "notes": "build-only + ci: the workflow builds the same Dockerfile/context that pass L2, so the PAIR is verified coherent, not an isolated YAML"}
```

- [ ] **Step 4: Update README**

Extend the artifact paragraph (after the compose sentence):

```
A `{"ci": {}}` intent additionally authors a build-image GitHub
Actions workflow (`.github/workflows/ci.yml`, SHA-pinned actions,
build-only — no registry push), verified statically incl. a pinned
actionlint.
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_corpus.py -k "not docker" -v && uv run pytest tests/test_bench.py -v && uv run pytest`
Expected: all PASS — `test_corpus_static_checks_pass_for_every_fixture` proves the fixture pair passes the CI L1 checks

- [ ] **Step 6: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/bench.py src/deployer/cli.py corpus/synthetic/ci-build tests/test_corpus.py tests/test_bench.py README.md
git commit -m "feat: ci-build corpus case + bench ci plumbing + pin-drift guard"
```

---

### Task 6: End-to-end gates and PR

**Files:** none new (verification + PR only)

- [ ] **Step 1: Mandatory PR gates**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
uv run deployer bench run --author fixture --label ci-workflow
```

Expected: all green; bench 11/11 matched, rate 1.0 (run detached if >10 min).

- [ ] **Step 2: Environment-gated checks**

```bash
brew install actionlint
actionlint --version   # must print 1.7.12; if brew ships newer, update ACTIONLINT_VERSION to the installed version and re-run unit tests
uv run pytest -m docker
```

Expected: docker suite green (ci-build's Dockerfile goes through real L2 build).

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feature/ci-workflow
gh pr create --title "feat: CI workflow authoring — build-image artifact + five static checks" --body "Implements docs/superpowers/specs/2026-07-22-ci-workflow-design.md ..."
```

PR body covers: contract (`ci: {}`), third sentinel, cascade with SKIPPED semantics, SHA-only pinning + `ACTIONS_CHECKOUT_PIN` rationale, actionlint pattern, corpus 11, MVP boundary (no push/login/secrets), evidence. Standard generation footer. Then the repo Copilot-review workflow; do NOT merge.

- [ ] **Step 4 (separate, explicitly paid; NEVER auto-accepted):** LLM bench + golden re-promote

```bash
uv run deployer bench run --author anthropic --label llm-ci   # detached, no timeout
uv run deployer bench compare .deployer-runs/<run-dir> golden
```

Then promote ONLY after reviewing the golden diff (`git diff` after `bench promote`, before committing) — per the spec: a successful LLM run alone is not sufficient grounds to accept a new golden state. Present the diff to the user if anything beyond the expected new `ci-build` case and routine Dockerfile drift appears.

---

## Self-review notes

- Spec coverage: D1 → Task 1; D2 → Tasks 2+4 (sentinel, transactional writes, prompt, pin constant, path); D3 → Task 3 (cascade, all five checks, exact verify signature, actionlint temp-file + version-gate); D4 → Tasks 4 (CLI) + 5 (bench/golden); D5 → Task 5; acceptance split (mandatory / environment-gated / paid LLM with diff review) → Task 6.
- Type consistency: `parse_artifact_response(text, expects_compose, expects_ci) -> tuple[str, str|None, str|None]` (Tasks 2/4); `_ci_l1_checks(ci) -> tuple[list[CheckResult], bool]` (Tasks 3); `FixtureAuthor(dockerfile, compose=None, ci=None)` (Task 5); `ACTIONS_CHECKOUT_PIN` (Tasks 4/5).
- Known deliberate scope notes: `_ci_wiring_problems` takes `(workflow, raw)` — the secrets sweep is raw-text (errs toward FAIL, incl. comments); `actionlint_available` mirrors `hadolint_available` on `VerificationReport` only (bench comparability advisories for it are follow-up if compare noise appears); two CLI/bench test bodies are written against existing file idioms at edit time with assertions specified.
