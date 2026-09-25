# CI Fix Authoring — Implementation Plan (stages 1, 1b, 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `deployer fix`, `deployer fix publish` and `deployer fix confirm` up to the recording gates: every path is implemented and tested offline, the local and CI "passed" template rows exist but are disabled until real recordings back them (spec stages 3–4, not in this plan).

**Architecture:** A new `deployer.fix` package holds the gate, the instruction binding, the two proposal paths (model + closed COPY envelope; closed F1/F2 for FROM), the local proof over R's adapter, the fix directory/worktree/commit, publication and CI confirmation. Three foundations are separate tasks in existing packages, each behaviour-preserving for its current callers: R's detailed check records (`reproduce`), the set plan + no-ignore-edit issuing mode (`provenance`), and the any-outcome run reader + the conclusion-independent build binding (`forge`, `reproduce.shape`).

**Tech Stack:** Python 3.12, `uv`, pydantic v2, pytest, ruff, pyrefly; Git CLI; `gh` via `forge.GhRunner`; Podman via `ContainerRuntime`; Anthropic SDK behind a protocol (always faked in tests).

**Spec:** `docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md` (rev 2.1 + `b45cc4b`, cited as **F**), which builds on `docs/superpowers/specs/2026-09-24-ci-failure-admission-design.md` (**A**) and `docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md` (**R**).

## Global Constraints

