# CI-Failure Reproduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `deployer diagnose <run> --reproduce` restores the failed run's tree at `head_sha`, runs a closed list of deterministic checks, rebuilds the failed `docker build` step on a confirmed-local container endpoint, and records findings, the build, and one CI-vs-local comparison state — with no causal class anywhere.

**Architecture:** A new package `src/deployer/reproduce/` (the spec's "`reproduce.py`", split by responsibility) of small pure modules — Dockerfile spans, ignore rules, static checks, build-line parser, supported-shape checks, restoration, `--check` reader, comparison — plus two thin I/O modules (endpoint confirmation, build adapter) that reach containers only through `deployer.runtime.container_run`, and one orchestrator (`run.py`). `forge.py` gains snapshot schema 1.3 fields and a binary `api_bytes` path; the CLI gains `--reproduce`. Acceptance is a set of committed bundles replayed offline against fakes.

**Tech Stack:** Python 3.12, `uv`, pydantic 2, PyYAML (already a dependency), stdlib `tarfile`/`hashlib`/`shlex`, pytest, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md` (rev 5 @ `009c4ef`). Read it before any task; section numbers below (§1.2, §7.3 …) refer to it.

## Global Constraints

- Python 3.12+, `uv` only (`uv run …`, `uv add …`); never pip.
- After every change: `uv run ruff format .`, `uv run ruff check . --fix`, `uv run pyrefly check`, `uv run pytest`. Line length 88.
- **No `FailureKind` anywhere in `deployer.reproduce`**, and `CheckResult` is neither imported nor loosened there (§6).
- Every container CLI call goes through `deployer.runtime.container_run`, called as `runtime.container_run(...)` via `from deployer import runtime` so one monkeypatch covers all modules. Every GitHub call goes through `forge.GhRunner`/`SubprocessGh`.
- Nothing taken from a workflow or a log is executed; the build line is parsed, and this repo issues its own argv (§4.1, §4.3).
- `--reproduce` never changes the reading layer's exit code except the two exit-2 cases of §6 (with `--container-host`; a try directory that cannot be created or whose `source.json` names another `head_sha`).
- Snapshot schema string becomes `"1.3"`; the committed 1.2 fixtures stay as they are. Verdict schema: `"1.2"` only when a `reproduction` key is present; without `--reproduce` the document is byte-identical to today's (`"1.1"`).
- Tests are offline: no container, no network, no `gh`. The only live actions in this plan are the read-only snapshot re-fetch and the four local Podman builds of Task 14, both run by hand, both recorded in `PROVENANCE.md`. No new workflow dispatch, no paid authoring or benchmark.
- Repository rules: work on a feature branch off `master` (e.g. `feat/ci-failure-reproduction`), PR at the end; update `TODO.md` in the closing PR.

## Review Focus

1. A Dockerfile with CRLF line endings — line numbers and the FROM-argument check must be the same as for LF (test in Task 4).
2. A workflow step written as a block scalar (`run: |` with one line and a trailing newline) — it is still a single-line build step (test in Task 7).
3. An archive that carries directory entries, including an empty directory git does not track — directories are not "extra paths" (test in Task 8).
4. Podman output with ANSI colour codes and multi-stage `[1/2] STEP …` prefixes — the local instruction still binds (test in Task 12).
5. Running `--reproduce` twice on the same run — the second try is `002`, `source/` is reused, nothing is overwritten (test in Task 13).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/deployer/forge.py` (modify) | snapshot 1.3 fields; `StepInfo`; `api_bytes`; `fetch_archive`, `fetch_tree_listing` |
| `src/deployer/reproduce/__init__.py` | package docstring; re-exports `reproduce_run`, `ReproductionSection`, `TryDirError` |
| `src/deployer/reproduce/model.py` | pydantic result types of §6 with their invariants |
| `src/deployer/reproduce/dockerfile.py` | line-span parser + the closed syntax list of §3.1 |
| `src/deployer/reproduce/ignore.py` | ignore-file selection per side, pattern matching (§3.2) |
| `src/deployer/reproduce/checks.py` | COPY/ADD sources, `--from` refs, exactness conditions (d)(e) |
| `src/deployer/reproduce/buildline.py` | the supported `docker build` line of §4.1 |
| `src/deployer/reproduce/shape.py` | §1.2 checks, checkout SHA, inert list (§1.3 a) |
| `src/deployer/reproduce/restore.py` | extraction (§1.4), listing comparison (§1.3 b,c) |
| `src/deployer/reproduce/endpoint.py` | confirmed-local endpoint (§4.2) |
| `src/deployer/reproduce/build.py` | build adapter, cleanup, local digests (§4.3–4.5) |
| `src/deployer/reproduce/buildcheck.py` | `--check` runner, reader table, parser-vs-builder merge (§2) |
| `src/deployer/reproduce/compare.py` | identity, signature, dimensions, ordered states (§7) |
| `src/deployer/reproduce/run.py` | orchestration, storage layout (§1.5), manifest |
| `src/deployer/diagnose.py` (modify) | `render_verdict(..., reproduction=None)` |
| `src/deployer/cli.py` (modify) | `--reproduce`, runtime/timeout flags on `diagnose`, printing |
| `tests/test_forge.py`, `tests/test_fixture_runs.py` (modify) | 1.3 fields; historic 1.2 fixtures |
| `tests/reproduce/test_*.py` (create, one per module) | unit tests |
| `tests/reproduce/conftest.py` | shared fakes: `FakeContainers`, `BundleGh` |
| `tests/fixtures/reproduction/make_bundle.py` | dev tool: snapshot re-fetch + anonymise, tree vendoring, listing |
| `tests/fixtures/reproduction/<case>/…` | bundles (§8.A) |
| `tests/reproduce/test_acceptance.py` | replays every bundle |

---

### Task 1: Snapshot schema 1.3 in `forge.py`

**Files:**
- Modify: `src/deployer/forge.py` (constants, `FailedJob`, `FailedRun`, `fetch_failed_run`, `_build_job`, `dump_snapshot`/`load_snapshot` docstrings)
- Modify: `tests/test_forge.py:662`, `tests/test_fixture_runs.py:60`
- Test: `tests/test_forge.py`

**Interfaces:**
- Produces: `StepInfo(number: int, name: str, conclusion: str | None)`; `FailedJob.all_steps: list[StepInfo] | None`; `FailedRun.workflow_ref_path: str | None`, `FailedRun.workflow_path: str | None`, `FailedRun.event: str | None`; `SNAPSHOT_SCHEMA_VERSION == "1.3"`; `normalise_workflow_path(raw: str) -> str`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_forge.py`)

```python
from deployer.forge import StepInfo, normalise_workflow_path


def test_snapshot_carries_workflow_path_event_and_all_steps(fake_gh):
    fake_gh.run = {
        **fake_gh.run,
        "path": ".github/workflows/build.yml@main",
        "event": "workflow_dispatch",
    }
    fake_gh.job_pages = [
        [job(1, steps=[step(1, "Set up job", "success"), step(2, "Build")])]
    ]
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.workflow_ref_path == ".github/workflows/build.yml@main"
    assert snapshot.workflow_path == ".github/workflows/build.yml"
    assert snapshot.event == "workflow_dispatch"
    assert snapshot.jobs[0].all_steps == [
        StepInfo(1, "Set up job", "success"),
        StepInfo(2, "Build", "failure"),
    ]
    # the kept failed steps are unchanged: only the non-green one
    assert [s.name for s in snapshot.jobs[0].steps] == ["Build"]


def test_run_without_path_or_event_records_none(fake_gh):
    snapshot = fetch_failed_run(RunRef("o/r", 1), attempt=1, runner=fake_gh)
    assert isinstance(snapshot, FailedRun)
    assert snapshot.workflow_ref_path is None
    assert snapshot.workflow_path is None
    assert snapshot.event is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".github/workflows/a.yml", ".github/workflows/a.yml"),
        (".github/workflows/a.yml@main", ".github/workflows/a.yml"),
        (".github/workflows/a.yml@refs/heads/x@y", ".github/workflows/a.yml@refs/heads/x"),
    ],
)
def test_normalise_workflow_path_strips_the_last_ref_suffix(raw, expected):
    assert normalise_workflow_path(raw) == expected


def test_a_1_2_snapshot_loads_with_the_new_fields_absent():
    old = json.loads(
        (Path(__file__).parent / "fixtures" / "runs" / "authoring.json").read_text()
    )
    snapshot = load_snapshot(json.dumps(old))
    assert snapshot.snapshot_schema_version == "1.2"
    assert snapshot.workflow_path is None and snapshot.event is None
    assert snapshot.jobs[0].all_steps is None
```

Add `from pathlib import Path` to the imports of `tests/test_forge.py` if it is not there.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_forge.py -k "workflow_path or normalise or new_fields" -v`
Expected: FAIL — `ImportError: cannot import name 'StepInfo'`.

- [ ] **Step 3: Implement**

In `src/deployer/forge.py`:

```python
SNAPSHOT_SCHEMA_VERSION = "1.3"
```

After `FailedStep`, add:

```python
@dataclass(frozen=True)
class StepInfo:
    """One step of a job as the jobs listing gives it, green or not.

    Reproduction binds workflow steps to these one-to-one (spec §1.2 #5); the
    reading layer keeps using ``FailedJob.steps``, which holds only the
    non-green ones.
    """

    number: int
    name: str
    conclusion: str | None
```

Add the field at the end of `FailedJob` (after `completeness`):

```python
    all_steps: list[StepInfo] | None = None
```

Add three fields to `FailedRun`, before `snapshot_schema_version`:

```python
    workflow_ref_path: str | None = None
    workflow_path: str | None = None
    event: str | None = None
```

Add the helper after `_run_path`:

```python
def normalise_workflow_path(raw: str) -> str:
    """The run's ``path`` without a trailing ``@<ref>`` (split on the last ``@``).

    GitHub documents values such as ``.github/workflows/build.yml@main``; the
    file in the tree is the part before the ref. Validation of the result is
    the reproduction layer's job (spec §1.1), not the snapshot's.
    """
    head, sep, _ = raw.rpartition("@")
    return head if sep else raw
```

In `fetch_failed_run`, compute before the `return`:

```python
    raw_path = run.get("path")
    ref_path = str(raw_path) if raw_path is not None else None
    raw_event = run.get("event")
```

and pass to `FailedRun(...)`:

```python
        workflow_ref_path=ref_path,
        workflow_path=normalise_workflow_path(ref_path) if ref_path else None,
        event=str(raw_event) if raw_event is not None else None,
```

In `_build_job`, build the full list and pass it:

```python
    step_infos = [
        StepInfo(
            number=int(s["number"]),
            name=str(s.get("name", "")),
            conclusion=None if s.get("conclusion") is None else str(s["conclusion"]),
        )
        for s in all_steps
    ]
```

and add `all_steps=step_infos,` to the `FailedJob(...)` call.

Update the docstring of `dump_snapshot`: "Schema 1.3 adds `workflow_ref_path`, `workflow_path`, `event` on the run and `all_steps` on each job; additive like 1.1 and 1.2, so older documents still load with them `None`."

- [ ] **Step 4: Keep the historic fixtures honest**

`tests/test_fixture_runs.py:60` asserts the committed fixtures carry the *current* schema version; they are 1.2 documents and must stay so. Replace that assertion with:

```python
    # The committed fixtures are 1.2 documents: historic, loaded as such.
    assert snapshot.snapshot_schema_version == "1.2"
```

and drop `SNAPSHOT_SCHEMA_VERSION` from that file's import if it becomes unused. In `tests/test_forge.py:662` change `"1.2"` to `"1.3"`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_forge.py tests/test_fixture_runs.py -v`
Expected: all PASS.

- [ ] **Step 6: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/forge.py tests/test_forge.py tests/test_fixture_runs.py
git commit -m "feat(forge): snapshot schema 1.3 — workflow path, event, all steps"
```

---

### Task 2: Binary archive and tree listing in `forge.py`

**Files:**
- Modify: `src/deployer/forge.py`
- Test: `tests/test_forge.py`

**Interfaces:**
- Consumes: `GhError`, `SubprocessGh`, `GH_TIMEOUT_S` (existing).
- Produces:
  - `class GhBytesRunner(GhRunner, Protocol)` with `api_bytes(argv: list[str], *, timeout: float) -> bytes`
  - `SubprocessGh.api_bytes(...) -> bytes`
  - `@dataclass(frozen=True) TreeEntry(path: str, mode: str, type: str, sha: str)`
  - `@dataclass(frozen=True) TreeListing(sha: str, entries: list[TreeEntry], truncated: bool)`
  - `fetch_tree_listing(repo: str, sha: str, runner: GhRunner) -> TreeListing`
  - `fetch_archive(repo: str, sha: str, runner: GhBytesRunner, *, max_bytes: int) -> bytes` (raises `GhError` with `status=None` and message `archive exceeds …` when over the cap)
  - `ARCHIVE_TIMEOUT_S = 120.0`, `DEFAULT_MAX_ARCHIVE_MB = 200`

- [ ] **Step 1: Write the failing tests**

```python
from deployer.forge import (
    TreeEntry,
    TreeListing,
    fetch_archive,
    fetch_tree_listing,
)


class BytesGh:
    """A GhBytesRunner fake: serves one tarball and one listing."""

    def __init__(self, blob: bytes, listing: dict[str, Any]) -> None:
        self.blob = blob
        self.listing = listing
        self.calls: list[list[str]] = []

    def api(self, argv: list[str], *, timeout: float) -> str:
        self.calls.append(list(argv))
        return json.dumps(self.listing)

    def api_bytes(self, argv: list[str], *, timeout: float) -> bytes:
        self.calls.append(list(argv))
        return self.blob


def test_fetch_archive_passes_bytes_unaltered():
    blob = bytes(range(256)) * 4  # not valid UTF-8 anywhere
    gh = BytesGh(blob, {})
    assert fetch_archive("o/r", "abc", gh, max_bytes=10_000) == blob
    assert gh.calls == [["repos/o/r/tarball/abc"]]


def test_fetch_archive_refuses_over_the_cap():
    gh = BytesGh(b"x" * 11, {})
    with pytest.raises(GhError, match="archive exceeds 10 bytes") as info:
        fetch_archive("o/r", "abc", gh, max_bytes=10)
    assert info.value.status is None


def test_fetch_tree_listing_keeps_path_mode_type_sha_and_truncation():
    listing = {
        "sha": "abc",
        "truncated": False,
        "tree": [
            {"path": "src", "mode": "040000", "type": "tree", "sha": "t1"},
            {"path": "src/a.py", "mode": "100644", "type": "blob", "sha": "b1"},
            {"path": "run.sh", "mode": "100755", "type": "blob", "sha": "b2"},
        ],
    }
    gh = BytesGh(b"", listing)
    out = fetch_tree_listing("o/r", "abc", gh)
    assert out == TreeListing(
        sha="abc",
        truncated=False,
        entries=[
            TreeEntry("src", "040000", "tree", "t1"),
            TreeEntry("src/a.py", "100644", "blob", "b1"),
            TreeEntry("run.sh", "100755", "blob", "b2"),
        ],
    )
    assert gh.calls == [["repos/o/r/git/trees/abc?recursive=1"]]


