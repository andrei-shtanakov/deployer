# QoL Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three Phase-4b friction points: `.env` auto-load for the anthropic author, `bench --filter` matching external targets, and an L1 check that the authored command honors the `entrypoint` intent.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-22-qol-bundle-design.md`. Three independent components: a narrow dependency-free dotenv loader in `cli.py` (auth-only scope, env wins); a filtering restructure in `run_bench` (externals fnmatch'd before cloning, combined-empty error); a `_check_entrypoint_in_command` static check scoped to the FINAL Dockerfile stage, added to `verify_static` via a new optional `target` parameter.

**Tech Stack:** Python 3.12, pytest, uv. Docker-marked tests need podman/docker.

## Global Constraints

- Package management with `uv` only: `uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`; "0 errors (1 warning not shown)" is the pre-existing pyrefly baseline.
- Type hints; line length 88; docstrings on public APIs.
- The dotenv loader: only `KEY=VALUE`; key regex `[A-Za-z_][A-Za-z0-9_]*`; no `export`, no interpolation, no escapes, no multiline; quotes stripped only when the whole value is wrapped in matching quotes; `os.environ.setdefault` (env always wins); values never logged. Auth-only: called ONLY before the two `AnthropicAuthor()` constructions, never before `verify`, never from library code.
- The L1 check considers ONLY the final stage (instructions after the last `FROM`); a builder-stage `CMD` must not satisfy it.
- Branch `feature/qol-bundle` (exists, spec committed). Never commit to `master`.

---

### Task 1: `.env` auto-load (auth-only)

**Files:**
- Modify: `src/deployer/cli.py` (new `_load_dotenv` helper; call sites at the `AnthropicAuthor()` constructions in `_cmd_author` ~line 205 and `_cmd_bench_run` ~line 248)
- Modify: `README.md` (usage section: one line about `.env` auto-load, auth-only scope)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `_load_dotenv(path: Path = Path(".env")) -> None` in `deployer.cli`. No other task depends on it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (the file already imports `cli`, `Path`, `pytest`; add `import os` to its imports if absent):

```python
def test_load_dotenv_sets_missing_and_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_env: dict[str, str] = {"EXISTING": "env-value"}
    monkeypatch.setattr(cli.os, "environ", fake_env)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "ANTHROPIC_API_KEY=from-file\n"
        "QUOTED='q-value'\n"
        'DOUBLE="d-value"\n'
        "HALF='not-stripped\n"
        "BAD KEY=skipped\n"
        "export EXPORTED=skipped\n"
        "1BAD=skipped\n"
        "EXISTING=file-value\n"
        "NOEQUALS\n"
    )
    cli._load_dotenv(env_file)
    assert fake_env["ANTHROPIC_API_KEY"] == "from-file"
    assert fake_env["QUOTED"] == "q-value"
    assert fake_env["DOUBLE"] == "d-value"
    assert fake_env["HALF"] == "'not-stripped"
    assert fake_env["EXISTING"] == "env-value"
    assert "EXPORTED" not in fake_env
    assert "1BAD" not in fake_env
    assert "BAD KEY" not in fake_env


def test_load_dotenv_missing_file_is_noop(tmp_path: Path) -> None:
    cli._load_dotenv(tmp_path / "absent.env")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k load_dotenv -v`
Expected: FAIL with `AttributeError: ... has no attribute '_load_dotenv'`

- [ ] **Step 3: Implement the loader and call sites**

In `src/deployer/cli.py` (add `import os` to the imports if absent; `re` and `Path` are already imported). Add near the other module-level helpers:

```python
_DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Narrow KEY=VALUE loader for Anthropic author auth; env always wins.

    Intentionally not a general dotenv: no `export`, no interpolation,
    no escapes, no multiline. Quotes are stripped only when the whole
    value is wrapped in matching quotes. Runtime env defaults
    (DEPLOYER_CONTAINER_*) are resolved before the author is
    constructed and never come from this file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not _DOTENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
```

Call sites — exactly two, both immediately before an `AnthropicAuthor()` construction:

In `_cmd_author`, directly above the `try:` that wraps `author_dockerfile(...)` (the construction is inside the call args):

```python
    _load_dotenv()
    try:
        run = author_dockerfile(
```

In `_cmd_bench_run`, inside the anthropic branch:

```python
    if args.author == "anthropic":
        _load_dotenv()
        shared = AnthropicAuthor()
```

Do NOT touch `_cmd_verify` or any library module.

- [ ] **Step 4: Update README**

