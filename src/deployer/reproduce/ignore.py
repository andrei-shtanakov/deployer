"""Ignore-file selection per side and Docker's pattern subset (spec §3.2).

Supported: literal paths, ``*``, ``?``, ``**``, leading ``!``, last match
wins, and a matched directory excludes everything under it. Anything else is
named as unsupported so the check that needs it is skipped, not guessed.
"""

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IgnoreRules:
    """Patterns of one ignore file: ``(line, pattern, negated)``."""

    file: str | None
    patterns: list[tuple[int, str, bool]]
    unsupported: str | None


def ci_ignore_file(context: Path, dockerfile: str) -> str | None:
    """Docker's rule: ``<Dockerfile>.dockerignore`` next to it, else the root one."""
    df = Path(dockerfile)
    specific = df.parent / f"{df.name}.dockerignore"
    if (context / specific).is_file():
        return specific.as_posix()
    if (context / ".dockerignore").is_file():
        return ".dockerignore"
    return None


def local_ignore_file(context: Path, dockerfile: str, backend: str) -> str | None:
    """Podman prefers ``.containerignore``; otherwise as Docker."""
    if backend == "podman" and (context / ".containerignore").is_file():
        return ".containerignore"
    return ci_ignore_file(context, dockerfile)


def load_rules(context: Path, file: str | None) -> IgnoreRules:
    """Read an ignore file; comments and blank lines skipped."""
    if file is None:
        return IgnoreRules(None, [], None)
    patterns: list[tuple[int, str, bool]] = []
    unsupported: str | None = None
    text = (context / file).read_text(errors="replace")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        body = line[1:].strip() if negated else line
        if "[" in body or "\\" in body:
            unsupported = unsupported or body
            continue
        cleaned = posixpath.normpath(body.lstrip("/"))
        patterns.append((number, cleaned, negated))
    return IgnoreRules(file, patterns, unsupported)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` spans segments, ``*`` and ``?`` stay within one."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def excluded_by(rules: IgnoreRules, path: str) -> int | None:
    """The line of the rule that leaves ``path`` excluded, or ``None``."""
    candidates = _self_and_parents(posixpath.normpath(path))
    excluded_line: int | None = None
    for line, pattern, negated in rules.patterns:
        regex = glob_to_regex(pattern)
        if any(regex.match(c) for c in candidates):
            excluded_line = None if negated else line
    return excluded_line


def _self_and_parents(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: n + 1]) for n in range(len(parts))]
