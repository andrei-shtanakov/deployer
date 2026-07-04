"""Deterministic project scanner. Never guesses: missing facts stay None."""

import tomllib
from pathlib import Path
from typing import Any

from deployer.models import ProjectFacts


def analyze_project(path: Path) -> ProjectFacts:
    """Collect Python-level facts about the project at *path* without any LLM."""
    pyproject: dict[str, Any] = {}
    pyproject_path = path / "pyproject.toml"
    if pyproject_path.is_file():
        pyproject = tomllib.loads(pyproject_path.read_text())
    project: dict[str, Any] = pyproject.get("project") or {}
    if not isinstance(project, dict):
        project = {}

    python_version: str | None = None
    pv_path = path / ".python-version"
    if pv_path.is_file():
        python_version = pv_path.read_text().strip() or None

    return ProjectFacts(
        name=project.get("name"),
        requires_python=project.get("requires-python"),
        python_version=python_version,
        dependencies=list(project.get("dependencies", [])),
        entrypoints=dict(project.get("scripts", {})),
        has_uv_lock=(path / "uv.lock").is_file(),
    )