- Entry only through `accept_for_fix` → `Accepted`, plus the trust re-check via `verify_ownership` with `checked_roots` = (R's `source/`, the clone, the fix worktree, the fix directory) (F §2).
- Allowed change: one instruction of `binding.artifact_path` + the re-issued provenance set: pointer modified, one set dir with 3 files added, other set dirs deleted; nothing else (F §3).
- Ignore files are never edited by `deployer fix` (F §5.2).
- Template rows (local Podman, CI BuildKit) ship **disabled**; a row may be enabled only with its recording and its test (F §9). A disabled row yields `templates not enabled`.
- Tests: no live runs, no model calls, no container builds, no network. Real local Git is allowed (G tests). Tests always fake the model and `gh`.
- Statuses: `in_progress | stopped | locally_confirmed | fix_proposed | ci_confirmed`; `last_operation` stored separately (F §1).
- Exit codes: `fix` 0 locally_confirmed / 1 stopped / 2 invocation or local I/O; `fix publish` 0 pushed+PR / 1 refused / 2 local I/O; `fix confirm` 0 positive attempt / 1 insufficient or unpublished / 2 local I/O. Existing commands unchanged (F §8.2).
- `--base` required for `fix publish`, recorded before any network action; `merge-base(base_tip, fix_commit) == head_sha` and the PR diff equals the allowed change (F §8.3).
- CI qualification is `qualified | excluded | undetermined`; any `undetermined` → insufficient (F §7.2, §7.4).
- Every command is total: exceptions become reasons, never tracebacks (F §8.4).
- Line length 88, type hints everywhere incl. tests, docstrings on public API, `uv` only.

## Review Focus

1. A Dockerfile with CRLF line endings — the binding (§3.1) and the byte span must work on the original bytes exactly as R parses them (R reads CRLF like LF); pinned in Task 4.
2. A clone whose `origin` is an HTTPS URL with different letter case than the admission repo — the gate must accept it (A compares repo case-insensitively); pinned in Task 10.
3. The fix directory under a path containing spaces or non-ASCII — worktree, context copy and `fix.json` must work; pinned in Task 11.
4. A second `deployer fix` for the same verdict while a first fix dir exists — a new `fixes/<NNN>` and a new branch, never overwriting; pinned in Task 11.
5. `gh` returning a run listing whose `total_count` exceeds the rows delivered — `undetermined`/incomplete, never a silently shorter listing; pinned in Task 16.

## PR split (review kit ≤ 30 files / ≤ 400 KB)

| PR | Branch | Tasks | Review |
|---|---|---|---|
| F1a | `feat/fix-1-foundations` | 1–2 | ai-prosto |
| F1b | `feat/fix-2-proposal` | 3–9 | ai-prosto |
| F1c | `feat/fix-3-prepare-publish` | 10–14 | ai-prosto |
| F1d | `feat/fix-4-derived-run1` | 15 | **owner** (data) |
| F2 | `feat/fix-5-confirm` | 16–20 | ai-prosto |

Each PR's layer is checked alone (ruff, pyrefly, full pytest) before opening; stacked PRs merge bottom-up with `--merge`. F1d is merged by the owner only.

## File structure

```
src/deployer/reproduce/detail.py        T1  CheckRecord + detailed runners; checks/syntax derive from them
src/deployer/reproduce/checks.py        T1  copy_source_checks derived from records (unchanged output)
src/deployer/reproduce/dockerfile.py    T1  syntax_checks derived from records (unchanged output)
src/deployer/provenance/issue.py        T2  plan_set, exclusion_proven, issue(edit_ignore=)
src/deployer/fix/__init__.py            T3
src/deployer/fix/document.py            T3  FixDocument schema 1.0, statuses, atomic save
src/deployer/fix/binding.py             T4  bind_instruction, link_problem
src/deployer/fix/fromfix.py             T5  stage-name grammar, F1/F2
src/deployer/fix/envelope.py            T6  COPY envelope, eligible candidates
src/deployer/fix/chooser.py             T7  model contract, answer validation, Anthropic impl
src/deployer/fix/regress.py             T8  defect-check pass + no-regression rule
src/deployer/fix/templates.py           T9  closed local+CI template tables (disabled), registry seam
src/deployer/fix/gate.py                T10 clone checks, target, admission, trust re-check
src/deployer/fix/workspace.py           T11 fix dir, worktree, full-diff check, commit (real git)
src/deployer/fix/localproof.py          T12 context, backend, R adapter build, records, templates
src/deployer/fix/author.py              T13 orchestration of `deployer fix`
src/deployer/fix/publish.py             T14 `fix publish`
src/deployer/forge.py                   T16 list_runs_for_sha, read_attempt (any outcome)
src/deployer/reproduce/shape.py         T17 bind_build_any (conclusion-independent)
src/deployer/fix/qualify.py             T17 three-state qualification
src/deployer/fix/ci_eval.py             T18 CI evidence + recurrence + §7.4 evaluation
src/deployer/fix/confirm.py             T19 `fix confirm`
src/deployer/cli.py                     T13, T14, T19 subcommands
tests/fixtures/fix/…                    T15 derived run-1 case (data)
```

---

### Task 1: R's detailed check records

**Files:** Create `src/deployer/reproduce/detail.py`; Modify `src/deployer/reproduce/checks.py:27-75`, `src/deployer/reproduce/dockerfile.py:171-236`; Test `tests/reproduce/test_detail.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class CheckRecord:
    check_id: str                 # "copy_sources" | "syntax_first_from" | "syntax_from_args" | "syntax_keyword" | "syntax_continuation"
    ordinal: int | None           # instruction index in parsed.instructions; None only for file-wide records with no instruction (empty file)
    lines: tuple[int, int] | None # the instruction's (first_line, last_line)
    subject: str                  # copy_sources: normalised source (or raw source for unmodelled); syntax: the rule's condition name
    status: CheckStatus           # reproduce.model.CheckStatus
    reason: str | None            # for every status but "passed"
    finding: ReproductionCheck | None  # the exact ReproductionCheck R emits for a failed/observation unit, else None

@dataclass(frozen=True)
class RecordRun:
    check_id: str                         # "copy_sources" or one syntax check id
    file_status: Literal["ran", "skipped"]
    file_reason: str | None               # R's exact file-wide reason when skipped
    records: list[CheckRecord]            # may be empty, e.g. a Dockerfile without COPY/ADD

def copy_source_records(parsed, context: Path, dockerfile: str, rules: IgnoreRules) -> RecordRun
def syntax_records(parsed, dockerfile: str) -> list[RecordRun]   # one per syntax check id
```

The file-level status is **part of the result**, so a file-wide skip is never inferred from
records: a Dockerfile without COPY/ADD gives `RecordRun("copy_sources", "ran", None, [])`
when checks ran and `RecordRun("copy_sources", "skipped", "<R's reason>", [])` under an
unsupported ignore file or an unread Dockerfile — the two are distinguishable, and the
fold reproduces R's single `skipped` check from `file_status`.

Rules (F §6.2):
- one `copy_sources` record **per source** of every COPY/ADD: `passed` (present, not excluded; glob with a match), `failed` (absent / matches nothing / excluded — `finding` = the `ReproductionCheck` `_check_source` builds today), `skipped` (why-skipped of `_sources`, remote ADD, unmodelled chars — `reason` = the exact reason string R builds today);
- an instruction whose sources cannot be read (`_sources` returns a skip reason) → one record with `subject="*"`, `skipped`;
- a file-wide skip (`unread_reason`, `rules.unsupported`) → `file_status="skipped"` with R's reason **and** a `skipped` record for every COPY/ADD source of every instruction (subject = raw source, or `"*"` when unreadable) — zero records when there are none;
- syntax: one record per `(check_id, instruction)` for every instruction the rule applies to — `syntax_first_from` on the first non-ARG instruction (or `ordinal=None` for an empty file), `syntax_from_args` on every FROM, `syntax_keyword` on every instruction, `syntax_continuation` on the last instruction; status `passed` or `failed`/`observation` (R's rule: `observation` under a `# syntax=` directive); unread → all `skipped`.

`copy_source_checks` and `syntax_checks` become folds over `RecordRun` and must return **exactly** today's lists (same order, same fields): `file_status="skipped"` → today's single skipped check with `file_reason`; otherwise findings of failed records in order, then one aggregate `passed` if there were no failures and at least one `passed`/`failed` record, then the skipped `ReproductionCheck`s in order. `syntax_checks` = per check id, today's order.

- [ ] **Step 1: Parity test first.** In `tests/reproduce/test_detail.py`, before touching `checks.py`, capture today's outputs over every committed bundle tree and a set of synthetic Dockerfiles, then assert the refactored functions return equal lists:

```python
import json
from pathlib import Path

import pytest

from deployer.reproduce import checks, dockerfile, ignore
from deployer.reproduce.detail import copy_source_records, syntax_records

BUNDLES = sorted(Path("tests/fixtures/reproduction").glob("*/tree"))
SYNTHETIC = {
    "multi_source_one_missing": "FROM a:1\nCOPY present.txt missing.txt /d/\n",
    "glob_match": "FROM a:1\nCOPY *.txt /d/\n",
    "remote_add": "FROM a:1\nADD https://x/y /d\n",
    "unmodelled": "FROM a:1\nCOPY $X /d\n",
    "escape_directive": "# escape=`\nFROM a:1\nCOPY present.txt /d\n",
    "syntax_directive_bad_from": "# syntax=docker/dockerfile:1\nFROM a b\n",
    "empty": "",
    "no_copy": "FROM a:1\nRUN true\n",
    "no_copy_unsupported_ignore": "FROM a:1\nRUN true\n",   # run with .dockerignore "[ab]"
}


def _golden(tree: Path, text: str) -> tuple[list[dict], list[dict]]:
    parsed = dockerfile.parse(text)
    rules = ignore.load_rules(tree, ignore.ci_ignore_file(tree, "Dockerfile"))
    return (
        [c.model_dump() for c in checks.copy_source_checks(parsed, tree, "Dockerfile", rules)],
        [c.model_dump() for c in dockerfile.syntax_checks(parsed, "Dockerfile")],
    )
```

Record the golden outputs to `tests/reproduce/detail_golden.json` with a one-off script run **on the unchanged code** (`uv run python tests/reproduce/make_detail_golden.py`; commit both), then `test_outputs_unchanged` asserts equality after the refactor for every bundle tree and every synthetic case (synthetic cases run in `tmp_path` with a `present.txt` and a `.dockerignore` excluding nothing).

- [ ] **Step 2: Record tests** (`test_detail.py`):
  - `test_one_record_per_source`: `COPY present.txt missing.txt /d/` → two records, `present.txt` passed, `missing.txt` failed with `finding.finding == "source missing.txt absent from the context"`.
  - `test_passed_sources_survive_a_failure`: the passed record of `present.txt` exists although another source failed (R's aggregate would hide it).
  - `test_file_wide_skip_propagates`: `# escape=` → every COPY source has a `skipped` record with R's reason; every syntax check id has a `skipped` record per applicable instruction.
  - `test_unsupported_ignore_propagates`: `.dockerignore` with `[ab]` → every source skipped with `ignore pattern not modelled: …`.
  - `test_syntax_per_instruction`: `FROM a b` then `FROM c:1` → `syntax_from_args` failed for ordinal 0, passed for ordinal 1; with `# syntax=` the failed one is `observation`.
  - `test_empty_file_first_from`: empty Dockerfile → one `syntax_first_from` record with `ordinal=None`, failed.
  - `test_no_copy_ran_vs_skipped`: a Dockerfile without COPY/ADD → `file_status="ran"`, no records; the same file with `.dockerignore` `[ab]` → `file_status="skipped"` with R's reason, no records; the parity test covers both (`no_copy`, `no_copy_unsupported_ignore`).
- [ ] **Step 3: Implement** `detail.py` by moving the loop bodies out of `copy_source_checks` / `syntax_checks` into record producers (reuse `_sources`, `_norm`, `_has_unmodelled_chars`, `_check_source`, `_skip`, `_from_args_ok`, `KEYWORDS`); rewrite `copy_source_checks` and `syntax_checks` as folds. Import cycle: `detail.py` imports from `checks.py` and `dockerfile.py` private helpers; `checks.copy_source_checks` imports `copy_source_records` lazily inside the function, or move the helpers into `detail.py` and re-export — choose the one that keeps `checks.py`'s public names unchanged.
- [ ] **Step 4:** `uv run pytest tests/reproduce -q` — parity + record tests pass; full suite green.
- [ ] **Step 5: Commit** `feat(reproduce): detailed per-instruction, per-source check records`.

---

### Task 2: Set plan, check-only exclusion, no-ignore-edit issuing

**Files:** Modify `src/deployer/provenance/issue.py`; Test `tests/provenance/test_issue_plan.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class PlannedSet:
    snapshot_bytes: bytes
    record_bytes: bytes
    record_sha256: str
    paths: tuple[str, ...]   # pointer + the three set files, repo-relative

def plan_set(pre: Preflight, dockerfile: bytes, deployer_version: str) -> PlannedSet
def exclusion_proven(project: Path, paths: Sequence[str]) -> str | None   # never writes; None = proven
def issue(pre, signing_key, deployer_version, dockerfile, *, edit_ignore: bool = True) -> Issued
```

`plan_set` wraps `_build` and computes `paths` exactly as `issue` does today. `exclusion_proven` is `_ensure_excluded_at` **without** the two `_ensure_pattern` calls: symlinked ignore file → reason; unsupported pattern → reason; each path must be excluded by the CI file and the local (podman) file, where a missing file excludes nothing → reason `"{path} is not excluded by {file}"`. `issue(..., edit_ignore=False)` calls `exclusion_proven` instead of `ensure_excluded`; `edit_ignore=True` keeps today's behaviour (authoring unchanged).

- [ ] **Step 1: Tests**
  - `test_plan_matches_issue`: `plan_set(pre, b, "0.1").record_sha256` equals the set dir `issue(pre, key, "0.1", b).set_dir` names; `paths` equals the files `issue` writes.
  - `test_exclusion_proven_never_writes`: a repo with no `.dockerignore` → reason; the repo's file listing and every ignore file's bytes unchanged.
  - `test_narrow_rule_is_not_enough`: `.dockerignore` = `.deployer/authoring/Dockerfile.current\n` → `exclusion_proven` refuses the set-file paths; `issue(edit_ignore=True)` would append `.deployer/` (today's behaviour, asserted) while `issue(edit_ignore=False)` returns `Issued(False, …)` and leaves `.dockerignore` byte-identical.
  - `test_no_edit_mode_issues_when_proven`: `.dockerignore` = `.deployer/\n` → `issue(edit_ignore=False)` publishes; `.dockerignore` unchanged.
  - `test_existing_authoring_unchanged`: the existing `tests/provenance/test_issue.py` passes unmodified.
- [ ] **Step 2–4:** red → implement → green (`uv run pytest tests/provenance -q`).
- [ ] **Step 5: Commit** `feat(provenance): set plan, check-only exclusion, issuing without ignore edits`. **Open PR F1a.**

---

### Task 3: The fix document

**Files:** Create `src/deployer/fix/__init__.py` (docstring), `src/deployer/fix/document.py`; Test `tests/fix/__init__.py`, `tests/fix/test_document.py`.

**Interfaces — Produces** (pydantic, `extra="forbid"`):

```python
FIX_SCHEMA_VERSION = "1.0"
Status = Literal["in_progress", "stopped", "locally_confirmed", "fix_proposed", "ci_confirmed"]
StopReason = Literal["no admission", "fix method not established", "no proposal",
                     "no local confirmation", "commit blocked"]

class LastOperation(BaseModel): command: Literal["fix", "publish", "confirm"]; at: str; result: str; reason: str | None
class StoredFile(BaseModel): path: str; sha256: str          # absolute path + hash at `deployer fix` time
class Input(BaseModel):
    verdict: StoredFile           # the 1.3 document as read; publish/confirm reload it and require the same hash
    root: str                     # the working root the try dir is relative to
    try_dir: str                  # R's try dir (absolute)
    source_dir: str               # R's restored source/ (absolute)
    evidence: list[StoredFile]    # every evidence file the admission section references (ci.log, build.*)
    binding: dict                 # admission binding (repo, head_sha, artifact_path, artifact_sha256)
    reproduction_binding: dict    # R's binding: job_id, workflow_job, build_step, dockerfile
    build: dict                   # R's BuildConfig: dockerfile, build_args, platform
    workflow_path: str            # R's bound workflow path (FailedRun.workflow_path)
    workflow_sha256: str          # sha256 of that workflow's bytes in source/
    backend: str                  # R's environment.backend
    clone: str; origin: str; head: str; clean: bool
    target: dict                  # Target(repo, head_sha, artifact_path, artifact_sha256)
class Proposal(BaseModel): cls: str (alias "class"); file: str; lines: tuple[int, int]; transformation: Literal["copy-source", "F1", "F2"]; original: str; replacement: str; ordinal: int; rationale: list[dict]; envelope: list[dict]
class LocalProof(BaseModel): dockerfile_sha256: str; build: dict; backend: str; versions: dict; records_before: list[dict]; records_after: list[dict]; evidence: list[dict]; later_failure: dict | None
class Publication(BaseModel): worktree: str; branch: str; fix_commit: str | None; diff_ok: bool; base: str | None; pr_url: str | None
class CiAttempt(BaseModel): at: str; considered: list[dict]; outcome: Literal["ci_confirmed", "ci_confirmation_insufficient"]; reason: str | None; evidence: list[dict]
class FixDocument(BaseModel):
    schema_version: Literal["1.0"]; fix_id: str (UUID4, set once at creation); status: Status; stop_reason: StopReason | None; stop_detail: str | None
    input: Input; proposal: Proposal | None; local_proof: LocalProof | None
    publication: Publication | None; ci_attempts: list[CiAttempt]; last_operation: LastOperation | None

def save(doc: FixDocument, path: Path) -> None     # atomic: write tmp in same dir, fsync, os.replace
def load(path: Path) -> FixDocument                # ValidationError/OSError propagate to the CLI (exit 2)
def check_writable(directory: Path) -> str | None  # create+remove a probe file; reason or None
def verify_inputs(doc: FixDocument) -> str | None  # every StoredFile re-hashed; a missing/changed file → reason
```

`Input` is the single producer of what `publish` (T14) and `confirm` (T19) need: they never
re-derive it from the clone. `verify_inputs` re-hashes the verdict and every evidence file
before `accept_for_fix` is re-run; a mismatch refuses (publish exit 1).

Invariants (validator): `stopped` ⇔ `stop_reason` set; `locally_confirmed`/`fix_proposed`/`ci_confirmed` need `proposal`, `local_proof`, `publication.fix_commit`; `fix_proposed`/`ci_confirmed` need `publication.pr_url`; `ci_confirmed` needs the last `ci_attempts` outcome `ci_confirmed`.

- [ ] Tests: each invariant; `verify_inputs` with a changed verdict, a missing evidence file, all intact; `class` on the wire; `save` atomic (a failing `os.replace` monkeypatched leaves the old file intact and no tmp file); `check_writable` on a read-only dir returns a reason; round-trip. Commit `feat(fix): the fix document`.

---

### Task 4: Binding the instruction

**Files:** Create `src/deployer/fix/binding.py`; Test `tests/fix/test_binding.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class Bound:
    ordinal: int
    lines: tuple[int, int]
    original: bytes          # exact bytes of those lines (incl. their line endings) from the original Dockerfile
    instruction: Instruction

def bind_instruction(dockerfile: bytes, defect: Defect) -> Bound | str   # str = reason (fix method not established)
def splice(dockerfile: bytes, bound: Bound, replacement: bytes) -> bytes
def link_problem(original: bytes, corrected: bytes, bound: Bound) -> str | None
```

`bind_instruction` (F §3.1): parse `dockerfile.decode("utf-8", errors="replace")` with R's `dockerfile.parse` (CRLF reads like LF — R's rule); exactly one instruction with `(first_line, last_line) == defect.lines`; cross-check (`missing_copy_source`: `defect.object` equals `_norm` of one of its sources; `from_argument_count`: `instruction.text == defect.object`); `original` = the byte slice of those lines from `dockerfile` split with `splitlines(keepends=True)`. `splice` replaces exactly that slice. `link_problem`: instruction count equal; every other instruction's `text` and span equal; the byte diff outside the span empty; the span's line range unchanged.

- [ ] Tests: unique match; zero/several matches → reason; cross-check failure → reason; CRLF Dockerfile binds and splices preserving `\r\n` (Review Focus 1); a replacement adding a line → `link_problem`; a change outside the span → `link_problem`. Commit `feat(fix): bind the admitted instruction`.

---

### Task 5: Stage-name grammar and F1/F2

**Files:** Create `src/deployer/fix/fromfix.py`, `docs/fix-stage-name-grammar.md` (pinned-source note); Test `tests/fix/test_fromfix.py`.

**Interfaces — Produces:**

```python
STAGE_NAME_RE: re.Pattern[str]
def is_stage_name(token: str) -> bool
@dataclass(frozen=True)
class FromFix: transformation: Literal["F1", "F2"]; replacement: str; conditions: list[str]
def propose_from(parsed: ParsedDockerfile, bound: Bound, build_args: dict[str, str]) -> FromFix | str   # str = no-proposal reason
```

The grammar note records the BuildKit and Buildah revisions read and the rule found in each (stage names are lowercased and must match `^[a-z][a-z0-9-_.]*$` in BuildKit's `instructions` parser; Buildah's `imagebuildah` uses BuildKit's parser package — the implementer **verifies** both at pinned tags, quotes the lines, and sets `STAGE_NAME_RE` to the intersection; tokens with uppercase letters are accepted only if both lowercase them the same way, else refused). F1/F2 exactly as F §4.2. The reference is validated by a **grammar**, not by the presence of `:` — `parse_reference(token) -> Reference | None` implements the distribution reference grammar (`[domain[:port]/]path-component(/path-component)*[:tag][@digest]`, lowercase path components `[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*`, tag `[\w][\w.-]{0,127}`, digest `sha256:[0-9a-f]{64}`, a `domain:port` colon is not a tag) and F1/F2 require `tag or digest` present; the reference comes after an optional inline `--platform=<v>`; no `$`; F1 conditions incl. no `--from=<token>`, no `FROM <token>`, no build arg value equal to the token, no collision with an existing stage name (case-insensitive); F2 dangling `AS`. The replacement keeps every original byte and inserts ` AS` before the token (F1) or deletes the trailing `AS` token and the whitespace before it (F2). `conditions` lists each checked condition for the deterministic explanation.

- [ ] Tests: `parse_reference` — `python:3.12-slim` tag; `registry:5000/app` no tag (port colon); `registry:5000/app:1` tag; `app@sha256:<64 hex>` digest; `Python:3` invalid (uppercase); `app:` invalid; run-5's `FROM python:3.12-slim extra` → F1 `FROM python:3.12-slim AS extra`; `FROM registry:5000/app extra` → no proposal (no tag); F2; each refusal of F §4.2 (`FROM python 3.12`, `FROM python builder`, `-slim`, 4 args, 3 args without AS, `--foo=x`, `$TAG`, collision, `COPY --from=extra`, `FROM extra`, build arg naming it); grammar accepts/rejects table incl. `AS` in any case. Commit `feat(fix): closed FROM transformations F1/F2`.

---

### Task 6: The COPY envelope

**Files:** Create `src/deployer/fix/envelope.py`; Test `tests/fix/test_envelope.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class Candidates: eligible: tuple[str, ...]; conditions: list[dict]   # each {"condition": str, "ok": bool, "detail": str}
def eligible_sources(bound: Bound, absent: str, listing: list[TreeRow], ci_rules: IgnoreRules,
                     local_rules: IgnoreRules) -> Candidates | str     # str = fix method not established (detail)
def apply_source(bound: Bound, absent: str, new: str) -> bytes | str   # the corrected instruction bytes, or a reason
```

F §4.1 conditions 1–5: regular files only (`100644`/`100755`), no symlink/submodule ancestor, not excluded by either rule set, unsupported pattern in either → reason, modelled form (reuse R's `_is_modelled_source`/`_norm`, no whitespace), notation of the original (leading `./` kept iff the original had it), collisions (a)/(b), multi-source instruction requires every other source to be a regular file of the listing, basename floor (> 1 eligible blob with the absent basename → reason, **before** any model call). `apply_source` replaces exactly the absent token's bytes in `bound.original`.

- [ ] Tests: one per condition incl. both collisions, a multi-source instruction with a directory/glob sibling, the basename floor, zero candidates, an unmodelled ignore pattern; `apply_source` keeps flags/destination/other sources byte for byte. Commit `feat(fix): the closed COPY envelope`.

---

### Task 7: The model's contract

**Files:** Create `src/deployer/fix/chooser.py`; Test `tests/fix/test_chooser.py`.

**Interfaces — Produces:**

```python
class SourceChooser(Protocol):
    def choose(self, prompt: str) -> str: ...      # raw model text
class AnthropicChooser:                              # same client/model conventions as llm.AnthropicAuthor
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None
    def choose(self, prompt: str) -> str
def build_prompt(dockerfile: str, bound: Bound, absent: str, facts: ProjectFacts, eligible: Sequence[str]) -> str
@dataclass(frozen=True)
class Choice: source: str; rationale: list[dict]
def validate_answer(raw: str, eligible: Sequence[str], facts: ProjectFacts, listing_paths: set[str]) -> Choice | str
```

`validate_answer` (F §4.1): strict JSON object with exactly `source`, `plausible`, `rationale`; `source` null → `"fix method not established: the model found no replacement"`; `plausible` must equal `[source]` exactly (empty, duplicates, outside `eligible`, mismatch → malformed); several distinct eligible entries → `fix method not established: several plausible candidates`; `rationale` non-empty list of `{"facts": [ {"kind": "path"|"fact", "ref": str} , …], "explanation": str}` with non-empty explanation; each `path` ref in `listing_paths`, each `fact` ref a `ProjectFacts` field name → else malformed. One call only: the orchestrator (Task 13) calls `choose` at most once.

- [ ] Tests: each malformed form; null; several plausible; a citation of a missing path and of an unknown field; a valid answer; `AnthropicChooser` with a fake client returns the text of the first content block. Commit `feat(fix): the model contract for a replacement source`.

---

### Task 8: Defect check and no-regression rule

**Files:** Create `src/deployer/fix/regress.py`; Test `tests/fix/test_regress.py`.

**Interfaces — Produces:**

```python
def defect_check_passes(after: list[RecordRun], cls: DefectClass, ordinal: int, new_source: str | None) -> str | None
def regressions(before: list[RecordRun], after: list[RecordRun], ordinal: int,
                absent: str | None, new_source: str | None) -> list[str]
```

A `RecordRun` with `file_status="skipped"` after (and `ran` before) is itself a regression
of every record it held before; a skipped run on the defect's check → not passed.

Keys `(check_id, ordinal, subject)`; the absent source's key maps to the new source's key; `passed` before and anything else after → a regression line; a key present before and missing after → a regression line; `skipped` or `observation` for the defect check → not passed.

- [ ] Tests over hand-built `RecordRun` values: a run skipped file-wide after, ran before → regressions; passes; regression on another source; a disappeared record; the mapped key; F1 with `syntax_from_args` passed. Commit `feat(fix): the per-record regression rule`.

---

### Task 9: Closed template tables, disabled, with a test seam

**Files:** Create `src/deployer/fix/templates.py`; Test `tests/fix/test_templates.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class Row: id: str; side: Literal["local", "ci"]; backend: Literal["podman", "buildkit"]; kind: Literal["copy", "from"]; recording: str | None
ROWS: tuple[Row, ...]        # "copy-passed/podman", "from-parsed/podman", "copy-passed/buildkit", "from-parsed/buildkit" — all recording=None
Evidence = Literal["passed", "not_confirmed", "binding_ambiguous", "not_enabled", "unknown_format"]
@dataclass(frozen=True)
class Outcome: evidence: Evidence; lines: tuple[int, ...]; detail: str | None; image_pull: str | None
def enabled_rows() -> tuple[Row, ...]            # rows with a recording + rows injected by the test registry
def match_local(kind, corrected_text: str, stdout: str, stderr: str) -> Outcome
def match_ci(kind, corrected_text: str, log: str) -> Outcome
```

A row is enabled iff `recording is not None` **or** it was injected through `_TEST_REGISTRY` (module-private list, only reachable from tests via the `enable_for_test` context manager defined in `tests/fix/conftest.py`, which patches the private list). With no enabled row for the kind → `not_enabled`. Hypothesis matchers (F §6.3, §7.3): COPY/Podman — the `STEP k/n: <corrected text>` line exactly once, then the next `STEP` line of the same build sequence or a completion line, no error after the step line before the next step; FROM — any `STEP` line and no parse error; COPY/BuildKit — exactly one `#k [...] <corrected text>` header and `#k DONE`, `#k CACHED` → `not_confirmed`, missing/repeated `k` → `binding_ambiguous`; FROM/BuildKit — a build-stage header and no `dockerfile parse error`. Lines split with `admission.templates.split_lines`.

- [ ] Tests: `test_no_row_enabled_without_recording` (production `ROWS` all `recording=None` → `enabled_rows()` empty without the seam); `not_enabled` everywhere by default; with the seam: negatives on synthetic lines (CACHED, repeated `k`, a STEP line with no following step, a parse error) — positives are not asserted as truth, only that the matcher returns `passed` on the synthetic shape used by the pipeline tests. Commit `feat(fix): closed template tables, disabled until recorded`. **Open PR F1b.**

---

### Task 10: The gate

**Files:** Create `src/deployer/fix/gate.py`; Test `tests/fix/test_gate.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class Admitted: section: AdmissionSection; target: Target; ownership: OwnershipFacts; clone_head: str; origin: str
def gate(document: Mapping[str, object], root: Path, clone: Path, env: Mapping[str, str],
         extra_roots: Sequence[Path]) -> Admitted | str     # str = no-admission reason (deployer fix)
def recheck_admission(doc: FixDocument, env: Mapping[str, str]) -> str | None
    # publish only: verify_inputs; reload the stored verdict; accept_for_fix(verdict, try_dir, stored target);
    # verify_ownership over the stored source_dir with the CURRENT trust dir and checked_roots =
    # (source_dir, clone, worktree, fix dir). No clone HEAD / cleanliness check (F §8.3).
```

Steps (F §2): clone is a checkout at its toplevel with `origin` (`provenance.gitrepo`), `HEAD` = `binding.head_sha`, `dirty_paths` empty; target = (`origin_slug`, `HEAD`, `binding.artifact_path`, sha256 of the clone's Dockerfile read no-follow); `try_dir = root / reproduction.try_dir`; `accept_for_fix`; `verify_ownership(source_dir, repo=target.repo, artifact_path=…, trust=trust_dir(env), checked_roots=(source_dir, clone, *extra_roots))` must be `confirmed`. `extra_roots` = the planned fix dir and worktree paths.

- [ ] Tests (reuse `tests/admission/conftest.py` replay + `AdmissionSet` to produce a real admitted document and try dir): `recheck_admission` passes after the user moves the clone's `HEAD` or dirties it, and refuses on a revoked key, a changed verdict or evidence file; `gate`: accepted; not at toplevel; no origin; untracked-only dirt; `HEAD` ≠ head_sha; Dockerfile bytes ≠; origin `HTTPS://GitHub.com/Example/Project` case variant accepted (Review Focus 2); revoked key; trust dir inside `source/`. Commit `feat(fix): the gate`.

---

### Task 11: Fix directory, worktree, full-diff check, commit

**Files:** Create `src/deployer/fix/workspace.py`; Test `tests/fix/test_workspace.py` (real local Git).

**Interfaces — Produces:**

```python
def new_fix_dir(attempt_dir: Path) -> Path                          # attempt_dir/fixes/<NNN>, never reused
def outside(path: Path, clone: Path) -> bool
def add_worktree(clone: Path, path: Path, branch: str, head_sha: str) -> str | None   # reason or None
def allowed_diff_problem(worktree: Path, dockerfile: str, bound: Bound) -> str | None # git status --porcelain -z vs F §3
def commit(worktree: Path, message: str) -> str                       # fix commit sha
BRANCH_FMT = "deployer/fix/{cls}/{head12}-{seq}"
```

`allowed_diff_problem`: the changed paths are exactly `Dockerfile` (modified, and only within the bound span — reuse `link_problem`), `.deployer/authoring/Dockerfile.current` (modified), one added `.deployer/authoring/Dockerfile/<sha>/{record.json,record.json.sig,snapshot.json}`, and deleted old set files; anything else → reason naming it.

- [ ] G tests: the clone's `HEAD`, branch and working tree unchanged after a worktree is added and committed in; a second fix dir gets `002` and a different branch (Review Focus 4); a fix dir under `tmp_path / "dir with space ü"` works (Review Focus 3); an extra changed file refused; a Dockerfile change outside the span refused; `outside` false for a path inside the clone. Commit `feat(fix): fix directory, worktree and the allowed diff`.

---

### Task 12: Local proof

**Files:** Create `src/deployer/fix/localproof.py`; Test `tests/fix/test_localproof.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class LocalResult: ok: bool; reason: str | None; proof: LocalProof   # document.LocalProof
def local_proof(section: ReproductionSection, source_dir: Path, fix_dir: Path, corrected: bytes,
                bound: Bound, cls: DefectClass, absent: str | None, new_source: str | None,
                rt: ContainerRuntime, env: Mapping[str, str], build: BuildConfig,
                fix_id: str, build_timeout: int) -> LocalResult
```

Steps (F §6): backend must equal `section.environment.backend` (else `local backend differs from R's`); the endpoint must be confirmed local with R's `endpoint.confirm_local(rt, env)` (a refusal → `no local confirmation: <R's reason>`); copy `source/` to `fix_dir/context` (symlinks kept, made writable, as R does), write `corrected` to `context/<dockerfile>`; records before on `source_dir` (original bytes) and after on the context via Task 1 (CI rules; local rules for the backend) → Task 8 checks; build through `reproduce.build.run_build(rt, context, config, tag, timeout)` with R's bound `BuildConfig` (stored in `Input.build`) and a service tag unique to the fix dir, `localhost/deployer-fix-<fix_id>` where `fix_id` is a UUID4 generated once per fix directory and stored in `FixDocument.fix_id` (never CI's `-t`; `<run_id>-<seq>` would repeat across `attempt-N` directories); afterwards `cleanup_image(rt, tag, built=exit_code == 0)` and `build_containers_state(rt, finished=launch_error is None)`, both recorded in `LocalProof.build` (`image_cleanup`, `build_containers`) exactly as R records them; write `build.stdout`/`build.stderr` with `reproduce.run._write_text`; `templates.match_local`; timeout → not ok; later failure recorded only after `passed`.

- [ ] Tests with the `FakeContainers` fixture from `tests/admission/conftest.py` and the replayed run-1/run-5 (no real builds): the endpoint refused under `DOCKER_HOST` / `--container-host`; the build argv carries the unique fix tag and never CI's; two fix directories with the same `run_id` and the same fix sequence number (under `attempt-1` and `attempt-2`) get different tags; `rmi -f <tag>` issued after a successful build and its result recorded (`removed` / `failed` from a fake non-zero rmi); default → `no local confirmation: templates not enabled`; with the seam and a synthetic passing stdout → ok and `later_failure` recorded for a synthetic later error; timeout → not ok; backend `docker` → refused; a regression (corrected Dockerfile that breaks another source) → not ok naming it; `source/` untouched (tree hash before/after). Commit `feat(fix): the local proof`.

---

### Task 13: `deployer fix`

**Files:** Create `src/deployer/fix/author.py`; Modify `src/deployer/cli.py` (subparser `fix` with `fix`, `publish`, `confirm` — `publish`/`confirm` added in Tasks 14/19); Test `tests/fix/test_author.py`, `tests/test_cli.py`.

**Interfaces — Produces:** `author_fix(verdict: Path, clone: Path, root: Path, env, signing_key: Path | None, chooser: SourceChooser, rt: ContainerRuntime | None, build_timeout: int) -> FixDocument`.

Order (F §5.3): new fix dir + writability (`check_writable`, else exit 2) → save `in_progress` → gate (Task 10) → bind (Task 4) → preconditions: `outside` fix dir/worktree (else exit 2), worktree add, `preflight` in the worktree with the key, preliminary `exclusion_proven` (pointer + a placeholder set path) → proposal (Task 5 or Tasks 6+7; the chooser is called **at most once** and never on an envelope stop) → local proof (Task 12) → `plan_set` + final `exclusion_proven` on its real paths → write the corrected bytes in the worktree, `issue(…, edit_ignore=False)`, require `Issued.set_dir == set_dir_name(plan.record_sha256)` → `allowed_diff_problem` → `commit` → `locally_confirmed`. Checkpoint saves after preconditions, proposal, local proof, commit. Stops set `stopped` + reason; exceptions → reason (never a traceback); a save failure after the commit → exit 2 naming fix dir, worktree, branch, commit.

- [ ] Tests (P level, faked runtime/model/gh, real Git; run-1 variants built at test time via `AdmissionSet`, not from committed data: `basename-unique` — one other file named `setup.md` exists, the envelope passes with every eligible blob in the prompt and the fake model picks it; `basename-ambiguous` — two files named `setup.md`): run-5 → `stopped: no local confirmation` by default; with the seam → `locally_confirmed`, the commit holds the F1 line and the new set; run-1 `basename-ambiguous` → `fix method not established` and the fake chooser asserts no call; run-1 `basename-unique` with the seam → `locally_confirmed`, the proposal's `rationale` from the fake; model malformed → stop, one call; no key → `commit blocked`; exclusion needing an edit → `commit blocked`, nothing written; save failure after commit → exit 2 with identifiers; CLI exit table. Commit `feat(fix): deployer fix`.

---

### Task 14: `deployer fix publish`

**Files:** Create `src/deployer/fix/publish.py`; Modify `src/deployer/cli.py`; Test `tests/fix/test_publish.py`.

**Interfaces — Produces:** `publish(doc_path: Path, base: str, env, git: GitRemote, gh: GhRunner) -> FixDocument` with

```python
class GitRemote(Protocol):
    def fetch(self, repo_dir: Path, branch: str) -> str        # remote tip sha
    def merge_base(self, repo_dir: Path, a: str, b: str) -> str
    def diff_names(self, repo_dir: Path, a: str, b: str) -> list[tuple[str, str]]   # (status, path)
    def push(self, repo_dir: Path, branch: str) -> None
```

Order (F §8.3): load; status must be `locally_confirmed`/`fix_proposed`/`ci_confirmed`; record `base` in `publication` and save **before** any network action; a stored different base → refused; `recheck_admission` (T10 — stored target, stored verdict and evidence, current trust; **not** the clone's `HEAD` or cleanliness); the worktree branch tip == stored fix commit; the **committed content** is re-checked with `git diff --name-status -z <head_sha> <fix_commit>` plus `link_problem` over `git show <head_sha>:<file>` / `git show <fix_commit>:<file>` (the commit, not the working tree); fetch base; `merge_base(base_tip, fix_commit) == head_sha` and `diff_names(merge_base, fix_commit)` equals the allowed change; push; look up an open PR whose head branch is the fix branch **and** whose `head.sha` equals the stored fix commit **and** whose `base.ref` equals the stored base — reuse it; a PR on that branch with another base or head → refused; create only if none. The resulting status: `locally_confirmed` → `fix_proposed`; `fix_proposed` and `ci_confirmed` are **kept** on a successful repeat. Refusals leave the status, set `last_operation`.

- [ ] Tests (faked `GitRemote` and `gh`, real Git for the tip/diff): happy path; the user's clone `HEAD` moved/dirty after `deployer fix` → publish still succeeds; the committed Dockerfile amended in the worktree (a new commit) → tip changed, refused; an existing PR with a different base or head sha → refused; a successful repeat on `ci_confirmed` keeps `ci_confirmed`; trust revoked; tip changed; stored diff tampered; a base already containing the fix commit (merge base ≠ head_sha) refused; a base missing head_sha refused; a timeout on create then repeat → the existing PR found, no second create; different `--base` on repeat refused; push failure on a `fix_proposed` repeat leaves `fix_proposed`; CLI exit table. Commit `feat(fix): fix publish`. **Open PR F1c.**

---

### Task 15: Derived run-1 case (data, owner review)

**Files:** Create `tests/fixtures/fix/copy-basename-unique/…`, `tests/fixtures/fix/copy-basename-ambiguous/…`, `tests/fixtures/fix/make_fix_bundle.py`, `tests/fixtures/fix/CHECKSUMS.sha256`, `tests/fix/test_fix_bundle_integrity.py`; Modify `pyproject.toml`/`tests/conftest.py` (exclude `tests/fixtures/fix/*/tree`).

Built from the run-1 **reproduction** bundle (`tests/fixtures/reproduction/run-1`; the A4 private key is gone by the owner's decision) by a generator that uses its **own new test key pair** (`--key PATH`, fresh ed25519 if omitted), writes its own `test-key.pub`, `trust/allowed_signers` and fingerprint into `PROVENANCE.md`, and never commits a private key. Cases, named for what they prove:

- `copy-basename-unique` — one other regular file named `setup.md` (`docs/guide/setup.md`) is added **before** issuing the set. The envelope does **not** make it the only eligible blob (every other regular file of the tree stays eligible); it makes it the only file passing the basename floor's check. The expected outcome is recorded as: envelope passed, the prompt lists all eligible blobs, and the proposal is the **model's** choice — the bundle carries the fake model answer used by the acceptance test, labelled as such.
- `copy-basename-ambiguous` — two files named `setup.md` (`docs/a/setup.md`, `docs/b/setup.md`) → `fix method not established: basename floor`, the model not called.

Tree, `tree-listing.json` and the signed set regenerated consistently; `PROVENANCE.md` separates the original failure record (run-1's logs, byte-identical) from the test modification. Integrity test as A4's (exact case set, checksums, tree = listing, provenance present, no private key).

- [ ] Build, scratch-replay through R → admission → `admitted` for both, commit, **open PR F1d for the owner** (no auto-merge). Task 13's run-1 P tests do **not** depend on this data: they build the one- and two-candidate variants at test time with `tests/admission/conftest.py`'s `AdmissionSet` (a generated key, a temporary tree). The committed bundles are the reviewed inputs for the stage-5 end-to-end acceptance.

---

### Task 16: Reading runs of any outcome

**Files:** Modify `src/deployer/forge.py`; Test `tests/test_forge_any.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class RunSummary: run_id: int; attempts: int; event: str; head_sha: str; path: str; head_branch: str | None
@dataclass(frozen=True)
class AttemptRead: run: RunSummary; attempt: int; status: str; conclusion: str | None
                   jobs: list[FailedJob] | None; logs_state: dict[int, LogsState]; error: str | None
def list_runs_for_sha(repo: str, sha: str, runner: GhRunner) -> list[RunSummary] | str   # str = incomplete listing
def read_attempt(repo: str, run: RunSummary, attempt: int, runner: GhRunner) -> AttemptRead
```

`list_runs_for_sha`: `actions/runs?head_sha=<sha>&per_page=100&page=n` with the same `total_count` completeness rule as `_Gh.jobs` (short, malformed or inconsistent → the reason string, never a shorter list). `read_attempt`: attempt metadata `actions/runs/{id}/attempts/{n}`, jobs of that attempt **without** the green filter, each job built with `_build_job` from its log (a job's `FailedJob.steps` keeps only non-green steps; `all_steps` keeps all — reuse as is), `logs_state` per job; a `GhError` with status → recorded in `error`. A status-less `GhError` (timeout, `gh` missing, unparseable) is **not** propagated out of the confirmation path: `list_runs_for_sha` returns it as its reason string and `read_attempt` records it in `error`, so `fix confirm` (T19) reports `ci_confirmation_insufficient` with that reason and exits 1 — never a traceback, never exit 2 (exit 2 stays for local `fix.json` I/O). `fetch_failed_run` unchanged.

- [ ] Tests with a fake runner: a status-less `GhError` on the listing → reason; on an attempt read → `error` set, no exception; complete listing; `total_count` 3 but 2 rows then an empty page → reason (Review Focus 5); malformed page → reason; a successful attempt read with all jobs; a 410 log → `logs_state` `unavailable`; `fetch_failed_run` tests unchanged. Commit `feat(forge): read runs of any outcome by head_sha`.

---

### Task 17: Conclusion-independent build binding and three-state qualification

**Files:** Modify `src/deployer/reproduce/shape.py` (add `bind_build_any`); Create `src/deployer/fix/qualify.py`; Test `tests/reproduce/test_shape_any.py`, `tests/fix/test_qualify.py`.

**Interfaces — Produces:**

```python
def job_key(workflow_text: str, job_name: str) -> str | Refusal      # R's name rule, extracted from check_workflow
def bind_build_any(workflow_text: str, job: FailedJob) -> Shape | Refusal
    # R's _bind + checkout rules, but the build step is the unique step whose run line parses as a build; conclusion ignored
Qualification = Literal["qualified", "excluded", "undetermined"]
@dataclass(frozen=True)
class Qualified: run_id: int; attempt: int; job_key: str | None; status: Qualification; reason: str | None; job: FailedJob | None; shape: Shape | None
def qualify(read: AttemptRead, fix_commit: str, original_path: str, original_key: str,
            original_build: BuildConfig, workflow_text: str) -> Qualified
    # original_path/original_key/original_build come from FixDocument.input (T3):
    # workflow_path, reproduction_binding["workflow_job"], build
```

`qualify` (F §7.2): excluded when proven from read data — event not push/workflow_dispatch, `head_sha` ≠ fix commit, path ≠ original, no job maps to the key (with the job listing complete), exactly one job maps but its build config differs, checkout SHA read and ≠ fix commit; undetermined when data is missing — `error` set, the mapped job's log `unavailable`/`error` (checkout and build binding need it), several jobs map (cannot decide), an incomplete attempt; qualified otherwise. `check_workflow` refactored to call `job_key` (behaviour unchanged, R tests guard).

- [ ] Tests: each excluded reason; each undetermined reason; qualified on a synthetic successful job; `bind_build_any` on a green build step and on a job that failed after the build; R's existing shape tests unchanged. Commit `feat(fix): three-state qualification of CI attempts`.

---

### Task 18: CI evidence, recurrence, evaluation

**Files:** Create `src/deployer/fix/ci_eval.py`; Test `tests/fix/test_ci_eval.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class AttemptEvidence: key: tuple[int, int, str]; qualification: Qualification; positive: bool; recurred: bool; detail: str | None; lines: tuple[int, ...]
def attempt_evidence(q: Qualified, cls: DefectClass, corrected_text: str, lines: tuple[int, int]) -> AttemptEvidence
    # key = (q.run_id, q.attempt, q.job_key or "")
def evaluate(evidence: list[AttemptEvidence], listing_complete: bool) -> tuple[Literal["ci_confirmed", "ci_confirmation_insufficient"], str | None]
```

Positive = `templates.match_ci(...)` → `passed` on the bound job's text (`shape.job_text`). Recurrence (F §7.3) = the admission template matchers (`admission.templates.match_copy_ci` / `match_from_ci`) of the admitted class returning a match whose span equals the corrected instruction's `lines`, whatever its object. `evaluate` (F §7.4): incomplete listing or any `undetermined` → insufficient `qualification undetermined`; positive and recurred → `contradictory runs`; recurred only → `defect recurred`; positive → `ci_confirmed`; none qualified → `no qualifying run`; otherwise the most specific reason (`templates not enabled` when every qualified attempt's outcome was `not_enabled`, `build step not reached`, `unknown format`, `binding ambiguous`).

- [ ] Tests: the full outcome table incl. a positive + an undetermined → insufficient, and a later independent failure with positive → confirmed. Commit `feat(fix): CI evidence and evaluation`.

---

### Task 19: `deployer fix confirm`

**Files:** Create `src/deployer/fix/confirm.py`; Modify `src/deployer/cli.py`; Test `tests/fix/test_confirm.py`.

**Interfaces — Produces:** `confirm(doc_path: Path, gh: GhRunner, clock: Callable[[], str]) -> FixDocument`.

Status must be `fix_proposed`/`ci_confirmed` (else exit 1, nothing read); list runs for the fix commit; read **every** completed attempt of every run (`1..run.attempts`); read the workflow at the fix commit from the worktree (`git show <fix>:<path>`); qualify; evidence; evaluate; append `CiAttempt` (time, considered `run_id`/attempt/job key, outcome, reason, evidence); status mirrors the attempt (`ci_confirmed` or `fix_proposed`); save atomically.

- [ ] Tests (faked `gh` bundles of synthetic runs): not published → exit 1; default (rows disabled) → insufficient `templates not enabled`; with the seam → `ci_confirmed`; then a re-check with a contradicting attempt → `fix_proposed`, the earlier positive kept in `ci_attempts`; unavailable log → `qualification undetermined`; incomplete listing; `pull_request` run excluded; CLI exit table. Commit `feat(fix): fix confirm`.

---

### Task 20: Docs and ledger for stages 1–2

**Files:** Modify `README.md`, `CLAUDE.md`, `TODO.md`.

README: the three commands and the statuses; that local confirmation and publication are not available yet: a correct input that passes every earlier check stops at the recording gate (`no local confirmation: templates not enabled`), while earlier refusals (no admission, fix method not established, no proposal, …) remain possible; CI confirmation is gated the same way (stages 3–4). CLAUDE.md: the `fix` package. TODO.md: `ci-fix-authoring` stays **open**; add the gated follow-ups with owner tags: L-recordings (owner permission), C-recordings (owner permission), end-to-end acceptance and closing. Commit `docs(fix): stages 1-2 documented; item stays open`. **Open PR F2.**

---

## Self-Review

- **Spec coverage:** F §1 → T3, T13, T14, T19; §1.1 → T13; §2 → T10; §3 → T11 (diff), T4 (link); §3.1 → T4; §4.1 → T6, T7; §4.2 → T5; §4.3 → T5 (grammar note); §5.1 → T11; §5.2 → T2, T13; §5.3 → T13; §6.1 → T12; §6.2 → T1, T8; §6.3/6.4 → T9, T12; §6.5 → T12, T3; §7.1 → T16; §7.2 → T17; §7.3 → T9, T18; §7.4/7.5 → T18, T19; §8.1 → T3; §8.2 → T13, T14, T19; §8.3 → T14; §8.4 → T13, T14, T19; §9 → T9 (seam + guard; recordings are stages 3–4); §10 → tests of every task, P via T13/T15, G via T11/T14; §11 stages 1, 1b, 2 → PRs F1a–F2.
- **Not in this plan (by design):** stages 3–5 (recordings, row enabling, end-to-end acceptance, closing the item) — each needs the owner's permission for real runs.
- **Placeholders:** Task 5's stage-name grammar is derived by the implementer from pinned sources; the plan fixes the method, the deliverable (the note with quoted lines) and the tests, not the regex — the regex is a fact to be read, not designed.
- **Type consistency:** `Bound` (T4) → T5, T6, T11, T12; `RecordRun`/`CheckRecord` (T1) → T8, T12; `FixDocument.input` (T3) → T10 `recheck_admission`, T12, T14, T17, T19; `PlannedSet`/`exclusion_proven`/`issue(edit_ignore=)` (T2) → T13; `FixDocument` (T3) → T13, T14, T19; `templates.match_local/match_ci` (T9) → T12, T18; `AttemptRead` (T16) → T17; `Qualified` (T17) → T18.
