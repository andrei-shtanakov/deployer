# Extras intent + source-layout facts (Phase 4b-2)

Date: 2026-07-22
Status: approved (brainstorm 2026-07-22; validation placement, extras
normalization, no-build-system×extras rule and golden-scope process rule
fixed in review)

## Motivation

Two gaps stand between the bench and a full-service locallogai-backend
target, both known since the 2026-07-04 lab_aist assessment:

1. **Extras are invisible.** The facts scanner reads only
   `[project.dependencies]`; locallogai's gradio lives in the `gui`
   optional-dependency group and llama-cpp-python in `inference`. The
   deploy intent cannot ask for an extra, the hints table cannot fire for
   extra-only packages, and the model cannot know an extra is required.
2. **Source layout is invisible.** Facts name specific files
   (`script_entrypoint`, requirement files), so the model copies only
   those — the locallogai run authored `COPY main.py` and shipped an
   image missing `agents/`, `database.py`, `webui.py` etc.

## Facts (all deterministic; missing → empty, never guessed)

Three new `ProjectFacts` fields:

```python
optional_dependencies: dict[str, list[str]] = Field(default_factory=dict)
root_modules: list[str] = Field(default_factory=list)   # ["app.py", "main.py"]
package_dirs: list[str] = Field(default_factory=list)   # ["agents", "src/foo"]
```

- `optional_dependencies`: raw requirement strings from
  `[project.optional-dependencies]`, exactly as `dependencies` is read
  today (list-of-str filter, no parsing). Group keys are stored
  **normalized** (PEP 503/685 style: lowercase, `_` → `-`).
  **Collision policy:** if two raw keys normalize to the same name
  (`my_extra` + `my-extra`), the metadata is ambiguous — the scanner
  records `optional_dependencies = {}` (invalid metadata is no fact;
  merging would silently accept ambiguity). A requested extra then fails
  target validation as unknown. This edge is unit-tested.
- `root_modules`: root-level `*.py`, minus the existing file denylist
  (`setup.py`, `conftest.py`, `manage.py`), sorted.
- `package_dirs`: directories containing **at least one root-level
  `*.py` file**, scanned at the project root and — when a `src/`
  directory exists — one level under `src/` (recorded as `"src/<pkg>"`),
  minus the directory denylist, sorted. A package dir is one *source
  unit*: it is copied whole.
  - `__init__.py` is deliberately NOT required (amended 2026-07-22
    against the real locallogai layout): the fact's purpose is "copyable
    source unit for a Dockerfile COPY", not Python packaging semantics —
    a PEP 420 namespace package like locallogai's `agents/` is exactly
    as copyable as a classic package. Classic `__init__.py` packages
    remain a subset (`__init__.py` is itself a root-level `*.py`).
  - Dirs whose Python files are only in *nested* subdirectories are not
    detected in this MVP — no full-tree recursion.
  - The denylist is what keeps this honest: without it, `scripts/`,
    `tests/`, `data/` would flood the fact.
- Directory denylist (curated, same philosophy as the entrypoint
  denylist): `tests`, `test`, `scripts`, `docs`, `examples`, `data`,
  `db`, `migrations`, `.venv`, `.git`, `__pycache__`, `.deployer`, plus
  any dot-directory.
- Unreadable/ambiguous → empty lists. No full-tree fact (prompt noise,
  invites copying junk), no import-graph analysis (dynamic imports make
  it a falsely-precise fact) — both rejected in review.

## Intent: `DeployTarget.extras`

```python
extras: list[str] = Field(default_factory=list)
```

Names of optional-dependency groups that MUST be installed in the image.
Composes with any runtime surface (service, run, build-only). Not a
secret — rendered in the prompt, no redaction.

