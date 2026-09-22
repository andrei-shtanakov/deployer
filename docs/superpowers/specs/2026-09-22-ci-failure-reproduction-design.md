# CI-failure reproduction — design (rev 3 of the diagnosis line)

**Status:** DRAFT, revised after the owner's review of 2026-09-22
(`_cowork_output/deployer-reproduction-draft-review-2026-09-22.md`); next: external
review by exact SHA, then a plan. No code exists for this design.
**Relation to the 2026-09-21 spec:** supersedes its causal half (§3 outcomes, §5
catalogue, §8.2 "three established classes"). Its reading layer — forge,
completeness, observations, CLI — is this design's input, under the reduced contract
stated in that spec's Addendum.

## Why this revision exists

PR-3's first design tried to establish the *cause* of a failed run from phrases in its
log. Eight review rounds showed the same thing from eight angles: a phrase names a
symptom, not a cause — "file not found" says a file is missing, not why (a wrong COPY,
a different build context, a file the project never generated) — and every exception
added for warnings, tracebacks, buildkit framing or quoting produced the next
counter-example. The owner stopped the catalogue on 2026-09-22.

The data that *can* ground findings about an authored Dockerfile already exist in
this repo: the artifact, its build context, deterministic checks over both (L1), and a
container runtime that builds and runs images (L2, `ContainerRuntime`). The
uv-minimal benchmark failure was explained by one local `podman build`, not by a
marker. This revision moves the work onto those data. Log text remains an *input*
(which step failed, what the tool printed) and is never the sole basis of a result.

## Non-goals

- **Naming a culprit.** The first slice returns **findings with status and evidence,
  and no `FailureKind`.** Deriving AUTHORING / ENVIRONMENT / PROJECT from reproduction
  results is a separate, later task with its own acceptance; this design does not
  pre-decide it (§6).
- Diagnosing arbitrary CI. Reproducible here: the authored Dockerfile/workflow pair
  and the checks the deploy target declares. Network, third-party services and a
  project's own suite are exercised only insofar as L2 already runs them.
- Executing anything taken from a log. The command line the runner printed is
  evidence, never an instruction (§4).
- Fixing the artifact — `todo://deployer/ci-fix-authoring` stays a later slice; this
  one gives it a reproducible red state to start from.
- A second Docker frontend. This repo's parser is used for what it already models;
  the builder is the authority on what the builder accepts (§2).

## 1. Inputs, and how exact the restored tree is

The reading layer supplies a `FailedRun` snapshot: repository, run id, attempt,
`head_sha`, failed jobs/steps, logs and annotations with completeness states.

**`head_sha` is not, by itself, the build context.** "Exact" is a statement about the
**tree**, not about the environment, and it is earned, not assumed:

| Restoration | Conditions (all must be established from the workflow file and the step list) | Recorded as |
|---|---|---|
| **exact** | the checkout step's *actual* SHA (from its log line `HEAD is now at …` / the `actions/checkout` output) equals `head_sha`; no `ref:`, `sparse-checkout`, `lfs: true`, `submodules` on the checkout step; the build runs in the repository root (no `working-directory`); no step between checkout and the build writes into the context; the build command's context is `.` and its `--file` is a path in the tree; the restored archive matches the checkout in the ways that matter to the builder — file modes and symlinks preserved, no `export-ignore` attributes in the tree (a `git archive`-style export drops those paths, a checkout does not) | `context: exact` |
| **approximation** | any condition above is unknown or false: a generated file, an unknown transforming step, a non-`.` context, `ref:` set, `working-directory` set, sparse/LFS/submodules, `export-ignore` present, the checkout log missing | `context: approximation` with every unmet condition listed |
| **unavailable** | the tree at the actual checkout SHA cannot be fetched (commit unreachable, no access) | `context: unavailable` — the verdict stops at the reading layer's outcome plus this fact |

The first slice supports the exact case and names its unmet conditions honestly;
sparse checkout, LFS, submodules and a non-root `working-directory` are **explicitly
unsupported** (→ approximation with the reason), not silently handled. The tree is
obtained as an archive of the commit through the forge (versioned like the snapshot),
never by executing the workflow's checkout logic. Every result of §3–§5 carries the
restoration state; an approximation can still yield a finding (a syntax error is a
syntax error in any tree) but never an `exact` claim.

**Storage (owner's decision).** Each reproduction lives under
`.deployer-runs/<run-id>/reproduction/` with a `manifest.json` (repository, run id,
attempt, actual checkout SHA, restoration state and unmet conditions, backend/frontend
capabilities, parameters); the restored **source tree** (`source/`, read-only after
restoration, never modified by sandbox preparation) is kept separate from the
**build context** the sandbox uses (`context/`, a copy the checks and builds operate
on). Kept until the operator deletes it — no automatic TTL. Temporary containers and
the images the reproduction creates are removed by the existing L2 cleanup order, and
the cleanup result is recorded in the manifest.

## 2. Backend and frontend — capabilities are detected, not inferred

Three different things, kept apart:

- **This repo's parser** (`parse_dockerfile`, extended only where a check of §3 needs
  it) reads instructions offline. It is not a Dockerfile frontend and does not claim
  to be; where it does not model a construct, the check that needs it is `skipped`
  with the reason.
