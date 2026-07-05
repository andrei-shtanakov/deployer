# CLI Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `deployer verify` never loses failure detail (full FAILED output + persisted `verify-report.json`), and both subcommands reject bad arguments with `error:` + exit 2 instead of tracebacks.

**Architecture:** All changes live in `src/deployer/cli.py` (the library layer is untouched). `_print_report` prints full FAILED message tails; `_cmd_verify` persists the `VerificationReport` to `<project>/.deployer/verify-report.json`; `_load_target` returns `DeployTarget | str` (error message) following the file's `_timeout_error` idiom; both command functions run all exit-2 validations (project dir → max-iterations → timeouts → target) before any exit-1 concern or work.

**Tech Stack:** Python 3.12, pydantic v2, pytest (uv-managed: `uv run pytest`), ruff, pyrefly.

Spec: `docs/superpowers/specs/2026-07-05-cli-hardening-design.md`.

## Global Constraints

- Exit-code semantics: **2** = invalid invocation (bad flag values, project path not a directory, `--target` missing/unreadable/malformed/invalid); **1** = verification concern failed (missing Dockerfile in `verify`, failed checks, authoring not successful); **0** = success.
- Validation order in both subcommands: (1) `project.is_dir()`, (2) `--max-iterations` (author only), (3) timeouts, (4) `--target` loading — all before any exit-1 check or work.
- FAILED tails indent with exactly **seven spaces** (width of the `[FAIL] ` prefix). WARNING/SKIPPED stay first-line-only. PASSED unchanged.
- `verify-report.json` is written on **both** pass and fail, latest run only (overwrite; cross-run history is out of scope).
- One error idiom per file: helpers return the problem, the command function prints `error: …` to stderr and returns 2.
- No bare traceback can escape from target loading.
- Package management via `uv` only. Line length 88. Run `uv run ruff format .`, `uv run ruff check .`, and `uv run pyrefly check` before each commit. Default pytest run excludes docker/llm marks — expected.

## Preamble: create the feature branch

- [ ] From a clean `master`, run:

```bash
git checkout -b feature/cli-hardening
```

---

### Task 1: Full FAILED output in `_print_report`

