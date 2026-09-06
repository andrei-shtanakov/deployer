import json
from pathlib import Path

import pytest

from deployer import cli
from deployer.artifacts import render_artifact_response
from deployer.cli import main
from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    VerificationReport,
)


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
        compose=None,
        ci=None,
        smoke_suite=None,
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


def test_verify_resolves_smoke_suite_from_target_and_forwards_it(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    """The CLI must pass verify() the RESOLVED absolute suite path — not the
    relative string from the document, and not None — resolved against the
    target file's own directory, not the project directory.
    """
    from deployer.models import VerificationReport

    project = tmp_path / "proj"
    project.mkdir()
    for name in ("pyproject.toml", "main.py"):
        (project / name).write_text((hello_service / name).read_text())
    (project / "Dockerfile").write_text((hello_service / "Dockerfile.good").read_text())

    target_dir = tmp_path / "targetdir"
    target_dir.mkdir()
    (target_dir / "suite.yaml").write_text("test_suite: x\n")
    target_file = target_dir / "target.json"
    target_file.write_text('{"run": {}, "smoke": {"suite": "suite.yaml"}}')

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
        compose=None,
        ci=None,
        smoke_suite=None,
    ):
        captured["smoke_suite"] = smoke_suite
        return VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.cli.verify", spy_verify)
    exit_code = cli.main(["verify", str(project), "--target", str(target_file)])
    assert exit_code == 0
    assert captured["smoke_suite"] == (target_dir / "suite.yaml").resolve()


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
        smoke_suite=None,
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


