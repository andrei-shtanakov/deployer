# deployer

Research bench for deploy-authoring agents: an LLM authors a Dockerfile from
deterministic project facts + a declarative `deploy_target` intent; a
deterministic pipeline verifies it (static checks, then a sandboxed
`docker build` + run + healthcheck) and feeds failures back for repair.
**Authoring ≠ execution**: the model only ever sees facts and reports and
returns text — files, docker, and control flow belong to the pipeline.
A deploy target may declare pinned infra `dependencies` (redis,
postgres, ...): the model then authors a compose.yaml next to the
Dockerfile and verification runs `compose up` with an in-network
healthcheck probe — no host ports are ever published.
A `{"ci": {}}` intent additionally authors a build-image GitHub
Actions workflow (`.github/workflows/ci.yml`, SHA-pinned actions,
build-only — no registry push), verified statically incl. a pinned
actionlint. Static checks bound what the workflow *declares*; a pinned
third-party action that pushes internally cannot be excluded statically —
the artifact still lands in a human-reviewed PR.

Facts cover uv, Poetry (poetry.lock, including legacy [tool.poetry]
metadata) and pip (requirements.txt) projects; a curated hints table
suggests apt packages for known no-wheel dependencies (hints, not facts —
build errors win), and `deploy_target.system_packages` lets the operator
require apt packages outright.

Design: `docs/superpowers/specs/2026-07-04-deployer-mvp-design.md`.

## Usage

```sh
uv run deployer author <project-path> [--target target.json] [--no-docker] \
    [--container-tool docker|podman] [--container-host ssh://user@host] \
    [--build-timeout 600] [--health-timeout 30] [--signing-key key]
uv run deployer verify <project-path> [--target target.json] \
    [--container-tool docker|podman] [--container-host ssh://user@host] \
    [--build-timeout 600] [--health-timeout 30]
# verify checks <project-path>/Dockerfile; --health-timeout bounds runtime
# checks (service healthcheck or run intent) and is ignored for build-only
# targets. Slow source builds (e.g. llama-cpp-python) need
# --build-timeout well above the 600s default.
```

Remote verification (the L2 sandbox on another machine over SSH):

```sh
DEPLOYER_CONTAINER_TOOL=docker \
DEPLOYER_CONTAINER_HOST=ssh://user@host \
uv run pytest -m docker
```

`--container-host` / `DEPLOYER_CONTAINER_HOST` accept `ssh://` URLs only;
a pre-existing `DOCKER_HOST`/`CONTAINER_HOST` is honored and recorded in
reports as `host_source: "native_env"`. The build context is copied to a
temp dir minus `.git`, `.venv`, `.deployer`, `.env*`, caches — secrets
never reach the daemon, local or remote. Invalid runtime configuration
(missing requested tool, non-ssh host) exits 2.

Exit codes: `0` success; `1` verification/authoring failed (including a
missing `Dockerfile` for `verify`, and an `author` run that could not
remove the previous authoring set it was required to withdraw); `2`
invalid invocation (bad flag values, project path not a directory,
unreadable or invalid `--target`, invalid runtime configuration).

`author --signing-key` (default `DEPLOYER_SIGNING_KEY`) takes an ed25519
private key and publishes a signed authoring provenance set for the written
Dockerfile under `.deployer/authoring/` (plus a `.deployer/` line in
`.dockerignore`, and in `.containerignore` when present), which
`diagnose --reproduce` later checks ownership against (see "Admission"
below). It is issued only from a clean checkout at the
repository root with an `origin` remote; otherwise authoring still runs and
warns that ownership will not be confirmable. Authoring leaves the tree dirty
(the Dockerfile and the set), so commit before the next signed run. Verifiers
trust keys from a store outside every repository:

```sh
uv run deployer trust add <key.pub>
uv run deployer trust revoke <key.pub>
uv run deployer trust replace <old.pub> <new.pub>
```

The store is `DEPLOYER_TRUST_DIR`, default `~/.config/deployer`
(`allowed_signers`, `revoked_keys`).

`verify` writes its full report to `<project>/.deployer/verify-report.json`
(latest run only). Alongside `hadolint_available`/`actionlint_available`/
`docker_available`, the report carries `atp_available` (whether the `atp_smoke`
check actually ran `atp`, as opposed to reporting `SKIPPED`) and `built_image`
(the L2 image's tag, runtime and cleanup outcome — see "Bench" below for the
`smoke` intent that consumes it).

