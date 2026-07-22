import pytest

from deployer.artifacts import (
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
    dockerfile, compose = parse_artifact_response(RESPONSE, expects_compose=True)
    assert dockerfile == "FROM python:3.12-slim"
    assert compose == "services:\n  app:\n    build: ."


def test_parse_no_deps_passthrough() -> None:
    dockerfile, compose = parse_artifact_response(
        "FROM python:3.12-slim\n", expects_compose=False
    )
    assert dockerfile == "FROM python:3.12-slim"
    assert compose is None


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


def test_render_round_trips() -> None:
    text = render_artifact_response("FROM x:1", "services: {}")
    assert parse_artifact_response(text, expects_compose=True) == (
        "FROM x:1",
        "services: {}",
    )
    assert render_artifact_response("FROM x:1", None) == "FROM x:1"
