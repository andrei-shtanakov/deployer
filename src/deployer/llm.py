"""Thin Anthropic SDK wrapper implementing the DockerfileAuthor protocol."""

from typing import Any

import anthropic

from deployer.models import DeployTarget, ProjectFacts, VerificationReport

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 8192

SYSTEM_PROMPT = """\
You are a deployment artifact author. You write production-quality Dockerfiles
for Python projects managed with uv.

Rules:
- Reply with ONLY the Dockerfile content. No prose, no markdown fences.
- Only COPY files that exist in the project facts you are given. Never invent
  files.
- Pin the base image to a specific tag (never :latest, never untagged).
- The project facts are deterministic ground truth; missing facts are None —
  do not guess values for them.
- Prefer slim base images and a non-root user where practical.
"""


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
            f"Project facts (deterministic scan):\n{facts.model_dump_json(indent=2)}\n\n"
            f"Deploy intent:\n{target.model_dump_json(indent=2)}\n"
        )
        return self._complete(prompt)

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        failures = [
            r for r in report.results if r.status.value in ("failed", "warning")
        ]
        findings = "\n".join(f"- [{r.check_id}] {r.message}" for r in failures)
        prompt = (
            "The following Dockerfile failed verification. Fix it.\n\n"
            f"Dockerfile:\n{dockerfile}\n\n"
            f"Verification findings:\n{findings}\n\n"
            f"Project facts (deterministic scan):\n{facts.model_dump_json(indent=2)}\n\n"
            f"Deploy intent:\n{target.model_dump_json(indent=2)}\n"
        )
        return self._complete(prompt)