### Report schema version

Every report deployer writes — `verify-report.json`, `authoring-run.json`,
`bench-report.json`, `golden.json` — carries `schema_version`, currently
`"2.0"`. A consumer should pin against it rather than against the shape.

The compatibility rules:

- A document with **no** `schema_version` key predates versioning and reads
  as `"0"`. deployer applies that when it reads such a file back; a consumer
  parsing these documents should do the same.
- Within a major version, **added fields are compatible**: a new report field
  does not bump the version. Only a breaking change to an existing field does.
- 2.0 widened the value set of an existing field — `CheckResult.failure_kind`
  gained `unknown` and `project` — which is why it is a major bump rather than
  an additive one: `CheckResult.model_validate` on a reader still pinned to
  the v1 two-member enum fails with a pydantic validation error on those
  values. A reader pinned to v1 refuses a v2 document **by design**; it is not
  a bug to fix on the reader's side.
- Because v1 is purely additive over v0, `bench compare` still diffs a v0
  baseline against a v1 run, and does not report the version gap as a
  finding.
- deployer holds itself to the same rule when reading a report back: a
  document whose **major** is none of `0`, `1`, `2` is refused (`error:` and
  exit 2) rather than compared as if understood. A later *minor* stays
  readable, since additive fields are compatible within a major.

`author` and `bench run --author anthropic` auto-load `./.env`
(KEY=VALUE lines) for the Anthropic API key; real environment variables
always win, and runtime flags (`DEPLOYER_CONTAINER_*`) are NOT read
from `.env`.

`target.json` is a `DeployTarget`: e.g.
`{"service": {"port": 8000, "healthcheck_path": "/health"}}`.
`{"system_packages": ["libpq5"]}` in the target requires apt packages unconditionally.
`{"extras": ["gui"]}` installs optional-dependency groups.
`{"entrypoint": "app.py"}` specifies the bare filename or [project.scripts] name to run.
`{"run": {}, "smoke": {"suite": "suite.yaml"}}` requests an ATP smoke test of the
built image in place of the plain job-completes check (details under Bench below).
Design: `docs/superpowers/specs/2026-07-04-facts-v2-design.md`.
Every `author` run writes `.deployer/authoring-run.json` — iteration count,
per-check outcomes, authoring-vs-environment failure taxonomy. That file is
the research output.

## Diagnose

```sh
uv run deployer diagnose <run-url>
uv run deployer diagnose --repo owner/name --run-id N [--attempt N]
# either form accepts --output-file verdict.json
```

Reads a failed GitHub Actions run into a snapshot of facts and reports what
the evidence shows: the lines that matched an observation shape (a parser's
own sentence, a tool's own network framing, an assertion, a missing file,
...), each cited from the block it was read from, and what could not be read.
It asserts **no cause**: no class is attached to any failure, `causes` is
always empty, and every verdict's `kind` is `null` — see the Addendum of
`docs/superpowers/specs/2026-09-21-ci-failure-diagnosis-design.md`. Exit codes:

| code | meaning |
|---|---|
| `0` | not produced by this layer (reserved for a future line; see the reproduction spec) |
| `3` | `UNCLASSIFIED` — the evidence was read completely; observations reported, no cause asserted |
| `4` | `EVIDENCE_UNAVAILABLE` — the evidence could not be read; **not** a success |
| `5` | adapter refusal — the run is not a finished, failed run |
| `2` | bad argument, or the run metadata could not be fetched |

stdout carries the human-readable summary, stderr the diagnostics.
`--output-file` writes the verdict document, which carries its own
`verdict_schema_version` (`"1.1"`, `"1.2"` once `--reproduce` adds a
`reproduction` section, `"1.3"` once an attempted reproduction adds an
`admission` section — see below; independent of the report
`schema_version` above); the run snapshot nested in it carries
`snapshot_schema_version` (`"1.3"`). Verdict 1.1 is additive over 1.0: the
keys are the same, `causes` is always `[]` and `kind` always `null`; 1.2 adds
only the `reproduction` key, so a document produced without `--reproduce` has
the verdict's own keys unchanged from 1.1 — the nested `run` snapshot is
schema 1.3 either way, so the document as a whole is not byte-identical to a
1.1 one. Snapshot 1.1 added a per-job `completeness` — how
that one job was read — beside the run-level worst-of; 1.2 added `level` on
each piece of evidence: a GitHub annotation's raw `annotation_level`, and
`null` for log text, which has no level; 1.3 added the run's workflow path,
its event and every job step (not only the failed ones) — inputs `--reproduce`
needs to restore and bind the failed step, unused otherwise. All three are
additive, so a stored 1.0/1.1/1.2 snapshot still loads — reading every job as
completely read, every piece of evidence as level-less, and every
reproduction-only field as absent.

