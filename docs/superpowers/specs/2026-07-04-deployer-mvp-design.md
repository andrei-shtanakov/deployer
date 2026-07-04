# Deployer MVP: Intent + Dockerfile Author — Design

Date: 2026-07-04
Status: approved by Andrei (brainstorming session); revised after external review
(sandbox, failure taxonomy, success gate)

## Goal and framing

`deployer` starts as a **research bench → utility hybrid**: we study deploy-agent
patterns (authoring loop, deterministic verification feedback, gate seams), but the
contracts are designed from day one for later practical use on real projects.

Founding constraint (from `docs/idea-deployer-subproject.md`): **authoring ≠
execution**. The LLM only authors artifacts and reads verification reports; files are
written by the pipeline and all execution (docker build, container run) is done by
deterministic code, never by the agent.

## MVP slice

A vertical slice on a single artifact type: given a Python project and a
`deploy_target` intent, the system generates a working `Dockerfile`, verified by a
two-level deterministic check (static + real `docker build`).

Decisions made:

- **Form**: Python library core + thin CLI on top. An MCP wrapper can be added later
  over the same core; it is out of scope for the MVP.
- **Agent stack**: direct Anthropic SDK calls with pydantic structured output. No
  autogen, no agent framework — the loop is a plain `while` controlled by code.
- **Loop architecture**: deterministic pipeline with an LLM step inside (not an
  agent-with-tools; not templates-with-LLM-fill).

## Architecture

Flat module layout, `src/deployer/`, ~6 modules (lab rule: minimum files):

| Module | Responsibility |
|---|---|
| `models.py` | All pydantic contracts: `DeployTarget` (intent: base image preference, port, healthcheck, resources, env), `ProjectFacts`, `CheckResult` / `VerificationReport` (each failure tagged `failure_kind: authoring \| environment`), `AuthoringRun` (per-iteration record — research metrics) |
| `facts.py` | `analyze_project(path) -> ProjectFacts` — deterministic scanner, no LLM: reads `pyproject.toml`, `uv.lock`, `.python-version`, entrypoints |
| `verify.py` | `verify(...) -> VerificationReport` — pluggable list of checks in two levels: L1 static (Dockerfile parses; `COPY`/`ADD` paths reference real project files; warn on unpinned base image tag / missing digest; hadolint at a pinned version), L2 sandboxed `docker build` + container run with healthcheck |
| `author.py` | Control loop: LLM generates Dockerfile → verify → on failure LLM repairs from the report → max N iterations (default 3); early-stop if the error signature is unchanged after a repair (no-progress detection) |
| `llm.py` | Thin Anthropic SDK wrapper with pydantic structured output |
| `cli.py` | `deployer author <path> [--target target.yaml]` — full loop; `deployer verify <path>` — run the check suite against the project's existing `Dockerfile` (no LLM) |

### Data flow

```
CLI → analyze_project() → ProjectFacts
    → author loop:
        LLM(facts + intent) → Dockerfile candidate
        → verify L1 (static, fast)   → fail? → LLM repairs from report
        → verify L2 (docker build)   → fail? → LLM repairs from report
    → output: Dockerfile + AuthoringRun report (JSON)
```

### Success gate

Build success alone is NOT success — an image can build and still fail to run the
app. When the intent declares a service (port/healthcheck present in
`DeployTarget`), L2 success requires: build passes **and** the container starts
**and** the healthcheck responds. MVP fixtures are all service-type, so in practice
the full gate always applies; the build-only gate exists solely for non-service
intents.

### L2 sandbox (day-one requirement)

`docker build` executes LLM-authored `RUN` instructions — the runner is
deterministic, but its input is untrusted model output. This is the sharpest risk in
the design and is mitigated from the first implementation, not retrofitted:

- Prefer rootless podman / rootless docker for the build daemon.
- Build runs against an isolated build context (only the target project is copied
  in); no secrets in the environment or context, ever.
- Resource limits and a hard timeout on build; never `--privileged`.
- The healthcheck *run* stage uses `--network=none` (the app under test needs no
  egress). The *build* stage cannot be network-less — package installation requires
  the network — which is exactly why the other controls above are mandatory.

### Design principles baked in

- **authoring ≠ execution**: the LLM sees only facts and reports; the pipeline writes
  files; deterministic code runs docker.