def test_verify_passes_cli_runtime_flags_through(
    hello_service: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    seen: dict = {}

    def fake_resolve(tool_arg=None, host_arg=None, env=None):
        seen["args"] = (tool_arg, host_arg)
        return None  # static-only keeps the test docker-free

    monkeypatch.setattr("deployer.cli.resolve_runtime", fake_resolve)
    cli.main(
        [
            "verify",
            str(project),
            "--container-tool",
            "docker",
            "--container-host",
            "ssh://u@h",
        ]
    )
    assert seen["args"] == ("docker", "ssh://u@h")


def test_verify_invalid_runtime_config_exits_2(
    hello_service: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    from deployer.runtime import RuntimeConfigError

    def boom(tool_arg=None, host_arg=None, env=None):
        raise RuntimeConfigError("--container-host must be an ssh:// URL")

    monkeypatch.setattr("deployer.cli.resolve_runtime", boom)
    code = cli.main(["verify", str(project), "--container-host", "tcp://h:1"])
    assert code == 2
    assert "ssh://" in capsys.readouterr().err


def test_author_invalid_runtime_config_exits_2(
    hello_service: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )
    from deployer.runtime import RuntimeConfigError

    def boom(tool_arg=None, host_arg=None, env=None):
        raise RuntimeConfigError("--container-host must be an ssh:// URL")

    monkeypatch.setattr("deployer.cli.resolve_runtime", boom)
    code = cli.main(["author", str(project), "--container-host", "tcp://h:1"])
    assert code == 2
    assert "ssh://" in capsys.readouterr().err


def test_author_no_docker_skips_runtime_resolution(
    hello_service: Path, tmp_path: Path, monkeypatch
) -> None:
    project = _make_project(
        hello_service, tmp_path, (hello_service / "Dockerfile.good").read_text()
    )

    def fail_resolve(*args, **kwargs):
        pytest.fail("resolve_runtime must not be called")

    monkeypatch.setattr("deployer.cli.resolve_runtime", fail_resolve)

    good = (hello_service / "Dockerfile.good").read_text()

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    exit_code = cli.main(["author", str(project), "--no-docker"])
    assert exit_code == 0


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


def test_author_parse_failure_does_not_write_new_dockerfile(
    tmp_path: Path, monkeypatch
) -> None:
    """Sentinel-less response for a ci-target is a parse failure every

    iteration (no_progress) — the raw, sentinel-laden text must never be
    written over Dockerfile; the transactional-write guarantee holds.
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    target_file = tmp_path / "target.json"
    target_file.write_text('{"ci": {}}')

    class NeverParsesAuthor:
        def generate(self, facts, target):
            return "FROM python:3.12-slim\nCOPY main.py .\n"  # no ci sentinel

        def repair(self, facts, target, artifact_text, report):
            return "FROM python:3.12-slim\nCOPY main.py .\n"

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: NeverParsesAuthor())
    exit_code = cli.main(
        ["author", str(project), "--no-docker", "--target", str(target_file)]
    )
    assert exit_code == 1
    run_data = json.loads((project / ".deployer" / "authoring-run.json").read_text())
    assert run_data["stopped_reason"] == "no_progress"
    assert not (project / "Dockerfile").exists()


def test_author_parse_failure_leaves_existing_dockerfile_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    target_file = tmp_path / "target.json"
    target_file.write_text('{"ci": {}}')
    existing = 'FROM python:3.12-slim\nCOPY main.py .\nCMD ["python", "main.py"]\n'
    (project / "Dockerfile").write_text(existing)

    class NeverParsesAuthor:
        def generate(self, facts, target):
            return "FROM python:3.12-slim\nCOPY main.py .\n"  # no ci sentinel

        def repair(self, facts, target, artifact_text, report):
            return "FROM python:3.12-slim\nCOPY main.py .\n"

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: NeverParsesAuthor())
    exit_code = cli.main(
        ["author", str(project), "--no-docker", "--target", str(target_file)]
    )
    assert exit_code == 1
    assert (project / "Dockerfile").read_text() == existing


def _make_corpus(tmp_path, name="case-one", requires_l2=False):
    import json as _json

    case = tmp_path / "corpus" / "synthetic" / name
    (case / "project").mkdir(parents=True)
    (case / "project" / "main.py").write_text("print('hi')\n")
    (case / "expected.json").write_text(_json.dumps({"requires_l2": requires_l2}))
    (case / "fixture.Dockerfile").write_text("FROM python:3.12-slim\n")
    return tmp_path / "corpus"


def test_bench_run_offline_exits_0_on_match(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--label", "t"])
    assert code == 0
    out = capsys.readouterr().out
    assert "case-one" in out and "bench-report" in out


def test_bench_run_bad_label_exits_2(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--label", "a/b"])
    assert code == 2
    assert "label" in capsys.readouterr().err


def test_bench_run_missing_corpus_exits_2(tmp_path, capsys):
    code = cli.main(["bench", "run", "--corpus", str(tmp_path / "nope")])
    assert code == 2


def test_bench_run_anthropic_requires_explicit_flag(tmp_path, monkeypatch):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.AnthropicAuthor",
        lambda: pytest.fail("AnthropicAuthor must not be constructed by default"),
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0


def test_bench_verify_static_only_pass_exits_0(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    code = cli.main(["bench", "verify", "--corpus", str(corpus)])
    assert code == 0
    assert "case-one" in capsys.readouterr().out


def test_bench_verify_static_only_prints_note(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert main(["bench", "verify", "--corpus", str(corpus)]) == 0
    assert "static-only" in capsys.readouterr().out


def test_bench_verify_no_matching_cases_exits_2(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    code = cli.main(["bench", "verify", "--corpus", str(corpus), "--filter", "zzz"])
    assert code == 2
    assert "zzz" in capsys.readouterr().err


def test_bench_run_clone_failure_exits_2(tmp_path, monkeypatch, capsys):
    from deployer.bench import CloneError

    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)

    def boom(*args, **kwargs):
        raise CloneError("cloning external target demo failed")

    monkeypatch.setattr("deployer.cli.run_bench", boom)
    code = cli.main(
        [
            "bench",
            "run",
            "--corpus",
            str(corpus),
            "--label",
            "t",
            "--include-external",
        ]
    )
    assert code == 2
    assert "demo" in capsys.readouterr().err


class _FakeSmokeCase:
    """Duck-typed BenchCase stand-in: only `.name` and `.target.smoke` matter
    to the `--require-atp` gate."""

    def __init__(self, name: str, declares_smoke: bool) -> None:
        self.name = name
        self.target = (
            DeployTarget(run={}, smoke={"suite": "s.yaml"})
            if declares_smoke
            else DeployTarget()
        )


def _fake_report(cases):
    from deployer.models import BenchCaseResult, BenchReport

    return BenchReport(
        label="t",
        author_backend="fixture",
        build_timeout_s=600,
        health_timeout_s=30,
        cases=[BenchCaseResult(**c) for c in cases],
    )


def test_require_atp_fails_on_atp_smoke_skip_marker(tmp_path, monkeypatch, capsys):
    """A case skipped by `atp_smoke` itself (e.g. version mismatch) trips
    the gate via the existing marker check."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [_FakeSmokeCase("case-one", declares_smoke=True)],
    )
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "skipped",
                "skip_reason": "atp_smoke skipped: atp not installed",
            }
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 1
    err = capsys.readouterr().err
    assert "case-one" in err


