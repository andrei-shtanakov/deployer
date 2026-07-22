# Poetry Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `"poetry"` as a first-class, lockfile-first package manager: detection, legacy `[tool.poetry]` metadata fallback, prompt install strategy, L1 checks, and a synthetic corpus case.

**Architecture:** `poetry.lock` sets `package_manager="poetry"` (precedence `uv.lock > poetry.lock > requirements*.txt`); legacy `[tool.poetry]` fills metadata gaps only. The authored Dockerfile installs a pinned Poetry in the builder stage, runs `poetry install --no-root --only main` into an in-project venv, and copies `.venv` to the final stage. L1 forbids any direct pip dependency install in a Poetry project (bootstrap-only exception).

**Tech Stack:** Python 3.12, pydantic, pytest, uv; Poetry pinned at `2.4.1` (verified latest on PyPI 2026-07-22).

**Spec:** `docs/superpowers/specs/2026-07-22-poetry-support-design.md` — read it first.

## Global Constraints

- Branch: `feature/poetry-support` (already exists, has the spec commits). Never commit to `master`.
- Poetry pin: `poetry==2.4.1`, defined ONCE as `POETRY_VERSION` in `src/deployer/llm.py` and repeated literally in the fixture Dockerfile (a test guards the drift).
- Detection precedence (verbatim from spec): `uv.lock > poetry.lock > requirements*.txt`. `[tool.poetry]` without `poetry.lock` never sets `package_manager`.
- Legacy fallback fires only when the key is **absent** from `[project]`; present-but-invalid/ambiguous resolves per existing rules (no fallback).
- After every task: `uv run ruff format . && uv run ruff check . && uv run pyrefly check` must be clean.
- Line length 88; type hints everywhere; follow existing file patterns exactly.

---

### Task 1: Detection — `poetry.lock` sets `package_manager="poetry"`

**Files:**
- Modify: `src/deployer/models.py:89` (ProjectFacts fields)
- Modify: `src/deployer/facts.py:236-241` (detection block in `analyze_project`)
- Test: `tests/test_facts.py`

