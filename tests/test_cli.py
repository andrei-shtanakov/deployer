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
