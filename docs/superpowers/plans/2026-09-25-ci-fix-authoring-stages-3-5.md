# ci-fix-authoring stages 3–5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Back the four template rows with real recordings, enable them, and run
`ci-fix-authoring` end to end on the polygon: a real failed CI run goes through
`diagnose --reproduce`, `fix`, `fix publish` and `fix confirm` and ends at `ci_confirmed`.
Then close `todo://deployer/ci-fix-authoring`.

**Architecture:** Recordings are data. They are committed under
`tests/fixtures/fix/recordings/`, each with a recorder script, `PROVENANCE.md` and
checksums, and reviewed and merged by the owner only. Enabling a row is code: it sets
`Row.recording` to a committed case, adds the test that replays that case, and changes a
matcher only if a recording refutes it. The end-to-end run uses real Podman, the real
GitHub polygon (a `workflow_dispatch`-only workflow on experiment branches) and one real
model call per COPY case. Its evidence is committed as data.

**Tech Stack:** Python 3.12, uv, pytest; Podman 5.7.0 (Buildah 1.42.0) locally; GitHub
Actions `ubuntu-24.04` with Docker/BuildKit; the `gh` CLI; Anthropic API (one call per
COPY e2e case).

**Spec:** `docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md` — §6.3, §6.4,
§7.3, §9, §10, §11 stages 3–5. The polygon rules come from
`docs/superpowers/specs/2026-09-21-ci-failure-diagnosis-design.md` §6.

## Global Constraints

- **Owner permission.** The owner permitted real runs on 2026-09-25: local Podman
  recordings, CI recordings on the polygon, and the end-to-end run with its model calls.
  Paid model calls: exactly one per COPY e2e case, no retries.
- **Data review.** Data PRs are reviewed and merged by the owner only; the agent never
  merges one. Code PRs go through the review kit and are agent-merged per `CLAUDE.md`.
- **PR size.** Review-kit limits are at most 30 files and 400 KB per PR. Data PRs are
  exempt from the kit because the owner reviews them.
- **Keys.** No private key is committed or kept. Each generator uses a fresh key pair,
  commits only the public half and the fingerprints, and deletes the private half from
  scratch when its task ends. Regenerating with a new key goes through a new data review.
- **Polygon (diagnosis spec §6).**
  - The polygon workflow `.github/workflows/diagnosis-polygon.yml` stays
    `workflow_dispatch`-only. Failure scenarios live only on experiment branches
    `polygon/…`.
  - Never push to `master`.
  - Before **every** dispatch, check that the experiment SHA is not the head of any open
    PR other than the case's own fix PR.
  - Fix PRs opened by `fix publish` target a polygon base branch. They are **never
    merged**: close them after evidence capture.
- **Recordings are verbatim.** Record the exact bytes a builder or GitHub produced. Edits,
  trimming and redaction are forbidden; if something must be redacted, stop and ask the
  owner.
- **Row enabling.**
  - A production row is enabled only together with the test that replays its recording
    (§9).
  - A recording that refutes a hypothesis leaves its row disabled until a matcher change
    passes code review. The change is never widened by assumption (§6.3).
  - If the later-stage bad-FROM recording (L4) shows Podman building a stage before it
    parses a later FROM, the local FROM row stays disabled. Report that to the owner.
- **Status.** `ci-fix-authoring` stays open until Task 27 merges.

## Review Focus

1. **Podman's parse order for a bad FROM in a later stage.** If stage 1 executes before
   the parse error, "a STEP exists" is not proof of a whole-file parse. Recording L4
   decides this; Task 23 has a test that fails if L4 shows a `STEP` line before the
   error.
2. **The same instruction text in two stages.** A repeated `STEP k/n: <text>` must stay
   `binding_ambiguous`. That is L5, tested in Task 23.
3. **BuildKit log decoration in GitHub job logs.** Timestamps, `##[group]` blocks and
   ANSI codes must not break header binding. The C1 replay test goes through forge's real
   log-to-job path, not raw text (Task 25).
4. **A re-run attempt.** The attempt counter goes up and an old attempt still exists.
   Evaluation must consider both (C2, Task 25).
5. **The fix PR's `pull_request` runs on the same SHA.** They must be `excluded`, never
   `undetermined`. Otherwise the e2e case can never confirm (Task 26, e2e assertion).

---

## Stage 3 — local

### Task 21: L-recordings (data, owner PR F3-data)

**Files:**
- Create: `tests/fixtures/fix/recordings/record_local.py`, the recorder. It runs R's own
  `reproduce.build.run_build`, so the argv is identical to production's
  (`podman build --file … --tag … --force-rm <ctx>`).
