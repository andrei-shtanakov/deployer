# Phase 4b QoL bundle (Phase 4b-4)

Date: 2026-07-22
Status: approved (brainstorm 2026-07-22; parser scope, external-filter
edge case and failure-message shape fixed in review)

Three small, independent friction points observed during the Phase 4b
research runs, bundled into one PR. Each has its own component, tests and
task; none changes the measured authoring subject except the third
(which adds a check → golden re-promote as usual).

## 1. `.env` auto-load for the anthropic author

**Observed friction:** two research runs (2026-07-22) started 0/N with
`llm_error: Could not resolve authentication method` because the CLI does
not read `./.env` — the operator must remember `set -a; source .env`.

**Design:** a deliberately narrow, dependency-free loader in the CLI
module:

- Parses only `KEY=VALUE` lines; key must match
  `[A-Za-z_][A-Za-z0-9_]*`; everything else (comments, blanks,
  `export`-prefixed lines, malformed lines) is silently skipped.
- Quotes are stripped only when the whole value is wrapped in matching
  single or double quotes. No interpolation, no escape semantics, no
  multiline values.
- Applies via `os.environ.setdefault` — the real environment always
  wins; `.env` only fills gaps.
- Loads `./.env` from the current working directory; a missing or
  unreadable file is a silent no-op.
- Called **only** immediately before the two `AnthropicAuthor()`
  constructions (`author` subcommand and `bench run --author
  anthropic`) — never before `verify`, and never from library code
  (`llm.py`/`author.py` stay env-agnostic).
- Values are never logged or echoed.
- **Scope: Anthropic author authentication only.** The CLI resolves the
  container runtime (`DEPLOYER_CONTAINER_TOOL`/`_HOST`) before the
  author is constructed, so `.env` is deliberately NOT a source of
  runtime env defaults — those still come from the real process
  environment.

## 2. `bench --filter` matches external targets

**Observed friction:** `run_bench` applies the fnmatch pattern to
synthetic cases only and raises "no corpus cases match" before externals
are even considered — running one external requires a synthetic
"carrier" case (`--filter uv-minimal --include-external`).

**Design:** in `run_bench`:

- Synthetic cases filter as today.
- With `include_external`, the same fnmatch pattern is applied to
  external target **names before cloning** — a non-matching external is
  never cloned (also saves the clone cost).
- The "no cases match" error fires only when **neither** synthetic nor
  external names match the pattern (externals counted only when
  `include_external` is set).
- `bench verify` (synthetic-only by definition) is unchanged.

Result: `bench run --author anthropic --include-external --filter
locallogai-backend` runs exactly one case, no carrier.

## 3. L1 check: `entrypoint_in_command`

**Observed gap (final review, PR #18):** with `--no-docker`, a
Dockerfile that ignores the operator's `entrypoint` intent passes
static-only verification; at L2 the mistake surfaces only through the
healthcheck, one repair iteration later than necessary.

**Design:** a new static check in `verify_static`, which gains an
optional `target: DeployTarget | None = None` parameter (passed by
`verify()`):

- **Included only when `target.entrypoint` is set** — no SKIPPED noise
  for the common case. Facts are NOT required for the check itself (the
  intent string is in the target); `verify()`'s existing facts-required
  config validation for `entrypoint` is unchanged and still runs first.
- Mechanics: only the **final stage** counts — find the last `FROM`
  and consider solely the instructions after it, taking that stage's
  last `ENTRYPOINT` and last `CMD`. (A builder-stage `CMD` is not the
  image's effective command: `FROM x AS build / CMD ["python",
  "app.py"] / FROM x` must FAIL, not false-pass — this exact case is
  unit-tested.) The check passes when `target.entrypoint` appears as a
  substring in either instruction — covering exec form, shell form,
  entrypoint-in-ENTRYPOINT with args-only CMD, and `[project.scripts]`
  names alike.
- Fails (AUTHORING) when the substring appears in neither, or when the
  final stage has no `CMD` and no `ENTRYPOINT` at all.
- The failure message names **both** the expectation and the reality, so
  the repair loop can fix it at L1 without Docker:
  `entrypoint intent 'app.py' not found in image command: ENTRYPOINT
  <...>, CMD <...>` (absent instructions rendered as `none`).
- Deliberately conservative: `CMD ["python", "-m", "main"]` fails for
  `entrypoint = "main.py"` — intended pressure toward the canonical
  exec form the prompt mandates.

**Golden impact:** the `entrypoint-override` case gains the check (its
fixture passes); committed golden diverges → re-promote after the next
green LLM run, as with every measured-subject change.

## Acceptance

- `uv run pytest`, `uv run pytest -m docker`, `bench verify` green over
  all 8 cases; fixture bench 8/8; `uv run ruff check .` and
  `uv run pyrefly check` clean (the standing repo rules, listed here
  explicitly).
- README updated: the bench section currently states `--filter` applies
  to synthetic cases only and externals are included wholesale — that
  sentence must reflect the new filtering semantics, and the `.env`
  auto-load (auth-only scope) gets a line in the usage section.
- Unit: env-loader (parses, skips junk, quote-stripping rules, env
  wins, missing file no-op); external filtering (match runs, non-match
  is not cloned, combined-empty error, synthetic-only unchanged);
  `entrypoint_in_command` matrix (exec form, shell form,
  ENTRYPOINT-holds-it + args-only CMD, scripts name, no
  CMD/ENTRYPOINT in the final stage → fail, builder-stage CMD with bare
  final stage → fail, message contains both intent and effective
  command, check absent when no entrypoint intent).
- Manual: `--author anthropic` run without sourcing `.env` succeeds;
  8/8 → `bench promote` → `bench compare` clean;
  `bench run --include-external --filter locallogai-backend` runs
  exactly one case.

## Deferred

- `.env` discovery beyond CWD (project-dir lookup, parent walk) — YAGNI
  until a real need.
- Pattern filtering for `bench verify` externals — `bench verify` has
  no external mode at all today.
