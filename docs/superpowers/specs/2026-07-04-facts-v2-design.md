# Facts v2: Package Managers + System-Dependency Hints — Design

Date: 2026-07-04
Status: approved by Andrei (brainstorming session); revised after his review
(wheel-audit of the hints table, image-size metric, install-strategy L1 check)
Builds on: `2026-07-04-deployer-mvp-design.md` (MVP merged as PR #1)

## Goal and motivation

Two gaps proven real by evidence, not hypothesized:

- **MVP dogfood**: on a fixture without `[build-system]` the model authored
  package-install Dockerfiles three times and the loop stopped on `no_progress` —
  the scanner never told it the project isn't installable.
- **lab_aist assessment**: `locallogai-model` is `requirements.txt`-only (scanner
  can't even start); `locallogai-backend` needs system packages (`build-essential`
  for a no-wheel dependency, `libpq`-style runtime libs) that no Python metadata
  reveals.

This feature closes the first two and takes the first honest bite of the third
("the hard half" the MVP spec explicitly deferred).

## Decisions made

- **System deps: three layers** — (1) curated static hints table, (2) the existing
  repair loop (build errors already reach the LLM), (3) `DeployTarget.system_packages`
  operator override. Rejected: loop-only (more iterations, worse reproducibility)
  and intent-only (pushes the hard half back onto the human).
- **requirements.txt: facts + install strategy** — new deterministic facts plus
  explicit install-strategy rules in the system prompt. Rejected: parse-into-
  dependencies-only (loses the uv-vs-pip signal the model needs).
- **Two separate fixtures** — one per feature, so a failure implicates exactly one.

## 1. Facts layer (`facts.py`, `models.py`)

`ProjectFacts` gains three fields, all deterministic (scanner still never guesses):

| Field | Type | Rule |
|---|---|---|
| `package_manager` | `Literal["uv", "pip"] \| None` | `"uv"` if `uv.lock` exists; else `"pip"` if any `requirements*.txt` exists; else `None` |
| `has_build_system` | `bool` | `[build-system]` table present in `pyproject.toml` |
| `requirements_files` | `dict[str, list[str]]` | filename → normalized package names from each top-level `requirements*.txt` |

`requirements*.txt` parsing rules (deliberately shallow):

- One requirement per line; strip comments (`#…`), blank lines, environment
  markers (`; …`), extras (`[…]`), and version specifiers (`==`, `>=`, `<`, `~=`,
  `!=`, `===`) — keep only the normalized name (lowercase, `_`→`-`).
- Lines starting with `-` (`-r`, `-e`, `--index-url`, …) are recorded verbatim
  under the same file entry prefixed as-is (so the LLM sees an `-r extra.txt`
  include exists) but are NOT resolved recursively.
- Unparseable lines are skipped, never invented (same degradation philosophy as
  the malformed-pyproject handling).

## 2. System-dependency hints (new module `hints.py`)

The model lives in `models.py` (so `AuthoringRun` can reference it without a
circular import — `hints.py` imports from `models.py`, never the reverse):

```python
class SystemDepHint(BaseModel):
    python_package: str          # normalized name that triggered the hint
    build_packages: list[str]    # apt packages needed at build/compile time
    runtime_packages: list[str]  # apt packages needed in the final image
```

The table and matcher live in `hints.py`:

- `KNOWN_SYSTEM_DEPS: dict[str, SystemDepHint]` — a curated, static table
  containing ONLY packages with no (or unreliable) wheels for the target
  platforms (linux x86_64 AND aarch64 — verification runs on Apple-Silicon
  podman, where wheel coverage is thinner). Launch set, post wheel-audit
  (state as of early 2026):
  - `psycopg2` — build: `libpq-dev, gcc`; runtime: `libpq5` (source-only by design)
  - `psycopg` — build: none; runtime: `libpq5` (pure-python wrapper needs libpq)
  - `psycopg2-binary` — explicit **no-hint entry** (empty lists): the whole point
    of the package is the prebuilt wheel; encoded so a prefix-match can never
    assign it build deps
  - `python-ldap` — build: `libldap2-dev, libsasl2-dev, gcc`; runtime: `libldap-2.5-0, libsasl2-2`
  - `uwsgi` — build: `build-essential`; runtime: none
  - `mysqlclient` — build: `default-libmysqlclient-dev, pkg-config, gcc`; runtime: `libmariadb3` (aarch64 wheels unreliable)
  - `llama-cpp-python` — build: `build-essential, cmake, git`; runtime: `libgomp1` (evidence: lab_aist)
  - `M2Crypto` — build: `libssl-dev, swig, gcc`; runtime: none
  - `pygraphviz` — build: `graphviz-dev, gcc`; runtime: `graphviz`
  - `pyaudio` — build: `portaudio19-dev, gcc`; runtime: `libportaudio2`

  Explicitly EXCLUDED as wheel-covered (would be confident false alarms whose
  cost — a useless apt layer — builds successfully and is invisible to the
  verify gate): lxml, pillow, cryptography, numpy, scipy, cffi, pycurl,
  confluent-kafka, pyodbc. Debian/bookworm package names (matches
  `python:3.x-slim`).

  **Table ownership and drift**: owner — Andrei; re-audit the table whenever the
  recommended base image major-bumps (bookworm→trixie) or at latest every 6
  months — both apt package names and wheel availability drift, and a stale
  entry fails silently (successful build, bloated image).
- `collect_hints(facts: ProjectFacts) -> list[SystemDepHint]` — matches the
  union of `facts.dependencies` and all `requirements_files` values against the
  table, using the same normalization; entries starting with `-` (recorded
  requirement-file directives like `-r extra.txt`) are skipped explicitly;
  deduplicated, sorted by package name; no-hint entries (empty lists) are
  filtered out of the result.
- **Epistemic status is explicit**: hints are NOT facts. In the prompt they
  appear under a separate heading — "Suspected system dependencies (curated
  hints — verify, and trust build errors over hints)". The table maps a name to
  *likely* apt packages; a project may still resolve to a wheel that needs none
  of them.