- Create: `tests/fixtures/fix/recordings/local/<case>/`, one directory per case, holding:
  - `tree/`: the build context;
  - `corrected.txt`: the corrected instruction's text;
  - `kind`: `copy` or `from`;
  - `build.stdout`, `build.stderr`, `build.exit`;
  - `argv.json`;
  - `environment.json`: `podman version`, `podman info` BuildahVersion, OS/arch, UTC time;
  - `expected.json`: the matcher outcome the owner reviews.
- Create: `tests/fixtures/fix/recordings/PROVENANCE.md` and
  `tests/fixtures/fix/recordings/CHECKSUMS.sha256`.

**Cases.** Each tree derives from the run-1/run-5 trees; the derivation is written in
PROVENANCE.

| Case | Dockerfile | Expected (hypothesis to confirm or refute) |
|---|---|---|
| `l1-copy-cold` | run-1 derived tree with `COPY docs/guide/setup.md ./setup.md`. The file text has a per-recording nonce so the layer cache misses. | `copy` → `passed`, the step line then the next `STEP` |
| `l2-copy-warm` | same tree, built again right after L1 | `copy` → `passed` through `--> Using cache` |
| `l3-from-run5` | run-5 tree with `FROM python:3.12-slim AS extra` | `from` → `passed` |
| `l4-from-bad-later` | `FROM python:3.12-slim AS a` / `RUN true` / `FROM python:3.12-slim extra` | `from` on the bad text → `not_confirmed`, with **no `STEP` line** before the parse error |
| `l5-stages-same-image` | two stages `FROM python:3.12-slim AS a` / `AS b`, each with the identical corrected COPY | `copy` → `binding_ambiguous` ("step line repeated"); `from` → `passed` |
| `l6-copy-later-failure` | the corrected COPY followed by `RUN false` | `copy` → `passed`, exit ≠ 0; §6.4's later failure is visible |

- [ ] **Step 1: Write the recorder.** It takes `--case` / `--all` and refuses to
  overwrite an existing case directory: re-recording means deleting the directory and
  going through a new review. It removes its own tag with `cleanup_image`. It never runs
  `podman system prune` or touches images it did not tag.
- [ ] **Step 2: Record every case.** Run `uv run python tests/fixtures/fix/recordings/record_local.py --all`.
- [ ] **Step 3: Replay each recording.** In scratch, run `templates.match_local` over each
  recording with the rows enabled through `tests.fix.conftest.enable_for_test`. Write the
  **observed** outcome into `expected.json`. When it differs from the hypothesis in the
  table above, record the difference as an owner question in the PR, not as a fix.
- [ ] **Step 4: Write PROVENANCE.md and the checksums.** PROVENANCE lists the real runs
  and versions, and states that the outputs are verbatim and the trees derived.
- [ ] **Step 5: Commit and open the PR.** Commit
  `test(fix): L-recordings — real Podman builds for the local rows (data)` on
  `feat/fix-6-l-recordings`. Open the PR, marked "data, owner review". The body gives the
  case → observed → hypothesis table and the questions.

### Task 22: Buildah FROM parse-path note (code PR F3b)

**Files:**
- Create: `docs/fix-buildah-from-parse.md`.
- Modify: `docs/fix-stage-name-grammar.md`, only to cross-link the new note.

- [ ] **Step 1: Read the pinned source.** Pin Buildah **v1.42.0**, the version the
  recordings ran; `docs/fix-stage-name-grammar.md` pins v1.45.1 for the grammar. Read the
  path `imagebuildah.BuildDockerfiles` → `imagebuilder.ParseDockerfile` →
  `imagebuilder.NewStages` → stage execution. Quote the lines, with repo, tag, commit and
  path:line, that show every instruction is parsed before any stage executes.
- [ ] **Step 2: Cross-check.** State in the note that L4 is the empirical cross-check. If
  source and recording disagree, the local FROM row stays disabled, and the note says so.
- [ ] **Step 3: Commit.** `docs(fix): Buildah parses the whole Dockerfile before stages (pinned v1.42.0)`.

### Task 23: Enable the local rows (code PR F3b, after F3-data merges)

**Files:**
- Modify: `src/deployer/fix/templates.py`. Set `ROWS` `recording=` for `copy-passed/podman`
  and `from-parsed/podman` to `"tests/fixtures/fix/recordings/local"`. Change a matcher
  only if a recording refuted it.
- Create: `tests/fix/test_local_recordings.py`.
- Modify: `tests/fix/test_templates.py`, the `test_no_row_enabled_without_recording`
  test. It now asserts that each enabled production row's `recording` is an existing
  directory whose cases replay.
- Modify: `README.md` and `TODO.md`. Local confirmation becomes available; the
  L-recordings item moves to Shipped.

**Interfaces:**
- Consumes: `templates.match_local(kind, corrected_text, stdout, stderr) -> Outcome`.
- Consumes: the recording layout from Task 21.