**Canonical at the model boundary:** a `field_validator` on
`DeployTarget.extras` normalizes each name PEP 503/685 (lowercase,
`_` → `-`), rejects empty strings, and deduplicates preserving first
occurrence. Prompt, reports and golden all see the canonical form —
`["GUI", "my_extra", "my-extra"]` becomes `["gui", "my-extra"]` — so
comparability never depends on how the operator spelled the extra. This
needs no facts and lives entirely in the model.

### Validation: `validate_target_against_facts(target, facts)`

Extras cannot be validated inside a `model_validator` — the check needs
`facts.optional_dependencies`. A dedicated library-level step raises
`TargetConfigError` (a new exception); failures are **config errors**,
never AUTHORING — the model cannot fix a target that asks for a
nonexistent extra, and repair iterations must not be burned on it.

Placement is library-level so the CLI and the Python API cannot
diverge:

- `author_dockerfile()` calls it after `analyze_project()`, before the
  first `author.generate()`;
- `verify()` calls it whenever facts are provided; if `target.extras`
  is non-empty and `facts is None`, that is itself a config error —
  never a silent skip;
- the CLI catches `TargetConfigError` and maps it to exit 2 (both
  subcommands);
- `bench run` / `bench verify` treat an invalid corpus case as a config
  error surfacing as exit 2 — not as a mismatched/AUTHORING case
  result.

Rules (extra names normalized PEP 503/685 on both sides before
comparison — `GUI`/`gui`, `my_extra`/`my-extra` must not be fragile):

1. Every requested extra must exist as a key of
   `facts.optional_dependencies`.
2. `extras` + `package_manager == "pip"` + `has_build_system == False`
   → config error: pip has no way to install extras without an
   installable project. **Unsupported in this PR** — an explicit error,
   not a prompt-covered hope.

For uv projects without a build-system the combination IS supported:
`uv sync --extra <name> --no-install-project` installs the extra's
dependencies without installing the project — no conflict with the
existing "do not install the project as a package" rule.

## Hints

`collect_hints(facts, extras)` gains the requested-extras parameter:
hints are collected from base dependencies + requirement files + the
dependencies of **requested extras only**. An unrequested extra fires no
hint (locallogai: `gui` requested → no llama-cpp-python hint from the
unrequested `inference` — correct). Both call sites update together so
prompt and report never diverge: `author_dockerfile` (`hints_offered`)
and `llm._context_blocks`.

## Prompt

Deploy-intent JSON already carries `extras` via `model_dump` (visible —
by design). Two SYSTEM_PROMPT rules:

- Extras rule: install **only** the extras listed in the deploy intent —
  never all groups from `optional_dependencies` — using the package
  manager's mechanism: `uv sync --extra <name>` (with
  `--no-install-project` when has_build_system is false), or
  `pip install ".[name]"` for installable pip projects.
- Copy-source rule: when copying **application source**, use
  `root_modules` and `package_dirs` from the facts; a package dir is
  copied whole; do not COPY directories outside these facts unless the
  deploy target explicitly requires it. This governs source code only —
  metadata and lockfiles (`pyproject.toml`, `uv.lock`,
  `requirements*.txt`) are copied per the existing install-strategy
  rules and are not restricted by it. When both layout facts are empty
  (nested-only source trees the MVP scan cannot see), the rule is
  explicitly inert rather than a prohibition.
- `_context_blocks` also filters the facts JSON's
  `optional_dependencies` down to the requested extras — unrequested
  groups appear nowhere in the prompt. This is intentional: it keeps
  the model from installing (or being tempted by) extras the deploy
  intent never asked for.

## Corpus

New synthetic case **`extras-job`**: a uv project whose root `main.py`
(under a `__main__` guard) imports a package that lives only in the
`cli` extra (a small pure-Python dependency, e.g. `cowsay`) and prints a
marker. `target.json`:

```json
{"extras": ["cli"], "run": {"expect_stdout": "hello from extras-job"}}
```

(`main.py` prints that marker only after successfully importing the
extra-only package, so the oracle is proof of installation, not just of
a running interpreter.)