def test_require_atp_ignores_unrelated_skip_reason(tmp_path, monkeypatch, capsys):
    """A skip with a reason unconnected to smoke must not trip the gate.

    A second, smoke-declaring case that matched cleanly keeps the corpus
    scope non-empty, so this exercises the "unrelated skip" tolerance in
    isolation from the separate `--require-atp`-on-empty-scope guard.
    """
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [
            _FakeSmokeCase("case-one", declares_smoke=False),
            _FakeSmokeCase("case-two", declares_smoke=True),
        ],
    )
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "skipped",
                "skip_reason": "no fixture.Dockerfile for the offline fixture author",
            },
            {
                "case": "case-two",
                "outcome": "matched",
                "success": True,
                "atp_smoke_status": "passed",
            },
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 0


def test_require_atp_fails_when_smoke_case_skipped_before_l2(
    tmp_path, monkeypatch, capsys
):
    """A smoke case skipped for an unrelated, EARLIER reason (no container
    runtime resolved) must also trip the gate: a SKIPPED smoke never closes
    the seam, whatever skipped it."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [_FakeSmokeCase("case-one", declares_smoke=True)],
    )
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "skipped",
                "skip_reason": "case requires L2 but no container runtime resolved",
            }
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 1
    err = capsys.readouterr().err
    assert "case-one" in err


def test_require_atp_fails_when_atp_smoke_failed(tmp_path, monkeypatch, capsys):
    """A smoke case can end `outcome="matched"` with a FAILED atp_smoke: a
    corpus case may legitimately declare `expected_success: false`, and a
    failing run then matches that expectation. The gate must still refuse,
    because the acceptance contract is `atp_smoke: PASSED` specifically."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [_FakeSmokeCase("case-one", declares_smoke=True)],
    )
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "matched",
                "success": False,
                "atp_smoke_status": "failed",
            }
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 1
    err = capsys.readouterr().err
    assert "case-one" in err
    assert "failed" in err


def test_require_atp_fails_when_atp_smoke_status_absent(tmp_path, monkeypatch, capsys):
    """A matched smoke case with no recorded `atp_smoke` check at all must
    not satisfy `--require-atp`: an absent check is not a PASSED one."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [_FakeSmokeCase("case-one", declares_smoke=True)],
    )
    report = _fake_report([{"case": "case-one", "outcome": "matched", "success": True}])
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 1
    err = capsys.readouterr().err
    assert "case-one" in err
    assert "no atp_smoke check recorded" in err


def test_require_atp_passes_when_atp_smoke_passed(tmp_path, monkeypatch, capsys):
    """The gate's success path: a matched smoke case with atp_smoke PASSED
    exits 0."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [_FakeSmokeCase("case-one", declares_smoke=True)],
    )
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "matched",
                "success": True,
                "atp_smoke_status": "passed",
            }
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 0


