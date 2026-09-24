# CI-failure reproduction — design (rev 5 of the diagnosis line)

**Status:** DRAFT rev 5. Revised after the external review of rev 4 at `567bc6d`
(`../../../../_cowork_output/deployer-reproduction-spec-rev4-review-2026-09-23.md`),
the external review of rev 3 at `ce19753`
(`../../../../_cowork_output/deployer-reproduction-spec-review-2026-09-23.md`) and the
owner's review of 2026-09-22
(`../../../../_cowork_output/deployer-reproduction-draft-review-2026-09-22.md`). All
three are dev-only workspace files, absent from clones; every decision they made is
restated in this document where it applies. The owner-ordered **targeted** review of
the rev 4 → rev 5 diff (2026-09-23,
`../../../../_cowork_output/deployer-reproduction-spec-rev5-targeted-review-2026-09-23.md`)
passed subject to four minor fixes, applied in this text, with no further review
required. Next: a plan. No code exists for this design.
**Base:** the reading layer as merged on `master` at `770066d` (PR #72): `forge.py`,
`diagnose.py`, `deployer diagnose`; **snapshot schema 1.2** (`forge.py:22`, which
added `Evidence.level`), verdict schema 1.1 (`diagnose.py:50`).
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
container runtime that builds images. This design moves the work onto those data.
Log text remains an *input* (which step failed, which instruction, what the tool
printed) and is never the sole basis of a result.

Rev 4 narrowed the slice; rev 5 narrows it further where the rev-4 review found a
bypass (owner, 2026-09-23): the build context is `.` only; the workflow and the
checkout must both be at `head_sha`, else refusal; the inert steps are exact strings;
only a confirmed-local container endpoint is used; any `.gitattributes` is an
approximation. Errors that narrowing cannot remove are fixed explicitly: snapshot
schema 1.3, the comparison order, the binary tarball, the real `--check` output, the
negative fixtures, the exit-code wording and Podman's build containers.

## Non-goals

- **Naming a culprit.** The first slice returns **findings with status and evidence,
  and no `FailureKind`.** A reproduced failure says the failure recurs, not whose it
  is: `polygon/run-2`'s unroutable apt mirror reproduces locally just as faithfully as
  `polygon/run-1`'s missing COPY source (§8). Deriving classes is a separate, later
  task with its own acceptance.
- **Running the image.** The slice reproduces a failed *build step*; the build is the
  reproduction. Running the built image needs a deploy target this input does not
  carry, and is a later slice (§5).
- Diagnosing arbitrary CI — only the supported shape of §1.2.
- Executing anything taken from a log or a workflow. The workflow's build line is
  *parsed* into a supported configuration; this repo then issues its own command (§4).
- Non-local container endpoints (§4.2): the context is built as restored, without the
  secret-stripping `CONTEXT_IGNORE` of `verify`, so it must not leave the machine.
- Fixing the artifact — `todo://deployer/ci-fix-authoring` stays a later slice.
- A second Docker frontend. This repo's parser checks a named, closed list of things
  (§3); the builder is the authority on what the builder accepts (§2).

## 1. Inputs and restoration

### 1.1 What the snapshot must carry (snapshot schema 1.3, additive)

Today's `FailedRun` (schema 1.2) carries no workflow path and no event, and
`FailedJob.steps` keeps only non-green steps (`forge.py:436–449`). The forge adds, from
data the adapter already fetches:

- `FailedRun.workflow_ref_path` — the run's `path` from `actions/runs/{id}` **as
  returned**, which may carry a ref suffix (GitHub's own example:
  `.github/workflows/build.yml@main`);
- `FailedRun.workflow_path` — `workflow_ref_path` with a trailing `@<ref>` removed
  (split on the last `@`); it must then start with `.github/workflows/` and end in
  `.yml`/`.yaml`, else reproduction refuses `workflow path not understood: <raw>`;
- `FailedRun.event` — the run's `event`;
- `FailedJob.all_steps` — every step of the job from the jobs listing: `number`,
  `name`, `conclusion`, in order.

Schema 1.2 → 1.3 is additive; older snapshots still load, with these fields absent.
Reproduction checks the **fields**, not the version string: any of the four absent →
refusal `snapshot lacks reproduction fields: <names>`. The existing 1.2 fixtures are
exactly such snapshots and are one of the negative cases (§8).

### 1.2 The supported shape — every check refuses

The checks run in this order; the first that fails ends reproduction with a named
refusal. None of them degrades to an approximation (that is §1.3's job).

1. **Event.** `event` is `push` or `workflow_dispatch`. For these events GitHub runs
   the workflow file of the commit it reports as `head_sha`; for `pull_request` and
   others the executed workflow and checkout come from a different commit (a merge
   ref, the base branch). Else refusal `event <e> not supported`.
2. **Actual checkout SHA = `head_sha`.** `actions/checkout` prints
   `[command]/usr/bin/git log -1 --format=%H` followed by the full 40-hex SHA on the
   next line (in all three committed snapshots, `tests/fixtures/runs/*.json:115,120`).
   That pair must occur **exactly once** in the job's text and name `head_sha`. Zero,
   several, or another SHA → refusal `checkout SHA not established` / `checkout at
   <sha>, run at <head_sha>`. The tree and the workflow file are both read at
   `head_sha`, so the workflow that ran and the tree that was built are the same
   commit's.
3. Exactly one failed job in the snapshot. Else refusal `several failed jobs`.
4. The job maps to exactly one workflow job: the job's name equals a job's `name:` or,
   without one, its key. No `strategy.matrix`, no job-level `uses:`, no
   `container:`/`services:`. Else refusal naming the construct.
5. **Step binding.** The workflow job's steps, in order, bind one-to-one to
   `all_steps` minus the runner's own `Set up job`, `Post *` and `Complete job`
   entries (names per GitHub's jobs API); each pair agrees by name — the step's
   `name:`, or `Run <first line of run:>`, or `Run <uses:>` (the runner's display
   name, `actions/runner` `ActionRunner.cs`). Else refusal `step binding failed at
   step <n>`.
6. **Checkout step.** Exactly one step before the build is `actions/checkout`, and
   its `with:` sets none of `ref`, `repository`, `path`, `sparse-checkout`, `lfs`,
   `submodules`, `fetch-depth`. Else refusal naming the input.
7. **The build step.** Exactly one step is a `run:` whose single line parses as the
   supported build of §4.1, and it is the failed step. Else refusal with §4.1's own
   reason when the line was a build but unsupported (`unsupported build
   configuration: shell chain`), otherwise `failed step is not a supported build` or
   `several build steps`.
8. **Working directory.** No `working-directory` on the build step and no
   `defaults.run.working-directory` on the job or workflow. Else refusal.

### 1.3 How exact the restored tree is

Once §1.2 passes, workflow, checkout and tree are one commit. "Exact" then claims
only that the **bytes the builder received** equal the restored tree:

| Restoration | Conditions | Recorded as |
|---|---|---|
| **exact** | all of: (a) every step between checkout and build is on the inert list below; (b) the Git tree at `head_sha` contains no `.gitattributes` file at any depth; (c) the extracted archive equals the Git tree listing — same paths, file modes (`100644`/`100755`/`120000`) and symlink targets — and the listing is not truncated; (d) no local `COPY`/`ADD` source may reach `.git` — none is `.`, none has a first path segment that can match `.git` (literally or as a glob), and none is a form this slice cannot read, including a source outside the closed alphabet of §3.2; the ignore file is **not** consulted for this condition (see the note below); (e) no `RUN --mount` of any type | `restoration: exact` |
| **approximation** | any condition false or unknown | `restoration: approximation` with every unmet condition listed |
| **unavailable** | the archive or the tree listing cannot be fetched or read (§1.4) | `restoration: unavailable` — reproduction stops; the reading layer's outcome stands |

**The inert list is exact strings, nothing else:** a `run:` whose whole value is one
of `ls`, `ls -la`, `pwd`, `docker version`, `docker info`, `docker buildx version`;
or `uses: docker/setup-buildx-action@<40-hex>` with no `with:`. No substitution, no
operator, no second line: `echo "$(touch x)"` is not on the list, so it is an
approximation naming the step.

Condition (b) looks at the **Git tree** (`repos/{repo}/git/trees/{sha}?recursive=1`),
not the archive: an `export-ignore` rule can remove itself and its targets from the
archive (GitHub builds archives with `git archive`), and the tree listing still shows
them. Any `.gitattributes` makes the tree an approximation, whatever it contains —
filters, `export-subst`, `eol` and the rest are never evaluated and never executed.
Condition (c) is what catches everything else the archive may have changed.

**Condition (d) is narrowed on purpose (owner, 2026-09-24).** An earlier text let
the applicable ignore file prove that `.git` stays out of the context. Four review
rounds of the implementation (#77) each produced a new counter-example to that proof
— an unmodelled pattern, a re-including negation such as `!.git/HEAD`, an unmodelled
`COPY` flag, a glob like `*/HEAD`, `COPY .git` with `!.git/config` — the same pattern
that stopped the causal catalogue. The slice therefore never claims the proof: any
source that may reach `.git` is recorded as `.git exclusion not proven: …` and the
restoration is an approximation. A `COPY --from` source and a heredoc source read
nothing from the context and do not count.

### 1.4 Fetching the tree — a binary path through the forge

`GhRunner.api` returns text decoded with `errors="replace"` (`forge.py:165–202`), which
does not preserve archive bytes. The forge gains a second method, `api_bytes`, on the
same chokepoint: `gh api repos/{repo}/tarball/{sha}` with `text=False`, the same
timeout and error mapping, and a size cap (`--max-archive-mb`, default 200); `gh`'s
HTTP client follows the endpoint's redirect to the archive. The tree listing uses the
existing text `api`. A fake runner for `api_bytes` asserts that bytes pass through
unaltered (§8.B).

Extraction uses Python's `tarfile` with the `data` extraction filter, which refuses
absolute paths, `..` components, links that point outside the destination and device
files, and keeps the executable bits. The archive must have exactly one top-level
directory (`<owner>-<repo>-<short-sha>/`), which is stripped. A refused member, a
second top-level entry, a truncated or unreadable archive, an exceeded cap →
`restoration: unavailable` with the reason. Nothing from the archive is executed.

### 1.5 Storage (owner's decision, versioned per try)

```
.deployer-runs/<run-id>/reproduction/attempt-<n>/
  source/            restored tree, read-only after restoration, shared by tries
  source.json        repo, run id, attempt, head_sha, archive size, tree listing
  tries/<seq>/       seq = 001, 002, …; a new try never overwrites an old one
    context/         a copy of source/ — what the build receives, unfiltered
    manifest.json    the §6 section for this try
    build.stdout, build.stderr, check.stdout, check.stderr
```

`source/` is reused only when `source.json`'s `head_sha` matches; a mismatch is exit 2
(§6), never a silent replacement. Kept until the operator deletes it — no automatic
TTL. `context/` is `source/` as is: `verify`'s `CONTEXT_IGNORE` stripping
(`verify.py:88–115`) is **not** applied, because CI's builder received any tracked
`.env` too — which is why only a local endpoint may receive it (§4.2).

## 2. Backend and frontend — capabilities are detected, not inferred

Three different things, kept apart:

- **This repo's parser** (`parse_dockerfile`, `verify.py:119–142`) today returns
  `(instruction, args)` pairs only. §3.1 extends it with source line spans and a
  closed list of syntax checks, and nothing more.
- **The builder's own check** — `docker build --check` — runs only in the runtime
  phase under `--reproduce`, never offline. It needs Buildx ≥ 0.15 and the
  Dockerfile 1.8 frontend (Docker's build-checks documentation) and may fetch image
  metadata or a frontend. **Podman** has no `--check`: `skipped: backend has no build
  check`. On this repo's acceptance machine (Podman) it is therefore always skipped;
  its reading is proven by recorded outputs only (§8).
- **The actual build** (§4) remains the primary way to learn what the builder does.

The manifest records the backend (docker / podman, version), Buildx version where
present, and the Dockerfile's `# syntax=` directive. When the directive names an
external frontend, the parser's syntax findings are recorded as `observation`, not
`failed`.

**Reading `--check` output.** Docker documents no machine-readable form for `check`,
and its two documented text forms differ: the build-checks page prints a rule header
as `JSONArgsRecommended - https://docs.docker.com/go/dockerfile/rule/…`, the
`buildx build` reference as `WARNING: InvalidBaseImagePlatform`. The reader therefore
recognises only these line shapes, after stripping progress lines (`[+] …`, `=> …`,
`#<n> …`):

- **lint block:** a header `(WARNING: )?<RuleName>( - https://docs.docker.com/go/dockerfile/rule/<slug>/)?`
  where `<RuleName>` is UpperCamelCase, then its description line, then
  `Dockerfile:<N>`, then the `---`-fenced numbered excerpt;
- **parse diagnostic:** a line containing `dockerfile parse error on line <N>: <text>`;
- **summary:** `Check complete, <k> warning(s) has/have been found!`, if present.

Decided in this order, first match wins:

| # | Condition | Recorded as |
|---|---|---|
| 1 | not launched, timed out | `skipped` with the reason |
| 2 | a parse diagnostic is present | syntax finding `line N: <text>`, source `builder` |
| 3 | exit 0 | syntax `passed`; each lint block a lint `observation` |
| 4 | exit nonzero, and **every** non-progress line belongs to a lint block or the summary, with at least one lint block | syntax `passed`; each lint block a lint `observation` (Docker documents a nonzero exit when violations are reported) |
| 5 | anything else — including lint blocks mixed with any other error line | `skipped: build check output not recognised`, raw output attached |

Row 4's "every line" is what keeps a lint warning followed by a builder or network
failure out of `passed` (row 5). The shapes are pinned by recorded outputs (§8.A),
including one of each documented form and one mixed case; a new Docker version that
prints anything else lands in row 5, never in a finding.

**Parser vs builder, syntax only.** Both found an error at the same line → one
finding, both sources cited. Only the builder → the builder's finding. Only the
parser, builder `passed` → the finding's status is `inconclusive`, both attached.
Builder `skipped` → the parser's result stands and says the builder check did not run.

## 3. Deterministic checks (offline, over `context/`)

These never start a container and never call a builder. Every check result has one
status from the single set used throughout this document (§6):
`passed | failed | skipped | observation | inconclusive`.

### 3.1 Syntax — a closed list

The parser keeps each instruction's first and last source line and runs exactly these
checks; each finding names its line and its check:

1. the first instruction, after comments, parser directives and `ARG`s, is `FROM`;
2. `FROM` has one argument, or three with `AS` as the second (after an optional
   `--platform=<p>` flag) — this is what makes `polygon/run-5`'s
   `FROM python:3.12-slim extra` a finding;
3. every instruction keyword is one of Dockerfile's documented instructions;
4. a line continuation does not end the file.

"No finding" is reported as `no finding among checks 1–4`, never as "valid syntax".
Heredocs are recognised only to skip over them.

### 3.2 COPY/ADD sources — against the CI side's ignore rules

The context is `.` = the restored workspace root (§4.1), so the build-context root,
the workspace root and `context/` are the same directory, and the Dockerfile path is
relative to it. CI's builder is `docker build`, so the ignore file is chosen by
Docker's rule: `<Dockerfile-name>.dockerignore` next to the Dockerfile, else the root
`.dockerignore`, else none; the choice is recorded with every finding.

Supported patterns: literal paths, `*`, `?`, `**`, leading `!`, Docker's last-match-
wins order. Anything else (character classes, escapes) → `skipped: ignore pattern not
modelled: <pattern>`.

**The source alphabet is closed (owner, 2026-09-24).** A `COPY`/`ADD` source this
slice reads is a literal path or a glob of `*`, `?`, `**`, `[...]` over letters,
digits and `. _ - / + = , @ ~`. Anything else — an escape, an `ARG` substitution
(`$X`, `${X}`), whitespace, a control character — is not read: the source check
records it `skipped: source pattern not modelled`, and §1.3 (d) records `.git
exclusion not proven`, so the restoration is an approximation. One gate decides both,
so the two checks can never disagree about what was read; review rounds of #77 kept
finding places where two separate heuristics did.

Every local source of every `COPY`/`ADD` is resolved against `context/` minus the
ignored paths. Findings: `source <path> absent from the context`, `source <path>
excluded by <file> line <n>`; an empty glob expansion is a finding. Remote `ADD`
sources, `COPY --from`, heredocs and any unmodelled form are `skipped`, never reported
as absent local files.

The **local** backend may choose differently: Podman prefers `.containerignore` when
it exists. §7.4's `ignore_file` dimension compares the file each side would use:
CI side by the rule above, local side by the backend's (Podman: `.containerignore`
if present, else as Docker). Different files → `differs`, values recorded.

### 3.3 `--from` references

`--from=<name>` naming a stage of this Dockerfile is recorded as resolved; any other
`--from` as an external image dependency. Neither is checked offline.

## 4. The build — through its own adapter, not `verify`'s L2

### 4.1 The supported build configuration

The build step's `run:` line is tokenised and accepted only in this shape:

`docker build [--file|-f <path>] [--build-arg K=V]* [--platform <p>] [--tag|-t <t>] .`

— the context is exactly `.`; `--file` is a relative path without `..`; no shell
operators, no variable or `$(...)` expansion, no `--secret`/`--ssh`/`--mount`/
`--network`/`--pull`/`--no-cache`, no `buildx build`. Anything else is a refusal
`unsupported build configuration: <what>` (e.g. `shell chain`, `context app`). The
refusal is a first-class result; §3's findings and the reading layer's outcome stand.

### 4.2 The container endpoint must be confirmed local

Refusing `--container-host` is not enough: `resolve_runtime` also reads
`DEPLOYER_CONTAINER_HOST`, `DOCKER_HOST` and `CONTAINER_HOST` (`runtime.py:76–84`),
and Docker selects a remote daemon through contexts (`DOCKER_CONTEXT` or the active
context). Before any context is handed over, the adapter determines the **actual
endpoint** and how it was chosen, and records both:

1. `--container-host`, `DEPLOYER_CONTAINER_HOST`, `DOCKER_HOST`, `CONTAINER_HOST`,
   `CONTAINER_CONNECTION` or `DOCKER_CONTEXT` set → refusal `endpoint set by <source>
   not confirmed local` — the slice does not try to judge them (`--container-host`
   itself is exit 2, §6);
2. docker: `docker context inspect --format '{{.Endpoints.docker.Host}}'` for the
   active context; podman: the default entry of `podman system connection list`, or
   the local socket when there is none;
3. accepted only when the endpoint is a `unix://` socket, or an `ssh://` or `tcp://`
   URI whose host is `127.0.0.1`, `::1` or `localhost`. A local VM — Podman machine,
   Docker Desktop — is local by this rule (on this machine:
   `ssh://core@127.0.0.1:56907/…`), since the context never leaves the host.
   Anything else, or a detection command that fails → refusal naming the endpoint.

### 4.3 The adapter

A new function in a new module (`reproduce.py`), calling `runtime.container_run` — the
single container-subprocess chokepoint — and **not** `verify._build`, which pipes the
Dockerfile through stdin (`-f -`), adds a memory limit, has no build args or platform,
and returns a classified `CheckResult` with only the output's tail
(`verify.py:1555–1604`). The command:

`<tool> build --file context/<path> [--build-arg K=V]* [--platform <p>] --tag localhost/deployer-repro-<run-id>-<seq> [--force-rm] context`

- the Dockerfile is passed **by path**, so the Dockerfile-specific ignore file applies;
- CI's own `-t` value is recorded, never used;
- `--force-rm` is passed on Podman (its default is already true; passing it pins it);
- no memory limit (CI had none); `--build-timeout` applies.

It records the argv, backend and version, endpoint (§4.2), host architecture,
`exit_code` (`null` when the build did not finish), `launch_error` (the reason when it
could not start or did not finish: `timeout`, `executable not found`, `<OSError>`), and
**full** stdout/stderr to the try directory. No `FailureKind`.

### 4.4 Cleanup — what is and is not covered

- **The adapter's image tag:** if it exists after the build, `rmi -f` it and **read
  the return code**: `removed`, `failed` (incl. timeout), or `not_attempted` (nothing
  was tagged).
- **Podman's build containers:** Buildah runs `RUN` steps in working containers.
  `--force-rm` removes them after a finished build, successful or not. A build that
  was killed (timeout) or crashed can leave them behind; the slice **does not look
  for or remove them**, and records `build_containers: not_checked` when the build
  did not finish, `removed_by_builder` otherwise. Docker/BuildKit creates no
  user-visible build containers.
- **Layers and build cache** are not removed; the manifest says so. Reclaiming them is
  the operator's `system prune` (`todo://deployer/bench-run-dir-litter`).

### 4.5 Network operations are named individually (owner's decision)

With an explicit `--reproduce` the backend may fetch base images it lacks through its
normal pull path; no tag is force-refreshed. Image fetches, network access from `RUN`
steps and the resolution of an external `# syntax=` frontend are three different
operations; the absence of an image pull is not "no network", and no offline mode is
promised. The manifest records the digest of every image the build used where the
backend exposes it; CI's digests are taken from BuildKit's `resolve <image>@sha256:…`
lines when the log has them (the committed snapshots do), else `unknown`.

## 5. No run in this slice

In the supported shape the failed CI step **is** the build, so the build is the whole
reproduction; a run would answer a question CI never asked, and it needs a deploy
target and suite the snapshot does not carry. A local build that succeeds yields
`not_reproduced` (§7) — never "behaviour verified". Running the image returns as its
own slice, with its own input contract.

## 6. The result document and the exit code

`deployer diagnose <run> --reproduce` adds a `reproduction` section to the verdict
document (verdict schema 1.1 → 1.2, additive; without `--reproduce` the document is
unchanged). Each try's `manifest.json` holds the same section. Relative paths have
**two bases, fixed by field**: paths that name the project — `binding.dockerfile`,
`binding.context`, `location.file`, `path_absent.path`, `ignore_file.path` — are
relative to the build context (`context/`, identical to `source/`); paths that name an
artifact of the try — `build.stdout`, `build.stderr`, `path_absent.listing` and any
`output_file` — are relative to the try directory (`…/attempt-<n>/tries/<seq>/`).
`try_dir` itself is relative to the working directory the command ran in, and the
verdict copy carries it so both bases resolve from the verdict alone.

```json
{
  "reproduction": {
    "status": "attempted",
    "try_dir": ".deployer-runs/35680991093/reproduction/attempt-1/tries/001",
    "refusal": null,
    "restoration": {"state": "exact", "sha": "d6e330fd8d85f761962d8a134f0ffdd0e914bf9b",
                    "unmet": []},
    "binding": {"job_id": 106597702099, "workflow_job": "build", "build_step": 3,
                "dockerfile": "Dockerfile", "context": "."},
    "environment": {"backend": "podman", "backend_version": "5.7.0",
                    "endpoint": "ssh://core@127.0.0.1:56907/run/user/501/podman/podman.sock",
                    "endpoint_source": "podman_default_connection",
                    "buildx_version": null, "host_arch": "arm64",
                    "syntax_directive": null},
    "checks": [
      {"check_id": "builder_check", "status": "skipped",
       "reason": "backend has no build check", "finding": null,
       "location": null, "evidence": []},
      {"check_id": "copy_sources", "status": "failed", "reason": null,
       "finding": "source docs/setup.md absent from the context",
       "location": {"file": "Dockerfile", "lines": [11, 11]},
       "evidence": [{"kind": "path_absent", "path": "docs/setup.md",
                     "listing": "../../source.json#tree"},
                    {"kind": "ignore_file", "path": null}]}
    ],
    "build": {"argv": ["podman", "build", "--file", "context/Dockerfile", "…"],
              "exit_code": 125, "launch_error": null,
              "failed_instruction": {"lines": [11, 11], "bound_by": "step_text"},
              "signature": null,
              "stdout": "build.stdout", "stderr": "build.stderr",
              "image_cleanup": "not_attempted", "build_containers": "removed_by_builder"},
    "comparison": {"state": "reproduced_with_differences",
                   "reason": null,
                   "ci_instruction": {"lines": [11, 11],
                                      "bound_by": "buildkit_error_block"},
                   "signature_match": "not_compared",
                   "dimensions": {"backend": "differs", "host_arch": "unknown",
                                  "base_image_digests": "unknown",
                                  "ignore_file": "same", "restoration": "same"}}
  }
}
```

(`exit_code` 125 for a failed COPY is what Podman 5.7.0 returned on this machine for
a probe Dockerfile with a missing COPY source.)

**Invariants.** `status` ∈ `attempted | refused | unavailable | not_requested`;
`refused`/`unavailable` have `refusal` set and no `build`/`comparison` — §7 applies
only to `status: attempted`. Check status ∈
`passed | failed | skipped | observation | inconclusive` — the one set used everywhere.
A `failed` check has a `finding` and at least one `evidence` entry; `skipped` and
`inconclusive` have a `reason`. Evidence entries are typed (`path_absent`,
`ignore_file`, `log_excerpt`, `output_file`, `tree_listing`), so an absence is an
assertion checked against a listing, not a pointer to a missing path. `exit_code` is
`null` iff `launch_error` is set. `comparison.reason` is set iff the state is
`not_attempted` or `inconclusive`. No field carries a `FailureKind`; the check type is
its own `ReproductionCheck`, and `CheckResult` (whose `enforce_failure_taxonomy`
requires a class on every `FAILED`) is neither loosened nor fed `UNKNOWN`.

**Exit code.** The process exits with the reading layer's code (`cli.py:58–65,461–483`:
3 unclassified, 4 evidence unavailable, 5 adapter refusal, 2 error) **in every case
except two**, both exit **2**: `--reproduce` given together with `--container-host`;
and a try directory that cannot be created, or an attempt directory whose
`source.json` names another `head_sha` (§1.5). Everything else — every refusal, an
unavailable tree, a missing container runtime (`refused: no container runtime`), any
comparison state — lives in the document and does not touch the exit code. On an
adapter refusal (5) reproduction is `not_requested`: there is no failed run.

## 7. CI versus local — one state, by a fixed order

### 7.1 Instruction identity

An instruction is identified by its **Dockerfile source line span**:

- **CI side:** BuildKit's error block `Dockerfile:<N>` followed by `>>>`-marked lines
  gives the span (committed snapshots: 11; 7–9; 15). It must occur exactly once in
  the job's text; it is attributed to the build step because the supported shape has
  exactly one build step and it is the failed one — `bound_by: buildkit_error_block`.
  A CI `dockerfile parse error on line <N>` gives `parse at line <N>` and **takes
  precedence** over the `Dockerfile:<N>` block BuildKit prints with it (run-5 has
  both). Anything else → unbound.
- **Local side, an executed instruction:** Podman's last `STEP k/n: <instruction>`
  before the error, or `Error: building at STEP "<instruction>"`, or BuildKit's block
  on Docker, matched against the parser's instructions by whitespace-normalised text;
  exactly one match gives the span — `bound_by: step_text` / `buildkit_error_block`.
- **Local side, a parse failure:** Podman states no line — it prints `Error: FROM
  requires either one argument, or three: …` and exits 125 (this machine, Podman
  5.7.0). It is bound to `parse at line <N>` only when **all** hold: no `STEP` line was
  printed; the error line is `Error: <K> …` whose first word `<K>` is a Dockerfile
  instruction keyword; §3.1 has exactly one finding, at line N, on an instruction with
  keyword `<K>` — `bound_by: parser_finding_keyword`. A backend failure (`Error:
  Cannot connect …`) has no keyword and stays unbound. Docker's BuildKit states the
  line and is bound as on the CI side.

### 7.2 The output signature

For a failed `RUN`, the signature is the last non-empty line of that instruction's own
output before the builder's error line, with timestamps, ANSI codes and BuildKit's
`#<n> <t.ttt>` prefix stripped. When that line is missing on one side or both, the
signature is **unavailable**. It is deliberately weak: for `run-3` it
is `FAILED (failures=1)`, which any single failing unittest prints. Equal signatures
show the instruction ended the same way, not that the same test failed; both outputs
are attached and no stronger claim is made.

`signature_match` ∈ `equal | unequal | unavailable | not_compared`. `not_compared`
applies only when the failed instruction is not a `RUN` (a parse failure, a `COPY`)
and the backends differ — builder messages differ by backend and are never compared
across them. With the same backend the builder's error line is the signature.

### 7.3 The states — evaluated in this order; the first that applies wins

§7 is evaluated only for `status: attempted`; a refusal or an unavailable tree ends
earlier with `refusal` set and no `comparison` (§6).

1. `not_attempted` — the build could not start (`launch_error` other than
   `timeout`). Reason recorded.
2. `inconclusive` (`build did not finish`) — `launch_error: timeout`.
3. `not_reproduced` — the local build exited 0. Checked **before** any binding: a
   successful build has no failed instruction to bind.
4. `inconclusive` (`CI instruction unbound` / `local failure unbound`) — the build
   exited nonzero and either side is unbound; a backend failure lands here.
5. `different_failure` — both bound, the spans differ. **Not a reproduction.**
6. `inconclusive` (`signature unavailable`) — same span, `signature_match: unavailable`.
7. `same_instruction_different_output` — same span, `signature_match: unequal`.
   **Not a reproduction.**
8. `reproduced` — same span, `signature_match` `equal` or `not_compared`, every §7.4
   dimension `same`.
9. `reproduced_with_differences` — as 8, with a dimension `differs` or `unknown`.

**Reachability of `reproduced`.** In this slice the CI side's `host_arch` is always
`unknown` (§7.4), so state 8 is **unreachable by design** and every reproduction is
state 9. It is kept in the order so that the day a runner fact supplies the CI
architecture it becomes reachable without a schema change; the tests assert it is
never produced now.

### 7.4 Dimensions

Each is `same`, `differs` (values from both sides) or `unknown` (a side missing):
`backend` (CI: docker, by §4.1; local: detected); `host_arch` (CI: `unknown` — no
runner line in the committed logs states it, and an `amd64` in apt's output is
program output, not a runner fact); `base_image_digests` (§4.5); `ignore_file`
(§3.2); `restoration` (`exact` → `same`, approximation → `unknown`). A difference is
never declared without values from both sides.

## 8. Acceptance — committed cases with expected results

Three layers, kept apart; the first is the proof, the other two support it.

**A. Offline regression cases** (committed, run by `uv run pytest`; no container, no
builder, no network). Each case is a **bundle** in `tests/fixtures/reproduction/<case>/`:

- `snapshot.json` — schema 1.3, re-fetched read-only from the existing run (no new
  dispatch), anonymised as the 1.2 fixtures were;
- `tree/` + `tree-listing.json` — the tree at `head_sha`, vendored from the pinned
  commit, and the Git tree listing it is checked against;
- `local.stdout`, `local.stderr`, `local.exit` — what a fake `container_run` replays
  for the build, recorded once from a real Podman build (C);
- `endpoint.json` — what the fake endpoint detection returns;
- `PROVENANCE.md` — run URL, SHA, how each file was obtained, every change made;
- `expected.json` — the expected `status`/`refusal`, `restoration`, `checks`,
  `build.failed_instruction`, `signature_match` and `comparison`.

**Base cases** — real runs, nothing changed:

| Case | Source | Expected |
|---|---|---|
| `run-1` | `d6e330f`, run 35680991093 | exact; `copy_sources` failed, `docs/setup.md`, line 11; local exit 125 bound `step_text` 11; `not_compared`; `reproduced_with_differences` |
| `run-2` | `37242cc` | exact; §3 no finding; both bound 7–9; signature `E: Some index files failed to download. …` `equal`; `reproduced_with_differences` — and **no** artifact finding, the point of the case |
| `run-3` | `43d7c39` | exact; §3 no finding; both bound 15; `FAILED (failures=1)` `equal`; `reproduced_with_differences` |
| `run-5` | `937d465` (`polygon/run-5`), evidence `9e89daa` | exact; syntax check 2 failed, line 1; `builder_check` skipped; local `Error: FROM requires …`, exit 125, bound `parser_finding_keyword` line 1; `not_compared`; `reproduced_with_differences` |

**Negative cases** — each derived from one base case; the **Changes** column lists
every *input* file of the bundle that differs from the base, so the bundle stays
internally consistent. In every negative case `PROVENANCE.md` records the base case and
each change, and `expected.json` is written for the case; a change to a file under
`tree/` (the workflow included, which lives at `tree/.github/workflows/…`) is mirrored
in `tree-listing.json`:

| Case | Base | Changes | Expected |
|---|---|---|---|
| `snapshot-1.2` | run-1 | `snapshot.json` = the committed 1.2 fixture as is | refused `snapshot lacks reproduction fields` |
| `event-pr` | run-1 | `snapshot.json` `event: pull_request` | refused `event pull_request not supported` |
| `checkout-sha-other` | run-1 | the SHA line after `git log -1 --format=%H` in the snapshot's job text | refused `checkout at <sha>, run at <head_sha>` |
| `checkout-sha-missing` | run-1 | that line pair removed from the job text | refused `checkout SHA not established` |
| `checkout-ref` | run-1 | workflow: `with: {ref: main}` on checkout | refused naming `ref` |
| `several-failed-jobs` | run-1 | snapshot: a second failed job | refused `several failed jobs` |
| `matrix` | run-1 | workflow: `strategy.matrix` on the job | refused naming `strategy.matrix` |
| `shell-chain` | run-1 | workflow: `docker build --file ./Dockerfile . && echo ok`; snapshot: the step's name and job text changed to the same line | refused `unsupported build configuration: shell chain` |
| `context-subdir` | run-1 | workflow and snapshot as above with `… app` | refused `unsupported build configuration: context app` |
| `endpoint-env` | run-1 | `endpoint.json`: `DOCKER_HOST` set | refused `endpoint set by DOCKER_HOST not confirmed local` |
| `endpoint-remote` | run-1 | `endpoint.json`: default connection `ssh://core@10.0.0.5/…` | refused naming the endpoint |
| `generating-step` | run-1 | workflow: a step `run: make gen` between checkout and build; snapshot: `all_steps` gains it and every later step is renumbered, together with the existing `steps[].ref.number` and every step-scoped `evidence.source.number` (the build moves from 3 to 4); job text gains its `##[group]Run make gen` block | approximation naming the step; checks as run-1; `reproduced_with_differences` |
| `gitattributes` | run-1 | `tree/` and `tree-listing.json` gain `.gitattributes` (`* export-subst`) | approximation `.gitattributes present` |
| `archive-mismatch` | run-1 | `tree-listing.json` lists a file `tree/` lacks | approximation `archive differs from tree listing` |
| `copy-git` | run-1 | `tree/Dockerfile` gains `COPY .git/HEAD /head`; `tree-listing.json` updated; no `.dockerignore` | approximation `.git exclusion not proven` |
| `run-mount` | run-3 | `tree/Dockerfile` line 15 becomes `RUN --mount=type=bind,target=/src uv run --frozen python -m unittest discover -s tests`; the snapshot's CI error block (`>>>` line 15, the `#16 [stage-0 …] RUN …` header) and `local.stdout`'s `STEP` line carry the same instruction text | approximation `RUN --mount`; both sides bound 15; signature `equal`; `reproduced_with_differences` |
| `containerignore` | run-1 | `tree/` and listing gain `.dockerignore` (`tests`) and `.containerignore` (empty) | `ignore_file: differs` (CI `.dockerignore`, local `.containerignore`); checks and comparison otherwise as run-1 — still `reproduced_with_differences`, now with two differing dimensions |
| `local-success` | run-2 | `local.*`: the recorded output of a successful build, exit 0 | `not_reproduced` (state 3, before binding) |
| `timeout` | run-3 | fake runtime raises timeout | `inconclusive`, `build did not finish`; `exit_code null`, `build_containers: not_checked` |
| `backend-down` | run-5 | `local.*`: `Error: Cannot connect to Podman …`, exit 125 | `inconclusive`, `local failure unbound` — **not** bound to the parser finding |
| `unbound-ci` | run-1 | job text: the `Dockerfile:11` block removed | `inconclusive`, `CI instruction unbound` |
| `other-line` | run-3 | `local.*`: the failure at `STEP 7/13: RUN uv sync --frozen` (line 12; the only instruction with that normalised text) | `different_failure` |
| `other-output` | run-3 | `local.stdout`: last line of the RUN output `FAILED (errors=1)` | `same_instruction_different_output` |
| `signature-missing` | run-3 | `local.stdout`: the RUN's output lines removed, error line kept | `inconclusive`, `signature unavailable` |

**`--check` reading** — recorded outputs, no bundle: the build-checks page form, the
`WARNING:`-prefixed form, a parse error, an unreachable builder, and a lint block
followed by `ERROR: failed to solve: …` → rows 4, 4, 2, 5, 5 of §2's table. The first
two are copied verbatim from Docker's documentation; the others are recorded from a
real Docker ≥ Buildx 0.15 run on synthetic Dockerfiles before the reader is written,
and `PROVENANCE.md` says where each came from.

**B. Contract tests with fakes.** `api_bytes` passes bytes unaltered; extraction
refuses `..`, absolute paths, outside links and a second top-level entry (→
unavailable); argv shape (file by path, own tag, `--force-rm` on Podman, no memory
limit); `rmi` return code read; the two exit-2 cases. These prove contracts, not
reproduction.

**C. Manual local run (this machine, Podman; secondary).** `run-1`, `run-2`, `run-3`,
`run-5` rebuilt for real; the outputs become the base cases' `local.*`, and each
`PROVENANCE.md` records the backend version and date. If a real build fails
differently from the CI run, that actual result is what is committed and the expected
state follows from it — a manual run never substitutes for A, and A never claims what
C did not record.

**Dropped from rev 3:** the uv-minimal bench failure. Its failing Dockerfile was never
preserved (`corpus/synthetic/uv-minimal/fixture.Dockerfile` already copies `src`
before `uv sync`), so naming the case pins nothing.

No new live dispatches and no paid authoring or benchmark are part of this acceptance.

## 9. Decisions, recorded

**Owner, 2026-09-22:**

| Question | Decision |
|---|---|
| Where the restored tree lives, for how long | under `.deployer-runs/<run-id>/reproduction/`, `source/` (restored, unmodified) separate from the build context; kept until explicit operator cleanup, no automatic TTL; images the reproduction creates cleaned, result recorded (layout versioned per try in §1.5) |
| Fetching images | an explicit `--reproduce` allows the backend's normal fetch of missing images; no forced tag refresh; no offline promise; actual digests recorded, `unknown` where CI's are missing; the saved tree is never modified when the sandbox is prepared |
| ENVIRONMENT candidate | not in this slice — the network failure of a specific step and the comparison results are recorded as facts, without a presumed class |

**Owner, 2026-09-23 (accepting rev 4's scope and rev 5's narrowing):** no image run;
one failed job with one build step that is the failed step; uv-minimal dropped; the
build context is `.` only; workflow and checkout must both be at `head_sha`, else
refusal; the inert steps are exact strings; only a confirmed-local endpoint; any
`.gitattributes` is an approximation. Next review is targeted (rev 4 → rev 5 diff and
the ten rev-4 findings), not a third full pass.

## Appendix — the ten rev-4 findings and where rev 5 closes them

| # | Finding | Closed in | Checkable criterion |
|---|---|---|---|
| 1 | Snapshot schema 1.2 already taken | §1.1, base line | new fields under 1.3; reproduction checks fields, not the version; case `snapshot-1.2` refuses |
| 2 | Workflow path suffix; workflow version vs checkout | §1.1, §1.2 #1–2 | raw and normalised path kept apart; event restricted to `push`/`workflow_dispatch`; checkout SHA must equal `head_sha`, tree and workflow read there; cases `event-pr`, `checkout-sha-*`, `checkout-ref` |
| 3 | Endpoint locality beyond the flag | §4.2 | env vars and contexts refuse; endpoint detected and recorded; loopback/unix only; cases `endpoint-env`, `endpoint-remote` |
| 4 | `exact` bypasses | §1.3 | inert list exact strings; `.gitattributes` read from the Git tree; archive vs listing incl. modes/symlinks; `.git` sources; `RUN --mount`; cases `generating-step`, `gitattributes`, `archive-mismatch`, `copy-git`, `run-mount` |
| 5 | Build-context root | §4.1, §3.2 | context must be `.`; one root for §3 and §4; case `context-subdir` refuses |
| 6 | Real `--check` output | §2 | both documented forms recognised; ordered table; "every line" rule for rows 4/5; five recorded outputs incl. the mixed case |
| 7 | Comparison order, parse binding, reachability | §7.1–7.3 | exit 0 checked before binding; `signature unavailable`; parse binding needs keyword + single parser finding; `reproduced` stated unreachable and asserted; cases `local-success`, `backend-down`, `signature-missing`, `timeout` |
| 8 | Binary tarball | §1.4 | `api_bytes`; redirect; size cap; `data` filter; single top-level; failures → unavailable; §8.B contracts |
| 9 | Acceptance consistency, `other-output` | §8.A | every negative lists all changed bundle files; `other-output` derived from run-3 (a `RUN` with a signature); walk of each case through §1–§7 in the Expected column |
| 10 | Result/evidence/exit/cleanup | §6, §4.4 | one status set; typed evidence; two path bases fixed by field (context / try dir); `launch_error`; `restoration` dimension in the example; "in every case except two"; `build_containers` recorded, killed-build leftovers `not_checked` |
