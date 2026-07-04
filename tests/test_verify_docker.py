from pathlib import Path

import pytest

from deployer.models import CheckStatus, DeployTarget, ServiceSpec
from deployer.verify import detect_container_tool, verify

pytestmark = pytest.mark.docker

TARGET = DeployTarget(service=ServiceSpec(port=8000, healthcheck_path="/health"))


@pytest.fixture(scope="module")
def tool() -> str:
    found = detect_container_tool()
    if found is None:
        pytest.skip("no container runtime available")
    return found


def _by_id(report, check_id: str):
    return next(r for r in report.results if r.check_id == check_id)


def test_good_dockerfile_builds_runs_and_healthchecks(
    hello_service: Path, tool: str
) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, TARGET, tool)
    assert report.docker_available
    assert _by_id(report, "build").status is CheckStatus.PASSED
    assert _by_id(report, "run_healthcheck").status is CheckStatus.PASSED
    assert report.passed


def test_broken_run_instruction_fails_build_as_authoring(
    hello_service: Path, tool: str
) -> None:
    dockerfile = (
        (hello_service / "Dockerfile.good")
        .read_text()
        .replace("WORKDIR /app", "WORKDIR /app\nRUN definitely-not-a-command")
    )
    report = verify(dockerfile, hello_service, TARGET, tool)
    check = _by_id(report, "build")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_wrong_port_fails_healthcheck(hello_service: Path, tool: str) -> None:
    bad_target = DeployTarget(service=ServiceSpec(port=9999))
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, bad_target, tool)
    check = _by_id(report, "run_healthcheck")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_no_tool_degrades_to_static_only(hello_service: Path) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, TARGET, tool=None)
    assert report.docker_available is False
    assert all(r.check_id not in ("build", "run_healthcheck") for r in report.results)