- **Research seam**: every run produces an `AuthoringRun` JSON (iteration count, which
  checks failed, timings) — raw material for "what works" conclusions.
- **Failure taxonomy**: every check failure is classified `authoring` (the model
  produced a bad artifact) vs `environment` (registry flake, network, timeout).
  Environment failures do not consume a repair iteration and are excluded from
  authoring-quality metrics — otherwise infra noise poisons the research signal.
  For run comparability, hadolint is a pinned dev dependency; a run without it is
  marked non-comparable rather than silently skipping the check.
- **Pluggable seams**: checks are a list where an arbiter gate and ATP smoke tests
  slot in later; the `DeployTarget` intent schema is the attachment point for the
  future MLOps layer (`docs/idea-mlops-layer.md`).
- **Ecosystem boundary**: deployer does not reimplement proctor-a's `infra/` plans;
  scope here is authoring + local verification only.

## Error handling

- **Loop budget**: max 3 iterations by default; on exhaustion return the best
  candidate with a failed `VerificationReport` — never silently succeed.
- **No-progress early-stop**: after a repair, compare the normalized error signature
  (check id + key error line) with the previous iteration; if unchanged, stop —
  the model is oscillating and further iterations waste budget and skew metrics.
- **Environment failures** (see failure taxonomy): retried once without consuming a
  repair iteration; recorded separately in `AuthoringRun`.
- **Docker unavailable**: degrade to static-only verification with an explicit
  warning in the report (`AuthoringRun.docker_available = false`); such runs never
  count as full successes and are excluded from L2 metrics.
- **LLM output**: validated via pydantic structured output; validation failure counts
  as an iteration and feeds the error back to the model.
- **Determinism of facts**: `analyze_project` never guesses; missing facts are
  explicit `None`s the LLM must handle, not hallucinated defaults.

## Testing

- **Unit** (no LLM, no docker): `facts.py` scanner and all L1 static checks.
- **Loop integration**: `author.py` driven by a fake LLM stub (deterministic canned
  responses) — covers repair-on-failure and iteration-budget paths.
- **E2E** (pytest marker `-m docker`, skipped when docker is absent): real
  `docker build` on a tiny fixture project in `tests/fixtures/` and on deployer
  itself (dogfood).

Framework: `uv run pytest`, anyio for any async tests.

## Known limitation stated honestly

`analyze_project` covers the easy half (Python-level facts from
`pyproject.toml`/`uv.lock`). The hard half of real Dockerfile authoring is **system
dependencies** (build-essential, libpq, ffmpeg) that are invisible in Python
metadata. The "facts never guess → explicit `None`" rule means the loop will
predictably stumble exactly there, and tiny fixtures hide it. This is deferred, not
solved. Backlog: a fixture with a system dependency (e.g. psycopg2 needing libpq) as
the first probe of the hard case.

## Research design notes

- **Baseline arm**: compare LLM-authored Dockerfiles against the official uv
  Dockerfile parameterized for the fixture — a near-free quality reference without
  manual labeling. It is a baseline, not an oracle: not ground truth for arbitrary
  projects.
- **Named tension — free-form vs templates**: the spec chose free-form authoring
  because the research goal is to measure the model's authoring ability. For the
  *utility* goal, template+fill would give higher success-rate and reproducibility
  at near-zero risk. `AuthoringRun` data is the decision criterion for "is free-form
  good enough, or fall back to templates" — this dual goal must not blur the
  metrics; runs measure free-form, the baseline arm represents templates.
- **Phase 2 — agent-with-tools comparison arm**: after the pipeline works, implement
  the rejected alternative (LLM drives itself via read_file/write_artifact/run_verify
  tools) as a second mode over the same fixtures and the same `AuthoringRun` schema.
  Pipeline vs agent then becomes a measured comparison, not a guess.

## Open questions

- Structured output wrapping the Dockerfile as a JSON string field may degrade code
  generation quality (JSON escaping). Check on a fixture whether a plain-text block
  for the Dockerfile + separate structured metadata performs better.

## Out of scope for the MVP

- CI workflows, Helm, Terraform artifact types (contract must be extensible to them,
  but they are not built now).
- MCP server wrapper; Maestro/arbiter/ATP integration (seams only).
- Any mutating deploy actions (push, apply, rollout) — per the founding constraint.
