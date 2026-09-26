# CI fix authoring — design ("remove exactly the admitted defect")

**Status:** DRAFT rev 2.1. Designed with the owner on 2026-09-25 (six sections, each
approved with refinements); revised after the targeted consistency review of rev 1 at
`648603d` (`../../../../_cowork_output/deployer-fix-authoring-spec-review-2026-09-25.md`,
a dev-only workspace file; 7 must-fix, 19 should-fix, 7 minor — every point resolved),
then after the owner's review of rev 2 at `6c30885` (four mechanical gaps and two
editorial contradictions, resolved in rev 2.1; the two choices of rev 2 are confirmed).
Next: the owner checks the rev 2.1 diff, then a plan. No code exists for this design.
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
2. **In CI**, after publication: real runs on the exact fix commit carry positive
   evidence of passing the corrected place, and no qualifying attempt contradicts it.

Three claims are kept apart and never merged:

- **Locally confirmed** — the check of the specific defect passes and the local build
  proves the corresponding place is passed.
- **Fix proposed** — a PR is published with the local evidence; CI confirmation is
  awaited.
- **Defect removal confirmed in CI** — among the qualifying attempts on the exact fix
  commit, at least one carries positive evidence of passing the corrected place and none
  shows the diagnosed defect again (§7.4). A later independent failure does not cancel
  it.

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
- Live or paid runs as part of the acceptance of the code stages. Recordings (§9) each
  need the owner's separate permission.
- A Docker local backend for the proof in this slice (§6.1).

## 1. Statuses and results

`status` is the **reached** state; `last_operation` is the outcome of the latest command
(its name, time, result and reason). They are stored separately (§8.1).

| `status` | Meaning |
|---|---|
| `in_progress` | `deployer fix` started; a checkpoint was saved, no final state yet |
| `stopped` (with `reason`) | No fix: see §1.1 |
| `locally_confirmed` | §6 passed and the commit was created in the fix worktree (§5) |
| `fix_proposed` | branch pushed, PR created or found (§8.3) |
| `ci_confirmed` | the **latest** confirmation attempt (§7.4) was positive |

Transitions:

- `in_progress → stopped | locally_confirmed` — only by `deployer fix`. A document left in
  `in_progress` (a crash) is never resumed: a new `deployer fix` starts a new fix
  directory (§5.1); the old one stays as evidence.
- `locally_confirmed → fix_proposed` — only by `fix publish`. Any refusal of
  `fix publish` (§8.3) leaves the status unchanged and is recorded in `last_operation`.
- `fix_proposed ↔ ci_confirmed` — only by `fix confirm`. The status mirrors
  the latest attempt: a positive attempt sets `ci_confirmed`; any other outcome
  (insufficient, contradictory, defect recurred) sets `fix_proposed`. Earlier positive
  evidence stays in `ci_attempts[]` (§7.5); a later check may change the current
  conclusion but never erases evidence.
- `fix publish` on `fix_proposed`/`ci_confirmed` re-verifies and returns the existing
  branch and PR (idempotent, §8.3). `fix confirm` on `in_progress`, `stopped` or
  `locally_confirmed` refuses (nothing is published).

`ci_confirmation_insufficient` is a `last_operation` outcome, never a `status`.

### 1.1 Stop reasons (top level, `deployer fix` only)

- `no admission` — the clone does not match the target, `accept_for_fix` did not return
  `Accepted`, or the current trust set does not confirm ownership (§2).
- `fix method not established` — the instruction could not be bound (§3.1), or the
  COPY/ADD envelope or the model's answer did not single out one replacement (§4.1),
  with the concrete explanation.
- `no proposal` — FROM outside F1/F2 (§4.2).
- `no local confirmation` — §6 did not produce positive evidence, including
  "templates not enabled" and "local backend differs from R's".
- `commit blocked` — a precondition of the commit failed (§5.2–§5.3): exclusion that
  would need an ignore-file edit, no signing key, an issuing failure, the full-diff
  check.

## 2. The gate

Input: the 1.3 verdict document, its try dir, and a local clone of the project.