def test_subprocess_api_bytes_runs_gh_without_text_mode(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x00\xff", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert SubprocessGh().api_bytes(["repos/o/r/tarball/x"], timeout=5) == b"\x00\xff"
    assert seen["cmd"] == ["gh", "api", "repos/o/r/tarball/x"]
    assert "text" not in seen["kwargs"] and "errors" not in seen["kwargs"]


def test_subprocess_api_bytes_maps_http_status(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout=b"", stderr=b"gh: Not Found (HTTP 404)"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GhError) as info:
        SubprocessGh().api_bytes(["repos/o/r/tarball/x"], timeout=5)
    assert info.value.status == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_forge.py -k "archive or listing or api_bytes" -v`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement** (in `forge.py`, after `SubprocessGh.api`)

Refactor the error mapping of `SubprocessGh.api` into a helper both methods share:

```python
def _gh_failure(what: str, returncode: int, stderr: str) -> GhError:
    stderr = stderr.strip()
    match = _HTTP_STATUS_RE.search(stderr)
    status = int(match.group(1)) if match else None
    return GhError(f"gh api {what} failed: {stderr or f'exit code {returncode}'}", status)


def _gh_env() -> dict[str, str]:
    return {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
```

`SubprocessGh.api` keeps its behaviour, using `_gh_env()` and `raise _gh_failure(what, proc.returncode, proc.stderr or "")`. Add:

```python
    def api_bytes(self, argv: list[str], *, timeout: float) -> bytes:
        """``gh api *argv`` returning raw stdout bytes (archives, not text).

        ``gh``'s HTTP client follows the tarball endpoint's redirect. Same
        timeout and status mapping as :meth:`api`.
        """
        cmd = ["gh", "api", *argv]
        what = " ".join(argv)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
                env=_gh_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh api {what} timed out after {timeout}s") from exc
        except OSError as exc:
            raise GhError(f"gh api could not start: {exc}") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise _gh_failure(what, proc.returncode, stderr)
        return proc.stdout
```

After the `GhRunner` protocol:

```python
class GhBytesRunner(GhRunner, Protocol):
    """A runner that can also return raw bytes (the tarball endpoint)."""

    def api_bytes(self, argv: list[str], *, timeout: float) -> bytes:
        """Return stdout bytes; raise :class:`GhError` on failure."""
        ...
```

New constants near `GH_TIMEOUT_S`:

```python
ARCHIVE_TIMEOUT_S = 120.0
"""Wall-clock budget for downloading one source archive."""

DEFAULT_MAX_ARCHIVE_MB = 200
```

Dataclasses and functions (after `FailedRun`/`AdapterRefusal` definitions and after `fetch_failed_run` respectively):

```python
@dataclass(frozen=True)
class TreeEntry:
    """One entry of a recursive Git tree listing, as GitHub returns it."""

    path: str
    mode: str
    type: str
    sha: str


@dataclass(frozen=True)
class TreeListing:
    """The Git tree at a commit; ``truncated`` is GitHub's own flag."""

    sha: str
    entries: list[TreeEntry]
    truncated: bool


def fetch_tree_listing(repo: str, sha: str, runner: GhRunner) -> TreeListing:
    """The recursive Git tree at ``sha`` (spec §1.3 b, c)."""
    body = json.loads(
        runner.api([f"repos/{repo}/git/trees/{sha}?recursive=1"], timeout=GH_TIMEOUT_S)
    )
    entries = [
        TreeEntry(str(e["path"]), str(e["mode"]), str(e["type"]), str(e["sha"]))
        for e in body.get("tree", [])
    ]
    return TreeListing(str(body.get("sha", sha)), entries, bool(body.get("truncated")))


def fetch_archive(
    repo: str, sha: str, runner: GhBytesRunner, *, max_bytes: int
) -> bytes:
    """The source tarball of ``sha``; bytes pass through unaltered (spec §1.4).

    Over ``max_bytes`` is refused as a status-less :class:`GhError`: the
    download happened, but this layer will not unpack it.
    """
    blob = runner.api_bytes([f"repos/{repo}/tarball/{sha}"], timeout=ARCHIVE_TIMEOUT_S)
    if len(blob) > max_bytes:
        raise GhError(f"archive exceeds {max_bytes} bytes ({len(blob)})", None)
    return blob
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_forge.py -v`
Expected: all PASS (existing `SubprocessGh.api` tests still pass after the refactor).

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/forge.py tests/test_forge.py
git commit -m "feat(forge): binary archive path and Git tree listing through the gh chokepoint"
```

---

### Task 3: Result model of §6

**Files:**
- Create: `src/deployer/reproduce/__init__.py`, `src/deployer/reproduce/model.py`
- Create: `tests/reproduce/__init__.py` (empty), `tests/reproduce/test_model.py`

**Interfaces:**
- Produces (all in `deployer.reproduce.model`):
  - `CheckStatus = Literal["passed", "failed", "skipped", "observation", "inconclusive"]`
  - `ReproEvidence(kind, path=None, listing=None, text=None)`; `kind ∈ {"path_absent","ignore_file","log_excerpt","output_file","tree_listing"}`
  - `Location(file: str, lines: tuple[int, int])`
  - `ReproductionCheck(check_id, status, finding=None, reason=None, location=None, evidence=[])`
  - `Restoration(state, sha, unmet=[])`, `Binding(job_id, workflow_job, build_step, dockerfile, context=".")`
  - `Environment(backend, backend_version, endpoint, endpoint_source, buildx_version, host_arch, syntax_directive)`
  - `InstructionRef(kind: Literal["span","parse"], lines: tuple[int,int], bound_by: BoundBy)`
  - `BuildResult(argv, exit_code, launch_error, failed_instruction, signature, stdout, stderr, image_cleanup, build_containers)`
  - `Dimension = Literal["same","differs","unknown"]`; `ComparisonState`; `SignatureMatch`
  - `Comparison(state, reason, ci_instruction, signature_match, dimensions: dict[str, Dimension], values: dict[str, tuple[str | None, str | None]])`
  - `ReproductionSection(status, try_dir=None, refusal=None, restoration=None, binding=None, environment=None, checks=[], build=None, comparison=None)`

- [ ] **Step 1: Write the failing tests** (`tests/reproduce/test_model.py`)

```python
"""The §6 result model enforces its own invariants."""

import pytest
from pydantic import ValidationError

from deployer.reproduce.model import (
    BuildResult,
    Comparison,
    ReproductionCheck,
    ReproductionSection,
    ReproEvidence,
)


def test_failed_check_needs_finding_and_evidence():
    with pytest.raises(ValidationError, match="finding"):
        ReproductionCheck(check_id="x", status="failed")
    with pytest.raises(ValidationError, match="evidence"):
        ReproductionCheck(check_id="x", status="failed", finding="f")
    ReproductionCheck(
        check_id="x",
        status="failed",
        finding="f",
        evidence=[ReproEvidence(kind="log_excerpt", text="t")],
    )


@pytest.mark.parametrize("status", ["skipped", "inconclusive"])
def test_skipped_and_inconclusive_need_a_reason(status):
    with pytest.raises(ValidationError, match="reason"):
        ReproductionCheck(check_id="x", status=status)
    ReproductionCheck(check_id="x", status=status, reason="why")


def _build(**overrides):
    base = dict(
        argv=["podman", "build"],
        exit_code=1,
        launch_error=None,
        failed_instruction=None,
        signature=None,
        stdout="build.stdout",
        stderr="build.stderr",
        image_cleanup="not_attempted",
        build_containers="removed_by_builder",
    )
    return BuildResult(**{**base, **overrides})


def test_exit_code_is_null_iff_launch_error():
    with pytest.raises(ValidationError):
        _build(exit_code=None)
    with pytest.raises(ValidationError):
        _build(exit_code=1, launch_error="timeout")
    _build(exit_code=None, launch_error="timeout")


def test_comparison_reason_set_iff_not_attempted_or_inconclusive():
    with pytest.raises(ValidationError):
        Comparison(state="inconclusive", reason=None)
    with pytest.raises(ValidationError):
        Comparison(state="not_reproduced", reason="x")
    Comparison(state="inconclusive", reason="build did not finish")


def test_refused_section_has_refusal_and_no_build_or_comparison():
    with pytest.raises(ValidationError):
        ReproductionSection(status="refused")
    with pytest.raises(ValidationError):
        ReproductionSection(status="refused", refusal="r", build=_build())
    ReproductionSection(status="refused", refusal="r")


def test_attempted_section_has_build_and_comparison_and_no_refusal():
    with pytest.raises(ValidationError):
        ReproductionSection(status="attempted")
    ReproductionSection(
        status="attempted",
        build=_build(),
        comparison=Comparison(state="not_reproduced"),
    )


def test_no_field_anywhere_is_named_failure_kind():
    for model in (ReproductionCheck, BuildResult, Comparison, ReproductionSection):
        assert "failure_kind" not in model.model_fields
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/reproduce/test_model.py -v`
Expected: FAIL — `ModuleNotFoundError: deployer.reproduce`.

- [ ] **Step 3: Implement**

`src/deployer/reproduce/__init__.py`:

```python
"""CI-failure reproduction (spec 2026-09-22 rev 5): findings, not causes.

Restores a failed run's tree at ``head_sha``, runs a closed list of
deterministic checks, rebuilds the failed build step on a confirmed-local
endpoint and compares it with CI. Nothing here carries a ``FailureKind``.
"""
```

(Task 13 adds the re-exports.)

`src/deployer/reproduce/model.py`:

```python
"""Result types of the ``reproduction`` section (spec §6). No causal class."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

CheckStatus = Literal["passed", "failed", "skipped", "observation", "inconclusive"]
EvidenceKind = Literal[
    "path_absent", "ignore_file", "log_excerpt", "output_file", "tree_listing"
]
BoundBy = Literal[
    "buildkit_error_block",
    "buildkit_parse_error",
    "step_text",
    "parser_finding_keyword",
]
Dimension = Literal["same", "differs", "unknown"]
ComparisonState = Literal[
    "not_attempted",
    "inconclusive",
    "not_reproduced",
    "different_failure",
    "same_instruction_different_output",
    "reproduced",
    "reproduced_with_differences",
]
SignatureMatch = Literal["equal", "unequal", "unavailable", "not_compared"]
SectionStatus = Literal["attempted", "refused", "unavailable", "not_requested"]


class ReproEvidence(BaseModel):
    """Typed evidence: an absence is an assertion checked against a listing."""

    kind: EvidenceKind
    path: str | None = None
    listing: str | None = None
    text: str | None = None


class Location(BaseModel):
    """A Dockerfile span, context-relative ``file``, 1-based inclusive lines."""

    file: str
    lines: tuple[int, int]


class ReproductionCheck(BaseModel):
    """One check result; its own type because ``CheckResult`` demands a class."""

    check_id: str
    status: CheckStatus
    finding: str | None = None
    reason: str | None = None
    location: Location | None = None
    evidence: list[ReproEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.status == "failed":
            if not self.finding:
                raise ValueError("a failed check needs a finding")
            if not self.evidence:
                raise ValueError("a failed check needs at least one evidence entry")
        if self.status in ("skipped", "inconclusive") and not self.reason:
            raise ValueError(f"a {self.status} check needs a reason")
        return self


class Restoration(BaseModel):
    """How exact the restored tree is (§1.3)."""

    state: Literal["exact", "approximation", "unavailable"]
    sha: str
    unmet: list[str] = Field(default_factory=list)


class Binding(BaseModel):
    """The failed job, the build step and what it builds (§1.2)."""

    job_id: int
    workflow_job: str
    build_step: int
    dockerfile: str
    context: str = "."


class Environment(BaseModel):
    """The local side as detected (§2, §4.2)."""

    backend: Literal["docker", "podman"]
    backend_version: str | None
    endpoint: str
    endpoint_source: str
    buildx_version: str | None
    host_arch: str
    syntax_directive: str | None


class InstructionRef(BaseModel):
    """An instruction identity: a source line span, or a parse failure line."""

    kind: Literal["span", "parse"]
    lines: tuple[int, int]
    bound_by: BoundBy


class BuildResult(BaseModel):
    """The adapter's build (§4.3) and its cleanup (§4.4)."""

    argv: list[str]
    exit_code: int | None
    launch_error: str | None
    failed_instruction: InstructionRef | None
    signature: str | None
    stdout: str
    stderr: str
    image_cleanup: Literal["removed", "failed", "not_attempted"]
    build_containers: Literal["removed_by_builder", "not_checked", "not_applicable"]

    @model_validator(mode="after")
    def _exit_iff_no_launch_error(self) -> Self:
        if (self.exit_code is None) != (self.launch_error is not None):
            raise ValueError("exit_code is null iff launch_error is set")
        return self


class Comparison(BaseModel):
    """One CI-vs-local state from the ordered list of §7.3."""

    state: ComparisonState
    reason: str | None = None
    ci_instruction: InstructionRef | None = None
    signature_match: SignatureMatch | None = None
    dimensions: dict[str, Dimension] = Field(default_factory=dict)
    values: dict[str, tuple[str | None, str | None]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reason_iff_unfinished(self) -> Self:
        needs = self.state in ("not_attempted", "inconclusive")
        if needs != (self.reason is not None):
            raise ValueError("reason is set iff state is not_attempted/inconclusive")
        return self


class ReproductionSection(BaseModel):
    """The ``reproduction`` section of the verdict and of each try's manifest."""

    status: SectionStatus
    try_dir: str | None = None
    refusal: str | None = None
    restoration: Restoration | None = None
    binding: Binding | None = None
    environment: Environment | None = None
    checks: list[ReproductionCheck] = Field(default_factory=list)
    build: BuildResult | None = None
    comparison: Comparison | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        if self.status in ("refused", "unavailable"):
            if not self.refusal:
                raise ValueError(f"a {self.status} section needs a refusal")
            if self.build is not None or self.comparison is not None:
                raise ValueError(f"a {self.status} section has no build/comparison")
        if self.status == "attempted":
            if self.refusal is not None:
                raise ValueError("an attempted section has no refusal")
            if self.build is None or self.comparison is None:
                raise ValueError("an attempted section has a build and a comparison")
        return self
```

- [ ] **Step 4: Run the tests** — `uv run pytest tests/reproduce/test_model.py -v` → PASS.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce tests/reproduce
git commit -m "feat(reproduce): the §6 result model with its invariants, no causal class"
```

---

### Task 4: Dockerfile spans and the closed syntax list (§3.1)

**Files:**
- Create: `src/deployer/reproduce/dockerfile.py`
- Test: `tests/reproduce/test_dockerfile.py`

**Interfaces:**
- Consumes: `ReproductionCheck`, `ReproEvidence`, `Location` (Task 3).
- Produces:
  - `KEYWORDS: frozenset[str]`
  - `Instruction(keyword: str, args: str, first_line: int, last_line: int)` with property `text -> str` (`"KEYWORD args"`, whitespace-normalised)
  - `ParsedDockerfile(instructions: list[Instruction], syntax_directive: str | None, escape_directive: str | None, dangling_continuation: bool)`
  - `parse(text: str) -> ParsedDockerfile`
  - `normalise(text: str) -> str` (collapse whitespace, drop `\`+newline)
  - `syntax_checks(parsed: ParsedDockerfile, dockerfile: str) -> list[ReproductionCheck]` — check ids `syntax_first_from`, `syntax_from_args`, `syntax_keyword`, `syntax_continuation`; status `observation` instead of `failed` when `syntax_directive` is set; all four `skipped` when `escape_directive` is set and not `\`.

- [ ] **Step 1: Write the failing tests**

```python
"""§3.1: line spans and exactly four syntax checks."""

from deployer.reproduce.dockerfile import normalise, parse, syntax_checks

RUN5 = "FROM python:3.12-slim extra\n\nRUN true\n"


def _failed(checks):
    return [(c.check_id, c.location.lines if c.location else None)
            for c in checks if c.status == "failed"]


def test_spans_follow_continuations_and_skip_comments():
    text = "# c\nFROM a\n\nRUN one \\\n  # inner comment\n  two\nCMD x\n"
    parsed = parse(text)
    assert [(i.keyword, i.first_line, i.last_line) for i in parsed.instructions] == [
        ("FROM", 2, 2), ("RUN", 4, 6), ("CMD", 7, 7)
    ]
    assert parsed.instructions[1].text == "RUN one two"


def test_crlf_gives_the_same_lines_as_lf():  # Review Focus 1
    assert parse(RUN5.replace("\n", "\r\n")) == parse(RUN5)
    assert _failed(syntax_checks(parse(RUN5.replace("\n", "\r\n")), "Dockerfile")) == [
        ("syntax_from_args", (1, 1))
    ]


def test_run5_from_with_extra_argument_is_the_only_finding():
    checks = syntax_checks(parse(RUN5), "Dockerfile")
    assert _failed(checks) == [("syntax_from_args", (1, 1))]
    finding = next(c for c in checks if c.status == "failed")
    assert finding.finding == "syntax error at line 1: FROM takes one or three arguments"


def test_from_forms_that_are_valid():
    for line in ("FROM a", "FROM a AS b", "FROM a as b", "FROM --platform=$P a AS b"):
        assert _failed(syntax_checks(parse(line + "\n"), "Dockerfile")) == []


def test_first_instruction_must_be_from_after_args():
    assert _failed(syntax_checks(parse("ARG V=1\nFROM a\n"), "D")) == []
    assert _failed(syntax_checks(parse("RUN x\nFROM a\n"), "D")) == [
        ("syntax_first_from", (1, 1))
    ]


def test_unknown_keyword_and_dangling_continuation():
    checks = syntax_checks(parse("FROM a\nRUNN x\nRUN y \\\n"), "D")
    assert sorted(_failed(checks)) == [
        ("syntax_continuation", (3, 3)),
        ("syntax_keyword", (2, 2)),
    ]


def test_parser_directives_and_heredoc_body():
    text = "# syntax=docker/dockerfile:1\nFROM a\nRUN <<EOF\nnot a keyword\nEOF\nCMD x\n"
    parsed = parse(text)
    assert parsed.syntax_directive == "docker/dockerfile:1"
    assert [(i.keyword, i.first_line, i.last_line) for i in parsed.instructions] == [
        ("FROM", 2, 2), ("RUN", 3, 5), ("CMD", 6, 6)
    ]


def test_external_frontend_turns_findings_into_observations():
    checks = syntax_checks(parse("# syntax=x/y\n" + RUN5), "D")
    assert [c.status for c in checks if c.check_id == "syntax_from_args"] == [
        "observation"
    ]


def test_non_backslash_escape_skips_all_four():
    checks = syntax_checks(parse("# escape=`\nFROM a\n"), "D")
    assert {c.status for c in checks} == {"skipped"}
    assert len(checks) == 4


def test_empty_dockerfile_fails_first_from():
    assert _failed(syntax_checks(parse(""), "D")) == [("syntax_first_from", (1, 1))]


def test_normalise():
    assert normalise("RUN  a \\\n   b") == "RUN a b"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/reproduce/test_dockerfile.py -v` → FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement** (`src/deployer/reproduce/dockerfile.py`)

```python
"""A Dockerfile reader with line spans and a closed list of syntax checks (§3.1).

Not a frontend: it checks exactly four things and says so. "No finding" means
"no finding among checks 1-4", never "valid syntax".
"""

import re
from dataclasses import dataclass

from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence

KEYWORDS = frozenset(
    {
        "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM",
        "HEALTHCHECK", "LABEL", "MAINTAINER", "ONBUILD", "RUN", "SHELL",
        "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
    }
)
_DIRECTIVE_RE = re.compile(r"^#\s*(syntax|escape|check)\s*=\s*(\S+)\s*$", re.IGNORECASE)
_HEREDOC_RE = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
_WS_RE = re.compile(r"\s+")
_CHECK_IDS = (
    "syntax_first_from",
    "syntax_from_args",
    "syntax_keyword",
    "syntax_continuation",
)


@dataclass(frozen=True)
class Instruction:
    """One instruction and the 1-based source lines it spans."""

    keyword: str
    args: str
    first_line: int
    last_line: int

    @property
    def text(self) -> str:
        """``KEYWORD args`` with whitespace collapsed, for matching."""
        return normalise(f"{self.keyword} {self.args}")


@dataclass(frozen=True)
class ParsedDockerfile:
    """Instructions with spans, the directives read, and a dangling ``\\``."""

    instructions: list[Instruction]
    syntax_directive: str | None
    escape_directive: str | None
    dangling_continuation: bool


def normalise(text: str) -> str:
    """Drop backslash-newline continuations and collapse whitespace."""
    return _WS_RE.sub(" ", text.replace("\\\r\n", " ").replace("\\\n", " ")).strip()


def parse(text: str) -> ParsedDockerfile:
    """Split into instructions with spans; CRLF reads exactly like LF."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    directives: dict[str, str] = {}
    instructions: list[Instruction] = []
    in_directives = True
    buffer: list[str] = []
    first = 0
    heredoc: tuple[str, bool] | None = None
    heredoc_owner: Instruction | None = None
    last_content = 0
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if heredoc is not None and heredoc_owner is not None:
            delimiter, dash = heredoc
            body = raw.lstrip("\t") if dash else raw
            heredoc_owner = Instruction(
                heredoc_owner.keyword,
                heredoc_owner.args,
                heredoc_owner.first_line,
                number,
            )
            instructions[-1] = heredoc_owner
            if body == delimiter:
                heredoc, heredoc_owner = None, None
            continue
        if in_directives:
            match = _DIRECTIVE_RE.match(stripped)
            if match and not buffer:
                directives.setdefault(match.group(1).lower(), match.group(2))
                continue
            in_directives = False
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if buffer and (not stripped or stripped.startswith("#")):
            continue  # blank line or comment inside a continuation
        if not buffer:
            first = number
        last_content = number
        if stripped.endswith("\\"):
            buffer.append(stripped[:-1])
            continue
        buffer.append(stripped)
        logical = " ".join(part.strip() for part in buffer if part.strip())
        buffer = []
        keyword, _, rest = logical.partition(" ")
        instruction = Instruction(keyword.upper(), rest.strip(), first, number)
        instructions.append(instruction)
        heredoc_match = _HEREDOC_RE.search(rest)
        if heredoc_match:
            heredoc = (heredoc_match.group(3), heredoc_match.group(1) == "-")
            heredoc_owner = instruction
    dangling = bool(buffer)
    if buffer:
        logical = " ".join(part.strip() for part in buffer if part.strip())
        keyword, _, rest = logical.partition(" ")
        instructions.append(
            Instruction(keyword.upper(), rest.strip(), first, last_content)
        )
    return ParsedDockerfile(
        instructions=instructions,
        syntax_directive=directives.get("syntax"),
        escape_directive=directives.get("escape"),
        dangling_continuation=dangling,
    )


def syntax_checks(parsed: ParsedDockerfile, dockerfile: str) -> list[ReproductionCheck]:
    """The four checks of §3.1; one passed check per clean rule."""
    if parsed.escape_directive not in (None, "\\"):
        return [
            ReproductionCheck(
                check_id=check_id,
                status="skipped",
                reason=f"escape directive not modelled: {parsed.escape_directive}",
            )
            for check_id in _CHECK_IDS
        ]
    findings: dict[str, list[tuple[tuple[int, int], str]]] = {c: [] for c in _CHECK_IDS}
    instructions = parsed.instructions
    head = next((i for i in instructions if i.keyword != "ARG"), None)
    if head is None or head.keyword != "FROM":
        span = (head.first_line, head.last_line) if head else (1, 1)
        findings["syntax_first_from"].append((span, "the first instruction is not FROM"))
    for inst in instructions:
        if inst.keyword == "FROM" and not _from_args_ok(inst.args):
            findings["syntax_from_args"].append(
                ((inst.first_line, inst.last_line), "FROM takes one or three arguments")
            )
        if inst.keyword not in KEYWORDS:
            findings["syntax_keyword"].append(
                ((inst.first_line, inst.last_line), f"unknown instruction {inst.keyword}")
            )
    if parsed.dangling_continuation and instructions:
        last = instructions[-1]
        findings["syntax_continuation"].append(
            ((last.last_line, last.last_line), "line continuation ends the file")
        )
    status = "observation" if parsed.syntax_directive else "failed"
    checks: list[ReproductionCheck] = []
    for check_id in _CHECK_IDS:
        if not findings[check_id]:
            checks.append(ReproductionCheck(check_id=check_id, status="passed"))
            continue
        for span, message in findings[check_id]:
            checks.append(
                ReproductionCheck(
                    check_id=check_id,
                    status=status,
                    finding=f"syntax error at line {span[0]}: {message}",
                    location=Location(file=dockerfile, lines=span),
                    evidence=[ReproEvidence(kind="log_excerpt", text="parser")],
                )
            )
    return checks


def _from_args_ok(args: str) -> bool:
    tokens = args.split()
    if tokens and tokens[0].startswith("--platform="):
        tokens = tokens[1:]
    return len(tokens) == 1 or (len(tokens) == 3 and tokens[1].upper() == "AS")
```

Note for the implementer: the empty-file case has no instructions, so `dangling_continuation` is irrelevant and `syntax_first_from` fails at `(1, 1)`.

- [ ] **Step 4: Run the tests** — `uv run pytest tests/reproduce/test_dockerfile.py -v` → PASS. If the heredoc test fails because the heredoc line itself matched, check that the owner update runs before the delimiter comparison.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/dockerfile.py tests/reproduce/test_dockerfile.py
git commit -m "feat(reproduce): Dockerfile line spans and the closed syntax list of §3.1"
```

---

### Task 5: Ignore rules, COPY/ADD sources, `--from`, exactness (d)(e)

**Files:**
- Create: `src/deployer/reproduce/ignore.py`, `src/deployer/reproduce/checks.py`
- Test: `tests/reproduce/test_ignore.py`, `tests/reproduce/test_checks.py`

**Interfaces:**
- Consumes: `ParsedDockerfile`, `Instruction` (Task 4); model types (Task 3).
- Produces:
  - `ignore.IgnoreRules(file: str | None, patterns: list[tuple[int, str, bool]], unsupported: str | None)`
  - `ignore.ci_ignore_file(context: Path, dockerfile: str) -> str | None`
  - `ignore.local_ignore_file(context: Path, dockerfile: str, backend: str) -> str | None`
  - `ignore.load_rules(context: Path, file: str | None) -> IgnoreRules`
  - `ignore.excluded_by(rules: IgnoreRules, path: str) -> int | None` (line of the rule that excludes it)
  - `ignore.glob_to_regex(pattern: str) -> re.Pattern[str]`
  - `checks.LISTING_REF = "../../source.json#tree"`
  - `checks.copy_source_checks(parsed, context: Path, dockerfile: str, rules: IgnoreRules) -> list[ReproductionCheck]`
  - `checks.from_ref_checks(parsed) -> list[ReproductionCheck]`
  - `checks.context_conditions(parsed, rules: IgnoreRules) -> list[str]`
  - `checks.external_images(parsed) -> list[str]` (FROM images and `--from=<image>` that are not stage names)

- [ ] **Step 1: Write the failing tests**

`tests/reproduce/test_ignore.py`:

```python
"""§3.2 ignore-file selection per side and Docker's matching subset."""

from deployer.reproduce.ignore import (
    ci_ignore_file,
    excluded_by,
    load_rules,
    local_ignore_file,
)


def test_ci_side_prefers_the_dockerfile_specific_file(tmp_path):
    (tmp_path / ".dockerignore").write_text("a\n")
    assert ci_ignore_file(tmp_path, "Dockerfile") == ".dockerignore"
    (tmp_path / "Dockerfile.dockerignore").write_text("b\n")
    assert ci_ignore_file(tmp_path, "Dockerfile") == "Dockerfile.dockerignore"


def test_podman_prefers_containerignore(tmp_path):
    (tmp_path / ".dockerignore").write_text("a\n")
    (tmp_path / ".containerignore").write_text("")
    assert local_ignore_file(tmp_path, "Dockerfile", "podman") == ".containerignore"
    assert local_ignore_file(tmp_path, "Dockerfile", "docker") == ".dockerignore"


def test_no_ignore_file(tmp_path):
    assert ci_ignore_file(tmp_path, "Dockerfile") is None
    assert load_rules(tmp_path, None).patterns == []


def test_matching_last_match_wins_and_parents_exclude_children(tmp_path):
    (tmp_path / ".dockerignore").write_text("# c\ntests\n*.md\n!README.md\n**/*.pyc\n")
    rules = load_rules(tmp_path, ".dockerignore")
    assert excluded_by(rules, "tests/test_a.py") == 2
    assert excluded_by(rules, "notes.md") == 3
    assert excluded_by(rules, "README.md") is None
    assert excluded_by(rules, "docs/setup.md") is None  # `*` does not cross `/`
    assert excluded_by(rules, "src/a/b.pyc") == 5


def test_unsupported_pattern_is_named(tmp_path):
    (tmp_path / ".dockerignore").write_text("file[0-9]\n")
    assert load_rules(tmp_path, ".dockerignore").unsupported == "file[0-9]"
```

`tests/reproduce/test_checks.py`:

```python
"""§3.2-3.3 source checks and the exactness conditions (d) and (e)."""

from deployer.reproduce.checks import (
    context_conditions,
    copy_source_checks,
    external_images,
    from_ref_checks,
)
from deployer.reproduce.dockerfile import parse
from deployer.reproduce.ignore import load_rules

RUN1 = (
    "FROM python:3.12-slim\n\n"
    "COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/\n\n"
    "WORKDIR /app\n\n"
    "COPY pyproject.toml uv.lock ./\n"
    "RUN uv sync --frozen --no-install-project\n\n"
    "COPY src/ci_build ./src/ci_build\n"
    "COPY docs/setup.md ./setup.md\n"
)


def _tree(tmp_path):
    for rel in ("pyproject.toml", "uv.lock", "src/ci_build/__init__.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return tmp_path


def test_run1_missing_copy_source_is_the_finding(tmp_path):
    ctx = _tree(tmp_path)
    checks = copy_source_checks(parse(RUN1), ctx, "Dockerfile", load_rules(ctx, None))
    failed = [c for c in checks if c.status == "failed"]
    assert [(c.finding, c.location.lines) for c in failed] == [
        ("source docs/setup.md absent from the context", (11, 11))
    ]
    kinds = [e.kind for e in failed[0].evidence]
    assert kinds == ["path_absent", "ignore_file"]
    assert failed[0].evidence[0].listing == "../../source.json#tree"


def test_excluded_source_names_file_and_line(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / "docs").mkdir()
    (ctx / "docs/setup.md").write_text("x")
    (ctx / ".dockerignore").write_text("docs\n")
    checks = copy_source_checks(
        parse(RUN1), ctx, "Dockerfile", load_rules(ctx, ".dockerignore")
    )
    assert [c.finding for c in checks if c.status == "failed"] == [
        "source docs/setup.md excluded by .dockerignore line 1"
    ]


def test_clean_tree_passes_and_from_sources_are_skipped(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / "docs").mkdir()
    (ctx / "docs/setup.md").write_text("x")
    checks = copy_source_checks(parse(RUN1), ctx, "Dockerfile", load_rules(ctx, None))
    assert [c.status for c in checks if c.status != "skipped"] == ["passed"]
    assert any(c.status == "skipped" and "--from" in (c.reason or "") for c in checks)


def test_empty_glob_and_remote_add(tmp_path):
    ctx = _tree(tmp_path)
    text = "FROM a\nCOPY *.txt /x/\nADD https://example.com/f /f\n"
    checks = copy_source_checks(parse(text), ctx, "Dockerfile", load_rules(ctx, None))
    assert [c.finding for c in checks if c.status == "failed"] == [
        "source *.txt matches nothing in the context"
    ]
    assert any(c.status == "skipped" and "remote" in (c.reason or "") for c in checks)


def test_unmodelled_ignore_pattern_skips_the_check(tmp_path):
    ctx = _tree(tmp_path)
    (ctx / ".dockerignore").write_text("a[bc]\n")
    checks = copy_source_checks(
        parse(RUN1), ctx, "Dockerfile", load_rules(ctx, ".dockerignore")
    )
    assert [(c.check_id, c.status) for c in checks] == [("copy_sources", "skipped")]


def test_from_refs_are_observations():
    text = "FROM python:3.12 AS base\nFROM base\nCOPY --from=base /a /a\n"
    text += "COPY --from=ghcr.io/x/y:1 /b /b\n"
    obs = from_ref_checks(parse(text))
    assert [c.finding for c in obs] == [
        "--from=base resolved to a stage of this Dockerfile",
        "--from=ghcr.io/x/y:1 is an external image dependency",
    ]
    assert external_images(parse(text)) == ["python:3.12", "ghcr.io/x/y:1"]


def test_context_conditions_git_and_mount(tmp_path):
    rules = load_rules(tmp_path, None)
    assert context_conditions(parse("FROM a\nCOPY . /app\n"), rules) == [
        ".git reachable: COPY . at line 2"
    ]
    assert context_conditions(parse("FROM a\nCOPY .git/HEAD /h\n"), rules) == [
        ".git reachable: COPY .git/HEAD at line 2"
    ]
    assert context_conditions(parse("FROM a\nRUN --mount=type=cache,target=/c x\n"), rules) == [
        "RUN --mount at line 2"
    ]
    (tmp_path / ".dockerignore").write_text(".git\n")
    assert context_conditions(
        parse("FROM a\nCOPY . /app\n"), load_rules(tmp_path, ".dockerignore")
    ) == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/reproduce/test_ignore.py tests/reproduce/test_checks.py -v` → FAIL.

- [ ] **Step 3: Implement**

`src/deployer/reproduce/ignore.py`:

```python
"""Ignore-file selection per side and Docker's pattern subset (spec §3.2).

Supported: literal paths, ``*``, ``?``, ``**``, leading ``!``, last match
wins, and a matched directory excludes everything under it. Anything else is
named as unsupported so the check that needs it is skipped, not guessed.
"""

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IgnoreRules:
    """Patterns of one ignore file: ``(line, pattern, negated)``."""

    file: str | None
    patterns: list[tuple[int, str, bool]]
    unsupported: str | None


def ci_ignore_file(context: Path, dockerfile: str) -> str | None:
    """Docker's rule: ``<Dockerfile>.dockerignore`` next to it, else the root one."""
    df = Path(dockerfile)
    specific = df.parent / f"{df.name}.dockerignore"
    if (context / specific).is_file():
        return specific.as_posix()
    if (context / ".dockerignore").is_file():
        return ".dockerignore"
    return None


def local_ignore_file(context: Path, dockerfile: str, backend: str) -> str | None:
    """Podman prefers ``.containerignore``; otherwise as Docker."""
    if backend == "podman" and (context / ".containerignore").is_file():
        return ".containerignore"
    return ci_ignore_file(context, dockerfile)


def load_rules(context: Path, file: str | None) -> IgnoreRules:
    """Read an ignore file; comments and blank lines skipped."""
    if file is None:
        return IgnoreRules(None, [], None)
    patterns: list[tuple[int, str, bool]] = []
    unsupported: str | None = None
    text = (context / file).read_text(errors="replace")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        body = line[1:].strip() if negated else line
        if "[" in body or "\\" in body:
            unsupported = unsupported or body
            continue
        cleaned = posixpath.normpath(body.lstrip("/"))
        patterns.append((number, cleaned, negated))
    return IgnoreRules(file, patterns, unsupported)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` spans segments, ``*`` and ``?`` stay within one."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def excluded_by(rules: IgnoreRules, path: str) -> int | None:
    """The line of the rule that leaves ``path`` excluded, or ``None``."""
    candidates = _self_and_parents(posixpath.normpath(path))
    excluded_line: int | None = None
    for line, pattern, negated in rules.patterns:
        regex = glob_to_regex(pattern)
        if any(regex.match(c) for c in candidates):
            excluded_line = None if negated else line
    return excluded_line


def _self_and_parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: n + 1]) for n in range(len(parts))]
```

`src/deployer/reproduce/checks.py`:

```python
"""Offline source checks over the restored context (spec §3.2, §3.3, §1.3 d-e)."""

import json
import os
import shlex
from pathlib import Path

from deployer.reproduce.dockerfile import Instruction, ParsedDockerfile
from deployer.reproduce.ignore import IgnoreRules, excluded_by, glob_to_regex
from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence

LISTING_REF = "../../source.json#tree"
_REMOTE_PREFIXES = ("http://", "https://", "git@", "git://")
_UNMODELLED_FLAGS = ("--parents", "--exclude")


def copy_source_checks(
    parsed: ParsedDockerfile, context: Path, dockerfile: str, rules: IgnoreRules
) -> list[ReproductionCheck]:
    """Every local COPY/ADD source against ``context`` minus ignored paths."""
    if rules.unsupported is not None:
        return [
            ReproductionCheck(
                check_id="copy_sources",
                status="skipped",
                reason=f"ignore pattern not modelled: {rules.unsupported}",
            )
        ]
    files = _context_paths(context)
    findings: list[ReproductionCheck] = []
    skipped: list[ReproductionCheck] = []
    for inst in parsed.instructions:
        if inst.keyword not in ("COPY", "ADD"):
            continue
        sources, why_skipped = _sources(inst)
        if why_skipped is not None:
            skipped.append(_skip(inst, why_skipped))
            continue
        for raw in sources:
            if inst.keyword == "ADD" and raw.startswith(_REMOTE_PREFIXES):
                skipped.append(_skip(inst, f"remote ADD source {raw}"))
                continue
            findings.extend(
                _check_source(inst, _norm(raw), files, context, dockerfile, rules)
            )
    if not findings:
        findings = [ReproductionCheck(check_id="copy_sources", status="passed")]
    return findings + skipped


def from_ref_checks(parsed: ParsedDockerfile) -> list[ReproductionCheck]:
    """Every ``--from=`` recorded as a stage or an external image (§3.3)."""
    stages = _stage_names(parsed)
    out: list[ReproductionCheck] = []
    for inst in parsed.instructions:
        for ref in _from_flags(inst):
            finding = (
                f"--from={ref} resolved to a stage of this Dockerfile"
                if ref.lower() in stages
                else f"--from={ref} is an external image dependency"
            )
            out.append(
                ReproductionCheck(check_id="from_ref", status="observation", finding=finding)
            )
    return out


def external_images(parsed: ParsedDockerfile) -> list[str]:
    """Images the build pulls: FROM images and ``--from=<image>``, not stages."""
    stages = _stage_names(parsed)
    images: list[str] = []
    for inst in parsed.instructions:
        if inst.keyword == "FROM":
            tokens = [t for t in inst.args.split() if not t.startswith("--")]
            if tokens and tokens[0].lower() not in stages and tokens[0] != "scratch":
                images.append(tokens[0])
        images.extend(r for r in _from_flags(inst) if r.lower() not in stages)
    return list(dict.fromkeys(images))


def context_conditions(parsed: ParsedDockerfile, rules: IgnoreRules) -> list[str]:
    """Exactness conditions (d) and (e) of §1.3, as unmet-condition strings."""
    unmet: list[str] = []
    git_excluded = excluded_by(rules, ".git") is not None
    for inst in parsed.instructions:
        if inst.keyword in ("COPY", "ADD") and not _from_flags(inst):
            sources, _ = _sources(inst)
            for source in (_norm(s) for s in sources):
                root_glob = "/" not in source and any(ch in source for ch in "*?")
                if (source == "." or root_glob or source.startswith(".git")) and (
                    not git_excluded
                ):
                    unmet.append(
                        f".git reachable: {inst.keyword} {source} at line {inst.first_line}"
                    )
        if inst.keyword == "RUN" and any(
            t.startswith("--mount") for t in inst.args.split()
        ):
            unmet.append(f"RUN --mount at line {inst.first_line}")
    return unmet


def _sources(inst: Instruction) -> tuple[list[str], str | None]:
    args = inst.args.strip()
    if "<<" in args:
        return [], "heredoc source not modelled"
    if args.startswith("["):
        try:
            tokens = [str(t) for t in json.loads(args)]
        except json.JSONDecodeError:
            return [], "unparseable JSON form"
    else:
        try:
            tokens = shlex.split(args)
        except ValueError:
            return [], "unparseable quoting"
    flags = [t for t in tokens if t.startswith("--")]
    rest = [t for t in tokens if not t.startswith("--")]
    if any(f.startswith("--from") for f in flags):
        return [], "--from source is checked in its stage or image, not the context"
    if any(f.startswith(_UNMODELLED_FLAGS) for f in flags):
        return [], "flag not modelled: " + ", ".join(flags)
    if len(rest) < 2:
        return [], "no source/destination pair"
    return rest[:-1], None


def _norm(source: str) -> str:
    """Context-relative form of a local source; ``.`` for the root."""
    return os.path.normpath(source).lstrip("/") or "."


def _check_source(
    inst: Instruction,
    source: str,
    files: list[str],
    context: Path,
    dockerfile: str,
    rules: IgnoreRules,
) -> list[ReproductionCheck]:
    location = Location(file=dockerfile, lines=(inst.first_line, inst.last_line))
    ignore_ev = ReproEvidence(kind="ignore_file", path=rules.file)
    if any(ch in source for ch in "*?"):
        regex = glob_to_regex(source)
        matched = [f for f in files if regex.match(f) and excluded_by(rules, f) is None]
        if matched:
            return []
        return [
            ReproductionCheck(
                check_id="copy_sources",
                status="failed",
                finding=f"source {source} matches nothing in the context",
                location=location,
                evidence=[
                    ReproEvidence(kind="path_absent", path=source, listing=LISTING_REF),
                    ignore_ev,
                ],
            )
        ]
    if source != "." and not (context / source).exists():
        return [
            ReproductionCheck(
                check_id="copy_sources",
                status="failed",
                finding=f"source {source} absent from the context",
                location=location,
                evidence=[
                    ReproEvidence(kind="path_absent", path=source, listing=LISTING_REF),
                    ignore_ev,
                ],
            )
        ]
    line = excluded_by(rules, source) if source != "." else None
    if line is None:
        return []
    return [
        ReproductionCheck(
            check_id="copy_sources",
            status="failed",
            finding=f"source {source} excluded by {rules.file} line {line}",
            location=location,
            evidence=[
                ReproEvidence(kind="ignore_file", path=rules.file, text=f"line {line}")
            ],
        )
    ]


def _skip(inst: Instruction, reason: str) -> ReproductionCheck:
    return ReproductionCheck(
        check_id="copy_sources",
        status="skipped",
        reason=f"{inst.keyword} at line {inst.first_line}: {reason}",
    )


def _stage_names(parsed: ParsedDockerfile) -> set[str]:
    names: set[str] = set()
    for inst in parsed.instructions:
        tokens = inst.args.split()
        if inst.keyword == "FROM" and len(tokens) >= 3 and tokens[-2].upper() == "AS":
            names.add(tokens[-1].lower())
    return names


def _from_flags(inst: Instruction) -> list[str]:
    if inst.keyword not in ("COPY", "ADD"):
        return []
    return [t.split("=", 1)[1] for t in inst.args.split() if t.startswith("--from=")]


def _context_paths(context: Path) -> list[str]:
    out: list[str] = []
    for root, dirs, names in os.walk(context):
        rel_root = Path(root).relative_to(context)
        for name in [*dirs, *names]:
            out.append((rel_root / name).as_posix())
    return out
```

- [ ] **Step 4: Run the tests** — `uv run pytest tests/reproduce/test_ignore.py tests/reproduce/test_checks.py -v` → PASS.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/ignore.py src/deployer/reproduce/checks.py tests/reproduce
git commit -m "feat(reproduce): ignore rules per side, COPY/ADD sources, --from refs, exactness (d)(e)"
```

---

### Task 6: The supported build line (§4.1)

**Files:**
- Create: `src/deployer/reproduce/buildline.py`
- Test: `tests/reproduce/test_buildline.py`

**Interfaces:**
- Produces:
  - `BuildConfig(dockerfile: str, build_args: tuple[tuple[str, str], ...], platform: str | None, tag: str | None)`
  - `Unsupported(what: str)`
  - `parse_build_line(line: str) -> BuildConfig | Unsupported | None` — `None` means "not a docker build line at all".

- [ ] **Step 1: Write the failing tests**

```python
"""§4.1: exactly one accepted shape; every other build line is named."""

import pytest

from deployer.reproduce.buildline import BuildConfig, Unsupported, parse_build_line


def test_polygon_build_line():
    assert parse_build_line("docker build --file ./Dockerfile .") == BuildConfig(
        dockerfile="Dockerfile", build_args=(), platform=None, tag=None
    )


def test_all_accepted_flags():
    line = "docker build -f docker/app.Dockerfile --build-arg A=1 --build-arg=B=2 "
    line += "--platform linux/amd64 -t x:1 ."
    assert parse_build_line(line) == BuildConfig(
        dockerfile="docker/app.Dockerfile",
        build_args=(("A", "1"), ("B", "2")),
        platform="linux/amd64",
        tag="x:1",
    )


@pytest.mark.parametrize(
    ("line", "what"),
    [
        ("docker build --file ./Dockerfile . && echo ok", "shell chain"),
        ("docker build . || true", "shell chain"),
        ("docker build .; ls", "shell chain"),
        ("docker build . | tee log", "shell operator |"),
        ("docker build . > log", "shell operator >"),
        ("docker build -t $TAG .", "variable expansion"),
        ("docker build -t $(git rev-parse HEAD) .", "variable expansion"),
        ("docker build app", "context app"),
        ("docker build --secret id=x .", "flag --secret"),
        ("docker build --no-cache .", "flag --no-cache"),
        ("docker build --weird .", "unknown flag --weird"),
        ("docker build -f ../Dockerfile .", "dockerfile path ../Dockerfile"),
        ("docker build -f /abs/Dockerfile .", "dockerfile path /abs/Dockerfile"),
        ("docker buildx build .", "buildx build"),
        ("docker build . extra", "context . extra"),
    ],
)
def test_unsupported_lines_are_named(line, what):
    assert parse_build_line(line) == Unsupported(what)


@pytest.mark.parametrize("line", ["echo hi", "docker version", "make build", ""])
def test_not_a_build_line(line):
    assert parse_build_line(line) is None
```

- [ ] **Step 2: Run to verify failure** — FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
"""The one supported ``docker build`` shape (spec §4.1). Parsed, never run."""

import posixpath
import shlex
from dataclasses import dataclass

_FORBIDDEN = (
    "--secret", "--ssh", "--mount", "--network", "--pull", "--no-cache",
)
_CHAIN = ("&&", "||", ";")
_OPERATORS = ("|", ">", "<", "&", "`")


@dataclass(frozen=True)
class BuildConfig:
    """What the adapter needs from the CI build line."""

    dockerfile: str
    build_args: tuple[tuple[str, str], ...]
    platform: str | None
    tag: str | None


@dataclass(frozen=True)
class Unsupported:
    """A build line outside the supported shape, and what put it there."""

    what: str


def parse_build_line(line: str) -> BuildConfig | Unsupported | None:
    """Parse one workflow ``run:`` line; ``None`` if it is not a docker build."""
    text = line.strip()
    head = text.split()[:3]
    if len(head) < 2 or head[0] != "docker" or head[1] not in ("build", "buildx"):
        return None
    if head[1] == "buildx":
        return Unsupported("buildx build") if head[2:3] == ["build"] else None
    if any(op in text for op in _CHAIN):
        return Unsupported("shell chain")
    if "$" in text:
        return Unsupported("variable expansion")
    for op in _OPERATORS:
        if op in text:
            return Unsupported(f"shell operator {op}")
    try:
        tokens = shlex.split(text)
    except ValueError:
        return Unsupported("unparseable quoting")
    return _parse_tokens(tokens[2:])


def _parse_tokens(tokens: list[str]) -> BuildConfig | Unsupported:
    dockerfile = "Dockerfile"
    build_args: list[tuple[str, str]] = []
    platform: str | None = None
    tag: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        name, eq, inline = token.partition("=")
        if not token.startswith("-"):
            positional.append(token)
            i += 1
            continue
        if name in _FORBIDDEN:
            return Unsupported(f"flag {name}")
        if name not in ("--file", "-f", "--build-arg", "--platform", "--tag", "-t"):
            return Unsupported(f"unknown flag {name}")
        if eq:
            value = inline
            i += 1
        elif i + 1 < len(tokens):
            value = tokens[i + 1]
            i += 2
        else:
            return Unsupported(f"flag {name} without a value")
        if name in ("--file", "-f"):
            dockerfile = value
        elif name == "--build-arg":
            key, sep, val = value.partition("=")
            if not sep:
                return Unsupported(f"build-arg {value} without a value")
            build_args.append((key, val))
        elif name == "--platform":
            platform = value
        else:
            tag = value
    if positional != ["."]:
        return Unsupported(f"context {' '.join(positional) or '(none)'}")
    normalised = posixpath.normpath(dockerfile)
    if dockerfile.startswith("/") or normalised.startswith(".."):
        return Unsupported(f"dockerfile path {dockerfile}")
    return BuildConfig(normalised, tuple(build_args), platform, tag)
```

- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/buildline.py tests/reproduce/test_buildline.py
git commit -m "feat(reproduce): the supported docker build line of §4.1, everything else named"
```

---

### Task 7: Supported shape, checkout SHA, inert list (§1.2, §1.3 a)

**Files:**
- Create: `src/deployer/reproduce/shape.py`
- Test: `tests/reproduce/test_shape.py`

**Interfaces:**
- Consumes: `FailedRun`, `FailedJob`, `StepInfo` (Task 1); `parse_build_line`, `BuildConfig`, `Unsupported` (Task 6).
- Produces:
  - `Refusal(reason: str)`
  - `Shape(job: FailedJob, workflow_job: str, build_step: int, build: BuildConfig, preceding_unmet: list[str])`
  - `job_text(job: FailedJob) -> str`
  - `precheck(run: FailedRun) -> FailedJob | Refusal` — §1.1 fields, workflow path validity, §1.2 #1, #2, #3 (in that order; #2 over the text of every kept job)
  - `check_workflow(run: FailedRun, job: FailedJob, workflow_text: str) -> Shape | Refusal` — §1.2 #4–#8 and the inert list
  - `INERT_RUNS: frozenset[str]`

- [ ] **Step 1: Write the failing tests** (`tests/reproduce/test_shape.py`)

```python
"""§1.2: every check refuses by name; §1.3 (a): the inert list."""

from dataclasses import replace

import pytest

from deployer.forge import Completeness, Evidence, FailedJob, FailedRun, FailedStep, StepInfo, StepRef
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.shape import Refusal, Shape, check_workflow, precheck

SHA = "d6e330fd8d85f761962d8a134f0ffdd0e914bf9b"
CHECKOUT = "Run actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
BUILD = "Run docker build --file ./Dockerfile ."
WORKFLOW = """\
name: build-image
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd
      - run: docker build --file ./Dockerfile .
"""


def _job(text: str = f"[command]/usr/bin/git log -1 --format=%H\n{SHA}", **kw) -> FailedJob:
    steps = [
        StepInfo(1, "Set up job", "success"),
        StepInfo(2, CHECKOUT, "success"),
        StepInfo(3, BUILD, "failure"),
        StepInfo(5, f"Post {CHECKOUT}", "success"),
        StepInfo(6, "Complete job", "success"),
    ]
    base = FailedJob(
        job_id=7,
        name="build",
        conclusion="failure",
        steps=[FailedStep(StepRef(7, 3), BUILD, "failure", [])],
        evidence=[Evidence(source=None, text=text)],
        completeness=Completeness("present", "absent"),
        all_steps=steps,
    )
    return replace(base, **kw)


def _run(**kw) -> FailedRun:
    base = FailedRun(
        repo="example/project", run_id=1, attempt=1, head_sha=SHA, url="u",
        jobs=[_job()], completeness=Completeness("present", "absent"),
        workflow_ref_path=".github/workflows/diagnosis-polygon.yml",
        workflow_path=".github/workflows/diagnosis-polygon.yml",
        event="workflow_dispatch",
    )
    return replace(base, **kw)


def test_polygon_shape_passes():
    run = _run()
    job = precheck(run)
    assert isinstance(job, FailedJob)
    shape = check_workflow(run, job, WORKFLOW)
    assert shape == Shape(
        job=job, workflow_job="build", build_step=3,
        build=BuildConfig("Dockerfile", (), None, None), preceding_unmet=[],
    )


@pytest.mark.parametrize(
    ("run_kw", "reason"),
    [
        ({"event": None}, "snapshot lacks reproduction fields: event"),
        ({"workflow_path": "ci.yml"}, "workflow path not understood: .github/workflows/diagnosis-polygon.yml"),
        ({"event": "pull_request"}, "event pull_request not supported"),
        ({"jobs": [_job(text="no checkout here")]}, "checkout SHA not established"),
        ({"jobs": [_job(text=f"[command]/usr/bin/git log -1 --format=%H\n{'a' * 40}")]},
         f"checkout at {'a' * 40}, run at {SHA}"),
        ({"jobs": [_job(), _job(job_id=8, evidence=[])]}, "several failed jobs"),
    ],
)
def test_precheck_refusals(run_kw, reason):
    assert precheck(_run(**run_kw)) == Refusal(reason)


def test_all_steps_missing_is_a_field_refusal():
    run = _run(jobs=[_job(all_steps=None)])
    assert precheck(run) == Refusal("snapshot lacks reproduction fields: all_steps")


@pytest.mark.parametrize(
    ("workflow", "reason"),
    [
        (WORKFLOW.replace("    runs-on", "    strategy:\n      matrix:\n        a: [1]\n    runs-on"),
         "job build: strategy.matrix not supported"),
        (WORKFLOW.replace("@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n",
                          "@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n        with:\n          ref: main\n"),
         "checkout input ref not supported"),
        (WORKFLOW.replace("./Dockerfile .", "./Dockerfile . && echo ok"),
         "step binding failed at step 2"),
        (WORKFLOW.replace("  build:\n", "  other:\n"), "no workflow job named build"),
        (WORKFLOW.replace("    runs-on", "    defaults:\n      run:\n        working-directory: app\n    runs-on"),
         "working-directory not supported"),
    ],
)
def test_workflow_refusals(workflow, reason):
    run = _run()
    job = precheck(run)
    assert isinstance(job, FailedJob)
    assert check_workflow(run, job, workflow) == Refusal(reason)


def test_shell_chain_with_consistent_step_name_refuses_with_the_parser_reason():
    wf = WORKFLOW.replace("./Dockerfile .", "./Dockerfile . && echo ok")
    name = "Run docker build --file ./Dockerfile . && echo ok"
    job = _job(
        steps=[FailedStep(StepRef(7, 3), name, "failure", [])],
        all_steps=[StepInfo(1, "Set up job", "success"), StepInfo(2, CHECKOUT, "success"),
                   StepInfo(3, name, "failure"), StepInfo(6, "Complete job", "success")],
    )
    run = _run(jobs=[job])
    assert check_workflow(run, job, wf) == Refusal(
        "unsupported build configuration: shell chain"
    )


def test_block_scalar_single_line_is_still_one_line():  # Review Focus 2
    wf = WORKFLOW.replace(
        "      - run: docker build --file ./Dockerfile .\n",
        "      - run: |\n          docker build --file ./Dockerfile .\n",
    )
    run = _run()
    job = precheck(run)
    assert isinstance(job, FailedJob)
    assert isinstance(check_workflow(run, job, wf), Shape)


def test_non_inert_step_between_checkout_and_build_is_an_unmet_condition():
    wf = WORKFLOW.replace(
        "      - run: docker build",
        "      - run: make gen\n      - run: docker build",
    )
    job = _job(
        steps=[FailedStep(StepRef(7, 4), BUILD, "failure", [])],
        all_steps=[StepInfo(1, "Set up job", "success"), StepInfo(2, CHECKOUT, "success"),
                   StepInfo(3, "Run make gen", "success"), StepInfo(4, BUILD, "failure"),
                   StepInfo(6, "Complete job", "success")],
    )
    run = _run(jobs=[job])
    shape = check_workflow(run, job, wf)
    assert isinstance(shape, Shape)
    assert shape.build_step == 4
    assert shape.preceding_unmet == ["step 3 (Run make gen) is not on the inert list"]


def test_inert_steps_are_exact_strings():
    wf = WORKFLOW.replace(
        "      - run: docker build",
        "      - run: ls -la\n      - run: echo \"$(touch x)\"\n      - run: docker build",
    )
    names = ["Run ls -la", 'Run echo "$(touch x)"']
    job = _job(
        steps=[FailedStep(StepRef(7, 5), BUILD, "failure", [])],
        all_steps=[StepInfo(1, "Set up job", "success"), StepInfo(2, CHECKOUT, "success"),
                   StepInfo(3, names[0], "success"), StepInfo(4, names[1], "success"),
                   StepInfo(5, BUILD, "failure"), StepInfo(6, "Complete job", "success")],
    )
    shape = check_workflow(_run(jobs=[job]), job, wf)
    assert isinstance(shape, Shape)
    assert shape.preceding_unmet == [f"step 4 ({names[1]}) is not on the inert list"]
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** (`src/deployer/reproduce/shape.py`)

```python
"""The supported shape of a failed run (spec §1.2) and the inert list (§1.3 a).

Every §1.2 check refuses by name; only a preceding non-inert step degrades the
restoration to an approximation, and that is recorded, not refused.
"""

import re
from dataclasses import dataclass
from typing import Any

import yaml

from deployer.forge import FailedJob, FailedRun, StepInfo
from deployer.reproduce.buildline import BuildConfig, Unsupported, parse_build_line

INERT_RUNS = frozenset(
    {"ls", "ls -la", "pwd", "docker version", "docker info", "docker buildx version"}
)
_SETUP_BUILDX_RE = re.compile(r"^docker/setup-buildx-action@[0-9a-f]{40}$")
_CHECKOUT_RE = re.compile(r"^actions/checkout@")
_SHA_LINE = "[command]/usr/bin/git log -1 --format=%H"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_EVENTS = ("push", "workflow_dispatch")
_CHECKOUT_FORBIDDEN = (
    "ref", "repository", "path", "sparse-checkout", "lfs", "submodules", "fetch-depth",
)
_RUNNER_STEPS = ("Set up job", "Complete job")


@dataclass(frozen=True)
class Refusal:
    """A named reason reproduction does not proceed."""

    reason: str


@dataclass(frozen=True)
class Shape:
    """A run in the supported shape, with its binding."""

    job: FailedJob
    workflow_job: str
    build_step: int
    build: BuildConfig
    preceding_unmet: list[str]


def job_text(job: FailedJob) -> str:
    """Every piece of a job's evidence text, steps first, in order."""
    parts = [e.text for s in job.steps for e in s.evidence]
    parts.extend(e.text for e in job.evidence)
    return "\n".join(parts)


def precheck(run: FailedRun) -> FailedJob | Refusal:
    """§1.1 fields, then §1.2 #1 event, #2 checkout SHA, #3 one failed job."""
    missing = [
        name
        for name, value in (
            ("workflow_ref_path", run.workflow_ref_path),
            ("workflow_path", run.workflow_path),
            ("event", run.event),
        )
        if value is None
    ]
    if any(job.all_steps is None for job in run.jobs):
        missing.append("all_steps")
    if missing:
        return Refusal(f"snapshot lacks reproduction fields: {', '.join(missing)}")
    path = run.workflow_path or ""
    if not path.startswith(".github/workflows/") or not path.endswith((".yml", ".yaml")):
        return Refusal(f"workflow path not understood: {run.workflow_ref_path}")
    if run.event not in _EVENTS:
        return Refusal(f"event {run.event} not supported")
    shas = _checkout_shas("\n".join(job_text(j) for j in run.jobs))
    if len(shas) != 1:
        return Refusal("checkout SHA not established")
    if shas[0] != run.head_sha:
        return Refusal(f"checkout at {shas[0]}, run at {run.head_sha}")
    if len(run.jobs) != 1:
        return Refusal("several failed jobs")
    return run.jobs[0]


def check_workflow(run: FailedRun, job: FailedJob, workflow_text: str) -> Shape | Refusal:
    """§1.2 #4-#8 against the workflow read from the tree at ``head_sha``."""
    try:
        document = yaml.safe_load(workflow_text)
    except yaml.YAMLError as exc:
        return Refusal(f"workflow not readable: {exc.__class__.__name__}")
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return Refusal("workflow has no jobs")
    matches = [k for k, v in jobs.items() if isinstance(v, dict) and (v.get("name") or k) == job.name]
    if len(matches) != 1:
        return Refusal(f"no workflow job named {job.name}" if not matches else f"several workflow jobs named {job.name}")
    key = matches[0]
    definition: dict[str, Any] = jobs[key]
    for construct, label in (("uses", "job-level uses"), ("container", "container"), ("services", "services")):
        if construct in definition:
            return Refusal(f"job {key}: {label} not supported")
    strategy = definition.get("strategy")
    if isinstance(strategy, dict) and "matrix" in strategy:
        return Refusal(f"job {key}: strategy.matrix not supported")
    steps = definition.get("steps")
    if not isinstance(steps, list):
        return Refusal(f"job {key}: no steps")
    runner_steps = [s for s in (job.all_steps or []) if _is_user_step(s)]
    bound = _bind(steps, runner_steps)
    if isinstance(bound, Refusal):
        return bound
    return _check_steps(document, definition, bound, job, key)


def _check_steps(
    document: dict[str, Any],
    definition: dict[str, Any],
    bound: list[tuple[dict[str, Any], StepInfo]],
    job: FailedJob,
    key: str,
) -> Shape | Refusal:
    checkouts = [i for i, (s, _) in enumerate(bound) if _CHECKOUT_RE.match(str(s.get("uses", "")))]
    builds = [
        (i, parse_build_line(_single_line(s)))
        for i, (s, _) in enumerate(bound)
        if "run" in s and parse_build_line(_single_line(s)) is not None
    ]
    multi = [i for i, (s, _) in enumerate(bound) if "run" in s and _is_multiline(s)
             and parse_build_line(str(s["run"]).strip().splitlines()[0]) is not None]
    if multi:
        return Refusal("unsupported build configuration: multi-line run")
    if len(builds) > 1:
        return Refusal("several build steps")
    failed_numbers = {s.ref.number for s in job.steps if s.conclusion in ("failure", "timed_out")}
    if not builds or bound[builds[0][0]][1].number not in failed_numbers:
        return Refusal("failed step is not a supported build")
    build_index, parsed = builds[0]
    if isinstance(parsed, Unsupported):
        return Refusal(f"unsupported build configuration: {parsed.what}")
    assert parsed is not None
    before = [i for i in checkouts if i < build_index]
    if len(before) != 1:
        return Refusal("exactly one checkout step before the build is required")
    with_block = bound[before[0]][0].get("with") or {}
    for name in _CHECKOUT_FORBIDDEN:
        if name in with_block:
            return Refusal(f"checkout input {name} not supported")
    build_step = bound[build_index][0]
    if "working-directory" in build_step or _default_wd(definition) or _default_wd(document):
        return Refusal("working-directory not supported")
    unmet = [
        f"step {info.number} ({info.name}) is not on the inert list"
        for step, info in bound[before[0] + 1 : build_index]
        if not _is_inert(step)
    ]
    return Shape(
        job=job,
        workflow_job=key,
        build_step=bound[build_index][1].number,
        build=parsed,
        preceding_unmet=unmet,
    )


def _checkout_shas(text: str) -> list[str]:
    lines = text.splitlines()
    return [
        lines[i + 1].strip()
        for i, line in enumerate(lines[:-1])
        if line.strip() == _SHA_LINE and _HEX40_RE.match(lines[i + 1].strip())
    ]


def _is_user_step(step: StepInfo) -> bool:
    return step.name not in _RUNNER_STEPS and not step.name.startswith("Post ")


def _display_name(step: dict[str, Any]) -> str:
    if step.get("name"):
        return str(step["name"])
    if "run" in step:
        return f"Run {str(step['run']).strip().splitlines()[0].strip()}"
    return f"Run {step.get('uses', '')}"


def _bind(
    steps: list[dict[str, Any]], runner_steps: list[StepInfo]
) -> list[tuple[dict[str, Any], StepInfo]] | Refusal:
    for n, (step, info) in enumerate(zip(steps, runner_steps, strict=False), start=1):
        if not isinstance(step, dict) or _display_name(step) != info.name:
            return Refusal(f"step binding failed at step {n}")
    if len(steps) != len(runner_steps):
        return Refusal(f"step binding failed at step {min(len(steps), len(runner_steps)) + 1}")
    return list(zip(steps, runner_steps, strict=True))


def _is_multiline(step: dict[str, Any]) -> bool:
    return len(str(step["run"]).strip().splitlines()) > 1


def _single_line(step: dict[str, Any]) -> str:
    value = str(step.get("run", "")).strip()
    return "" if "\n" in value else value


def _default_wd(scope: dict[str, Any]) -> bool:
    defaults = scope.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return isinstance(run, dict) and "working-directory" in run


def _is_inert(step: dict[str, Any]) -> bool:
    if "run" in step:
        return str(step["run"]).strip() in INERT_RUNS and "\n" not in str(step["run"]).strip()
    uses = str(step.get("uses", ""))
    return bool(_SETUP_BUILDX_RE.match(uses)) and "with" not in step
```

Note on the step-binding test with `&& echo ok` and unchanged runner names: `_bind` fails at step 2 because the display name differs — that is the `step binding failed at step 2` expectation. The consistent case (`test_shell_chain_with_consistent_step_name…`) binds and then refuses with the parser's reason.

- [ ] **Step 4: Run the tests** — `uv run pytest tests/reproduce/test_shape.py -v` → PASS. Long test lines: let `ruff format` wrap them.
- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/shape.py tests/reproduce/test_shape.py
git commit -m "feat(reproduce): supported shape of §1.2, checkout SHA from the log, exact inert list"
```

---

### Task 8: Restoration — extraction and listing comparison (§1.3 b,c; §1.4)

**Files:**
- Create: `src/deployer/reproduce/restore.py`
- Test: `tests/reproduce/test_restore.py`

**Interfaces:**
- Consumes: `TreeListing`, `TreeEntry` (Task 2).
- Produces:
  - `extract(archive: bytes, dest: Path) -> str | None` — `None` on success, else the unavailability reason
  - `listing_conditions(dest: Path, listing: TreeListing) -> list[str]` — unmet conditions (b) and (c)
  - `git_blob_sha(data: bytes) -> str`
  - `make_tarball(tree: Path, prefix: str) -> bytes` — test/bundle helper that builds a GitHub-shaped tarball from a directory (used by Tasks 13–14)

- [ ] **Step 1: Write the failing tests**

```python
"""§1.4 extraction and §1.3 (b)(c): what the archive changed is caught."""

import io
import os
import tarfile

from deployer.forge import TreeEntry, TreeListing
from deployer.reproduce.restore import (
    extract,
    git_blob_sha,
    listing_conditions,
    make_tarball,
)


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("print(1)\n")
    (src / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)
    os.symlink("pkg/a.py", src / "link.py")
    return src


def _listing(src) -> TreeListing:
    entries = [
        TreeEntry("pkg", "040000", "tree", "t"),
        TreeEntry("pkg/a.py", "100644", "blob", git_blob_sha(b"print(1)\n")),
        TreeEntry("run.sh", "100755", "blob", git_blob_sha(b"#!/bin/sh\n")),
        TreeEntry("link.py", "120000", "blob", git_blob_sha(b"pkg/a.py")),
    ]
    return TreeListing("sha", entries, False)


def test_round_trip_is_exact(tmp_path):
    src = _tree(tmp_path)
    dest = tmp_path / "out"
    assert extract(make_tarball(src, "example-project-d6e330f"), dest) is None
    assert os.access(dest / "run.sh", os.X_OK)
    assert (dest / "link.py").is_symlink()
    assert listing_conditions(dest, _listing(src)) == []


def test_git_blob_sha_matches_git():
    assert git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_missing_extra_mode_and_content_are_named(tmp_path):
    src = _tree(tmp_path)
    dest = tmp_path / "out"
    extract(make_tarball(src, "p"), dest)
    (dest / "pkg" / "a.py").write_text("changed\n")
    os.chmod(dest / "run.sh", 0o644)
    (dest / "extra.txt").write_text("x")
    listing = _listing(src)
    listing.entries.append(TreeEntry("gone.txt", "100644", "blob", "0" * 40))
    assert sorted(listing_conditions(dest, listing)) == sorted([
        "archive differs from tree listing: content of pkg/a.py",
        "archive differs from tree listing: mode of run.sh",
        "archive differs from tree listing: extra extra.txt",
        "archive differs from tree listing: missing gone.txt",
    ])


def test_gitattributes_truncation_and_submodules(tmp_path):
    src = _tree(tmp_path)
    dest = tmp_path / "out"
    extract(make_tarball(src, "p"), dest)
    listing = _listing(src)
    listing.entries.append(TreeEntry("docs/.gitattributes", "100644", "blob", "x"))
    listing.entries.append(TreeEntry("vendor/lib", "160000", "commit", "y"))
    out = listing_conditions(dest, TreeListing("sha", listing.entries, True))
    assert "tree listing truncated" in out
    assert ".gitattributes present: docs/.gitattributes" in out
    assert "submodule entry vendor/lib" in out


def test_directory_entries_are_not_extra_paths(tmp_path):  # Review Focus 3
    src = _tree(tmp_path)
    (src / "empty").mkdir()
    dest = tmp_path / "out"
    extract(make_tarball(src, "p"), dest)
    assert listing_conditions(dest, _listing(src)) == []


def _raw_tar(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_unsafe_or_malformed_archives_are_unavailable(tmp_path):
    assert extract(_raw_tar([("a/x", b""), ("b/y", b"")]), tmp_path / "1") == (
        "archive has 2 top-level entries"
    )
    assert "refused" in (extract(_raw_tar([("p/../../evil", b"")]), tmp_path / "2") or "")
    assert extract(b"not a tarball", tmp_path / "3") == "archive unreadable: ReadError"
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

```python
"""Unpack the forge's tarball and prove it equals the Git tree (§1.3 b,c; §1.4).

Extraction uses ``tarfile``'s ``data`` filter (no absolute paths, no ``..``,
no links out of the destination, no devices; executable bits kept) and strips
the single top-level directory GitHub adds. Nothing from the archive runs.
"""

import hashlib
import io
import os
import tarfile
from pathlib import Path

from deployer.forge import TreeListing

_MODE_BLOB = "100644"
_MODE_EXEC = "100755"
_MODE_LINK = "120000"
_MODE_GITLINK = "160000"
_DIFF = "archive differs from tree listing"


def git_blob_sha(data: bytes) -> str:
    """Git's blob object id of ``data``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def extract(archive: bytes, dest: Path) -> str | None:
    """Unpack into ``dest``; the reason it could not, or ``None``."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            members = tar.getmembers()
            tops = {m.name.split("/", 1)[0] for m in members}
            if len(tops) != 1:
                return f"archive has {len(tops)} top-level entries"
            prefix = tops.pop() + "/"
            renamed = [
                _strip(m, prefix) for m in members if m.name.startswith(prefix)
            ]
            dest.mkdir(parents=True, exist_ok=True)
            tar.extractall(dest, members=renamed, filter="data")
    except tarfile.FilterError as exc:
        return f"archive member refused: {exc}"
    except (tarfile.TarError, EOFError) as exc:
        return f"archive unreadable: {exc.__class__.__name__}"
    except OSError as exc:
        return f"archive not written: {exc}"
    return None


def listing_conditions(dest: Path, listing: TreeListing) -> list[str]:
    """Unmet (b) ``.gitattributes`` and (c) path/mode/content equality."""
    unmet: list[str] = []
    if listing.truncated:
        unmet.append("tree listing truncated")
    expected = {}
    for entry in listing.entries:
        if entry.path.rsplit("/", 1)[-1] == ".gitattributes":
            unmet.append(f".gitattributes present: {entry.path}")
        if entry.mode == _MODE_GITLINK:
            unmet.append(f"submodule entry {entry.path}")
        elif entry.type == "blob":
            expected[entry.path] = entry
    on_disk = _files(dest)
    for path in sorted(set(expected) - set(on_disk)):
        unmet.append(f"{_DIFF}: missing {path}")
    for path in sorted(set(on_disk) - set(expected)):
        unmet.append(f"{_DIFF}: extra {path}")
    for path in sorted(set(expected) & set(on_disk)):
        unmet.extend(_compare(dest / path, path, expected[path].mode, expected[path].sha))
    return unmet


def make_tarball(tree: Path, prefix: str) -> bytes:
    """A GitHub-shaped tarball of ``tree`` under one top-level ``prefix``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(tree, arcname=prefix, recursive=True)
    return buf.getvalue()


def _strip(member: tarfile.TarInfo, prefix: str) -> tarfile.TarInfo:
    name = member.name[len(prefix) :]
    changes: dict[str, str] = {"name": name or "."}
    if member.islnk() and member.linkname.startswith(prefix):
        changes["linkname"] = member.linkname[len(prefix) :]
    return member.replace(**changes)


def _files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in filenames:
            out.append((base / name).relative_to(root).as_posix())
        for name in dirnames:
            if (base / name).is_symlink():
                out.append((base / name).relative_to(root).as_posix())
    return out


def _compare(file: Path, path: str, mode: str, sha: str) -> list[str]:
    if mode == _MODE_LINK:
        if not file.is_symlink():
            return [f"{_DIFF}: mode of {path}"]
        target = os.readlink(file).encode()
        return [] if git_blob_sha(target) == sha else [f"{_DIFF}: symlink target of {path}"]
    if file.is_symlink():
        return [f"{_DIFF}: mode of {path}"]
    executable = bool(file.stat().st_mode & 0o111)
    problems: list[str] = []
    if executable != (mode == _MODE_EXEC):
        problems.append(f"{_DIFF}: mode of {path}")
    if git_blob_sha(file.read_bytes()) != sha:
        problems.append(f"{_DIFF}: content of {path}")
    return problems
```

`_MODE_BLOB` documents the regular-file mode and is otherwise unused; drop it if ruff flags it.

- [ ] **Step 4: Run the tests** — PASS. If the `..` test does not report `refused`, check that `tarfile.FilterError` (Python ≥ 3.12) is caught before `TarError`.
- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/restore.py tests/reproduce/test_restore.py
git commit -m "feat(reproduce): safe extraction and archive-vs-Git-tree equality (§1.3 b,c; §1.4)"
```

---

### Task 9: Confirmed-local endpoint (§4.2)

**Files:**
- Create: `src/deployer/reproduce/endpoint.py`, `tests/reproduce/conftest.py`
- Test: `tests/reproduce/test_endpoint.py`

**Interfaces:**
- Consumes: `ContainerRuntime` (`deployer.models`), `deployer.runtime.container_run`, `Refusal` (Task 7).
- Produces:
  - `ENV_OVERRIDES: tuple[str, ...]`
  - `Endpoint(uri: str, source: str)`
  - `confirm_local(runtime: ContainerRuntime, env: Mapping[str, str]) -> Endpoint | Refusal`
  - `is_local(uri: str) -> bool`
  - test fixture `FakeContainers` in `tests/reproduce/conftest.py`: a callable `(runtime, args, **kwargs) -> CompletedProcess` with a `responses: dict[tuple[str, ...], CompletedProcess | Exception]` keyed by the argv prefix, and a `calls` list; plus fixture `fake_containers(monkeypatch)` that patches `deployer.runtime.container_run`.

- [ ] **Step 1: Write the shared fake** (`tests/reproduce/conftest.py`)

```python
"""Shared fakes: every container call goes through deployer.runtime.container_run."""

import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from deployer import runtime as runtime_mod


def proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@dataclass
class FakeContainers:
    """Answers by the longest matching argv prefix; records every call."""

    responses: dict[tuple[str, ...], Any] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, runtime: Any, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(args))
        for n in range(len(args), 0, -1):
            answer = self.responses.get(tuple(args[:n]))
            if answer is not None:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return proc(1, stderr=f"unexpected call {args}")


@pytest.fixture()
def fake_containers(monkeypatch: pytest.MonkeyPatch) -> FakeContainers:
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    return fake
```

- [ ] **Step 2: Write the failing tests** (`tests/reproduce/test_endpoint.py`)

```python
"""§4.2: only a confirmed-local endpoint receives the unfiltered context."""

import json

import pytest

from deployer.models import ContainerRuntime
from deployer.reproduce.endpoint import Endpoint, confirm_local, is_local
from deployer.reproduce.shape import Refusal
from tests.reproduce.conftest import proc

PODMAN = ContainerRuntime(tool="podman")
DOCKER = ContainerRuntime(tool="docker")
MACHINE = "ssh://core@127.0.0.1:56907/run/user/501/podman/podman.sock"


@pytest.mark.parametrize(
    "var", ["DEPLOYER_CONTAINER_HOST", "DOCKER_HOST", "CONTAINER_HOST",
            "CONTAINER_CONNECTION", "DOCKER_CONTEXT"],
)
def test_any_override_refuses_before_detection(var, fake_containers):
    assert confirm_local(PODMAN, {var: "x"}) == Refusal(
        f"endpoint set by {var} not confirmed local"
    )
    assert fake_containers.calls == []


def test_podman_machine_on_loopback_is_local(fake_containers):
    fake_containers.responses[("system", "connection", "list")] = proc(
        stdout=json.dumps([
            {"Name": "root", "URI": "ssh://root@127.0.0.1:1/x", "Default": False},
            {"Name": "default", "URI": MACHINE, "Default": True},
        ])
    )
    assert confirm_local(PODMAN, {}) == Endpoint(MACHINE, "podman_default_connection")


def test_podman_without_connections_uses_the_local_socket(fake_containers):
    fake_containers.responses[("system", "connection", "list")] = proc(stdout="[]")
    assert confirm_local(PODMAN, {}) == Endpoint("unix://", "podman_local_socket")


def test_remote_default_connection_refuses(fake_containers):
    remote = "ssh://core@10.0.0.5/run/podman/podman.sock"
    fake_containers.responses[("system", "connection", "list")] = proc(
        stdout=json.dumps([{"Name": "d", "URI": remote, "Default": True}])
    )
    assert confirm_local(PODMAN, {}) == Refusal(f"endpoint {remote} not confirmed local")


def test_docker_active_context(fake_containers):
    fake_containers.responses[("context", "inspect")] = proc(
        stdout="unix:///var/run/docker.sock\n"
    )
    assert confirm_local(DOCKER, {}) == Endpoint(
        "unix:///var/run/docker.sock", "docker_active_context"
    )


def test_detection_failure_refuses(fake_containers):
    fake_containers.responses[("context", "inspect")] = proc(1, stderr="boom")
    assert confirm_local(DOCKER, {}) == Refusal("endpoint detection failed: boom")


def test_runtime_with_a_host_refuses(fake_containers):
    remote = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    assert confirm_local(remote, {}) == Refusal(
        "endpoint set by --container-host not confirmed local"
    )


@pytest.mark.parametrize(
    ("uri", "local"),
    [("unix:///run/x.sock", True), ("tcp://localhost:2375", True),
     ("ssh://u@[::1]:22/x", True), ("tcp://10.0.0.5:2375", False),
     ("npipe:////./pipe/docker", False)],
)
def test_is_local(uri, local):
    assert is_local(uri) is local
```

- [ ] **Step 3: Run to verify failure** — FAIL. If `tests.reproduce.conftest` cannot be imported, confirm `tests/__init__.py` exists (it does) and `tests/reproduce/__init__.py` was created in Task 3.

- [ ] **Step 4: Implement** (`src/deployer/reproduce/endpoint.py`)

```python
"""Confirm the container endpoint is on this machine (spec §4.2).

The restored context is built unfiltered — CI's builder saw any tracked
``.env`` too — so it may only reach a builder on this host. Overrides are
refused, not judged; the detected endpoint must be a unix socket or loopback.
"""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from deployer import runtime
from deployer.models import ContainerRuntime
from deployer.reproduce.shape import Refusal

ENV_OVERRIDES = (
    "DEPLOYER_CONTAINER_HOST",
    "DOCKER_HOST",
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
    "DOCKER_CONTEXT",
)
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TIMEOUT_S = 15


@dataclass(frozen=True)
class Endpoint:
    """The endpoint the builder will use and how it was chosen."""

    uri: str
    source: str


def is_local(uri: str) -> bool:
    """A unix socket, or ssh/tcp to a loopback host."""
    parsed = urlparse(uri)
    if parsed.scheme == "unix":
        return True
    return parsed.scheme in ("ssh", "tcp") and (parsed.hostname or "") in _LOOPBACK


def confirm_local(rt: ContainerRuntime, env: Mapping[str, str]) -> Endpoint | Refusal:
    """The actual endpoint if it is confirmed local, else a named refusal."""
    for name in ENV_OVERRIDES:
        if env.get(name):
            return Refusal(f"endpoint set by {name} not confirmed local")
    if rt.host is not None:
        return Refusal("endpoint set by --container-host not confirmed local")
    detected = _detect(rt)
    if isinstance(detected, Refusal):
        return detected
    if not is_local(detected.uri):
        return Refusal(f"endpoint {detected.uri} not confirmed local")
    return detected


def _detect(rt: ContainerRuntime) -> Endpoint | Refusal:
    if rt.tool == "docker":
        args = ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    else:
        args = ["system", "connection", "list", "--format", "json"]
    try:
        proc = runtime.container_run(
            rt, args, capture_output=True, text=True, timeout=_TIMEOUT_S
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Refusal(f"endpoint detection failed: {exc.__class__.__name__}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return Refusal(f"endpoint detection failed: {detail}")
    if rt.tool == "docker":
        return Endpoint(proc.stdout.strip(), "docker_active_context")
    try:
        connections = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return Refusal("endpoint detection failed: connection list not JSON")
    defaults = [c for c in connections if isinstance(c, dict) and c.get("Default")]
    if not connections:
        return Endpoint("unix://", "podman_local_socket")
    if len(defaults) != 1:
        return Refusal("endpoint detection failed: no single default connection")
    return Endpoint(str(defaults[0].get("URI", "")), "podman_default_connection")
```

- [ ] **Step 5: Run the tests** — PASS.
- [ ] **Step 6: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/endpoint.py tests/reproduce/conftest.py tests/reproduce/test_endpoint.py
git commit -m "feat(reproduce): confirmed-local endpoint — overrides refuse, loopback or unix only"
```

---

### Task 10: Build adapter, cleanup, local digests (§4.3–§4.5)

**Files:**
- Create: `src/deployer/reproduce/build.py`
- Test: `tests/reproduce/test_build.py`

**Interfaces:**
- Consumes: `BuildConfig` (Task 6), `FakeContainers` (Task 9).
- Produces:
  - `BuildRun(argv: list[str], exit_code: int | None, launch_error: str | None, stdout: str, stderr: str)`
  - `repro_tag(run_id: int, seq: str) -> str` → `localhost/deployer-repro-<run_id>-<seq>`
  - `run_build(rt, context: Path, config: BuildConfig, tag: str, timeout: int) -> BuildRun`
  - `cleanup_image(rt, tag: str, built: bool) -> Literal["removed", "failed", "not_attempted"]`
  - `build_containers_state(rt, finished: bool) -> Literal["removed_by_builder", "not_checked", "not_applicable"]`
  - `local_repo_digests(rt, images: list[str]) -> dict[str, list[str]]`

- [ ] **Step 1: Write the failing tests**

```python
"""§4.3-4.5: own argv, full output, honest cleanup, digests as recorded."""

import json
import subprocess
from pathlib import Path

from deployer.models import ContainerRuntime
from deployer.reproduce.build import (
    build_containers_state,
    cleanup_image,
    local_repo_digests,
    repro_tag,
    run_build,
)
from deployer.reproduce.buildline import BuildConfig
from tests.reproduce.conftest import proc

PODMAN = ContainerRuntime(tool="podman")
DOCKER = ContainerRuntime(tool="docker")
CONFIG = BuildConfig("Dockerfile", (("A", "1"),), "linux/amd64", "ci-tag:1")


def test_argv_passes_the_file_by_path_own_tag_force_rm_no_memory(fake_containers):
    fake_containers.responses[("build",)] = proc(1, stdout="out", stderr="err")
    ctx = Path("/w/tries/001/context")
    run = run_build(PODMAN, ctx, CONFIG, repro_tag(35680991093, "001"), 900)
    assert run.argv == [
        "podman", "build", "--file", "/w/tries/001/context/Dockerfile",
        "--build-arg", "A=1", "--platform", "linux/amd64",
        "--tag", "localhost/deployer-repro-35680991093-001", "--force-rm",
        "/w/tries/001/context",
    ]
    assert "ci-tag:1" not in run.argv and "--memory" not in run.argv
    assert (run.exit_code, run.launch_error, run.stdout, run.stderr) == (
        1, None, "out", "err"
    )


def test_docker_gets_no_force_rm(fake_containers):
    fake_containers.responses[("build",)] = proc(0)
    run = run_build(DOCKER, Path("/c"), CONFIG, "t", 900)
    assert "--force-rm" not in run.argv


def test_timeout_keeps_partial_output(fake_containers):
    exc = subprocess.TimeoutExpired(["podman"], 5, output=b"partial", stderr=None)
    fake_containers.responses[("build",)] = exc
    run = run_build(PODMAN, Path("/c"), CONFIG, "t", 5)
    assert (run.exit_code, run.launch_error, run.stdout) == (None, "timeout", "partial")


def test_missing_executable(fake_containers):
    fake_containers.responses[("build",)] = FileNotFoundError("podman")
    run = run_build(PODMAN, Path("/c"), CONFIG, "t", 5)
    assert (run.exit_code, run.launch_error) == (None, "executable not found")


def test_cleanup_reads_the_return_code(fake_containers):
    assert cleanup_image(PODMAN, "t", built=False) == "not_attempted"
    fake_containers.responses[("rmi",)] = proc(0)
    assert cleanup_image(PODMAN, "t", built=True) == "removed"
    fake_containers.responses[("rmi",)] = proc(1)
    assert cleanup_image(PODMAN, "t", built=True) == "failed"
    fake_containers.responses[("rmi",)] = subprocess.TimeoutExpired(["x"], 1)
    assert cleanup_image(PODMAN, "t", built=True) == "failed"


def test_build_containers_state():
    assert build_containers_state(PODMAN, finished=True) == "removed_by_builder"
    assert build_containers_state(PODMAN, finished=False) == "not_checked"
    assert build_containers_state(DOCKER, finished=False) == "not_applicable"


def test_local_repo_digests(fake_containers):
    fake_containers.responses[("image", "inspect", "--format", "{{json .RepoDigests}}", "a")] = proc(
        stdout=json.dumps(["docker.io/library/a@sha256:" + "1" * 64])
    )
    fake_containers.responses[("image", "inspect", "--format", "{{json .RepoDigests}}", "b")] = proc(1)
    assert local_repo_digests(PODMAN, ["a", "b"]) == {
        "a": ["sha256:" + "1" * 64],
        "b": [],
    }
```

- [ ] **Step 2: Run to verify failure** — FAIL.
- [ ] **Step 3: Implement**

```python
"""The build adapter (spec §4.3), its cleanup (§4.4) and local digests (§4.5).

Its own argv through ``runtime.container_run``: the Dockerfile by path so the
Dockerfile-specific ignore file applies, its own tag, ``--force-rm`` on
Podman, no memory limit. Full output is kept; no class is attached.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from deployer import runtime
from deployer.models import ContainerRuntime
from deployer.reproduce.buildline import BuildConfig

_RMI_TIMEOUT_S = 60
_INSPECT_TIMEOUT_S = 15


@dataclass(frozen=True)
class BuildRun:
    """What the build did; ``exit_code`` is ``None`` iff it did not finish."""

    argv: list[str]
    exit_code: int | None
    launch_error: str | None
    stdout: str
    stderr: str


def repro_tag(run_id: int, seq: str) -> str:
    """The adapter's own, fully qualified tag; CI's ``-t`` is never used."""
    return f"localhost/deployer-repro-{run_id}-{seq}"


def run_build(
    rt: ContainerRuntime, context: Path, config: BuildConfig, tag: str, timeout: int
) -> BuildRun:
    """Build ``context`` with ``config``; never raises for build outcomes."""
    args = ["build", "--file", str(context / config.dockerfile)]
    for key, value in config.build_args:
        args += ["--build-arg", f"{key}={value}"]
    if config.platform is not None:
        args += ["--platform", config.platform]
    args += ["--tag", tag]
    if rt.tool == "podman":
        args.append("--force-rm")
    args.append(str(context))
    argv = [rt.tool, *args]
    try:
        proc = runtime.container_run(
            rt, args, capture_output=True, text=True, errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        return BuildRun(argv, None, "timeout", _text(exc.output), _text(exc.stderr))
    except FileNotFoundError:
        return BuildRun(argv, None, "executable not found", "", "")
    except OSError as exc:
        return BuildRun(argv, None, f"{exc.__class__.__name__}: {exc}", "", "")
    return BuildRun(argv, proc.returncode, None, proc.stdout or "", proc.stderr or "")


def cleanup_image(
    rt: ContainerRuntime, tag: str, built: bool
) -> Literal["removed", "failed", "not_attempted"]:
    """Remove the adapter's tag and read the return code."""
    if not built:
        return "not_attempted"
    try:
        proc = runtime.container_run(
            rt, ["rmi", "-f", tag], capture_output=True, timeout=_RMI_TIMEOUT_S
        )
    except (subprocess.TimeoutExpired, OSError):
        return "failed"
    return "removed" if proc.returncode == 0 else "failed"


def build_containers_state(
    rt: ContainerRuntime, finished: bool
) -> Literal["removed_by_builder", "not_checked", "not_applicable"]:
    """Podman's working containers: removed by ``--force-rm`` only if it finished.

    Docker/BuildKit creates no user-visible build containers.
    """
    if rt.tool == "docker":
        return "not_applicable"
    return "removed_by_builder" if finished else "not_checked"


def local_repo_digests(rt: ContainerRuntime, images: list[str]) -> dict[str, list[str]]:
    """``sha256:…`` digests the local store records per image; ``[]`` if unknown."""
    out: dict[str, list[str]] = {}
    for image in images:
        args = ["image", "inspect", "--format", "{{json .RepoDigests}}", image]
        try:
            proc = runtime.container_run(
                rt, args, capture_output=True, text=True, timeout=_INSPECT_TIMEOUT_S
            )
            values = json.loads(proc.stdout) if proc.returncode == 0 else []
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            values = []
        out[image] = [str(v).split("@", 1)[1] for v in values or [] if "@" in str(v)]
    return out


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
```

- [ ] **Step 4: Run the tests** — PASS.
- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/build.py tests/reproduce/test_build.py
git commit -m "feat(reproduce): build adapter with its own argv, honest cleanup, local digests"
```

---

### Task 11: The builder's `--check` (§2)

**Files:**
- Create: `src/deployer/reproduce/buildcheck.py`
- Create: `tests/fixtures/reproduction/check-outputs/` (five text files + `PROVENANCE.md`)
- Test: `tests/reproduce/test_buildcheck.py`

**Interfaces:**
- Consumes: model types; `FakeContainers`.
- Produces:
  - `BuilderSyntax(state: Literal["passed", "error", "skipped"], line: int | None, text: str | None, reason: str | None)`
  - `read_check_output(exit_code: int | None, launch_error: str | None, output: str) -> tuple[BuilderSyntax, list[ReproductionCheck]]` — the §2 table; the list holds lint `observation`s
  - `detect_buildx(rt) -> str | None` (e.g. `"0.15.1"`)
  - `run_builder_check(rt, context: Path, dockerfile: str, timeout: int) -> tuple[BuilderSyntax, list[ReproductionCheck], str | None]` — third item is the buildx version; Podman → `skipped: backend has no build check`; buildx < 0.15 or absent → skipped
  - `merge_syntax(parser_checks: list[ReproductionCheck], builder: BuilderSyntax, dockerfile: str) -> list[ReproductionCheck]`

- [ ] **Step 1: Create the recorded outputs**

`tests/fixtures/reproduction/check-outputs/docs-lint.txt` — verbatim from https://docs.docker.com/build/checks/ :

```
[+] Building 1.5s (5/5) FINISHED
=> [internal] connecting to local controller
=> [internal] load build definition from Dockerfile
=> => transferring dockerfile: 253B
=> [internal] load metadata for docker.io/library/node:22
=> [auth] library/node:pull token for registry-1.docker.io
=> [internal] load .dockerignore
=> => transferring context: 50B
JSONArgsRecommended - https://docs.docker.com/go/dockerfile/rule/json-args-recommended/
JSON arguments recommended for ENTRYPOINT/CMD to prevent unintended behavior related to OS signals
Dockerfile:7
--------------------
5 |
6 |     COPY index.js .
7 | >>> CMD node index.js
8 |
--------------------
```

`warning-prefixed-lint.txt` — the `WARNING:` form (buildx reference, https://docs.docker.com/reference/cli/docker/buildx/build/):

```
WARNING: InvalidBaseImagePlatform
Base image wrong platform
Dockerfile:1
--------------------
   1 | >>> FROM --platform=linux/arm64 alpine
--------------------
```

`parse-error.txt`, `builder-unreachable.txt`, `lint-then-error.txt`: **record them** from a real Docker ≥ Buildx 0.15 host on synthetic Dockerfiles, before writing the reader — e.g. on the homelab docker host (`ssh` to it and run there; it is a recording of builder output on synthetic files, not a reproduction, so §4.2 does not apply):

```bash
mkdir -p /tmp/chk && cd /tmp/chk
printf 'FROM alpine extra\n' > Dockerfile.parse
printf 'FROM alpine\nCMD echo hi\n' > Dockerfile.lint
printf 'FROM alpine\nCMD echo hi\nCOPY missing /x\n' > Dockerfile.linterr
docker build --check -f Dockerfile.parse . > parse-error.txt 2>&1; echo $?
DOCKER_HOST=tcp://127.0.0.1:1 docker build --check -f Dockerfile.lint . > builder-unreachable.txt 2>&1; echo $?
docker build --check -f Dockerfile.linterr . > lint-then-error.txt 2>&1; echo $?
```

If `lint-then-error.txt` shows only lint (checks do not stat COPY sources), make the mixed case instead by appending one real builder error line recorded from `parse-error.txt` to a copy of `docs-lint.txt`, and say so in `PROVENANCE.md`. Record Docker/Buildx versions, host and date for each file in `PROVENANCE.md`, and the exit codes (write them as the first line `# exit=<n>` of each recorded file; the reader test strips that line).

- [ ] **Step 2: Write the failing tests**

```python
"""§2: two documented lint forms, an ordered table, the every-line rule."""

from pathlib import Path

import pytest

from deployer.models import ContainerRuntime
from deployer.reproduce.buildcheck import (
    BuilderSyntax,
    merge_syntax,
    read_check_output,
    run_builder_check,
)
from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence
from tests.reproduce.conftest import proc

OUT = Path(__file__).parent.parent / "fixtures" / "reproduction" / "check-outputs"


def _recorded(name: str) -> tuple[int, str]:
    text = (OUT / name).read_text()
    if text.startswith("# exit="):
        first, _, rest = text.partition("\n")
        return int(first.removeprefix("# exit=")), rest
    return 1, text  # the two documentation copies: nonzero per Docker's docs


@pytest.mark.parametrize("name", ["docs-lint.txt", "warning-prefixed-lint.txt"])
def test_both_documented_lint_forms_are_row_4(name):
    code, text = _recorded(name)
    syntax, lint = read_check_output(code, None, text)
    assert syntax.state == "passed"
    assert [c.status for c in lint] == ["observation"]


def test_parse_error_is_row_2():
    code, text = _recorded("parse-error.txt")
    syntax, _ = read_check_output(code, None, text)
    assert (syntax.state, syntax.line) == ("error", 1)


@pytest.mark.parametrize("name", ["builder-unreachable.txt", "lint-then-error.txt"])
def test_unrecognised_and_mixed_output_is_row_5(name):
    code, text = _recorded(name)
    syntax, _ = read_check_output(code, None, text)
    assert syntax.state == "skipped"
    assert syntax.reason == "build check output not recognised"


def test_rows_1_and_3():
    assert read_check_output(None, "timeout", "")[0].state == "skipped"
    assert read_check_output(0, None, "")[0].state == "passed"


def test_podman_has_no_check(fake_containers):
    syntax, lint, buildx = run_builder_check(
        ContainerRuntime(tool="podman"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, syntax.reason, lint, buildx) == (
        "skipped", "backend has no build check", [], None
    )
    assert fake_containers.calls == []


def test_old_buildx_is_skipped(fake_containers):
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.14.1 abc\n"
    )
    syntax, _, buildx = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, buildx) == ("skipped", "0.14.1")


def _parser_finding(line: int) -> ReproductionCheck:
    return ReproductionCheck(
        check_id="syntax_from_args", status="failed",
        finding=f"syntax error at line {line}: FROM takes one or three arguments",
        location=Location(file="Dockerfile", lines=(line, line)),
        evidence=[ReproEvidence(kind="log_excerpt", text="parser")],
    )


def test_merge_same_line_cites_both():
    merged = merge_syntax([_parser_finding(1)], BuilderSyntax("error", 1, "x", None), "Dockerfile")
    failed = [c for c in merged if c.status == "failed"]
    assert len(failed) == 1 and len(failed[0].evidence) == 2


def test_merge_builder_only_adds_its_finding():
    merged = merge_syntax([], BuilderSyntax("error", 3, "bad", None), "Dockerfile")
    assert [c.finding for c in merged if c.status == "failed"] == ["line 3: bad"]


def test_merge_parser_only_with_builder_passed_is_inconclusive():
    merged = merge_syntax([_parser_finding(1)], BuilderSyntax("passed", None, None, None), "Dockerfile")
    assert [c.status for c in merged if c.check_id == "syntax_from_args"] == ["inconclusive"]


def test_merge_builder_skipped_keeps_the_parser_result_and_says_so():
    merged = merge_syntax([_parser_finding(1)], BuilderSyntax("skipped", None, None, "backend has no build check"), "Dockerfile")
    assert [c.status for c in merged] == ["failed", "skipped"]
    assert merged[1].check_id == "builder_check"
```

- [ ] **Step 3: Run to verify failure** — FAIL.
- [ ] **Step 4: Implement** (`src/deployer/reproduce/buildcheck.py`)

```python
"""The builder's own ``--check`` (spec §2): run only under ``--reproduce``.

Docker documents no machine-readable form and two text forms; the reader
recognises exactly the shapes below and sends everything else to "not
recognised" — never to a finding.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from deployer import runtime
from deployer.models import ContainerRuntime
from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence

_PROGRESS_RE = re.compile(r"^(\[\+\] |=> |#\d+ )")
_PARSE_RE = re.compile(r"dockerfile parse error on line (\d+): (.*)$")
_HEADER_RE = re.compile(
    r"^(?:WARNING: )?([A-Z][A-Za-z0-9]+)"
    r"(?: - https://docs\.docker\.com/go/dockerfile/rule/[a-z0-9-]+/)?$"
)
_LOCATION_RE = re.compile(r"^\S+:(\d+)$")
_FENCE_RE = re.compile(r"^-+$")
_NUMBERED_RE = re.compile(r"^\s*\d+ \|")
_SUMMARY_RE = re.compile(r"^Check complete, \d+ warnings? (?:has|have) been found!$")
_BUILDX_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_MIN_BUILDX = (0, 15, 0)


@dataclass(frozen=True)
class BuilderSyntax:
    """The builder check's syntax verdict: passed, a line error, or skipped."""

    state: Literal["passed", "error", "skipped"]
    line: int | None
    text: str | None
    reason: str | None


def read_check_output(
    exit_code: int | None, launch_error: str | None, output: str
) -> tuple[BuilderSyntax, list[ReproductionCheck]]:
    """The ordered table of §2; first match wins."""
    if launch_error is not None or exit_code is None:
        return BuilderSyntax("skipped", None, None, launch_error or "not launched"), []
    lines = [ln.rstrip() for ln in output.splitlines()]
    for line in lines:
        match = _PARSE_RE.search(line)
        if match:
            return BuilderSyntax("error", int(match.group(1)), match.group(2), None), []
    content = [ln for ln in lines if ln.strip() and not _PROGRESS_RE.match(ln)]
    blocks, leftover = _lint_blocks(content)
    lint = [
        ReproductionCheck(
            check_id="builder_lint",
            status="observation",
            finding=f"{rule}: {description}",
            location=Location(file="Dockerfile", lines=(line_no, line_no)),
        )
        for rule, description, line_no in blocks
    ]
    if exit_code == 0:
        return BuilderSyntax("passed", None, None, None), lint
    if blocks and not leftover:
        return BuilderSyntax("passed", None, None, None), lint
    return BuilderSyntax("skipped", None, None, "build check output not recognised"), []


def detect_buildx(rt: ContainerRuntime) -> str | None:
    """``docker buildx version`` → ``"0.15.1"``, or ``None``."""
    try:
        proc = runtime.container_run(
            rt, ["buildx", "version"], capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _BUILDX_RE.search(proc.stdout or "") if proc.returncode == 0 else None
    return ".".join(match.groups()) if match else None


def run_builder_check(
    rt: ContainerRuntime, context: Path, dockerfile: str, timeout: int
) -> tuple[BuilderSyntax, list[ReproductionCheck], str | None]:
    """Run ``docker build --check`` when the backend has it; else skipped."""
    if rt.tool != "docker":
        return BuilderSyntax("skipped", None, None, "backend has no build check"), [], None
    version = detect_buildx(rt)
    if version is None:
        return BuilderSyntax("skipped", None, None, "buildx not found"), [], None
    if tuple(int(p) for p in version.split(".")) < _MIN_BUILDX:
        reason = f"buildx {version} < 0.15"
        return BuilderSyntax("skipped", None, None, reason), [], version
    args = ["build", "--check", "--file", str(context / dockerfile), str(context)]
    try:
        proc = runtime.container_run(
            rt, args, capture_output=True, text=True, errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return BuilderSyntax("skipped", None, None, "timeout"), [], version
    except OSError as exc:
        return BuilderSyntax("skipped", None, None, str(exc)), [], version
    syntax, lint = read_check_output(
        proc.returncode, None, (proc.stdout or "") + "\n" + (proc.stderr or "")
    )
    return syntax, lint, version


def merge_syntax(
    parser_checks: list[ReproductionCheck], builder: BuilderSyntax, dockerfile: str
) -> list[ReproductionCheck]:
    """Parser vs builder, syntax only (§2)."""
    builder_ev = ReproEvidence(kind="output_file", path="check.stdout", text=builder.text)
    out: list[ReproductionCheck] = []
    matched = False
    for check in parser_checks:
        if check.status != "failed" or check.location is None:
            out.append(check)
            continue
        if builder.state == "error" and check.location.lines[0] == builder.line:
            out.append(check.model_copy(update={"evidence": [*check.evidence, builder_ev]}))
            matched = True
        elif builder.state == "passed":
            out.append(
                check.model_copy(
                    update={
                        "status": "inconclusive",
                        "reason": "parser found an error the builder check passed",
                        "evidence": [*check.evidence, builder_ev],
                    }
                )
            )
        else:
            out.append(check)
    if builder.state == "error" and not matched and builder.line is not None:
        out.append(
            ReproductionCheck(
                check_id="builder_syntax",
                status="failed",
                finding=f"line {builder.line}: {builder.text}",
                location=Location(file=dockerfile, lines=(builder.line, builder.line)),
                evidence=[builder_ev],
            )
        )
    if builder.state == "skipped":
        out.append(
            ReproductionCheck(
                check_id="builder_check", status="skipped", reason=builder.reason
            )
        )
    return out


def _lint_blocks(lines: list[str]) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Recognised lint blocks and every line that belongs to none."""
    blocks: list[tuple[str, str, int]] = []
    leftover: list[str] = []
    i = 0
    while i < len(lines):
        header = _HEADER_RE.match(lines[i].strip())
        loc = _LOCATION_RE.match(lines[i + 2].strip()) if i + 2 < len(lines) else None
        fence = i + 3 < len(lines) and _FENCE_RE.match(lines[i + 3].strip())
        if header and loc and fence:
            j = i + 4
            while j < len(lines) and _NUMBERED_RE.match(lines[j]):
                j += 1
            if j < len(lines) and _FENCE_RE.match(lines[j].strip()):
                blocks.append((header.group(1), lines[i + 1].strip(), int(loc.group(1))))
                i = j + 1
                continue
        if not _SUMMARY_RE.match(lines[i].strip()):
            leftover.append(lines[i])
        i += 1
    return blocks, leftover
```

The docs copy's excerpt lines `5 |` (no trailing text) match `_NUMBERED_RE` because it only requires `<digits> |`.

- [ ] **Step 5: Run the tests** — PASS. If a recorded file from Step 1 does not land in the expected row, the reader is wrong or the recording is — look at the text before changing either, and note the decision in `PROVENANCE.md`.
- [ ] **Step 6: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/buildcheck.py tests/reproduce/test_buildcheck.py tests/fixtures/reproduction/check-outputs
git commit -m "feat(reproduce): --check reader over both documented forms, every-line rule, merge"
```

---

### Task 12: CI versus local (§7)

**Files:**
- Create: `src/deployer/reproduce/compare.py`
- Test: `tests/reproduce/test_compare.py`

**Interfaces:**
- Consumes: `ParsedDockerfile`, `Instruction`, `normalise` (Task 4); model types (Task 3).
- Produces:
  - `ci_instruction(job_text: str) -> InstructionRef | None`
  - `ci_signature(job_text: str) -> str | None`
  - `ci_digests(job_text: str) -> dict[str, str]` (image → `sha256:…`, from `resolve <image>@sha256:…`)
  - `local_instruction(stdout: str, stderr: str, backend: str, parsed: ParsedDockerfile, parser_findings: list[ReproductionCheck]) -> InstructionRef | None`
  - `local_signature(stdout: str, stderr: str, backend: str) -> str | None`
  - `Side(instruction: InstructionRef | None, signature: str | None, builder_error: str | None)`
  - `compare(*, exit_code: int | None, launch_error: str | None, ci: Side, local: Side, parsed: ParsedDockerfile, dimensions: dict[str, Dimension], values: dict[str, tuple[str | None, str | None]]) -> Comparison`
  - `dimension(ci: str | None, local: str | None) -> Dimension`
  - `digest_dimension(ci: dict[str, str], local: dict[str, list[str]], images: list[str]) -> Dimension` — `same` only when every image's CI digest is among the local repo digests; otherwise `unknown` (a mismatch may be list-vs-instance digest, so no `differs` is claimed)

- [ ] **Step 1: Write the failing tests**

```python
"""§7: identity by line span, a weak signature, one state by a fixed order."""

import json
from pathlib import Path

from deployer.forge import load_snapshot
from deployer.reproduce.compare import (
    Side,
    ci_digests,
    ci_instruction,
    ci_signature,
    compare,
    digest_dimension,
    local_instruction,
    local_signature,
)
from deployer.reproduce.dockerfile import parse, syntax_checks
from deployer.reproduce.model import InstructionRef
from deployer.reproduce.shape import job_text

RUNS = Path(__file__).parent.parent / "fixtures" / "runs"


def _text(name: str) -> str:
    return job_text(load_snapshot((RUNS / f"{name}.json").read_text()).jobs[0])


def test_ci_spans_of_the_committed_snapshots():
    assert ci_instruction(_text("authoring")) == InstructionRef(
        kind="span", lines=(11, 11), bound_by="buildkit_error_block"
    )
    assert ci_instruction(_text("environment")).lines == (7, 9)
    assert ci_instruction(_text("project")).lines == (15, 15)


def test_ci_signatures():
    assert ci_signature(_text("project")) == "FAILED (failures=1)"
    assert ci_signature(_text("environment")).startswith("E: Some index files failed")
    assert ci_signature(_text("authoring")) is None  # COPY: no program output


def test_ci_digests_from_resolve_lines():
    digests = ci_digests(_text("project"))
    assert digests["docker.io/library/python:3.12-slim"].startswith("sha256:")


def test_parse_error_takes_precedence_over_the_block():
    text = "Dockerfile:1\n--------------------\n   1 | >>> FROM a extra\n"
    text += "--------------------\nERROR: failed to build: failed to solve: "
    text += "dockerfile parse error on line 1: FROM requires either one or three arguments\n"
    assert ci_instruction(text) == InstructionRef(
        kind="parse", lines=(1, 1), bound_by="buildkit_parse_error"
    )


def test_two_error_blocks_leave_ci_unbound():
    block = "Dockerfile:3\n--------------------\n   3 | >>> RUN x\n--------------------\n"
    assert ci_instruction(block + block) is None


RUN3 = (
    "FROM python:3.12-slim\n\nCOPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/\n\n"
    "WORKDIR /app\n\nCOPY pyproject.toml uv.lock ./\nRUN uv sync --frozen --no-install-project\n\n"
    "COPY src/ci_build ./src/ci_build\n\nRUN uv sync --frozen\n\nCOPY tests ./tests\n"
    "RUN uv run --frozen python -m unittest discover -s tests\n"
)
PODMAN_RUN3 = (
    "STEP 8/9: COPY tests ./tests\n--> abc\n"
    "STEP 9/9: RUN uv run --frozen python -m unittest discover -s tests\n"
    "F\nFAIL: test_greeting_text\nFAILED (failures=1)\n"
)
PODMAN_RUN3_ERR = (
    'Error: building at STEP "RUN uv run --frozen python -m unittest discover -s tests": '
    "while running runtime: exit status 1\n"
)


def test_local_podman_binding_and_signature():
    parsed = parse(RUN3)
    ref = local_instruction(PODMAN_RUN3, PODMAN_RUN3_ERR, "podman", parsed, [])
    assert ref == InstructionRef(kind="span", lines=(15, 15), bound_by="step_text")
    assert local_signature(PODMAN_RUN3, PODMAN_RUN3_ERR, "podman") == "FAILED (failures=1)"


def test_ansi_and_multistage_prefix_still_bind():  # Review Focus 4
    out = "\x1b[1m[2/2] STEP 9/9: RUN uv run --frozen python -m unittest discover -s tests\x1b[0m\n"
    out += "FAILED (failures=1)\n"
    ref = local_instruction(out, "Error: exit status 1\n", "podman", parse(RUN3), [])
    assert ref is not None and ref.lines == (15, 15)


def test_podman_parse_error_binds_only_with_keyword_and_single_finding():
    parsed = parse("FROM python:3.12-slim extra\n\nRUN true\n")
    findings = [c for c in syntax_checks(parsed, "Dockerfile") if c.status == "failed"]
    err = "Error: FROM requires either one argument, or three: FROM <source> [AS <name>]\n"
    assert local_instruction("", err, "podman", parsed, findings) == InstructionRef(
        kind="parse", lines=(1, 1), bound_by="parser_finding_keyword"
    )
    down = "Error: Cannot connect to Podman. Please verify your connection\n"
    assert local_instruction("", down, "podman", parsed, findings) is None


def _side(lines, sig=None):
    ref = None if lines is None else InstructionRef(kind="span", lines=lines, bound_by="step_text")
    return Side(ref, sig, None)


def _cmp(exit_code=1, launch_error=None, ci=None, local=None, backends=("docker", "podman")):
    return compare(
        exit_code=exit_code, launch_error=launch_error,
        ci=ci or _side((15, 15), "FAILED (failures=1)"),
        local=local or _side((15, 15), "FAILED (failures=1)"),
        parsed=parse(RUN3),
        dimensions={"backend": "differs", "host_arch": "unknown"},
        values={"backend": backends},
    )


def test_state_order():
    assert _cmp(exit_code=None, launch_error="executable not found").state == "not_attempted"
    assert _cmp(exit_code=None, launch_error="timeout").reason == "build did not finish"
    assert _cmp(exit_code=0, local=_side(None)).state == "not_reproduced"
    assert _cmp(local=_side(None)).reason == "local failure unbound"
    assert _cmp(ci=_side(None)).reason == "CI instruction unbound"
    assert _cmp(local=_side((12, 12), "x")).state == "different_failure"
    assert _cmp(local=_side((15, 15), None)).reason == "signature unavailable"
    assert _cmp(local=_side((15, 15), "FAILED (errors=1)")).state == (
        "same_instruction_different_output"
    )
    assert _cmp().state == "reproduced_with_differences"


def test_reproduced_is_unreachable_while_ci_arch_is_unknown():
    result = _cmp()
    assert result.state != "reproduced"
    assert result.dimensions["host_arch"] == "unknown"


def test_copy_across_backends_is_not_compared():
    parsed = parse(RUN3)
    ci = Side(InstructionRef(kind="span", lines=(10, 10), bound_by="buildkit_error_block"), None, "ERROR: a")
    local = Side(InstructionRef(kind="span", lines=(10, 10), bound_by="step_text"), None, "Error: b")
    result = compare(exit_code=125, launch_error=None, ci=ci, local=local, parsed=parsed,
                     dimensions={"backend": "differs"}, values={})
    assert (result.state, result.signature_match) == ("reproduced_with_differences", "not_compared")


def test_digest_dimension_never_claims_differs():
    images = ["python:3.12-slim"]
    assert digest_dimension({"python:3.12-slim": "sha256:a"}, {"python:3.12-slim": ["sha256:a"]}, images) == "same"
    assert digest_dimension({"python:3.12-slim": "sha256:a"}, {"python:3.12-slim": ["sha256:b"]}, images) == "unknown"
    assert digest_dimension({}, {"python:3.12-slim": ["sha256:a"]}, images) == "unknown"
```

`ci_digests` keys: BuildKit prints `resolve docker.io/library/python:3.12-slim@sha256:…`; the Dockerfile says `python:3.12-slim`. `digest_dimension` must therefore match an image to a CI key by suffix: the key equals the image, or ends with `/` + the image, or ends with `/library/` + the image. Put that rule in a helper `_ci_digest_for(image, ci)`.

- [ ] **Step 2: Run to verify failure** — FAIL.
- [ ] **Step 3: Implement** (`src/deployer/reproduce/compare.py`)

```python
"""CI versus local (spec §7): identity by Dockerfile span, one ordered state."""

import re
from dataclasses import dataclass

from deployer.reproduce.dockerfile import KEYWORDS, ParsedDockerfile, normalise
from deployer.reproduce.model import (
    Comparison,
    Dimension,
    InstructionRef,
    ReproductionCheck,
    SignatureMatch,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BLOCK_HEAD_RE = re.compile(r"^[^\s:]+:(\d+)$")
_MARKED_RE = re.compile(r"^\s*(\d+) \| >>>")
_PARSE_RE = re.compile(r"dockerfile parse error on line (\d+):")
_STEP_ERROR_RE = re.compile(r"^#(\d+) ERROR: ")
_STEP_RE = re.compile(r"^(?:\[\d+/\d+\] )?STEP \d+/\d+: (.*)$")
_BUILDING_AT_RE = re.compile(r'building at STEP "(.*)": ')
_RESOLVE_RE = re.compile(r"resolve (\S+)@(sha256:[0-9a-f]{64})")
_PODMAN_ERROR_RE = re.compile(r"^Error: (\S+) ")


@dataclass(frozen=True)
class Side:
    """One side's bound instruction, program-output signature and builder error."""

    instruction: InstructionRef | None
    signature: str | None
    builder_error: str | None


def ci_instruction(job_text: str) -> InstructionRef | None:
    """BuildKit's parse error, else its single ``Dockerfile:N`` block (§7.1)."""
    parses = _PARSE_RE.findall(job_text)
    if len(parses) == 1:
        line = int(parses[0])
        return InstructionRef(kind="parse", lines=(line, line), bound_by="buildkit_parse_error")
    spans = _error_blocks(job_text.splitlines())
    if len(spans) != 1:
        return None
    return InstructionRef(kind="span", lines=spans[0], bound_by="buildkit_error_block")


def ci_signature(job_text: str) -> str | None:
    """Last program-output line of the failing BuildKit step, framing stripped."""
    return _buildkit_signature(job_text.splitlines())


def ci_digests(job_text: str) -> dict[str, str]:
    """Image → digest from BuildKit's ``resolve <image>@sha256:…`` lines."""
    return {m.group(1): m.group(2) for m in _RESOLVE_RE.finditer(job_text)}


def local_instruction(
    stdout: str,
    stderr: str,
    backend: str,
    parsed: ParsedDockerfile,
    parser_findings: list[ReproductionCheck],
) -> InstructionRef | None:
    """Bind the local failure to a span, or to a parse line by keyword (§7.1)."""
    if backend == "docker":
        return ci_instruction(stdout + "\n" + stderr)
    out = _clean(stdout).splitlines()
    err = _clean(stderr)
    at = _BUILDING_AT_RE.findall(err)
    steps = [m.group(1) for ln in out if (m := _STEP_RE.match(ln.strip()))]
    text = at[-1] if at else (steps[-1] if steps else None)
    if text is not None:
        span = _match_instruction(text, parsed)
        return None if span is None else InstructionRef(kind="span", lines=span, bound_by="step_text")
    return _parse_binding(err, parser_findings, parsed)


def local_signature(stdout: str, stderr: str, backend: str) -> str | None:
    """Last program-output line after the last STEP (Podman) or BuildKit's."""
    if backend == "docker":
        return _buildkit_signature((stdout + "\n" + stderr).splitlines())
    lines = _clean(stdout).splitlines()
    last_step = max((i for i, ln in enumerate(lines) if _STEP_RE.match(ln.strip())), default=None)
    if last_step is None:
        return None
    body = [ln.strip() for ln in lines[last_step + 1 :] if ln.strip() and not ln.startswith("--> ")]
    return body[-1] if body else None


def dimension(ci: str | None, local: str | None) -> Dimension:
    """``same``/``differs`` only with values from both sides."""
    if ci is None or local is None:
        return "unknown"
    return "same" if ci == local else "differs"


def digest_dimension(
    ci: dict[str, str], local: dict[str, list[str]], images: list[str]
) -> Dimension:
    """``same`` iff every image's CI digest is recorded locally; else unknown."""
    if not images:
        return "unknown"
    for image in images:
        digest = _ci_digest_for(image, ci)
        if digest is None or digest not in local.get(image, []):
            return "unknown"
    return "same"


def compare(
    *,
    exit_code: int | None,
    launch_error: str | None,
    ci: Side,
    local: Side,
    parsed: ParsedDockerfile,
    dimensions: dict[str, Dimension],
    values: dict[str, tuple[str | None, str | None]],
) -> Comparison:
    """The first state of §7.3 that applies."""
    base = {"ci_instruction": ci.instruction, "dimensions": dimensions, "values": values}
    if launch_error is not None and launch_error != "timeout":
        return Comparison(state="not_attempted", reason=f"build could not start: {launch_error}", **base)
    if launch_error == "timeout":
        return Comparison(state="inconclusive", reason="build did not finish", **base)
    if exit_code == 0:
        return Comparison(state="not_reproduced", **base)
    if ci.instruction is None:
        return Comparison(state="inconclusive", reason="CI instruction unbound", **base)
    if local.instruction is None:
        return Comparison(state="inconclusive", reason="local failure unbound", **base)
    if (ci.instruction.kind, ci.instruction.lines) != (local.instruction.kind, local.instruction.lines):
        return Comparison(state="different_failure", **base)
    match = _signature_match(ci, local, parsed, values.get("backend"))
    if match == "unavailable":
        return Comparison(state="inconclusive", reason="signature unavailable", signature_match=match, **base)
    if match == "unequal":
        return Comparison(state="same_instruction_different_output", signature_match=match, **base)
    state = "reproduced" if all(d == "same" for d in dimensions.values()) else "reproduced_with_differences"
    return Comparison(state=state, signature_match=match, **base)


def _signature_match(
    ci: Side,
    local: Side,
    parsed: ParsedDockerfile,
    backends: tuple[str | None, str | None] | None,
) -> SignatureMatch:
    same_backend = backends is not None and backends[0] == backends[1]
    assert ci.instruction is not None
    first = ci.instruction.lines[0]
    keyword = next((i.keyword for i in parsed.instructions if i.first_line == first), None)
    if ci.instruction.kind == "span" and keyword == "RUN":
        if ci.signature is None or local.signature is None:
            return "unavailable"
        return "equal" if ci.signature == local.signature else "unequal"
    if not same_backend:
        return "not_compared"
    if ci.builder_error is None or local.builder_error is None:
        return "unavailable"
    return "equal" if ci.builder_error == local.builder_error else "unequal"


def _error_blocks(lines: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not _BLOCK_HEAD_RE.match(line.strip()):
            continue
        marked: list[int] = []
        for follow in lines[i + 2 :]:
            if follow.strip().startswith("---"):
                break
            m = _MARKED_RE.match(follow)
            if m:
                marked.append(int(m.group(1)))
        if marked:
            spans.append((min(marked), max(marked)))
    return spans


def _buildkit_signature(lines: list[str]) -> str | None:
    clean = [_clean(ln) for ln in lines]
    errors = [(i, m.group(1)) for i, ln in enumerate(clean) if (m := _STEP_ERROR_RE.match(ln))]
    if not errors:
        return None
    index, step = errors[-1]
    prefix = re.compile(rf"^#{step} \d+\.\d+ (.*)$")
    body = [m.group(1).strip() for ln in clean[:index] if (m := prefix.match(ln)) and m.group(1).strip()]
    return body[-1] if body else None


def _match_instruction(text: str, parsed: ParsedDockerfile) -> tuple[int, int] | None:
    wanted = normalise(text)
    head, _, rest = wanted.partition(" ")
    wanted = f"{head.upper()} {rest}".strip()
    hits = [(i.first_line, i.last_line) for i in parsed.instructions if i.text == wanted]
    return hits[0] if len(hits) == 1 else None


def _parse_binding(
    err: str, findings: list[ReproductionCheck], parsed: ParsedDockerfile
) -> InstructionRef | None:
    lines = [ln.strip() for ln in err.splitlines() if ln.strip().startswith("Error: ")]
    if not lines:
        return None
    m = _PODMAN_ERROR_RE.match(lines[-1])
    keyword = m.group(1) if m else None
    syntax = [f for f in findings if f.status == "failed" and f.check_id.startswith("syntax_")]
    if keyword not in KEYWORDS or len(syntax) != 1 or syntax[0].location is None:
        return None
    line = syntax[0].location.lines[0]
    at_line = next((i for i in parsed.instructions if i.first_line == line), None)
    if at_line is None or at_line.keyword != keyword:
        return None
    return InstructionRef(kind="parse", lines=(line, line), bound_by="parser_finding_keyword")


def _ci_digest_for(image: str, ci: dict[str, str]) -> str | None:
    for key, digest in ci.items():
        if key == image or key.endswith("/" + image) or key.endswith("/library/" + image):
            return digest
    return None


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text)
```

- [ ] **Step 4: Run the tests** — PASS. Watch `test_ci_signatures` for run-2: the last `#11 15.89 …` line before `#11 ERROR:` must be `E: Some index files failed to download. …`.
- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce/compare.py tests/reproduce/test_compare.py
git commit -m "feat(reproduce): CI vs local — line-span identity, weak signature, ordered states"
```

---

### Task 13: Orchestration, storage, verdict and CLI (§1.5, §6)

**Files:**
- Create: `src/deployer/reproduce/run.py`
- Modify: `src/deployer/reproduce/__init__.py` (re-exports)
- Modify: `src/deployer/diagnose.py` (`render_verdict`), `src/deployer/cli.py` (`diagnose` flags, `_cmd_diagnose`, printing)
- Test: `tests/reproduce/test_run.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `run.TryDirError(Exception)`
  - `run.reproduce_run(snapshot: FailedRun, *, gh: GhBytesRunner, rt: ContainerRuntime | None, runtime_error: str | None, env: Mapping[str, str], root: Path, build_timeout: int, max_archive_mb: int = DEFAULT_MAX_ARCHIVE_MB) -> ReproductionSection`
  - `diagnose.render_verdict(diagnosis, reproduction: ReproductionSection | None = None) -> str`
  - CLI: `deployer diagnose … --reproduce [--container-tool …] [--container-host …] [--build-timeout N]`

- [ ] **Step 1: Write the failing orchestration tests** (`tests/reproduce/test_run.py`)

Build a tiny self-contained case in `tmp_path` (the full bundles come in Task 14):

```python
"""Orchestration: order of refusals, storage per try, the manifest."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from deployer.forge import GhError, TreeEntry
from deployer.models import ContainerRuntime
from deployer.reproduce.restore import git_blob_sha, make_tarball
from deployer.reproduce.run import TryDirError, reproduce_run
from tests.reproduce.conftest import proc
from tests.reproduce.test_shape import SHA, WORKFLOW, _run

PODMAN = ContainerRuntime(tool="podman")
DOCKERFILE = "FROM python:3.12-slim\nCOPY docs/setup.md ./setup.md\n"
CONNECTIONS = json.dumps([{"URI": "ssh://core@127.0.0.1:1/x", "Default": True}])


class TreeGh:
    """Serves a directory as GitHub would: tarball bytes and a tree listing."""

    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.fail: GhError | None = None

    def api(self, argv, *, timeout):
        entries = []
        for p in sorted(self.tree.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.tree).as_posix()
                entries.append({"path": rel, "mode": "100644", "type": "blob",
                                "sha": git_blob_sha(p.read_bytes())})
        return json.dumps({"sha": SHA, "tree": entries, "truncated": False})

    def api_bytes(self, argv, *, timeout):
        if self.fail is not None:
            raise self.fail
        return make_tarball(self.tree, "example-project-d6e330f")


@pytest.fixture()
def tree(tmp_path) -> Path:
    t = tmp_path / "tree"
    (t / ".github/workflows").mkdir(parents=True)
    (t / ".github/workflows/diagnosis-polygon.yml").write_text(WORKFLOW)
    (t / "Dockerfile").write_text(DOCKERFILE)
    return t


def _go(tmp_path, tree, fake_containers, **kw):
    fake_containers.responses[("system", "connection", "list")] = proc(stdout=CONNECTIONS)
    fake_containers.responses[("version",)] = proc(stdout=json.dumps({"Client": {"Version": "5.7.0"}}))
    fake_containers.responses[("image", "inspect")] = proc(1)
    args = dict(gh=TreeGh(tree), rt=PODMAN, runtime_error=None, env={},
                root=tmp_path / "work", build_timeout=60)
    args.update(kw)
    return reproduce_run(_run(), **args)


def test_attempted_run_writes_source_try_and_manifest(tmp_path, tree, fake_containers):
    fake_containers.responses[("build",)] = proc(
        125, stdout="STEP 1/2: FROM python:3.12-slim\nSTEP 2/2: COPY docs/setup.md ./setup.md\n",
        stderr='Error: building at STEP "COPY docs/setup.md ./setup.md": no such file\n',
    )
    section = _go(tmp_path, tree, fake_containers)
    assert section.status == "attempted"
    assert section.restoration.state == "exact"
    assert section.try_dir == ".deployer-runs/1/reproduction/attempt-1/tries/001"
    base = tmp_path / "work" / section.try_dir
    assert (base / "context" / "Dockerfile").is_file()
    assert (base / "build.stderr").read_text().startswith("Error: building")
    assert json.loads((base / "manifest.json").read_text())["status"] == "attempted"
    assert json.loads((base.parent.parent / "source.json").read_text())["head_sha"] == SHA
    assert [c.finding for c in section.checks if c.status == "failed"] == [
        "source docs/setup.md absent from the context"
    ]
    assert section.build.failed_instruction.lines == (2, 2)
    assert section.comparison.state in ("inconclusive", "reproduced_with_differences")


def test_second_try_is_002_and_reuses_source(tmp_path, tree, fake_containers):  # Review Focus 5
    fake_containers.responses[("build",)] = proc(1)
    first = _go(tmp_path, tree, fake_containers)
    gh = TreeGh(tree)
    gh.fail = GhError("must not be called", 500)
    second = _go(tmp_path, tree, fake_containers, gh=gh)
    assert first.try_dir.endswith("/001") and second.try_dir.endswith("/002")
    assert (tmp_path / "work" / first.try_dir / "manifest.json").is_file()


def test_source_json_for_another_sha_is_a_try_dir_error(tmp_path, tree, fake_containers):
    fake_containers.responses[("build",)] = proc(1)
    section = _go(tmp_path, tree, fake_containers)
    source = tmp_path / "work" / section.try_dir / ".." / ".." / "source.json"
    data = json.loads(source.read_text())
    source.write_text(json.dumps({**data, "head_sha": "0" * 40}))
    with pytest.raises(TryDirError):
        _go(tmp_path, tree, fake_containers)


def test_precheck_refusal_creates_nothing(tmp_path, tree, fake_containers):
    section = reproduce_run(
        replace(_run(), event="pull_request"), gh=TreeGh(tree), rt=PODMAN,
        runtime_error=None, env={}, root=tmp_path / "work", build_timeout=60,
    )
    assert (section.status, section.refusal) == ("refused", "event pull_request not supported")
    assert not (tmp_path / "work").exists()


def test_archive_http_error_is_unavailable(tmp_path, tree, fake_containers):
    gh = TreeGh(tree)
    gh.fail = GhError("gh: Not Found (HTTP 404)", 404)
    section = _go(tmp_path, tree, fake_containers, gh=gh)
    assert section.status == "unavailable"
    assert section.refusal.startswith("archive fetch failed")


def test_no_runtime_is_refused_with_checks(tmp_path, tree, fake_containers):
    section = _go(tmp_path, tree, fake_containers, rt=None, runtime_error="no container tool found")
    assert (section.status, section.refusal) == ("refused", "no container runtime: no container tool found")
    assert any(c.status == "failed" for c in section.checks)


def test_endpoint_refusal_keeps_checks(tmp_path, tree, fake_containers):
    section = _go(tmp_path, tree, fake_containers, env={"DOCKER_HOST": "tcp://x:1"})
    assert section.refusal == "endpoint set by DOCKER_HOST not confirmed local"
    assert section.build is None and section.checks
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** `src/deployer/reproduce/run.py`

```python
"""Orchestration of one reproduction try (spec §1.5, §6).

Order: prechecks (no I/O) → source (fetch once per attempt, reuse) → workflow
shape → a new try directory → static checks → runtime and endpoint → build →
builder check → comparison → manifest. Refusals after the tree is available
keep the checks already made; a refusal before it creates nothing.
"""

import json
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path

from deployer.forge import (
    DEFAULT_MAX_ARCHIVE_MB,
    FailedRun,
    GhBytesRunner,
    GhError,
    TreeListing,
    fetch_archive,
    fetch_tree_listing,
)
from deployer.models import ContainerRuntime
from deployer.reproduce import build as build_mod
from deployer.reproduce import buildcheck, checks, compare, dockerfile, endpoint, ignore, restore, shape
from deployer.reproduce.model import (
    Binding,
    BuildResult,
    Environment,
    ReproductionCheck,
    ReproductionSection,
    Restoration,
)
from deployer.runtime import probe_runtime_versions


class TryDirError(Exception):
    """The try directory cannot be created, or the attempt names another SHA."""


def reproduce_run(
    snapshot: FailedRun,
    *,
    gh: GhBytesRunner,
    rt: ContainerRuntime | None,
    runtime_error: str | None,
    env: Mapping[str, str],
    root: Path,
    build_timeout: int,
    max_archive_mb: int = DEFAULT_MAX_ARCHIVE_MB,
) -> ReproductionSection:
    """One try; raises :class:`TryDirError` only for the §6 exit-2 case."""
    job = shape.precheck(snapshot)
    if isinstance(job, shape.Refusal):
        return ReproductionSection(status="refused", refusal=job.reason)
    attempt_dir = root / ".deployer-runs" / str(snapshot.run_id) / "reproduction" / f"attempt-{snapshot.attempt}"
    source = _source(snapshot, gh, attempt_dir, max_archive_mb)
    if isinstance(source, str):
        return ReproductionSection(status="unavailable", refusal=source)
    source_dir, listing = source
    workflow = source_dir / (snapshot.workflow_path or "")
    if not workflow.is_file():
        return ReproductionSection(status="refused", refusal=f"workflow file {snapshot.workflow_path} not in the tree")
    found = shape.check_workflow(snapshot, job, workflow.read_text(errors="replace"))
    if isinstance(found, shape.Refusal):
        return ReproductionSection(status="refused", refusal=found.reason)
    try_dir = _new_try(attempt_dir)
    context = try_dir / "context"
    shutil.copytree(source_dir, context, symlinks=True)
    rel_try = try_dir.relative_to(root).as_posix()
    df_path = context / found.build.dockerfile
    parsed = dockerfile.parse(df_path.read_text(errors="replace") if df_path.is_file() else "")
    ci_rules = ignore.load_rules(context, ignore.ci_ignore_file(context, found.build.dockerfile))
    parser_checks = dockerfile.syntax_checks(parsed, found.build.dockerfile)
    static = parser_checks + checks.copy_source_checks(parsed, context, found.build.dockerfile, ci_rules) + checks.from_ref_checks(parsed)
    unmet = found.preceding_unmet + restore.listing_conditions(source_dir, listing) + checks.context_conditions(parsed, ci_rules)
    restoration = Restoration(state="approximation" if unmet else "exact", sha=snapshot.head_sha, unmet=unmet)
    binding = Binding(job_id=job.job_id, workflow_job=found.workflow_job, build_step=found.build_step, dockerfile=found.build.dockerfile)
    common = dict(try_dir=rel_try, restoration=restoration, binding=binding)
    if rt is None:
        section = ReproductionSection(status="refused", refusal=f"no container runtime: {runtime_error or 'none found'}", checks=static, **common)
        return _write(try_dir, section)
    confirmed = endpoint.confirm_local(rt, env)
    if isinstance(confirmed, shape.Refusal):
        section = ReproductionSection(status="refused", refusal=confirmed.reason, checks=static, **common)
        return _write(try_dir, section)
    section = _build_and_compare(snapshot, found, rt, confirmed, context, try_dir, parsed, parser_checks, static, ci_rules, restoration, build_timeout)
    return _write(try_dir, section.model_copy(update=common))


def _build_and_compare(snapshot, found, rt, confirmed, context, try_dir, parsed, parser_checks, static, ci_rules, restoration, build_timeout) -> ReproductionSection:
    seq = try_dir.name
    tag = build_mod.repro_tag(snapshot.run_id, seq)
    run = build_mod.run_build(rt, context, found.build, tag, build_timeout)
    (try_dir / "build.stdout").write_text(run.stdout)
    (try_dir / "build.stderr").write_text(run.stderr)
    syntax, lint, buildx = buildcheck.run_builder_check(rt, context, found.build.dockerfile, build_timeout)
    merged = buildcheck.merge_syntax(parser_checks, syntax, found.build.dockerfile)
    checks_out = merged + lint + [c for c in static if c not in parser_checks]
    images = checks.external_images(parsed)
    local_digests = build_mod.local_repo_digests(rt, images)
    text = shape.job_text(found.job)
    failed = [c for c in parser_checks if c.status == "failed"]
    local_ref = compare.local_instruction(run.stdout, run.stderr, rt.tool, parsed, failed) if run.exit_code not in (None, 0) else None
    ci_side = compare.Side(compare.ci_instruction(text), compare.ci_signature(text), _last_error(text))
    local_side = compare.Side(local_ref, compare.local_signature(run.stdout, run.stderr, rt.tool), _last_error(run.stdout + "\n" + run.stderr))
    ci_ignore = ci_rules.file
    local_ignore = ignore.local_ignore_file(context, found.build.dockerfile, rt.tool)
    values = {"backend": ("docker", rt.tool), "host_arch": (None, platform.machine()), "ignore_file": (ci_ignore, local_ignore)}
    dimensions = {
        "backend": compare.dimension("docker", rt.tool),
        "host_arch": "unknown",
        "base_image_digests": compare.digest_dimension(compare.ci_digests(text), local_digests, images),
        "ignore_file": "same" if ci_ignore == local_ignore else "differs",
        "restoration": "same" if restoration.state == "exact" else "unknown",
    }
    comparison = compare.compare(exit_code=run.exit_code, launch_error=run.launch_error, ci=ci_side, local=local_side, parsed=parsed, dimensions=dimensions, values=values)
    versions = probe_runtime_versions(rt)
    return ReproductionSection(
        status="attempted",
        environment=Environment(backend=rt.tool, backend_version=versions.client_version, endpoint=confirmed.uri, endpoint_source=confirmed.source, buildx_version=buildx, host_arch=platform.machine(), syntax_directive=parsed.syntax_directive),
        checks=checks_out,
        build=BuildResult(argv=run.argv, exit_code=run.exit_code, launch_error=run.launch_error, failed_instruction=local_ref, signature=local_side.signature, stdout="build.stdout", stderr="build.stderr", image_cleanup=build_mod.cleanup_image(rt, tag, built=run.exit_code == 0), build_containers=build_mod.build_containers_state(rt, finished=run.launch_error is None)),
        comparison=comparison,
    )


def _source(snapshot: FailedRun, gh: GhBytesRunner, attempt_dir: Path, max_archive_mb: int) -> tuple[Path, TreeListing] | str:
    source_dir = attempt_dir / "source"
    meta = attempt_dir / "source.json"
    if meta.is_file():
        data = json.loads(meta.read_text())
        if data.get("head_sha") != snapshot.head_sha:
            raise TryDirError(f"{meta} names {data.get('head_sha')}, the run is at {snapshot.head_sha}")
        stored = data["listing"]
        return source_dir, TreeListing(
            sha=stored["sha"],
            entries=[TreeEntry(**e) for e in stored["entries"]],
            truncated=stored["truncated"],
        )
    try:
        listing = fetch_tree_listing(snapshot.repo, snapshot.head_sha, gh)
        archive = fetch_archive(snapshot.repo, snapshot.head_sha, gh, max_bytes=max_archive_mb * 1024 * 1024)
    except GhError as exc:
        return f"archive fetch failed: {exc}"
    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TryDirError(f"cannot create {attempt_dir}: {exc}") from exc
    reason = restore.extract(archive, source_dir)
    if reason is not None:
        shutil.rmtree(source_dir, ignore_errors=True)
        return reason
    meta.write_text(json.dumps({
        "repo": snapshot.repo, "run_id": snapshot.run_id, "attempt": snapshot.attempt,
        "head_sha": snapshot.head_sha, "archive_bytes": len(archive),
        "listing": {"sha": listing.sha, "truncated": listing.truncated,
                    "entries": [e.__dict__ for e in listing.entries]},
        "tree": [e.path for e in listing.entries if e.type == "blob"],
    }, indent=2))
    return source_dir, listing
```

Add `TreeEntry` to the `deployer.forge` import of `run.py`.

Remaining helpers:

```python
def _new_try(attempt_dir: Path) -> Path:
    tries = attempt_dir / "tries"
    try:
        tries.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name) for p in tries.iterdir() if p.name.isdigit()]
        path = tries / f"{max(existing, default=0) + 1:03d}"
        path.mkdir()
    except OSError as exc:
        raise TryDirError(f"cannot create a try under {tries}: {exc}") from exc
    return path


def _write(try_dir: Path, section: ReproductionSection) -> ReproductionSection:
    (try_dir / "manifest.json").write_text(section.model_dump_json(indent=2))
    return section


def _last_error(text: str) -> str | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("ERROR: ", "Error: "))]
    return lines[-1] if lines else None
```

The long signatures and lines above must be wrapped by `ruff format`; give `_build_and_compare` full type hints (`found: shape.Shape`, `rt: ContainerRuntime`, `confirmed: endpoint.Endpoint`, `context: Path`, `try_dir: Path`, `parsed: dockerfile.ParsedDockerfile`, `parser_checks: list[ReproductionCheck]`, `static: list[ReproductionCheck]`, `ci_rules: ignore.IgnoreRules`, `restoration: Restoration`, `build_timeout: int`) so pyrefly passes. `common` is `dict[str, Any]` — type it so.

`src/deployer/reproduce/__init__.py` gains:

```python
from deployer.reproduce.model import ReproductionSection
from deployer.reproduce.run import TryDirError, reproduce_run

__all__ = ["ReproductionSection", "TryDirError", "reproduce_run"]
```

- [ ] **Step 4: Run the orchestration tests** — `uv run pytest tests/reproduce/test_run.py -v` → PASS.

- [ ] **Step 5: Write the failing verdict and CLI tests**

Create `tests/reproduce/test_verdict.py`:

```python
import json
from pathlib import Path

import pytest

from deployer.diagnose import diagnose_run, render_verdict
from deployer.forge import FailedRun, load_snapshot
from deployer.reproduce.model import ReproductionSection

RUNS = Path(__file__).parent.parent / "fixtures" / "runs"


@pytest.fixture()
def authoring_snapshot() -> FailedRun:
    return load_snapshot((RUNS / "authoring.json").read_text())


def test_verdict_without_reproduction_is_unchanged(authoring_snapshot):
    doc = json.loads(render_verdict(diagnose_run(authoring_snapshot)))
    assert doc["verdict_schema_version"] == "1.1" and "reproduction" not in doc


def test_verdict_with_reproduction_is_1_2(authoring_snapshot):
    section = ReproductionSection(status="refused", refusal="event x not supported")
    doc = json.loads(render_verdict(diagnose_run(authoring_snapshot), section))
    assert doc["verdict_schema_version"] == "1.2"
    assert doc["reproduction"]["refusal"] == "event x not supported"
```


In `tests/test_cli.py`, next to the existing `diagnose` tests (the autouse `_stub_fetch_failed_run` already keeps `gh` away; `diagnosis()` and `RUN_URL` are that file's helpers):

```python
from deployer.reproduce import ReproductionSection, TryDirError


def test_reproduce_with_container_host_is_exit_2(capsys) -> None:
    code = cli.main(["diagnose", RUN_URL, "--reproduce", "--container-host", "ssh://u@h"])
    assert code == 2
    assert "--reproduce" in capsys.readouterr().err


def test_reproduce_keeps_the_reading_exit_code(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))
    monkeypatch.setattr(cli, "resolve_runtime", lambda *a, **k: None)
    section = ReproductionSection(status="refused", refusal="event x not supported")
    monkeypatch.setattr(cli, "reproduce_run", lambda *a, **k: section)
    out = tmp_path / "v.json"
    assert cli.main(["diagnose", RUN_URL, "--reproduce", "--output-file", str(out)]) == 3
    document = json.loads(out.read_text())
    assert document["verdict_schema_version"] == "1.2"
    assert document["reproduction"]["status"] == "refused"


def test_try_dir_error_is_exit_2(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))
    monkeypatch.setattr(cli, "resolve_runtime", lambda *a, **k: None)

    def boom(*a, **k):
        raise TryDirError("source.json names another head_sha")

    monkeypatch.setattr(cli, "reproduce_run", boom)
    assert cli.main(["diagnose", RUN_URL, "--reproduce"]) == 2
    assert "another head_sha" in capsys.readouterr().err