`run_completes` then proves the extra was actually installed — a missing
extra is an ImportError at run time, not a green build. The case ships
with `fixture.Dockerfile` and joins the golden corpus (7 cases).

Validation coverage: unit tests only (a corpus case cannot exercise a
config error — `bench verify` must stay green).

## locallogai-backend: build-only → service

`corpus/external.toml` entry gains a real target:

```toml
[targets.target]
extras = ["gui"]
[targets.target.service]
port = 7860
healthcheck_path = "/"
```

(Gradio serves 200 on `/`; `launch()` binds 127.0.0.1:7860 and the
healthcheck probes via in-container exec, so the loopback bind is fine.)

`expected_success` is **`false`** from the start, with notes naming the
real blocker: `script_entrypoint` resolves to the `main.py` stub
(main.py wins over app.py by rule), and the SYSTEM_PROMPT rule makes
that fact binding — "CMD MUST execute that file". Success would
therefore require the model to *violate* an authoring contract, which is
a contradiction between the healthcheck oracle and the prompt rules, not
honest difficulty. The research run still happens (`max_iterations = 3`)
and its outcome is recorded; the prompt rule is NOT weakened for service
intents in this PR. The real fix is a future service-entrypoint
disambiguation fact/intent — see Deferred. `expected_success` flips to
`true` only when that lands and a run proves it.

**Golden-scope process rule:** external targets never enter the golden —
`bench promote` runs are made from bench runs **without**
`--include-external`; locallogai runs are separate research runs. This
is now the recorded process, not a habit.

## Acceptance

- `uv run pytest` and `uv run pytest -m docker` green; `bench verify`
  green over all **7** synthetic cases.
- `bench run --author fixture` → 7/7 matched.
- Unit negative: requested extra absent from facts → exit 2 from both
  `verify` and `author` CLI paths; pip×no-build-system×extras → exit 2.
- Facts on the real locallogai checkout (or an equivalent fixture):
  `optional_dependencies` has `inference`/`gui`, `root_modules` includes
  `app.py`/`main.py`/`webui.py`, `package_dirs == ["agents"]`.
- Manual research run `--author anthropic` (synthetic only) → 7/7,
  `bench promote`, `bench compare` clean.
- Manual locallogai-service research run; outcome recorded in the ledger
  (and `expected_success` adjusted only on evidence).

## Testing strategy

- Unit: scanner (optional-deps parsing incl. normalization of group
  keys, the collision→`{}` edge, denylist filtering, `src/` layout,
  dot-dirs, unreadable files → empty); `DeployTarget.extras` validator
  (canonicalization, dedupe-preserving-first, empty-string rejection);
  `validate_target_against_facts` matrix (unknown extra, case/underscore
  variants accepted after normalization, pip×no-build-system rejection,
  empty extras no-op, extras-with-facts-None config error); hints
  filtering by requested extras (llama-cpp fires only when `inference`
  requested); prompt rendering (extras + layout facts present, only
  requested extras named as to-install); CLI exit-2 paths.
- Docker-marked: corpus smoke covers `extras-job` end-to-end via its
  fixture (extra actually imported at run time).
- LLM paths stay out of CI; research runs are manual.

## Deferred

- **Service-entrypoint disambiguation** (fact or intent): the mechanism
  that would let a service target name/derive its real entrypoint when
  `script_entrypoint` points at a stub (locallogai: main.py stub vs
  app.py). Prerequisite for flipping locallogai-service to
  `expected_success = true`.
- L1 check "run/service intent must COPY the entrypoint / package_dirs"
  (candidate hardening, explicitly not in this PR).
- pip×no-build-system extras support (rejected as config error for now).
- Import-graph source facts; full-tree facts (rejected).
- `.env` auto-load for `--author anthropic` (separate UX fix; noted
  2026-07-22 when a bench run silently lost auth).
