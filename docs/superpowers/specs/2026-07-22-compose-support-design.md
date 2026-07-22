# Compose support (Phase 4c) — design

Date: 2026-07-22
Status: approved for implementation
Prior art: `2026-07-21-bench-remote-verify-design.md` (Phase 4 item 3:
"compose (multi-service) — a new artifact contract and a new
deterministic verifier").

## Context

Every deployer artifact so far is a single Dockerfile. Real services
rarely run alone: the app needs a cache or a database next to it. This
design adds the second artifact type: a `compose.yaml` that wires the
app container to declared infra dependencies. The Dockerfile stays the
app artifact; compose becomes the runtime wiring for service
dependencies; verification stays deterministic and never exposes a
host port.

Scope (MVP): **one buildable app + infra dependencies from pinned
images**. Multi-app monorepos are a later iteration.

## Decision 1 — target contract: `DeployTarget.dependencies`

```python
class ServiceDependency(BaseModel):
    name: str           # valid compose service name, never "app"
    image: str          # pinned: tag allowed, digest preferred
    env: dict[str, str] = {}
```

- `name` validator: `[a-z][a-z0-9_-]*` and not equal to `"app"` (the
  app service's reserved name); duplicate names across the list are
  rejected.
- `image` validator mirrors the base-image pinning rule: reject
  no-tag/no-digest and `:latest`; allow `redis:7-alpine`,
  `postgres:16-alpine`, `redis@sha256:...`.
- `DeployTarget.dependencies: list[ServiceDependency] = []`.
  Validator: non-empty `dependencies` require `service`
  (`dependencies` + `run` is a config error — jobs with deps are out
  of scope for the MVP).
- Non-empty `dependencies` ⇒ the artifact is **Dockerfile +
  compose.yaml**; empty ⇒ everything behaves exactly as today.
- App-to-dep connectivity is operator intent expressed through the
  existing `target.env` (e.g. `REDIS_URL=redis://cache:6379/0`); the
  model must carry it into the compose app service's `environment`.
- `IterationRecord` gains `compose: str | None`; run directories and
  golden cases persist both files.

## Decision 2 — authoring: one response, sentinel sections

One API call per iteration. When `dependencies` is non-empty the model
returns both files under deterministic sentinels:

```
=== Dockerfile ===
<content>
=== compose.yaml ===
<content>
```

- A deterministic parser splits the response; a missing or duplicated
  section is an authoring finding fed to the repair loop, never a
  crash.
- Repair receives BOTH artifacts plus the findings — cross-artifact
  fixes (e.g. a compose healthcheck needing a tool installed by the
  Dockerfile) must be possible in one iteration.
- Without dependencies the plain-Dockerfile response contract is
  unchanged (no sentinels).

Prompt rules for the compose artifact:

- The app service is named exactly `app`, built from the project
  Dockerfile: `build: {context: ".", dockerfile: "Dockerfile"}`.
- Each dependency becomes a service using the intent's `name` and
  `image` **verbatim**, with an author-chosen `healthcheck` (the model
  knows `redis-cli ping` vs `pg_isready` — that is "how", not "what").
- `app` declares `depends_on: {<dep>: {condition: service_healthy}}`
  for every dependency.
- `target.env` goes into the app service `environment`; per-dependency
  `env` into that dependency's `environment`.
- **No service may declare `ports`.** Compose networking is
  internal-only for the verifier; ingress/publishing is a deployment-
  environment concern, not this artifact's job. (`expose` is allowed
  but unnecessary.)

## Decision 3 — L1 checks (always run when dependencies are declared)

YAML parsing uses a new dependency **PyYAML**, strictly via
`yaml.safe_load`. The checks validate only the slice of the Compose
schema deployer relies on — top-level mapping, `services` mapping,
service values are mappings — never the full Compose spec.

- `compose_present`: `dependencies` declared but no compose artifact →
  FAILED (authoring).
- `compose_parses`: `safe_load` succeeds, top level is a mapping with a
  `services` mapping, each service value is a mapping.
- `compose_services`: exactly `app` + the declared dependency names;
  `app` has `build` with context `.` and dockerfile `Dockerfile`; each
  dependency's `image` equals the target's **verbatim**.
- `compose_wiring`: every dependency defines a `healthcheck`; `app`
  has `depends_on` on every dependency with
  `condition: service_healthy`; every `target.env` key appears in the
  app service `environment`; **no service declares `ports`**.

Note on `service_healthy`: modern Docker Compose and podman-compose
support it; older compose implementations may not. It is required by
the deployer verifier, not a portable fallback — documented, not
negotiated.

Existing Dockerfile L1 checks run unchanged on the Dockerfile
artifact.

## Decision 4 — L2: compose up, in-network probe, guaranteed teardown

Provider probe: `<tool> compose version` through `container_run`.
Semantics of a missing provider (deliberately NOT a skip):

- Runtime absent entirely → current static-only semantics (bench cases
  with `requires_l2` are skipped, as today).
- Runtime present, `dependencies` declared, compose provider missing →
  **FAILED, ENVIRONMENT** — a compose case must never look green
  without its L2.
- L1 compose checks run in every mode.

Execution (all through the `container_run` chokepoint):

- Isolated context, explicitly: create the temp context (existing
  CONTEXT_IGNORE copy — `.env`, `.git`, `.venv`, `.deployer` never
  reach the daemon), write the candidate `Dockerfile` AND
  `compose.yaml` into it, and run compose from that context; the
  compose `app.build.context: "."` therefore resolves inside the
  sandbox.
- Unique project name per run:
  `<tool> compose -p deployer-verify-<uuid8> -f compose.yaml up
  --build -d` — parallel bench runs and stale containers cannot
  collide on project/service names.
- Probe: our own polling loop with the `health_timeout` deadline
  (same mechanics as `_run_healthcheck`), executed inside the compose
  network: `compose -p <proj> exec app python -c
  "urllib.request.urlopen('http://127.0.0.1:<port><path>')"`. No host
  ports involved.
- On failure: `compose -p <proj> logs` (app and dependencies) feeds
  the findings; classification reuses the AUTHORING/ENVIRONMENT
  taxonomy.
- Teardown in `finally`: `compose -p <proj> down -v --timeout <n>`,
  guarded so a cleanup timeout never clobbers the real result (same
  pattern as the single-container flow).
- `memory_limit` is NOT enforced on the compose path (provider support
  is inconsistent) — documented limitation.

## Decision 5 — corpus: `compose-redis`

`corpus/synthetic/compose-redis/`:

- `project/requirements.txt`: `flask`, `redis`; `project/main.py`:
  Flask app whose `/health` performs `redis.ping()` against
  `REDIS_URL` — a green healthcheck PROVES the wiring (env
  propagation, depends_on, dependency healthcheck), not merely that
  the app started.
- `target.json`: service port 8000, `env: {"REDIS_URL":
  "redis://cache:6379/0"}`, `dependencies: [{"name": "cache",
  "image": "redis:7-alpine"}]`.
- `fixture.Dockerfile` + `fixture.compose.yaml` follow the reference
  pattern above; the corpus loader learns to pick up
  `fixture.compose.yaml`; `EXPECTED_CASES` grows to 10.
- `redis:7-alpine` (~40 MB) keeps bench cost low; a postgres case is a
  later PR after this one is green.

## Decision 6 — CLI surface

- `deployer verify` with a dependencies-bearing target reads
  `<project>/compose.yaml` alongside `<project>/Dockerfile`
  (`compose_present` FAILED when missing).
- `deployer author` writes both files into the project (and run
  directories keep per-iteration copies).
- Golden promote stores both artifacts per case (whole-snapshot
  semantics, unchanged).

## Testing & acceptance

- Unit: `ServiceDependency`/`DeployTarget` validators (pin rules,
  name rules, deps×run, deps-require-service), sentinel parser
  (both-present / missing-section / duplicated-section / no-deps
  passthrough), all L1 checks positive + negative (unparseable YAML,
  wrong service set, image mismatch, missing healthcheck, missing
  depends_on/condition, missing env key, declared ports on app AND on
  a dependency), provider-probe environment semantics.
- Docker-marked: `compose-redis` e2e — up → in-network ping probe →
  down; teardown verified (no leftover project containers).
- Fixture bench 10/10; LLM golden re-promote after a green LLM run
  (separate chore commit).
- README: artifact section mentions compose (app + pinned infra deps,
  internal-only networking).
- Gates: `uv run pytest`, `uv run pytest -m docker`,
  `uv run ruff check .`, `uv run pyrefly check`.

Workflow: branch `feature/compose` → PR; this spec is committed on
that branch.

## Out of scope

- Multi-app monorepos (several buildable services), a postgres corpus
  case (second PR), volumes/profiles/secrets, `memory_limit`
  enforcement on compose, `expose` validation, CI workflow authoring
  (the next and final Phase 4 item).
