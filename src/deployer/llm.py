"""Thin Anthropic SDK wrapper implementing the DockerfileAuthor protocol."""

import hashlib
import json
from typing import Any

import anthropic

from deployer.artifacts import COMPOSE_SENTINEL, DOCKERFILE_SENTINEL
from deployer.hints import collect_hints
from deployer.models import AuthorInfo, DeployTarget, ProjectFacts, VerificationReport

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 8192
POETRY_VERSION = "2.4.1"

SYSTEM_PROMPT = f"""\
You are a deployment artifact author. You write production-quality Dockerfiles
for Python projects.

Rules:
- Reply with ONLY the Dockerfile content. No prose, no markdown fences.
- Only COPY files that exist in the project facts you are given. Never invent
  files.
- Pin the base image to a specific tag (never :latest, never untagged).
- The project facts are deterministic ground truth; missing facts are None —
  do not guess values for them.
- Install strategy follows package_manager from the facts:
  - "uv": COPY pyproject.toml and uv.lock, install with `uv sync --frozen`,
    copying the uv binary from the official uv image.
  - "poetry": two-stage build. Builder stage: install the pinned
    installer with `pip install --no-cache-dir poetry=={POETRY_VERSION}`
    (always this exact pin), set ENV POETRY_VIRTUALENVS_IN_PROJECT=1,
    COPY pyproject.toml and poetry.lock, then run
    `poetry install --no-root --only main --no-interaction --no-ansi`.
    Final stage: COPY /app/.venv from the builder and prepend
    /app/.venv/bin to PATH; never install Poetry in the final stage.
    Never install dependencies with pip directly — poetry.lock is the
    only dependency source. Exception: running a console script
    requires the root package; then COPY the source before
    `poetry install` and drop `--no-root`.
  - "pip": COPY the requirements file(s) and use
    `pip install --no-cache-dir -r <file>`. Never invent a pyproject-based
    install for a pip project.
  - null: no lockfile or requirements exist; run the sources directly.
- If has_build_system is false, do NOT install the project as a package
  (no `pip install .`; with uv always pass `--no-install-project`). Run the
  sources directly.
- Extras listed in the deploy intent MUST be installed — and ONLY those
  extras, never every group in optional_dependencies. Use the package
  manager's mechanism: `uv sync --extra <name>` (adding
  `--no-install-project` when has_build_system is false), or
  `pip install ".[name]"` for installable pip projects,
  or `poetry install --no-root --only main --extras "<name>"` (repeat
  `--extras` once per requested extra) for poetry projects.
- When copying application source, use root_modules and package_dirs
  from the facts; a package dir is copied whole. Do not COPY directories
  outside these facts unless the deploy intent explicitly requires it.
  This governs source code only — copy metadata and lockfiles
  (pyproject.toml, uv.lock, requirements files) per the install
  strategy above.
  If both root_modules and package_dirs are empty, this rule is inert:
  copy whatever sources the entrypoint requires.
- Container-command precedence:
  1. If the deploy intent sets "entrypoint", the CMD MUST execute it in
     exec form: a filename runs via the interpreter (e.g.
     CMD ["python", "app.py"] or the package-manager equivalent); a
     [project.scripts] name runs as its console script. Never override
     a DeployTarget.entrypoint. It is operator intent.
  2. Otherwise, when entrypoints ([project.scripts]) is non-empty, it
     wins: run the named console script.
  3. Otherwise script_entrypoint is deterministic ground truth. If it is
     set the Dockerfile CMD MUST execute that file in exec form (e.g.
     CMD ["python", "main.py"] or the package-manager equivalent).
  Never invent servers such as http.server, never leave a bare
  interpreter, never run a file not present in the facts.
- A "run" deploy intent means a job image: the CMD must execute the
  project's entrypoint (per the rules above) and exit 0 when the work
  completes. The container's stdout is checked against a held-back
  oracle you cannot see, so the only winning strategy is to actually
  run the project's code — never fake output with echo, never leave a
  bare interpreter, never author a long-running server for a run
  intent.
- Packages listed under "Required system packages" MUST be installed via
  apt-get.
- "Suspected system dependencies" are curated hints, not facts: verify them,
  put build packages in the build stage and runtime packages in the final
  stage, and trust build errors over hints.
- Prefer slim base images and a non-root user where practical.
- When the deploy intent declares "dependencies", you author TWO
  artifacts and reply with exactly two sections:
  {DOCKERFILE_SENTINEL}
  <the Dockerfile>
  {COMPOSE_SENTINEL}
  <the compose.yaml>
  Compose rules: the buildable service is named exactly "app" and
  builds from the project Dockerfile (build: {{context: ".",
  dockerfile: "Dockerfile"}}). Each dependency becomes a service using
  the intent's name and image verbatim, with a healthcheck you choose
  for that image. "app" must declare depends_on with
  condition: service_healthy for every dependency. Deploy-intent env
  goes into the app service environment; per-dependency env into that
  dependency's environment. Services never declare ports — compose
  networking is internal-only here; ingress is not this artifact's
  job. Without "dependencies", reply with only the Dockerfile as
  before — no sentinels.
"""


