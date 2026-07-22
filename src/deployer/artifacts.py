"""Sentinel-delimited multi-artifact responses: parse and render.

Deterministic pipeline code: the model returns raw text; this module
splits it. A malformed response raises ArtifactParseError, which the
authoring loop converts into an authoring finding — never a crash.
"""

DOCKERFILE_SENTINEL = "=== Dockerfile ==="
COMPOSE_SENTINEL = "=== compose.yaml ==="


class ArtifactParseError(ValueError):
    """The response does not match the required sentinel format."""


def parse_artifact_response(text: str, expects_compose: bool) -> tuple[str, str | None]:
    """Split a raw author response into (dockerfile, compose).

    Without compose expectation the whole text is the Dockerfile —
    the single-artifact contract is unchanged.
    """
    if not expects_compose:
        return text.strip(), None
    for sentinel in (DOCKERFILE_SENTINEL, COMPOSE_SENTINEL):
        count = text.count(sentinel)
        if count != 1:
            raise ArtifactParseError(
                f"response must contain the line {sentinel!r} exactly once "
                f"(found {count}); reply with both sections under "
                f"{DOCKERFILE_SENTINEL!r} and {COMPOSE_SENTINEL!r}"
            )
    head, _, rest = text.partition(DOCKERFILE_SENTINEL)
    if COMPOSE_SENTINEL in head:
        raise ArtifactParseError(
            f"{DOCKERFILE_SENTINEL!r} must come before {COMPOSE_SENTINEL!r}"
        )
    dockerfile, _, compose = rest.partition(COMPOSE_SENTINEL)
    if not dockerfile.strip() or not compose.strip():
        raise ArtifactParseError("both artifact sections must be non-empty")
    return dockerfile.strip(), compose.strip()


def render_artifact_response(dockerfile: str, compose: str | None) -> str:
    """Inverse of parse: the format fixture authors and prompts use."""
    if compose is None:
        return dockerfile
    return f"{DOCKERFILE_SENTINEL}\n{dockerfile}\n{COMPOSE_SENTINEL}\n{compose}"