- [ ] **Step 1: Write the failing replay test.**

```python
CASES = sorted(p.name for p in LOCAL.iterdir() if (p / "expected.json").is_file())

@pytest.mark.parametrize("case", CASES)
def test_local_recording_replays(case: str) -> None:
    d = LOCAL / case
    exp = json.loads((d / "expected.json").read_text())
    out = templates.match_local(
        (d / "kind").read_text().strip(),
        (d / "corrected.txt").read_text().rstrip("\n"),
        (d / "build.stdout").read_text(),
        (d / "build.stderr").read_text(),
    )
    assert (out.evidence, list(out.lines), out.detail) == (
        exp["evidence"], exp["lines"], exp["detail"])
```

  Also add `test_l4_no_step_before_parse_error`, which asserts that `build.stdout` of
  `l4-from-bad-later` has no `STEP` line (Review Focus 1).
- [ ] **Step 2: Run it before enabling.**
  `uv run pytest tests/fix/test_local_recordings.py -v`. Expected: FAIL, because the rows
  are disabled and the outcome is `not_enabled`.
- [ ] **Step 3: Set `recording=` on the two local rows.** Make a matcher change only if a
  case failed for a reason the owner accepted in the F3-data review.
- [ ] **Step 4: Run the suite.** `uv run pytest -q`, `uv run ruff check .`,
  `uv run pyrefly check`. The pipeline tests that asserted `templates not enabled` for the
  local side now reach `locally_confirmed` without the seam. Update exactly those
  assertions, and make sure each one is intended.
- [ ] **Step 5: Commit.** `feat(fix): enable the local rows on L-recordings`. Then PR F3b
  goes through the review kit and the agent merge.

## Stage 4 — CI

### Task 24: C-recordings on the polygon (data, owner PR F4-data)

**Files:**
- Create: `tests/fixtures/fix/recordings/record_ci.py`. For each case it:
  1. creates branch `polygon/fix-c-<case>` from `master`, with the case tree plus a
     replaced `diagnosis-polygon.yml`. The workflow stays `workflow_dispatch`-only, and
     its job is `actions/checkout` followed by one `docker build --file Dockerfile --tag
     ci-build .`, a line that R's `parse_build_line` accepts;
  2. pushes the branch, then runs the open-PR SHA check;
  3. runs `gh workflow run diagnosis-polygon.yml --ref polygon/fix-c-<case>`;
  4. waits for the run to complete;
  5. stores what `forge.list_runs_for_sha` and `forge.read_attempt` return, through the
     real `SubprocessGh`.
- Create: `tests/fixtures/fix/recordings/ci/<case>/`, holding:
  - `tree/` and `workflow.yml`;
  - `corrected.txt` and `kind`;
  - `runs.json`, the listing as read;
  - `attempt-<n>.json`, the `AttemptRead`, jobs and logs as read;
  - `environment.json`: run URL, runner image, date;
  - `expected.json`.

**Cases.**

| Case | Content | Expected (hypothesis) |
|---|---|---|
| `c1-copy-done` | corrected COPY, cold runner | `#k [..] COPY …` then `#k DONE` → `passed` |
| `c2-copy-rerun` | `gh run rerun` of C1 | attempt 2 recorded; both attempts evaluated; outcome per its log |
| `c3-from-parsed` | `FROM python:3.12-slim AS extra` | `from` → `passed` |
| `c4-copy-later-failure` | corrected COPY, then `RUN false` | COPY `passed`; `ci_confirmed` with a later failure (narrow claim, §7.4) |
| `c5-copy-recurred` | COPY of a still-missing path at the corrected span | admission matcher recurrence → `defect recurred` |
| `c6-copy-cached` | a job with the same build twice, to capture the `#k CACHED` form | `binding_ambiguous` (several builds) — records the CACHED form only |

- [ ] **Step 1: Write the recorder.** It must refuse to dispatch when the SHA check fails,
  and must never touch `master` or a non-`polygon/` branch.
- [ ] **Step 2: Record each case.** Run `uv run python tests/fixtures/fix/recordings/record_ci.py --all`.
  Each case is one dispatch; C2 adds one re-run. Keep the `polygon/fix-c-*` branches as
  evidence, as with `polygon/run-*`.
- [ ] **Step 3: Replay each case.** In scratch, replay through `qualify` →
  `attempt_evidence` → `evaluate`, with the CI rows enabled through the seam, and write
  the observed outcomes into `expected.json`. List every difference from the table as an
  owner question.
- [ ] **Step 4: Write PROVENANCE, the checksums and the integrity test.** Commit
  `test(fix): C-recordings — real polygon runs for the CI rows (data)` on
  `feat/fix-7-c-recordings`, and open the PR (owner review).