def test_require_atp_fails_when_no_smoke_case_in_scope(tmp_path, monkeypatch, capsys):
    """A filter that selects zero smoke-declaring cases must not let
    `report.all_matched` alone satisfy `--require-atp`: an empty scope
    exercises nothing, closing the gate even less than a SKIPPED smoke."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.setattr(
        "deployer.cli.load_corpus",
        lambda *a, **k: [_FakeSmokeCase("case-one", declares_smoke=False)],
    )
    report = _fake_report([{"case": "case-one", "outcome": "matched", "success": True}])
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 1
    assert "no case in scope declares a smoke intent" in capsys.readouterr().err


def test_bench_promote_cli(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    code = main(["bench", "promote", str(run_dir), "--corpus", str(corpus)])
    assert code == 0
    assert (corpus / "golden" / "golden.json").is_file()
    assert "golden" in capsys.readouterr().out


def test_bench_promote_missing_run_dir_exits_2(tmp_path, capsys):
    assert main(["bench", "promote", str(tmp_path / "nope")]) == 2


def test_bench_promote_force_overrides_mismatch(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    # Run bench to create a run
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    # Corrupt bench-report.json to mark one case as mismatched
    report_file = run_dir / "bench-report.json"
    report_data = json.loads(report_file.read_text())
    if report_data.get("cases"):
        report_data["cases"][0]["outcome"] = "mismatched"
        report_file.write_text(json.dumps(report_data, indent=2))
    # Promote without --force should fail with exit code 1
    code = main(["bench", "promote", str(run_dir), "--corpus", str(corpus)])
    assert code == 1
    assert "mismatched" in capsys.readouterr().err
    # Promote with --force should succeed
    code = main(["bench", "promote", str(run_dir), "--corpus", str(corpus), "--force"])
    assert code == 0
    assert (corpus / "golden" / "golden.json").is_file()
    assert "golden" in capsys.readouterr().out


def test_bench_compare_cli_regression_exits_1(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "base"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    assert main(["bench", "promote", str(run_dir), "--corpus", str(corpus)]) == 0

    import json as _json

    report_file = run_dir / "bench-report.json"
    data = _json.loads(report_file.read_text())
    data["cases"][0]["success"] = False
    data["cases"][0]["outcome"] = "mismatched"
    data["cases"][0]["stopped_reason"] = "no_progress"
    report_file.write_text(_json.dumps(data))

    code = main(["bench", "compare", str(run_dir), "golden", "--corpus", str(corpus)])
    assert code == 1
    out = capsys.readouterr().out
    assert "hard" in out and "case-one" in out


def test_bench_compare_clean_exits_0(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "base"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    assert main(["bench", "promote", str(run_dir), "--corpus", str(corpus)]) == 0
    code = main(["bench", "compare", str(run_dir), "golden", "--corpus", str(corpus)])
    assert code == 0
    assert "no regressions" in capsys.readouterr().out


def test_bench_compare_bad_baseline_exits_2(tmp_path, monkeypatch, capsys):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    code = main(
        [
            "bench",
            "compare",
            str(run_dir),
            str(tmp_path / "nope"),
            "--corpus",
            str(corpus),
        ]
    )
    assert code == 2
    assert "nope" in capsys.readouterr().err


def test_bench_compare_golden_as_candidate_exits_2_with_message(
    tmp_path, monkeypatch, capsys
):
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bench", "run", "--corpus", str(corpus), "--label", "t"]) == 0
    run_dir = next((tmp_path / ".deployer-runs").iterdir())
    code = main(["bench", "compare", "golden", str(run_dir), "--corpus", str(corpus)])
    assert code == 2
    assert (
        "candidate must be a raw run dir (the golden can only be a baseline)"
        in capsys.readouterr().err
    )


def test_verify_unknown_extra_exits_2(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    target = tmp_path / "target.json"
    target.write_text('{"extras": ["nope"]}')
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert cli.main(["verify", str(tmp_path), "--target", str(target)]) == 2


def test_author_unknown_extra_exits_2(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    target = tmp_path / "target.json"
    target.write_text('{"extras": ["nope"]}')

    class FakeAuthor:
        def generate(self, facts, target):
            raise AssertionError("generate must not be called")

        def repair(self, facts, target, dockerfile, report):
            raise AssertionError("repair must not be called")

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    assert (
        cli.main(["author", str(tmp_path), "--target", str(target), "--no-docker"]) == 2
    )


def test_verify_unknown_entrypoint_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "resolve_runtime", lambda *a, **k: None)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    target = tmp_path / "target.json"
    target.write_text('{"entrypoint": "app.py"}')
    assert cli.main(["verify", str(tmp_path), "--target", str(target)]) == 2


def test_author_unknown_entrypoint_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n'
    )
    target = tmp_path / "target.json"
    target.write_text('{"entrypoint": "app.py"}')

    class FakeAuthor:
        def generate(self, facts, target):
            raise AssertionError("generate must not be called")

        def repair(self, facts, target, dockerfile, report):
            raise AssertionError("repair must not be called")

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    assert (
        cli.main(["author", str(tmp_path), "--target", str(target), "--no-docker"]) == 2
    )


def test_load_dotenv_sets_missing_and_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_env: dict[str, str] = {"EXISTING": "env-value"}
    monkeypatch.setattr(cli.os, "environ", fake_env)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "ANTHROPIC_API_KEY=from-file\n"
        "QUOTED='q-value'\n"
        'DOUBLE="d-value"\n'
        "HALF='not-stripped\n"
        "BAD KEY=skipped\n"
        "export EXPORTED=skipped\n"
        "1BAD=skipped\n"
        "EXISTING=file-value\n"
        "NOEQUALS\n"
    )
    cli._load_dotenv(env_file)
    assert fake_env["ANTHROPIC_API_KEY"] == "from-file"
    assert fake_env["QUOTED"] == "q-value"
    assert fake_env["DOUBLE"] == "d-value"
    assert fake_env["HALF"] == "'not-stripped"
    assert fake_env["EXISTING"] == "env-value"
    assert "EXPORTED" not in fake_env
    assert "1BAD" not in fake_env
    assert "BAD KEY" not in fake_env


def test_load_dotenv_missing_file_is_noop(tmp_path: Path) -> None:
    cli._load_dotenv(tmp_path / "absent.env")


def test_load_dotenv_strips_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows-editor BOM must not silently disarm the first key."""
    fake_env: dict[str, str] = {}
    monkeypatch.setattr(cli.os, "environ", fake_env)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbfANTHROPIC_API_KEY=from-file\n")
    cli._load_dotenv(env_file)
    assert fake_env["ANTHROPIC_API_KEY"] == "from-file"