In `README.md`, in the usage/author section (near the exit-codes or ANTHROPIC_API_KEY mention — find the right spot by reading it), add one line:

```text
`author` and `bench run --author anthropic` auto-load `./.env`
(KEY=VALUE lines) for the Anthropic API key; real environment variables
always win, and runtime flags (`DEPLOYER_CONTAINER_*`) are NOT read
from `.env`.
```

- [ ] **Step 5: Run tests, format, typecheck, commit**

```bash
uv run pytest tests/test_cli.py -v && uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/cli.py tests/test_cli.py README.md
git commit -m "feat: auto-load .env for the anthropic author (env wins, auth-only)"
```

---

### Task 2: `bench --filter` matches external targets

**Files:**
- Modify: `src/deployer/bench.py` (`run_bench` ~lines 351-397)
- Modify: `README.md` (bench section, ~line 82: the "`--filter` applies to synthetic cases only; external targets are included wholesale" sentence)
- Test: `tests/test_bench.py`

**Interfaces:**
- Consumes: existing `load_corpus`, `load_external`, `clone_external`, `fnmatch` (already imported in bench.py).
- Produces: `run_bench` filters externals by the same pattern BEFORE cloning; "no cases" error only when synthetic and (if included) external names both fail to match.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bench.py`. The file has corpus-building helpers (used by the existing `run_bench`/external tests — read the nearest existing test that builds a tmp corpus with an `external.toml` and mirror its helper usage exactly). The tests to add (adapt the corpus-construction lines to those helpers):

```python
def test_bench_filter_skips_nonmatching_external_without_cloning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _make_corpus(tmp_path)  # helper: >=1 synthetic case named e.g. "case-a"
    _write_external_toml(corpus, names=["ext-match", "ext-other"])

    cloned: list[str] = []
    real_clone = bench.clone_external

    def tracking_clone(ext, dest_root):
        cloned.append(ext.name)
        return real_clone(ext, dest_root)

    monkeypatch.setattr(bench, "clone_external", tracking_clone)
    report, _ = bench.run_bench(
        corpus,
        make_author=lambda case: None,  # every case skips (no fixture author)
        runtime=None,
        label="t",
        author_backend="fixture",
        pattern="ext-match",
        runs_root=tmp_path / "runs",
        include_external=True,
    )
    assert cloned == ["ext-match"]
    assert [c.case for c in report.cases] == ["ext-match"]


def test_bench_filter_no_match_anywhere_raises(tmp_path: Path) -> None:
    corpus = _make_corpus(tmp_path)
    _write_external_toml(corpus, names=["ext-a"])
    with pytest.raises(ValueError, match="no corpus cases match"):
        bench.run_bench(
            corpus,
            make_author=lambda case: None,
            runtime=None,
            label="t",
            author_backend="fixture",
            pattern="zzz-*",
            runs_root=tmp_path / "runs",
            include_external=True,
        )


def test_bench_filter_synthetic_only_still_raises(tmp_path: Path) -> None:
    corpus = _make_corpus(tmp_path)
    with pytest.raises(ValueError, match="no corpus cases match"):
        bench.run_bench(
            corpus,
            make_author=lambda case: None,
            runtime=None,
            label="t",
            author_backend="fixture",
            pattern="zzz-*",
            runs_root=tmp_path / "runs",
            include_external=False,
        )
```

Notes for adapting: if the existing helpers name things differently (`_make_corpus`, `_write_external_toml` are placeholders for whatever the file actually uses), keep the SEMANTICS: one synthetic case whose name does not match "ext-*"; an external.toml with two targets whose local-path URLs clone successfully the same way existing external tests arrange it. If existing external-test helpers make cloning heavy, the tracking_clone stub may instead return a fabricated minimal `BenchCase` (name, project_dir pointing at a tmp project, default target/expected) without calling `real_clone` — the assertions on `cloned` and `report.cases` are what matter.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_bench.py -k "bench_filter" -v`
Expected: first test FAIL (`ValueError: no corpus cases match pattern 'ext-match'` — synthetic filter empties the list before externals are considered); third PASSES already (guard it stays green).

- [ ] **Step 3: Restructure `run_bench`**

Replace the body's case-selection prologue (current shape: `cases = load_corpus(...)`; `if not cases: raise`; create run dir; `if not include_external: ... return`; `with tempfile...: cases += [clone_external(e) for e in load_external(...)]`) with:

```python
    cases = load_corpus(corpus_root, pattern)
    externals: list[ExternalTarget] = []
    if include_external:
        externals = [
            e
            for e in load_external(corpus_root)
            if fnmatch.fnmatch(e.name, pattern)
        ]
    if not cases and not externals:
        raise ValueError(f"no corpus cases match pattern {pattern!r}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = _create_run_dir(runs_root, stamp, label)
    if not externals:
        report = _run_bench_cases(
            cases,
            make_author,
            runtime,
            label=label,
            author_backend=author_backend,
            run_dir=run_dir,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        return report, run_dir
    with tempfile.TemporaryDirectory(prefix="deployer-external-") as ext_tmp:
        cases = cases + [
            clone_external(ext, Path(ext_tmp)) for ext in externals
        ]
        report = _run_bench_cases(
            cases,
            make_author,
            runtime,
            label=label,
            author_backend=author_backend,
            run_dir=run_dir,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        return report, run_dir
```

(`ExternalTarget` — confirm it is already imported in bench.py; add to the models import if not. Note the branch condition changed from `include_external` to `externals`: `--include-external` with zero matching externals now runs synthetic-only without creating a temp dir — intended.)

- [ ] **Step 4: Update README**

Replace the sentence at README.md ~line 82-83:

```text
`--filter` applies to synthetic
cases only; external targets are included wholesale via `--include-external`.
```

with:

```text
`--filter` applies to synthetic
and (with `--include-external`) external targets alike; non-matching
externals are not even cloned.
```

- [ ] **Step 5: Run tests, format, typecheck, commit**

```bash
uv run pytest tests/test_bench.py -v && uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/bench.py tests/test_bench.py README.md
git commit -m "feat: bench --filter matches external targets before cloning"
```

---

### Task 3: L1 check `entrypoint_in_command`

**Files:**
- Modify: `src/deployer/verify.py` (new helpers before `verify_static`; `verify_static` signature ~line 300; `verify()` call site)
- Test: `tests/test_verify_static.py`

**Interfaces:**
- Consumes: `parse_dockerfile(text) -> list[tuple[str, str]]` (existing), `DeployTarget` (verify.py already imports it).
- Produces: `verify_static(dockerfile, project_path, facts=None, target=None)` — new keyword-only-compatible optional param; check id `"entrypoint_in_command"` appended only when `target.entrypoint` is set and the Dockerfile parses.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py` (it already imports `verify_static`, `CheckStatus`; extend imports with `DeployTarget` from `deployer.models`; mirror its existing tmp_path usage — `verify_static` needs a project dir containing the COPY'd files):

```python
def _by_id(report, check_id):
    return next(r for r in report.results if r.check_id == check_id)


def _entry_target() -> DeployTarget:
    return DeployTarget(entrypoint="app.py")


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "main.py").write_text("y = 2\n")
    return tmp_path


def test_entrypoint_check_absent_without_intent(tmp_path: Path) -> None:
    df = 'FROM python:3.12-slim\nCOPY app.py .\nCMD ["python", "app.py"]\n'
    report = verify_static(df, _project(tmp_path))
    assert all(r.check_id != "entrypoint_in_command" for r in report.results)


def test_entrypoint_in_exec_cmd_passes(tmp_path: Path) -> None:
    df = 'FROM python:3.12-slim\nCOPY app.py .\nCMD ["python", "app.py"]\n'
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_entrypoint_in_shell_cmd_passes(tmp_path: Path) -> None:
    df = "FROM python:3.12-slim\nCOPY app.py .\nCMD python app.py\n"
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_entrypoint_in_entrypoint_with_args_cmd_passes(tmp_path: Path) -> None:
    df = (
        "FROM python:3.12-slim\n"
        "COPY app.py .\n"
        'ENTRYPOINT ["python", "app.py"]\n'
        'CMD ["--port", "8000"]\n'
    )
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_scripts_name_entrypoint_matches(tmp_path: Path) -> None:
    # scripts names are not files: the project dir needs no "serve"
    df = 'FROM python:3.12-slim\nCMD ["serve"]\n'
    report = verify_static(
        df, _project(tmp_path), target=DeployTarget(entrypoint="serve")
    )
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_wrong_cmd_fails_with_both_named(tmp_path: Path) -> None:
    df = 'FROM python:3.12-slim\nCOPY main.py .\nCMD ["python", "main.py"]\n'
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    check = _by_id(report, "entrypoint_in_command")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "app.py" in check.message
    assert "main.py" in check.message