1. The clone is checked **as a whole**: a Git checkout at its repository root with an
   `origin`, `HEAD` = the admission `binding.head_sha`, and a **clean** working tree
   (staged, unstaged and untracked, as A's provenance preflight does). Equality of `HEAD`
   and of the Dockerfile bytes is not enough on its own.
2. The fix directory (§5.1) and the worktree must lie **outside** the clone; otherwise
   exit `2` (invalid invocation) — a worktree inside the clone would make it dirty.
3. The target is derived from the clone: `repo` from `origin`, `head_sha` = `HEAD`,
   `artifact_path` from the admission binding, `artifact_sha256` over the clone's
   Dockerfile bytes. In practice `admitted` implies `artifact_path == "Dockerfile"` at
   the repository root (A's `issue` fixes the path and ownership step 4 requires the
   record's path to equal the run's); the comparison inside `accept_for_fix` is kept as
   a guard.
4. `accept_for_fix(document, try_dir, target)` (A §7). Anything but `Accepted` →
   `stopped: no admission`.
5. **Trust re-check.** `accept_for_fix` checks the document's shape and evidence, not the
   trust set. Ownership is therefore re-verified with A's `verify_ownership` over R's
   restored `source/`, with the **current** trust directory and `checked_roots` covering
   R's restored `source/` itself, the clone, the fix worktree and the fix directory
   (`verify_ownership` does not add its `source_dir` to the roots on its own). Not
   `confirmed` →
   `stopped: no admission`. The same two checks run again in `fix publish` (§8.3); a key
   revoked between admission and publication blocks it.

Fix authoring never re-derives admission from logs and never works on a clone state other
than the admitted one.

## 3. Allowed change

Exactly:

- **one instruction** of the admitted Dockerfile (`binding.artifact_path`), and
- the **provenance set** re-issued for the corrected bytes (§5.2), whose effect on the
  tree is fixed by A's `issue`: `.deployer/authoring/Dockerfile.current` modified, one new
  directory `.deployer/authoring/Dockerfile/<record_sha256>/` with its three files added,
  every other set directory under `.deployer/authoring/Dockerfile/` deleted.

Every other path is unchanged. The full diff of the prepared commit is checked against
exactly these paths and change types before the commit (§5.3).

### 3.1 Binding the instruction

A's `defect` carries `class`, `file`, `lines` and `object` — not an ordinal or exact
bytes. They are derived:

1. Parse the admitted Dockerfile bytes (R's `source/Dockerfile`, equal to the clone's by
   the gate) with R's `dockerfile.parse`.
2. Select the instruction whose `(first_line, last_line)` equals `defect.lines`; exactly
   one must match.
3. Cross-check: for `missing_copy_source`, `defect.object` is one of its sources; for
   `from_argument_count`, R's normalised instruction text equals `defect.object`.
4. The original bytes and the byte span are those lines of the original bytes; the
   ordinal is the instruction's index.

Zero or several matches, or a failed cross-check → `stopped: fix method not established`.

After the edit:

- the instruction count is unchanged (no instruction added or removed);
- every other instruction is byte-identical;
- the byte diff lies within the bound instruction's span, and the instruction's line
  range is unchanged (both transformations are line-preserving: the COPY replacement
  contains no whitespace, F1/F2 edit within the line).

The stored link is `(file, lines, original, replacement)`. The ordinal is an internal
binding only, **not** evidence from CI: step numbers (`#k`, `[stage k/n]`) differ between
runs, and a matcher separately binds a real diagnostic to the corrected instruction (§6.3,
§7.3). Ambiguity there means no positive evidence.

## 4. The proposal

**Strict form, file-wide, first** (stage 3 ruling, 2026-09-25). Our Dockerfile reader
and Buildah split instructions differently on some inputs; review rounds of stage 3
reproduced such divergences on real Podman builds (a heredoc opener, a form feed or a
lone `\r` after a line-ending `\`, quoted heredoc delimiters, a BOM before
`# escape=`), and each let an instruction hide from our reader. Rather than patching
divergences one by one, one allow-list gate (`fix/reading.py::strict_form_reason`) runs
on the whole Dockerfile before anything else in the COPY envelope (§4.1), the FROM
transformations (§4.2) and the local matchers (§6.3). It refuses: a BOM; bytes that are
not UTF-8; line endings other than `\n` / `\r\n` (any other `\r`); control characters
other than tab and vertical whitespace (`\x0b`, `\x0c`, `\x1c`–`\x1f`, `\x85`,
U+2028, U+2029); `<<` anywhere, comments and quotes included; any parser directive
(`# <word>=` before the first instruction, `# syntax=` included); and anything but
spaces or tabs after a line-ending `\`. A refusal is `fix method not established` in
§4.1, no proposal in §4.2 and `binding ambiguous` in §6.3. This narrows coverage on
purpose; a rule is loosened only with a recording that shows both readers agree.

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
   ignore file on either side (CI, and local for R's recorded backend), in the modelled
   form (no glob, `$`, `\`, whitespace or `..`; R's normalisation), written in the
   notation of the original (e.g. no leading `./` when the original had none). An
   effective ignore file with an unmodelled pattern leaves exclusion unprovable → stop.
   **The artifact being corrected is never a candidate:** the exact path
   `binding.artifact_path` is excluded, because the fix itself changes its bytes and
   substituting it as a source would make the result depend on the edit. Only that path
   is excluded — another file named `Dockerfile` elsewhere in the listing stays a
   candidate under conditions 2–4. No other file is excluded by name (e.g. a
   `.dockerignore` that passes conditions 2–4 stays a candidate): being listed means only
   that a candidate is admissible; the model must justify its choice and a human reviews
   it.
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
source, the facts of the signed snapshot, and the eligible blobs after conditions 2–4.
The snapshot is the one `verify_ownership` returns in the gate's trust re-check (§2
step 5, `OwnershipFacts.snapshot`) — already bound to the record and signature. Output,
strict JSON:

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
of it by grammar. **Recognised flags** are exactly those R's FROM check skips: an inline
`--platform=<value>`. Substitutions in the reference (`$`, `${…}`) and unmodelled forms
are excluded. The reference, the flags and an existing stage name are kept byte for byte;
only the listed tokens change.

| Id | Original arguments | Conditions | Transformation | Example |
|---|---|---|---|---|
| F1 | `<ref> <token>` | `<token>` matches the stage-name grammar of §4.3, is not `AS` in any case, collides with no stage name, and **takes part in no reference or build-target selection** — no `--from=<token>`, no `FROM <token>`, no build arg of R's bound build configuration naming it (R's build-line parser refuses `--target` and other unknown flags, so a build arg is the only remaining channel). If this cannot be checked unambiguously → no proposal. | insert `AS` before `<token>` | `FROM python:3.12-slim extra` → `FROM python:3.12-slim AS extra` (R's run-5) |
| F2 | `<ref> AS` (dangling, any case) | — | remove the dangling `AS` | `FROM python:3.12-slim AS` → `FROM python:3.12-slim` |

Everything else → `stopped: no proposal`, for example: a reference without tag or digest
(`FROM python 3.12`, `FROM python builder` — the token may be a lost tag); a token
outside the name grammar (`FROM python:3.12 -slim`); four or more arguments; three
arguments with a middle other than `AS`; unrecognised flags; a name collision; a name
referenced anywhere. A reference to the name elsewhere does **not** prove that adding the
stage restores the link the author meant: `COPY --from=<name>` may denote an external
image or a named context, and adding a stage changes what it resolves to.

The explanation is deterministic: the id, the original and new text, and the conditions
checked. For run-5 the PR says it is a syntactic correction, not evidence that the author
intended a stage named `extra`.

### 4.3 The stage-name grammar

The intersection of what BuildKit and Buildah (Podman) accept for a stage name, derived
from their sources at pinned versions (§11 stage 1 deliverable: source note with the
pinned revisions, and tests over accepted and rejected tokens, `AS` in any case, and tokens
only one builder accepts). A token either builder would reject or treat differently → no
proposal.

## 5. Preparation — fix directory, worktree, provenance, commit

### 5.1 The fix directory and the worktree

R's layout is `<root>/.deployer-runs/<run_id>/reproduction/attempt-<n>/` holding
`source/`, `source.json` and `tries/<NNN>/`. Each `deployer fix` creates a **new
sequenced** fix directory `attempt-<n>/fixes/<NNN>/` (never reused or overwritten,
like R's tries). It holds `fix.json`, the local proof's `context/`, `build.stdout`,
`build.stderr`, and the Git worktree `worktree/`.

The worktree is a linked worktree of the clone at `head_sha`, on a new branch
`deployer/fix/<class>/<head_sha[:12]>-<NNN>`. The clone's `HEAD` and working tree are
never switched or touched. Both must lie outside the clone (§2 step 2). A's publication
lock resolves per worktree (`git rev-parse --git-path` gives
`<clone>/.git/worktrees/<id>/…`), so it does not serialise with a `deployer author` in the
main clone; the two operate on different working trees.

### 5.2 The new provenance set

The corrected Dockerfile gets a **new signed set**; the old record is never left as a
confirmation of the new bytes.

- Its source is the **original `head_sha`**: A's `preflight` runs in the fresh worktree
  before the edit is written, fixing the snapshot and facts; the signature covers the
  exact bytes of the corrected Dockerfile.
- **No ignore-file edit, ever.** A's `issue` appends `.deployer/` when `.deployer` itself
  is not excluded (`ensure_excluded` writes, then proves the concrete set paths) — so a set
  excluded by a narrower rule would still get an appended line. This design adds to
  `provenance` (stage 1 code deliverable):
  - an issuing **mode that never edits ignore files**: it only proves exclusion of the
    concrete set paths and refuses otherwise;
  - a pure **plan** step that builds the snapshot, the record and its `<record_sha256>` in
    memory from the preflight and the corrected bytes, yielding the exact set paths.

  A **preliminary** exclusion check (§5.3 step 1) uses the pointer path and a placeholder
  set path; it can only stop early. The **final** check runs on the real paths from the plan
  (§5.3 step 4), before the corrected Dockerfile or any set file is written. Failure of
  either → `stopped: commit blocked`, nothing written.
- A missing signing key or any issuing failure → `stopped: commit blocked`.
- The signature proves provenance, not the correctness of the fix, and does not
  guarantee a future `admitted`.

### 5.3 Order

1. Cheap preconditions first: fix directory writable, worktree creatable, signing key
   present and usable (A's `preflight` in the worktree), the preliminary exclusion check.
2. Proposal (§4).
3. Local proof (§6) in the fix directory's own context.
4. Plan the set in memory (§5.2) and run the final exclusion check on its real paths.
5. Write the corrected Dockerfile into the worktree; issue the planned set in the
   no-ignore-edit mode (it re-verifies the bytes on disk and must reproduce the planned
   `<record_sha256>`).
6. Check the **full** diff against §3 (paths and change types).
7. One commit containing both the corrected Dockerfile and the new set →
   `locally_confirmed`.

The corrected bytes are produced **once**, in memory: the same bytes are written to the
proof context (step 3) and the worktree (step 5), passed to the plan and to `issue`, and
their SHA-256 recorded once (§6.5).

A failure after the worktree exists keeps the worktree, and after the commit keeps the
branch and the commit; nothing is deleted automatically. The result and the reason are
stored (§8).

## 6. Local proof

### 6.1 Context and backend

R's restored `source/` stays untouched. It is copied into the fix directory's `context/`
and the corrected Dockerfile is written there; the build context is formed from it as R
forms it, with the same effective ignore files. The context is the committed tree with the
corrected Dockerfile; `.deployer/` (the old set, at this point) is excluded on both sides,
as A requires.

The build configuration is the one R bound from CI — `BuildConfig`'s `dockerfile` (`-f`),
context `.`, `build_args` and `platform` (the image `tag` is not compared) — run through
R's adapter.

The local backend must equal the backend R recorded (`environment.backend`). The local
templates are Podman-only in this slice; any other backend →
`no local confirmation: local backend differs from R's` (a `--container-tool docker` run
included), since both the templates and the local effective ignore file
(`.containerignore` is read only for Podman) depend on it.

### 6.2 Offline checks, per instruction, subject and condition

R's check results cannot be projected per instruction after the fact: `copy_sources`
returns one aggregate `passed` without a location, a single failure suppresses the passed
results of every other source, a matching glob returns nothing, skips carry the
instruction only in free text, the four syntax checks emit one file-level `passed` each,
and the count of checked instructions is a local variable, not part of the result.
Reconstructing "passed" from the absence of a finding is therefore not allowed.

This design adds a **structured detailed result**, produced **while the checks run**
(a stage 1 code deliverable). R's offline checks gain a detailed entry point — the same
logic, emitting one record per unit checked; R's existing aggregate output is derived
from those records and stays byte-identical (R's own tests guard this). A record is:

- `check_id` — `copy_sources` or one of the four syntax checks;
- `instruction` — the instruction's span `(first_line, last_line)` and ordinal;
- `subject` — for `copy_sources` the normalised source path (one record **per source**,
  so several sources of one instruction are distinct); for a syntax check the rule's
  condition on that instruction (e.g. `from_args`);
- `status` — R's (`passed | failed | skipped | observation | inconclusive`);
- `reason` — for every status but `passed`.

A skip that R applies file-wide (e.g. an unread Dockerfile, an unmodelled ignore file)
**propagates** to a `skipped` record for every affected `(check_id, instruction,
subject)`; a check that did not run for a unit emits `skipped`, never nothing.

The comparison covers R's **offline** checks (`copy_sources` and the four syntax checks);
the builder checks (`builder_check`, `builder_syntax`, `builder_lint`) are skipped by a
local Podman run on both sides and are not part of it.

- The defect's check passes **for the corrected instruction and its specific subject**:
  for `missing_copy_source`, a `copy_sources` record `passed` for the **new source** of the
  bound instruction; for `from_argument_count`, the `syntax_from_args` record of the bound
  instruction `passed`.
- **No regressions:** the offline checks run **fresh** with the detailed entry point on
  `source/` (original bytes) and on the fix context (corrected bytes) — not read from R's
  recorded manifest, whose checks are merged with builder results. Records are matched by
  `(check_id, instruction ordinal, subject)`; the corrected source's record is matched to
  the absent source's. Any record `passed` before and `failed`, `skipped`, `observation` or
  `inconclusive` after fails the rule; a record present before and missing after fails it
  too.

### 6.3 Local positive evidence — closed, recording-backed

A closed table of local "passed" templates (Podman). A row is enabled only with the
recording that backs it (§9); a disabled row yields `no local confirmation: templates not
enabled`. Both local rows are backed by the L-recordings
(`tests/fixtures/recordings/local`, Podman 5.7.0 / Buildah 1.42.0) and by the pinned
Buildah reading in `docs/fix-buildah-from-parse.md`, and are enabled (owner, 2026-09-25).

What the recordings and the reading show, and every rule below relies on:

- Podman checks each stage's FROM when the stage starts, not in a whole-file pass, and
  prints the stage's FROM step line only **after** that check passed (note steps 5–6;
  `l8`). This holds for FROM only: a COPY/ADD step line is printed **before** the step
  runs, so it proves only that the step started.
- Podman prints the FROM line **rebuilt** for display (Buildah `stage_executor.go`
  `prepare`: the base after ARG/env expansion with quotes removed, `--platform=` in
  front, ` AS <name>` only for a non-numeric name); COPY/ADD step lines print the
  instruction as written.
- A stage nothing depends on is **skipped**: none of its instructions runs and none of
  its step lines is printed (`l5`, `l9`). A skipped stage's instruction is therefore
  never confirmed: its step line is absent → not confirmed.
- In a file with more than one stage every step line carries an `[i/n] ` prefix
  (`[2/2] STEP 3/4: COPY …`, `[2/2] COMMIT <tag>`); `n` counts every stage, skipped ones
  included (`l5`, `l7`). The matchers read the prefixed form and the unprefixed one.
- **Uniqueness in the Dockerfile, first.** The corrected instruction's text must occur
  exactly once among the corrected Dockerfile's instructions, compared as the rest of the
  fix compares them (A's `decode_as_read_text`, R's `dockerfile.parse`, `Instruction.text`; a FROM
  by its rebuilt display form); otherwise → `binding ambiguous`, decided before any output
  is read. That comparison is sound only in the modelled form, so it is refused
  (`binding ambiguous`) when the whole corrected Dockerfile fails the fix-wide reading
  checks (`fix/reading.py`: the strict form of §4, a continuation without a preceding blank, a keyword not
  separated by a space, a comment inside a continuation, an unmodelled escape
  directive), or when any instruction of the kind's family (FROM; COPY and ADD) holds
  `$`, a quote or a backslash, is a COPY/ADD heredoc or JSON form, or is a FROM with a
  numeric stage name. A skipped identical
  instruction prints nothing, so it must never make the one printed line look unique:
  `l5` has the identical COPY in a skipped and a built stage and prints one step line;
  `l7` builds both and prints two.

- **COPY/ADD:** the step line carrying the corrected instruction's exact text, prefixed
  or not, exactly once across all stages (else `binding ambiguous`). A `STEP` line only
  proves the step **started**. It passes only if the next marker is the **same stage's**
  `STEP k+1/m`. After the last step (`k == m`) of the **final** stage, the build's
  completion counts, and only bound to the build's own tag (the one the build was given,
  `localhost/deployer-fix-<fix_id>`): `COMMIT <tag>` with the stage's prefix, or
  `Successfully tagged <tag>` — `<tag>:latest` when the tag has no explicit tag part. A
  completion naming another tag does not count (`l2` prints one) → `binding ambiguous`.
  The last step of a **non-final** stage stays `binding ambiguous`: the next stage's
  start does not prove it. An error bound to the step itself
  (`Error: building at STEP "<text>"`) → not confirmed.
