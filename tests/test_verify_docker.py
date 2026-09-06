from pathlib import Path

import pytest

from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    RunSpec,
    ServiceSpec,
)
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


def test_build_context_excludes_dotenv(
    hello_service: Path, runtime, tmp_path: Path
) -> None:
    import shutil as _shutil

    project = tmp_path / "proj"
    _shutil.copytree(hello_service, project)
    (project / ".env").write_text("SECRET=do-not-ship\n")
    base_from = (project / "Dockerfile.good").read_text().splitlines()[0]
    dockerfile = (
        f"{base_from}\nCOPY . /ctx\nRUN test ! -e /ctx/.env\nRUN test -e /ctx/main.py\n"
    )
    report = verify(dockerfile, project, DeployTarget(), runtime)
    assert _by_id(report, "build").status is CheckStatus.PASSED


CORPUS_JOB = Path(__file__).parent.parent / "corpus" / "synthetic" / "no-build-system"
JOB_TARGET = DeployTarget(run=RunSpec(expect_stdout="hello from no-build-system"))


def test_job_fixture_passes_run_completes(runtime: ContainerRuntime) -> None:
    dockerfile = (CORPUS_JOB / "fixture.Dockerfile").read_text()
    report = verify(dockerfile, CORPUS_JOB / "project", JOB_TARGET, runtime)
    assert _by_id(report, "build").status is CheckStatus.PASSED
    assert _by_id(report, "run_completes").status is CheckStatus.PASSED
    assert report.passed


def test_inert_cmd_fails_run_completes_without_leaking_oracle(
    runtime: ContainerRuntime,
) -> None:
    """The motivating blind spot: bare `python` exits 0 silently — only
    the hidden stdout oracle catches it, and the failure names the CMD
    but never the oracle."""
    dockerfile = (
        (CORPUS_JOB / "fixture.Dockerfile")
        .read_text()
        .replace(
            'CMD ["uv", "run", "--no-sync", "python", "main.py"]',
            'CMD ["python"]',
        )
    )
    report = verify(dockerfile, CORPUS_JOB / "project", JOB_TARGET, runtime)
    check = _by_id(report, "run_completes")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "container command" in check.message
    assert "hello from no-build-system" not in check.message


def test_smoke_target_does_not_run_the_job_itself(monkeypatch, tmp_path) -> None:
    """ATP is the runtime check for a smoke target.

    `_run_completes` starts the container with no stdin at all, and a correct
    ATP agent reading stdin would get EOF; running it first would fail a
    healthy image.
    """
    from deployer.models import DeployTarget
    from deployer.verify import verify_docker

    called: list[str] = []
    monkeypatch.setattr(
        "deployer.verify._run_completes",
        lambda *a, **k: (
            called.append("run")
            or CheckResult(check_id="run_completes", status=CheckStatus.PASSED)
        ),
    )
    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr("deployer.verify.container_run", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: (
            CheckResult(check_id="atp_smoke", status=CheckStatus.PASSED),
            True,
        ),
    )
    target = DeployTarget(run={}, smoke={"suite": "s.yaml"})

    results, _size, image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        target,
        ContainerRuntime(tool="docker"),
        smoke_suite=tmp_path / "s.yaml",
    )

    assert called == []  # the job check never ran
    assert [r.check_id for r in results] == ["build", "atp_smoke"]
    assert available is True
    assert image.tag.startswith("localhost/deployer-verify-")


def test_built_image_records_a_failed_cleanup(monkeypatch, tmp_path) -> None:
    """rmi is best-effort, so cleanup_status must report what happened."""
    from deployer.models import DeployTarget
    from deployer.verify import verify_docker

    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)

    def boom(*a, **k):
        raise OSError("no daemon")

    monkeypatch.setattr("deployer.verify.container_run", boom)

    _results, _size, image, _available = verify_docker(
        "FROM x:1\n", tmp_path, DeployTarget(), ContainerRuntime(tool="docker")
    )

    assert image.cleanup_status == "failed"


def test_smoke_on_a_remote_runtime_is_skipped(monkeypatch, tmp_path) -> None:
    """ATP's container adapter has no remote-host support, so it must not
    silently test whatever image happens to be local."""
    from deployer.models import DeployTarget
    from deployer.verify import verify_docker

    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr("deployer.verify.container_run", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: pytest.fail("ATP must not run against a remote build"),
    )
    remote = ContainerRuntime(tool="docker", host="ssh://box", host_source="cli")

    results, _size, _image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        DeployTarget(run={}, smoke={"suite": "s.yaml"}),
        remote,
        smoke_suite=tmp_path / "s.yaml",
    )

    smoke = [r for r in results if r.check_id == "atp_smoke"][0]
    assert smoke.status is CheckStatus.SKIPPED
    assert "remote" in smoke.message
    assert available is False
