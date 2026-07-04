"""Curated system-dependency hints.

Hints are NOT facts: the table lists only packages with no (or unreliable)
wheels for linux x86_64/aarch64 as of early 2026. Owner: Andrei. Re-audit on
base-image major bumps (bookworm->trixie) or every 6 months — stale entries
fail silently (successful build, bloated image). Debian (bookworm/trixie) names.
"""

import re

from deployer.models import ProjectFacts, SystemDepHint

_NAME_SPLIT = re.compile(r"[=<>!~;\[\s]")

KNOWN_SYSTEM_DEPS: dict[str, SystemDepHint] = {
    "psycopg2": SystemDepHint(
        python_package="psycopg2",
        build_packages=["libpq-dev", "gcc", "libc6-dev"],
        runtime_packages=["libpq5"],
    ),
    # Explicit no-hint entry: the whole point of -binary is the prebuilt wheel.
    "psycopg2-binary": SystemDepHint(python_package="psycopg2-binary"),
    "psycopg": SystemDepHint(python_package="psycopg", runtime_packages=["libpq5"]),
    "python-ldap": SystemDepHint(
        python_package="python-ldap",
        build_packages=["libldap2-dev", "libsasl2-dev", "gcc", "libc6-dev"],
        runtime_packages=["libldap-2.5-0", "libsasl2-2"],
    ),
    "uwsgi": SystemDepHint(python_package="uwsgi", build_packages=["build-essential"]),
    "mysqlclient": SystemDepHint(
        python_package="mysqlclient",
        build_packages=["default-libmysqlclient-dev", "pkg-config", "gcc", "libc6-dev"],
        runtime_packages=["libmariadb3"],
    ),
    "llama-cpp-python": SystemDepHint(
        python_package="llama-cpp-python",
        build_packages=["build-essential", "cmake", "git"],
        runtime_packages=["libgomp1"],
    ),
    "m2crypto": SystemDepHint(
        python_package="m2crypto",
        build_packages=["libssl-dev", "swig", "gcc", "libc6-dev"],
    ),
    "pygraphviz": SystemDepHint(
        python_package="pygraphviz",
        build_packages=["graphviz-dev", "gcc", "libc6-dev"],
        runtime_packages=["graphviz"],
    ),
    "pyaudio": SystemDepHint(
        python_package="pyaudio",
        build_packages=["portaudio19-dev", "gcc", "libc6-dev"],
        runtime_packages=["libportaudio2"],
    ),
}


def _normalize(raw: str) -> str:
    name = _NAME_SPLIT.split(raw.strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def collect_hints(facts: ProjectFacts) -> list[SystemDepHint]:
    """Match project dependencies against the curated table.

    Top-level dependencies only (pyproject deps + requirements files);
    transitive no-wheel packages stay invisible and fall through to the
    repair loop — a documented limitation, not a bug.
    """
    candidates: set[str] = set()
    for dep in facts.dependencies:
        candidates.add(_normalize(dep))
    for entries in facts.requirements_files.values():
        for entry in entries:
            if entry.startswith("-"):
                continue
            candidates.add(_normalize(entry))
    hints: list[SystemDepHint] = []
    for name in sorted(candidates):
        hint = KNOWN_SYSTEM_DEPS.get(name)
        if hint is not None and (hint.build_packages or hint.runtime_packages):
            hints.append(hint)
    return hints