## 3. Intent escape hatch (`models.py`)

`DeployTarget.system_packages: list[str] = []` — apt packages the operator
demands. In the prompt these are requirements ("MUST be installed via apt-get"),
unlike hints. No validation against a package universe (operator knows best);
names are passed through verbatim.

## 4. Prompt changes (`llm.py`)

System-prompt additions (install-strategy rules):

- `package_manager == "uv"` → use the uv workflow (`COPY pyproject.toml uv.lock`,
  `uv sync --frozen`), copy uv binary from the official image.
- `package_manager == "pip"` → `COPY requirements*.txt` + `pip install -r …`;
  never invent a pyproject-based install.
- `has_build_system == false` → do NOT install the project as a package
  (`uv sync --no-install-project` / no `pip install .`); run sources directly.
- Hints section: add the listed apt packages where needed — build-stage packages
  in the builder stage, runtime packages in the final stage; drop hints the
  build proves unnecessary.
- `system_packages` from intent: unconditional `apt-get install` requirement.

User-prompt (generate/repair): facts JSON now carries the new fields; hints and
intent system_packages are rendered as their own labeled blocks.

## 5. Research seam (`models.py`, `author.py`, `verify.py`)

- `AuthoringRun` gains `hints_offered: list[SystemDepHint]` (what
  `collect_hints` returned for the run). Combined with the recorded Dockerfiles
  per iteration this lets us measure offline whether hints reduce iterations —
  no extra plumbing now (YAGNI on automatic "hint adopted" detection; the
  Dockerfile text is recorded).
- **Second quality metric — image size.** Iteration count alone is a one-sided
  measure: on wheel-covered projects a bad hint adds a useless apt layer that
  *builds successfully* and never shows up as an extra iteration. To catch
  that: `VerificationReport.image_size_bytes: int | None = None`, captured via
  `{tool} image inspect --format '{{.Size}}'` immediately after a successful
  build (before the `finally` rmi). Serialized into the run report through
  `IterationRecord.report` with zero extra plumbing. Research conclusions about
  hint value must weigh BOTH metrics (iterations saved vs bytes added).

