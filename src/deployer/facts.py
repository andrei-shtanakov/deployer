"""Deterministic project scanner. Never guesses: missing facts stay None."""

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from deployer.models import ProjectFacts

_REQ_NAME_SPLIT = re.compile(r"[=<>!~;\[\s]")
_VALID_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_MAIN_GUARD = re.compile(r"if\s+__name__\s*==\s*[\"']__main__[\"']\s*:")
_ENTRYPOINT_DENYLIST = frozenset({"setup.py", "conftest.py", "manage.py"})


def _normalize_requirement_name(raw: str) -> str:
    """PEP 503-ish normalization: name only, lowercase, underscores to dashes."""
    name = _REQ_NAME_SPLIT.split(raw.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _parse_requirements(path: Path) -> list[str]:
    """Names from one requirements file; directives kept verbatim; never raises."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    entries: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            entries.append(stripped)
            continue
        name = _normalize_requirement_name(stripped)
        if name and _VALID_NAME.match(name):
            entries.append(name)
    return entries


def _scan_script_entrypoint(path: Path) -> str | None:
    """Root-level script with a __main__ guard; never guesses.

    main.py wins among candidates; otherwise the fact exists only when
    exactly one candidate does. Ambiguity or absence -> None.

    Denylisted files (setup.py, conftest.py, manage.py) are never
    candidates: they carry __main__ guards in the wild but are not app
    entrypoints, and a wrong authoritative fact is worse than no fact.
    """
    candidates: list[str] = []
    for file in sorted(path.glob("*.py")):
        if file.name in _ENTRYPOINT_DENYLIST:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _MAIN_GUARD.search(text):
            candidates.append(file.name)
    if "main.py" in candidates:
        return "main.py"
    if len(candidates) == 1:
        return candidates[0]
    return None


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

    requirements_files = {
        req.name: _parse_requirements(req)
        for req in sorted(path.glob("requirements*.txt"))
    }
    has_uv_lock = (path / "uv.lock").is_file()
    package_manager: Literal["uv", "pip"] | None = None
    if has_uv_lock:
        package_manager = "uv"
    elif requirements_files:
        package_manager = "pip"

    return ProjectFacts(
        name=name,
        requires_python=requires_python,
        python_version=python_version,
        dependencies=dependencies,
        entrypoints=entrypoints,
        has_uv_lock=has_uv_lock,
        package_manager=package_manager,
        has_build_system=isinstance(pyproject.get("build-system"), dict),
        script_entrypoint=_scan_script_entrypoint(path),
        requirements_files=requirements_files,
    )
