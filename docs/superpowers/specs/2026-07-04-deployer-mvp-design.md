# Deployer MVP: Intent + Dockerfile Author — Design

Date: 2026-07-04
Status: approved by Andrei (brainstorming session)

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
| `models.py` | All pydantic contracts: `DeployTarget` (intent: base image preference, port, healthcheck, resources, env), `ProjectFacts`, `CheckResult` / `VerificationReport`, `AuthoringRun` (per-iteration record — research metrics) |
| `facts.py` | `analyze_project(path) -> ProjectFacts` — deterministic scanner, no LLM: reads `pyproject.toml`, `uv.lock`, `.python-version`, entrypoints |
| `verify.py` | `verify(...) -> VerificationReport` — pluggable list of checks in two levels: L1 static (Dockerfile parses; `COPY`/`ADD` paths reference real project files; hadolint if installed), L2 real `docker build` (+ optional container run with healthcheck) |
| `author.py` | Control loop: LLM generates Dockerfile → verify → on failure LLM repairs from the report → max N iterations (default 3) |
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

### Design principles baked in

- **authoring ≠ execution**: the LLM sees only facts and reports; the pipeline writes
  files; deterministic code runs docker.
- **Research seam**: every run produces an `AuthoringRun` JSON (iteration count, which
  checks failed, timings) — raw material for "what works" conclusions.
- **Pluggable seams**: checks are a list where an arbiter gate and ATP smoke tests
  slot in later; the `DeployTarget` intent schema is the attachment point for the
  future MLOps layer (`docs/idea-mlops-layer.md`).
- **Ecosystem boundary**: deployer does not reimplement proctor-a's `infra/` plans;
  scope here is authoring + local verification only.

## Error handling

- **Loop budget**: max 3 iterations by default; on exhaustion return the best
  candidate with a failed `VerificationReport` — never silently succeed.
- **Docker unavailable**: degrade to static-only verification with an explicit
  warning in the report (`AuthoringRun.docker_available = false`).
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

## Out of scope for the MVP

- CI workflows, Helm, Terraform artifact types (contract must be extensible to them,
  but they are not built now).
- MCP server wrapper; Maestro/arbiter/ATP integration (seams only).
- Any mutating deploy actions (push, apply, rollout) — per the founding constraint.
