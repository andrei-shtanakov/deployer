import json
from pathlib import Path

import pytest

from deployer import cli
from deployer.models import CheckResult, CheckStatus, FailureKind, VerificationReport


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
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
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
        runtime,
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
        runtime,
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


def test_print_report_shows_full_failed_message_only(capsys) -> None:
    report = VerificationReport(
        results=[
            CheckResult(
                check_id="build",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message="compile failed\ngcc: fatal error: killed\nstopped",
            ),
            CheckResult(
                check_id="base_pinned",
                status=CheckStatus.WARNING,
                message="unpinned image\nwarning tail must stay hidden",
            ),
        ],
        docker_available=True,
    )
    cli._print_report(report)
    out = capsys.readouterr().out
    assert "[FAIL] build: compile failed" in out
    assert "\n       gcc: fatal error: killed\n" in out  # 7-space alignment
    assert "\n       stopped\n" in out
    assert "warning tail must stay hidden" not in out  # WARNING stays one line


def _make_project(hello_service: Path, tmp_path: Path, dockerfile: str) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text(dockerfile)
    return project


def test_verify_writes_report_json_on_pass(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert cli.main(["verify", str(project)]) == 0
    report_path = project / ".deployer" / "verify-report.json"
    report = VerificationReport.model_validate_json(report_path.read_text())
    assert report.results  # round-trips and is non-empty


def test_verify_writes_report_json_on_fail(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = _make_project(
        hello_service, tmp_path, "FROM python:3.12-slim\nCOPY nope.py .\n"
    )
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert cli.main(["verify", str(project)]) == 1
    report_path = project / ".deployer" / "verify-report.json"
    report = VerificationReport.model_validate_json(report_path.read_text())
    failed = [r for r in report.results if r.status is CheckStatus.FAILED]
    assert failed and "nope.py" in failed[0].message  # full detail persisted


def test_verify_rejects_nondir_project(tmp_path: Path, capsys) -> None:
    assert cli.main(["verify", str(tmp_path / "ghost")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_author_rejects_nondir_project(tmp_path: Path, capsys) -> None:
    assert cli.main(["author", str(tmp_path / "ghost")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_verify_rejects_missing_target_file(tmp_path: Path, capsys) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    code = cli.main(["verify", str(tmp_path), "--target", str(tmp_path / "nope.json")])
    assert code == 2
    assert capsys.readouterr().err.startswith("error:")


def test_author_rejects_missing_target_file(tmp_path: Path, capsys) -> None:
    code = cli.main(["author", str(tmp_path), "--target", str(tmp_path / "nope.json")])
    assert code == 2
    assert capsys.readouterr().err.startswith("error:")


def test_rejects_malformed_target_json(tmp_path: Path) -> None:
    bad = tmp_path / "target.json"
    bad.write_text("{not json")
    assert cli.main(["verify", str(tmp_path), "--target", str(bad)]) == 2
    assert cli.main(["author", str(tmp_path), "--target", str(bad)]) == 2


def test_rejects_target_failing_validation(tmp_path: Path) -> None:
    bad = tmp_path / "target.json"
    bad.write_text('{"service": {"port": "not-a-port"}}')
    assert cli.main(["verify", str(tmp_path), "--target", str(bad)]) == 2
    assert cli.main(["author", str(tmp_path), "--target", str(bad)]) == 2


def test_nondir_project_wins_over_bad_target(tmp_path: Path, capsys) -> None:
    """Pins the validation order Part 2 of the spec exists to fix."""
    code = cli.main(
        [
            "verify",
            str(tmp_path / "ghost"),
            "--target",
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err
    code = cli.main(
        [
            "author",
            str(tmp_path / "ghost"),
            "--target",
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err


def test_missing_dockerfile_still_exit_1(tmp_path: Path) -> None:
    assert cli.main(["verify", str(tmp_path)]) == 1


def test_rejects_non_utf8_target_file(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "target.json"
    bad.write_bytes(b"\xff\xfe{")
    assert cli.main(["verify", str(tmp_path), "--target", str(bad)]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_print_report_blank_tail_lines_have_no_trailing_spaces(capsys) -> None:
    report = VerificationReport(
        results=[
            CheckResult(
                check_id="build",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.AUTHORING,
                message="boom\n\ntail after blank",
            )
        ],
        docker_available=True,
    )
    cli._print_report(report)
    out = capsys.readouterr().out
    assert "tail after blank" in out
    assert all(not line.endswith(" ") for line in out.splitlines())


def test_verify_report_write_failure_warns_not_crashes(
    hello_service: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    (project / ".deployer").write_text("a file where a dir must go")
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert cli.main(["verify", str(project)]) == 0  # exit reflects checks
    captured = capsys.readouterr()
    assert "warning: could not write verify-report.json" in captured.err
    assert "report:" not in captured.out


def test_author_report_write_failure_warns_not_crashes(
    hello_service: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    (project / ".deployer").write_text("a file where a dir must go")
    good = (hello_service / "Dockerfile.good").read_text()

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    assert cli.main(["author", str(project), "--no-docker"]) == 0
    captured = capsys.readouterr()
    assert "warning: could not write authoring-run.json" in captured.err
    assert "stopped: static_only" in captured.out