def test_without_reproduce_nothing_is_called(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))

    def forbidden(*a, **k):
        raise AssertionError("reproduce_run must not run without --reproduce")

    monkeypatch.setattr(cli, "reproduce_run", forbidden)
    assert cli.main(["diagnose", RUN_URL]) == 3
    assert not (tmp_path / ".deployer-runs").exists()
```

- [ ] **Step 6: Implement verdict and CLI**

`src/deployer/diagnose.py`:

```python
REPRODUCTION_VERDICT_SCHEMA_VERSION = "1.2"


def render_verdict(
    diagnosis: RunDiagnosis, reproduction: "ReproductionSection | None" = None
) -> str:
    """... (keep the existing docstring) With ``reproduction`` the document
    gains that section and reads schema 1.2 (additive); without it the
    document is byte-identical to 1.1."""
    payload = _diagnosis_adapter.dump_python(diagnosis, mode="json")
    if reproduction is None:
        document = {"verdict_schema_version": VERDICT_SCHEMA_VERSION, **payload}
    else:
        document = {
            "verdict_schema_version": REPRODUCTION_VERDICT_SCHEMA_VERSION,
            **payload,
            "reproduction": reproduction.model_dump(mode="json"),
        }
    return json.dumps(document, indent=2) + "\n"
```

Import `ReproductionSection` under `if TYPE_CHECKING:` from `deployer.reproduce.model` to keep `diagnose.py` free of a runtime dependency cycle.

`src/deployer/cli.py`: import `from deployer.reproduce import ReproductionSection, TryDirError, reproduce_run` and `from deployer.forge import SubprocessGh`. On `p_diagnose` add:

```python
    p_diagnose.add_argument(
        "--reproduce",
        action="store_true",
        help="restore the tree at head_sha and rebuild the failed step locally",
    )
    p_diagnose.add_argument(
        "--build-timeout",
        type=int,
        default=DEFAULT_BUILD_TIMEOUT,
        help="seconds allowed for the reproduction build",
    )
    _add_runtime_flags(p_diagnose)