- **FROM:** the evidence is the corrected FROM's **own** step line,
  `STEP 1/m: <corrected FROM text>` (prefixed or not), exactly once. It needs no next
  marker: Buildah prints it only after the stage's FROM check passed (note step 6). Any
  Podman parse error in either stream, or an error bound to that step, → not confirmed.
  The earlier file-wide rule ("a build-stage step exists and no parse error") is
  withdrawn for Podman: `l9` exits 0 with the bad FROM still in the file, in a skipped
  stage; `l8` builds an earlier stage and then fails the bad FROM when its stage starts.
  Loading the Dockerfile or the context is not evidence. Parsing and pulling the image are
  distinct: a later image-pull failure of the same FROM does not refute the step line, is
  recorded explicitly, and is not called "a failure of another instruction". The optional
  image-pull record needs a pull line bound to the corrected FROM unambiguously; if the
  builder normalises the reference so that none binds, the pull result is not recorded —
  this never affects the FROM evidence.
- A step line the closed template cannot bind to the instruction unambiguously →
  `binding ambiguous`. Forms the recordings do not show are not supported; the matcher is
  not widened by assumption.

BuildKit (§7.3) is unchanged: its FROM evidence stays file-wide. The C-recording
`c8-from-bad-in-skipped-stage` (PR #98, C-recordings) confirmed
BuildKit's whole-file parse: a bad FROM in a stage nothing depends on fails before any
stage, unlike Podman's `l9`.

### 6.4 Later failures

A build failure after the corrected place is allowed only when the boundary is
**proven**: the positive evidence of §6.3 was already obtained. If that cannot be
established → no local confirmation. A timeout never confirms. The PR states the narrow
claim — "the diagnosed source error is removed locally" — never "the substitution builds"
when the build passed the corrected step and failed later.

### 6.5 Evidence

The configuration is fixed in the evidence: the SHA-256 of the corrected Dockerfile, the
build configuration (§6.1), the backend and its versions. `build.stdout` /
`build.stderr` of the fix directory are referenced with A's line rules (UTF-8, split on
`\n`, a final newline opens no line).

