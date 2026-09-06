# Seam audit of the Phase-4 artifacts

Date: 2026-09-06
Item: `todo://deployer/seam-audit` (#51)
Status: inventory — factual, not a design. Its output is the shortlist
`todo://deployer/first-consumer-seam` (#52) picks from.

Verified against: deployer `372a506` (2026-09-01); atp-platform `e263d69`,
proctor `394cdd5` (2026-08-31); arbiter `3503557` (2026-09-01);
dispatcher `13d5000` (2026-09-04); maestro, robin-runtime, spec-runner,
research-bench read at the same time. Neighbours were read only.

## What deployer emits today (producer side)

| | |
|---|---|
| Artifacts | `<project>/Dockerfile` always; `<project>/compose.yaml` when the target declares `dependencies`; `<project>/.github/workflows/ci.yml` when it declares `{"ci": {}}` |
| Delivery | files written **in place into the scanned project directory** (`src/deployer/cli.py:279-285`). There is no PR-authoring path, no publish step, no export of the artifact set as a bundle |
| Status channel | process exit code — `0` success, `1` authoring/verification failed, `2` invalid invocation (`README.md:58-61`); plus one human-readable stdout line `stopped: <reason> after N iteration(s)` |
| Machine-readable status | `<project>/.deployer/authoring-run.json` (`AuthoringRun`) and `<project>/.deployer/verify-report.json` (`VerificationReport`, latest run only) |
| Error channel | `CheckResult{check_id, status, failure_kind, message}` inside those reports; `failure_kind` is `AUTHORING` or `ENVIRONMENT` (`src/deployer/models.py:274-295`); `error_signature()` fingerprints a failure set |
| Report versioning | none. `deployer_version` is stamped on run records; the report **schema** carries no version field, so a consumer has nothing to pin against |
| Built image | L2 tags `deployer-verify-<uuid8>` and **`rmi -f`s it when verification ends** (`src/deployer/verify.py:1452,1469`) — no image survives a run |

The last two rows are the load-bearing ones: the producer half of every
image-consuming seam does not exist yet, and every report-consuming seam
would pin an unversioned schema.

## Producer → consumer pairs

"Whose half exists" is the graph that decides #52: deployer may build only
its own half, so a pair whose consumer side is missing means a handoff and
the neighbour's PR (ADR-ECO-006, `repo-boundaries.md`).

| Consumer | Artifact & path | Status channel | Error channel | Whose half exists |
|---|---|---|---|---|
| **ATP** (`atp-platform`) | the **built image** by tag, not a file: `atp test <suite>.yaml --adapter=container --adapter-config='image=<tag>'` (`examples/test_suites/docker_agent_test.yaml:12-14`) | ATP run report — JUnit XML / JSON / markdown summary, threshold-gated (`action.yml`) | ATP assertion failures per test; adapter raises `AdapterConnectionError` / `AdapterTimeoutError` / `AdapterResponseError` | **Consumer's half exists and needs no change.** `ContainerAdapterConfig` already takes `image` + `runtime: docker\|podman\|auto` (`packages/atp-adapters/atp/adapters/container.py:56-63`), mirroring deployer's own `ContainerRuntime`. **Producer's half missing** — the image is destroyed at the end of L2 |
| **ATP in CI** (same repo) | the authored `.github/workflows/ci.yml`, as a step inside it | GitHub check status | workflow log | **Consumer's half exists**: `atp-platform/action.yml` is a composite action taking `suite_path`, `adapter`, `threshold`. Producer's half missing: deployer's CI authoring emits a build-only workflow and has no notion of an extra verification step |
| **proctor** | the **built image** by tag — `ContainerRuntime.run(ContainerSpec) -> container_id`, plus `inspect`/`logs`/`stop`/`remove` (`src/proctor/infra/docker.py:84-150`), remote fleets via `ssh_host` | `ContainerStatus` from `inspect` | `logs(container_id, tail)` | Consumer's half exists for *running* an image; but proctor consumes images of **its own workers**, and nothing there asks deployer to author them. The pairing is aspirational until proctor's owner asks for it |
| **proctor / atp-platform as a target repo** | `Dockerfile`, `compose.yaml` authored **for their repo** (both hand-write these today: `proctor/Dockerfile`, `atp-platform/deploy/{Dockerfile,docker-compose.yml}`) | a PR into their repo | their CI | Neither half exists. Deployer cannot open a PR at all, and the artifact would land in a neighbour's repo — handoff plus their agreement, by construction |
| **arbiter** | *(policy gate in front of a mutating action)* | — | — | **No half on either side, and the premise is off.** arbiter's MCP surface is six tools — `route_task`, `report_benchmark`, `report_outcome`, `get_agent_status`, `get_metrics`, `get_budget_status` (`arbiter-mcp/src/server.rs:495-513`) — agent routing and telemetry, not a deploy policy gate. deployer also has no mutating action to gate yet: L2 builds and runs in a sandbox and cleans up after itself |
| **Maestro** | *(deployer as a workstream/spawner type)* | — | — | Neither. One aspirational line in `maestro/docs/idea-workstream-framework.md:47-48` ("`deployer` может стать workstream/spawner-типом"), no deployer workstream type in `maestro/maestro/` |
| **spec-runner** | — | — | — | No pairing exists in either direction: zero mentions of deployer anywhere in `spec-runner`, and no surface that consumes container artifacts |
| **steward** | *(named role, no path)* — steward's gate model assigns **"Gate 5 Deployment = deployer"** (`README.md:41`, `spec/00-charter.md:48`, `spec/30-acceptance.md:45`) | — | — | Neither half exists, **by steward's own design**: DEC-002 puts the executive gates 3/4/5 outside steward — "форсят PR/CI/deployer" (`spec/20-design.md:37`), enforced by branch protection, not by steward code. Its shipped surface (`gate-check`, `steward-compile`) has no Gate-5 implementation and never calls deployer. The nearest thing, `src/steward/compile/project_yaml.py`, passes Maestro deployment knobs through verbatim and knows nothing of deploy artifacts |
| **Robin** (`robin-runtime`) | **not an artifact seam** — consumes deployer's `TODO.md` plan state for the fleet digest (`src/robin/config.py:32`) | digest output | — | Both halves exist and work. Recorded here only so the existing integration is not mistaken for artifact consumption |
| **dispatcher** | **not an artifact seam** — consumes deployer's *work items*: `repo_key github.com/andrei-shtanakov/deployer` in the run-identity contract (`contracts/maestro-repo-identity/v1/cases.json:6-7`), `work_id todo://deployer/...` in Dark Factory slice 0 | run status in the console | run failure in the console | Both halves exist and are **proven twice**: deployer was the slice-0 pilot subject for #36 and #40, and pass 1 was accepted (`dispatcher/TODO.md:646`). Again: work items, not deploy artifacts |

## Findings

1. **Nobody in the fleet consumes a deploy artifact today.** A search for
   `deployer` across every neighbour repo returns hits in exactly four:
   dispatcher and robin-runtime consume deployer as a *repo with work items*;
   maestro has one aspirational line; steward names it as a role in a gate
   model it deliberately does not implement. Not one reference is to a
   Dockerfile, compose file, or workflow that deployer authored — and
   atp-platform, proctor, arbiter, spec-runner and research-bench do not
   mention deployer at all. The 307 remaining hits are all in
   `_cowork_output/`, which is dev-only and does not count as a consumer.

2. **The existing integration runs on a different axis than the direction
   assumes.** Robin and dispatcher already consume deployer, successfully and
   in production — but they consume `TODO.md` items and run identities. The
   artifact axis has zero consumers. This is worth stating plainly because
   "deployer is integrated with the fleet" is true and irrelevant to #52.

3. **Exactly one pair has a consumer half that needs no change: ATP by
   image tag.** `ContainerAdapterConfig(image=..., runtime=docker|podman)`
   is a shipped, documented, example-covered entry point. The missing half
   is entirely inside deployer's own boundary: stop destroying the built
   image, tag it stably, and put the tag in the run report.

4. **That missing half is small and already half-specified.** `_build`
   already takes a `tag` parameter (`verify.py:973-979`); the tag is
   currently a throwaway UUID that `rmi -f` removes at line 1469.

5. **Two constraints on the ATP pair, both real:**
   - ATP's `ContainerAdapter` has **no remote-host support** — no
     `DOCKER_HOST`, no `-H`. deployer verifies happily against
     `--container-host ssh://`, and an image built there is not visible to a
     local ATP run. The seam is local-runtime only unless someone builds
     that half.
   - ATP's assertions are agent-shaped (`artifact_exists`, `contains`,
     `no_errors`), so the fit is strongest for a `run`-intent target. For a
     `service` target, ATP's value overlaps what L2's healthcheck already
     does, and the seam would need the `http` adapter against an
     already-running container.

6. **The `arbiter-policy-gate-seam` item rests on a premise that does not
   hold.** arbiter today routes agent tasks and reports telemetry; it exposes
   no allow/deny decision for a deploy action. The item is correctly parked
   behind a trigger — deployer has nothing mutating to gate — but its wording
   should not imply arbiter's half is waiting.

7. **A duplicated concept worth naming before it hardens — but a narrower
   one than the shared class name suggests.** deployer's `ContainerRuntime`
   (`src/deployer/models.py:195`, resolved and invoked from `runtime.py`) is
   a config record: `tool` (docker/podman), `host`, `host_source`, with a
   single subprocess chokepoint `container_run` over it. proctor's
   same-named class (`src/proctor/infra/docker.py:84`) is a lifecycle
   wrapper — `run(ContainerSpec) -> container_id`, `inspect`, `logs`,
   `stop`, `remove`. These are different kinds of object; what is actually
   built twice is the docker-vs-podman-plus-remote-host selection. Neither
   repo references the other. Not actionable inside this audit; recorded so
   the third copy is a decision rather than an accident.

## Shortlist for #52

**1 — ATP smoke-test above L2, by image tag.** The only candidate where the
consumer's half already exists and needs no change, so the whole seam is
buildable inside deployer's boundary with no handoff and no waiting on an
owner. It is a genuine vertical slice: artifact → built image → external
consumer → status and error back. It also happens to be the seam already
written down as `todo://deployer/atp-smoke-test-seam`, so it closes two
items. Scope it to a `run`-intent corpus case on a local runtime, per
finding 5.

**2 — ATP as a step in the authored `ci.yml`.** Same consumer, second
channel, and `action.yml` is likewise shipped. Weaker than the first only
because deployer's CI authoring emits a build-only workflow, so the
producer-side change is larger, and because verifying it end-to-end means a
real GitHub run rather than a local one.

**Not recommended as the first seam:** arbiter (no consumer half, wrong
premise — finding 6), Maestro (aspirational line only), steward (names
deployer as Gate 5 but by design implements no gate that calls it), proctor
and atp-platform as target repos (artifact lands in a neighbour's repo —
handoff by construction), spec-runner (no pairing at all).

## Handoffs

Neither is an edit to a neighbour's repo; both are recorded per
`repo-boundaries.md`.

- **dispatcher** — the slice-0 design states "deployer's CI runs **no
  tests**: its only workflow is the governance caller"
  (`docs/superpowers/specs/2026-08-22-dark-factory-control-plane-slice0-design.md:557-565`)
  and builds its acceptance argument on that. Stale since 2026-09-01:
  deployer gained `.github/workflows/ci.yml` running `uv run pytest -q` on
  every PR (commit `c836cbc`, the devtools wave). A future pass can read a
  green signal deployer now does emit.
- **arbiter** — finding 6. If the ecosystem still wants a policy gate in
  front of deploy actions, arbiter needs a decision tool it does not have;
  that is arbiter's half and their PR, and nothing should be planned here as
  though it exists.
