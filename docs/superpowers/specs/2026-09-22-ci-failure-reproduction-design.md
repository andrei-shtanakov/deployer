# CI-failure reproduction — design (rev 4 of the diagnosis line)

**Status:** DRAFT rev 4, revised after the external review of rev 3 at `ce19753`
(`../../../../_cowork_output/deployer-reproduction-spec-review-2026-09-23.md`) and the
owner's review of 2026-09-22
(`../../../../_cowork_output/deployer-reproduction-draft-review-2026-09-22.md`). Both
are dev-only workspace files, absent from clones; every decision they made is restated
in this document where it applies. Next: external review by exact SHA, then a plan. No
code exists for this design.
**Base:** the reading layer as merged on `master` at `770066d` (PR #72): `forge.py`,
`diagnose.py`, `deployer diagnose`, snapshot schema 1.1, verdict schema 1.1.
**Relation to the 2026-09-21 spec:** supersedes its causal half (§3 outcomes, §5
catalogue, §8.2 "three established classes"). Its reading layer is this design's
input, under the reduced contract of that spec's Addendum: no causal classes —
`FailureVerdict.kind` is always `None`, `RunDiagnosis.causes` always empty.

## Why this revision exists

PR-3's first design tried to establish the *cause* of a failed run from phrases in its
log. Eight review rounds showed the same thing from eight angles: a phrase names a
symptom, not a cause — "file not found" says a file is missing, not why (a wrong COPY,
a different build context, a file the project never generated) — and every exception
added for warnings, tracebacks, buildkit framing or quoting produced the next
counter-example. The owner stopped the catalogue on 2026-09-22.

The data that *can* ground findings about an authored Dockerfile already exist in
this repo: the artifact, its build context, deterministic checks over both, and a
container runtime that builds images. This revision moves the work onto those data.
Log text remains an *input* (which step failed, which instruction, what the tool
printed) and is never the sole basis of a result.

Rev 4 closes the gaps the external review found in rev 3 by **narrowing the slice**
rather than widening the design: one job, one build step that is the failed step, a
local backend only, and no container run after the build (§4–§5). Everything outside
that shape is a named refusal or a named approximation, never a guess.

## Non-goals

- **Naming a culprit.** The first slice returns **findings with status and evidence,
  and no `FailureKind`.** A reproduced failure says the failure recurs, not whose it
  is: `polygon/run-2`'s unroutable apt mirror reproduces locally just as faithfully as
  `polygon/run-1`'s missing COPY source (§8). Deriving classes is a separate, later
  task with its own acceptance.
- **Running the image.** The slice reproduces a failed *build step*; the build is the
  reproduction. Running the built image (run intent, healthcheck, ATP smoke) needs a
  deploy target this input does not carry, and is a later slice (§5).
- Diagnosing arbitrary CI — only the supported shape of §1.2.
- Executing anything taken from a log or a workflow. The workflow's build line is
  *parsed* into a supported configuration; this repo then issues its own command (§4).
- Remote hosts. `--container-host` is refused with `--reproduce` in this slice: the
  context is built as restored (§1.4), without the secret-stripping `CONTEXT_IGNORE`
  of `verify`, so it must not leave the machine.
- Fixing the artifact — `todo://deployer/ci-fix-authoring` stays a later slice.
- A second Docker frontend. This repo's parser checks a named, closed list of things
  (§3); the builder is the authority on what the builder accepts (§2).

## 1. Inputs and restoration

### 1.1 What the snapshot must carry (snapshot schema 1.2, additive)

Today's `FailedRun` (schema 1.1, `forge.py:136–146`) carries no workflow path, and
`FailedJob.steps` keeps only non-green steps (`forge.py:436–449`). Reproduction needs
both, so the forge adds two fields, read from data the adapter already fetches:

- `FailedRun.workflow_path` — the run's `path` from `actions/runs/{id}`
  (e.g. `.github/workflows/diagnosis-polygon.yml`);
- `FailedJob.all_steps` — every step of the job from the jobs listing:
  `number`, `name`, `conclusion`, in order.

Both are additive (schema 1.1 → 1.2); a 1.1 snapshot loads with both absent, and
reproduction over it is a refusal: `snapshot predates schema 1.2`. The deploy target
and any ATP suite are **not** inputs of this slice (§5).

### 1.2 The supported shape — checked in this order; 1–4 refuse, 5 degrades

The workflow file is read from the **restored tree** at the checkout SHA (§1.3), at
`workflow_path` — never from `master`, never from the log.

1. Exactly one failed job in the snapshot. Several → refusal `several failed jobs`.
2. The job maps to exactly one workflow job: the job's name equals a job's `name:` or,
   without one, its key. No `strategy.matrix`, no `uses:` (reusable workflow), no
   `container:`/`services:`. Else refusal naming the construct.
3. **Step binding.** The workflow job's steps, in order, bind one-to-one to
   `all_steps` minus the runner's own `Set up job`, `Post *` and `Complete job`
   entries; each pair must agree by name — the step's `name:`, or `Run <first line of
   run:>`, or `Run <uses:>`. A count or name mismatch (a step skipped by `if:`, a
   composite action) → refusal `step binding failed at step <n>`.
4. Exactly one step is a `run:` whose single line parses as a supported build (§4),
   and that step is the failed step. Else refusal: `failed step is not a supported
   build` or `several build steps`.
5. Every step before the build is either an `actions/checkout` step or a step on the
   **inert list**: a `run:` of `echo`/`ls`/`pwd`/`docker version`/`docker info` with
   no redirection, or a `uses:` of `docker/setup-buildx-action`. Any other step is a
   potential transformer of the context → the tree is an **approximation** with that
   step named (§1.3), not a refusal.

### 1.3 How exact the restored tree is

"Exact" is a statement about the **tree the builder received**, not about the
environment, and it is earned from four sources: the workflow file (§1.2), the step
list (`all_steps`), the checkout step's log, and the restored tree itself.

**The actual checkout SHA.** `actions/checkout` prints
`[command]/usr/bin/git log -1 --format=%H` followed by the full 40-hex SHA on the next
line (verified in all three committed snapshots, `tests/fixtures/runs/*.json`). That
pair, found **exactly once** in the job's text, is the actual SHA. Found zero or
several times → the SHA is `unknown`: the tree is restored at `head_sha` and the
restoration is an approximation with `checkout SHA not established from the log`. An
actual SHA that differs from `head_sha` → the tree is restored at the actual SHA and
the difference is recorded.

| Restoration | Conditions | Recorded as |
|---|---|---|
| **exact** | all of: the actual SHA is established from the log; the checkout step sets none of `ref:`, `path:`, `sparse-checkout`, `lfs: true`, `submodules`; no step before the build outside the inert list (§1.2 #5); the build runs in the workspace root (no `working-directory`, no `defaults.run.working-directory`); the tree carries no `.gitattributes` using `export-ignore`, `export-subst`, `filter`, `ident`, `eol`, `text`, or `working-tree-encoding` (the tarball applies the first two, a checkout the rest — either way the builder may have seen other bytes); no `COPY`/`ADD` of the context root (`.`) or a root glob while the applicable ignore file (§3.2) does not exclude `.git` — a checkout has a `.git` directory the tarball lacks | `restoration: exact` |
| **approximation** | any condition above false or unknown | `restoration: approximation` with every unmet condition listed |
| **unavailable** | the tree at the chosen SHA cannot be fetched (commit unreachable, no access) | `restoration: unavailable` — reproduction stops; the reading layer's outcome stands |

Filters, smudge/clean drivers and hooks from the repository are **never executed** to
reach `exact`; their presence is an approximation. The tree is fetched as the forge's
tarball of the commit (`repos/{repo}/tarball/{sha}` through the same `gh api`
chokepoint), never by running the workflow's checkout. An approximation can still
yield findings — a syntax error is a syntax error in any tree — but never an `exact`
claim.

### 1.4 Storage (owner's decision, versioned per try)

```
.deployer-runs/<run-id>/reproduction/attempt-<n>/
  source/            restored tree, read-only after restoration, shared by tries
  source.json        repo, run id, attempt, chosen SHA, how it was chosen
  tries/<seq>/       seq = 001, 002, …; a new try never overwrites an old one
    context/         a copy of source/ — what the build receives, unfiltered
    manifest.json    §6 document for this try
    build.stdout, build.stderr, check.stdout, check.stderr
```

`source/` is reused by a later try only when `source.json`'s SHA matches; otherwise a
new attempt directory is refused as a conflict (exit 2) rather than silently replaced.
Kept until the operator deletes it — no automatic TTL. `context/` is `source/` as is:
the `CONTEXT_IGNORE` stripping of `verify`'s `_isolated_context` (`verify.py:88–115`)
is **not** applied, because CI's builder received the tracked `.env` or `.envrc` too;
that is also why remote hosts are refused (Non-goals).

## 2. Backend and frontend — capabilities are detected, not inferred

Three different things, kept apart:

- **This repo's parser** (`parse_dockerfile`, `verify.py:119–142`) today returns
  `(instruction, args)` pairs only — no line numbers, no diagnostics. §3.1 extends it
  with source line spans and a closed list of syntax checks, and nothing more.
- **The builder's own check** — `docker build --check` — runs only in the runtime
  phase under `--reproduce` (§4), never in the offline phase. It needs a builder
  (Buildx ≥ 0.15 with the Dockerfile 1.8 frontend, per Docker's build-checks
  documentation) and may fetch image metadata or an external frontend, so it is
  neither offline nor container-free. **Podman** has no `--check`; on a Podman-only
  machine it is `skipped: backend has no build check`.
- **The actual build** (§4) remains the primary way to learn what the builder does
  with the file.

The manifest records the detected backend (docker / podman, version), Buildx version
where present, and the Dockerfile's `# syntax=` directive. When the directive names
an external frontend, the parser's syntax findings are recorded as `observation`
status, not `failed` — the parser does not model that frontend.

**Reading `--check` output.** Exactly four readings, from exit code and output:

| Builder output | Recorded as |
|---|---|
| exit 0 | `passed`; any `WARNING: <Rule>` lines kept as lint observations |
| nonzero with `WARNING: <Rule> - <text>` lines and no parse error | `passed` for syntax; each rule a **lint observation** (e.g. `JSONArgsRecommended`) — never a syntax finding |
| nonzero with `dockerfile parse error on line <N>: <text>` | syntax finding `line N: <text>`, source `builder` |
| anything else — command missing, builder unreachable, frontend fetch failure, unrecognised output | `skipped` with the raw output attached |

**Parser vs builder, syntax only.** Both found an error at the same line → one
finding, both sources cited. Only the builder found one → the builder's finding
(it is the authority; the parser does not model everything). Only the parser found
one and the builder check `passed` → the finding's status is `inconclusive`, both
results attached. The builder check `skipped` → the parser's result stands and says
the builder check did not run.

## 3. Deterministic checks (offline, over `context/`)

These never start a container and never call a builder. Each is `passed`, `failed`
(with a finding), `skipped` (with a reason) or `observation`.

### 3.1 Syntax — a closed list

The parser keeps each instruction's first and last source line and runs exactly these
checks; each finding names its line and the check that produced it:

1. the first instruction, after comments, parser directives and `ARG`s, is `FROM`;
2. `FROM` has one argument, or three with `AS` as the second (after an optional
   `--platform=<p>` flag) — this is what makes `polygon/run-5`'s
   `FROM python:3.12-slim extra` a finding;
3. every instruction keyword is one of Dockerfile's documented instructions;
4. a line continuation does not end the file.

"No finding" is reported as `no finding among checks 1–4`, never as "valid syntax".
Heredocs are recognised only to skip over them; their bodies are not checked.

### 3.2 COPY/ADD sources — against the CI side's ignore rules

The question is what **CI's** builder received, and CI's builder is `docker build`
(the only supported command, §4). So the ignore file is chosen by Docker's rule: a
Dockerfile-specific `<Dockerfile-name>.dockerignore` next to the Dockerfile, else the
root `.dockerignore`, else none. The chosen file is recorded with every finding.

Supported patterns: literal paths, `*`, `?`, `**`, and leading `!` exceptions, with
Docker's documented last-match-wins order. A file using anything else (character
classes, escapes) → this check is `skipped: ignore pattern not modelled: <pattern>`.

Every local source of every `COPY`/`ADD` is resolved against `context/` minus the
ignored paths. Findings: `source <path> absent from the context`, `source <path>
excluded by <file> rule <line>`; an empty glob expansion is a finding. Remote `ADD`
sources (URLs, git refs), `COPY --from`, heredocs and any form the parser does not
model are `skipped`, never reported as absent local files.

The **local** backend may choose differently: Podman prefers `.containerignore` when
it exists. When the local backend is Podman and the tree has a `.containerignore` or a
Dockerfile-specific ignore file, the §7 dimension `ignore file` is `differs` or
`unknown`, and the local build can reach at most `reproduced with differences`.

### 3.3 `--from` references

`--from=<name>` naming a stage of this Dockerfile is recorded as resolved; any other
`--from` is recorded as an external image dependency. Neither is checked offline.

## 4. The build — through its own adapter, not `verify`'s L2

### 4.1 The supported build configuration

The build step's `run:` line (from the workflow file, §1.2) is tokenised and accepted
only in this shape:

`docker build [--file|-f <path>] [--build-arg K=V]* [--platform <p>] [--tag|-t <t>] <context>`

with the context and `--file` resolving inside the workspace, no shell operators, no
variable or `$(...)` expansion, no `--secret`/`--ssh`/`--mount`/`--network`/`--pull`/
`--no-cache`, no `buildx build`. Anything else — a chain, a script, an unknown flag,
an expansion — is a **refusal**: `reproduction refused: unsupported build
configuration: <what>`. The refusal is a first-class result; §3's findings and the
reading layer's outcome still stand.

### 4.2 The adapter

A new function in a new module (`reproduce.py`), calling `runtime.container_run` — the
single container-subprocess chokepoint — and **not** `verify._build`, which pipes the
Dockerfile through stdin (`-f -`), adds a memory limit, has no build args or platform,
and returns a classified `CheckResult` with only the output's tail
(`verify.py:1555–1604`). The adapter's command:

`<tool> build --file context/<path> [--build-arg K=V]* [--platform <p>] --tag localhost/deployer-repro-<run-id>-<seq> context/<dir>`

- the Dockerfile is passed **by path**, so the Dockerfile-specific ignore file applies
  as it did in CI;
- CI's own `-t` value is recorded, never used; the adapter's tag is its own;
- no memory limit (CI had none); `--build-timeout` applies;
- `--container-host` is refused (Non-goals).

It records: the exact argv, backend tool and version, host architecture, exit code
(`null` on timeout or launch failure, with that reason), and **full** stdout and
stderr to files in the try directory. It returns no `FailureKind`.

### 4.3 Cleanup — what is and is not covered

After the build, if the adapter's tag exists it is removed with `rmi -f`, and the
**return code is read**: `removed`, `failed`, or `not_attempted` (no image was
tagged). A timeout of the removal is `failed`. Intermediate layers and build cache of
a failed build are **not** removed and the manifest says so; reclaiming them is the
operator's `docker/podman system prune` (`todo://deployer/bench-run-dir-litter`). No
container is started in this slice, so there is no container to clean up.

### 4.4 Network operations are named individually (owner's decision)

With an explicit `--reproduce` the backend may fetch base images it lacks through its
normal pull path; no tag is force-refreshed. Image fetches, network access from `RUN`
steps and the resolution of an external `# syntax=` frontend are three different
operations; the absence of an image pull is not "no network", and no offline mode is
promised. The manifest records the digest of every image the build used where the
backend exposes it; CI's digest is taken from BuildKit's `resolve … @sha256:` lines
when the log has them, else `unknown` — equality is never assumed.

## 5. No run in this slice

Rev 3 ran the image when a deploy target declared a run intent, healthcheck or ATP
suite. Rev 4 removes it: in the supported shape the failed CI step **is** the build,
so the build is the whole reproduction; a run would answer a question CI never asked,
and it needs a deploy target and suite the snapshot does not carry. A local build that
succeeds yields `not reproduced` (§7) — never "behaviour verified". Running the image
returns as its own slice, with its own input contract, when a supported CI shape fails
after the build.

## 6. The result document

`deployer diagnose <run> --reproduce` adds a `reproduction` section to the verdict
document (verdict schema 1.1 → 1.2, additive; without `--reproduce` the document is
unchanged). Each try's `manifest.json` holds the same section.

```json
{
  "reproduction": {
    "status": "attempted",
    "refusal": null,
    "restoration": {"state": "exact", "sha": "d6e330fd…", "sha_source": "checkout_log",
                    "unmet": []},
    "binding": {"job_id": 106597702099, "workflow_job": "build",
                "build_step": 3, "dockerfile": "Dockerfile", "context": "."},
    "environment": {"backend": "podman", "backend_version": "5.7.0",
                    "buildx_version": null, "host_arch": "arm64",
                    "syntax_directive": null},
    "checks": [
      {"check_id": "copy_sources", "status": "failed",
       "finding": "source docs/setup.md absent from the context",
       "location": {"file": "Dockerfile", "lines": [11, 11]},
       "evidence": [{"kind": "tree", "ref": "source/docs"}],
       "reason": null}
    ],
    "build": {"argv": ["podman", "build", "--file", "…"], "exit_code": 125,
              "timed_out": false, "failed_instruction": {"lines": [11, 11],
              "bound_by": "step_text"},
              "stdout": "tries/001/build.stdout", "stderr": "tries/001/build.stderr",
              "cleanup": "not_attempted"},
    "comparison": {"state": "reproduced_with_differences",
                   "ci_instruction": {"lines": [11, 11], "bound_by": "buildkit_error_block"},
                   "signature_match": "not_compared",
                   "dimensions": {"backend": "differs", "host_arch": "unknown",
                                  "base_image_digests": "unknown",
                                  "ignore_file": "same"}}
  }
}
```

Invariants: `status` ∈ `attempted | refused | unavailable | not_requested`; a
`refused`/`unavailable` document has `refusal` set and no `build`; a `failed` check
always has a `finding` and at least one `evidence` entry; `exit_code` is `null` iff
`timed_out` or the launch failed; no field anywhere carries a `FailureKind`. The
check result is its own type — `ReproductionCheck` — because `CheckResult`'s
`enforce_failure_taxonomy` requires a class on every `FAILED`; `CheckResult` is
neither loosened for it nor fed a placeholder `UNKNOWN`.

**Headline findings** printed by the CLI, one per line, e.g.:
`syntax error at line 1: FROM takes one or three arguments (parser; builder check
skipped: podman)` · `COPY source docs/setup.md absent from the context (exact)` ·
`build fails at Dockerfile:15 locally; CI failed at Dockerfile:15; output signature
matches` · `reproduction refused: unsupported build configuration: shell chain`.

**Exit codes.** `--reproduce` never changes the reading layer's exit code
(`cli.py:58–65,461–483`: 3 unclassified, 4 evidence unavailable, 5 adapter refusal,
2 error), so a caller that scripts on it is unaffected; the reproduction result lives
in the document. Two additions, both **2**: `--reproduce` combined with
`--container-host`, and a try directory that cannot be created or conflicts
(§1.4). A missing container runtime is not an error: the result is `refused` with
`no container runtime`, and the exit code is the reading layer's. On an adapter
refusal (5) reproduction is `not_requested` — there is no failed run to reproduce.

## 7. CI versus local — one state, by a fixed order

### 7.1 Instruction identity

An instruction is identified by its **Dockerfile source line span** — the one thing
both sides can name:

- **CI side:** BuildKit's error block `Dockerfile:<N>` followed by the `>>>`-marked
  lines gives the span (all three committed snapshots carry it: lines 11, 7 and 15).
  The block must occur exactly once in the job's text. It is attributed to the build
  step because the supported shape has exactly one build step and it is the failed
  one (§1.2) — `bound_by: buildkit_error_block`. Absent or repeated → unbound.
- **Local side:** Podman's `STEP k/n: <instruction>` last printed before the error, or
  BuildKit's block on Docker, is matched against the parser's instructions by
  whitespace-normalised text; exactly one match gives the span —
  `bound_by: step_text` / `buildkit_error_block`. None or several → unbound.
- A parse failure has no executed instruction: its identity is `parse at line <N>`.
  BuildKit states the line. Podman does not — it prints `Error: FROM requires either
  one argument, or three: …` with no line and exits 125 (observed on this machine,
  Podman 5.7.0). A local failure with no `STEP` line printed is therefore bound to
  `parse at line <N>` only when §3.1 has exactly one syntax finding, at line N —
  `bound_by: parser_finding`, recorded so the reader sees the binding is ours, not
  the builder's. Otherwise it is unbound.

### 7.2 The output signature

For a failed `RUN`, the signature is the last non-empty line of that instruction's
own output before the builder's error line, with timestamps, ANSI codes and BuildKit's
`#<n> <t.ttt>` prefix stripped. It is deliberately weak and the document says so: for
`run-3` it is `FAILED (failures=1)`, which any single failing unittest prints. Equal
signatures show that the instruction ended the same way, not that the same test
failed; both full outputs are attached for the reader, and no stronger claim is made
from them. Only program output is compared — builder messages
differ by backend and are never compared across backends. On the same backend, the
builder's error line is compared too. For a parse failure or a non-`RUN` instruction
across different backends: `signature_match: not_compared`.

### 7.3 The states, evaluated in this order; the first that applies wins

1. `not_attempted` — refusal, restoration unavailable, or the build could not launch.
2. `inconclusive` — the build timed out; or the CI or local instruction is unbound.
3. `not_reproduced` — the local build exited 0.
4. `different_failure` — both bound, the spans differ. **Not a reproduction.**
5. `same_instruction_different_output` — same span, signatures compared and unequal.
   **Not a reproduction.**
6. `reproduced` — same span; signatures equal or `not_compared` only because the
   backends match; every §7.4 dimension observed `same`.
7. `reproduced_with_differences` — same span; signatures equal or `not_compared`;
   some dimension `differs` or `unknown`, listed.

### 7.4 Dimensions

Each is `same`, `differs` (values from both sides) or `unknown` (a side missing):
`backend` (CI is docker by §4.1; local detected), `host_arch` (CI side `unknown` in
this slice: no runner line in the committed logs states it, and an `amd64` in apt's
output is program output, not a runner fact), `base_image_digests` (§4.4), `ignore_file` (§3.2),
`restoration` (`exact` counts as `same`; an approximation is `unknown`). A difference
is never declared without values from both sides.

## 8. Acceptance — committed cases with expected results

Three layers, kept apart; the first is the proof, the other two support it.

**A. Offline regression cases (committed, run by `uv run pytest`, no container, no
builder, no network).** Each case lives in `tests/fixtures/reproduction/<case>/`:

- `snapshot.json` — a schema-1.2 snapshot of the existing run, re-fetched read-only
  from GitHub (no new dispatch) and anonymised as the 1.1 fixtures were;
- `tree/` — the tree at the actual checkout SHA, vendored from the pinned commit;
- `PROVENANCE.md` — run URL, SHA, how the tree was obtained, any injected change;
- `local.stdout`/`local.stderr`/`local.exit` — the output a fake `container_run`
  replays for the build (recorded once from a real Podman build, §8.C);
- `expected.json` — the expected `restoration`, `checks`, `build.failed_instruction`
  and `comparison` sections of §6.

| Case | Source | Expected |
|---|---|---|
| `run-1` | `d6e330f`, run 35680991093 | exact; `copy_sources` failed: `docs/setup.md` absent, lines 11; comparison `reproduced_with_differences` (backend differs) |
| `run-2` | `37242cc` | exact; §3 no findings; CI and local both fail at lines 7–9; signature `E: Some index files failed to download. …` compared; the case records that a reproduced network failure carries **no** artifact finding |
| `run-3` | `43d7c39` | exact; §3 no findings; both fail at line 15; signature `FAILED (failures=1)` matches; `reproduced_with_differences` |
| `run-5` | `937d465` (branch `polygon/run-5`), evidence `9e89daa` | exact; syntax check 2 failed at line 1 (parser); builder check `skipped`; local exit 125, no `STEP` line, bound `parser_finding`; comparison `reproduced_with_differences`, signature `not_compared` |

Negative cases, each derived from `run-1` with **one** recorded change:
`shell-chain` → refused `unsupported build configuration`; `checkout-ref` →
approximation `ref:`; `generating-step` → approximation naming the step;
`gitattributes-subst` → approximation `export-subst`; `copy-root-with-git` →
approximation `.git`; `no-checkout-sha` → approximation `checkout SHA not established`;
`containerignore` → `ignore_file: differs`, at most `reproduced_with_differences`;
`other-line` → `different_failure`; `other-output` → `same_instruction_different_output`;
`unbound-ci` (error block removed) → `inconclusive`; `several-failed-jobs`,
`matrix`, `snapshot-1.1` → the named refusals. `--check` reading is covered by three
recorded outputs (lint warning, parse error, builder unreachable) → the §2 table.

**B. Contract tests with the fake runtime.** Argv shape (file by path, own tag, no
memory limit), timeout → `exit_code: null`, cleanup return code read, remote host
refused. These prove the adapter's contract, not reproduction.

**C. Manual local run (this machine, Podman; secondary).** `run-1`, `run-2`, `run-3`,
`run-5` rebuilt for real; the outputs become the `local.*` files of A, and the PR
records that they were produced this way. A manual run never substitutes for A.

**Dropped from rev 3:** the uv-minimal bench failure. Its failing Dockerfile was never
preserved (`corpus/synthetic/uv-minimal/fixture.Dockerfile` already copies `src`
before `uv sync`), so naming the case pins nothing; it motivated this design and is
not evidence for it.

No new live dispatches and no paid authoring or benchmark are part of this acceptance.

## 9. Decisions, recorded

**Owner, 2026-09-22:**

| Question | Decision |
|---|---|
| Where the restored tree lives, for how long | under `.deployer-runs/<run-id>/reproduction/`, `source/` (restored, unmodified) separate from the build context; kept until explicit operator cleanup, no automatic TTL; images the reproduction creates cleaned, result recorded (layout versioned per try in §1.4) |
| Fetching images | an explicit `--reproduce` allows the backend's normal fetch of missing images; no forced tag refresh; no offline promise; actual digests recorded, `unknown` where CI's are missing; the saved tree is never modified when the sandbox is prepared |
| ENVIRONMENT candidate | not in this slice — the network failure of a specific step and the comparison results are recorded as facts, without a presumed class |

**Rev 4 scope decisions (author's, open to the review):** no image run (§5); local
backend only; one failed job with one build step that is the failed step; the
reproduction never changes the exit code; uv-minimal dropped from acceptance.