### Task 25: Enable the CI rows (code PR F4b, after F4-data merges)

**Files:**
- Modify: `src/deployer/fix/templates.py`. Set `recording=` for `copy-passed/buildkit` and
  `from-parsed/buildkit` to `"tests/fixtures/fix/recordings/ci"`.
- Create: `tests/fix/test_ci_recordings.py`. It replays every CI case through a faked
  `GhRunner` that serves `runs.json` and `attempt-<n>.json` byte for byte, and runs
  `fix.confirm.confirm` on a `fix_proposed` document built for that case.
- Modify: `tests/fix/test_templates.py`, the same way as Task 23. Also modify `README.md`
  and `TODO.md`.

- [ ] **Step 1: Write the failing test.** Each case's `confirm` outcome and reason must
  equal its `expected.json`. For C2, both attempts must appear in `considered`.
- [ ] **Step 2: Run it.** Expected: FAIL (`templates not enabled`).
- [ ] **Step 3: Enable the two CI rows.** Make a matcher change only where the owner
  accepted a refutation.
- [ ] **Step 4: Run the full suite, ruff and pyrefly.** Then commit
  `feat(fix): enable the CI rows on C-recordings` and open PR F4b.

## Stage 5 — end-to-end

### Task 26: End-to-end run on the polygon (evidence data, owner PR F5-data)

Run this as the controller, not as a subagent: every step is a real, outward-facing
action.

**Cases:** `e2e-copy` (run-1-like, needs one model call) and `e2e-from` (run-5-like, no
model).

- [ ] **Step 1: Prepare the key and trust.** Generate a fresh ed25519 key in scratch. Run
  `uv run deployer trust add <key.pub>` for the polygon repo.
- [ ] **Step 2: Create the base branch.** Make `polygon/fix-e2e-<case>-base` from
  `master` with:
  - the case tree;
  - the failing Dockerfile;
  - a signed authoring set issued with that key through `provenance.issue`;
  - the polygon workflow with one `docker build` line.

  Push it, check the SHA, and dispatch. The run must fail at the build.
- [ ] **Step 3: Diagnose.** Run
  `uv run deployer diagnose <run-url> --reproduce --container-tool podman` → verdict
  with `admission.verdict == "admitted"`.
- [ ] **Step 4: Author the fix.** Clone the repo to scratch at the base head. Run
  `uv run deployer fix <verdict.json> --clone <clone> --signing-key <key>` →
  `locally_confirmed`. The COPY case makes exactly one real model call.
- [ ] **Step 5: Publish.** Run
  `uv run deployer fix publish <fix.json> --base polygon/fix-e2e-<case>-base` →
  `fix_proposed`; the fix branch is pushed and the PR opened.
- [ ] **Step 6: Dispatch on the fix commit.** Check the SHA: its only open PR must be the
  case's own fix PR. Then run
  `gh workflow run diagnosis-polygon.yml --ref <fix branch>`.
- [ ] **Step 7: Confirm.** Run `uv run deployer fix confirm <fix.json>` → `ci_confirmed`.
  The fix PR's `pull_request` runs on the same SHA must be listed as `excluded` (Review
  Focus 5).
- [ ] **Step 8: Clean up and capture evidence.**
  - Close the fix PR without merging, and delete the private key.
  - Revoke the key with `deployer trust revoke`.
  - Commit to `tests/fixtures/fix/e2e/<case>/`: the verdict, `fix.json`, the build logs,
    the run URLs, and a `PROVENANCE.md` that states the model was real, gives its model
    id and counts the calls.
  - Add an integrity test.
  - Commit `test(fix): end-to-end evidence — real polygon runs to ci_confirmed (data)` and
    open the PR (owner review).

### Task 27: Close the item (code/docs PR F5b, after F5-data merges)

**Files:** Modify `README.md` (the "not available yet" paragraph goes), `TODO.md` (move
`ci-fix-authoring` and the e2e item to Shipped) and `CLAUDE.md`.

- [ ] **Step 1: Update the docs** to the shipped state, using the exact outcomes of Task
  28.
- [ ] **Step 2: Commit.** `docs(fix): ci-fix-authoring complete — closes the item`, then
  open PR F5b.

## PR map

| PR | Tasks | Kind | Merge |
|---|---|---|---|
| F3-data | 21 | data | owner |
| F3b | 22, 23 | code | agent after the review kit, and only after F3-data merges |
| F4-data | 24 | data | owner |
| F4b | 25 | code | agent after the review kit, and only after F4-data merges |
| F5-data | 26 | data | owner |
| F5b | 27 | docs | agent after the review kit, and only after F5-data merges |

Stage 4 may record (Task 24) while F3 is in review; the recordings don't depend on the
local rows. The e2e run (Task 26) needs F3b and F4b merged.
