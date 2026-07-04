from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
