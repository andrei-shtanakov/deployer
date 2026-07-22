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
actionlint.

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
    [--build-timeout 600] [--health-timeout 30]
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
missing `Dockerfile` for `verify`); `2` invalid invocation (bad flag
values, project path not a directory, unreadable or invalid `--target`,
invalid runtime configuration).
`verify` writes its full report to `<project>/.deployer/verify-report.json`
(latest run only).

`author` and `bench run --author anthropic` auto-load `./.env`
(KEY=VALUE lines) for the Anthropic API key; real environment variables
always win, and runtime flags (`DEPLOYER_CONTAINER_*`) are NOT read
from `.env`.

`target.json` is a `DeployTarget`: e.g.
`{"service": {"port": 8000, "healthcheck_path": "/health"}}`.
`{"system_packages": ["libpq5"]}` in the target requires apt packages unconditionally.
`{"extras": ["gui"]}` installs optional-dependency groups.
`{"entrypoint": "app.py"}` specifies the bare filename or [project.scripts] name to run.
Design: `docs/superpowers/specs/2026-07-04-facts-v2-design.md`.
Every `author` run writes `.deployer/authoring-run.json` — iteration count,
per-check outcomes, authoring-vs-environment failure taxonomy. That file is
the research output.

## Bench

The corpus (`corpus/synthetic/`) is a set of small target projects with
declared intent (`target.json`) and expectations (`expected.json`).

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

## Development

```sh
uv sync
uv run pytest              # unit tests (no docker, no LLM)
uv run pytest -m docker    # + sandboxed docker build/run tests
uv run ruff format . && uv run ruff check . --fix && pyrefly check
```

Optional: `hadolint` 2.12.0 on PATH enables the lint check; runs without it
are marked non-comparable in the run report.