## 7. CI confirmation

### 7.1 Reading runs of any outcome

A new reading function in `forge` reads a run of **any** conclusion without any rebuild:
its metadata (event, `head_sha`, `head_branch`, workflow path, attempts), each completed
attempt's jobs with their steps and conclusions, and the full log of the bound job,
through the same GitHub API R uses (`_Gh.jobs(run_id, attempt)` and `_Gh.logs(job_id)`
are already conclusion-independent). Listing runs by `head_sha` is a new endpoint with
the same pagination completeness check. The failed-run reading is unchanged.

The existing binding (`shape.precheck` / `_check_steps`) is failure-bound — it works on
non-green jobs only and locates the build from the failed step — so it is **not** reused
as is. A new **qualification** function reuses R's pieces (the job-name-to-key rule, the
event and path checks, `parse_build_line`, the checkout-SHA rule) but locates the build
step by its parsed build line, independent of the step's conclusion. Both are a stage 2
deliverable with their own tests.

### 7.2 Which runs qualify

Runs are listed by `head_sha` = the **exact fix commit**. Every completed attempt of every
listed run gets one of three qualification results:

- `qualified` — every condition below is established from data that was read;
- `excluded` — a condition is **proven** false from data that was read (e.g. the event is
  `pull_request`, the workflow path differs), with that reason;