**Files:**
- Modify: `src/deployer/cli.py:55-63` (`_print_report`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `VerificationReport`, `CheckResult`, `CheckStatus`, `FailureKind` from `deployer.models` (all existing).
- Produces: `_print_report` behavior later tasks' tests observe — FAILED messages printed in full, tails indented with 7 spaces; WARNING/SKIPPED unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py` (the file already imports `cli`, `CheckResult`, `CheckStatus`; extend the models import):

```python
# at top of file, extend the existing import:
from deployer.models import CheckResult, CheckStatus, FailureKind, VerificationReport
```

Append at the end of the file:

```python
def test_print_report_shows_full_failed_message_only(capsys) -> None:
    report = VerificationReport(
        results=[
            CheckResult(
                check_id="build",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message="compile failed\ngcc: fatal error: killed\nstopped",
            ),
            CheckResult(
                check_id="base_pinned",
                status=CheckStatus.WARNING,
                message="unpinned image\nwarning tail must stay hidden",
            ),
        ],
        docker_available=True,
    )
    cli._print_report(report)
    out = capsys.readouterr().out
    assert "[FAIL] build: compile failed" in out
    assert "\n       gcc: fatal error: killed\n" in out  # 7-space alignment
    assert "\n       stopped\n" in out
    assert "warning tail must stay hidden" not in out  # WARNING stays one line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_print_report_shows_full_failed_message_only -v`
Expected: FAIL — the assertion on `"gcc: fatal error"` fails because `_print_report` prints only the first line.

- [ ] **Step 3: Implement in `src/deployer/cli.py`**

Replace `_print_report` (lines 55-63):

```python
def _print_report(report: VerificationReport) -> None:
    for result in report.results:
        icon = _STATUS_ICONS[result.status]
        line = f"[{icon:>4}] {result.check_id}"
        if result.message:
            first, *rest = result.message.splitlines()
            line += f": {first}"
            if result.status is CheckStatus.FAILED:
                line += "".join(f"\n       {tail}" for tail in rest)
        print(line)
    if not report.docker_available:
        print("note: no container runtime found; static-only verification")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 5: Full check + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest
git add src/deployer/cli.py tests/test_cli.py
git commit -m "feat: print full FAILED check messages in CLI report"
```

---

### Task 2: Persist `verify-report.json`

**Files:**
- Modify: `src/deployer/cli.py` (`_cmd_verify`, currently lines 66-87; `_cmd_author` mkdir at line 113)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `VerificationReport.model_dump_json` / `model_validate_json` (pydantic v2, existing).
- Produces: `<project>/.deployer/verify-report.json` written by every `deployer verify` run (pass and fail); a trailing `report: <path>` stdout line.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _make_project(hello_service: Path, tmp_path: Path, dockerfile: str) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text(dockerfile)
    return project


def test_verify_writes_report_json_on_pass(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    monkeypatch.setattr("deployer.cli.detect_container_tool", lambda: None)
    assert cli.main(["verify", str(project)]) == 0
    report_path = project / ".deployer" / "verify-report.json"
    report = VerificationReport.model_validate_json(report_path.read_text())
    assert report.results  # round-trips and is non-empty


def test_verify_writes_report_json_on_fail(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = _make_project(
        hello_service, tmp_path, "FROM python:3.12-slim\nCOPY nope.py .\n"
    )
    monkeypatch.setattr("deployer.cli.detect_container_tool", lambda: None)
    assert cli.main(["verify", str(project)]) == 1
    report_path = project / ".deployer" / "verify-report.json"
    report = VerificationReport.model_validate_json(report_path.read_text())
    failed = [r for r in report.results if r.status is CheckStatus.FAILED]
    assert failed and "nope.py" in failed[0].message  # full detail persisted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k report_json`
Expected: both FAIL with `FileNotFoundError` — `verify-report.json` is not written yet.

- [ ] **Step 3: Implement in `src/deployer/cli.py`**

In `_cmd_verify`, replace the tail (currently `_print_report(report)` / `return 0 if report.passed else 1`):

```python
    _print_report(report)
    report_dir = project / ".deployer"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "verify-report.json"
    report_path.write_text(report.model_dump_json(indent=2))
    print(f"report: {report_path}")
    return 0 if report.passed else 1
```

In `_cmd_author` (line 113), add `parents=True`:

```python
    report_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS.

- [ ] **Step 5: Full check + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest
git add src/deployer/cli.py tests/test_cli.py
git commit -m "feat: persist verify report to .deployer/verify-report.json"
```

---

### Task 3: Argument validation — `_load_target` boundary + ordering + README

**Files:**
- Modify: `src/deployer/cli.py` (`_load_target` at lines 26-29; `_cmd_verify`; `_cmd_author`)
- Modify: `README.md` (Usage section)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `_timeout_error` idiom (existing), `DeployTarget` (existing).
- Produces: `_load_target(path: str | None) -> DeployTarget | str` (str = error message); validation order in both commands: is_dir → max-iterations (author) → timeouts → target, all exit 2, before any exit-1 concern.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_verify_rejects_nondir_project(tmp_path: Path, capsys) -> None:
    assert cli.main(["verify", str(tmp_path / "ghost")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_author_rejects_nondir_project(tmp_path: Path, capsys) -> None:
    assert cli.main(["author", str(tmp_path / "ghost")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_verify_rejects_missing_target_file(tmp_path: Path, capsys) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    code = cli.main(
        ["verify", str(tmp_path), "--target", str(tmp_path / "nope.json")]
    )
    assert code == 2
    assert capsys.readouterr().err.startswith("error:")


def test_author_rejects_missing_target_file(tmp_path: Path, capsys) -> None:
    code = cli.main(
        ["author", str(tmp_path), "--target", str(tmp_path / "nope.json")]
    )
    assert code == 2
    assert capsys.readouterr().err.startswith("error:")


def test_rejects_malformed_target_json(tmp_path: Path) -> None:
    bad = tmp_path / "target.json"
    bad.write_text("{not json")
    assert cli.main(["verify", str(tmp_path), "--target", str(bad)]) == 2
    assert cli.main(["author", str(tmp_path), "--target", str(bad)]) == 2


def test_rejects_target_failing_validation(tmp_path: Path) -> None:
    bad = tmp_path / "target.json"
    bad.write_text('{"service": {"port": "not-a-port"}}')
    assert cli.main(["verify", str(tmp_path), "--target", str(bad)]) == 2
    assert cli.main(["author", str(tmp_path), "--target", str(bad)]) == 2


def test_nondir_project_wins_over_bad_target(tmp_path: Path, capsys) -> None:
    """Pins the validation order Part 2 of the spec exists to fix."""
    code = cli.main(
        [
            "verify",
            str(tmp_path / "ghost"),
            "--target",
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err
    code = cli.main(
        [
            "author",
            str(tmp_path / "ghost"),
            "--target",
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err


def test_missing_dockerfile_still_exit_1(tmp_path: Path) -> None:
    assert cli.main(["verify", str(tmp_path)]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k "nondir or target or dockerfile_still"`
Expected: the nondir/target tests FAIL (missing-target raises `FileNotFoundError` traceback today; nondir paths reach later checks). `test_missing_dockerfile_still_exit_1` may already pass — it pins existing behavior.

- [ ] **Step 3: Implement in `src/deployer/cli.py`**

Extend imports (top of file):

```python
from pydantic import ValidationError
```

Replace `_load_target` (lines 26-29):

```python
def _load_target(path: str | None) -> DeployTarget | str:
    """Load a DeployTarget JSON file; return an error message on failure."""
    if path is None:
        return DeployTarget()
    try:
        return DeployTarget.model_validate_json(Path(path).read_text())
    except OSError as exc:
        return f"cannot read --target file: {exc}"
    except ValidationError as exc:
        return f"--target is not a valid DeployTarget: {exc}"
```

Replace the head of `_cmd_verify` (everything before the `dockerfile_path` check) with:

```python
def _cmd_verify(args: argparse.Namespace) -> int:
    project = Path(args.path)
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    target = _load_target(args.target)
    if isinstance(target, str):
        print(f"error: {target}", file=sys.stderr)
        return 2
    dockerfile_path = project / "Dockerfile"
    if not dockerfile_path.is_file():
        print(f"error: {dockerfile_path} not found", file=sys.stderr)
        return 1
```

(The rest of `_cmd_verify` — the `verify(...)` call and report tail from Task 2 — is unchanged.)

Replace the head of `_cmd_author` (everything before the `author_dockerfile(...)` call) with:

```python
def _cmd_author(args: argparse.Namespace) -> int:
    project = Path(args.path)
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 2
    if args.max_iterations < 1:
        print("error: --max-iterations must be >= 1", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    target = _load_target(args.target)
    if isinstance(target, str):
        print(f"error: {target}", file=sys.stderr)
        return 2
```

(The `author_dockerfile(...)` call and everything after it is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS, including the pre-existing tests (their fixtures use real directories, so the new is_dir check does not affect them).

- [ ] **Step 5: Update README**

In `README.md`, after the Usage code block's trailing comment lines, add:

```markdown
Exit codes: `0` success; `1` verification/authoring failed (including a
missing `Dockerfile` for `verify`); `2` invalid invocation (bad flag
values, project path not a directory, unreadable or invalid `--target`).
`verify` writes its full report to `<project>/.deployer/verify-report.json`
(latest run only).
```

- [ ] **Step 6: Full check + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest
git add src/deployer/cli.py tests/test_cli.py README.md
git commit -m "feat: validate CLI arguments before work — exit 2, no tracebacks"
```

---

### Task 4: Docker-marked smoke check + wrap-up

**Files:**
- No source changes expected; runs the opt-in suite and finishes the branch.

- [ ] **Step 1: Run the docker-marked tests locally (podman present on this machine)**

Run: `uv run pytest -m docker -v`
Expected: all PASS (`test_cli_author_with_real_docker_exits_zero` exercises the reordered `_cmd_author` end-to-end).

- [ ] **Step 2: Final full sweep**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest
```

Expected: clean.

- [ ] **Step 3: Hand off** — implementation complete; proceed per superpowers:finishing-a-development-branch (PR to master, as with PR #1/#2/#3).