## 5a. Install-strategy L1 check (`verify.py`)

The strongest deterministic prompt rules are promoted from prompt-hope to a
verifiable invariant — a new L1 check `install_strategy` (FAILED/authoring on
violation, PASSED otherwise, SKIPPED when facts are unavailable):

- `facts.package_manager == "pip"` and the Dockerfile invokes `uv sync` /
  `uv pip` → FAILED (wrong toolchain for the project).
- `facts.package_manager == "uv"` and the Dockerfile invokes `pip install` →
  FAILED.
- `facts.has_build_system == False` and the Dockerfile installs the project as
  a package (`pip install .` / `uv sync` without `--no-install-project`) →
  FAILED (exactly the dogfood no_progress loop, now caught statically at
  iteration 1).

Plumbing: `verify_static(dockerfile, project_path, facts: ProjectFacts | None
= None)` — the check runs only when facts are provided; `verify()` gains the
same optional parameter and passes it through; the author loop and the CLI
(`verify` command runs `analyze_project` first) supply facts. Existing callers
without facts keep the old behavior.

## 6. Fixtures and tests

- `tests/fixtures/pip_service/` — the hello service with ONLY `requirements.txt`
  (containing one comment line and no real deps) + `.python-version`; no
  pyproject. Unit tests: scanner yields `package_manager="pip"`,
  `has_build_system=False`, correct `requirements_files`. Docker e2e (FakeAuthor
  with a known-good pip-style Dockerfile): build + healthcheck pass.
- `tests/fixtures/sysdep_service/` — the hello service plus `psycopg2` in
  `requirements.txt`, an `import psycopg2` in `main.py` (importing works
  without a running PostgreSQL server — the healthcheck response includes
  `psycopg2.__version__` to prove the import genuinely executed), and a
  known-good Dockerfile that installs `libpq-dev gcc` at build and `libpq5` at
  runtime. psycopg2 (non-binary) is chosen because it is source-only BY DESIGN
  — the one hint that can never be invalidated by a future wheel (lxml, the
  earlier candidate, already ships wheels and would have made the fixture's
  premise false). Unit test: `collect_hints` returns the psycopg2 hint and NOT
  a hint for psycopg2-binary. Docker e2e: the apt-layer Dockerfile builds and
  healthchecks — first honest pipeline test of the hard half.
- Backlog item folded in (same loop, cheap): regression test for
  `stopped_reason == "environment_failure"` when a second environment failure
  occurs after the once-per-run retry budget is spent.
- Optional `llm`-marked dogfood tests on both fixtures (skipped by default) to
  measure hint effect with the real model.

## 7. Error handling

- Unreadable/undecodable `requirements.txt` → recorded as an empty list for that
  file, never a crash (same as malformed pyproject).
- Hints table lookups are total (dict get) — no failure modes.
- Nothing in this feature touches the verify layer; apt-get failures inside
  builds already flow through the existing authoring/environment classification
  (registry/network markers → environment).

## Known limitations (stated honestly)

- **Hints cover top-level dependencies only.** `collect_hints` matches
  `facts.dependencies` + top-level `requirements_files`; parsing is
  deliberately non-recursive. A *transitive* no-wheel dependency (exactly the
  locallogai-backend case) stays invisible to hints and falls through to the
  repair loop. Research conclusions must not read hint coverage as covering
  the whole "hard half".
- The hints table encodes a point-in-time wheel landscape; it fails silently
  when stale (successful build, bloated image) — hence the ownership/re-audit
  rule in §2 and the image-size metric in §5.

## Out of scope

- Automatic system-dep detection from build errors (stays with the LLM in repair).
- conda/poetry/pipenv; non-Python projects; `-r`-include resolution.
- Mounted runtime artifacts (GGUF models etc. — the locallogai-backend pattern)
  and multi-process containers.
- Distro families other than Debian slim in the hints table.