- `undetermined` — a condition could not be established because data is missing (an
  unavailable log or job listing needed for the checkout or the build binding, an
  incomplete API response).

The conditions:

- its event is `push` or `workflow_dispatch` (`pull_request` excluded in this slice);
- its workflow path equals the one R bound in the original run, and the workflow bytes at
  the fix commit equal those at `head_sha` (follows from §3);
- exactly one of its jobs maps, by R's name rule (API job `name` against
  `definition.name or key` in the workflow at the fix commit), to R's original
  `binding.workflow_job` key — not the numeric GitHub job id, which belongs to one run;
- the qualification function binds a build line in that job with the original build
  configuration (`dockerfile`, context, `build_args`, `platform`);
- the checkout was at the fix commit, by R's checkout-SHA rule.

A merge or squash commit does not confirm the fix commit: it is a different object.
An attempt that could not be read far enough to be excluded is `undetermined`, never
silently dropped: dropping it would let a positive attempt elsewhere hide a possible
contradiction.

### 7.3 CI positive evidence — closed, recording-backed

A closed table of CI "passed" templates (BuildKit plain progress). Both CI rows are
backed by the C-recordings (`tests/fixtures/recordings/ci`, real `workflow_dispatch`
runs on the polygon, PR #98) and are enabled (owner, 2026-09-26; F4b); a row without a
recording would be disabled → `templates not enabled`. What the recordings show, and
every rule below relies on:

- **The corrected Dockerfile first, file-wide.** The corrected Dockerfile's bytes at the
  fix commit are read by the guarded chokepoint, like the workflow, with replace refs
  off. The path must be the bound build's Dockerfile, and the bytes must hash to the
  locally proved `local_proof.dockerfile_sha256`: the file CI built is the one proved
  locally. An unreadable file or a mismatch makes the confirmation `qualification
  undetermined` with the reason. The bytes must pass the strict form
  of §4 before any log line is read; a refusal → `binding ambiguous` with the gate's
  reason. This holds for both kinds.
- **COPY/ADD uniqueness in the Dockerfile** (the local rule of §6.3, unchanged): the
  corrected text must occur exactly once among the corrected Dockerfile's COPY/ADD
  instructions in the modelled form — the whole file passes the fix-wide reading checks,
  and no instruction of the family holds `$`, a quote or a backslash or is a heredoc or
  JSON form — else `binding ambiguous`, decided before the log is read. BuildKit also
  skips a stage nothing depends on and prints no header for it, so an identical COPY in
  a skipped stage must never make one printed header look unique.
- **The bound build step's own section only.** The template reads only the part of the
  job log that the build step printed. The bound header is exactly one runner group
  header `##[group]Run <bound build line>` (the build step's name as the jobs API gives
  it). The section is the lines after the first `##[endgroup]` that follows that header:
  the runner's echo of the script, `shell:` and `env:` is not build output, and an
  `env:` value with newlines prints untimestamped continuation lines there (review N2).
  The section runs the first line that starts what follows the build step: any `##[group]` line,
  the runner's post phase `Post job cleanup.` (every C-recording prints it right after
  the build output: `c1` line 208, `c4` line 213), or `Post <step name>` for a step of
  the job. If none of these comes, the section runs to the end of the log. Post-job
  output never supplies evidence. Every section line must start with the runner's
  timestamp, exactly as the recordings show it (`2026-09-25T14:30:21.1506466Z `, seven
  fraction digits, `c1` line 109). The recordings carry a BOM only before the log's
  first line, which is never in the section. A line without the timestamp, or a second
  `##[endgroup]` inside the section → `binding ambiguous`. It works on the raw log
  as read, split on `\n` only; a CRLF ending counts as one `\n` break. No such header,
  or several → `binding ambiguous`. A section line that holds any other line break
  `str.splitlines` would split on is also `binding ambiguous`, and the character is
  named: a lone `\r`, `\x0b`, `\x0c`, `\x1c`–`\x1e`, `\x85`, U+2028 or U+2029. So
  another step's output (a lint or test step printing `#0 [stage-0 …] COPY …` /
  `#0 DONE`), or a RUN's output that smuggles such lines behind a non-`\n` break, can
  never supply the evidence. None of the C-recordings holds such a break. Every
  recorded BuildKit line sits in the build step's section. The template's evidence
  lines are numbered in that raw log; recurrence keeps A's whole-job reading, and its
  lines are numbered in the job text.
- **Padded step numbers.** BuildKit right-aligns the step number to the width of the
  step count: `[stage-0  7/10]` (`c4`, `c9`); `[stage-0 7/9]` when both are one digit
  (`c1`). The header is read only in that form: the spaces before `k` plus its digits
  are exactly `len(n)` wide (the unpadded form when `k` is as wide as `n`), `k` has no
  leading zero, and any other run of spaces, or a tab, is not a stage header. A bracket
  without a stage name (`[k/n]`, `[ k/n]`) is not read either: every recorded header
  is named (`stage-0`, `extra`). (`c7`
  was meant to show padding, but `LABEL` is not a build step, so it prints nine steps
  unpadded; it stays as recorded.)
