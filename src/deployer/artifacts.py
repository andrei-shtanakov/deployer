"""Sentinel-delimited multi-artifact responses: parse and render.

Deterministic pipeline code: the model returns raw text; this module
splits it. A malformed response raises ArtifactParseError, which the
authoring loop converts into an authoring finding — never a crash.
"""

DOCKERFILE_SENTINEL = "=== Dockerfile ==="
COMPOSE_SENTINEL = "=== compose.yaml ==="
CI_SENTINEL = "=== ci.yml ==="


class ArtifactParseError(ValueError):
    """The response does not match the required sentinel format."""


def _sentinel_line_indices(lines: list[str], sentinel: str) -> list[int]:
    """Indices of lines whose stripped content equals `sentinel` exactly.

    A sentinel is a LINE, not a substring — text like a Dockerfile
    comment that merely mentions the sentinel string does not count.
    """
    return [i for i, line in enumerate(lines) if line.strip() == sentinel]


def parse_artifact_response(
    text: str, expects_compose: bool, expects_ci: bool = False
) -> tuple[str, str | None, str | None]:
    """Split a raw author response into (dockerfile, compose, ci).

    With no extra sections expected the whole text is the Dockerfile —
    the single-artifact contract is unchanged. Otherwise sentinels are
    matched line-anchored (a line counts only when its stripped content
    equals the sentinel exactly), sections must appear in the order
    Dockerfile -> compose -> ci, each exactly once and non-empty.
    Prose before the Dockerfile sentinel is dropped as chatter.
    """
    expected = [DOCKERFILE_SENTINEL]
    if expects_compose:
        expected.append(COMPOSE_SENTINEL)
    if expects_ci:
        expected.append(CI_SENTINEL)
    if len(expected) == 1:
        return text.strip(), None, None
    lines = text.splitlines()
    listed = ", ".join(repr(s) for s in expected)
    positions: list[int] = []
    for sentinel in expected:
        idxs = _sentinel_line_indices(lines, sentinel)
        if len(idxs) != 1:
            raise ArtifactParseError(
                f"response must contain the line {sentinel!r} exactly once "
                f"(found {len(idxs)}); reply with one section per expected "
                f"sentinel ({listed}), each sentinel on its own line"
            )
        positions.append(idxs[0])
    if positions != sorted(positions):
        order = " -> ".join(repr(s) for s in expected)
        raise ArtifactParseError(f"sections must appear in order: {order}")
    bounds = positions[1:] + [len(lines)]
    contents: list[str] = []
    for start, end in zip(positions, bounds):
        section = "\n".join(lines[start + 1 : end]).strip()
        if not section:
            raise ArtifactParseError("every artifact section must be non-empty")
        contents.append(section)
    dockerfile = contents[0]
    compose = contents[1] if expects_compose else None
    ci = contents[-1] if expects_ci else None
    return dockerfile, compose, ci


def render_artifact_response(
    dockerfile: str, compose: str | None = None, ci: str | None = None
) -> str:
    """Inverse of parse: the format fixture authors and prompts use."""
    if compose is None and ci is None:
        return dockerfile
    parts = [DOCKERFILE_SENTINEL, dockerfile]
    if compose is not None:
        parts.extend([COMPOSE_SENTINEL, compose])
    if ci is not None:
        parts.extend([CI_SENTINEL, ci])
    return "\n".join(parts)