Requires `gh` authenticated for the repository, and a `gh` new enough to
support `gh api --allow-escape-sequences` (real build logs carry ANSI colour
and `gh` refuses to print them without it; verified with `gh` 2.98.0). A `gh`
that fails for its own reasons — unknown flag, timeout, missing binary —
exits 2 rather than being reported as an unreadable log.

The reading layer itself is offline and pure: it is a function from the
fetched snapshot to the verdict. Fixture input is a **test affordance, not a user
contract** — there is no flag to feed a saved snapshot in.

### `diagnose --reproduce`

```sh
uv run deployer diagnose <run-url> --reproduce [--build-timeout 900] \
    [--output-file verdict.json]
```

On top of the reading layer above, restores the failed run's tree and
workflow at its actual checkout SHA, rebuilds the run's one failed build step
locally through its own adapter (never `verify`'s L2 build), and reports
where that local build agrees or disagrees with CI. Like the reading layer it
asserts **no cause** — no `FailureKind` is attached to a reproduction finding
either; see `docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md`.

Only a narrow shape is supported; the first unmet condition below refuses
reproduction (the plain diagnosis above still runs and is reported): the
run's `event` must be `push` or `workflow_dispatch`; `actions/checkout`'s
logged SHA must equal `head_sha` exactly once, and the checkout step must set
none of `ref`/`repository`/`path`/`sparse-checkout`/`lfs`/`submodules`/
`fetch-depth`; exactly one job failed, with no `strategy.matrix`, job-level
`uses:`, or `container:`/`services:`; its steps bind one-to-one to the
workflow job's own steps by name; exactly one step is a `run:` step that
parses as a plain `docker build [-f path] [--build-arg K=V]* [--platform p]
[-t tag] .` — build context `.` only, no shell operators or substitution,
none of `--secret`/`--ssh`/`--mount`/`--network`/`--pull`/`--no-cache`, and
no `buildx build` — and it is the failed step (other, non-build `run:` steps
are allowed; before the build they only downgrade restoration to
`approximation` unless they are on the exact inert list); and neither the
job nor the build step sets a working directory. The container endpoint is
checked separately and must resolve to a confirmed-local socket (`unix://`,
or `ssh://`/`tcp://` to `127.0.0.1`/`::1`/`localhost`): `--container-host`
and every host-selecting environment variable (`DEPLOYER_CONTAINER_HOST`,
`DOCKER_HOST`, `CONTAINER_HOST`, `CONTAINER_CONNECTION`, `DOCKER_CONTEXT`)
refuse outright, because the restored build context skips `verify`'s
`CONTEXT_IGNORE` stripping (CI's own builder saw any tracked `.env` too) and
must never leave the machine — `--reproduce` combined with `--container-host`
is refused before either runs.

Each attempt's restored tree and every try's build output land under
`.deployer-runs/<run-id>/reproduction/attempt-<n>/` (`source/` the read-only
restored tree, shared across tries; `tries/<seq>/` each build's own context
copy, manifest and stdout/stderr) — kept until the operator deletes them.
There is no automatic TTL; cleanup covers only the image tag the reproduction
build itself created. Because `source/` is read-only, give the owner write
permission back before deleting a reproduction:

```bash
chmod -R u+w .deployer-runs/<run-id>/reproduction
rm -rf .deployer-runs/<run-id>/reproduction
```

The exit code is the reading layer's (3 unclassified, 4 evidence
unavailable, 5 adapter refusal, 2 bad argument) in every case except two,
both exit `2`: `--reproduce` given together with `--container-host`, and a
try directory that cannot be created, prepared or written (copying the
tree, changing its permissions, writing its files) or whose stored
`source.json` names a different `head_sha`. Every refusal (an unsupported shape, an unconfirmed
endpoint), an unreadable tree, a missing container runtime, and every
CI-vs-local comparison state live in the verdict document's `reproduction`
section instead of changing the exit code.

### Admission

Every attempted reproduction adds an `admission` section (verdict 1.3,
additive over 1.2): the one decision whether the failed run may enter fix
authoring. `admitted` needs three things proven together — (1) ownership:
the Dockerfile at `head_sha` equals, byte for byte, a record signed by a
trusted key for this repository and path (the set `author --signing-key`
published, committed with it); (2) a defect from a closed catalogue of two
classes, `missing_copy_source` and `from_argument_count`; (3) a link: CI's and
the local build's failures both match a recorded template row for that class
at that instruction, over an `exact` restoration and a known dialect.
Anything else is `insufficient_grounds`, with each unmet condition numbered
and explained — not a claim that deployer is blameless. Admission never
claims a cause beyond those two proven defect classes, and the exit codes
above are unchanged: the verdict lives in the document only, and a consumer
reads it from there (absent or malformed means not admitted). Design:
`docs/superpowers/specs/2026-09-24-ci-failure-admission-design.md`.

## Fix

```sh
uv run deployer fix <verdict.json> --clone <path> [--signing-key …] [--build-timeout 900]
uv run deployer fix publish <fix.json> --base <branch>
uv run deployer fix confirm <fix.json>
```

Given a verdict document whose `admission` section reads `admitted` (see Admission
above), authors the one instruction that removes the proven defect, proves it locally,
publishes the fix as a PR, and confirms in CI that the defect is gone — three claims kept
apart (locally confirmed / fix proposed / defect removal confirmed in CI; see
`docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md`). `fix.json` (schema
`"1.0"`) is dev-side evidence written to the fix directory and never committed to the
project; its `status` is one of `in_progress`, `stopped`, `locally_confirmed`,
`fix_proposed`, `ci_confirmed`. A `stopped` document carries one of five reasons: `no
admission`, `fix method not established`, `no proposal`, `no local confirmation`,
`commit blocked`.

**Local and CI confirmation are both available; the end-to-end acceptance run (design
§11 stage 5) is not yet.** Both proof stages read
their positive evidence off a closed table of template rows, and a row is enabled only
together with the test that checks it against a real recording of that build (design
§9). The two local (Podman) rows, COPY/ADD and FROM, are backed by the L-recordings
(`tests/fixtures/recordings/local`) and enabled, so `deployer fix` can reach
`locally_confirmed` and `fix publish` can publish. The local evidence is the corrected
instruction's own step line (with or without Podman's `[i/n] ` stage prefix), unique in
the Dockerfile and in the output: COPY/ADD also needs the same stage's next step, or a
completion naming the fix build's own tag; FROM needs no next marker, since Buildah
prints that line only after its FROM check passed (design §6.3,
`docs/fix-buildah-from-parse.md`). A skipped stage, a repeated instruction, a
completion of another tag, or a Dockerfile outside the modelled form (a substitution or
quote in that instruction family, or an unmodelled continuation anywhere) never
confirms.

