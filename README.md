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
(latest run only). Alongside `hadolint_available`/`actionlint_available`/
`docker_available`, the report carries `atp_available` (whether the `atp_smoke`
check actually ran `atp`, as opposed to reporting `SKIPPED`) and `built_image`
(the L2 image's tag, runtime and cleanup outcome — see "Bench" below for the
`smoke` intent that consumes it).

### Report schema version

Every report deployer writes — `verify-report.json`, `authoring-run.json`,
`bench-report.json`, `golden.json` — carries `schema_version`, currently
`"1.0"`. A consumer should pin against it rather than against the shape.

The compatibility rules:

- A document with **no** `schema_version` key predates versioning and reads
  as `"0"`. deployer applies that when it reads such a file back; a consumer
  parsing these documents should do the same.
- Within a major version, **added fields are compatible**: a new report field
  does not bump the version. Only a breaking change to an existing field does.
- Because v1 is purely additive over v0, `bench compare` still diffs a v0
  baseline against a v1 run, and does not report the version gap as a
  finding.
- deployer holds itself to the same rule when reading a report back: a
  document whose **major** is neither `0` nor `1` is refused (`error:` and
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
