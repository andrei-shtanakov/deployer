# ATP smoke-test seam (first production consumer seam) — design

Date: 2026-09-06
Status: approved for implementation (external spec review 2026-09-06)
Closes: `todo://deployer/first-consumer-seam` (#52) and
`todo://deployer/atp-smoke-test-seam`
Depends on: `todo://deployer/report-schema-version`, which ships **first, as
its own PR** — see §6
Prior art: `docs/2026-09-06-phase4-seam-audit.md` (the inventory that
produced this shortlist); founding doc `docs/idea-deployer-subproject.md`
("ATP = validation/smoke-test of built artifacts")

## Context

deployer authors and verifies deploy artifacts, but nothing in the fleet
consumes them. The seam audit found exactly one candidate whose consumer half
is already shipped and needs no change — ATP, consuming a **built image by
tag** through its container adapter — and no other candidate that avoids a
handoff to a neighbour's repo.

One correction to the audit's framing, found while designing: ATP's container
adapter is not a general container prober. It runs `docker run -i`, writes an
`ATPRequest` JSON document to stdin and requires a valid `ATPResponse` JSON
document on stdout; a non-zero exit is `AdapterError`, empty stdout is
`AdapterResponseError`
(`atp-platform/packages/atp-adapters/atp/adapters/container.py:282-360`). The
http adapter has the same expectation against an "Agent HTTP endpoint URL".
So the consumer's half exists for **ATP-compatible agent images**, not for
arbitrary artifacts. That does not devalue the seam; it fixes its honest
domain of applicability, and the resulting loop — deployer packages an agent,
ATP proves the packaged agent still answers — is exactly the ecosystem
division of labour the founding doc describes.

## Non-goals

- **Not a general contract for verifying any container.** This seam covers
  ATP-compatible agent images only. Ordinary services keep L2's healthcheck.
- **deployer does not author ATP suites.** A suite is fixture-owned input, not
  a fourth artifact type; authoring one belongs to
  `todo://deployer/further-artifact-types` territory, if ever.
- **Remote runtimes are out of scope.** ATP's container adapter has no
  `DOCKER_HOST` / `-H` support. With `--container-host ssh://` and a declared
  smoke consumer, the check reports `SKIPPED` with that reason rather than
  pretending to pass.
- **No new bench axis for one case.** `atp_smoke` gets no dedicated column in
  `bench compare`, on the same reasoning that parks `actionlint_status` until
  a second CI case exists: a metric over one example measures one example.

## 1. Corpus case `atp-agent`

`corpus/synthetic/atp-agent/`, shaped like its neighbours: `project/`,
`target.json`, `expected.json`, `fixture.Dockerfile`, plus `suite.yaml`.

The fixture is a `run`-intent agent: read `ATPRequest` JSON from stdin, write
`ATPResponse` JSON to stdout, exit 0. No network, no model, deterministic.
The suite asserts the agent answers; it lives in the case directory and is
owned by the fixture.

## 2. Target intent

`DeployTarget` gains `smoke: SmokeSpec | None` (`suite: str`, optional
timeout), following the existing `ci: CISpec | None` and `service` / `run`
precedent. A declarative field rather than a CLI flag because corpus cases are
expressed as `target.json`, and a flag cannot be recorded in a case.

A declared `smoke` **is** the "explicitly requested downstream consumer" that
keeps the image alive for the length of the run.

Validation (pydantic `model_validator` on `DeployTarget`):

- `smoke` requires `run` — the target still declares that this is a job, not
  a service.
- `smoke` forbids `service`.

The second rule is not a shortcut, and the http path it closes is not a cheap
extension of this design. A service container today runs with
`--network=none`, publishes no port, is probed from *inside* via
`exec python -c`, and is removed as soon as the healthcheck returns
(`verify.py:1075`). ATP's http adapter works from *outside* and would need a
different lifecycle altogether: hold the container up, publish a port, pick a
free host port, wait for readiness, hand over the endpoint with
`allow_internal=true`, then guarantee cleanup. That is a second vertical seam
with its own security surface, not a flag on this one.

Loosening a validator later is backward compatible, so the cost of deciding
this now is zero. When a real service-shaped agent appears, `smoke` can grow a
transport discriminator with two implemented variants to generalize from —
rather than a second variant designed from guesswork.

## 3. Control flow: ATP replaces `_run_completes`, it does not follow it

For a `smoke` target the L2 branch is:

    build → ATP smoke

and explicitly **not**

    build → _run_completes → ATP smoke

`_run_completes` starts the container with no stdin at all
(`src/deployer/verify.py:1224-1238`: `container_run(runtime, ["run", "--name",
container, "--network=none", "--memory", …, tag], …)`). A correct ATP agent
reading stdin gets EOF and is entitled to fail. Teaching the fixture to
tolerate an empty request would be inventing a behaviour to satisfy our own
harness, and would weaken exactly the property the smoke test exists to prove.

ATP is itself the runtime check for these targets. So: `smoke` requires `run`
to be declared, but when `smoke` is present `_run_completes` is not invoked.

`verify_docker` already owns the tag lifecycle (`verify.py:1452`, with
`rmi -f` in `finally` at `:1469`). The smoke check slots in before the `try`
block ends, so the failure path is covered by the existing `finally`.

## 4. Invoking ATP

An optional external tool driven by subprocess without a shell, modelled on
`_check_actionlint` (`verify.py:661`): `shutil.which("atp")`, then a pinned
version check against `ATP_VERSION = "2.1.0"` — the version the fleet manifest
pins (`ai-orchestrators-workspace/workspace-manifest.toml:49`).

    atp test <abs suite path>
      --adapter container
      --adapter-config image=<tag>
      --adapter-config runtime=<runtime.tool>
      --output json --output-file <tmp>/atp-report.json
      --no-save

Three details that are load-bearing, not decoration:

- **`runtime` must be passed explicitly.** ATP's `auto` detection prefers
  podman when both are installed
  (`atp/adapters/container.py:188-200`), so an image built by docker could be
  looked for in podman. The runtime deployer actually built with is the one
  ATP must use.
- **The tag is fully qualified as `localhost/deployer-verify-<uuid8>`**, so a
  short name is never treated as something to pull. Applied to the tag
  everywhere, keeping one code path rather than a smoke-only variant.
- **`--no-save`, and cwd set to a temporary directory.** Without it an
  external smoke run writes into the dashboard database and
  `.atp-runs/checkpoints/` (`atp/cli/main.py:477-481`). A verification step
  must not leave state on the operator's machine.

## 5. Verdict mapping

The JSON report is authoritative; the exit code is a consistency check, not a
substitute for the report.

| JSON | rc | Outcome |
|---|---|---|
| `version=1.0`, `summary.total_tests >= 1`, `summary.success=true` | 0 | `atp_smoke: PASSED` |
| `version=1.0`, `summary.success=false` | 1 | `atp_smoke: FAILED`, `AUTHORING` |
| `version=1.0`, `summary.total_tests == 0` | any | `atp_smoke: FAILED`, `ENVIRONMENT` |
| missing / unparseable / unknown version | any | `atp_smoke: FAILED`, `ENVIRONMENT` |
| JSON and rc disagree | — | `atp_smoke: FAILED`, `ENVIRONMENT` |
| binary absent or version mismatch | — | `atp_smoke: SKIPPED`, `atp_available: false` |
| timeout / launch error | — | `atp_smoke: FAILED`, `ENVIRONMENT` |

The `total_tests == 0` row guards a contract invariant, not a reachable bug
today. ATP computes `summary.success` as `passed_tests == total_tests`
(`atp/reporters/json_reporter.py:101`), so a zero-test suite would arithmetically
report **success** — but ATP's own CLI closes the routes there: a mis-resolved
path is rejected by `click.Path(exists=True)` before anything runs
(`atp/cli/main.py:382`, verified), and an empty suite is rejected ahead of
report generation (reported in review; not verified here).

The guard stays because the invariant this seam depends on is "a passing
smoke check means tests actually ran", and that invariant currently lives in
someone else's CLI. If ATP's validation drifts, this seam's strongest signal
becomes its cheapest false positive. Classified `ENVIRONMENT`, because a suite
that ran no tests says nothing about the authored artifact.

Assertion failure is `AUTHORING` because packaging is what deployer controls:
a wrong entrypoint, workdir or missing COPY is precisely what makes a
correct agent stop answering. The existing two-value taxonomy is sufficient;
`FailureKind` does not grow.

## 6. Report shape

### Report versioning ships first, separately

`schema_version` is **not** part of this slice. It is its own contract, with
its own compatibility policy and its own migration of existing files, and it
must land as a prerequisite PR before the seam.

Its scope is all four root JSON artifacts, not the two this seam happens to
touch: `VerificationReport` (`models.py:298`), `AuthoringRun` (`:353`),
`BenchReport` (`:394`), `GoldenReport` (`:443`).

The policy it must state: a missing field reads as **legacy v0**, and
**additive fields are compatible within v1**. That second half is what keeps
this seam cheap — `atp_available` and the built-image reference are additions,
so they do not force a v2.

Sequencing the two changes rather than mixing them keeps each golden movement
attributable: first the format version alone, then the new corpus case and
`atp_smoke` alone. `bench promote` / `compare` must treat the version as a
normalized constant, not a diff.

### This slice's additions

`VerificationReport` gains:

- `atp_available: bool`, beside `hadolint_available` / `actionlint_available`,
  with the same "tool absent ⇒ run is non-comparable" meaning.
- A built-image reference: `tag`, `runtime`, `lifecycle: "ephemeral"`, and
  `cleanup_status`.

`cleanup_status` records what actually happened — `removed`, `failed`, or
`not_attempted`. `rmi` is best-effort and may return non-zero or raise
(`verify.py:1469` swallows both), so a hardcoded `removed: true` would be a
claim the code never checks. `cleanup_owner` is deliberately **not** added: in
this slice it would carry one value forever, and `lifecycle` already names the
contract that a later external owner would extend.

**Image lifetime:** the image lives only inside the run. The report reference
answers "what was tested", not "what is sitting on your machine". Leaked
images are the same class of problem as the already-open bench run-dir litter,
and worse: they survive an `rm -rf` of the working directory.

## 7. Bench semantics: `SKIPPED` must not read as success

Today `VerificationReport.passed` is `all(r.status is not FAILED)`
(`src/deployer/models.py:309-311`), so a `SKIPPED` check passes, and
`run_case` would then count such an authoring run as successful
(`src/deployer/bench.py:271-276`). For a case declaring `target.smoke`, that
would silently accept a run in which the seam never executed.

Therefore: for a case with `target.smoke`, `atp_smoke: SKIPPED` turns
`BenchCaseResult.outcome` into `skipped` with a `skip_reason` — the mechanism
bench already uses for a ci target with no fixture (`bench.py:220-226`) — and
the acceptance command must reject that skip separately. The
"run is non-comparable" message alone does not achieve this.

## 8. Suite path resolution

`suite` is resolved **relative to the `target.json` file**, not the current
working directory and not `project/`.

Two facts force this: `_load_target` reads the file and discards its origin
(`src/deployer/cli.py:79`), and bench copies only `case.project_dir` into the
scratch directory (`src/deployer/bench.py:231-237`), so a suite living beside
`target.json` is never copied. The resolved absolute path is therefore passed
to the verifier as a separate argument, while the serialized `DeployTarget`
keeps the relative path — a target document must stay portable.

## 9. Acceptance

The seam is closed by an end-to-end run:

    fixture → authored Dockerfile → L2 image → ATP suite → status in the deployer report

with **`atp_smoke: PASSED`**. `SKIPPED` keeps an ordinary run portable but
does not close the seam.

That implies `atp` 2.1.0 must be installed on the bench machine — a new open
item, paired with the existing `todo://deployer/install-hadolint`. The new
corpus case moves the golden baseline, so acceptance runs `bench promote`
with the diff reviewed before promoting, per the usual rhythm.

## 10. Testing

- Unit: verdict mapping and report parsing against a stub `atp` binary placed
  on `PATH` in a temporary directory — every row of the §5 table, including
  the JSON/rc disagreement and the empty-suite false positive, without a real
  ATP install.
- Unit: the `smoke ⇒ run`, `smoke` forbids `service` validator rules; suite
  path resolution relative to `target.json`.
- Unit: `SKIPPED` on a smoke case yields bench `outcome: skipped`.
- `docker`-marked: the real build plus a real ATP run against the fixture.
- Acceptance: the bench run above.

## Decisions from spec review (2026-09-06)

Both questions this spec raised were settled in review; recorded here so the
reasoning survives without re-reading the thread.

1. **Report versioning ships as its own prerequisite PR** covering all four
   root artifacts, with "missing field = legacy v0, additive fields compatible
   within v1" as the stated policy. Two attributable golden movements beat one
   mixed movement. See §6.
2. **`smoke` keeps forbidding `service`.** The http path needs a different
   container lifecycle and security surface — a separate seam, not a flag.
   Loosening the validator later is backward compatible, so nothing is lost by
   deciding it now. See §2.

A third, smaller correction: the `total_tests == 0` guard stays, but its
justification is narrowed to protecting a contract invariant against future
ATP drift, rather than a false positive reachable today. See §5.
