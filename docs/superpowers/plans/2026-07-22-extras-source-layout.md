# Extras + Source-Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a deploy target request optional-dependency extras and give the authoring model deterministic source-layout facts, unblocking the full-service locallogai-backend research target.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-22-extras-source-layout-design.md`. Three new `ProjectFacts` fields (`optional_dependencies`, `root_modules`, `package_dirs`) from the deterministic scanner; `DeployTarget.extras` canonicalized at the model boundary; a library-level `validate_target_against_facts` gate raising `TargetConfigError` (config error → exit 2, never AUTHORING); hints and prompt consume requested extras only; new `extras-job` corpus case proves extra installation at run time via the existing `run_completes` oracle.

**Tech Stack:** Python 3.12, pydantic v2, pytest, uv. Docker-marked tests need podman/docker.

## Global Constraints

- Package management with `uv` only (never pip): `uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`.
- Run `uv run pyrefly check` after every code change; "0 errors (1 warning not shown)" is the pre-existing baseline.
- Type hints on all code; line length 88; docstrings on public APIs.
- Facts are deterministic: missing/unreadable/ambiguous → empty values, never guessed.
- Extra names are canonical PEP 503/685 (lowercase, `_` → `-`) everywhere: `DeployTarget.extras` after validation, `optional_dependencies` keys at scan time.
- Config errors (`TargetConfigError`) map to CLI exit 2 and are never AUTHORING failures.
- Branch `feature/extras-layout` (exists, spec committed). Never commit to `master`.
- `corpus/**` is excluded from pyrefly; corpus project files need no type hints.
- Directory denylist (verbatim from spec): `tests`, `test`, `scripts`, `docs`, `examples`, `data`, `db`, `migrations`, `.venv`, `.git`, `__pycache__`, `.deployer`, plus any dot-directory.

---

### Task 1: Models — facts fields + canonical `DeployTarget.extras`

**Files:**
- Modify: `src/deployer/models.py` (`ProjectFacts` ~line 60, `DeployTarget` ~line 40)
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `ProjectFacts.optional_dependencies: dict[str, list[str]]`, `ProjectFacts.root_modules: list[str]`, `ProjectFacts.package_dirs: list[str]` (all default-empty); `DeployTarget.extras: list[str]` (default `[]`), canonicalized by a `field_validator` (PEP 503/685, empty-string rejection, dedupe preserving first occurrence). Later tasks import nothing new — they use these fields.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_models.py` (extend existing imports as needed; `pytest`, `ValidationError`, `DeployTarget`, `ProjectFacts` are already imported there):

```python
def test_project_facts_layout_fields_default_empty() -> None:
    facts = ProjectFacts()
    assert facts.optional_dependencies == {}
    assert facts.root_modules == []
    assert facts.package_dirs == []


def test_extras_default_empty_and_roundtrip() -> None:
    target = DeployTarget(extras=["gui"])
    parsed = DeployTarget.model_validate_json(target.model_dump_json())
    assert parsed.extras == ["gui"]
    assert DeployTarget().extras == []


def test_extras_canonicalized_and_deduped() -> None:
    target = DeployTarget(extras=["GUI", "my_extra", "my-extra"])
    assert target.extras == ["gui", "my-extra"]


def test_extras_reject_empty_entries() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        DeployTarget(extras=["gui", "  "])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -k "layout_fields or extras" -v`
Expected: FAIL (`optional_dependencies` unknown attribute; `extras` unknown field)

- [ ] **Step 3: Implement**

In `src/deployer/models.py`, extend `ProjectFacts` (after `requirements_files`):

```python
    requirements_files: dict[str, list[str]] = Field(default_factory=dict)
    optional_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    root_modules: list[str] = Field(default_factory=list)
    package_dirs: list[str] = Field(default_factory=list)
```

Extend `DeployTarget` — add the field after `service`/`run` and the validator after `_service_and_run_exclusive`:

```python
    extras: list[str] = Field(default_factory=list)