**Interfaces:**
- Produces: `ProjectFacts.package_manager: Literal["uv", "pip", "poetry"] | None`; new field `ProjectFacts.has_poetry_lock: bool = False`. Tasks 3–5 rely on the `"poetry"` literal value.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_facts.py`, next to `test_uv_lock_wins_over_requirements`)

```python
def test_poetry_lock_sets_poetry_manager(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    assert facts.package_manager == "poetry"
    assert facts.has_poetry_lock is True


def test_tool_poetry_without_lock_does_not_detect(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "x"\n')
    facts = analyze_project(tmp_path)
    assert facts.package_manager is None
    assert facts.has_poetry_lock is False


def test_uv_lock_wins_over_poetry_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "poetry.lock").write_text("")
    assert analyze_project(tmp_path).package_manager == "uv"


def test_poetry_lock_wins_over_requirements(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("")
    (tmp_path / "requirements.txt").write_text("flask\n")
    facts = analyze_project(tmp_path)
    assert facts.package_manager == "poetry"
    assert facts.requirements_files == {"requirements.txt": ["flask"]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_facts.py -k poetry -v`
Expected: FAIL (`has_poetry_lock` unknown field / `package_manager` is None or "pip")

- [ ] **Step 3: Implement**

In `src/deployer/models.py` change the `ProjectFacts` fields:

```python
    has_uv_lock: bool = False
    has_poetry_lock: bool = False
    package_manager: Literal["uv", "pip", "poetry"] | None = None
```

In `src/deployer/facts.py` replace the detection block in `analyze_project`:

```python
    has_uv_lock = (path / "uv.lock").is_file()
    has_poetry_lock = (path / "poetry.lock").is_file()
    package_manager: Literal["uv", "pip", "poetry"] | None = None
    if has_uv_lock:
        package_manager = "uv"
    elif has_poetry_lock:
        package_manager = "poetry"
    elif requirements_files:
        package_manager = "pip"
```

and pass `has_poetry_lock=has_poetry_lock` in the `ProjectFacts(...)` constructor call (right after `has_uv_lock=has_uv_lock`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_facts.py tests/test_models.py -v`
Expected: all PASS

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/models.py src/deployer/facts.py tests/test_facts.py
git commit -m "feat: detect poetry.lock as package_manager=poetry (lockfile-first)"
```

---

### Task 2: Legacy `[tool.poetry]` metadata fallback

**Files:**
- Modify: `src/deployer/facts.py` (`_scan_optional_dependencies` at :105-122, metadata blocks in `analyze_project` at :199-230)
- Test: `tests/test_facts.py`

**Interfaces:**
- Consumes: Task 1's detection (fallback must NOT affect it).
- Produces: `analyze_project` fills `name`, `dependencies`, `entrypoints`, `optional_dependencies` from `[tool.poetry]` when the corresponding `[project]` key is absent. Signature of `_scan_optional_dependencies` becomes `(project: dict[str, Any], poetry_meta: dict[str, Any]) -> dict[str, list[str]]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_facts.py`)

```python
_LEGACY_PYPROJECT = """\
[tool.poetry]
name = "legacy-app"
version = "0.1.0"

[tool.poetry.dependencies]
python = ">=3.12"
flask = "^3.0"
psycopg2 = { version = "^2.9", optional = true }

[tool.poetry.extras]
db = ["psycopg2"]

[tool.poetry.scripts]
legacy-app = "legacy_app.main:run"

[build-system]
requires = ["poetry-core"]
build-backend = "poetry.core.masonry.api"
"""


def test_legacy_poetry_metadata_fallback(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    assert facts.name == "legacy-app"
    assert facts.dependencies == ["flask"]  # python and optional excluded
    assert facts.entrypoints == {"legacy-app": "legacy_app.main:run"}
    assert facts.optional_dependencies == {"db": ["psycopg2"]}
    assert facts.package_manager == "poetry"


def test_legacy_fallback_does_not_detect_poetry(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    facts = analyze_project(tmp_path)
    assert facts.name == "legacy-app"  # metadata visible
    assert facts.package_manager is None  # but no lock -> no strategy


def test_project_table_wins_over_legacy(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pep621"\ndependencies = ["requests"]\n\n'
        '[tool.poetry]\nname = "legacy"\n\n'
        '[tool.poetry.dependencies]\nflask = "^3.0"\n'
    )
    facts = analyze_project(tmp_path)
    assert facts.name == "pep621"
    assert facts.dependencies == ["requests"]


def test_invalid_project_key_is_not_replaced_by_legacy(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = 42\n\n[tool.poetry]\nname = "legacy"\n'
    )
    assert analyze_project(tmp_path).name is None


def test_pep621_extras_collision_not_papered_over(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n'
        "[project.optional-dependencies]\n"
        'my_extra = []\n"my-extra" = []\n\n'
        '[tool.poetry.extras]\ndb = ["psycopg2"]\n'
    )
    assert analyze_project(tmp_path).optional_dependencies == {}


def test_legacy_extras_normalize_and_collide(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.extras]\nmy_extra = ["a"]\n"my-extra" = ["b"]\n'
    )
    assert analyze_project(tmp_path).optional_dependencies == {}


def test_validate_entrypoint_against_legacy_scripts(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    validate_target_against_facts(DeployTarget(entrypoint="legacy-app"), facts)


def test_validate_extras_against_legacy_extras(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(_LEGACY_PYPROJECT)
    (tmp_path / "poetry.lock").write_text("")
    facts = analyze_project(tmp_path)
    validate_target_against_facts(DeployTarget(extras=["db"]), facts)
    with pytest.raises(TargetConfigError):
        validate_target_against_facts(DeployTarget(extras=["gui"]), facts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_facts.py -k legacy -v`
Expected: FAIL (empty name/dependencies/entrypoints/optional_dependencies)

- [ ] **Step 3: Implement in `src/deployer/facts.py`**

Replace `_scan_optional_dependencies` with a shared normalizer + presence-aware wrapper (keeping the collision docstring):

```python
def _normalize_extras(raw: Any) -> dict[str, list[str]]:
    """Normalized extras mapping; wrong shape or key collision -> {}.

    Two raw keys normalizing to the same name (my_extra + my-extra) make
    the metadata ambiguous — ambiguous metadata is no fact.
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        name = normalize_extra_name(key)
        if name in result:
            return {}
        result[name] = [d for d in value if isinstance(d, str)]
    return result


def _scan_optional_dependencies(
    project: dict[str, Any], poetry_meta: dict[str, Any]
) -> dict[str, list[str]]:
    """[project.optional-dependencies], else legacy [tool.poetry.extras].

    The fallback fires only when the PEP 621 key is absent: a present
    but ambiguous field (e.g. a collision) resolves to {} and is not
    papered over by legacy metadata.
    """
    if "optional-dependencies" in project:
        return _normalize_extras(project["optional-dependencies"])
    return _normalize_extras(poetry_meta.get("extras"))
```

In `analyze_project`, right after the `project` dict is extracted, add:

```python
    tool = pyproject.get("tool")
    poetry_meta: dict[str, Any] = {}
    if isinstance(tool, dict) and isinstance(tool.get("poetry"), dict):
        poetry_meta = tool["poetry"]
```

Then extend the three metadata blocks (fallback only on absent key):

```python
    name = project.get("name")
    if not isinstance(name, str):
        name = None
    if "name" not in project:
        legacy_name = poetry_meta.get("name")
        if isinstance(legacy_name, str):
            name = legacy_name
```

```python
    deps = project.get("dependencies", [])
    if isinstance(deps, list):
        dependencies = [d for d in deps if isinstance(d, str)]
    else:
        dependencies = []
    if "dependencies" not in project:
        legacy_deps = poetry_meta.get("dependencies")
        if isinstance(legacy_deps, dict):
            # optional deps are exposed only via extras, never as base
            # deps — otherwise hints would fire for unrequested extras
            dependencies = [
                k
                for k, v in legacy_deps.items()
                if isinstance(k, str)
                and k != "python"
                and not (isinstance(v, dict) and v.get("optional") is True)
            ]
```

```python
    scripts = project.get("scripts", {})
    if isinstance(scripts, dict):
        entrypoints = {
            k: v
            for k, v in scripts.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    else:
        entrypoints = {}
    if "scripts" not in project:
        legacy_scripts = poetry_meta.get("scripts")
        if isinstance(legacy_scripts, dict):
            entrypoints = {
                k: v
                for k, v in legacy_scripts.items()
                if isinstance(k, str) and isinstance(v, str)
            }
```

Update the constructor call: `optional_dependencies=_scan_optional_dependencies(project, poetry_meta)`.

- [ ] **Step 4: Run the full unit suite**

Run: `uv run pytest`
Expected: all PASS (existing extras tests use the new signature transparently via `analyze_project`; if `test_optional_dependencies_*` call `_scan_optional_dependencies` directly, update those call sites to pass `{}` as `poetry_meta`)

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/facts.py tests/test_facts.py
git commit -m "feat: legacy [tool.poetry] metadata fallback (name, deps, scripts, extras)"
```

---

### Task 3: L1 install-strategy rules for Poetry

**Files:**
- Modify: `src/deployer/verify.py` (`_check_install_strategy` at :174-229, module regexes at :33-34)
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `ProjectFacts.package_manager == "poetry"` (Task 1); `_normalize_requirement_name` from `deployer.facts`.
- Produces: `install_strategy` check emits FAILED / WARNING / PASSED per the spec's Decision 5. New helper `_pip_install_payload(cmd: str) -> list[str] | None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_verify_static.py`, after the existing install-strategy tests)

```python
POETRY_FACTS = ProjectFacts(package_manager="poetry", has_build_system=True)

POETRY_GOOD = (
    "FROM python:3.12-slim AS builder\nWORKDIR /app\n"
    "RUN pip install --no-cache-dir poetry==2.4.1\n"
    "RUN poetry install --no-root --only main --no-interaction --no-ansi\n"
    "FROM python:3.12-slim\nWORKDIR /app\n"
    'CMD ["python", "main.py"]\n'
)


def _poetry_report(hello_service: Path, run_line: str):
    dockerfile = POETRY_GOOD.replace(
        "RUN pip install --no-cache-dir poetry==2.4.1", run_line
    )
    return verify_static(dockerfile, hello_service, POETRY_FACTS)


def test_poetry_pinned_bootstrap_passes(hello_service: Path) -> None:
    report = verify_static(POETRY_GOOD, hello_service, POETRY_FACTS)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


@pytest.mark.parametrize(
    "line",
    ["RUN pip install poetry", "RUN pip install 'poetry>=1.8'"],
)
def test_poetry_unpinned_bootstrap_warns(hello_service: Path, line: str) -> None:
    report = _poetry_report(hello_service, line)
    check = _by_id(report, "install_strategy")
    assert check.status is CheckStatus.WARNING
    assert "pin" in check.message or "==" in check.message


@pytest.mark.parametrize(
    "line",
    [
        "RUN pip install -r requirements.txt",
        "RUN pip install .",
        "RUN pip install flask",
        "RUN pip install poetry==2.4.1 flask",
        "RUN pip3 install flask",
        "RUN python -m pip install flask",
        "RUN python3 -m pip install flask",
        "RUN uv sync --frozen",
        "RUN uv pip install flask",
    ],
)
def test_poetry_project_direct_dep_install_fails(
    hello_service: Path, line: str
) -> None:
    report = _poetry_report(hello_service, line)
    check = _by_id(report, "install_strategy")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


@pytest.mark.parametrize("manager", ["uv", "pip"])
def test_non_poetry_project_poetry_install_fails(
    hello_service: Path, manager: str
) -> None:
    facts = ProjectFacts(package_manager=manager, has_build_system=True)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\n"
        "RUN poetry install --no-root\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


def test_pip_project_poetry_bootstrap_alone_is_not_flagged(
    hello_service: Path,
) -> None:
    facts = ProjectFacts(package_manager="pip", has_build_system=False)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\n"
        "RUN pip install poetry==2.4.1\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED
```

Note: `'poetry>=1.8'` keeps its quotes through `_run_commands` token
splitting — strip quotes in the payload helper (Step 3) so the name
still normalizes to `poetry`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -k poetry -v`
Expected: FAIL (no poetry rules yet — everything PASSES)

- [ ] **Step 3: Implement in `src/deployer/verify.py`**

Add the import (`_normalize_requirement_name` joins the existing `deployer.facts` import) and, next to `_PYTHON_M_PIP`:

```python
_PIP_INSTALL = re.compile(r"^(?:\S*python[\d.]*\s+-m\s+pip|pip3?)\s+install\b")
```

Add the helper above `_check_install_strategy`:

```python
def _pip_install_payload(cmd: str) -> list[str] | None:
    """Positional install args of a pip invocation; None if not one.

    Covers pip / pip3 / python -m pip / python3 -m pip. Flags are
    dropped, so a flag's value may survive as a positional (e.g. an
    index URL) — that errs toward FAIL, never a false pass.
    """
    if not _PIP_INSTALL.match(cmd):
        return None
    tokens = cmd.split()
    idx = tokens.index("install")
    return [
        t.strip("'\"") for t in tokens[idx + 1 :] if not t.startswith("-")
    ]
```

In `_check_install_strategy`, add `warnings: list[str] = []` next to `problems`, and after the existing pip/uv rules add:

```python
    # poetry rules: poetry.lock is the only dependency source
    if facts.package_manager == "poetry":
        for cmd in commands:
            if cmd.startswith(("uv sync", "uv pip")):
                problems.append(
                    "project uses poetry (poetry.lock) but Dockerfile invokes uv"
                )
                break
        for cmd in commands:
            payload = _pip_install_payload(cmd)
            if not payload:
                continue
            names = {_normalize_requirement_name(t) for t in payload}
            if names == {"poetry"}:
                # the builder bootstrap — allowed, but must be pinned
                if not all("==" in t for t in payload):
                    warnings.append(
                        "poetry bootstrap is not pinned; use poetry==<version>"
                    )
                continue
            problems.append(
                "project uses poetry (poetry.lock) but Dockerfile installs "
                "dependencies with pip; poetry.lock is the only dependency "
                "source"
            )
            break

    # poetry-in-non-poetry rule
    if facts.package_manager in ("uv", "pip"):
        for cmd in commands:
            if cmd.startswith("poetry install"):
                problems.append(
                    f"project uses {facts.package_manager} but Dockerfile "
                    "invokes poetry install"
                )
                break
```

Replace the return block:

```python
    if problems:
        return CheckResult(
            check_id="install_strategy",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="; ".join(problems),
        )
    if warnings:
        return CheckResult(
            check_id="install_strategy",
            status=CheckStatus.WARNING,
            message="; ".join(warnings),
        )
    return CheckResult(check_id="install_strategy", status=CheckStatus.PASSED)
```

- [ ] **Step 4: Run the static-verify suite**

Run: `uv run pytest tests/test_verify_static.py -v`
Expected: all PASS (including the pre-existing uv/pip cases)

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: L1 poetry install-strategy rules (payload-based, bootstrap exception)"
```

---

### Task 4: Prompt — Poetry install strategy and extras rule

**Files:**
- Modify: `src/deployer/llm.py` (constants at :12-13, `SYSTEM_PROMPT` at :15-75)
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `POETRY_VERSION = "2.4.1"` module constant (Task 5's pin-drift test imports it); `SYSTEM_PROMPT` becomes an f-string embedding it.

- [ ] **Step 1: Write the failing test** (append to `tests/test_llm.py`)

```python
def test_system_prompt_carries_poetry_rules() -> None:
    from deployer.llm import POETRY_VERSION, SYSTEM_PROMPT

    assert f"poetry=={POETRY_VERSION}" in SYSTEM_PROMPT
    assert "--no-root" in SYSTEM_PROMPT
    assert "POETRY_VIRTUALENVS_IN_PROJECT" in SYSTEM_PROMPT
    assert "--extras" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm.py::test_system_prompt_carries_poetry_rules -v`
Expected: FAIL (ImportError: `POETRY_VERSION`)

- [ ] **Step 3: Implement in `src/deployer/llm.py`**

Add below `MAX_TOKENS`:

```python
POETRY_VERSION = "2.4.1"
```

Turn `SYSTEM_PROMPT = """\` into `SYSTEM_PROMPT = f"""\` (the prompt contains no literal braces). Insert a new bullet between the `"uv"` and `"pip"` install-strategy bullets:

```
  - "poetry": two-stage build. Builder stage: install the pinned
    installer with `pip install --no-cache-dir poetry=={POETRY_VERSION}`
    (always this exact pin), set ENV POETRY_VIRTUALENVS_IN_PROJECT=1,
    COPY pyproject.toml and poetry.lock, then run
    `poetry install --no-root --only main --no-interaction --no-ansi`.
    Final stage: COPY /app/.venv from the builder and prepend
    /app/.venv/bin to PATH; never install Poetry in the final stage.
    Never install dependencies with pip directly — poetry.lock is the
    only dependency source. Exception: running a console script
    requires the root package; then COPY the source before
    `poetry install` and drop `--no-root`.
```

Extend the extras bullet's mechanism list (after the `pip install ".[name]"` clause):

```
  or `poetry install --no-root --only main --extras "<name>"` (repeat
  `--extras` once per requested extra) for poetry projects.
```

- [ ] **Step 4: Run the llm suite**

Run: `uv run pytest tests/test_llm.py -v`
Expected: all PASS (the prompt-hash test derives from `SYSTEM_PROMPT`, so it stays green)

- [ ] **Step 5: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/llm.py tests/test_llm.py
git commit -m "feat: poetry install strategy and extras rule in the authoring prompt"
```

---

### Task 5: Corpus case `poetry-legacy`, pin-drift test, README

**Files:**
- Create: `corpus/synthetic/poetry-legacy/project/pyproject.toml`
- Create: `corpus/synthetic/poetry-legacy/project/main.py`
- Create: `corpus/synthetic/poetry-legacy/project/poetry.lock` (generated, committed)
- Create: `corpus/synthetic/poetry-legacy/fixture.Dockerfile`
- Create: `corpus/synthetic/poetry-legacy/target.json`
- Create: `corpus/synthetic/poetry-legacy/expected.json`
- Modify: `tests/test_corpus.py:14-23` (`EXPECTED_CASES`) and append the pin-drift test
- Modify: `README.md:10` (facts coverage sentence)

**Interfaces:**
- Consumes: `POETRY_VERSION` from `deployer.llm` (Task 4); `"poetry"` detection (Task 1); L1 rules (Task 3 — the fixture must pass them).

- [ ] **Step 1: Write the failing corpus tests**

In `tests/test_corpus.py` insert `"poetry-legacy"` into `EXPECTED_CASES` between `"pip-requirements"` and `"service-healthcheck"` (the list is directory-sorted), and append:

```python
def test_poetry_pin_matches_llm_constant() -> None:
    from deployer.llm import POETRY_VERSION

    fixture = CORPUS / "synthetic" / "poetry-legacy" / "fixture.Dockerfile"
    assert f"poetry=={POETRY_VERSION}" in fixture.read_text()
```

Run: `uv run pytest tests/test_corpus.py -k "parses or pin" -v`
Expected: FAIL (case directory missing)

- [ ] **Step 2: Create the fixture project**

`corpus/synthetic/poetry-legacy/project/pyproject.toml`:

```toml
[tool.poetry]
name = "poetry-legacy"
version = "0.1.0"
description = "Legacy-format Poetry service fixture"
authors = ["deployer corpus <corpus@example.invalid>"]

[tool.poetry.dependencies]
python = ">=3.12,<4.0"
flask = "^3.0"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```

`corpus/synthetic/poetry-legacy/project/main.py`:

```python
"""Legacy-format Poetry service fixture: Flask app with /health."""

from flask import Flask

app = Flask(__name__)


@app.get("/health")
def health() -> str:
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

- [ ] **Step 3: Generate and commit the lockfile with the pinned Poetry**

```bash
cd corpus/synthetic/poetry-legacy/project
uvx --from poetry==2.4.1 poetry lock
cd -
```

Expected: `poetry.lock` created (flask + its transitive deps). Poetry 2.x reads the legacy table and may print a deprecation warning — that is fine; the legacy format is the point of this case.

- [ ] **Step 4: Create the case files**

`corpus/synthetic/poetry-legacy/fixture.Dockerfile`:

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
ENV POETRY_VIRTUALENVS_IN_PROJECT=1
RUN pip install --no-cache-dir poetry==2.4.1
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main --no-interaction --no-ansi

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY main.py ./
EXPOSE 8000
CMD ["python", "main.py"]
```

`corpus/synthetic/poetry-legacy/target.json`:

```json
{"service": {"port": 8000, "healthcheck_path": "/health"}}
```

`corpus/synthetic/poetry-legacy/expected.json`:

```json
{"capabilities": ["poetry", "service"], "notes": "legacy [tool.poetry] metadata + poetry.lock; builder-stage Poetry install, .venv copy"}
```

- [ ] **Step 5: Update README**

In `README.md`, replace the sentence starting "Facts cover uv and pip":

```
Facts cover uv, Poetry (poetry.lock, including legacy [tool.poetry]
metadata) and pip (requirements.txt) projects; a curated hints table
```

- [ ] **Step 6: Run the non-docker corpus tests**

Run: `uv run pytest tests/test_corpus.py -k "not docker" -v && uv run pytest`
Expected: all PASS — in particular `test_corpus_static_checks_pass_for_every_fixture` proves the fixture passes the new L1 poetry rules

- [ ] **Step 7: Gates and commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add corpus/synthetic/poetry-legacy tests/test_corpus.py README.md
git commit -m "feat: poetry-legacy synthetic corpus case + pin-drift guard + README"
```

---

### Task 6: End-to-end gates and PR

**Files:** none new (verification + PR only)

- [ ] **Step 1: Full unit suite and gates**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
```

Expected: all PASS, no diffs, no findings

- [ ] **Step 2: Docker-marked tests (sandboxed build/run of all 9 fixtures)**

Run: `uv run pytest -m docker`
Expected: PASS (or SKIP if no container runtime is reachable — then run against the homelab host per the established remote-verify workflow before opening the PR)

- [ ] **Step 3: Fixture bench 9/9**

```bash
uv run deployer bench run --author fixture --label poetry-support
```

Expected: 9/9 matched, success for `poetry-legacy`

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin feature/poetry-support
gh pr create --title "feat: Poetry support (lockfile-first detection, legacy metadata fallback, L1 rules, corpus case)" --body "Implements docs/superpowers/specs/2026-07-22-poetry-support-design.md ..."
```

Then follow the repo git workflow: read GitHub Copilot review, fix valid findings with new commits, answer invalid ones. Do NOT merge — the user merges.

- [ ] **Step 5 (post-green follow-up, separate chore commit as in PR #19):** re-promote the LLM golden after a green LLM bench run:

```bash
uv run deployer bench run --author llm --label poetry-llm
uv run deployer bench compare --baseline golden <run-dir>
uv run deployer bench promote <run-dir>
```

---

## Self-review notes

- Spec coverage: Decision 1 → Task 4+5 fixture; Decision 2 → Task 1; Decision 3 → Task 2; Decision 4 → Task 2 (fallback) + Task 4 (prompt rule); Decision 5 → Task 3; Decision 6 → Task 5; acceptance gates → Task 6; README + pin-drift → Task 5.
- Types: `_pip_install_payload(cmd: str) -> list[str] | None` (Task 3); `POETRY_VERSION: str` (Tasks 4, 5); `_scan_optional_dependencies(project, poetry_meta)` (Task 2) — call-site updated in the same task.
- Known conservative behavior (documented, intended): in a poetry project `pip install --upgrade pip` fails the L1 check (payload `pip` is not the bootstrap); the repair loop removes such lines.
