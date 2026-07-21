from pathlib import Path

import pytest

from deployer.models import CheckStatus, ContainerRuntime, DeployTarget, ServiceSpec
from deployer.runtime import resolve_runtime
from deployer.verify import verify

pytestmark = pytest.mark.docker

TARGET = DeployTarget(service=ServiceSpec(port=8000, healthcheck_path="/health"))


@pytest.fixture(scope="module")
def runtime() -> ContainerRuntime:
    found = resolve_runtime()
    if found is None:
        pytest.skip("no container runtime available")
    return found


def _by_id(report, check_id: str):
    return next(r for r in report.results if r.check_id == check_id)


def test_good_dockerfile_builds_runs_and_healthchecks(
    hello_service: Path, runtime: ContainerRuntime
) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, TARGET, runtime)
    assert report.docker_available
    assert _by_id(report, "build").status is CheckStatus.PASSED
    assert _by_id(report, "run_healthcheck").status is CheckStatus.PASSED
    assert report.passed
    assert report.image_size_bytes is not None and report.image_size_bytes > 0


def test_broken_run_instruction_fails_build_as_authoring(
    hello_service: Path, runtime: ContainerRuntime
) -> None:
    dockerfile = (
        (hello_service / "Dockerfile.good")
        .read_text()
        .replace("WORKDIR /app", "WORKDIR /app\nRUN definitely-not-a-command")
    )
    report = verify(dockerfile, hello_service, TARGET, runtime)
    check = _by_id(report, "build")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_wrong_port_fails_healthcheck(
    hello_service: Path, runtime: ContainerRuntime
) -> None:
    bad_target = DeployTarget(service=ServiceSpec(port=9999))
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, bad_target, runtime)
    check = _by_id(report, "run_healthcheck")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_no_tool_degrades_to_static_only(hello_service: Path) -> None:
    dockerfile = (hello_service / "Dockerfile.good").read_text()
    report = verify(dockerfile, hello_service, TARGET, runtime=None)
    assert report.docker_available is False
    assert all(r.check_id not in ("build", "run_healthcheck") for r in report.results)


def test_e2e_author_loop_with_real_docker(
    hello_service: Path, runtime: ContainerRuntime
) -> None:
    from deployer.author import author_dockerfile

    good = (hello_service / "Dockerfile.good").read_text()

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    run = author_dockerfile(hello_service, TARGET, FakeAuthor(), runtime=runtime)
    assert run.success is True
    assert run.stopped_reason == "success"


def test_cli_author_with_real_docker_exits_zero(
    hello_service: Path, runtime: ContainerRuntime, tmp_path: Path, monkeypatch
) -> None:
    import json

    from deployer import cli

    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    good = (hello_service / "Dockerfile.good").read_text()

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    target_file = tmp_path / "target.json"
    target_file.write_text('{"service": {"port": 8000}}')
    exit_code = cli.main(["author", str(project), "--target", str(target_file)])
    assert exit_code == 0
    run_data = json.loads((project / ".deployer" / "authoring-run.json").read_text())
    assert run_data["stopped_reason"] == "success"
    assert run_data["success"] is True


def test_pip_service_e2e(pip_service: Path, runtime: ContainerRuntime) -> None:
    from deployer.facts import analyze_project

    dockerfile = (pip_service / "Dockerfile.good").read_text()
    report = verify(
        dockerfile, pip_service, TARGET, runtime, analyze_project(pip_service)
    )
    assert report.passed
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


def test_sysdep_service_apt_layers_build_and_healthcheck(
    sysdep_service: Path, runtime: ContainerRuntime
) -> None:
    from deployer.facts import analyze_project

    dockerfile = (sysdep_service / "Dockerfile.good").read_text()
    report = verify(
        dockerfile, sysdep_service, TARGET, runtime, analyze_project(sysdep_service)
    )
    assert report.passed, report.error_signature()
    assert _by_id(report, "run_healthcheck").status is CheckStatus.PASSED
    assert report.image_size_bytes is not None and report.image_size_bytes > 0
