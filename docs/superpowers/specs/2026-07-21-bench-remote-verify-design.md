# Bench + remote verify roadmap (2026-07-21)

Design for the next stage of `deployer`: turn the working MVP into a research
bench (corpus, metrics, golden runs) and unblock real Docker verification by
running the L2 sandbox on a remote SSH host (the local machine is podman-only).

Externally reviewed: `../../../../_cowork_output/deployer-roadmap-design-review-2026-07-21.md`
(review accepted with two deviations, noted inline below).

## Non-goals

- No remote *deployment* and no agent-driven execution on remote resources.
  The remote host is only where the deterministic verify sandbox runs.
  Authoring ≠ execution stays non-negotiable.
- No rename. `deployer` stays; `drydock` is recorded as a candidate for a
  later product rename after the bench stabilizes.

## Phase ordering

| Phase | Content |
|---|---|
| 1 | Remote runtime abstraction (`ContainerRuntime` + single subprocess wrapper) |
| 1.5 | Run-metadata hardening in reports (runtime, versions, timeouts, model, prompt hash) |
| 2 | Corpus + `bench run`, offline fixture author first, LLM author explicit |
| 3 | Raw run store, normalized golden promotion, `bench compare` |
| 4 | System-deps hardening → Poetry → compose → CI workflow |

Rationale: capability to run (1) before capability to measure (2) before
capability to compare over time (3) before expansion (4). Phase 1.5 exists
because runs recorded without runtime/model/prompt metadata are not
comparable, so it must land before the first bench numbers.

## Phase 1 — Remote verify over SSH

Mechanism: docker and podman CLIs already speak to a remote engine via
`DOCKER_HOST=ssh://user@host` / `CONTAINER_HOST=ssh://user@host`. No asyncssh,
no manual context copying — the CLI ships the build context itself.

### ContainerRuntime

New model in `models.py`:

```python
class ContainerRuntime(BaseModel):
    tool: Literal["docker", "podman"]
    host: str | None = None          # ssh://... or None = local socket
    host_source: Literal["cli", "deployer_env", "native_env", "local"] = "local"

    @property
    def remote(self) -> bool:
        return self.host is not None
```

Deviation from review: `remote` is a derived property, not a stored field —
a stored bool can desync from `host` on deserialization.

`detect_container_tool()` evolves into
`resolve_runtime(tool_arg, host_arg) -> ContainerRuntime | None`.

Resolution precedence:

1. `--container-tool` if given (else current podman-then-docker detection).
2. `--container-host` if given (`host_source="cli"`).
3. `DEPLOYER_CONTAINER_HOST` env (`host_source="deployer_env"`).
4. Tool-native env already set by the user (`DOCKER_HOST` for docker,
   `CONTAINER_HOST` for podman): **captured and recorded** as
   `host_source="native_env"`, not scrubbed. Today's code silently inherits
   these (no subprocess passes `env=`), which would make reports lie about
   where a run happened.
5. Otherwise local (`host=None`, `host_source="local"`).

An explicitly requested tool that is not on PATH is a usage error (exit 2),
not a silent fall-back to static-only.

### Single subprocess wrapper

All seven container CLI calls in `verify.py` (`build`, `image inspect`,
`run`, `exec`, `logs`, `rm -f`, `rmi -f`) go through one helper:

```python
def _container_run(runtime: ContainerRuntime, args: list[str], **kwargs):
    return subprocess.run([runtime.tool, *args], env=_runtime_env(runtime), **kwargs)
```

Injecting env only into build/run would build on the remote daemon while
cleanup and inspect hit the local one — polluting the remote host and
reporting garbage sizes. `verify()`, `verify_docker()`, `_build`,
`_image_size`, `_run_healthcheck` take `runtime: ContainerRuntime` instead of
`tool: str`.

The healthcheck needs no change: the container runs with `--network=none`
and the probe is `tool exec ... python -c urlopen(127.0.0.1:...)` inside the
container, which is remote-transparent.

### Failure classification

`ENVIRONMENT_MARKERS` gains SSH/daemon-connectivity markers:
`permission denied (publickey)`, `host key verification failed`,
`could not resolve hostname`, `ssh: connect to host`,
`connection timed out`, `cannot connect to the docker daemon`,
`error during connect`, `context deadline exceeded`.

`_classify` matches against combined `stdout + "\n" + stderr`, not stderr
only. (Marker false-positives on build output are a pre-existing,
accepted risk; this change does not widen it materially.)

An unreachable SSH host must classify as `FailureKind.ENVIRONMENT`, never
`AUTHORING` — the authoring loop treats environment failures as retryable
and must not "repair" the Dockerfile for them.

### Report fields