```

```python
    @field_validator("extras")
    @classmethod
    def _canonicalize_extras(cls, value: list[str]) -> list[str]:
        """PEP 503/685-normalize, reject empties, dedupe keeping first."""
        canonical: list[str] = []
        for raw in value:
            name = raw.strip().lower().replace("_", "-")
            if not name:
                raise ValueError(
                    "DeployTarget.extras entries must be non-empty"
                )
            if name not in canonical:
                canonical.append(name)
        return canonical
```

(`field_validator` is already imported in models.py.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: all PASS

- [ ] **Step 5: Format, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/models.py tests/test_models.py
git commit -m "feat: layout facts fields + canonical DeployTarget.extras"
```

---

### Task 2: Scanner — optional-dependencies, root modules, package dirs

**Files:**
- Modify: `src/deployer/facts.py`
- Test: `tests/test_facts.py`

**Interfaces:**
- Consumes: `ProjectFacts` fields from Task 1.
- Produces: `analyze_project` fills `optional_dependencies` (normalized keys; collision → `{}`), `root_modules` (root `*.py` minus `_ENTRYPOINT_DENYLIST`, sorted), `package_dirs` (dirs with `__init__.py` at root and one level under `src/` as `"src/<pkg>"`, minus `_DIR_DENYLIST` and dot-dirs, sorted).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_facts.py` (it already imports `analyze_project` and `Path`; follow its existing tmp_path style):

```python
def test_optional_dependencies_scanned_and_normalized(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        "[project.optional-dependencies]\n"
        'My_GUI = ["gradio>=6.0"]\n'
        'inference = ["llama-cpp-python>=0.2"]\n'
    )
    facts = analyze_project(tmp_path)
    assert facts.optional_dependencies == {
        "my-gui": ["gradio>=6.0"],
        "inference": ["llama-cpp-python>=0.2"],
    }


def test_optional_dependencies_collision_yields_no_fact(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n'
        "[project.optional-dependencies]\n"
        'my_extra = ["a"]\n'
        'my-extra = ["b"]\n'
    )
    assert analyze_project(tmp_path).optional_dependencies == {}


def test_root_modules_respect_file_denylist(tmp_path: Path) -> None:
    for name in ("app.py", "main.py", "setup.py", "conftest.py"):
        (tmp_path / name).write_text("x = 1\n")
    assert analyze_project(tmp_path).root_modules == ["app.py", "main.py"]


