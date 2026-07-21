# Entrypoint Fact + Repair Feedback (Phase 4a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Requirements-only projects get a deterministic `script_entrypoint` fact (fixing both llm-baseline failures), healthcheck failures name the container command so the repair loop can converge, and the deferred `slow-build` corpus case lands.

**Architecture:** A regex guard-scan in `facts.py` fills `ProjectFacts.script_entrypoint`; the fact flows into prompts automatically (facts are serialized wholesale) plus one new `SYSTEM_PROMPT` rule with `[project.scripts]` precedence. `verify.py` gains a best-effort `_image_command` (image inspect of Config.Entrypoint/Cmd) appended to AUTHORING healthcheck-failure messages. `corpus/synthetic/slow-build/` is the no-hint native-build twin of the psycopg2 case.

**Tech Stack:** Python 3.12, pydantic v2, re, pytest (`docker` marker), uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-07-21-bench-remote-verify-design.md`, section "Phase 4a addendum".

## Global Constraints

- uv only; line 88; type hints; docstrings on public APIs. After every task: `uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check` clean; `uv run pytest` green before each commit. Branch `feature/entrypoint-feedback`; never commit to master.
- Guard regex, verbatim from the spec: `if\s+__name__\s*==\s*["']__main__["']\s*:` — root-level `*.py` only, no recursion.
- Detection never guesses: `main.py` among candidates → `main.py`; else exactly one candidate → it; else `None`.
- Prompt rule is prompt-only (NO L1 CMD check) and states `[project.scripts]` precedence.
- `_image_command` is best-effort: an inspect failure must not alter the check result and must not flip it to ENVIRONMENT; obtained from the **image**, never the container.
- MarkupSafe must NOT enter `KNOWN_SYSTEM_DEPS` (`slow-build` is the no-hint case).
- The golden is NOT regenerated in this branch's committed state — after merge-time acceptance, the LLM golden is promoted (acceptance section), which is a separate commit produced by the real run.

---

### Task 1: `script_entrypoint` fact + prompt rule

**Files:**
- Modify: `src/deployer/models.py` (`ProjectFacts` gains the field)
- Modify: `src/deployer/facts.py` (`_scan_script_entrypoint`, wired into `analyze_project`)
- Modify: `src/deployer/llm.py` (`SYSTEM_PROMPT` rule)
- Test: `tests/test_facts.py`, `tests/test_llm.py` (append)

**Interfaces:**
- Produces: `ProjectFacts.script_entrypoint: str | None = None`; `deployer.facts._scan_script_entrypoint(path: Path) -> str | None`; `analyze_project` populates the field. The fact reaches prompts automatically via `facts.model_dump_json` in `_context_blocks` — no llm.py plumbing beyond the SYSTEM_PROMPT rule.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_facts.py` (follow the file's existing tmp-project patterns):

```python
GUARD = 'if __name__ == "__main__":\n    main()\n'


def _py(tmp_path: Path, name: str, body: str = "") -> None:
    (tmp_path / name).write_text(f"def main() -> None:\n    pass\n\n{body}")


def test_script_entrypoint_main_py_with_guard(tmp_path: Path) -> None:
    _py(tmp_path, "main.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "main.py"


def test_script_entrypoint_single_other_guarded_file(tmp_path: Path) -> None:
    _py(tmp_path, "worker.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "worker.py"


def test_script_entrypoint_main_py_wins_over_other_candidates(
    tmp_path: Path,
) -> None:
    _py(tmp_path, "main.py", GUARD)
    _py(tmp_path, "worker.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint == "main.py"


def test_script_entrypoint_ambiguous_is_none(tmp_path: Path) -> None:
    _py(tmp_path, "alpha.py", GUARD)
    _py(tmp_path, "beta.py", GUARD)
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_no_guard_is_none(tmp_path: Path) -> None:
    _py(tmp_path, "app.py")  # no guard: filename convention must NOT win
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_ignores_nested_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(GUARD)
    assert analyze_project(tmp_path).script_entrypoint is None


def test_script_entrypoint_single_quotes_and_spacing(tmp_path: Path) -> None:
    _py(tmp_path, "main.py", "if __name__=='__main__' :\n    main()\n")
    assert analyze_project(tmp_path).script_entrypoint == "main.py"
```

Append to `tests/test_llm.py`:

```python
def test_system_prompt_carries_entrypoint_rule() -> None:
    assert "script_entrypoint" in SYSTEM_PROMPT
    assert "[project.scripts]" in SYSTEM_PROMPT or "entrypoints" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_facts.py tests/test_llm.py -v -k "entrypoint or script"`
Expected: FAIL — `AttributeError`/assertion (field absent, prompt lacks the rule).

- [ ] **Step 3: Implement**

`src/deployer/models.py`, `ProjectFacts` after `has_build_system`:

```python
    script_entrypoint: str | None = None
```

`src/deployer/facts.py` (module level; `import re` if absent):

```python
_MAIN_GUARD = re.compile(r"if\s+__name__\s*==\s*[\"']__main__[\"']\s*:")


def _scan_script_entrypoint(path: Path) -> str | None:
    """Root-level script with a __main__ guard; never guesses.

    main.py wins among candidates; otherwise the fact exists only when
    exactly one candidate does. Ambiguity or absence -> None.
    """
    candidates: list[str] = []
    for file in sorted(path.glob("*.py")):
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _MAIN_GUARD.search(text):
            candidates.append(file.name)
    if "main.py" in candidates:
        return "main.py"
    if len(candidates) == 1:
        return candidates[0]
    return None
```

Wire into `analyze_project`'s returned `ProjectFacts(...)`:
`script_entrypoint=_scan_script_entrypoint(path),`

`src/deployer/llm.py` — add to `SYSTEM_PROMPT` immediately after the
install-strategy block:

```text
- script_entrypoint is deterministic ground truth. If it is set and
  entrypoints ([project.scripts]) is empty, the Dockerfile CMD MUST execute
  that file in exec form (e.g. CMD ["python", "main.py"] or the
  package-manager equivalent). Never invent servers such as http.server,
  never leave a bare interpreter, never run a file not present in the
  facts. When entrypoints is non-empty it wins over script_entrypoint.
```

- [ ] **Step 4: Run tests, full suite, format/lint/typecheck, commit**

Run: `uv run pytest && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
Note: existing corpus/fixture facts tests may assert full `ProjectFacts` equality — a new defaulted field can break exact-equality assertions; update them to include `script_entrypoint` where needed (e.g. hello_service main.py HAS a guard → its facts now carry `"main.py"`).

```bash
git add src/deployer tests
git commit -m "feat: deterministic script_entrypoint fact with prompt rule"
```

---

### Task 2: Healthcheck failure names the container command

**Files:**
- Modify: `src/deployer/verify.py` (`_image_command`, message wiring in `_run_healthcheck`)
- Test: `tests/test_verify_static.py` (append; mocked `container_run`)

**Interfaces:**
- Produces: `deployer.verify._image_command(runtime: ContainerRuntime, tag: str) -> str | None` — best-effort `"ENTRYPOINT <json>, CMD <json>"` string from image inspect; `None` on any failure. Appended as `\ncontainer command: <...>` to `run_healthcheck` failure messages whose `failure_kind` is AUTHORING (both the container-start-failure branch when classified AUTHORING and the poll-exhaustion branch when not a transport failure). ENVIRONMENT failures gain nothing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_verify_static.py` (reuse the file's existing fake-`container_run` dispatch pattern from the cleanup/transport tests):

```python
def _dispatch_with_inspect(inspect_out: str | None):
    """container_run fake: run -d ok, exec fails, image inspect configurable."""

    def fake(runtime, args, **kwargs):
        if args[0] == "run":
            return _fake_proc(0, stdout="cid")
        if args[0] == "exec":
            return _fake_proc(1, stderr="probe refused")
        if args[:2] == ["image", "inspect"]:
            if inspect_out is None:
                raise OSError("inspect exploded")
            return _fake_proc(0, stdout=inspect_out + "\n")
        if args[0] in ("logs", "rm"):
            return _fake_proc(0)
        raise AssertionError(f"unexpected container command: {args}")

    return fake


def test_healthcheck_failure_names_container_command(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify.container_run",
        _dispatch_with_inspect('ENTRYPOINT null, CMD ["python3"]'),
    )
    result = _run_healthcheck(
        DeployTarget(service=ServiceSpec(port=8000)),
        ContainerRuntime(tool="docker"),
        "tag",
        timeout=1,
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert 'container command: ENTRYPOINT null, CMD ["python3"]' in result.message


def test_healthcheck_command_feedback_is_best_effort(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify.container_run", _dispatch_with_inspect(None)
    )
    result = _run_healthcheck(
        DeployTarget(service=ServiceSpec(port=8000)),
        ContainerRuntime(tool="docker"),
        "tag",
        timeout=1,
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING  # not flipped
    assert "container command:" not in result.message
```

(`_run_healthcheck`'s `timeout` parameter is positional in the current
signature — check and match the real signature; `_fake_proc` helper already
exists in this file or is three lines to add. If the poll loop's `time`
calls make `timeout=1` slow, that is acceptable — one second.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_verify_static.py -v -k "container_command or best_effort"`
Expected: FAIL — no `container command:` line is produced today.

- [ ] **Step 3: Implement in `src/deployer/verify.py`**

```python
_IMAGE_COMMAND_FORMAT = (
    "ENTRYPOINT {{json .Config.Entrypoint}}, CMD {{json .Config.Cmd}}"
)


def _image_command(runtime: ContainerRuntime, tag: str) -> str | None:
    """Best-effort ENTRYPOINT/CMD of the built image, for repair feedback.

    Reads the image (never the container, so cleanup cannot race it) and
    swallows every failure: feedback must not change a verdict.
    """
    try:
        proc = container_run(
            runtime,
            ["image", "inspect", "--format", _IMAGE_COMMAND_FORMAT, tag],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()
```

In `_run_healthcheck`, at both AUTHORING failure sites:

- container-start failure branch (`started.returncode != 0`): after
  computing `failure_kind=_classify(...)`, when the kind is AUTHORING,
  append to the message: `command = _image_command(runtime, tag)`, and if
  `command is not None` add `f"\ncontainer command: {command}"`.
- poll-exhaustion branch (the non-transport AUTHORING result): same
  append before constructing the `CheckResult`.

Keep the ENVIRONMENT/transport branches untouched. Suggested shape — a
tiny local helper inside the module to avoid duplicating the append:

```python
def _with_command_feedback(
    message: str, runtime: ContainerRuntime, tag: str
) -> str:
    command = _image_command(runtime, tag)
    if command is None:
        return message
    return f"{message}\ncontainer command: {command}"
```

- [ ] **Step 4: Run tests, full suite (incl. docker), format/lint/typecheck, commit**

Run: `uv run pytest && uv run pytest -m docker && uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check`
(docker suite: healthcheck-failure tests in `tests/test_verify_docker.py` assert on message content — verify none break; the wrong-port test's message will now ALSO carry the command line, which is correct new behavior.)

```bash
git add src/deployer/verify.py tests/test_verify_static.py
git commit -m "feat: healthcheck failures name the image ENTRYPOINT/CMD"
```

---

### Task 3: `slow-build` corpus case

**Files:**
- Create: `corpus/synthetic/slow-build/{project/{main.py,requirements.txt},target.json,expected.json,fixture.Dockerfile}`
- Modify: `tests/test_corpus.py` (`EXPECTED_CASES` 5 → 6)
- Test: `tests/test_corpus.py` (existing parametrized docker test covers the new case automatically), `tests/test_facts.py` (one assertion)

**Interfaces:**
- Produces: the sixth committed corpus case; `EXPECTED_CASES = ["no-build-system", "pip-requirements", "service-healthcheck", "slow-build", "system-deps-psycopg2", "uv-minimal"]`.

- [ ] **Step 1: Write the case files**

`corpus/synthetic/slow-build/project/requirements.txt`:

```text
markupsafe==3.0.2
--no-binary MarkupSafe
```

`corpus/synthetic/slow-build/project/main.py` (mirror the style of
`corpus/synthetic/pip-requirements/project/main.py`; must use markupsafe so
the C extension provably loads, and must keep the `__main__` guard):

```python
"""HTTP service proving the source-built markupsafe extension loads."""

from http.server import BaseHTTPRequestHandler, HTTPServer

from markupsafe import escape


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            body = str(escape("<ok>")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def main() -> None:
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
```

`corpus/synthetic/slow-build/target.json`:

```json
{"service": {"port": 8000, "healthcheck_path": "/health"}}
```

`corpus/synthetic/slow-build/expected.json`:

```json
{"capabilities": ["pip", "service", "slow-build", "no-hint-system-deps"], "notes": "sdist C build with NO hints: the model must derive gcc from the build error"}
```

`corpus/synthetic/slow-build/fixture.Dockerfile`:

```dockerfile
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
```

Do NOT touch `src/deployer/hints.py` — MarkupSafe stays out of
`KNOWN_SYSTEM_DEPS` by design.

- [ ] **Step 2: Update the corpus tests**

`tests/test_corpus.py`: insert `"slow-build"` into `EXPECTED_CASES`
(alphabetical position shown in Interfaces). Append to `tests/test_facts.py`:

```python
def test_slow_build_corpus_case_has_entrypoint_fact() -> None:
    corpus_case = (
        Path(__file__).parent.parent
        / "corpus" / "synthetic" / "slow-build" / "project"
    )
    facts = analyze_project(corpus_case)
    assert facts.script_entrypoint == "main.py"
    assert facts.package_manager == "pip"
```

- [ ] **Step 3: Run unit, then docker corpus suite**

Run: `uv run pytest`
Expected: PASS (unit; parse + static checks over 6 cases).
Run: `uv run pytest tests/test_corpus.py -m docker -v`
Expected: 6/6 including the new case (source build of markupsafe ~ tens of seconds). If the fixture fails, fix the Dockerfile — project files and target are fixed points.

- [ ] **Step 4: Format/lint/typecheck, commit**

```bash
git add corpus tests
git commit -m "feat: slow-build corpus case (no-hint sdist C build)"
```

---

### Task 4: Sweep + research acceptance

**Files:**
- Modify: `.superpowers/sdd/progress.md` (results; gitignored)

- [ ] **Step 1: Full sweep**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest && uv run pytest -m docker`
Expected: all clean/green.

- [ ] **Step 2: Offline acceptance**

```sh
uv run deployer bench verify                       # 6/6 ok
uv run deployer bench run --label fixture-6        # 6/6 matched, exit 0
```

- [ ] **Step 3: Research acceptance (manual, spends money)**

```sh
uv run --env-file .env deployer bench run --author anthropic --label llm-entrypoint
uv run deployer bench compare .deployer-runs/*-llm-entrypoint golden
```

Expected: 6/6 matched (the two llm-baseline failures now converge via the
fact; slow-build converges via the build-error repair path). On 6/6:

```sh
uv run deployer bench promote .deployer-runs/*-llm-entrypoint
git add corpus/golden
git commit -m "feat: promote LLM-authored golden (6/6 after entrypoint fact)"
```

If NOT 6/6 — do not promote; record per-case diagnosis in the ledger and
report to the user (a partial result is honest research data, not a
failure of this PR; the PR can still merge with the fixture golden).

- [ ] **Step 4: Record everything in `.superpowers/sdd/progress.md`**
