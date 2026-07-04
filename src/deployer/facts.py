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
        try:
            pyproject = tomllib.loads(pyproject_path.read_text())
        except tomllib.TOMLDecodeError:
            pyproject = {}
    project: dict[str, Any] = pyproject.get("project") or {}
    if not isinstance(project, dict):
        project = {}

    python_version: str | None = None
    pv_path = path / ".python-version"
    if pv_path.is_file():
        python_version = pv_path.read_text().strip() or None

    name = project.get("name")
    if not isinstance(name, str):
        name = None

    requires_python = project.get("requires-python")
    if not isinstance(requires_python, str):
        requires_python = None

    deps = project.get("dependencies", [])
    if isinstance(deps, list):
        dependencies = [d for d in deps if isinstance(d, str)]
    else:
        dependencies = []

    scripts = project.get("scripts", {})
    if isinstance(scripts, dict):
        entrypoints = {
            k: v
            for k, v in scripts.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    else:
        entrypoints = {}

    return ProjectFacts(
        name=name,
        requires_python=requires_python,
        python_version=python_version,
        dependencies=dependencies,
        entrypoints=entrypoints,
        has_uv_lock=(path / "uv.lock").is_file(),
    )
