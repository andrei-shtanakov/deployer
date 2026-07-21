# Bench + remote verify roadmap (2026-07-21)

Design for the next stage of `deployer`: turn the working MVP into a research
bench (corpus, metrics, golden runs) and unblock real Docker verification by
running the L2 sandbox on a remote SSH host (the local machine is podman-only).

Externally reviewed: `../../../../_cowork_output/deployer-roadmap-design-review-2026-07-21.md`
(review accepted with two deviations, noted inline below) and
`../../../../_cowork_output/deployer-bench-remote-verify-spec-review-2026-07-21.md`
(all seven must-fixes incorporated).

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
`DOCKER_HOST=ssh://user@host` / `CONTAINER_HOST=ssh://user@host`. No asyncssh —
the CLI ships the build context itself.

### Build context hygiene / remote trust boundary

The MVP spec requires an isolated build context ("only the target project is
copied in; no secrets in the environment or context, ever"), but today's
`_build()` passes `project_path` directly — any `.env` in the project would
ship to the daemon, and with a remote daemon it would leave the machine.

Phase 1 therefore builds from a **deterministic temporary context**: the
project is copied to scratch minus an ignore-list (`.git`, `.venv`,
`.deployer`, `.env`, `.env.*`, `__pycache__`, `.pytest_cache`,
`.ruff_cache`), for local and remote L2 alike. Same ignore logic is reused
by `bench run` in Phase 2. The remote host itself must be trusted — it runs
LLM-authored `RUN` instructions under the same sandbox rules as the MVP
(rootless daemon preferred, resource limits, `--network=none` at run stage).

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
`resolve_runtime(tool_arg, host_arg) -> ContainerRuntime | None`, which
**raises `RuntimeConfigError`** for explicitly-invalid configuration
(requested tool not on PATH, malformed host URL) — the CLI maps it to
exit 2. `None` means only one thing: no runtime found implicitly ⇒
static-only, matching existing behavior.

Tool precedence:

1. `--container-tool` if given.
2. `DEPLOYER_CONTAINER_TOOL` env.
3. Current podman-then-docker detection. (Without an env default, a
   docker-remote host plus podman-first local detection would pick the
   wrong CLI for every test run.)

Host precedence:

1. `--container-host` if given (`host_source="cli"`).
2. `DEPLOYER_CONTAINER_HOST` env (`host_source="deployer_env"`).
3. Tool-native env already set by the user (`DOCKER_HOST` for docker,
   `CONTAINER_HOST` for podman): **captured and recorded** as
   `host_source="native_env"`, not scrubbed. Today's code silently inherits
   these (no subprocess passes `env=`), which would make reports lie about
   where a run happened.
4. Otherwise local (`host=None`, `host_source="local"`).

Deployer-provided hosts (`cli`, `deployer_env`) accept **`ssh://` only** in
this phase — `tcp://` is a different threat model. Invalid scheme ⇒
`RuntimeConfigError` (exit 2). Native env values are captured as-is: they
are the user's own preconfiguration.

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

`_runtime_env(runtime)` is precisely defined:

- starts from `os.environ.copy()` — never a minimal dict (`PATH`, `HOME`,
  `SSH_AUTH_SOCK` and docker/podman config vars must survive, or SSH agent
  auth and CLI lookup break);
- overlays `DOCKER_HOST` (docker) / `CONTAINER_HOST` (podman) when
  `host_source` is `cli` or `deployer_env`, overriding any native value for
  the selected tool; leaves env untouched for `native_env`/`local`;
- is never logged (env may carry secrets).

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
review's optional rename). Its semantics with podman/remote are really
"container L2 runtime available" — a `container_runtime_available` alias may
be added later, `docker_available` then deprecated; not in this phase.

### CLI

`verify` and `author` gain `--container-host URL` and
`--container-tool {docker,podman}`. Env defaults: `DEPLOYER_CONTAINER_HOST`,
`DEPLOYER_CONTAINER_TOOL`. Canonical remote test invocation:

```sh
DEPLOYER_CONTAINER_TOOL=docker \
DEPLOYER_CONTAINER_HOST=ssh://user@host \
uv run pytest -m docker
```

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
3. `VerificationReport.runtime` and `AuthoringRun.runtime` round-trip JSON
   (runtime recording lands here; effective *timeouts* recording is
   Phase 1.5 — acceptance must not depend on a later phase).
4. L2 build context excludes the ignore-list (`.env` never reaches the
   daemon, local or remote).
5. Unreachable SSH host ⇒ `FailureKind.ENVIRONMENT`.
6. Docker-marked test suite runs both locally and with
   `DEPLOYER_CONTAINER_TOOL`/`DEPLOYER_CONTAINER_HOST` pointing at a remote
   host, unchanged.
7. Existing unit suite still needs neither Docker nor SSH.
8. Explicitly-invalid runtime config (missing requested tool, non-`ssh://`
   deployer-provided host) ⇒ exit 2, not silent static-only.

## Phase 1.5 — Run-metadata hardening

Everything that affects comparability gets recorded in `VerificationReport`
(where it varies per verification) and `AuthoringRun` (per run):

- runtime (tool, host, host_source), tool client/server versions, platform;
- effective `build_timeout` / `health_timeout` (already a backlog item:
  "AuthoringRun should record effective timeouts");
- model id, prompt hash (system prompt + renderer), author backend,
  `max_iterations`;
- hadolint availability/version (exists), deployer version/git sha.

Version/platform probing is **best-effort and non-fatal**: a misconfigured
remote engine records a metadata warning; verification outcome depends only
on the actual L2 checks.

## Phase 2 — Corpus + bench

Layout:

```text
corpus/
  synthetic/
    uv-minimal/
      project/...
      target.json
      expected.json
      fixture.Dockerfile   # known-good, for the offline author; outside
                           # project/ so it never enters the build context
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

- copies each case's project to scratch with the same `CONTEXT_IGNORE`
  list as the Phase 1 build context (`.git`, `.venv`, `.deployer`, `.env`,
  `.env.*`, `__pycache__`, `.pytest_cache`, `.ruff_cache`) — the corpus is
  never mutated;
- runs the author loop in the copy; per-case `authoring-run.json`;
- aggregates `bench-report.json` + a Markdown table: success rate,
  iterations-to-green, failure taxonomy, image size, wall time;
- stores the raw run under `.deployer-runs/<timestamp>-<label>/`.

**Offline path is the default**: `--author fixture` replays each case's
`fixture.Dockerfile` (and `bench verify` checks committed Dockerfiles) with no API
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
  those made a raw copy churn on every promote. Golden keeps only
  comparability-relevant runtime facts: `remote` flag, tool,
  platform/arch — never the hostname (raw runs keep the exact host).
- `deployer bench compare <runA> <runB|golden>` reports regressions by
  level:

| Level | Example |
|---|---|
| Hard | case green → red |
| Important | iterations above threshold; failure kind flipped authoring↔environment |
| Advisory | image size +N%; hadolint warnings up |
| Advisory (raw-vs-raw only) | wall time +N% |

Wall-time comparison exists only between raw runs — golden intentionally
stores no durations, so `run vs golden` never reports it.

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

## Phase 4a addendum (2026-07-21): script-entrypoint fact + repair feedback

**Status: spec accepted, implementation pending** (this section describes
the first Phase-4 PR; Phase 4 deliberately starts by closing the Phase-2
`slow-build` debt before new capability expansion).

Scope is driven by the llm-baseline research run
(`.deployer-runs/20260721-191555-llm-baseline`, local artifact — evidence
reproduced here): both failures shared one root cause — requirements-only
projects carry no entrypoint fact, so the model invents a CMD and the
repair loop cannot converge because the healthcheck failure never names
the command.

| case | authored CMD | failure | why no convergence |
|---|---|---|---|
| pip-requirements | `["python", "-m", "http.server", "8000"]` | file server answers `/health` with 404 → healthcheck HTTPError | error shows urllib traceback, never the CMD; 2 identical signatures → no_progress |
| system-deps-psycopg2 | `["python3"]` | bare REPL exits instantly → "container state improper", empty logs | no logs, no CMD in message; same → no_progress |

Both builds (incl. the psycopg2 apt build/runtime split) were correct —
only the CMD was invented. Spec recommendations externally reviewed:
`../../../../_cowork_output/deployer-script-entrypoint-addendum-review-2026-07-21.md`
(recommendations incorporated into this text).

### script_entrypoint fact

`ProjectFacts.script_entrypoint: str | None = None`. Deterministic scan
(variant A — never guesses):

- candidates: root-level `*.py` files containing a `__main__` guard,
  matched as `if\s+__name__\s*==\s*["']__main__["']\s*:` (whitespace- and
  quote-tolerant, still a cheap text scan);
- denylist first: `setup.py`, `conftest.py`, `manage.py` are never
  candidates — they carry `__main__` guards in the wild but are not app
  entrypoints, and a wrong authoritative fact is worse than no fact
  (amendment from the whole-branch review);
- `main.py` among candidates → `"main.py"`;
- else exactly one candidate → that filename;
- else `None`. No recursion into `src/`; no filename-convention fallback
  without a guard (an `app.py` without a guard stays invisible).

Note: `corpus/synthetic/no-build-system/project/main.py` has no guard and
correctly stays `None` — the case already passes without the fact and now
doubles as the no-fact regression case.

### Prompt rule (prompt-only in this PR)

Added to `SYSTEM_PROMPT` next to the install-strategy rules, with explicit
precedence:

> `script_entrypoint` is deterministic ground truth. If it is set and
> `entrypoints` (`[project.scripts]`) is empty, the Dockerfile CMD MUST
> execute that file in exec form (e.g. `CMD ["python", "main.py"]` or the
> package-manager equivalent). Never invent `http.server`, never leave a
> bare interpreter, never use a file not present in the facts.

`[project.scripts]` wins over `script_entrypoint` (packaged CLIs/services
keep their console-script CMD). A deterministic L1 check for
"CMD runs the entrypoint" is deliberately NOT part of this PR — CMD
equivalence (`python main.py` vs `uv run --no-sync python main.py`) is
brittle to parse; recorded as a future candidate if prompt-only proves
insufficient.

### Healthcheck failure names the container command

On a `run_healthcheck` AUTHORING failure the report message gains one line:

```text
container command: ENTRYPOINT <json-or-null>, CMD <json-or-null>
```

obtained via **image** inspect (`--format` on Config.Entrypoint/Cmd — the
image, not the container, so cleanup cannot race the diagnostic), strictly
best-effort: an inspect failure must not alter the check result or flip it
to ENVIRONMENT. Repair prompts see only `CheckResult.message`, so this is
exactly where the model finally sees the culprit.

### slow-build corpus case (Phase 2 debt)

`corpus/synthetic/slow-build/`: requirements.txt

```text
markupsafe==3.0.2
--no-binary MarkupSafe
```

forces an sdist C build. MarkupSafe is deliberately NOT added to
`KNOWN_SYSTEM_DEPS` — this is the no-hint native-build case: the model must
derive `gcc`/`libc6-dev` from the build error, not from a hint
(`system-deps-psycopg2` remains the with-hints twin). `project/main.py` is
a service with a `__main__` guard that imports and uses `markupsafe`
(exercises the new fact and proves the C extension actually loads);
`target.json`: port 8000, `/health`; `expected.json`: success, ≤3
iterations, `requires_l2: true`, capabilities
`["pip", "service", "slow-build", "no-hint-system-deps"]`.
`--no-binary` lines create no false hints (`collect_hints` skips
`-`-prefixed entries).

### Acceptance (Phase 4a)

- `uv run pytest` and `uv run pytest -m docker` green;
- `uv run deployer bench verify` green over all **6** cases;
- `uv run deployer bench run --author fixture` → **6/6 matched**;
- manual research run `--author anthropic` → **6/6 matched**, then
  `bench promote` (the golden becomes LLM-authored — compare tracks the
  measured subject; the fixture baseline stays reproducible on demand);
- `bench compare <run> golden` after promote: no hard/important findings.

## Testing strategy

- Unit: runtime resolution matrix (flag/env/native/local precedence,
  `RuntimeConfigError` cases), env injection into every container call
  (mocked subprocess), build-context ignore-list, classification markers,
  bench aggregation, promote normalization (no absolute paths, no
  timestamps in golden), compare regression levels.
- Docker-marked: existing suite parametrized by `DEPLOYER_CONTAINER_HOST`;
  corpus smoke via `bench verify` (no LLM).
- LLM paths stay out of CI; real-author bench runs are manual research runs.
