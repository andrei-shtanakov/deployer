# deployer

Research bench for deploy-authoring agents: an LLM authors a Dockerfile from
deterministic project facts + a declarative `deploy_target` intent; a
deterministic pipeline verifies it (static checks, then a sandboxed
`docker build` + run + healthcheck) and feeds failures back for repair.
**Authoring ≠ execution**: the model only ever sees facts and reports and
returns text — files, docker, and control flow belong to the pipeline.

Facts cover uv and pip (requirements.txt) projects; a curated hints table
suggests apt packages for known no-wheel dependencies (hints, not facts —
build errors win), and `deploy_target.system_packages` lets the operator
require apt packages outright.

Design: `docs/superpowers/specs/2026-07-04-deployer-mvp-design.md`.

## Usage

```sh
uv run deployer author <project-path> [--target target.json] [--no-docker] \
    [--build-timeout 600] [--health-timeout 30]
uv run deployer verify <project-path> [--build-timeout 600] [--health-timeout 30]
# verify checks <project-path>/Dockerfile; --health-timeout is ignored for
# non-service targets. Slow source builds (e.g. llama-cpp-python) need
# --build-timeout well above the 600s default.
```

`target.json` is a `DeployTarget`: e.g.
`{"service": {"port": 8000, "healthcheck_path": "/health"}}`.
`{"system_packages": ["libpq5"]}` in the target requires apt packages
unconditionally. Design: `docs/superpowers/specs/2026-07-04-facts-v2-design.md`.
Every `author` run writes `.deployer/authoring-run.json` — iteration count,
per-check outcomes, authoring-vs-environment failure taxonomy. That file is
the research output.

## Development

```sh
uv sync
uv run pytest              # unit tests (no docker, no LLM)
uv run pytest -m docker    # + sandboxed docker build/run tests
uv run ruff format . && uv run ruff check . --fix && pyrefly check
```

Optional: `hadolint` 2.12.0 on PATH enables the lint check; runs without it
are marked non-comparable in the run report.