def test_package_dirs_root_src_and_denylist(tmp_path: Path) -> None:
    for pkg in ("agents", "tests", ".hidden"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("")
    (tmp_path / "data").mkdir()  # no __init__.py -> not a package
    src_pkg = tmp_path / "src" / "foo"
    src_pkg.mkdir(parents=True)
    (src_pkg / "__init__.py").write_text("")
    facts = analyze_project(tmp_path)
    assert facts.package_dirs == ["agents", "src/foo"]


def test_layout_facts_empty_without_pyproject(tmp_path: Path) -> None:
    facts = analyze_project(tmp_path)
    assert facts.optional_dependencies == {}
    assert facts.root_modules == []
    assert facts.package_dirs == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_facts.py -k "optional_dependencies or root_modules or package_dirs or layout_facts" -v`
Expected: FAIL (fields stay empty / assertions unmet)

- [ ] **Step 3: Implement the scanners**

In `src/deployer/facts.py`, add after `_ENTRYPOINT_DENYLIST`:

```python
_DIR_DENYLIST = frozenset(
    {
        "tests",
        "test",
        "scripts",
        "docs",
        "examples",
        "data",
        "db",
        "migrations",
        ".venv",
        ".git",
        "__pycache__",
        ".deployer",
    }
)


def _normalize_extra(raw: str) -> str:
    """PEP 503/685-style extra-name normalization."""
    return raw.strip().lower().replace("_", "-")
```

Add the three scan helpers before `analyze_project`:

```python
def _scan_optional_dependencies(project: dict[str, Any]) -> dict[str, list[str]]:
    """Normalized [project.optional-dependencies]; key collision -> {}.

    Two raw keys normalizing to the same name (my_extra + my-extra) make
    the metadata ambiguous — ambiguous metadata is no fact.
    """
    raw = project.get("optional-dependencies", {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        name = _normalize_extra(key)
        if name in result:
            return {}
        result[name] = [d for d in value if isinstance(d, str)]
    return result


def _scan_root_modules(path: Path) -> list[str]:
    """Root-level *.py files minus the entrypoint file denylist, sorted."""
    try:
        return sorted(
            f.name
            for f in path.glob("*.py")
            if f.is_file() and f.name not in _ENTRYPOINT_DENYLIST
        )
    except OSError:
        return []


def _package_dirs_in(base: Path, prefix: str) -> list[str]:
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    found: list[str] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in _DIR_DENYLIST or entry.name.startswith("."):
            continue
        if (entry / "__init__.py").is_file():
            found.append(f"{prefix}{entry.name}")
    return found


def _scan_package_dirs(path: Path) -> list[str]:
    """Package dirs (with __init__.py) at root and one level under src/."""
    dirs = _package_dirs_in(path, prefix="")
    src = path / "src"
    if src.is_dir():
        dirs.extend(_package_dirs_in(src, prefix="src/"))
    return sorted(dirs)
```

In `analyze_project`, extend the returned `ProjectFacts`:

```python
    return ProjectFacts(
        name=name,
        requires_python=requires_python,
        python_version=python_version,
        dependencies=dependencies,
        entrypoints=entrypoints,
        has_uv_lock=has_uv_lock,
        package_manager=package_manager,
        has_build_system=isinstance(pyproject.get("build-system"), dict),
        script_entrypoint=_scan_script_entrypoint(path),
        requirements_files=requirements_files,
        optional_dependencies=_scan_optional_dependencies(project),
        root_modules=_scan_root_modules(path),
        package_dirs=_scan_package_dirs(path),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_facts.py -v`
Expected: all PASS

- [ ] **Step 5: Format, typecheck, run full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/facts.py tests/test_facts.py
git commit -m "feat: scan optional-dependencies and source layout facts"
```

---

### Task 3: `TargetConfigError` + `validate_target_against_facts` wiring

**Files:**
- Modify: `src/deployer/facts.py` (new exception + validator function)
- Modify: `src/deployer/author.py` (`author_dockerfile`, after `analyze_project` ~line 82)
- Modify: `src/deployer/verify.py` (`verify()`, top of function ~line 712)
- Modify: `src/deployer/cli.py` (`_cmd_verify` ~line 159, `_cmd_author` ~line 201)
- Test: `tests/test_facts.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `ProjectFacts.optional_dependencies` (Task 2), canonical `DeployTarget.extras` (Task 1).
- Produces: `TargetConfigError(ValueError)` and `validate_target_against_facts(target: DeployTarget, facts: ProjectFacts) -> None` in `deployer.facts`. `verify()` raises it when facts are provided (or when `target.extras` is non-empty and `facts is None`); `author_dockerfile` raises it before the first generate; CLI maps it to exit 2. Bench paths need no change: `_cmd_bench_run` and `_cmd_bench_verify` already catch `ValueError` → exit 2, which `TargetConfigError` subclasses.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_facts.py` (add imports: `from deployer.facts import TargetConfigError, validate_target_against_facts` and `from deployer.models import DeployTarget, ProjectFacts` — merge with existing import lines):

```python
def test_validate_extras_ok_and_noop() -> None:
    facts = ProjectFacts(optional_dependencies={"gui": ["gradio>=6.0"]})
    validate_target_against_facts(DeployTarget(extras=["GUI"]), facts)
    validate_target_against_facts(DeployTarget(), ProjectFacts())


def test_validate_unknown_extra_raises() -> None:
    facts = ProjectFacts(optional_dependencies={"gui": []})
    with pytest.raises(TargetConfigError, match="inference"):
        validate_target_against_facts(
            DeployTarget(extras=["inference"]), facts
        )


def test_validate_pip_without_build_system_rejects_extras() -> None:
    facts = ProjectFacts(
        optional_dependencies={"gui": []},
        package_manager="pip",
        has_build_system=False,
    )
    with pytest.raises(TargetConfigError, match="build-system"):
        validate_target_against_facts(DeployTarget(extras=["gui"]), facts)
```

Append to `tests/test_cli.py` (follow the file's existing style — it calls `cli.main([...])` on tmp_path projects; reuse its project-scaffolding helper if one exists, else write files directly):

```python
def test_verify_unknown_extra_exits_2(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    target = tmp_path / "target.json"
    target.write_text('{"extras": ["nope"]}')
    assert (
        cli.main(
            ["verify", str(tmp_path), "--target", str(target), "--no-docker"]
        )
        == 2
    )


def test_author_unknown_extra_exits_2(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    target = tmp_path / "target.json"
    target.write_text('{"extras": ["nope"]}')
    assert (
        cli.main(
            ["author", str(tmp_path), "--target", str(target), "--no-docker"]
        )
        == 2
    )
```

Note: check `deployer verify --help` output first — if `verify` has no
`--no-docker` flag (it may auto-detect the runtime), drop that argument;
the validation error must fire before any container work either way. The
author test needs no LLM: validation raises before `author.generate` is
ever called — but if the CLI constructs the Anthropic author before
calling `author_dockerfile` and that requires an API key, use the same
author-selection flag the existing author tests in this file use.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_facts.py tests/test_cli.py -k "validate or unknown_extra" -v`
Expected: FAIL with `ImportError: cannot import name 'TargetConfigError'`

- [ ] **Step 3: Implement the validator**

In `src/deployer/facts.py` (after the imports, before the regex constants), add — and extend the module imports with `from deployer.models import DeployTarget, ProjectFacts`:

```python
class TargetConfigError(ValueError):
    """Deploy target asks for something the project facts cannot satisfy.

    A config error, not an authoring failure: the model cannot fix it,
    so it must surface as CLI exit 2 before any authoring/verification.
    """


def validate_target_against_facts(
    target: DeployTarget, facts: ProjectFacts
) -> None:
    """Config-level compatibility gate between intent and scanned facts."""
    if not target.extras:
        return
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
```

(`target.extras` is already canonical via the Task-1 validator and
`optional_dependencies` keys are normalized at scan time, so plain `in`
comparison is correct.)

- [ ] **Step 4: Wire into `verify()`, `author_dockerfile`, CLI**

`src/deployer/verify.py` — extend the import from `deployer.facts`-adjacent modules (verify.py currently imports models only; add `from deployer.facts import TargetConfigError, validate_target_against_facts`) and add at the very top of `verify()`'s body, before `verify_static`:

```python
    if facts is not None:
        validate_target_against_facts(target, facts)
    elif target.extras:
        raise TargetConfigError(
            "deploy target requests extras but no project facts were "
            "provided to validate them against"
        )
```

Check for an import cycle: `facts.py` must import models only (it does — the Task-3 addition imports `DeployTarget`/`ProjectFacts` from models); `verify.py` importing `facts` is new but acyclic (facts does not import verify).

`src/deployer/author.py` — after `facts = analyze_project(project_path)` add:

```python
    validate_target_against_facts(target, facts)
```

with the import `from deployer.facts import analyze_project, validate_target_against_facts`.

`src/deployer/cli.py` — in `_cmd_verify`, wrap the `verify(...)` call:

```python
    try:
        report = verify(
            ...existing arguments unchanged...
        )
    except TargetConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

and in `_cmd_author`, wrap the `author_dockerfile(...)` call the same way:

```python
    try:
        run = author_dockerfile(
            ...existing arguments unchanged...
        )
    except TargetConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

Add `TargetConfigError` to the existing `from deployer.facts import analyze_project` import. Do NOT touch `_cmd_bench_run`/`_cmd_bench_verify` — their existing `ValueError` handlers already map it to exit 2; verify this by reading those handlers, and note it in your report.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_facts.py tests/test_cli.py -v`
Expected: all PASS

- [ ] **Step 6: Format, typecheck, full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/facts.py src/deployer/author.py src/deployer/verify.py src/deployer/cli.py tests/test_facts.py tests/test_cli.py
git commit -m "feat: TargetConfigError gate for extras (config error, exit 2)"
```

---

### Task 4: Hints by requested extras + prompt rules

**Files:**
- Modify: `src/deployer/hints.py` (`collect_hints` ~line 62)
- Modify: `src/deployer/author.py` (`collect_hints` call ~line 83)
- Modify: `src/deployer/llm.py` (`SYSTEM_PROMPT`, `collect_hints` call in `_context_blocks` ~line 81)
- Test: `tests/test_hints.py`, `tests/test_llm.py`

**Interfaces:**
- Consumes: `ProjectFacts.optional_dependencies`, canonical `target.extras`.
- Produces: `collect_hints(facts: ProjectFacts, extras: Sequence[str] = ()) -> list[SystemDepHint]` — base deps + requirements + deps of the requested extras only. Both call sites pass `target.extras`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hints.py` (follow its import style; it imports `collect_hints` and `ProjectFacts`):

```python
def test_requested_extra_deps_fire_hints() -> None:
    facts = ProjectFacts(
        optional_dependencies={
            "inference": ["llama-cpp-python>=0.2.0"],
            "gui": ["gradio>=6.0"],
        }
    )
    names = [h.python_package for h in collect_hints(facts, ["inference"])]
    assert names == ["llama-cpp-python"]


def test_unrequested_extras_stay_silent() -> None:
    facts = ProjectFacts(
        optional_dependencies={"inference": ["llama-cpp-python>=0.2.0"]}
    )
    assert collect_hints(facts) == []
    assert collect_hints(facts, ["gui"]) == []
```

Append to `tests/test_llm.py`:

```python
def test_prompt_includes_extras_and_layout_facts() -> None:
    facts = ProjectFacts(
        optional_dependencies={"gui": ["gradio>=6.0"]},
        root_modules=["app.py", "main.py"],
        package_dirs=["agents"],
    )
    target = DeployTarget(extras=["gui"])
    rendered = _context_blocks(facts, target)
    assert '"extras"' in rendered and '"gui"' in rendered
    assert "app.py" in rendered and "agents" in rendered


def test_prompt_hints_follow_requested_extras() -> None:
    facts = ProjectFacts(
        optional_dependencies={"inference": ["llama-cpp-python>=0.2.0"]}
    )
    with_extra = _context_blocks(facts, DeployTarget(extras=["inference"]))
    without = _context_blocks(facts, DeployTarget())
    assert "llama-cpp-python" in with_extra
    assert "llama-cpp-python" not in without


def test_system_prompt_states_extras_and_copy_rules() -> None:
    assert "--extra" in SYSTEM_PROMPT
    assert "root_modules" in SYSTEM_PROMPT and "package_dirs" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hints.py tests/test_llm.py -v`
Expected: new tests FAIL (extras param unknown / prompt assertions unmet)

- [ ] **Step 3: Implement**

`src/deployer/hints.py` — change `collect_hints` (add `from collections.abc import Sequence` to imports):

```python
def collect_hints(
    facts: ProjectFacts, extras: Sequence[str] = ()
) -> list[SystemDepHint]:
    """Match project dependencies against the curated table.

    Top-level dependencies only (pyproject deps + requirements files +
    the dependencies of *requested* extras); transitive no-wheel packages
    stay invisible and fall through to the repair loop — a documented
    limitation, not a bug. Unrequested extras never fire hints.
    """
    candidates: set[str] = set()
    for dep in facts.dependencies:
        candidates.add(_normalize(dep))
    for entries in facts.requirements_files.values():
        for entry in entries:
            if entry.startswith("-"):
                continue
            candidates.add(_normalize(entry))
    for extra in extras:
        for dep in facts.optional_dependencies.get(extra, []):
            candidates.add(_normalize(dep))
    hints: list[SystemDepHint] = []
    for name in sorted(candidates):
        hint = KNOWN_SYSTEM_DEPS.get(name)
        if hint is not None and (hint.build_packages or hint.runtime_packages):
            hints.append(hint.model_copy(deep=True))
    return hints
```

`src/deployer/author.py` line ~83: `hints = collect_hints(facts, target.extras)`

`src/deployer/llm.py` `_context_blocks`: `hints = collect_hints(facts, target.extras)`

`src/deployer/llm.py` `SYSTEM_PROMPT` — insert these two rules after the "If has_build_system is false…" bullet:

```text
- Extras listed in the deploy intent MUST be installed — and ONLY those
  extras, never every group in optional_dependencies. Use the package
  manager's mechanism: `uv sync --extra <name>` (adding
  `--no-install-project` when has_build_system is false), or
  `pip install ".[name]"` for installable pip projects.
- When copying application source, use root_modules and package_dirs
  from the facts; a package dir is copied whole. Do not COPY directories
  outside these facts unless the deploy intent explicitly requires it.
  This governs source code only — copy metadata and lockfiles
  (pyproject.toml, uv.lock, requirements files) per the install
  strategy above.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hints.py tests/test_llm.py -v`
Expected: all PASS

- [ ] **Step 5: Format, typecheck, full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/hints.py src/deployer/author.py src/deployer/llm.py tests/test_hints.py tests/test_llm.py
git commit -m "feat: hints and prompt consume requested extras + layout facts"
```

---

### Task 5: Corpus `extras-job` case + locallogai service target

**Files:**
- Create: `corpus/synthetic/extras-job/project/pyproject.toml`
- Create: `corpus/synthetic/extras-job/project/main.py`
- Create: `corpus/synthetic/extras-job/project/uv.lock` (generated)
- Create: `corpus/synthetic/extras-job/target.json`
- Create: `corpus/synthetic/extras-job/expected.json`
- Create: `corpus/synthetic/extras-job/fixture.Dockerfile`
- Modify: `corpus/external.toml` (locallogai-backend entry)
- Test: `tests/test_bench.py`

**Interfaces:**
- Consumes: everything above; existing `load_corpus` from `deployer.bench`.
- Produces: 7th synthetic case; locallogai external entry with service+extras target and `expected_success = false`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py` (same style as the existing `test_no_build_system_is_a_job_case`):

```python
def test_extras_job_case_shape() -> None:
    corpus = Path(__file__).parent.parent / "corpus" / "synthetic"
    case = next(c for c in load_corpus(corpus) if c.name == "extras-job")
    assert case.target.extras == ["cli"]
    assert case.target.run is not None
    assert case.target.run.expect_stdout == "hello from extras-job"


def test_extras_job_facts() -> None:
    from deployer.facts import analyze_project

    project = (
        Path(__file__).parent.parent
        / "corpus"
        / "synthetic"
        / "extras-job"
        / "project"
    )
    facts = analyze_project(project)
    assert "cli" in facts.optional_dependencies
    assert facts.script_entrypoint == "main.py"
    assert facts.has_build_system is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bench.py -k extras_job -v`
Expected: FAIL (`StopIteration` — case does not exist)

- [ ] **Step 3: Create the corpus case**

`corpus/synthetic/extras-job/project/pyproject.toml`:

```toml
[project]
name = "extras-job"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.optional-dependencies]
cli = ["cowsay>=6.0"]
```

`corpus/synthetic/extras-job/project/main.py`:

```python
"""Job that proves the 'cli' extra is installed at run time."""

if __name__ == "__main__":
    import cowsay  # noqa: F401  # ImportError here = extra not installed

    print("hello from extras-job")
```

Generate the lockfile (network access needed once):

```bash
cd corpus/synthetic/extras-job/project && uv lock && cd -
```

`corpus/synthetic/extras-job/target.json`:

```json
{"extras": ["cli"], "run": {"expect_stdout": "hello from extras-job"}}
```

`corpus/synthetic/extras-job/expected.json`:

```json
{"capabilities": ["uv", "extras", "run-check"], "notes": "extra-only import under run_completes proves the extra is installed, not just that the image builds"}
```

`corpus/synthetic/extras-job/fixture.Dockerfile` (mirror the other uv fixtures):

```dockerfile
FROM ghcr.io/astral-sh/uv:0.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra cli
COPY main.py ./
CMD ["uv", "run", "--no-sync", "python", "main.py"]
```

- [ ] **Step 4: Update `corpus/external.toml`**

Replace the locallogai-backend entry's `[targets.expected]` block and add a `[targets.target]` block, keeping name/url/commit unchanged:

```toml
[targets.target]
extras = ["gui"]
[targets.target.service]
port = 7860
healthcheck_path = "/"
[targets.expected]
expected_success = false
max_iterations = 3
requires_l2 = true
capabilities = ["uv", "external", "service", "extras", "python-3.13"]
notes = "service entrypoint disambiguation missing: script_entrypoint resolves to the main.py stub and the prompt rule makes it binding, while the real Gradio app is app.py — success requires violating an authoring contract. Flip to true when a service-entrypoint fact/intent lands (see 2026-07-22 spec, Deferred)."
```

Also update the file's header comment sentence that says extras cannot be expressed yet — extras are now expressible; the blocker is service-entrypoint disambiguation.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_bench.py -v`
Expected: all PASS

- [ ] **Step 6: Docker smoke for the new case**

Run: `uv run deployer bench verify --filter extras-job`
Expected: the case verifies green (build + run_completes) on the local podman. If the CLI flag for filtering differs, check `uv run deployer bench verify --help` and use the pattern argument it documents.

- [ ] **Step 7: Format, typecheck, full unit suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add corpus/synthetic/extras-job corpus/external.toml tests/test_bench.py
git commit -m "feat: extras-job corpus case; locallogai-backend service target"
```

---

### Task 6: Acceptance sweep

**Files:**
- No production code changes expected.

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Full local sweep**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
uv run pytest
uv run pytest -m docker
```

Expected: all clean/green (docker suite now includes the extras-job corpus smoke).

- [ ] **Step 2: Fixture bench acceptance**

Run: `uv run deployer bench run --author fixture --label extras-fixture`
Expected: **7/7 matched**, success rate 1.0.

- [ ] **Step 3: Real-project facts spot-check**

Run: `uv run python -c "from deployer.facts import analyze_project; from pathlib import Path; f = analyze_project(Path('/Users/Andrei_Shtanakov/lab_aist/locallogai-backend')); print(sorted(f.optional_dependencies), f.root_modules, f.package_dirs)"`
Expected: extras include `gui` and `inference`; `root_modules` includes `app.py`, `main.py`, `webui.py`; `package_dirs` includes `agents` (exact list may contain more — record what it prints in the report).

- [ ] **Step 4: Commit anything the sweep touched, else no-op**

```bash
git status --short
```

Expected: clean tree (Steps 1-3 change nothing). If formatting touched files, commit them:

```bash
git add -A && git commit -m "chore: acceptance sweep formatting"
```

- [ ] **Step 5: Record manual follow-ups (controller/operator, not this task)**

Remaining acceptance from the spec, run manually at PR time: `--author anthropic` bench run (synthetic only, `.env` must be sourced) → 7/7 → `bench promote` (golden grows to 7 cases) → `bench compare` clean; separate locallogai-service research run (`--include-external`), outcome recorded in `.superpowers/sdd/progress.md`, `expected_success` adjusted only on evidence.