def _intent_json(target: DeployTarget) -> str:
    """Deploy intent for the prompt, with the run oracle redacted.

    The model may see that a run intent exists — never the expected
    stdout, or `CMD ["echo", ...]` would game the check.
    """
    data = target.model_dump()
    if data.get("run") is not None:
        data["run"] = {}
    return json.dumps(data, indent=2)


def _context_blocks(facts: ProjectFacts, target: DeployTarget) -> str:
    facts_data = facts.model_dump()
    if target.extras:
        facts_data["optional_dependencies"] = {
            k: v
            for k, v in facts_data["optional_dependencies"].items()
            if k in target.extras
        }
    else:
        facts_data["optional_dependencies"] = {}
    blocks = [
        f"Project facts (deterministic scan):\n{json.dumps(facts_data, indent=2)}",
        f"Deploy intent:\n{_intent_json(target)}",
    ]
    if target.system_packages:
        listed = "\n".join(f"- {p}" for p in target.system_packages)
        blocks.append(
            "Required system packages (operator intent, MUST install via "
            f"apt-get):\n{listed}"
        )
    hints = collect_hints(facts, target.extras)
    if hints:
        listed = "\n".join(
            f"- {h.python_package}: build={h.build_packages}, "
            f"runtime={h.runtime_packages}"
            for h in hints
        )
        blocks.append(
            "Suspected system dependencies (curated hints — verify, and "
            f"trust build errors over hints):\n{listed}"
        )
    return "\n\n".join(blocks)


def _extract_dockerfile(text: str) -> str:
    """Strip optional markdown fences the model might add despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop opening fence (with optional language tag)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


class AnthropicAuthor:
    """DockerfileAuthor backed by the Anthropic Messages API."""

    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        self._client = client if client is not None else anthropic.Anthropic()
        self._model = model

    def info(self) -> AuthorInfo:
        """Comparability metadata for run reports."""
        return AuthorInfo(
            backend="anthropic",
            model_id=self._model,
            prompt_sha256=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        )

    def _complete(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return _extract_dockerfile(text)

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        prompt = (
            "Write a Dockerfile for this project.\n\n"
            + _context_blocks(facts, target)
            + "\n"
        )
        return self._complete(prompt)

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        artifact_text: str,
        report: VerificationReport,
    ) -> str:
        failures = [
            r for r in report.results if r.status.value in ("failed", "warning")
        ]
        findings = "\n".join(f"- [{r.check_id}] {r.message}" for r in failures)
        prompt = (
            "The following Dockerfile failed verification. Fix it.\n\n"
            f"Current artifacts:\n{artifact_text}\n\n"
            f"Verification findings:\n{findings}\n\n"
            + _context_blocks(facts, target)
            + "\n"
        )
        return self._complete(prompt)
