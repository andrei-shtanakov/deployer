# CI fix authoring — design ("remove exactly the admitted defect")

**Status:** DRAFT rev 1. Designed with the owner on 2026-09-25 (six sections, each
approved with refinements). Next: a targeted consistency review, then the owner's
review, then a plan. No code exists for this design.
**Item:** `todo://deployer/ci-fix-authoring`.
**Base:** `master` @ `2417a9d` — the admission stack #84–#89. Cited specs:
`docs/superpowers/specs/2026-09-24-ci-failure-admission-design.md` as **A** and
`docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md` as **R**
(e.g. A §7, R §6).

## Purpose

Given a failed CI run whose admission is `admitted` — *"a defect of a deployer-authored
artifact is proven"* (A §1) — author a change that removes **exactly that defect** and
prove it in two separate stages:

1. **Locally**, before any publication: the diagnosed defect's check passes for the
   corrected instruction and a local build shows positive evidence of passing that
   place.
2. **In CI**, after publication: a real run on the exact fix commit carries positive
   evidence of passing the corrected place.

Three claims are kept apart and never merged:

- **Locally confirmed** — the check of the specific defect passes and the local build
  proves the corresponding place is passed.
- **Fix proposed** — a PR is published with the local evidence; CI confirmation is
  awaited.
- **Defect removal confirmed in CI** — a run on the exact fix commit, executing the
  required build, carries positive evidence of passing the corrected place. A later
  independent failure does not cancel it.

Successful L1/L2 of the artifact on its own is none of these. The absence of the old
finding is not evidence either: a run that failed earlier, skipped the build or printed
an unknown diagnostic format gives *confirmation insufficient*. An
`insufficient_grounds` admission of a later run is not evidence of a fix — admission
answers a different question.

`admitted` proves one defect. A fix of it does not promise that every failure of the
run is cleared (owner decision, 2026-09-25).

## Non-goals

- Defect classes beyond A's two. Supporting a class in admission does not oblige this
  design to fix every case of it: an explicit "no proposal" beats turning a proven
  defect into an unproven guess about the right content.
- Any file change beyond one instruction of the admitted Dockerfile plus its
  provenance set (§3).
- Merging. Triggering CI by a separate command. (Publishing a branch may itself start a
  configured workflow; that is an expected part of the process.)
- `pull_request` runs as CI evidence; merge or squash commits as evidence for the fix
  commit (§7.2).
- Proximity heuristics for choosing a replacement source (edit distance etc.).
- Live or paid runs as part of this design's acceptance of the code stages. Recordings
  (§9) each need the owner's separate permission.

## 1. Statuses and results

`status` is the **reached** state; `last_operation` is the result of the latest
command. They are stored separately (§8).

| `status` | Meaning |
|---|---|
| `stopped` (with `reason`) | No fix: see §1.1 |
| `locally_confirmed` | §6 passed, commit prepared in the fix worktree (§5) |
| `fix_proposed` | branch pushed, PR created or found (§8.3) |
| `ci_confirmed` | the current confirmation evaluation (§7) is positive |

Transitions: `locally_confirmed → fix_proposed` only by `fix publish`;
`fix_proposed ↔ ci_confirmed` only by `fix confirm`. A failed publication leaves
`locally_confirmed`. A confirmation attempt that finds a contradiction takes a current
`ci_confirmed` back to `fix_proposed`; the earlier positive evidence stays in the
history (§7.5). `ci_confirmation_insufficient` is a `last_operation` outcome, never a
`status`.

### 1.1 Stop reasons (top level)

- `no admission` — `accept_for_fix` did not return `Accepted`, or the clone does not
  match the target (§2).
- `fix method not established` — the COPY/ADD envelope or the model's answer did not
  single out one replacement (§4), with the concrete explanation (no eligible
  candidate; basename ambiguity; several plausible candidates; malformed answer).
- `no proposal` — FROM outside F1/F2 (§4.2).
- `no local confirmation` — §6 did not produce positive evidence, including "templates
  not enabled".