def test_cli_verify_reads_compose_for_deps_target(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY main.py .\n")
    target = {
        "service": {"port": 8000},
        "dependencies": [{"name": "cache", "image": "redis:7-alpine"}],
    }
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target))
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    # no compose.yaml -> compose_present FAILED -> exit 1
    code = main(["verify", str(tmp_path), "--target", str(target_path)])
    assert code == 1
    assert "compose_present" in capsys.readouterr().out


def test_load_dotenv_mismatched_quotes_not_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_env: dict[str, str] = {}
    monkeypatch.setattr(cli.os, "environ", fake_env)
    env_file = tmp_path / ".env"
    env_file.write_text("TRAIL=not-stripped'\nMIXED='a\"\n")
    cli._load_dotenv(env_file)
    assert fake_env["TRAIL"] == "not-stripped'"
    assert fake_env["MIXED"] == "'a\""


def test_cli_verify_reads_ci_workflow(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY main.py .\n")
    target_path = tmp_path / "target.json"
    target_path.write_text('{"ci": {}}')
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    # no .github/workflows/ci.yml -> ci_present FAILED -> exit 1
    code = main(["verify", str(tmp_path), "--target", str(target_path)])
    assert code == 1
    assert "ci_present" in capsys.readouterr().out


def test_cli_verify_ignores_artifacts_for_plain_target(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A plain target must not depend on unrequested artifact files.

    An unreadable (non-UTF-8) ci.yml or compose.yaml next to a plain
    target used to crash the read; now they are never consulted.
    """
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY main.py .\n")
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_bytes(b"\xff\xfe garbage")
    (tmp_path / "compose.yaml").write_bytes(b"\xff\xfe garbage")
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    code = main(["verify", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "ci_present" not in out and "compose_present" not in out


def test_cli_author_writes_ci_workflow(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    target_file = tmp_path / "target.json"
    target_file.write_text('{"ci": {}}')

    good = render_artifact_response(
        "FROM python:3.12-slim\nCOPY main.py .", ci="name: ci"
    )

    class FakeAuthor:
        def generate(self, facts, target):
            return good

        def repair(self, facts, target, dockerfile, report):
            return good

    monkeypatch.setattr("deployer.cli.AnthropicAuthor", lambda: FakeAuthor())
    main(["author", str(tmp_path), "--no-docker", "--target", str(target_file)])
    ci_path = tmp_path / ".github" / "workflows" / "ci.yml"
    assert ci_path.read_text() == "name: ci\n"


def test_smoke_suite_resolves_against_the_target_file(tmp_path: Path) -> None:
    """Not the cwd and not project/: a target document must stay portable."""
    from deployer.cli import _resolve_smoke_suite
    from deployer.models import DeployTarget

    target_dir = tmp_path / "case"
    target_dir.mkdir()
    (target_dir / "suite.yaml").write_text("test_suite: x\n")
    target = DeployTarget(run={}, smoke={"suite": "suite.yaml"})

    resolved = _resolve_smoke_suite(target, str(target_dir / "target.json"))

    assert resolved == target_dir / "suite.yaml"


def test_smoke_suite_is_none_without_a_smoke_intent() -> None:
    from deployer.cli import _resolve_smoke_suite
    from deployer.models import DeployTarget

    assert _resolve_smoke_suite(DeployTarget(), "/anywhere/target.json") is None


def test_smoke_suite_without_a_target_file_is_an_error() -> None:
    """A smoke intent can only arrive through a --target file, so a missing
    path is a broken invocation, not a silent skip."""
    from deployer.cli import _resolve_smoke_suite
    from deployer.models import DeployTarget

    with pytest.raises(ValueError, match="--target"):
        _resolve_smoke_suite(DeployTarget(run={}, smoke={"suite": "s.yaml"}), None)