The two CI (BuildKit) rows are backed by the C-recordings
(`tests/fixtures/recordings/ci`, real polygon runs) and enabled, so `deployer fix confirm`
can reach `ci_confirmed`. The corrected Dockerfile is read at the fix commit first
(replace refs off): it must be the bound build's file, hash to the locally proved bytes,
be in the strict form, and hold a corrected COPY/ADD exactly once (BuildKit also skips a
stage nothing depends on). Only the build step's own output is read: the
runner-timestamped lines after the `##[endgroup]` that closes its one
`##[group]Run <build line>` header, up to the next `##[group]` line or the post phase
(`Post job cleanup.`), split on `\n` only; any other line break, or a line without the
runner's timestamp, refuses. COPY/ADD then needs
exactly one named stage header carrying the corrected text (BuildKit's step number
right-aligned to the step count, as in `[stage-0  7/10]`) and that step's `#k DONE`;
`#k CACHED` never confirms. The header must sit at the COPY's position derived from
the Dockerfile (single stage, only recorded instructions; anything else refuses), and
if the build failed — searched to the end of the log — the failure must be provably
at a later step of the COPY's stage. FROM is file-wide: a named build-stage header and no
`dockerfile parse error` (BuildKit parses the whole file first). A later independent
failure in the same run does not cancel a proven pass; any `undetermined` attempt, a
recurrence or an ambiguous binding does (design §7.3–§7.4).

Exit codes: `fix` — `0` `locally_confirmed`, `1` `stopped`, `2` invalid invocation or
local I/O; `fix publish` — `0` pushed and a PR created or found, `1` refused, `2` local
I/O; `fix confirm` — `0` the attempt is positive (`ci_confirmed`), `1` insufficient or not
published, `2` local I/O.

