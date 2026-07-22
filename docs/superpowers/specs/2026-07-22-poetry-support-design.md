# Poetry support (Phase 4b) — design

Date: 2026-07-22
Status: approved for implementation
Prior art: `2026-07-21-bench-remote-verify-design.md` (Phase 4 item 2:
"decide the install strategy up front").

## Context

`facts.py` detects `package_manager: "uv" | "pip"` from `uv.lock` /
`requirements*.txt`. Poetry projects currently fall to `None` (or to
`"pip"` when a stray requirements file exists), so the authoring model
gets no install strategy and legacy `[tool.poetry]` metadata is
invisible. This design adds `"poetry"` as a first-class, lockfile-first
package manager.

## Decision 1 — install strategy: Poetry in the builder stage

The authored Dockerfile pattern for `package_manager="poetry"`:

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
ENV POETRY_VIRTUALENVS_IN_PROJECT=1
RUN pip install --no-cache-dir poetry==<PINNED>
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main --no-interaction --no-ansi

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY <source facts> ./
CMD [...]
```

Rationale:

- The only option of the three considered (Poetry-in-builder,
  `poetry export`, `uv pip install -r pyproject.toml`) that honestly
  honors `poetry.lock`.
- `poetry export` is fragile since the Poetry 2.x plugin split
  (`poetry-plugin-export` is no longer bundled); `uv pip install -r
  pyproject.toml` ignores the lockfile pins entirely.
- `--no-root` preserves the established rule: do not install the
  project as a package unless the run command needs it.
- Poetry stays in the builder stage only; the runtime image carries no
  toolchain.

The Poetry version is **pinned** (`poetry==<PINNED>`): the installer
version is part of lock reproducibility. The pin lives as a named
constant in `llm.py` (next to `DEFAULT_MODEL`) and in the fixture
Dockerfile; the concrete version is confirmed at implementation time.

Console-script entrypoints: running a `[project.scripts]` /
`[tool.poetry.scripts]` console script requires the root package to be
installed. In that case the source is copied before `poetry install`
and `--no-root` is omitted. Otherwise `--no-root` is always used.

## Decision 2 — detection: lockfile-first, `poetry.lock` only

`package_manager` precedence is lockfile-first:

```
uv.lock > poetry.lock > requirements*.txt
```

- `poetry.lock` sets `package_manager="poetry"` and the new fact
  `has_poetry_lock=True` (mirror of `has_uv_lock`).
- `[tool.poetry]` without `poetry.lock` is **not** enough to set
  `package_manager`: a non-reproducible install is not detected as a
  strategy. Its metadata may still be visible via the fallback below.
- `uv.lock` + `poetry.lock` → `"uv"` (a uv lockfile usually means an
  explicit migration/override of the Poetry workflow).

Detection acceptance matrix:

| files present                        | package_manager |
| ------------------------------------ | --------------- |
| only `poetry.lock`                   | `"poetry"`      |
| `[tool.poetry]`, no lock, no reqs    | `None`          |
| `uv.lock` + `poetry.lock`            | `"uv"`          |
| `poetry.lock` + `requirements.txt`   | `"poetry"`      |
| only `requirements*.txt`             | `"pip"`         |

## Decision 3 — legacy `[tool.poetry]` metadata fallback

Legacy Poetry metadata is read as **fallback only** — it never affects
detection, only fills metadata gaps for Poetry 1.x-style projects:

- `name`: `[project].name` else `[tool.poetry].name`
- `dependencies`: `[project].dependencies` else keys of
  `[tool.poetry].dependencies` **excluding `"python"` and entries with
  `optional = true`**. Optional deps are exposed only through
  `optional_dependencies` (below) — otherwise `collect_hints()` would
  suggest system deps for extras nobody requested.
- `entrypoints`: `[project].scripts` else `[tool.poetry].scripts`
  (same `name -> "module:func"` shape)
- `optional_dependencies`: `[project.optional-dependencies]` else
  `[tool.poetry.extras]`, normalized through `normalize_extra_name()`
  with the same collision rule (collision → `{}`).

Fallback triggers only when the key is **absent** from `[project]`. A
key that is present but empty, invalid, or ambiguous (e.g. a PEP 621
extras collision normalizing to `{}`) resolves per the existing rules
and is **not** papered over by legacy metadata.

`[project]` always wins on conflict. The fallback never sets
`package_manager="poetry"`; only `poetry.lock` does.

## Decision 4 — extras for Poetry (this PR)

`target.extras` works on Poetry projects, including legacy ones, via
the `optional_dependencies` fallback above. Prompt/install rule for
`package_manager="poetry"`, one flag per requested extra:

```
poetry install --no-root --only main --extras "gui" --extras "cli"
```

Only requested extras are installed, never every group — same rule as
uv/pip. Unknown extras still exit 2 (`TargetConfigError`) before any
authoring.

## Decision 5 — L1 `install_strategy` rules

Extends `_check_install_strategy` in `verify.py`; install strategy is
an L1-checkable rule, not just a prompt rule (same promotion as uv/pip).

"pip invocation" below covers all forms: `pip install`, `pip3
install`, `python -m pip install`, `python3 -m pip install`. The
bootstrap exception applies to all of these forms as well.

For `package_manager="poetry"`:

- FAILED: `uv sync` / `uv pip` present;
- FAILED: pip invocation with `-r ...`;
- FAILED: pip invocation installing `.`;
- ALLOWED: pip invocation installing `poetry==<version>` — the builder
  bootstrap. Recognized **before** the general pip rules, otherwise the
  checker would forbid its own recommended pattern;
- WARNING: pip invocation installing `poetry` without `==` — unpinned
  installer version partially defeats lock reproducibility.

For `package_manager != "poetry"`:

- FAILED: `poetry install` present.
- A pip invocation installing `poetry==...` alone is **not** a
  violation (a weird dependency, not an install-strategy breach); the
  rule keys on `poetry install`.

Test matrix:

- poetry + `pip install poetry==1.8.5` + `poetry install --no-root
  --only main` → pass
- poetry + `pip install poetry` → warning
- poetry + `pip install -r requirements.txt` → fail
- poetry + `pip install .` → fail
- poetry + `uv sync` / `uv pip` → fail
- same outcomes for `pip3` / `python -m pip` / `python3 -m pip` forms
- uv/pip project + `poetry install` → fail
- pip project + `pip install poetry==...` without `poetry install` →
  no install-strategy violation

## Decision 6 — corpus: one synthetic `poetry-legacy` case

Target before capability: the corpus case lands first.

`corpus/synthetic/poetry-legacy/`:

- `pyproject.toml` uses legacy `[tool.poetry]`, no `[project]`;
  `[build-system]` = `poetry-core` (so `has_build_system=True` and
  entrypoint validation holds).
- `poetry.lock` is present (real lock, generated with the Poetry CLI).
- Root-level `main.py` with a `__main__` guard; Flask service
  listening on the target port.
- `target.json` = `{"service": {"port": 8000, "healthcheck_path":
  "/health"}}`.
- `expected_success=true`, `max_iterations=3`.
- `fixture.Dockerfile` uses the builder-stage Poetry pattern from
  Decision 1 (pinned Poetry, `--no-root --only main`, `.venv` copy).

Extras are covered by unit tests only in this PR (an extras e2e case
would add build/run cost to every bench run). An external pinned
Poetry target comes in a later PR, after the synthetic case is green —
otherwise two variables (Poetry support, unknown real-project
complexity) would be conflated.

## Testing & acceptance

- `test_facts.py`: the detection matrix (Decision 2) and the fallback
  matrix — `[project]` wins on conflict; `python` and `optional=true`
  deps excluded; `[tool.poetry.scripts]` validates
  `DeployTarget.entrypoint`; extras normalize/dedupe; PEP 621 extras
  collision does not fall back; `[tool.poetry]` without lock leaves
  `package_manager` unset.
- `test_verify_static.py`: the L1 test matrix (Decision 5).
- `test_llm.py`: prompt contains the Poetry install rules and the
  Poetry extras rule.
- `test_corpus.py` / fixture bench: 9/9 fixture-authored cases green.
- LLM golden: re-promote after a green LLM bench run (separate chore
  commit, as with PR #19).

Workflow: branch `feature/poetry-support` → PR; this spec is committed
on that branch.

## Out of scope

- `poetry export` support, Poetry dependency **groups**
  (`[tool.poetry.group.*]` — dev groups are simply never installed by
  `--only main`), path/git dependencies, external Poetry bench target,
  compose/CI artifacts (next Phase 4 items).