`VerificationReport` gains `runtime: ContainerRuntime | None = None`.
`docker_available` stays as-is (backward-compatible JSON; deviation from
review's optional rename).

### CLI

`verify` and `author` gain `--container-host URL` and
`--container-tool {docker,podman}`. Env default: `DEPLOYER_CONTAINER_HOST`.

### Known risks

- **Podman remote semantics** differ from docker (`--connection` model,
  socket path on the remote side). Before implementation: a small spike
  matrix — docker local, docker ssh, podman local, podman remote — recorded
  in the plan.
- **`build --memory` portability**: BuildKit/remote/podman support differs;
  an "unsupported" marker must classify as ENVIRONMENT/capability warning,
  not AUTHORING.

### Acceptance criteria (Phase 1)

1. `deployer verify --container-host ssh://user@host --container-tool docker <p>`
   drives build/run/exec/logs/rm/rmi with `DOCKER_HOST` set.
2. `deployer author ...` uses the same resolved runtime on every iteration.
3. `VerificationReport.runtime` round-trips JSON.
4. `AuthoringRun.runtime` and effective timeouts round-trip JSON (see 1.5).
5. Unreachable SSH host ⇒ `FailureKind.ENVIRONMENT`.
6. Docker-marked test suite runs both locally and with
   `DEPLOYER_CONTAINER_HOST` pointing at a remote host, unchanged.
7. Existing unit suite still needs neither Docker nor SSH.

## Phase 1.5 — Run-metadata hardening

Everything that affects comparability gets recorded in `VerificationReport`
(where it varies per verification) and `AuthoringRun` (per run):

- runtime (tool, host, host_source), tool client/server versions, platform;
- effective `build_timeout` / `health_timeout` (already a backlog item:
  "AuthoringRun should record effective timeouts");
- model id, prompt hash (system prompt + renderer), author backend,
  `max_iterations`;
- hadolint availability/version (exists), deployer version/git sha.

## Phase 2 — Corpus + bench

Layout:

```text
corpus/
  synthetic/
    uv-minimal/
      project/...
      target.json
      expected.json
    pip-requirements/
    service-healthcheck/
    no-build-system/
    system-deps-psycopg2/
    slow-build/
  external.toml        # real projects: URL + pinned commit, cloned to scratch
```

`expected.json`:

```json
{
  "expected_success": true,
  "max_iterations": 3,
  "requires_l2": true,
  "expected_failure_kind": null,
  "capabilities": ["pip", "service", "system-deps"],
  "notes": "..."
}
```

`deployer bench run [--corpus PATH] [--filter GLOB] [--label NAME]
[--author fixture|anthropic]`:

- copies each case's project to scratch (ignoring `.git`, `.venv`,
  `.deployer`, `__pycache__`) — the corpus is never mutated;
- runs the author loop in the copy; per-case `authoring-run.json`;
- aggregates `bench-report.json` + a Markdown table: success rate,
  iterations-to-green, failure taxonomy, image size, wall time;
- stores the raw run under `.deployer-runs/<timestamp>-<label>/`.

**Offline path is the default**: `--author fixture` replays known-good
Dockerfiles (and `bench verify` checks committed Dockerfiles) with no API
key and no spend — CI-friendly. The LLM author must be selected explicitly.
This requires an author seam in the CLI (today `_cmd_author` constructs
`AnthropicAuthor` unconditionally).

Comparability metadata per bench run: everything from Phase 1.5 plus corpus
version/commit and external target URL+commit.

## Phase 3 — Run store, golden, compare

- Raw runs: `.deployer-runs/<timestamp>-<label>/` — gitignored.
- `deployer bench promote <run>` writes a **normalized** golden to
  `corpus/golden/` (committed): normalized `authoring-run.json` (no absolute
  paths), final Dockerfile per case, selected metrics, per-case verification
  summary. No wall-clock durations, host paths, or remote host names —
  those made a raw copy churn on every promote.
- `deployer bench compare <runA> <runB|golden>` reports regressions by
  level:

| Level | Example |
|---|---|
| Hard | case green → red |
| Important | iterations above threshold; failure kind flipped authoring↔environment |
| Advisory | image size +N%; wall time +N%; hadolint warnings up |

- External-project goldens store pinned URL/commit + normalized outputs
  only — never a source snapshot or build artifacts.

## Phase 4 — Capability expansion

Each expansion adds its corpus case *first* (target before capability):

1. **System-deps hardening** (not "first support" — `system_packages`,
   the hints table and the `sysdep_service` fixture already exist and have
   a green dogfood run): transitive no-wheel deps, apt package validation,
   slow/native builds, real locallogai cases from lab_aist.
2. **Poetry** (decide the install strategy up front: install Poetry in the
   image vs export requirements vs `uv pip` from the lockfile).
3. **New artifacts**: compose (multi-service) — a new artifact contract and
   a new deterministic verifier; then CI workflow authoring.

## Testing strategy

- Unit: runtime resolution matrix (flag/env/native/local precedence), env
  injection into every container call (mocked subprocess), classification
  markers, bench aggregation, promote normalization (no absolute paths, no
  timestamps in golden), compare regression levels.
- Docker-marked: existing suite parametrized by `DEPLOYER_CONTAINER_HOST`;
  corpus smoke via `bench verify` (no LLM).
- LLM paths stay out of CI; real-author bench runs are manual research runs.
