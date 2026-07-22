"""Sentinel-delimited multi-artifact responses: parse and render.

Deterministic pipeline code: the model returns raw text; this module
splits it. A malformed response raises ArtifactParseError, which the
authoring loop converts into an authoring finding — never a crash.
"""

DOCKERFILE_SENTINEL = "=== Dockerfile ==="
COMPOSE_SENTINEL = "=== compose.yaml ==="


class ArtifactParseError(ValueError):
    """The response does not match the required sentinel format."""


def _sentinel_line_indices(lines: list[str], sentinel: str) -> list[int]:
    """Indices of lines whose stripped content equals `sentinel` exactly.

    A sentinel is a LINE, not a substring — text like a Dockerfile
    comment that merely mentions the sentinel string does not count.
    """
    return [i for i, line in enumerate(lines) if line.strip() == sentinel]


def parse_artifact_response(text: str, expects_compose: bool) -> tuple[str, str | None]:
    """Split a raw author response into (dockerfile, compose).

    Without compose expectation the whole text is the Dockerfile —
    the single-artifact contract is unchanged.

    With compose expectation, sentinels are matched line-by-line: a
    line counts only when its stripped content equals the sentinel
    exactly, so incidental occurrences inside artifact content (e.g.
    a Dockerfile `RUN echo "=== compose.yaml ==="`) are not mistaken
    for the delimiter. Any prose the model emits before the Dockerfile
    sentinel is dropped as chatter tolerance.
    """
    if not expects_compose:
        return text.strip(), None
    lines = text.splitlines()
    dockerfile_idxs = _sentinel_line_indices(lines, DOCKERFILE_SENTINEL)
    compose_idxs = _sentinel_line_indices(lines, COMPOSE_SENTINEL)
    if len(dockerfile_idxs) != 1:
        raise ArtifactParseError(
            f"response must contain the line {DOCKERFILE_SENTINEL!r} exactly "
            f"once (found {len(dockerfile_idxs)}); reply with both sections "
            f"under {DOCKERFILE_SENTINEL!r} and {COMPOSE_SENTINEL!r}, each "
            "sentinel on its own line"
        )
    if len(compose_idxs) != 1:
        raise ArtifactParseError(
            f"response must contain the line {COMPOSE_SENTINEL!r} exactly "
            f"once (found {len(compose_idxs)}); reply with both sections "
            f"under {DOCKERFILE_SENTINEL!r} and {COMPOSE_SENTINEL!r}, each "
            "sentinel on its own line"
        )
    dockerfile_idx = dockerfile_idxs[0]
    compose_idx = compose_idxs[0]
    if compose_idx < dockerfile_idx:
        raise ArtifactParseError(
            f"{DOCKERFILE_SENTINEL!r} must come before {COMPOSE_SENTINEL!r}"
        )
    dockerfile = "\n".join(lines[dockerfile_idx + 1 : compose_idx]).strip()
    compose = "\n".join(lines[compose_idx + 1 :]).strip()
    if not dockerfile or not compose:
        raise ArtifactParseError("both artifact sections must be non-empty")
    return dockerfile, compose


def render_artifact_response(dockerfile: str, compose: str | None) -> str:
    """Inverse of parse: the format fixture authors and prompts use."""
    if compose is None:
        return dockerfile
    return f"{DOCKERFILE_SENTINEL}\n{dockerfile}\n{COMPOSE_SENTINEL}\n{compose}"
