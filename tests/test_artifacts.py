import pytest

from deployer.artifacts import (
    CI_SENTINEL,
    COMPOSE_SENTINEL,
    DOCKERFILE_SENTINEL,
    ArtifactParseError,
    parse_artifact_response,
    render_artifact_response,
)

RESPONSE = (
    f"{DOCKERFILE_SENTINEL}\nFROM python:3.12-slim\n"
    f"{COMPOSE_SENTINEL}\nservices:\n  app:\n    build: .\n"
)


def test_parse_both_sections() -> None:
    dockerfile, compose, ci = parse_artifact_response(RESPONSE, expects_compose=True)
    assert dockerfile == "FROM python:3.12-slim"
    assert compose == "services:\n  app:\n    build: ."
    assert ci is None


def test_parse_no_deps_passthrough() -> None:
    dockerfile, compose, ci = parse_artifact_response(
        "FROM python:3.12-slim\n", expects_compose=False
    )
    assert dockerfile == "FROM python:3.12-slim"
    assert compose is None
    assert ci is None


def test_parse_missing_compose_section_raises() -> None:
    with pytest.raises(ArtifactParseError):
        parse_artifact_response("FROM python:3.12-slim\n", expects_compose=True)


def test_parse_missing_dockerfile_section_raises() -> None:
    text = f"{COMPOSE_SENTINEL}\nservices: {{}}\n"
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(text, expects_compose=True)


def test_parse_duplicated_sentinel_raises() -> None:
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(RESPONSE + RESPONSE, expects_compose=True)


def test_parse_incidental_sentinel_text_inside_section() -> None:
    """A sentinel string embedded in a longer line is not a sentinel line."""
    text = (
        f"{DOCKERFILE_SENTINEL}\n"
        "FROM python:3.12-slim\n"
        'RUN echo "=== compose.yaml ==="\n'
        f"{COMPOSE_SENTINEL}\n"
        "services:\n  app:\n    build: .\n"
    )
    dockerfile, compose, ci = parse_artifact_response(text, expects_compose=True)
    assert dockerfile == ('FROM python:3.12-slim\nRUN echo "=== compose.yaml ==="')
    assert compose == "services:\n  app:\n    build: ."
    assert ci is None


def test_parse_duplicated_true_sentinel_line_raises() -> None:
    text = (
        f"{DOCKERFILE_SENTINEL}\n"
        "FROM python:3.12-slim\n"
        f"{DOCKERFILE_SENTINEL}\n"
        f"{COMPOSE_SENTINEL}\n"
        "services: {}\n"
    )
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(text, expects_compose=True)


def test_parse_present_but_empty_section_raises() -> None:
    text = f"{DOCKERFILE_SENTINEL}\n{COMPOSE_SENTINEL}\nservices: {{}}\n"
    with pytest.raises(ArtifactParseError, match="non-empty"):
        parse_artifact_response(text, expects_compose=True)


def test_parse_prose_preamble_before_dockerfile_sentinel_dropped() -> None:
    text = (
        "Sure, here are the artifacts you requested:\n\n"
        f"{DOCKERFILE_SENTINEL}\n"
        "FROM python:3.12-slim\n"
        f"{COMPOSE_SENTINEL}\n"
        "services:\n  app:\n    build: .\n"
    )
    dockerfile, compose, ci = parse_artifact_response(text, expects_compose=True)
    assert dockerfile == "FROM python:3.12-slim"
    assert compose == "services:\n  app:\n    build: ."
    assert ci is None


def test_render_round_trips() -> None:
    text = render_artifact_response("FROM x:1", "services: {}")
    assert parse_artifact_response(text, expects_compose=True) == (
        "FROM x:1",
        "services: {}",
        None,
    )
    assert render_artifact_response("FROM x:1", None) == "FROM x:1"


CI_RESPONSE = f"{DOCKERFILE_SENTINEL}\nFROM python:3.12-slim\n{CI_SENTINEL}\nname: ci\n"


def test_parse_ci_only_section() -> None:
    dockerfile, compose, ci = parse_artifact_response(
        CI_RESPONSE, expects_compose=False, expects_ci=True
    )
    assert dockerfile == "FROM python:3.12-slim"
    assert compose is None
    assert ci == "name: ci"


def test_parse_all_three_sections() -> None:
    text = (
        f"{DOCKERFILE_SENTINEL}\nFROM x:1\n"
        f"{COMPOSE_SENTINEL}\nservices: {{}}\n"
        f"{CI_SENTINEL}\nname: ci\n"
    )
    assert parse_artifact_response(text, True, True) == (
        "FROM x:1",
        "services: {}",
        "name: ci",
    )


def test_parse_ci_out_of_order_raises() -> None:
    text = f"{CI_SENTINEL}\nname: ci\n{DOCKERFILE_SENTINEL}\nFROM x:1\n"
    with pytest.raises(ArtifactParseError):
        parse_artifact_response(text, False, True)


def test_parse_missing_ci_section_raises() -> None:
    with pytest.raises(ArtifactParseError):
        parse_artifact_response("FROM x:1", False, True)


def test_render_three_sections_round_trips() -> None:
    text = render_artifact_response("FROM x:1", "services: {}", "name: ci")
    assert parse_artifact_response(text, True, True) == (
        "FROM x:1",
        "services: {}",
        "name: ci",
    )
    ci_only = render_artifact_response("FROM x:1", ci="name: ci")
    assert parse_artifact_response(ci_only, False, True) == (
        "FROM x:1",
        None,
        "name: ci",
    )
    assert render_artifact_response("FROM x:1") == "FROM x:1"


def test_unrequested_known_sentinel_rejected() -> None:
    # ci-only expectation, model also emits a compose section
    text = (
        f"{DOCKERFILE_SENTINEL}\nFROM x:1\n"
        f"{COMPOSE_SENTINEL}\nservices: {{}}\n"
        f"{CI_SENTINEL}\nname: ci\n"
    )
    with pytest.raises(ArtifactParseError, match="unrequested"):
        parse_artifact_response(text, expects_compose=False, expects_ci=True)


def test_unrequested_sentinel_rejected_in_plain_mode() -> None:
    text = f"FROM x:1\n{COMPOSE_SENTINEL}\nservices: {{}}\n"
    with pytest.raises(ArtifactParseError, match="unrequested"):
        parse_artifact_response(text, expects_compose=False)