```

In `_cmd_diagnose`, first thing after resolving the ref:

```python
    if args.reproduce and args.container_host:
        print("error: --reproduce builds locally only; drop --container-host", file=sys.stderr)
        return 2
```

After `_print_diagnosis(diagnosis)`:

```python
    section: ReproductionSection | None = None
    if args.reproduce:
        try:
            rt = resolve_runtime(args.container_tool, None)
            runtime_error = None if rt is not None else "no container tool found"
        except RuntimeConfigError as exc:
            rt, runtime_error = None, str(exc)
        try:
            section = reproduce_run(
                result,
                gh=SubprocessGh(),
                rt=rt,
                runtime_error=runtime_error,
                env=os.environ,
                root=Path.cwd(),
                build_timeout=args.build_timeout,
            )
        except TryDirError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _print_reproduction(section)
```

and pass `section` to `render_verdict(diagnosis, section)`. Add:

```python
def _print_reproduction(section: ReproductionSection) -> None:
    """Headline findings, one per line (§6)."""
    print(f"reproduction: {section.status}")
    if section.refusal:
        print(f"  refused: {section.refusal}" if section.status == "refused"
              else f"  unavailable: {section.refusal}")
    if section.restoration is not None:
        unmet = "; ".join(section.restoration.unmet)
        print(f"  restoration: {section.restoration.state}" + (f" ({unmet})" if unmet else ""))
    for check in section.checks:
        if check.status in ("failed", "inconclusive"):
            print(f"  {check.status}: {check.finding or check.reason}")
    if not any(c.status == "failed" and c.check_id.startswith("syntax_") for c in section.checks) and section.checks:
        print("  syntax: no finding among checks 1-4")
    if section.comparison is not None:
        extra = f" ({section.comparison.reason})" if section.comparison.reason else ""
        print(f"  comparison: {section.comparison.state}{extra}")
    if section.try_dir:
        print(f"  try: {section.try_dir}")