- **The builder's own check** — `docker build --check` — is a check *by the builder*:
  it does not execute build steps, but it needs a builder (Buildx ≥ 0.15 and the
  Dockerfile 1.8 frontend, per Docker's documentation of build checks), so it is not an
  offline or container-free check. Its availability is **detected** the way hadolint
  and actionlint are today: present with a comparable version → its result is
  recorded; absent → `skipped`. **Podman** builds with Buildah and has no `--check`;
  on a Podman-only machine this check is `skipped` and every syntax result rests on
  the parser alone and says so.
- **The actual build** (§4) remains the primary way to learn what the builder does
  with the file.

The manifest records the detected backend (docker / podman, version), Buildx and
frontend availability, and the Dockerfile's `# syntax=` directive if any. When the
directive names a frontend this repo's parser does not model, the parser's findings
are recorded as observations, not findings.

## 3. Deterministic checks (offline, over the restored context)

Over `context/` and the Dockerfile the workflow builds:

1. **Syntax** — this repo's parser; the builder's check when available (§2). Finding:
   `syntax error at line N: <parser message>`. When both ran and disagree, both
   results are recorded and the finding is `skipped` as inconclusive.
2. **COPY/ADD sources** — every local source of every `COPY`/`ADD` is resolved
   against `context/` after the builder's ignore rules: a **Dockerfile-specific
   ignore file** (`<Dockerfile-name>.dockerignore` next to the Dockerfile) takes
   precedence over the root `.dockerignore`, per Docker's build-context
   documentation; patterns, `!` exceptions and directory semantics as the builder
   applies them. Findings: `source <path> absent from the context`, `source <path>
   excluded by <ignore-file> rule <rule>`. Globs are expanded against the tree; an
   empty expansion is a finding. **Remote `ADD` sources (URLs, git refs), `COPY
   --from`, heredocs and any form the parser does not model are `skipped`, never
   reported as absent local files.**
3. **`--from` references** — a `--from=<name>` naming a stage of this Dockerfile is
   recorded as resolved to that stage; a `--from=<image>` is recorded as an external
   dependency; neither is checked offline.
4. **Build command** — the workflow's build line is *parsed* (§4), not run; the
   `--file` and context it names are compared with what the checks used, and a
   mismatch is a finding.

Each check reports `passed`, `failed` (with the finding) or `skipped` (with the
reason). **These results do not use `CheckResult`:** that type's
`enforce_failure_taxonomy` requires a `failure_kind` on every `FAILED`, and a
reproduction finding carries no class. A small separate type is introduced —
`ReproductionCheck(check_id, status, evidence: list[str], reason: str | None)` — and
`CheckResult` is neither loosened for it nor fed a placeholder `UNKNOWN`.

## 4. Reproduction through L2 — supported configurations only

`deployer diagnose --reproduce` (name provisional) builds the image through
`ContainerRuntime` exactly as `verify` does today, with one difference: the
configuration comes from the **parsed** workflow, and only a supported shape is
accepted:

- accepted: `docker build [--file <path>] [--build-arg K=V]* [--platform <p>]
  [-t <tag>] <context>` where the context is inside `context/`, with no shell
  operators, no variable or `$(...)` expansion, no `--secret`/`--ssh`/`--mount`, no
  `--network`, no `--no-cache`/`--pull` flags this slice does not model;
- anything else — a chain, a script, an unknown flag, an expansion — is a
  **refusal**: `reproduction refused: unsupported build configuration: <what>`. The
  verdict keeps the reading layer's outcome and §3's findings; the refusal is a
  first-class result, not an error path.

The command printed in the log is never executed.

**Network operations are named individually (owner's decision).** With an explicit
`--reproduce` the backend may fetch base images it does not have, through its normal
pull path; no tag is force-refreshed. Image fetches, network access from `RUN` steps
and the resolution of an external `# syntax=` frontend are **three different
operations** — the absence of an image pull is not "no network", and this design does
not promise an offline mode; a pull policy / offline mode is a separate later mode.
The manifest records the **digests** actually used for every image where the backend
exposes them; where the CI run's log carries no digest, equality with CI's image is
recorded as `unknown`, never assumed.

The reproduction records the exact `ContainerRuntime` invocation, backend tool and
version, host architecture, exit code and captured output, next to the CI step's own
conclusion and output, so both sides are visible to a later reader.

## 5. Running the image — only what is declared

A successful local build does **not** license running the container. After a build:

- if the deploy target declares a **run intent**, a **service healthcheck** or an
  **ATP smoke suite**, L2 runs exactly that, as `verify` does today, and records the
  result with its evidence;
- if the failed CI step was itself a declared check inside the build (a `RUN` that
  executes the project's tests), the build's own failure at that step *is* the
  reproduction and no further run happens;
- otherwise the result is `build succeeded; behaviour not verified` — stated, not
  implied.

## 6. The verdict: findings, status, evidence — no class

The verdict document (its own `verdict_schema_version`, bumped additively) gains a
`reproduction` section: the manifest facts (§1–§2), the §3 checks, the L2 build/run
results or the refusal (§4–§5), the comparison with CI (§7). Its headline is a list
of **findings**, each with a status and the evidence that produced it:

- `syntax error at line 1: FROM requires either one or three arguments` — parser
  (builder check skipped: Podman)
- `COPY source docs/setup.md absent from the context` — exact restoration
- `build reproduced: fails at step 7 (RUN uv sync --frozen), exit 1` — output attached
- `declared test reproduced: fails inside the build at step 9` — output attached
- `build succeeded locally; CI failed at the same step` — differences listed (§7)
- `reproduction refused: unsupported build configuration: shell chain`
- `context unavailable: commit not reachable`

**The first slice attaches no `FailureKind` to any finding.** A missing COPY source
in an exact tree proves that the instruction and the tree disagree — not which side
moved, nor who authored what; a build that succeeds while a declared check fails
proves the check failed — not that the image's command, dependencies or configuration
are right. Turning such findings into causal classes needs its own evidence design and
is a later task; until then the reading layer's heuristic classes (2026-09-21 Addendum)
and these findings are reported side by side, and neither is a basis for an automatic
action.

## 7. CI versus local — what was observed, what is unknown

Local reproduction may differ from GitHub Actions: architecture (this bench is arm64;
hosted runners amd64), network egress, secrets, tool versions, base-image digests
resolved at different times. The verdict records, per dimension, either the observed
values on both sides or `unknown` — a difference is never declared observed without
data from both sides. The comparison states exactly one of:

- `reproduced` — the same step fails locally with the same tool message;
- `reproduced with differences` — the same step fails; listed dimensions differ or
  are unknown;
- `different failure` — a build failed locally, but at a different step or with a
  different message; **this is not a reproduction of the CI failure**;
- `not reproduced` — CI failed, the local build/run passed; differences and unknowns
  listed;
- `not attempted` — refusal, or context unavailable.

## 8. Acceptance — offline first; no live dispatches, no paid runs

- **Offline (primary):** fixtures = restored contexts plus snapshots of the existing
  live runs: `polygon/run-5` (injected `FROM` syntax error; evidence on
  `rescue/pr3-negchecks-tail` @ `9e89daa`), `polygon/run-1` (COPY of a missing path),
  `polygon/run-3` (the project's test failing inside the build), and the uv-minimal
  bench case with its Dockerfile and hatchling output. Each replays to its §3
  findings without a container; the L2 half is exercised with the fake runtime the
  existing tests use.
- **Local reproduction (secondary, this machine, Podman):** run-5 → syntax finding
  (parser; builder check `skipped`, stated); run-1 → `COPY source absent`, exact
  restoration; run-3 → `declared test reproduced: fails inside the build` with the
  assertion output as evidence — **the criterion is the reproduced failure of the
  declared test, not a PROJECT class**; uv-minimal → build fails at the install step
  with the hatchling output, matched against the bench report.
- **Refusal:** a fixture workflow with a shell chain → `reproduction refused`.
- **Approximation:** a fixture workflow with `ref:` on checkout, and one with a
  generating step between checkout and build → `context: approximation` with the
  unmet condition named; findings still reported.
- **Different failure:** a fixture where the local build fails at another step →
  `different failure`, not `reproduced`.
- No new live dispatches and no paid authoring or benchmark are part of this
  acceptance.

## 9. Owner's decisions (2026-09-22), recorded

| Question | Decision |
|---|---|
| Where the restored tree lives, for how long | `.deployer-runs/<run-id>/reproduction/` with a manifest; `source/` (restored, unmodified) separate from `context/` (what the sandbox builds); kept until explicit operator cleanup, no automatic TTL; containers/images cleaned by the existing L2 order, result recorded |
| Fetching images | an explicit `--reproduce` allows the backend's normal fetch of missing images; no forced tag refresh; no offline promise; actual digests recorded, `unknown` where CI's are missing; the saved archive is never modified when the sandbox is prepared |
| ENVIRONMENT candidate | not in this slice — the network failure of a specific step and the comparison results are recorded as facts, without a presumed class |