def test_no_command_in_final_stage_fails(tmp_path: Path) -> None:
    df = "FROM python:3.12-slim\nCOPY app.py .\n"
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    check = _by_id(report, "entrypoint_in_command")
    assert check.status is CheckStatus.FAILED
    assert "none" in check.message


def test_builder_stage_cmd_does_not_satisfy_entrypoint(tmp_path: Path) -> None:
    """The spec-review blocker case: a builder-stage CMD must not
    false-pass when the final stage sets no command."""
    df = (
        "FROM python:3.12-slim AS build\n"
        'CMD ["python", "app.py"]\n'
        "FROM python:3.12-slim\n"
        "COPY app.py .\n"
    )
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.FAILED
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_verify_static.py -k entrypoint -v`
Expected: FAIL (`TypeError: verify_static() got an unexpected keyword argument 'target'`)

- [ ] **Step 3: Implement**

In `src/deployer/verify.py`, add before `verify_static`:

```python
def _final_stage_commands(
    instructions: list[tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Last ENTRYPOINT and CMD args after the last FROM (the final stage)."""
    last_from = -1
    for i, (name, _) in enumerate(instructions):
        if name == "FROM":
            last_from = i
    entrypoint: str | None = None
    cmd: str | None = None
    for name, args in instructions[last_from + 1 :]:
        if name == "ENTRYPOINT":
            entrypoint = args
        elif name == "CMD":
            cmd = args
    return entrypoint, cmd


def _check_entrypoint_in_command(
    instructions: list[tuple[str, str]], target: DeployTarget
) -> CheckResult:
    """The image's effective command must reference the operator entrypoint.

    Only the final stage counts: a builder-stage CMD is not the image's
    command. Substring match covers exec form, shell form, and
    [project.scripts] names alike; deliberately conservative (e.g.
    `python -m main` does not satisfy `main.py`).
    """
    assert target.entrypoint is not None
    entry_args, cmd_args = _final_stage_commands(instructions)
    haystack = " ".join(a for a in (entry_args, cmd_args) if a is not None)
    if target.entrypoint in haystack:
        return CheckResult(
            check_id="entrypoint_in_command", status=CheckStatus.PASSED
        )
    return CheckResult(
        check_id="entrypoint_in_command",
        status=CheckStatus.FAILED,
        failure_kind=FailureKind.AUTHORING,
        message=(
            f"entrypoint intent {target.entrypoint!r} not found in image "
            f"command: ENTRYPOINT {entry_args if entry_args is not None else 'none'}, "
            f"CMD {cmd_args if cmd_args is not None else 'none'}"
        ),
    )
```

Change `verify_static`'s signature and body:

```python
def verify_static(
    dockerfile: str,
    project_path: Path,
    facts: ProjectFacts | None = None,
    target: DeployTarget | None = None,
) -> VerificationReport:
```

and inside the `if results[0].status is CheckStatus.PASSED:` block, after the install_strategy branch:

```python
        if target is not None and target.entrypoint is not None:
            results.append(_check_entrypoint_in_command(instructions, target))
```

In `verify()`, pass the target through:

```python
    report = verify_static(dockerfile, project_path, facts, target=target)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_verify_static.py tests/test_verify_run.py -v`
Expected: all PASS (the existing verify tests keep passing — `target` defaults to None everywhere else)

- [ ] **Step 5: Full unit suite, format, typecheck, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: L1 entrypoint_in_command check (final stage only)"
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

Expected: all clean/green.

- [ ] **Step 2: Fixture bench acceptance**

Run: `uv run deployer bench run --author fixture --label qol-fixture`
Expected: **8/8 matched**, rate 1.0 (entrypoint-override's fixture passes the new L1 check).

- [ ] **Step 3: Filter smoke (no clone, no carrier)**

Run: `uv run deployer bench run --author fixture --label qol-filter-smoke --filter entrypoint-override`
Expected: exactly 1 case, matched.

- [ ] **Step 4: Commit anything the sweep touched, else no-op**

```bash
git status --short
```

Expected: clean (gitignored `.deployer-runs/` only).

- [ ] **Step 5: Record manual follow-ups (controller/operator, not this task)**

At PR time, manually: `--author anthropic` bench run **without sourcing `.env`** (the loader is the feature) → 8/8 → `bench promote` (golden gains `entrypoint_in_command` on entrypoint-override — measured-subject change) → `bench compare` clean; `bench run --author anthropic --include-external --filter locallogai-backend` runs exactly one case with no carrier. Record outcomes in `.superpowers/sdd/progress.md`.