```

Keep the existing exit code: `return _EXIT_BY_OUTCOME[diagnosis.outcome]`.

- [ ] **Step 7: Run everything** — `uv run pytest -v` → PASS.
- [ ] **Step 8: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add src/deployer/reproduce src/deployer/diagnose.py src/deployer/cli.py tests
git commit -m "feat(reproduce): orchestration, per-try storage, verdict 1.2 and diagnose --reproduce"
```

---

### Task 14: Acceptance bundles and their replay (§8)

**Files:**
- Create: `tests/fixtures/reproduction/make_bundle.py`
- Create: `tests/fixtures/reproduction/{run-1,run-2,run-3,run-5}/` and the 24 negative bundles
- Create: `tests/reproduce/test_acceptance.py`

**Interfaces:**
- Consumes: `reproduce_run`, `fetch_failed_run`, `dump_snapshot`, `make_tarball`, `git_blob_sha`, `FakeContainers`.
- Produces: bundle layout `snapshot.json`, `tree/`, `tree-listing.json`, `local.stdout`, `local.stderr`, `local.exit` (or `local.timeout` containing `true`), `endpoint.json`, `PROVENANCE.md`, `expected.json`.

- [ ] **Step 1: The bundle tool** (`tests/fixtures/reproduction/make_bundle.py`; not collected by pytest)

