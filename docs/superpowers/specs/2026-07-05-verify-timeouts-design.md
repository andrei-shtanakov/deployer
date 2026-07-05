# Verify Timeout Forwarding — Design

Date: 2026-07-05
Status: approved by Andrei (brainstorming session)
Builds on: `2026-07-04-facts-v2-design.md` (Facts v2 merged as PR #2)

## Goal and motivation

`verify()` calls `verify_docker()` with its hardcoded defaults (600s build,
30s health) and offers no way to override them. This blocks L2 verification
for real targets: `locallogai-backend` builds `llama-cpp-python` from source
and needs more than 600 seconds (lab_aist assessment, 2026-07-04). Backlog
item from the MVP review.

Scope note: the original "items 1–3" branch shrank to this single item during
context recovery — the `environment_failure`-after-retry test and
`ProjectFacts.has_build_system` both already shipped in Facts v2
(`test_author.py::test_second_environment_failure_stops_run`,
`models.py`/`facts.py`).

## Decision

Timeouts are configured via **CLI flags**, threaded through the library as
keyword parameters. Rejected: `DeployTarget` fields (timeouts are a property
of the execution environment, not deploy intent — target stays "what, never
how"); function-params-only (unusable from the terminal, which is where
dogfooding happens).

## Changes

### `verify.py`

- New module constants `DEFAULT_BUILD_TIMEOUT = 600` and
  `DEFAULT_HEALTH_TIMEOUT = 30`; `verify_docker()`'s existing keyword
  defaults reference them instead of literals.
- `verify()` gains `*, build_timeout: int = DEFAULT_BUILD_TIMEOUT,
  health_timeout: int = DEFAULT_HEALTH_TIMEOUT` and forwards both to
  `verify_docker()`.

### `author.py`

- `author_dockerfile()` gains the same two keyword parameters (same
  defaults, imported constants) and forwards them to **both** `verify()`
  call sites (the main call and the environment-retry call).

### `cli.py`

- Both subcommands (`verify`, `author`) gain `--build-timeout` and
  `--health-timeout` (`type=int`, seconds, defaults from the constants).
- Validation: values `< 1` exit with code 2 and an error message, matching
  the existing `--max-iterations` pattern.
- Flags are passed through to `verify()` / `author_dockerfile()`.

## Behavior

Defaults are unchanged everywhere; a run without the new flags is
byte-identical to today. The constants live in `verify.py` only — no
duplicated numbers.

## Testing

- `test_verify_docker.py`: `verify()` forwards both values to
  `verify_docker` (monkeypatch spy captures kwargs).
- `test_author.py`: `author_dockerfile(build_timeout=…, health_timeout=…)`
  forwards to `verify` (spy), including on the environment-retry call.
- `test_cli.py`: flags parse and reach the library call (spy);
  `--build-timeout 0` exits 2 with an error message.
- Existing podman e2e tests untouched.

## Out of scope

- Per-target timeout persistence (would belong to a future run-config file,
  not `DeployTarget`).
- Timeouts for hadolint / container-tool bookkeeping subprocesses (fixed
  internal values, not user-facing).