## Bench

The corpus (`corpus/synthetic/`) is a set of small target projects with
declared intent (`target.json`) and expectations (`expected.json`), e.g.
`atp-agent` — a minimal ATP-compatible agent whose target declares a
`smoke` intent.

    uv run deployer bench run [--corpus corpus] [--filter GLOB] [--label NAME] \
        [--author fixture|anthropic] [runtime/timeout flags]
    uv run deployer bench verify [--corpus corpus] [--filter GLOB]

`bench run` authors every case in a scratch copy and writes the raw run
(per-case `authoring-run.json` + final Dockerfile, aggregate
`bench-report.json` + `bench-report.md`) under `.deployer-runs/<ts>-<label>/`
(gitignored). The default author is `fixture` — it replays each case's
committed `fixture.Dockerfile`, needs no API key, and measures the
verification pipeline. `--author anthropic` runs the real LLM and spends
money; select it explicitly. `bench verify` just verifies the committed
fixtures (corpus smoke). Exit codes: 0 all matched/passed, 1 mismatch/fail,
2 invalid invocation. Cases with `requires_l2: true` are skipped (not
failed) when no container runtime is available. `--filter` applies to synthetic
and (with `--include-external`) external targets alike; non-matching
externals are not even cloned.

`smoke` in a target requests an ATP smoke test of the built image:

```json
{"run": {}, "smoke": {"suite": "suite.yaml", "timeout_s": 300}}
```

The suite path is resolved relative to the `target.json` that declares it.
The check id is `atp_smoke`; it needs `atp` 2.1.0 on `PATH` and a local
container runtime (ATP's container adapter has no remote-host support), and
reports `SKIPPED` otherwise. `deployer bench run --require-atp` turns such a
skip into a failure, which is how the seam is accepted; it also fails if the
filtered corpus declares no `smoke`-intent case at all, since an empty scope
closes the gate even less than a SKIPPED smoke does.

Once accepted, `bench promote` puts the smoke case into the golden baseline.
From then on, comparing against that baseline on a machine without `atp` on
`PATH` reports an `important` `missing_case` finding: `atp_smoke` reports
`SKIPPED`, the case is dropped from the candidate before comparison, and its
absence is `important` like any other missing case — the seam's result is
unknown on that machine, not passing, so it must not read as green.

### Installing `atp` 2.1.0 (temporary source-install workaround)

The published release cannot be installed: `atp-platform==2.1.0` requires
`atp-adapters`, which was never published to PyPI, and its CLI additionally
imports `fastapi` and `atp_sdk` without declaring them. Tracked as
atp-platform#320 (`publish-installable-container-cli`). **Until that closes,
install from the tagged source.** Do not assume a checkout of atp-platform is
already on disk:

```bash
tmp=$(mktemp -d)
git clone --depth 1 --branch v2.1.0 \
    git@github.com:andrei-shtanakov/atp-platform.git "$tmp/atp"
cd "$tmp/atp" && uv tool install '.[dashboard]' --with ./packages/atp-sdk
```

Verify both, not just the first:

```bash
atp --version                        # atp, version 2.1.0
atp plugins list --type=adapter      # must list `container`
```

### Golden baseline

    uv run deployer bench promote .deployer-runs/<ts>-<label> [--corpus corpus] [--force]
    uv run deployer bench compare .deployer-runs/<ts>-<label> golden
    uv run deployer bench compare <runA> <runB>   # raw-vs-raw

`promote` normalizes a raw run (no wall times, paths, hostnames, or check
messages) into `corpus/golden/` (committed) and refuses runs with
mismatched cases unless `--force`. `compare` reports regressions by level:
hard (green→red), important (iteration growth, failure-kind flip, missing
case), advisory (image size, hadolint status, new case; wall time only for
raw-vs-raw). Exit 1 on hard/important findings, 0 otherwise.

Promote only a full-corpus run unless replacing the baseline with a subset is
intentional: `bench promote` replaces the entire `corpus/golden/` tree, and a
filtered run can therefore erase otherwise healthy golden cases.

## Development

```sh
uv sync
uv run pytest              # unit tests (no docker, no LLM)
uv run pytest -m docker    # + sandboxed docker build/run tests
uv run ruff format . && uv run ruff check . --fix && pyrefly check
```

Optional: `hadolint` 2.12.0 on PATH enables the lint check; runs without it
are marked non-comparable in the run report.