```python
"""Dev tool: build a reproduction bundle from a real run (spec §8.A).

    uv run python tests/fixtures/reproduction/make_bundle.py snapshot \
        --run-id 35680991093 --out tests/fixtures/reproduction/run-1
    uv run python tests/fixtures/reproduction/make_bundle.py tree \
        --ref refs/keep/polygon-run-1 --out tests/fixtures/reproduction/run-1

Read-only: the snapshot is re-fetched through forge (no dispatch); the tree
and its listing come from local git objects of the pinned commit.
"""

import argparse
import json
import subprocess
import sys
import tarfile
import io
from pathlib import Path

from deployer.forge import RunRef, dump_snapshot, fetch_failed_run, FailedRun

REPO = "andrei-shtanakov/deployer"
ANON = "example/project"
PATHS = ("/home/runner/work/deployer/deployer", "/home/runner/work/project/project")


def snapshot(run_id: int, out: Path) -> None:
    run = fetch_failed_run(RunRef(REPO, run_id), attempt=None)
    if not isinstance(run, FailedRun):
        sys.exit(f"refused: {run}")
    text = dump_snapshot(run).replace(REPO, ANON).replace(*PATHS)
    out.mkdir(parents=True, exist_ok=True)
    (out / "snapshot.json").write_text(text)


def tree(ref: str, out: Path) -> None:
    archive = subprocess.run(["git", "archive", "--format=tar", ref], check=True, capture_output=True).stdout
    dest = out / "tree"
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    listing = subprocess.run(["git", "ls-tree", "-r", "-t", "--full-tree", ref], check=True, capture_output=True, text=True).stdout
    entries = []
    for line in listing.splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        entries.append({"path": path, "mode": mode, "type": kind, "sha": sha})
    sha = subprocess.run(["git", "rev-parse", ref], check=True, capture_output=True, text=True).stdout.strip()
    (out / "tree-listing.json").write_text(json.dumps({"sha": sha, "truncated": False, "tree": entries}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--run-id", type=int, required=True); s.add_argument("--out", type=Path, required=True)
    t = sub.add_parser("tree"); t.add_argument("--ref", required=True); t.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    snapshot(args.run_id, args.out) if args.cmd == "snapshot" else tree(args.ref, args.out)


if __name__ == "__main__":
    main()
```

