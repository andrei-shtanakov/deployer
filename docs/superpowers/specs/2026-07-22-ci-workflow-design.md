# CI workflow authoring (Phase 4d) — design

Date: 2026-07-22
Status: approved for implementation
Prior art: `2026-07-21-bench-remote-verify-design.md` (Phase 4 item 3
tail: "then CI workflow authoring"); founding doc
`docs/idea-deployer-subproject.md` ("Agent does authoring: generate/fix
Dockerfile, CI workflows ... Execution stays deterministic: real CI
applies artifacts").

## Context

The third artifact type: a GitHub Actions **build-image workflow** that
deterministically builds the authored Dockerfile on push/PR. The agent
authors the pair atomically (Dockerfile + workflow reference the same
build context), the verifier stays fully static for the workflow — a
workflow is never executed by deployer. Scope is one kind only:
build-image, no registry push, no deploy, no secrets.

## Decision 1 — contract: `DeployTarget.ci`

```python
class CISpec(BaseModel):
    """Request for a build-image CI workflow. Presence is the request."""
```

- `DeployTarget.ci: CISpec | None = None`; `{"ci": {}}` in target.json
  requests the workflow. No `kind`, `registry`, `push` or `triggers`
  fields until a second implemented workflow kind exists — a
  discriminator now would create false extensibility and a premature
  stability commitment.
- Composes with build-only, service and run: all shapes produce one
  Dockerfile for the workflow to build.
- `ci` + `dependencies` → rejected in the `DeployTarget` **model
  validator** itself (like the service/run exclusivity rule): the
  combination involves no project facts, and model-level validation
  automatically covers author, verify, bench and direct library calls
  identically (the CLI maps the pydantic error to exit 2 as it already
  does for invalid targets). Compose-aware CI is a later iteration; an
  honest refusal beats a half-artifact.
- `IterationRecord.ci: str | None = None`; run dirs and golden persist
  the final workflow.

## Decision 2 — authoring: third sentinel section, atomic result

- Sentinel: `=== ci.yml ===`. Section order in a response: Dockerfile →
  compose (if requested) → ci (if requested).
  `parse_artifact_response(text, expects_compose, expects_ci) ->
  tuple[str, str | None, str | None]` — same line-anchored machinery;
  a malformed response is an `artifact_format` finding, never a crash.
- **Transactional writes**: parse and validate the presence of ALL
  expected sections first, only then write Dockerfile / compose.yaml /
  ci.yml. A malformed CI section must never leave a new Dockerfile
  next to a stale workflow. (The authoring loop already gets this for
  free — parse precedes any use; the CLI writes only from a parsed
  `IterationRecord`.)
- Canonical output path: `<project>/.github/workflows/ci.yml`;
  author/CLI creates parent directories.
- Prompt rules for the workflow: minimal build-image shape —
  `on: push + pull_request`; one job on the fixed runner label
  `ubuntu-24.04` (a deterministic label, NOT an immutable pin — the
  GitHub image updates under the same label; more deterministic than
  `ubuntu-latest`, and that is all we claim); steps: checkout, then
  `docker build --file ./Dockerfile .`. No secrets, no registry
  login/push, no `pull_request_target`.
- **`ACTIONS_CHECKOUT_PIN`** constant in `llm.py`: full commit SHA with
  a `# vX.Y.Z` comment (e.g.
  `actions/checkout@<40-hex>  # v4.x.x`), interpolated into the
  prompt; the fixture uses the same pin; a pin-drift test guards
  prompt ↔ fixture. Rationale: `ci_pinned` requires SHA pins, and a
  model inventing SHAs from memory would hallucinate — the one action
  the workflow needs is supplied as a verified constant.

## Decision 3 — verifier: five static checks with an explicit cascade

Check dependency tree (a failed prerequisite gives dependents SKIPPED,
never a cascading FAILED):

```
ci_present
  └─ ci_parses
       ├─ ci_wiring
       ├─ ci_pinned
       └─ actionlint
```

`ci_wiring` and `ci_pinned` are independent of each other; `actionlint`
needs an existing, syntactically readable file but not a green
wiring/pinned. All five classify failures as `failure_kind="authoring"`.

- `ci_present` — `target.ci` set but no workflow artifact → FAILED.
- `ci_parses` — `yaml.safe_load`; top level is a mapping with triggers
  and a `jobs` mapping whose jobs are mappings with `steps` lists of
  mappings. YAML 1.1 gotcha: an unquoted `on:` key parses as boolean
  `True` — normalize with a helper:
  `on_value = workflow.get("on", workflow.get(True))`; if BOTH `"on"`
  and `True` keys are present, that is an ambiguous-contract FAILED.
- `ci_wiring` — structural, never text-matching:
  - triggers include `push` and `pull_request` (mapping keys or list
    entries after normalization);
  - a checkout step identified by `uses:` (never by `name:`) and a
    build step identified by its `run:` command (multiline scalars
    parsed line-by-line, comments/empty lines skipped) exist **in the
    same job**, checkout strictly before build;
  - accepted build forms: `docker build .`,
    `docker build -f Dockerfile .`, `-f ./Dockerfile`,
    `--file=./Dockerfile` (flag with `=` or space); absolute paths and
    any other Dockerfile path are rejected;
  - forbidden: `pull_request_target` trigger anywhere; registry-push
    commands (`docker push`, `docker buildx build --push`,
    `push: true` inputs) — the `push` TRIGGER and arbitrary step text
    must not trip this; login (`docker login` commands and
    `*/login-action` uses); `secrets.` references anywhere in the
    workflow, including inside `${{ ... }}` expressions.
- `ci_pinned` — every `uses:` is a **remote** `owner/repo@<40-hex-SHA>`
  reference. `@vN`, `@main`/`@master`, missing `@` → FAILED. Local
  (`uses: ./...`) and Docker (`uses: docker://...`) forms are
  explicitly OUT of the MVP and rejected (stated rule, not an
  accident of the regex). Honesty note: L1 verifies the pin's SHAPE;
  that a SHA is genuine and matches the claimed version is not
  statically provable — hence the prompt-supplied
  `ACTIONS_CHECKOUT_PIN`.
- `actionlint` — hadolint pattern: `ACTIONLINT_VERSION` pinned
  constant; binary missing OR version mismatch → SKIPPED and the run
  marked non-comparable (a mismatched version is never executed);
  errors → FAILED (authoring). Runs against a real temporary
  `.github/workflows/ci.yml` file (proper filename/context for
  actionlint's own path-based rules), NOT stdin.

Verifier signature (exact, to protect existing positional call sites):

```python
def verify(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime | None,
    facts: ProjectFacts | None = None,
    *,
    compose: str | None = None,
    ci: str | None = None,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> VerificationReport: ...
```

CI checks are appended only when `target.ci is not None`. There is no
CI L2: workflows are not executed (no `act` — weight and fidelity are
not worth it; documented out of scope).

## Decision 4 — IO, CLI, bench plumbing

- `deployer verify`: reads `<project>/.github/workflows/ci.yml` only
  when `target.ci` is set; missing file → `ci_present` FAILED.
- `deployer author`: writes the workflow (with parent dirs) from the
  last iteration when `IterationRecord.ci` is not None — after the
  transactional-parse guarantee above.
- Bench: `fixture.ci.yml` at case level; `BenchCase.fixture_ci`;
  `FixtureAuthor(dockerfile, compose=None, ci=None)` renders the
  sentinel response; `run_case` skips a ci-target fixture case without
  `fixture.ci.yml` (FixtureAuthor only) and persists `ci.yml` in the
  case out dir; `promote_run` copies it; golden stores all final
  artifacts.

## Decision 5 — corpus: `ci-build` (corpus → 11)

`corpus/synthetic/ci-build/`: a simple uv project; `target.json` =
`{"ci": {}}` (build-only + ci); `expected.json` capabilities `["ci"]`,
`requires_l2: true` — the Dockerfile goes through the real L2 build
while the CI artifact is L1-checked. This verifies a COHERENT pair
(the workflow references the same Dockerfile and context that actually
build), not an isolated YAML. Fixtures: `fixture.Dockerfile` +
`fixture.ci.yml` (same `ACTIONS_CHECKOUT_PIN`).

## Testing & acceptance

Unit (mandatory PR gates, with fixture bench 11/11 and all existing
checks):

- contract: `ci+dependencies` rejected at shared validation (author,
  verify and bench paths all covered); `{"ci": {}}` round-trips.
- parser: three-section responses; ci-only (no compose); missing /
  duplicated / out-of-order ci section; no-ci passthrough unchanged.
- `ci_parses`: `on`-as-True normalization; both-keys ambiguity FAILED.
- `ci_wiring` negatives: missing workflow, wrong Dockerfile path,
  absolute path, checkout after build, checkout and build in
  different jobs, `pull_request_target`, `docker push` /
  `--push` / `push: true`, `docker login` and `login-action`,
  `secrets.` inside `${{ }}`; positives: every accepted `docker build`
  form; `push` trigger does NOT trip the push-command rule.
- `ci_pinned` negatives: `@v4`, `@main`, missing `@`, local `./`,
  `docker://`; positive: `owner/repo@<40-hex>`.
- actionlint: missing binary → SKIPPED + non-comparable; version
  mismatch → SKIPPED without executing; ran against a temp file path
  ending in `.github/workflows/ci.yml`.
- pin-drift: fixture `ci.yml` carries the exact `ACTIONS_CHECKOUT_PIN`.

Environment-gated (not PR-blocking when the environment lacks them):

- docker-marked e2e for `ci-build` (L2 Dockerfile build);
- actionlint at the pinned version installed locally
  (`brew install actionlint`; exact pin chosen at implementation).

Separate, explicitly paid step (never an ordinary CI gate): LLM bench
run + golden re-promote. **Promotion requires reviewing the golden
diff first** — a successful LLM run alone is not sufficient grounds to
accept a new golden state.

README: artifact section mentions the CI workflow (build-image,
no-push MVP). Gates: `uv run pytest`, `uv run pytest -m docker`,
`uv run ruff check .`, `uv run pyrefly check`.

Workflow: branch `feature/ci-workflow` → PR; this spec is committed on
that branch.

## Out of scope

- Registry push/login, deploy jobs, secrets handling;
- compose-aware CI (`ci` + `dependencies`);
- test-kind CI workflows and a `kind` discriminator;
- local (`./`) and `docker://` action forms;
- executing workflows (`act`);
- runner-image pinning beyond the fixed `ubuntu-24.04` label.
