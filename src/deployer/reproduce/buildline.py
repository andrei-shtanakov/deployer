"""The one supported ``docker build`` shape (spec §4.1). Parsed, never run."""

import posixpath
import shlex
from dataclasses import dataclass

_FORBIDDEN = (
    "--secret",
    "--ssh",
    "--mount",
    "--network",
    "--pull",
    "--no-cache",
)
_CHAIN = ("&&", "||", ";")
_OPERATORS = ("|", ">", "<", "&", "`")


@dataclass(frozen=True)
class BuildConfig:
    """What the adapter needs from the CI build line."""

    dockerfile: str
    build_args: tuple[tuple[str, str], ...]
    platform: str | None
    tag: str | None


@dataclass(frozen=True)
class Unsupported:
    """A build line outside the supported shape, and what put it there."""

    what: str


def parse_build_line(line: str) -> BuildConfig | Unsupported | None:
    """Parse one workflow ``run:`` line; ``None`` if it is not a docker build."""
    text = line.strip()
    head = text.split()[:3]
    if len(head) < 2 or head[0] != "docker" or head[1] not in ("build", "buildx"):
        return None
    if head[1] == "buildx":
        return Unsupported("buildx build") if head[2:3] == ["build"] else None
    if any(op in text for op in _CHAIN):
        return Unsupported("shell chain")
    if "$" in text:
        return Unsupported("variable expansion")
    for op in _OPERATORS:
        if op in text:
            return Unsupported(f"shell operator {op}")
    try:
        tokens = shlex.split(text)
    except ValueError:
        return Unsupported("unparseable quoting")
    return _parse_tokens(tokens[2:])


def _parse_tokens(tokens: list[str]) -> BuildConfig | Unsupported:
    dockerfile = "Dockerfile"
    build_args: list[tuple[str, str]] = []
    platform: str | None = None
    tag: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        name, eq, inline = token.partition("=")
        if not token.startswith("-"):
            positional.append(token)
            i += 1
            continue
        if name in _FORBIDDEN:
            return Unsupported(f"flag {name}")
        if name not in ("--file", "-f", "--build-arg", "--platform", "--tag", "-t"):
            return Unsupported(f"unknown flag {name}")
        if eq:
            value = inline
            i += 1
        elif i + 1 < len(tokens):
            value = tokens[i + 1]
            i += 2
        else:
            return Unsupported(f"flag {name} without a value")
        if name in ("--file", "-f"):
            dockerfile = value
        elif name == "--build-arg":
            key, sep, val = value.partition("=")
            if not sep:
                return Unsupported(f"build-arg {value} without a value")
            build_args.append((key, val))
        elif name == "--platform":
            platform = value
        else:
            tag = value
    if positional != ["."]:
        return Unsupported(f"context {' '.join(positional) or '(none)'}")
    normalised = posixpath.normpath(dockerfile)
    if dockerfile.startswith("/") or normalised.startswith(".."):
        return Unsupported(f"dockerfile path {dockerfile}")
    return BuildConfig(normalised, tuple(build_args), platform, tag)