Format it with ruff (the compressed argparse lines are for brevity here).

- [ ] **Step 2: Build the four base bundles (by hand, read-only)**

| Bundle | `--run-id` | `--ref` |
|---|---|---|
| `run-1` | 35680991093 | `refs/keep/polygon-run-1` (`d6e330f`) |
| `run-2` | 35680992960 | `refs/keep/polygon-run-2` (`37242cc`) |
| `run-3` | 35680994771 | `refs/keep/polygon-run-3` (`43d7c39`) |
| `run-5` | 35706782471 | `origin/polygon/run-5` (`937d465`) |

`refs/keep/polygon-run-{1,2,3}` exist only in the author's local clone. If they are missing, fetch the commits from GitHub by SHA (`git fetch origin <sha>`) — they are reachable through the runs — or stop and ask.

Then record the local builds (Podman, this machine; §8.C):

```bash
for b in run-1 run-2 run-3 run-5; do
  d=tests/fixtures/reproduction/$b
  podman build --file $d/tree/Dockerfile --tag localhost/repro-probe --force-rm $d/tree \
    > $d/local.stdout 2> $d/local.stderr; echo $? > $d/local.exit
  podman rmi -f localhost/repro-probe >/dev/null 2>&1 || true
done
podman system connection list --format json   # → endpoint.json, as {"tool": "podman", "connections": <that JSON>, "env": {}}
```

