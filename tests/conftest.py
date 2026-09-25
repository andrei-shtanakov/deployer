from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Reproduction and admission bundles vendor a project's tree, its own tests
# included; those are data for the replay and integrity tests, never collected.
collect_ignore_glob = [
    "fixtures/reproduction/*/tree",
    "fixtures/admission/*/tree",
]


@pytest.fixture()
def hello_service() -> Path:
    """Path to the tiny stdlib HTTP service fixture project."""
    return FIXTURES / "hello_service"


@pytest.fixture()
def pip_service() -> Path:
    """Path to the requirements.txt-only (no pyproject) service fixture."""
    return FIXTURES / "pip_service"


@pytest.fixture()
def sysdep_service() -> Path:
    """Path to the fixture whose dependency needs real apt packages."""
    return FIXTURES / "sysdep_service"