- `publication blocked` — a precondition of the commit failed (§5.3): issuing the new
  provenance set, an ignore-file edit it would need, the full-diff check.

## 2. The gate

Input: the 1.3 verdict document, its try dir, and a local clone of the project.

1. The clone is checked **as a whole**: a Git checkout at its repository root with an
   `origin`, and a **clean** working tree (staged, unstaged and untracked, as A's
   provenance preflight does). Equality of `HEAD` and of the Dockerfile bytes is not
   enough on its own.
2. The target is derived from the clone: `repo` from `origin`, `head_sha` = `HEAD`,
   `artifact_path` from the admission binding, `artifact_sha256` over the clone's
   Dockerfile bytes.
3. `accept_for_fix(document, try_dir, target)` (A §7). Anything but `Accepted` →
   `stopped: no admission`.

Fix authoring never re-derives admission and never works on a clone state other than
the admitted one.

## 3. Allowed change

Exactly:

- **one instruction** of the admitted Dockerfile (`binding.artifact_path`), and
- the **provenance set** under `.deployer/authoring/` re-issued for the corrected
  bytes (§5.2).

Every other project file is unchanged. The full diff of the prepared commit is checked
against this before the commit (§5.3).

### 3.1 The instruction link

The defect instruction is identified internally by its ordinal among the parsed
instructions, its exact original text and its span (from A's `defect`). After the edit:

- the instruction count is unchanged (no instruction added or removed);
- every other instruction is byte-identical;
- the byte diff lies within the bound instruction's span.

The stored link is `(file, original, replacement)`. The ordinal is an internal binding
only, **not** evidence from CI: line numbers may shift, and a matcher separately binds a
real diagnostic to the corrected instruction (§6.3, §7.3). Ambiguity there means no
positive evidence.

## 4. The proposal

### 4.1 `missing_copy_source` — the model proposes, a closed envelope admits

**Envelope** (deterministic; any failure → `stopped: fix method not established` with
the failing condition):

1. The bound instruction's keyword, flags, destination and every other source are kept
   byte for byte; exactly one token changes — the source A proved absent.
2. The new source is a **regular file** in R's `head_sha` listing (`100644` / `100755`).
   Excluded in this slice: the context root, directories, symlinks (`120000`),
   submodules (`160000`) and anything below a symlink or submodule ancestor. This is a
   boundary of the slice, not a claim that the absent source was a file: an absent path
   establishes no type.
3. The new source is in the **effective build context**: not excluded by R's effective
   ignore file on either side (CI and local), in the modelled form (no glob, `$`, `\`,
   space or `..`; R's normalisation), written in the notation of the original (e.g. no
   leading `./` when the original had none).
4. **Collisions**, both checked after R's normalisation:
   - (a) *duplicate source* — the new source equals another source of the instruction;
   - (b) *destination name conflict* — the instruction has two or more sources (so the
     destination is a directory) and the new source's basename equals another source's
     basename.

   For an instruction with several sources, **every** source must be an unambiguous
   regular file of the listing; if any other source is a directory, a glob or an
   unmodelled form, the absence of a destination conflict is not proven → stop.
5. **Basename floor:** more than one eligible blob with the absent source's basename →
   stop without calling the model.

Zero eligible candidates, the basename floor, and several plausible candidates all stop
with the single top-level reason `fix method not established` and a specific
explanation. The model is not called in a case the envelope already knows to be
ambiguous.

**The model's contract.** Input: the Dockerfile text, the bound instruction, the absent
source, the facts of the signed snapshot (those admission verified), and the eligible
blobs after conditions 2–4. Output, strict JSON:

- `source` — one path from that list, or `null`;
- `plausible` — the candidates the model judges plausible. With a non-null `source` it
  must contain **exactly** that path; an empty list, duplicates, paths outside the list
  or a mismatch with `source` → malformed. Several distinct eligible candidates →
  `fix method not established`;
- `rationale` — non-empty; each entry cites facts (a listing path or a `ProjectFacts`
  field) **and** explains how they bear on the choice. Citations are checked to exist;
  whether the inference holds is for review.

`null` → `fix method not established`. A malformed answer is refused; there is **one**
attempt, no retry with hints. `plausible` is the model's assessment: a single candidate
is not proven intent of the author, and even a substitution that passes every check
stays a proposal for review — replacing the source with any existing file may remove the
build error and still break the image's purpose. The model backend follows `author`'s
rule; tests always use a fake.

### 4.2 `from_argument_count` — a closed transformation list, no model

A FROM is edited only when the original instruction unambiguously contains a **complete
literal image reference**: the first argument after recognised flags is a syntactically
valid reference with an **explicit tag or digest**, so the trailing tokens cannot be part
of it by grammar. Substitutions in the reference (`$`, `${…}`) and unmodelled forms are
excluded. The reference, the recognised flags and an existing stage name are kept byte
for byte; only the listed tokens change.

| Id | Original arguments | Conditions | Transformation | Example |
|---|---|---|---|---|
| F1 | `<ref> <token>` | `<token>` matches the stage-name grammar fixed for the supported builder pair (§4.3), is not `AS` in any case, collides with no stage name, and **takes part in no reference or build-target selection** — no `--from=<token>`, no `FROM <token>`, no `--target`/build parameter naming it. If this cannot be checked unambiguously → no proposal. | insert `AS` before `<token>` | `FROM python:3.12-slim extra` → `FROM python:3.12-slim AS extra` (R's run-5) |
| F2 | `<ref> AS` (dangling, any case) | — | remove the dangling `AS` | `FROM python:3.12-slim AS` → `FROM python:3.12-slim` |

Everything else → `stopped: no proposal`, for example: a reference without tag or digest
(`FROM python 3.12`, `FROM python builder` — the token may be a lost tag); a token
outside the name grammar (`FROM python:3.12 -slim`); four or more arguments; three
arguments with a middle other than `AS`; unrecognised flags; a name collision; a name
referenced anywhere. A reference to the name elsewhere does **not** prove that adding the
stage restores the link the author meant: `COPY --from=<name>` may denote an external
image or a named context, and adding a stage changes what it resolves to.

The explanation is deterministic: the id, the original and new text, and the
conditions checked. For run-5 the PR says it is a syntactic correction, not evidence that
the author intended a stage named `extra`.

### 4.3 The stage-name grammar

Fixed for the supported pair (BuildKit, Podman/Buildah) in the plan from their
implementations, as the intersection both accept; a token either builder would reject
or treat differently → no proposal.

## 5. Preparation — worktree, provenance, commit

### 5.1 The fix worktree

All preparation happens in a **separate Git worktree** of the clone at `head_sha`. The
clone's `HEAD` and working tree are never switched or touched. The branch name is
derived from the class and `head_sha`.

### 5.2 The new provenance set

The corrected Dockerfile gets a **new signed set**; the old record is never left as a
confirmation of the new bytes.

- Its source is the **original `head_sha`**: the snapshot and facts are fixed before
  the edit is written (A's preflight in the worktree), and the signature covers the
  exact bytes of the corrected Dockerfile — A's `issue` semantics, reused.
- The signature proves provenance, not the correctness of the fix, and does not
  guarantee a future `admitted`.
- If issuing would need an ignore-file edit, a missing key, or any issuing failure →
  `stopped: publication blocked`.

### 5.3 Order

1. local proof (§6) in its own context;
2. write the corrected Dockerfile in the worktree; issue the new set;
3. check the **full** diff against §3 (one instruction + the provenance set, nothing
   else);
4. one commit containing both the corrected Dockerfile and the new set →
   `locally_confirmed`.

A failure after the commit keeps the branch and the commit; nothing is deleted
automatically. The result and the reason are stored (§8).

## 6. Local proof

### 6.1 Context

R's restored `source/` stays untouched. It is copied into a separate fix directory next
to the try dir and the corrected Dockerfile is written there; the build context is formed
from it as R forms it, with the same effective ignore files. The context holds the result
intended for the commit; the new provenance set is excluded from it. The build
configuration is the one R bound from CI (`-f`, context `.`, build args), run through R's
adapter and runtime.

### 6.2 Offline checks, per instruction and condition

- The defect's check passes **for the corrected instruction and its specific
  condition**: for `missing_copy_source`, `copy_sources` confirms the new source of that
  instruction present and not excluded — a general `copy_sources: passed` is not
  enough; for `from_argument_count`, `syntax_from_args` passes on it.
- **No regressions:** R's closed checks run on the original and the corrected
  Dockerfile and are compared per instruction and condition. Any change from `passed` to
  `failed`, `skipped`, `unknown` or `inconclusive` fails the rule.

### 6.3 Local positive evidence — closed, recording-backed

A closed table of local "passed" templates (Podman). Until real recordings back a row,
the row is a **hypothesis** and is disabled; a disabled row yields `no local confirmation:
templates not enabled`.

- **COPY/ADD (hypothesis):** the step line carrying the corrected instruction's exact text,
  exactly once. A `STEP` line only proves the step **started**. Passing it needs the next
  step of the **same stage and sequence**, or the build's completion, bound
  unambiguously; the stage boundary is not guessed from `k/n`. An error bound to the step
  itself → not confirmed.
- **FROM (hypothesis):** evidence that the Dockerfile was **parsed** — a build-stage
  step exists and no parse error — removes the diagnosed argument-count error. This rests
  on the builder parsing the whole instruction list before building stages: established
  for BuildKit (its instruction parse precedes stage construction), to be **verified**
  for Podman against its implementation and by recordings that include a bad FROM in a
  later stage. Loading the Dockerfile, the context or a frontend is not such evidence.
  Parsing and pulling the image are distinct: a later image-pull failure of the same FROM
  does not refute the parse result, is recorded explicitly, and is not called "a failure
  of another instruction".
- If the builder drops `AS` or normalises the reference so that the closed template
  cannot bind the line to the instruction unambiguously → `binding ambiguous`. No support
  for such forms is promised before recordings; the matcher is not widened by
  assumption.

### 6.4 Later failures

A build failure after the corrected place is allowed only when the boundary is
**proven**: the positive evidence of §6.3 was already obtained. If that cannot be
established → no local confirmation. A timeout never confirms. The PR states the narrow
claim: "the diagnosed source error is removed locally" — never "the substitution builds"
when the build passed the corrected step and failed later.

### 6.5 Evidence

The configuration is fixed in the evidence: the exact bytes (SHA-256) of the corrected
Dockerfile, the build parameters, the runtime and its versions. `build.stdout` /
`build.stderr` of the fix directory are referenced with the line rules of A (UTF-8,
split on `\n`).

## 7. CI confirmation

### 7.1 Reading runs of any outcome

A new reading function in `forge` reads a run of **any** conclusion: its metadata
(event, `head_sha`, workflow path, attempts), jobs with their steps and conclusions, and
the full log of the bound job, through the same GitHub API R uses, **without any
rebuild**. The failed-run reading is unchanged. The existing reading and admission
pipeline is **not** assumed to handle successful jobs: this is a separate requirement
with its own tests.

### 7.2 Which runs qualify

Runs are listed by `head_sha` = the **exact fix commit**. A run qualifies only when:

- its event is `push` or `workflow_dispatch` (`pull_request` excluded in this slice);
- its workflow path equals the one R bound in the original run, and the workflow bytes at
  the fix commit equal those at `head_sha` (follows from §3);
- a job with the same **workflow job key** (not the numeric GitHub job id, which belongs
  to one run) exists;
- R's `shape` applied to that job binds a build line with the original configuration
  (`-f`, context, build args);
- the checkout was at the fix commit, by R's own rules.

A merge or squash commit does not confirm the fix commit: it is a different object.

### 7.3 CI positive evidence — closed, recording-backed (hypotheses until recorded)

- **COPY/ADD (BuildKit):** exactly one step header whose instruction text is the
  corrected instruction, and `#k DONE` for the same `k`. `#k CACHED` is **not** accepted
  automatically. A missing or repeated `k` → `binding ambiguous`.
- **FROM (BuildKit):** the whole file parsed — a build-stage step exists and no
  `dockerfile parse error`; the evidence must bind unambiguously to the required build and
  the exact corrected bytes; frontend, context or definition loading steps are not
  evidence. The image-pull result of that FROM is recorded when visible.

Until recordings back a row it is disabled → `templates not enabled`.

### 7.4 Evaluating attempts

All qualifying **completed attempts** available at the time of the check are
considered — not only the latest of each run, since a re-run may hide an earlier
positive or negative result.

- Positive evidence present, no recurrence of the diagnosed defect on the same
  instruction → `ci_confirmed`.
- Both present → `ci_confirmation_insufficient: contradictory runs`.
- Network failures, skips or an unreached step are not contradictions, but confirm
  nothing on their own.
- An incomplete API listing or an unavailable log of a qualifying attempt means the
  absence of contradictions cannot be claimed → insufficient.

A later independent failure in the same run does not cancel proven passage of the
corrected place. Ambiguous binding → no positive evidence.

### 7.5 The result of a confirmation attempt

Each attempt records the check time and the exact list of `run_id` / attempt / job key
considered, the outcome, the reason and the evidence (run, attempt, job, log lines). A
later check may change the current conclusion; earlier evidence is kept. Reasons for
`ci_confirmation_insufficient` include: no qualifying run; CI failed before the build;
build step not reached; unknown format; binding ambiguous; templates not enabled;
contradictory runs; incomplete listing or unavailable log.

## 8. The fix document and the CLI

### 8.1 `fix.json` (schema 1.0)

Stored in the fix directory next to R's try dir — dev-side evidence, never committed to
the project. Fields:

- `input` — SHA-256 of the verdict document, its `binding`, the clone state (origin,
  `HEAD`, clean-tree result);
- `proposal` — `class`, `file`, `transformation` (`copy-source` | `F1` | `F2`),
  `original`, `replacement`, internal `ordinal`, `rationale` (model or deterministic),
  `envelope` (each condition and its result);
- `local_proof` — configuration, checks before/after per instruction and condition,
  evidence files and lines, `later_failure` (separately: image pull of the same FROM /
  another instruction);
- `publication` — worktree, branch, fix commit, the full-diff check, PR URL;
- `ci_attempts[]` — append-only (§7.5);
- `status`, `last_operation`.

Writability is checked before any operation; the document is saved **atomically at
checkpoints** (after the proposal, after the local proof, after the commit, after
publication, after each confirmation attempt).

### 8.2 Commands

- `deployer fix <verdict.json> --clone <path> [--signing-key …]` — gate, proposal, local
  proof, new provenance set, full-diff check, commit in the fix worktree. Exit `0` after
  local confirmation and the prepared commit; `1` when stopped; `2` invalid invocation or
  a local I/O failure (including reading/saving `fix.json`).
- `deployer fix publish <fix.json>` — the explicit permission to push and create the
  PR; no interactive confirmation inside. Exit `0` after a confirmed push and a created or
  found PR.
- `deployer fix confirm <fix.json>` — one confirmation attempt. Exit `0` when the current
  status is `ci_confirmed`; `1` when insufficient (including an unavailable CI log or an
  incomplete API listing); `2` for a local I/O failure.

Exit codes of existing commands are unchanged.

### 8.3 Publishing safely and repeatably

Before pushing, `fix publish`:

- re-checks admission against the original state, **including the current trust set**
  (a revoked key blocks publication) — a ready branch does not preserve the permission to
  publish;
- verifies the stored commit, its full diff and the evidence;
- refuses when the recorded `HEAD`/branch has changed;
- reuses an existing branch and PR: after a network timeout it neither publishes a
  changed commit nor creates a duplicate PR.

A push or PR-creation failure leaves `locally_confirmed` and the local evidence intact.

### 8.4 Errors

Every step is total: an exception becomes a reason in the document, never a traceback.
After operations already performed, a later failure exits `2` with a short message and
the identifiers of what was already created (worktree, branch, commit, PR).

## 9. Recordings — the gates

Each class of recording needs the owner's **separate** permission for real runs.

- **L-recordings (local Podman):** real builds of corrected Dockerfiles for run-1 and
  run-5, and the cases: a bad FROM in a later stage; several stages on the same image; a
  later failure after the corrected COPY.
- **C-recordings (CI):** real `push` runs of fix commits on the polygon repository:
  successful BuildKit forms (COPY header + `DONE`, `CACHED`, the FROM stage header), a run
  with a later independent failure, a re-run.

A template row is enabled **only together with** the test that checks it against its
real recording. If real logs require a matcher change, that code goes through the
ordinary review; the data and expected results are the owner's separate review. Without
L-recordings no `locally_confirmed` and no publication; without C-recordings a published
proposal is never CI-confirmed.

## 10. Acceptance — offline

- **D (decision):** pure parts with hand-built inputs, one targeted mutation per case —
  every §4.1 condition incl. collisions (a)/(b) and a multi-source instruction with a
  non-file source; F1/F2 and each no-proposal branch; the model-answer checks (malformed,
  `plausible` ≠ `source`, several candidates); the regression rule per instruction and
  condition; matchers on synthetic lines (negatives only until recordings).
- **P (pipeline):** from the committed A4 bundles `admit-run-1` / `admit-run-5`, with
  Git, runtime, model and GitHub faked, a real worktree and `fix.json`:
  - run-5: F1 → `FROM python:3.12-slim AS extra`;
  - run-1: a **derived** case whose tree holds exactly one eligible file for the absent
    `docs/setup.md` — tree, listings and signed data updated consistently, its
    `PROVENANCE.md` separating the original failure record from the test modification
    (owner review of the data); and a case with two same-basename candidates →
    `fix method not established`.

  Before recordings, P tests end at the expected refusals `no local confirmation:
  templates not enabled` and `templates not enabled` — asserted outcomes, not skips.
- **G (real local Git, offline — no model, no container builds):** the worktree leaves
  the user's checkout untouched; the commit contains exactly the Dockerfile instruction
  and the provenance set; a changed `HEAD` is refused.
- **Boundaries:** trust revoked before `publish`; a repeat after a network timeout; no
  duplicate PR; a document-save failure after the commit was created; positive and
  contradicting attempts; an incomplete API listing; an unavailable log; the transition
  `ci_confirmed → fix_proposed`.

## 11. Delivery

Stages; code is split into PRs by the review kit's limits, data stays separate:

1. Fix authoring, local proof with templates disabled, worktree, `fix.json`,
   `fix publish`.
2. Reading runs of any outcome and all attempts; `fix confirm` with templates disabled.
3. L-recordings, local template rows, their acceptance tests (data: owner review).
4. C-recordings, CI template rows, their acceptance tests (data: owner review).
5. End-to-end acceptance, documentation, and closing `todo://deployer/ci-fix-authoring`.

The item stays open until stage 5; stages 1–2 may be accepted as implementation
without claiming the agreed scope complete.

## 12. Decisions (owner, 2026-09-25)

1. Proof level: local proof before the PR **and** CI confirmation by a real run on the
   fixed commit; three claims kept apart; missing old finding ≠ fix.
2. Authoring: the model proposes, a deterministic closed envelope admits; one
   instruction of the admitted Dockerfile.
3. COPY: only the proven-absent source changes; regular files only in this slice;
   basename floor, no proximity threshold; `plausible` is the model's assessment.
4. FROM: closed list F1/F2 with a complete literal reference; no model; F1 only when the
   name takes part in no reference or target selection.
5. Local "passed" templates are recording-backed; the fixed Dockerfile gets a new
   signed set from the original `head_sha`.
6. CI: exact fix commit, `push`/`workflow_dispatch`, all completed attempts; at least one
   positive and no contradiction; `CACHED` not automatic.
7. `fix publish` is a separate command; worktree preparation; statuses and last
   operation stored apart.
8. Delivery order as §11; recordings each need separate permission.
