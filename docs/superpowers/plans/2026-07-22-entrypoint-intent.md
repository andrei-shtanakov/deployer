# Entrypoint Intent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the deploy target name the program that serves its runtime surface (`DeployTarget.entrypoint`), closing the main.py-stub-vs-app.py blocker for full-service locallogai-backend.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-22-entrypoint-intent-design.md`. One new optional field on `DeployTarget`, validated in the existing `validate_target_against_facts` gate (bare name; must match `facts.entrypoints` or `facts.root_modules`; `TargetConfigError` → exit 2). The SYSTEM_PROMPT entrypoint rule becomes an explicit precedence chain (operator intent → `[project.scripts]` → `script_entrypoint`) with branches 2-3 keeping their Phase-4a wording. New synthetic corpus case `entrypoint-override` measures exactly this feature; locallogai-backend flips to `expected_success = true`.

**Tech Stack:** Python 3.12, pydantic v2, pytest, uv. Docker-marked tests need podman/docker.

## Global Constraints

- Package management with `uv` only: `uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`; "0 errors (1 warning not shown)" is the pre-existing pyrefly baseline.
- Type hints; line length 88; docstrings on public APIs.
- `entrypoint` value is a bare name only — any value containing `/` or `\` is a config error. Config errors (`TargetConfigError`) are exit 2, never AUTHORING; the gate fires before any authoring/verification.
- Prompt branches 2-3 (scripts, script_entrypoint) keep their existing wording — this PR weakens nothing.
- Branch `feature/entrypoint-intent` (exists, spec committed). Never commit to `master`.
- `corpus/**` is excluded from pyrefly; corpus project files need no type hints.

---

### Task 1: `DeployTarget.entrypoint` + validation gate

**Files:**
- Modify: `src/deployer/models.py` (`DeployTarget`, after the `run` field)
- Modify: `src/deployer/facts.py` (`validate_target_against_facts`, ~line 19)
- Modify: `src/deployer/verify.py` (the facts-None guard at the top of `verify()`)
- Test: `tests/test_models.py`, `tests/test_facts.py`, `tests/test_cli.py`, `tests/test_verify_run.py`

**Interfaces:**
- Produces: `DeployTarget.entrypoint: str | None = None`; `validate_target_against_facts` additionally validates it (rules below). CLI wiring needs NO change — `TargetConfigError` handlers from the extras PR already map to exit 2 on all four paths.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_models.py`:

```python
def test_entrypoint_default_none_and_roundtrip() -> None:
    assert DeployTarget().entrypoint is None
    target = DeployTarget(entrypoint="app.py")
    parsed = DeployTarget.model_validate_json(target.model_dump_json())
    assert parsed.entrypoint == "app.py"
```

Append to `tests/test_facts.py` (extend the existing `validate_target_against_facts` test group; `TargetConfigError`, `DeployTarget`, `ProjectFacts` are already imported there):

```python
def test_validate_entrypoint_root_module_ok() -> None:
    facts = ProjectFacts(root_modules=["app.py", "main.py"])
    validate_target_against_facts(DeployTarget(entrypoint="app.py"), facts)


def test_validate_entrypoint_scripts_name_ok() -> None:
    facts = ProjectFacts(entrypoints={"serve": "pkg.app:main"})
    validate_target_against_facts(DeployTarget(entrypoint="serve"), facts)


def test_validate_entrypoint_unknown_raises() -> None:
    facts = ProjectFacts(root_modules=["main.py"])
    with pytest.raises(TargetConfigError, match="app.py"):
        validate_target_against_facts(DeployTarget(entrypoint="app.py"), facts)


def test_validate_entrypoint_rejects_paths() -> None:
    facts = ProjectFacts(root_modules=["app.py"])
    for bad in ("src/app.py", "./app.py", "pkg\\mod.py"):
        with pytest.raises(TargetConfigError, match="bare name"):
            validate_target_against_facts(
                DeployTarget(entrypoint=bad), facts
            )


def test_validate_entrypoint_unset_is_noop() -> None:
    validate_target_against_facts(DeployTarget(), ProjectFacts())
```

Append to `tests/test_cli.py` (same shape as `test_verify_unknown_extra_exits_2` in this file — reuse its monkeypatch style for `resolve_runtime`):

```python
def test_verify_unknown_entrypoint_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "resolve_runtime", lambda *a, **k: None)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    target = tmp_path / "target.json"
    target.write_text('{"entrypoint": "app.py"}')
    assert (
        cli.main(["verify", str(tmp_path), "--target", str(target)]) == 2
    )


def test_author_unknown_entrypoint_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    target = tmp_path / "target.json"
    target.write_text('{"entrypoint": "app.py"}')
    assert (
        cli.main(
            ["author", str(tmp_path), "--target", str(target), "--no-docker"]
        )
        == 2
    )
```

(If the existing extras CLI tests patch `resolve_runtime` or the author
backend differently — e.g. a different attribute path or a sentinel
`FakeAuthor` — mirror exactly what `test_verify_unknown_extra_exits_2` /
`test_author_unknown_extra_exits_2` in this file do, including any
AnthropicAuthor monkeypatch they need.)

Append to `tests/test_verify_run.py` (the facts-None guard):

```python
def test_verify_entrypoint_without_facts_is_config_error(
    tmp_path: Path,
) -> None:
    from deployer.facts import TargetConfigError

    with pytest.raises(TargetConfigError, match="no project facts"):
        verify_mod.verify(
            "FROM python:3.12-slim",
            tmp_path,
            DeployTarget(entrypoint="app.py"),
            None,
            None,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py tests/test_facts.py tests/test_cli.py -k "entrypoint" -v`
Expected: FAIL (`entrypoint` unknown field / validators absent)

- [ ] **Step 3: Implement**

`src/deployer/models.py` — add to `DeployTarget` after the `run` field:

```python
    entrypoint: str | None = None
```

(No field validator: the value is compared verbatim against fact names; normalization would break exact filename matching.)

`src/deployer/facts.py` — restructure `validate_target_against_facts` (the current body early-returns on empty extras; entrypoint must be validated regardless):

```python
def validate_target_against_facts(target: DeployTarget, facts: ProjectFacts) -> None:
    """Config-level compatibility gate between intent and scanned facts."""
    if target.extras:
        unknown = [
            e for e in target.extras if e not in facts.optional_dependencies
        ]
        if unknown:
            raise TargetConfigError(
                "deploy target requests extras not present in "
                f"[project.optional-dependencies]: {', '.join(unknown)}"
            )
        if facts.package_manager == "pip" and not facts.has_build_system:
            raise TargetConfigError(
                "extras require an installable project; pip projects without "
                "a build-system are unsupported"
            )
    if target.entrypoint is not None:
        name = target.entrypoint
        if "/" in name or "\\" in name:
            raise TargetConfigError(
                "DeployTarget.entrypoint must be a bare name (a root module "
                f"filename or a [project.scripts] name), not a path: {name!r}"
            )
        if name not in facts.entrypoints and name not in facts.root_modules:
            raise TargetConfigError(
                f"deploy target entrypoint {name!r} matches neither a "
                "[project.scripts] name nor a file in root_modules"
            )
```

`src/deployer/verify.py` — extend the facts-None guard in `verify()` (currently `elif target.extras:`):

```python
    elif target.extras or target.entrypoint is not None:
        raise TargetConfigError(
            "deploy target requires facts-based validation (extras or "
            "entrypoint) but no project facts were provided"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py tests/test_facts.py tests/test_cli.py -v`
Expected: all PASS (including all pre-existing extras tests — the restructure must not change their behavior)

- [ ] **Step 5: Format, typecheck, full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/models.py src/deployer/facts.py src/deployer/verify.py tests/test_models.py tests/test_facts.py tests/test_cli.py
git commit -m "feat: DeployTarget.entrypoint intent with facts-gate validation"
```

---

### Task 2: Prompt precedence rewrite

**Files:**
- Modify: `src/deployer/llm.py` (`SYSTEM_PROMPT` — replace the `script_entrypoint is deterministic ground truth...` bullet)
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `DeployTarget.entrypoint` (Task 1). The intent JSON already carries it via `model_dump` — no `_intent_json` change (entrypoint is not a secret; only `run` is redacted).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm.py`:

```python
def test_system_prompt_entrypoint_precedence() -> None:
    assert "Never override a DeployTarget.entrypoint" in SYSTEM_PROMPT
    first = SYSTEM_PROMPT.index('deploy intent sets "entrypoint"')
    second = SYSTEM_PROMPT.index("[project.scripts]) is non-empty")
    third = SYSTEM_PROMPT.index("script_entrypoint is deterministic")
    assert first < second < third


def test_intent_json_renders_entrypoint() -> None:
    rendered = _context_blocks(
        ProjectFacts(root_modules=["app.py"]),
        DeployTarget(entrypoint="app.py"),
    )
    assert '"entrypoint": "app.py"' in rendered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_llm.py -k "precedence or renders_entrypoint" -v`
Expected: FAIL (`ValueError: substring not found` / assertion)

- [ ] **Step 3: Rewrite the prompt rule**

In `src/deployer/llm.py`, replace this bullet:

```text
- script_entrypoint is deterministic ground truth. If it is set and
  entrypoints ([project.scripts]) is empty, the Dockerfile CMD MUST execute
  that file in exec form (e.g. CMD ["python", "main.py"] or the
  package-manager equivalent). Never invent servers such as http.server,
  never leave a bare interpreter, never run a file not present in the
  facts. When entrypoints is non-empty it wins over script_entrypoint.
```

with:

```text
- Container-command precedence:
  1. If the deploy intent sets "entrypoint", the CMD MUST execute it in
     exec form (e.g. CMD ["python", "app.py"] or the package-manager
     equivalent). Never override a DeployTarget.entrypoint. It is
     operator intent.
  2. Otherwise, when entrypoints ([project.scripts]) is non-empty, it
     wins: run the named console script.
  3. Otherwise script_entrypoint is deterministic ground truth. If it is
     set the Dockerfile CMD MUST execute that file in exec form (e.g.
     CMD ["python", "main.py"] or the package-manager equivalent).
  Never invent servers such as http.server, never leave a bare
  interpreter, never run a file not present in the facts.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_llm.py -v`
Expected: all PASS (the pre-existing job-rule and extras-rule tests must stay green; the run-intent bullet references "the rules above" which still holds)

- [ ] **Step 5: Format, typecheck, full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/llm.py tests/test_llm.py
git commit -m "feat: prompt entrypoint precedence (operator intent > scripts > script_entrypoint)"
```

---

### Task 3: Corpus `entrypoint-override` case + locallogai flip

**Files:**
- Create: `corpus/synthetic/entrypoint-override/project/pyproject.toml`
- Create: `corpus/synthetic/entrypoint-override/project/main.py`
- Create: `corpus/synthetic/entrypoint-override/project/app.py`
- Create: `corpus/synthetic/entrypoint-override/target.json`
- Create: `corpus/synthetic/entrypoint-override/expected.json`
- Create: `corpus/synthetic/entrypoint-override/fixture.Dockerfile`
- Modify: `corpus/external.toml` (locallogai-backend entry)
- Modify: `tests/test_corpus.py` (`EXPECTED_CASES`)
- Test: `tests/test_bench.py`

**Interfaces:**
- Consumes: everything above; `load_corpus` takes the `corpus/` ROOT (it appends `synthetic` itself — do not pass `corpus/synthetic`).
- Produces: 8th synthetic case; locallogai entry with `entrypoint = "app.py"` and `expected_success = true`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py` (same style as `test_extras_job_case_shape`):

```python
def test_entrypoint_override_case_shape() -> None:
    corpus = Path(__file__).parent.parent / "corpus"
    case = next(
        c for c in load_corpus(corpus) if c.name == "entrypoint-override"
    )
    assert case.target.entrypoint == "app.py"
    assert case.target.service is not None
    assert case.target.service.port == 8000


def test_entrypoint_override_facts() -> None:
    from deployer.facts import analyze_project

    project = (
        Path(__file__).parent.parent
        / "corpus"
        / "synthetic"
        / "entrypoint-override"
        / "project"
    )
    facts = analyze_project(project)
    assert facts.script_entrypoint == "main.py"  # the decoy wins the fact
    assert "app.py" in facts.root_modules
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bench.py -k entrypoint_override -v`
Expected: FAIL (`StopIteration`)

- [ ] **Step 3: Create the corpus case**

`corpus/synthetic/entrypoint-override/project/pyproject.toml`:

```toml
[project]
name = "entrypoint-override"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []
```

`corpus/synthetic/entrypoint-override/project/main.py`:

```python
"""Decoy stub: script_entrypoint picks this file; the service is app.py."""

if __name__ == "__main__":
    print("stub: not the service")
```

`corpus/synthetic/entrypoint-override/project/app.py`:

```python
"""Real service entrypoint; the deploy intent names this file."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
```

`corpus/synthetic/entrypoint-override/target.json`:

```json
{"entrypoint": "app.py", "service": {"port": 8000, "healthcheck_path": "/health"}}
```

`corpus/synthetic/entrypoint-override/expected.json`:

```json
{"capabilities": ["entrypoint", "service"], "notes": "miniature of the locallogai shape: script_entrypoint picks the main.py decoy, the intent names app.py; without the entrypoint intent the rules force the stub and the healthcheck fails"}
```

`corpus/synthetic/entrypoint-override/fixture.Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY main.py app.py ./
CMD ["python", "app.py"]
```

In `tests/test_corpus.py`, add `"entrypoint-override"` to `EXPECTED_CASES` in alphabetical position (before `"extras-job"`).

- [ ] **Step 4: Update `corpus/external.toml`**

In the locallogai-backend entry: add `entrypoint = "app.py"` to `[targets.target]`, flip `expected_success` to `true`, add `"entrypoint"` to capabilities, and replace the notes with:

```toml
notes = "full-service target: gui extra + explicit entrypoint intent (app.py) — the service-entrypoint disambiguation blocker from the 4b-2 run is closed by DeployTarget.entrypoint; healthcheck '/' relies on Gradio's root-200"
```

Keep name/url/commit and the rest of `[targets.target]` unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_bench.py tests/test_corpus.py -v`
Expected: all PASS (corpus completeness test now expects 8 cases)

- [ ] **Step 6: Docker smoke for the new case**

Run: `uv run deployer bench verify --filter entrypoint-override`
Expected: green (build + run_healthcheck on app.py). Check the actual filter flag name via `--help` if this errors.

- [ ] **Step 7: Format, typecheck, full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add corpus/synthetic/entrypoint-override corpus/external.toml tests/test_bench.py tests/test_corpus.py
git commit -m "feat: entrypoint-override corpus case; locallogai flips to expected success"
```

---

### Task 4: Acceptance sweep

**Files:**
- No production code changes expected.

- [ ] **Step 1: Full local sweep**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
uv run pytest
uv run pytest -m docker
```

Expected: all clean/green (docker suite now includes the entrypoint-override smoke).

- [ ] **Step 2: Fixture bench acceptance**

Run: `uv run deployer bench run --author fixture --label entrypoint-fixture`
Expected: **8/8 matched**, success rate 1.0.

- [ ] **Step 3: Commit anything the sweep touched, else no-op**

```bash
git status --short
```

Expected: clean tree. If formatting touched files: `git add -A && git commit -m "chore: acceptance sweep formatting"`.

- [ ] **Step 4: Record manual follow-ups (controller/operator, not this task)**

At PR time, manually: `--author anthropic` bench run (synthetic, `.env` sourced) → 8/8 → `bench promote` → `bench compare` clean; locallogai research run (`--include-external`, synthetic carrier case needed for the filter) — **expected matched-as-success now**; record either outcome in `.superpowers/sdd/progress.md` and adjust `expected_success` only on evidence.
