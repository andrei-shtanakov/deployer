# Entrypoint intent (Phase 4b-3)

Date: 2026-07-22
Status: approved (brainstorm 2026-07-22; dependency note and
unsupported-shapes deferred added in review)
Builds on: `2026-07-22-extras-source-layout-design.md` (merged, PR #17) —
root-module validation depends on `ProjectFacts.root_modules`; this
feature is unimplementable on the older facts model.

## Motivation

The extras/source-layout research run (`llm-locallogai-svc`,
2026-07-22) isolated the last blocker for a full-service
locallogai-backend target with surgical precision: the extras half was
flawless, but `script_entrypoint` resolves to the `main.py` stub
(main.py wins over app.py by rule) and the Phase-4a SYSTEM_PROMPT rule
makes that fact binding — the model authored `CMD ["python", "main.py"]`
twice, the stub printed "Hello" and exited, and the repair loop *could
not legally* switch to `app.py` → `no_progress`. Success would have
required violating an authoring contract.

The fix is operator intent, not scanner cleverness: the scanner can say
"there is a main.py, an app.py, a webui.py" — it must not guess which
one serves the runtime surface. "Which program serves this service" is
part of the deploy intent, exactly like `port` and `healthcheck_path`.

Rejected alternatives (brainstorm 2026-07-22):

- **Server-like heuristic fact** (scan for `launch()`/`app.run()`/
  `serve_forever`): choosing among candidates is guessing, and the
  marker table rots like any framework list.
- **Relaxing the script_entrypoint MUST for service intents**: breaks
  the guarantee Phase 4a just added (invented CMDs), non-deterministic,
  burns repair iterations.
- **Intent + relaxation combined**: two mechanisms in one PR destroy
  the measurement — you cannot tell which one helped.

## Intent: `DeployTarget.entrypoint`

```python
class DeployTarget(BaseModel):
    service: ServiceSpec | None = None
    run: RunSpec | None = None
    entrypoint: str | None = None  # root module filename or [project.scripts] name
    ...
```

- Top-level, not inside `ServiceSpec`: the same stub-vs-real ambiguity
  can hit a `run` intent, and the field means the same thing for both.
  This PR exercises it via the service external target and a synthetic
  service case; run-intent usage needs no extra code.
- Value is either a **root module filename** (`"app.py"`) or a
  **`[project.scripts]` name**. Not a path: `src/x.py`, `./app.py`,
  `pkg/mod.py` are rejected by validation (below). Keeping the value a
  bare name keeps the fact/intent comparison trivial and blocks
  path-shaped ambiguity.
- Not a secret — rendered in the deploy-intent JSON as-is, no
  redaction.

## Validation (extends `validate_target_against_facts`)

Same gate, same `TargetConfigError`, same exit-2 semantics as extras —
config errors, never AUTHORING:

1. `target.entrypoint` set → it must be **either** a key of
   `facts.entrypoints` (`[project.scripts]`) **or** a filename present
   in `facts.root_modules`. Anything else — including any value
   containing a path separator — raises `TargetConfigError` naming the
   value and both fact sources checked.
2. `target.entrypoint` set + `facts is None` (in `verify()`) → config
   error, mirroring the extras rule — never a silent skip.
3. Unset → no-op; all existing targets remain valid.

## Prompt: precedence, not relaxation

The SYSTEM_PROMPT entrypoint rule is rewritten as an explicit
precedence chain:

1. **`DeployTarget.entrypoint`, if set — binding.** The CMD MUST
   execute it (exec form; package-manager equivalent allowed, e.g.
   `["uv", "run", "--no-sync", "python", "app.py"]`). *"Never override a
   DeployTarget.entrypoint. It is operator intent."*
2. Otherwise `[project.scripts]` (`entrypoints` fact) wins, as today.
3. Otherwise `script_entrypoint`, with the Phase-4a wording unchanged —
   the MUST stays a MUST; this PR weakens nothing in branches 2-3.

## Corpus

New synthetic case **`entrypoint-override`** — a deterministic
miniature of the exact locallogai shape:

- `project/main.py`: a stub under a `__main__` guard (prints and
  exits) — the file `script_entrypoint` will pick.
- `project/app.py`: a stdlib HTTP server under a `__main__` guard
  answering 200 on `/health` on port 8000 (same shape as the
  `hello_service` fixture).
- `target.json`: `{"entrypoint": "app.py", "service": {"port": 8000}}`.
- Without the intent the rules force the stub and the case fails;
  with it the rules force `app.py` — the case measures exactly this
  feature.
- Ships with `fixture.Dockerfile`, joins the golden corpus (8 cases).

## locallogai-backend: flip to expected success

`corpus/external.toml`:

```toml
[targets.target]
extras = ["gui"]
entrypoint = "app.py"
[targets.target.service]
port = 7860
healthcheck_path = "/"
```

`expected_success` flips to **`true`** (`max_iterations = 3`); notes
rewritten: the service-entrypoint blocker is closed by this PR,
capabilities gain `"entrypoint"`. If the research run fails for a new
reason, record the evidence and adjust honestly — do not pre-weaken.

## Acceptance

- `uv run pytest`, `uv run pytest -m docker`, `bench verify` green over
  all **8** synthetic cases.
- `bench run --author fixture` → 8/8 matched.
- Unit negatives: entrypoint not in facts → exit 2 (verify and author
  CLI paths); path-shaped value → exit 2; entrypoint + `facts is None`
  → config error.
- Manual `--author anthropic` run (synthetic) → 8/8, `bench promote`,
  `bench compare` clean.
- Manual locallogai research run (`--include-external`, `.env`
  sourced): **expected matched-as-success now**; outcome recorded in
  the ledger either way.

## Testing strategy

- Unit: validation matrix (scripts-name hit, root-module hit, both
  present, unknown name, path separators `/` and `\`, unset no-op,
  facts-None error); prompt precedence (rule text present, ordering
  stated, "Never override" sentence); intent JSON rendering
  (entrypoint visible, no redaction).
- Docker-marked: corpus smoke covers `entrypoint-override` end-to-end
  via its fixture (server actually healthchecks on `app.py`).
- LLM paths stay out of CI; research runs manual.

## Deferred

- **Module/path entrypoint forms** — `src/foo.py`, dotted module refs
  (`pkg.module:main`), ASGI/WSGI app references: deliberately out of
  scope (a scope cut, not an oversight) until a concrete corpus case
  needs them. Today's contract is bare names only: a root-module
  filename or a `[project.scripts]` key.
- Bench `--filter` does not match external targets (today a synthetic
  carrier case is needed to run one external — quirk observed
  2026-07-22); separate small fix.
- `.env` auto-load for `--author anthropic` (recurring operator trap).
- Run-intent corpus coverage for `entrypoint` (capability already
  works; add a case when a real need appears).