Write `PROVENANCE.md` per bundle: run URL, SHA, snapshot fetch date, `git archive` of which ref, Podman version (`podman --version`), date, and "no change" for base bundles. **If a real build fails differently from the spec's Expected column, commit the actual output and write `expected.json` from it; note the divergence in `PROVENANCE.md`** (§8.C: A never claims what C did not record).

`expected.json` for `run-1` (the other three follow the spec's §8 table the same way):

```json
{
  "status": "attempted",
  "refusal": null,
  "restoration": {"state": "exact", "unmet": []},
  "failed_checks": [["copy_sources", "source docs/setup.md absent from the context"]],
  "build_failed_instruction": {"kind": "span", "lines": [11, 11]},
  "signature_match": "not_compared",
  "comparison": "reproduced_with_differences",
  "comparison_reason": null
}
```

- [ ] **Step 3: Build the 24 negative bundles**

Each is a copy of its base bundle with exactly the changes of the spec's §8.A negative table, plus: `PROVENANCE.md` naming the base and every change; `expected.json` for the case; `tree-listing.json` mirrored for any change under `tree/` (recompute the entry's `sha` with `git_blob_sha(new_bytes)`; add/remove entries). Make them with a small throwaway script or by hand; commit only the results. Case-specific notes:

- `generating-step`: in `snapshot.json`, insert `{"number": 3, "name": "Run make gen", "conclusion": "success"}` into `all_steps`, renumber every later step (build 3→4, post/complete +1), update `steps[0].ref.number` and every `evidence[].source.number` of the build from 3 to 4, and add a job-level evidence block `##[group]Run make gen\nmake gen\n##[endgroup]`; in `tree/.github/workflows/diagnosis-polygon.yml` add `- run: make gen` between checkout and build.
- `shell-chain` / `context-subdir`: change the workflow line, the build step's `name` in `all_steps` and in `steps[0]`, and the `##[group]Run …` header and echoed command in the evidence text, all to the same new line.
- `run-mount`: `tree/Dockerfile` line 15 → `RUN --mount=type=bind,target=/src uv run --frozen python -m unittest discover -s tests`; in `snapshot.json` change the `>>>` line 15 and the `#16 [stage-0  9/10] RUN …` header to that text; in `local.stdout` change the `STEP 9/…: RUN …` line to the same text.
- `timeout`: add `local.timeout` containing `true` (the harness raises `TimeoutExpired`).
- `backend-down`: `local.stdout` empty, `local.stderr` = `Error: Cannot connect to Podman. Please verify your connection to the Linux system using \`podman system connection list\`, or try \`podman machine init\` and \`podman machine start\` to manage a new Linux VM\n`, `local.exit` = `125`.
- `snapshot-1.2`: `snapshot.json` = `tests/fixtures/runs/authoring.json` unchanged.
- `endpoint-env`: `endpoint.json` `"env": {"DOCKER_HOST": "tcp://10.0.0.5:2375"}`.

- [ ] **Step 4: The replay test** (`tests/reproduce/test_acceptance.py`)

```python
"""§8.A: every committed bundle replays to its expected result, offline."""

import json
import subprocess
from pathlib import Path

import pytest

from deployer.forge import load_snapshot
from deployer.models import ContainerRuntime
from deployer.reproduce.restore import make_tarball
from deployer.reproduce.run import reproduce_run
from tests.reproduce.conftest import proc

BUNDLES = Path(__file__).parent.parent / "fixtures" / "reproduction"
CASES = sorted(p.name for p in BUNDLES.iterdir() if (p / "expected.json").is_file())


class BundleGh:
    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle

    def api(self, argv, *, timeout):
        return (self.bundle / "tree-listing.json").read_text()

    def api_bytes(self, argv, *, timeout):
        return make_tarball(self.bundle / "tree", "example-project-bundle")


def _containers(bundle: Path, fake) -> tuple[ContainerRuntime, dict[str, str]]:
    endpoint = json.loads((bundle / "endpoint.json").read_text())
    fake.responses[("system", "connection", "list")] = proc(stdout=json.dumps(endpoint["connections"]))
    fake.responses[("version",)] = proc(stdout=json.dumps({"Client": {"Version": "5.7.0"}}))
    fake.responses[("image", "inspect")] = proc(1)
    fake.responses[("rmi",)] = proc(0)
    if (bundle / "local.timeout").is_file():
        fake.responses[("build",)] = subprocess.TimeoutExpired(["podman"], 1)
    else:
        fake.responses[("build",)] = proc(
            int((bundle / "local.exit").read_text()),
            stdout=(bundle / "local.stdout").read_text(),
            stderr=(bundle / "local.stderr").read_text(),
        )
    return ContainerRuntime(tool=endpoint["tool"]), endpoint.get("env", {})


@pytest.mark.parametrize("case", CASES)
def test_bundle_replays_to_expected(case, tmp_path, fake_containers):
    bundle = BUNDLES / case
    rt, env = _containers(bundle, fake_containers)
    snapshot = load_snapshot((bundle / "snapshot.json").read_text())
    section = reproduce_run(snapshot, gh=BundleGh(bundle), rt=rt, runtime_error=None,
                            env=env, root=tmp_path, build_timeout=60)
    expected = json.loads((bundle / "expected.json").read_text())
    assert section.status == expected["status"]
    assert section.refusal == expected["refusal"]
    if expected.get("restoration") is not None:
        assert section.restoration.state == expected["restoration"]["state"]
        for condition in expected["restoration"]["unmet"]:
            assert any(condition in u for u in section.restoration.unmet)
    got_failed = [[c.check_id, c.finding] for c in section.checks if c.status == "failed"]
    assert got_failed == expected.get("failed_checks", got_failed)
    if "comparison" in expected:
        assert section.comparison.state == expected["comparison"]
        assert section.comparison.reason == expected.get("comparison_reason")
        assert section.comparison.signature_match == expected.get("signature_match", section.comparison.signature_match)
        assert section.comparison.state != "reproduced"  # §7.3: unreachable now


def test_every_case_of_the_spec_is_committed():
    spec_cases = {
        "run-1", "run-2", "run-3", "run-5", "snapshot-1.2", "event-pr",
        "checkout-sha-other", "checkout-sha-missing", "checkout-ref",
        "several-failed-jobs", "matrix", "shell-chain", "context-subdir",
        "endpoint-env", "endpoint-remote", "generating-step", "gitattributes",
        "archive-mismatch", "copy-git", "run-mount", "containerignore",
        "local-success", "timeout", "backend-down", "unbound-ci", "other-line",
        "other-output", "signature-missing",
    }
    assert spec_cases <= set(CASES)
```

Note: `archive-mismatch` lists a file in `tree-listing.json` that `tree/` lacks; `BundleGh` tars `tree/` as is, so the listing comparison reports it. `gitattributes` adds `.gitattributes` to both.

- [ ] **Step 5: Run** — `uv run pytest tests/reproduce/test_acceptance.py -v` → all 28 cases PASS. A failing case means the bundle, the expectation or the code disagrees with the spec — find which before changing anything, and never edit `expected.json` to match output without a spec reason written in `PROVENANCE.md`.
- [ ] **Step 6: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add tests/fixtures/reproduction tests/reproduce/test_acceptance.py
git commit -m "test(reproduce): 28 committed bundles replayed offline against the spec's §8 table"
```

---

### Task 15: Docs, ledger, PR

**Files:**
- Modify: `README.md` (a `deployer diagnose --reproduce` section), `CLAUDE.md` (package list gains `reproduce`), `TODO.md`

- [ ] **Step 1: README** — document the flag, what it refuses (the §1.2 list in one paragraph, endpoint rule), where tries land (`.deployer-runs/<run>/reproduction/…`, operator-deleted), that the exit code is unchanged except the two exit-2 cases, and that no cause is asserted.
- [ ] **Step 2: CLAUDE.md** — in the package paragraph add `reproduce` (restoration, closed checks, local rebuild, CI-vs-local comparison; no causal class).
- [ ] **Step 3: TODO.md** — move `ci-failure-reproduction` to `## Shipped` with a short summary (PR number, 28 bundles, the live Podman recordings of run-1/2/3/5); leave `ci-failure-diagnosis` open and change its `@blocked_by` only if the owner says the reproduction satisfies it (otherwise record the question beneath it). Add the follow-ups this work surfaced as open items: running the image after a supported shape (the §5 later slice); a runner fact for the CI host architecture (makes `reproduced` reachable); Buildx `--check` recordings refreshed when Docker changes its output.
- [ ] **Step 4: Full verification**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest
```

Expected: all green. Then one real run against a live failed run, recorded in the PR body (not a test):

```bash
uv run deployer diagnose https://github.com/andrei-shtanakov/deployer/actions/runs/35680994771 --reproduce --output-file /tmp/v.json
```

Expected: exit 3, `comparison: reproduced_with_differences`, a try under `.deployer-runs/35680994771/…`.

- [ ] **Step 5: Commit and open the PR**

```bash
git add README.md CLAUDE.md TODO.md
git commit -m "docs: diagnose --reproduce; TODO ledger"
git push -u origin feat/ci-failure-reproduction
gh pr create --title "feat: CI-failure reproduction — findings, not causes" --body "<summary, the live run, the 28 bundles; ends with the Claude Code attribution line>"
```

Then follow the repo's review rhythm (`sh ../devtools/review-pr.sh deployer <pr> --dry-run`, then without it).

---

## Self-Review

**Spec coverage.** §1.1 → T1; §1.2 → T7; §1.3 (a) → T7, (b)(c) → T8, (d)(e) → T5; §1.4 → T2 + T8; §1.5 → T13; §2 → T11; §3.1 → T4; §3.2–3.3 → T5; §4.1 → T6; §4.2 → T9; §4.3–4.5 → T10 (+ CI digests in T12); §5 → nothing to build (no run), asserted by T13's attempted path never starting a container run; §6 → T3 + T13; §7 → T12; §8.A → T14, §8.B → T2/T8/T10/T13 contract tests, §8.C → T14 Step 2; §9 decisions → T13 storage/no TTL, T10 digests, no ENVIRONMENT candidate anywhere.

**Four interpretations the spec leaves open, decided here and to be confirmed in review:**
- Verdict schema: `"1.2"` only when `reproduction` is present, so the document without `--reproduce` stays byte-identical (§6 "unchanged").
- §3 checks on a refusal *before* the Dockerfile is bound (e.g. `unsupported build configuration`) are not run: they need a bound Dockerfile path. After binding, every refusal keeps them.
- `build_containers` gains `not_applicable` for Docker (BuildKit creates none); the spec names only the two Podman values.
- `base_image_digests` is `same` or `unknown`, never `differs`: CI's `resolve` digest may be a manifest list and the local store may hold the instance digest, so an inequality is not a difference observed on both sides (§7.4's rule).

**Placeholders.** None: every code step carries its code. Hand-run steps (Task 11 Step 1 recordings, Task 14 Steps 2–3) carry their exact commands and per-case edits.

**Type consistency.** `Refusal` lives in `shape.py` and is reused by `endpoint.py`; `InstructionRef`, `Comparison`, `BuildResult`, `ReproductionSection` are defined once in `model.py`; `BuildConfig`/`Unsupported` in `buildline.py`; `TreeListing`/`TreeEntry` in `forge.py`; `BuildRun` in `build.py`; `Side` in `compare.py`.
