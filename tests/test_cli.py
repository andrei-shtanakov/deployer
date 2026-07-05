import json
from pathlib import Path

import pytest

from deployer import cli
from deployer.models import CheckResult, CheckStatus


@pytest.fixture(autouse=True)
def _no_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


def test_verify_command_passes_on_good_dockerfile(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text((hello_service / "Dockerfile.good").read_text())
    monkeypatch.setattr("deployer.cli.detect_container_tool", lambda: None)
    assert cli.main(["verify", str(project)]) == 0


def test_verify_command_fails_without_dockerfile(tmp_path: Path) -> None:
    assert cli.main(["verify", str(tmp_path)]) == 1


def test_author_command_writes_dockerfile_and_report(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
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
    exit_code = cli.main(["author", str(project), "--no-docker"])
    assert exit_code == 0
    assert (project / "Dockerfile").read_text().rstrip() == good.rstrip()
    run_data = json.loads((project / ".deployer" / "authoring-run.json").read_text())
    assert run_data["stopped_reason"] == "static_only"


def test_author_reads_target_json(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    target_file = tmp_path / "target.json"
    target_file.write_text('{"service": {"port": 8000}}')

    captured = {}

    class FakeAuthor:
        def generate(self, facts, target):
            captured["target"] = target
            return (hello_service / "Dockerfile.good").read_text()

        def repair(self, facts, target, dockerfile, report):
            return dockerfile

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    cli.main(["author", str(project), "--no-docker", "--target", str(target_file)])
    assert captured["target"].service.port == 8000


def test_author_rejects_nonpositive_max_iterations(tmp_path: Path) -> None:
    assert cli.main(["author", str(tmp_path), "--max-iterations", "0"]) == 2


def test_verify_rejects_nonpositive_timeouts(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    assert cli.main(["verify", str(tmp_path), "--build-timeout", "0"]) == 2
    assert cli.main(["verify", str(tmp_path), "--health-timeout", "0"]) == 2


def test_author_rejects_nonpositive_timeouts(tmp_path: Path) -> None:
    assert cli.main(["author", str(tmp_path), "--build-timeout", "0"]) == 2
    assert cli.main(["author", str(tmp_path), "--health-timeout", "-5"]) == 2


def test_verify_flags_reach_library(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    from deployer.models import VerificationReport

    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text((hello_service / "Dockerfile.good").read_text())
    captured = {}

    def spy_verify(
        dockerfile,
        project_path,
        target,
        tool,
        facts=None,
        *,
        build_timeout,
        health_timeout,
    ):
        captured["timeouts"] = (build_timeout, health_timeout)
        return VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.cli.verify", spy_verify)
    exit_code = cli.main(
        [
            "verify",
            str(project),
            "--build-timeout",
            "1200",
            "--health-timeout",
            "45",
        ]
    )
    assert exit_code == 0
    assert captured["timeouts"] == (1200, 45)


def test_author_flags_reach_library(tmp_path: Path, monkeypatch) -> None:
    from deployer.models import AuthoringRun, DeployTarget

    captured = {}

    def spy_author(
        project_path,
        target,
        author,
        *,
        max_iterations,
        run_docker,
        build_timeout,
        health_timeout,
    ):
        captured["timeouts"] = (build_timeout, health_timeout)
        return AuthoringRun(
            project="p",
            target=DeployTarget(),
            stopped_reason="static_only",
            success=False,
        )

    monkeypatch.setattr("deployer.cli.author_dockerfile", spy_author)
    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: object())
    exit_code = cli.main(
        [
            "author",
            str(tmp_path),
            "--no-docker",
            "--build-timeout",
            "1200",
            "--health-timeout",
            "45",
        ]
    )
    assert exit_code == 0
    assert captured["timeouts"] == (1200, 45)