- **COPY/ADD (BuildKit):** exactly one stage header whose instruction text is the
  corrected instruction, and `#k DONE` for the same `k`. `#k CACHED` is **not** accepted
  automatically. A missing or repeated `k` → `binding ambiguous`. A later failure of
  another step (`c4`: `RUN false` after the corrected `COPY … DONE`) does not refute it
  (§7.4) — but only when it provably follows the COPY. The runner reads step output
  with .NET `ReadLine()`, which also splits on `\r` and stores each piece as a clean
  timestamped line, so a failing RUN can print its own header / `#0 DONE` pair for a
  COPY the build never reached (review N1). Had the real COPY run, its header would
  repeat. So when the section holds any `#k ERROR`, the COPY passes only if there is
  exactly one erroring vertex and its stage headers give one step, in the COPY's stage,
  numbered higher than the COPY's (`c4`: `#15 ERROR` at `[stage-0  8/10]` after the
  COPY at `7/10`). An error in another stage, at an equal or lower step, unmapped, or
  more than one erroring vertex → `binding ambiguous` ("failure not provably after the
  corrected step"). Successful builds are unchanged. The forged header could name any
  step, so the COPY's position is never read from the header (review R1): it is
  derived from the corrected Dockerfile, and the matched header's bracket must be
  exactly `<stage> <k right-aligned>/<n>`, the failing vertex compared against that
  derived `k`, stage name and `n`. The numbering model is only what the C-recordings
  show: one stage, its FROM first, printed `stage-0` unnamed (`c1`) or by its name for
  `FROM <image> AS <name>` (`c3`), no FROM flag; `FROM`, `RUN`, `COPY` (also
  `--from=<image>`) and `WORKDIR` are steps; `ENV`, `USER`, `CMD` (`c1`, count 9) and
  `LABEL` (`c7`, count 9) are not. Any other instruction (`ADD`, `ARG`, `ENTRYPOINT`,
  `EXPOSE`, …), several stages or another stage form → `binding ambiguous`; a step 0
  or past `n` is no stage header. Failing vertices (`#k ERROR`/`#k CANCELED`) are
  searched from the section start to the **end of the log**, not only in the section
  (review R2): a failing step can print a section-end line (`Post job cleanup.`,
  `##[group]…`), which ends the positive evidence but must not hide the real
  failure. Both rules only refuse. The COPY pass is also bound to the bound build step's
  conclusion in the jobs API, which no build output can forge (rulings AB, AD): only
  `success` can confirm, and then with no failing vertex anywhere. A build step that
  concluded `failure` never proves the corrected COPY ran: every line that could show
  it — the header, `#k DONE`, a later `#k ERROR` placing the failure after the COPY — is
  output a failing `RUN` before the COPY can print, and a killed build prints no real
  `#k ERROR` (#100 review). So `failure`, `success` with a failing vertex, any other
  conclusion (`cancelled`, `timed_out`, `skipped`, none) and a build step that is not
  uniquely identified → `binding ambiguous`. The after-the-COPY rule above is thereby
  superseded for confirmation; `c4` is insufficient. A workflow that uses
  `continue-on-error` is `undetermined` (the API may then report a failed step as
  `success`). Known limitation: the build step is found by its runner group title
  `Run <build line>`; a build step with a workflow `name:` is titled by that name and
  is not read, so it never confirms (`binding ambiguous`). A step can print `::add-mask::<text>`, after which the runner logs that
  text as `***` everywhere: any line the rules above rely on could be erased, so `***`
  anywhere from the section start to the end of the log → `binding ambiguous` (ruling
  AC; legitimately masked secrets refuse too). BuildKit re-prints a vertex header when progress interleaves (`c1` prints
  `#7 [stage-0 1/9] FROM …` twice). A corrected COPY re-printed this way reads as a
  repeated header or `k` → `binding ambiguous`. That is a known, conservative false
  negative; no C-recording shows it for the corrected COPY.
- **FROM (BuildKit):** the whole file parsed. In the bound build step's section, a named
  build-stage header exists and there is no `dockerfile parse error`. The rule does not
  look at the corrected FROM's text. What ties it to the corrected bytes is the file
  check above: the Dockerfile at the fix commit hashes to the locally proved one and is
  in the strict form. Frontend, context or definition loading steps are not evidence. This rule stays file-wide, with no uniqueness check: BuildKit parses the
  whole Dockerfile before any stage, and a bad FROM in a stage nothing depends on fails
  the build with `dockerfile parse error on line 1` before any stage runs (`c8`), unlike
  Podman (`l9`, §6.3). The image-pull result of that FROM is recorded when visible.
- A job log holding more than one build (the definition loaded twice, `#k` numbering
  restarting) → `binding ambiguous`; `c6` shows it, though its two build steps already
  leave the attempt `qualification undetermined` at §7.2.

**Recurrence of the diagnosed defect** is an A template row of the admitted class, bound
by A's binding rules (A §4.2) to the corrected instruction — same line span, the corrected
text — whatever its object. (For `missing_copy_source`, a "not found" for the new source
is a recurrence of the class at the same instruction.)

### 7.4 Evaluating attempts

All qualifying **completed attempts** available at the time of the check are considered —
not only the latest of each run, since a re-run may hide an earlier positive or negative
result.

- Positive evidence present, no recurrence → `ci_confirmed`.
- Positive evidence and a recurrence → insufficient: `contradictory runs`.
- A recurrence and no positive evidence → insufficient: `defect recurred`.
- Failures inside a qualifying run (network, skipped or unreached steps, earlier jobs) are
  not contradictions, but confirm nothing on their own.
- Any `undetermined` attempt (§7.2), or an incomplete run listing, means the absence of
  contradictions cannot be claimed → insufficient: `qualification undetermined`, even
  when another attempt is positive. `excluded` attempts are listed with their reasons and
  take no further part.

A later independent failure in the same run does not cancel proven passage of the
corrected place. Ambiguous binding → no positive evidence. On CI, passage is proven only
by a build step that concluded `success` (§7.3, ruling AD): a failed step's log cannot
prove it, so a CI run whose build fails after the corrected instruction confirms nothing
(`c4`), and the local proof's own later-failure record (§6.4) is unaffected.

### 7.5 The result of a confirmation attempt

Each attempt records the check time and the exact list of `run_id` / attempt / job key
considered, the outcome, the reason and the evidence (run, attempt, job, log lines).
Reasons for `ci_confirmation_insufficient`: no qualifying run; CI failed before the build;
build step not reached; unknown format; binding ambiguous; templates not enabled;
contradictory runs; defect recurred; qualification undetermined (incomplete listing or
unavailable log).

## 8. The fix document and the CLI

### 8.1 `fix.json` (schema 1.0)

Stored in the fix directory (§5.1) — dev-side evidence, never committed to the project.
Fields:

- `input` — SHA-256 of the verdict document, its `binding`, the clone state (path,
  origin, `HEAD`, clean-tree result), the target;
- `proposal` — `class`, `file`, `lines`, `transformation` (`copy-source` | `F1` | `F2`),
  `original`, `replacement`, internal `ordinal`, `rationale` (model or deterministic),
  `envelope` (each condition and its result);
- `local_proof` — configuration, checks before/after as detailed records (§6.2), evidence
  files and lines, `later_failure` (separately: image pull of the same FROM / another
  instruction);
- `publication` — worktree path, branch, fix commit, the full-diff check, `base` branch,
  PR URL;
- `ci_attempts[]` — append-only (§7.5);
- `status`, `last_operation`.

Writability is checked before any operation; the document is saved **atomically at
checkpoints**: after the preconditions (`in_progress`), after the proposal, after the
local proof, after the commit, after publication, after each confirmation attempt.

### 8.2 Commands and exit codes

- `deployer fix <verdict.json> --clone <path> [--signing-key …]` — §2–§6, commit in the
  fix worktree.
  - `0`: `locally_confirmed` (commit prepared);
  - `1`: `stopped` — every stop reason, whether or not a worktree or commit already
    exists;
  - `2`: invalid invocation (incl. a fix directory inside the clone) or a local I/O
    failure (reading the verdict, saving `fix.json`).
- `deployer fix publish <fix.json> --base <branch>` — the explicit permission to push and
  create the PR; no interactive confirmation inside. The PR's base branch is a
  required flag: the failed-run snapshot does not record the run's branch, and inferring
  one is a guess.
  - `0`: pushed and a PR created or found (also on an already `fix_proposed` or
    `ci_confirmed` document);
  - `1`: refused — admission or trust re-check failed, stored state changed, push or PR
    creation failed, status not `locally_confirmed`/`fix_proposed`/`ci_confirmed`;
  - `2`: local I/O failure.
- `deployer fix confirm <fix.json>` — one confirmation attempt.
  - `0`: the attempt is positive (`ci_confirmed`);
  - `1`: insufficient (every §7.5 reason, including an unavailable CI log or an incomplete
    API listing), or the document is not published;
  - `2`: local I/O failure.

Exit codes of existing commands are unchanged.

### 8.3 Publishing safely and repeatably

Before pushing, `fix publish`:

- records the chosen `--base` in `fix.json` (atomic save) **before any network action**;
  a later `fix publish` with a different base is refused;
- re-runs the gate's admission and trust checks (§2 steps 4–5) with the **stored**
  target and the **current** trust directory — a ready branch does not preserve the
  permission to publish;
- checks the **future PR's diff**, not only the fix commit against its parent: for a new
  publication it computes the actual `merge-base(base_tip, fix_commit)` on the fetched
  base and requires it to **equal `head_sha`** (ancestry alone is not enough: a base that
  already contains the fix commit gives another merge base and an empty PR), then
  requires the resulting `merge-base..fix_commit` diff to match the allowed change (§3)
  exactly; otherwise the PR would be empty or carry unrelated changes → refused;
- verifies that the fix branch's tip is the stored fix commit, and re-checks that
  commit's full diff (§3) and the stored evidence hashes; any change → refused. The user's
  own clone `HEAD` is not checked here (the worktree is independent of it);
- reuses an existing remote branch and PR for that fix commit: after a network timeout it
  neither publishes a changed commit nor creates a duplicate PR (a PR is looked up by the
  branch before creation).

A push or PR-creation failure leaves the status as it was (`locally_confirmed` on the
first publication; `fix_proposed` or `ci_confirmed` on a repeat, §1) and the local
evidence intact; the failure is recorded in `last_operation`.

### 8.4 Errors

Every step is total: an exception becomes a reason in the document, never a traceback.
Exit `2` is reserved for local I/O failures; when one happens after operations already
performed (a `fix.json` save after the commit was created), the message names the
identifiers of what was created (fix directory, worktree, branch, commit, PR).

## 9. Recordings — the gates

Each class of recording needs the owner's **separate** permission for real runs.

- **L-recordings (local Podman):** real builds of corrected Dockerfiles for run-1 and
  run-5, and the cases: a bad FROM in a later stage; several stages on the same image; a
  later failure after the corrected COPY.
- **C-recordings (CI):** real `workflow_dispatch` (polygon) runs of fix commits on the polygon repository:
  successful BuildKit forms (COPY header + `DONE`, `CACHED`, the FROM stage header), a run
  with a later independent failure, a re-run.

A template row is enabled **only together with** the test that checks it against its
real recording; the production table carries, per row, the recording it is backed by, and
a test asserts that no row is enabled without one. If real logs require a matcher change,
that code goes through the ordinary review; the data and expected results are the owner's
separate review. Without L-recordings no `locally_confirmed` and no publication; without
C-recordings a published proposal is never CI-confirmed.

## 10. Acceptance — offline

**Test seam.** A production row is enabled only with its recording (§9): the two local
rows are, on the L-recordings, and the two CI rows on the C-recordings
(`tests/fix/test_ci_recordings.py` replays every case through forge, qualification,
evidence and `fix confirm`). Tests reach a disabled row by injecting an **enabled synthetic row** through a
test-only registry (not reachable from the CLI or configuration); the test that no
production row is enabled without a recording (§9) guards the seam.

- **D (decision):** pure parts with hand-built inputs, one targeted mutation per case:
  - §3.1 binding: zero/several instruction matches; a failed cross-check; the link
    invariants (instruction count, other instructions byte-identical, diff within the
    span, line range unchanged);
  - §4.1: every envelope condition incl. collisions (a)/(b), a multi-source instruction
    with a non-file source, an unmodelled ignore pattern; the model not called on zero
    candidates or the basename floor (the fake asserts no call); the answer checks
    (malformed, `plausible` ≠ `source`, several candidates, a citation of a missing path or
    an unknown `ProjectFacts` field); exactly one call, no retry;
  - §4.2/§4.3: F1/F2, each no-proposal branch, a name used by a build arg; the stage-name
    grammar tests;
  - §6.2: the detailed records (one per source of a multi-source instruction; passed
    records of other sources present when one fails; a file-wide skip propagated to every
    unit; R's aggregate output byte-identical) and the regression rule per
    `(check_id, instruction, subject)`, incl. a record that disappears;
  - §6.3/§7.3 matchers on synthetic lines: negatives only until recordings, incl.
    `#k CACHED` not accepted and a missing or repeated `k` → `binding ambiguous`; a
    timeout never confirms; the narrow PR claim after a proven pass and a later failure;
  - §7.2/§7.4: `qualified`, `excluded` and `undetermined` attempts; a positive attempt
    plus an `undetermined` one → insufficient; contradictory, recurred;
  - status transitions and the exit-code table of each command (§1, §8.2).
- **P (pipeline):** from the committed A4 bundles `admit-run-1` / `admit-run-5`, with the
  container runtime, the model and GitHub faked, and **real** local Git (worktree, commit);
  push and `gh` are faked:
  - run-5: F1 → `FROM python:3.12-slim AS extra`;
  - run-1: a **derived** case whose tree holds exactly one eligible file for the absent
    `docs/setup.md` (data, §11 stage 1b); and a case with two same-basename candidates →
    `fix method not established`.

  Without the seam, the local side runs on the enabled, recording-backed rows: P tests
  whose faked build prints the passing local template (the corrected instruction's step
  line, then the same stage's next step) end at `locally_confirmed`, and those whose
  output does not end at `no local confirmation: <the matcher's reason>`. The CI rows are
  enabled on the C-recordings too; their outcomes are asserted end to end, through
  `fix confirm`, by `tests/fix/test_ci_recordings.py` — asserted outcomes, not skips.
- **G (real local Git, offline — no model, no container builds):** the worktree leaves the
  user's checkout untouched; the commit contains exactly the §3 paths and change types; an
  extra changed file is refused by the full-diff check; a fix directory inside the clone
  exits `2`; the fix-branch tip changed before `publish` is refused.
- **Gate (§2):** clone not at the repository root; no `origin`; untracked-only dirt;
  `HEAD` ≠ `head_sha`; Dockerfile bytes ≠ `artifact_sha256`; a revoked key; a trust
  directory inside R's restored `source/` → each `no admission` (at `fix` and at
  `publish`).
- **Commit (§5.2–§5.3):** exclusion needing an ignore-file edit — at the preliminary
  check and, separately, at the final check on the planned paths (e.g. a rule excluding
  `.deployer` but not the concrete set directory) — with nothing written; the issuing mode
  never touching ignore files; `issue` reproducing the planned `<record_sha256>`; no
  signing key; an `issue` failure → `commit blocked`; the configuration and SHA-256
  recorded (§6.5).
- **Document and errors (§8):** writability checked first; atomic checkpoint saves; a save
  failure after the commit (exit `2`, identifiers named); an injected exception becomes a
  reason, never a traceback; a crashed `in_progress` document is not resumed.
- **Publication and confirmation boundaries:** trust revoked before `publish`; a stored
  commit or diff tampered with; the base recorded before any network action and a
  different `--base` refused on repeat; a base whose merge base with the fix commit is not
  `head_sha` refused (incl. a base that already contains the fix commit); a PR diff that
  differs from the allowed change refused;
  a push failure on a repeat publish leaving `fix_proposed`; a repeat after a network timeout; no duplicate PR;
  `publish` on an already published document; `confirm` on an unpublished one; positive and
  contradicting attempts; an incomplete API listing; an unavailable log; the transition
  `ci_confirmed → fix_proposed`.
- **Reading (§7.1–§7.2), stage 2:** a successful run and all attempts are read; each
  qualification filter refuses (a `pull_request` event; another workflow path; a job key
  missing or matching several jobs; another build configuration; a checkout at another
  SHA; a run on a merge commit).

## 11. Delivery

Stages; code is split into PRs by the review kit's limits, data stays separate and is the
owner's review:

1. **Fix authoring:** gate, binding, proposal and envelopes, the stage-name grammar with
   its pinned-source note (§4.3), the set plan and the no-ignore-edit issuing mode in
   `provenance` (§5.2), the detailed check results in `reproduce` (§6.2), local proof with templates disabled, fix
   directory and worktree, `fix.json`, `fix publish`.
   **1b (data):** the derived run-1 case — tree, listings and signed data updated
   consistently; its `PROVENANCE.md` separates the original failure record from the test
   modification.
2. **Reading runs** of any outcome and all attempts; the qualification function;
   `fix confirm` with templates disabled.
3. **L-recordings**, local template rows and their acceptance tests (data); the Buildah
   parse-path reading for FROM (§6.3).
4. **C-recordings**, CI template rows and their acceptance tests (data).
5. End-to-end acceptance, documentation, and closing `todo://deployer/ci-fix-authoring`.

The item stays open until stage 5; stages 1–2 may be accepted as implementation without
claiming the agreed scope complete.

## 12. Decisions (owner, 2026-09-25)

1. Proof level: local proof before the PR **and** CI confirmation by a real run on the
   fixed commit; three claims kept apart; missing old finding ≠ fix.
2. Authoring: the model proposes, a deterministic closed envelope admits; one instruction
   of the admitted Dockerfile.
3. COPY: only the proven-absent source changes; regular files only in this slice;
   basename floor, no proximity threshold; `plausible` is the model's assessment.
4. FROM: closed list F1/F2 with a complete literal reference; no model; F1 only when the
   name takes part in no reference or target selection.
5. Local "passed" templates are recording-backed; the fixed Dockerfile gets a new signed
   set from the original `head_sha`.
6. CI: exact fix commit, `push`/`workflow_dispatch`, all completed attempts; at least one
   positive and no contradiction; `CACHED` not automatic.
7. `fix publish` is a separate command; worktree preparation; statuses and last operation
   stored apart.
8. Delivery order as §11; recordings each need separate permission.

9. (rev 2, confirmed) The status mirrors the latest completed CI evaluation; the history
   of positive evidence is kept (§1). The PR base branch is a required `--base` flag,
   recorded before any network action (§8.2–§8.3).
