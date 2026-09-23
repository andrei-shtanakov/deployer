import json
from pathlib import Path

import pytest

from deployer import cli
from deployer.artifacts import render_artifact_response
from deployer.cli import main
from deployer.diagnose import FailureVerdict, Outcome, RunDiagnosis
from deployer.forge import (
    AdapterRefusal,
    Completeness,
    FailedJob,
    FailedRun,
    GhError,
    RunRef,
    StepRef,
    load_snapshot,
)
from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    VerificationReport,
)
from deployer.reproduce import ReproductionSection, TryDirError


@pytest.fixture(autouse=True)
def _no_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


RUN_URL = "https://github.com/o/r/actions/runs/1"


def _minimal_run(
    *,
    jobs: list[FailedJob] | None = None,
    completeness: Completeness | None = None,
) -> FailedRun:
    return FailedRun(
        repo="o/r",
        run_id=1,
        attempt=1,
        head_sha="deadbeef",
        url=RUN_URL,
        jobs=jobs if jobs is not None else [],
        completeness=(
            completeness
            if completeness is not None
            else Completeness(logs="present", annotations="present")
        ),
    )


def diagnosis(
    outcome: Outcome, *, failures: list[FailureVerdict] | None = None
) -> RunDiagnosis:
    return RunDiagnosis(
        run=_minimal_run(),
        failures=failures if failures is not None else [],
        outcome=outcome,
        causes=[],
        observations=[],
    )


@pytest.fixture(autouse=True)
def _stub_fetch_failed_run(monkeypatch) -> None:
    """No `diagnose` test may reach `gh`: a network-free stub by default."""
    monkeypatch.setattr(cli, "fetch_failed_run", lambda *a, **k: _minimal_run())


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
    from deployer.models import AuthoringRun

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


def test_bench_verify_declared_smoke_unsatisfied_is_fail(tmp_path, monkeypatch, capsys):
    """`report.passed` alone treats SKIPPED as success (correct for optional
    linters like hadolint), so a case that declares smoke must not print
    `ok` on a SKIPPED (or FAILED, or absent) `atp_smoke` just because no
    other check failed."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = VerificationReport(
        results=[CheckResult(check_id="atp_smoke", status=CheckStatus.SKIPPED)],
        smoke_declared=True,
    )
    monkeypatch.setattr(
        "deployer.cli.verify_corpus", lambda *a, **k: [("case-one", report)]
    )
    code = cli.main(["bench", "verify", "--corpus", str(corpus)])
    assert code == 1
    out = capsys.readouterr().out
    assert "[FAIL] case-one" in out
    assert "skipped" in out


def test_bench_verify_declared_smoke_satisfied_is_ok(tmp_path, monkeypatch, capsys):
    """The mirror case: `atp_smoke` PASSED, so the case prints `ok`."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = VerificationReport(
        results=[CheckResult(check_id="atp_smoke", status=CheckStatus.PASSED)],
        smoke_declared=True,
    )
    monkeypatch.setattr(
        "deployer.cli.verify_corpus", lambda *a, **k: [("case-one", report)]
    )
    code = cli.main(["bench", "verify", "--corpus", str(corpus)])
    assert code == 0
    assert "[  ok] case-one" in capsys.readouterr().out


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


def _fake_report(cases):
    from deployer.models import BenchCaseResult, BenchReport

    return BenchReport(
        label="t",
        author_backend="fixture",
        build_timeout_s=600,
        health_timeout_s=30,
        cases=[BenchCaseResult(**c) for c in cases],
    )


def test_require_atp_fails_when_atp_smoke_skipped(tmp_path, monkeypatch, capsys):
    """A case whose `atp_smoke` itself reported SKIPPED (e.g. version
    mismatch) trips the gate via the recorded `atp_smoke_status`, never by
    matching `skip_reason` prose — the scope and the verdict both come from
    `report.cases`, with no second `load_corpus` read."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "skipped",
                "skip_reason": "atp_smoke skipped: atp not installed",
                "smoke_declared": True,
                "atp_smoke_status": "skipped",
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


def test_require_atp_ignores_case_without_smoke_declared(tmp_path, monkeypatch, capsys):
    """A skipped case that never declared smoke must not trip the gate.

    A second, smoke-declaring case that matched cleanly keeps the scope
    non-empty, so this exercises the "not in scope" tolerance in isolation
    from the separate `--require-atp`-on-empty-scope guard.
    """
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "skipped",
                "skip_reason": "no fixture.Dockerfile for the offline fixture author",
                "smoke_declared": False,
            },
            {
                "case": "case-two",
                "outcome": "matched",
                "success": True,
                "smoke_declared": True,
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
    runtime resolved) must also trip the gate: `atp_smoke_status` stays
    unset on this path, and an absent check on a declared-smoke case is
    unsatisfied whatever skipped it."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "skipped",
                "skip_reason": "case requires L2 but no container runtime resolved",
                "smoke_declared": True,
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
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "matched",
                "success": False,
                "smoke_declared": True,
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
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "matched",
                "success": True,
                "smoke_declared": True,
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
    assert "no atp_smoke check recorded" in err


def test_require_atp_passes_when_atp_smoke_passed(tmp_path, monkeypatch, capsys):
    """The gate's success path: a matched smoke case with atp_smoke PASSED
    exits 0."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "matched",
                "success": True,
                "smoke_declared": True,
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
    exercises nothing, closing the gate even less than an unsatisfied
    smoke does."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    report = _fake_report(
        [
            {
                "case": "case-one",
                "outcome": "matched",
                "success": True,
                "smoke_declared": False,
            }
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 1
    assert "no case in scope declares a smoke intent" in capsys.readouterr().err


def test_require_atp_scope_is_report_cases_not_corpus(tmp_path, monkeypatch, capsys):
    """The gate's scope must come from `report.cases`, never a second
    `load_corpus` read: an external target declaring smoke is invisible to
    `load_corpus` (it only walks `corpus/synthetic/`), so a gate that
    re-derived scope from the corpus could never see it. Patching
    `deployer.cli.load_corpus` here would raise (the name no longer exists
    on the module), which is itself evidence the re-read is gone."""
    corpus = _make_corpus(tmp_path)
    monkeypatch.setattr("deployer.cli.resolve_runtime", lambda *a, **k: None)
    assert not hasattr(cli, "load_corpus")
    report = _fake_report(
        [
            {
                "case": "external-one",
                "outcome": "matched",
                "success": True,
                "smoke_declared": True,
                "atp_smoke_status": "passed",
                "external_url": "https://example.invalid/repo.git",
                "external_commit": "deadbeef",
            }
        ]
    )
    monkeypatch.setattr(
        "deployer.cli.run_bench", lambda *a, **k: (report, tmp_path / "run")
    )
    code = cli.main(["bench", "run", "--corpus", str(corpus), "--require-atp"])
    assert code == 0


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

    target_dir = tmp_path / "case"
    target_dir.mkdir()
    (target_dir / "suite.yaml").write_text("test_suite: x\n")
    target = DeployTarget(run={}, smoke={"suite": "suite.yaml"})

    resolved = _resolve_smoke_suite(target, str(target_dir / "target.json"))

    assert resolved == target_dir / "suite.yaml"


def test_smoke_suite_is_none_without_a_smoke_intent() -> None:
    from deployer.cli import _resolve_smoke_suite

    assert _resolve_smoke_suite(DeployTarget(), "/anywhere/target.json") is None


def test_smoke_suite_without_a_target_file_is_an_error() -> None:
    """A smoke intent can only arrive through a --target file, so a missing
    path is a broken invocation, not a silent skip."""
    from deployer.cli import _resolve_smoke_suite

    with pytest.raises(ValueError, match="--target"):
        _resolve_smoke_suite(DeployTarget(run={}, smoke={"suite": "s.yaml"}), None)


# --- Task 11: `deployer diagnose` -----------------------------------------


# The reading layer asserts no cause and never produces `CLASSIFIED`, so exit
# 0 is not a row here: no path through `diagnose` reaches it.
@pytest.mark.parametrize(
    "outcome,code",
    [("UNCLASSIFIED", 3), ("EVIDENCE_UNAVAILABLE", 4)],
)
def test_exit_code_per_outcome(outcome, code, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis(outcome))
    out = tmp_path / "v.json"
    assert cli.main(["diagnose", RUN_URL, "--output-file", str(out)]) == code
    assert json.loads(out.read_text())["outcome"] == outcome


def test_adapter_refusal_has_its_own_code(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "fetch_failed_run",
        lambda *a, **k: AdapterRefusal("not_failed", "run succeeded"),
    )
    assert cli.main(["diagnose", RUN_URL]) == 5


def test_url_and_repo_run_id_are_mutually_exclusive() -> None:
    assert cli.main(["diagnose", RUN_URL, "--repo", "o/r", "--run-id", "1"]) == 2


def test_attempt_must_be_a_positive_int() -> None:
    assert cli.main(["diagnose", RUN_URL, "--attempt", "0"]) == 2


def test_verdict_document_carries_its_own_schema_version(tmp_path) -> None:
    out = tmp_path / "v.json"
    cli.main(["diagnose", RUN_URL, "--output-file", str(out)])
    assert json.loads(out.read_text())["verdict_schema_version"] == "1.1"


def _raise_gh_error(*args: object, **kwargs: object) -> FailedRun:
    raise GhError("gh api boom")


def test_gh_error_exits_2_with_message_not_a_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "fetch_failed_run", _raise_gh_error)
    assert cli.main(["diagnose", RUN_URL]) == 2
    assert "gh api boom" in capsys.readouterr().err


def test_url_attempt_is_forwarded_to_fetch(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def spy(ref, *, attempt, **kwargs):
        captured["ref"] = ref
        captured["attempt"] = attempt
        return _minimal_run()

    monkeypatch.setattr(cli, "fetch_failed_run", spy)
    url = "https://github.com/o/r/actions/runs/1/attempts/3"
    cli.main(["diagnose", url])
    assert captured == {"ref": RunRef("o/r", 1), "attempt": 3}


def test_url_attempt_matching_flag_attempt_is_fine(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def spy(ref, *, attempt, **kwargs):
        captured["attempt"] = attempt
        return _minimal_run()

    monkeypatch.setattr(cli, "fetch_failed_run", spy)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))
    url = "https://github.com/o/r/actions/runs/1/attempts/3"
    assert cli.main(["diagnose", url, "--attempt", "3"]) == 3
    assert captured["attempt"] == 3


def test_url_attempt_conflicts_with_flag_attempt() -> None:
    url = "https://github.com/o/r/actions/runs/1/attempts/3"
    assert cli.main(["diagnose", url, "--attempt", "2"]) == 2


def test_repo_without_run_id_is_invalid() -> None:
    assert cli.main(["diagnose", "--repo", "o/r"]) == 2


def test_run_id_without_repo_is_invalid() -> None:
    assert cli.main(["diagnose", "--run-id", "1"]) == 2


def test_stdout_and_stderr_are_split(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))
    cli.main(["diagnose", RUN_URL])
    captured = capsys.readouterr()
    assert "outcome: UNCLASSIFIED" in captured.out
    assert "causes: none asserted" in captured.out
    assert "completeness:" in captured.err


def test_summary_lists_job_level_and_step_level_where(monkeypatch, capsys) -> None:
    job_where = 42
    step_where = StepRef(job_id=42, number=2)
    verdicts = [
        FailureVerdict(
            where=job_where,
            outcome="UNCLASSIFIED",
            evidence=[],
            observations=["disk full: write /var/lib/docker/tmp/x: no space left"],
        ),
        FailureVerdict(
            where=step_where,
            outcome="UNCLASSIFIED",
            evidence=[],
            observations=["assertion error: AssertionError: boom"],
        ),
    ]
    monkeypatch.setattr(
        cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED", failures=verdicts)
    )
    cli.main(["diagnose", RUN_URL])
    out = capsys.readouterr().out
    assert "[job 42] UNCLASSIFIED: disk full: " in out
    assert "[job 42 step 2] UNCLASSIFIED: assertion error: " in out


def test_every_observation_of_a_verdict_is_printed_not_only_the_first(
    monkeypatch, capsys
) -> None:
    """diagnose.py appends a caveat as a second observation (e.g. `cited
    evidence is job-level (no step binding)`); it must reach a terminal-only
    operator, not only the structured --output-file document."""
    verdicts = [
        FailureVerdict(
            where=42,
            outcome="UNCLASSIFIED",
            evidence=[],
            observations=[
                "disk full: write /var/lib/docker/tmp/x: no space left",
                "cited evidence is job-level (no step binding)",
            ],
        ),
    ]
    monkeypatch.setattr(
        cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED", failures=verdicts)
    )
    cli.main(["diagnose", RUN_URL])
    out = capsys.readouterr().out
    assert "[job 42] UNCLASSIFIED: disk full: " in out
    assert "    cited evidence is job-level (no step binding)" in out


def test_output_file_write_failure_exits_2_not_a_traceback(tmp_path, capsys) -> None:
    """--output-file is an operator argument: an unwritable path is a clean
    exit 2, not an uncaught OSError — the document IS the deliverable."""
    bad_path = tmp_path / "nonexistent-dir-xyz" / "v.json"
    assert cli.main(["diagnose", RUN_URL, "--output-file", str(bad_path)]) == 2
    assert "cannot write" in capsys.readouterr().err


# --- final review: slug/attempt validation, single print, offline fixture ---


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/../..%2Fx/actions/runs/1",
        "https://github.com/o/r%2F../actions/runs/1",
    ],
)
def test_a_url_whose_slug_is_not_a_repo_name_is_rejected(url, capsys) -> None:
    """The slug is interpolated straight into `repos/{repo}/...`, so a run URL
    pasted from an issue could steer `gh api` at a path other than the one it
    appears to name."""
    assert cli.main(["diagnose", url]) == 2
    assert "owner/name" in capsys.readouterr().err


def test_a_repo_flag_that_is_not_a_repo_name_is_rejected(capsys) -> None:
    assert cli.main(["diagnose", "--repo", "a b", "--run-id", "1"]) == 2
    assert "owner/name" in capsys.readouterr().err


def test_a_url_whose_slug_segments_are_all_dots_is_rejected(capsys) -> None:
    """`../..` matches the old `[A-Za-z0-9._-]+` alternation character for
    character; rejecting an all-dots segment costs one alternation."""
    url = "https://github.com/../../actions/runs/1"
    assert cli.main(["diagnose", url]) == 2
    assert "owner/name" in capsys.readouterr().err


def test_a_repo_flag_that_is_all_dots_is_rejected(capsys) -> None:
    assert cli.main(["diagnose", "--repo", "./.", "--run-id", "1"]) == 2
    assert "owner/name" in capsys.readouterr().err


def test_an_ordinary_slug_still_passes_both_branches(monkeypatch) -> None:
    """The twin: dots, dashes and underscores are legal in a repo name."""
    seen: list[RunRef] = []

    def spy(ref, *, attempt, **kwargs):
        seen.append(ref)
        return _minimal_run()

    monkeypatch.setattr(cli, "fetch_failed_run", spy)
    cli.main(["diagnose", "https://github.com/a-b/c.d_e/actions/runs/7"])
    cli.main(["diagnose", "--repo", "a-b/c.d_e", "--run-id", "7"])
    assert seen == [RunRef("a-b/c.d_e", 7), RunRef("a-b/c.d_e", 7)]


def test_a_url_attempt_of_zero_is_rejected_like_the_flag(capsys) -> None:
    """`--attempt 0` already got a clean exit 2; the URL's attempt reached
    `gh` and 404'd. Same check, same message, both doors."""
    url = "https://github.com/o/r/actions/runs/1/attempts/0"
    assert cli.main(["diagnose", url]) == 2
    assert "positive integer" in capsys.readouterr().err


def test_a_url_run_id_of_zero_is_rejected_like_the_flag(capsys) -> None:
    """`--run-id 0` already got a clean exit 2; the URL's run id was taken
    with a bare `int()` and reached `gh`. Same check, same message, both
    doors — the twin of the attempt test above."""
    url = "https://github.com/o/r/actions/runs/0"
    assert cli.main(["diagnose", url]) == 2
    assert "positive integer" in capsys.readouterr().err


def test_an_ordinary_url_run_id_still_passes(monkeypatch) -> None:
    """The positive twin: a real run id is unaffected by the range check."""
    seen: list[RunRef] = []

    def spy(ref, *, attempt, **kwargs):
        seen.append(ref)
        return _minimal_run()

    monkeypatch.setattr(cli, "fetch_failed_run", spy)
    cli.main(["diagnose", "https://github.com/o/r/actions/runs/17"])
    assert seen == [RunRef("o/r", 17)]


def test_run_level_observations_are_printed_once(monkeypatch, capsys) -> None:
    """stdout carries the human summary, stderr the diagnostics (spec §7).
    The EVIDENCE_UNAVAILABLE branch used to repeat the observations on both."""
    monkeypatch.setattr(
        cli,
        "diagnose_run",
        lambda s: RunDiagnosis(
            run=_minimal_run(),
            failures=[],
            outcome="EVIDENCE_UNAVAILABLE",
            causes=[],
            observations=["logs fetch error"],
        ),
    )
    assert cli.main(["diagnose", RUN_URL]) == 4
    captured = capsys.readouterr()
    assert captured.out.count("logs fetch error") == 1
    assert "logs fetch error" not in captured.err
    assert "completeness:" in captured.err


def test_run_id_and_attempt_document_themselves_in_help(capsys) -> None:
    """Every other diagnose argument already does."""
    with pytest.raises(SystemExit):
        cli.main(["diagnose", "--help"])
    help_text = capsys.readouterr().out
    assert "the run's numeric id (with --repo)" in help_text
    assert "re-run attempt number" in help_text


def test_the_real_project_fixture_diagnoses_through_the_cli(
    monkeypatch, tmp_path
) -> None:
    """The seam the live runs proved, under regression: a real anonymised
    snapshot through the real `diagnose_run` and the real exit-code map.
    Every other CLI diagnose test stubs one of the two. A complete read
    exits 3 with its observations and asserts no cause."""
    fixture = Path(__file__).parent / "fixtures" / "runs" / "project.json"
    snapshot = load_snapshot(fixture.read_text())
    monkeypatch.setattr(cli, "fetch_failed_run", lambda *a, **k: snapshot)

    out = tmp_path / "v.json"
    assert cli.main(["diagnose", RUN_URL, "--output-file", str(out)]) == 3

    document = json.loads(out.read_text())
    assert document["verdict_schema_version"] == "1.1"
    assert document["outcome"] == "UNCLASSIFIED"
    assert document["causes"] == []
    (failure,) = document["failures"]
    assert failure["kind"] is None
    assert any(o.startswith("assertion error: ") for o in failure["observations"])


# --- Task 13: `deployer diagnose --reproduce` -----------------------------


def test_reproduce_with_container_host_is_exit_2(capsys) -> None:
    code = cli.main(
        ["diagnose", RUN_URL, "--reproduce", "--container-host", "ssh://u@h"]
    )
    assert code == 2
    assert "--reproduce" in capsys.readouterr().err


def test_reproduce_keeps_the_reading_exit_code(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))
    monkeypatch.setattr(cli, "resolve_runtime", lambda *a, **k: None)
    section = ReproductionSection(status="refused", refusal="event x not supported")
    monkeypatch.setattr(cli, "reproduce_run", lambda *a, **k: section)
    out = tmp_path / "v.json"
    assert (
        cli.main(["diagnose", RUN_URL, "--reproduce", "--output-file", str(out)]) == 3
    )
    document = json.loads(out.read_text())
    assert document["verdict_schema_version"] == "1.2"
    assert document["reproduction"]["status"] == "refused"


def test_try_dir_error_is_exit_2(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))
    monkeypatch.setattr(cli, "resolve_runtime", lambda *a, **k: None)

    def boom(*a, **k):
        raise TryDirError("source.json names another head_sha")

    monkeypatch.setattr(cli, "reproduce_run", boom)
    assert cli.main(["diagnose", RUN_URL, "--reproduce"]) == 2
    assert "another head_sha" in capsys.readouterr().err


def test_without_reproduce_nothing_is_called(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "diagnose_run", lambda s: diagnosis("UNCLASSIFIED"))

    def forbidden(*a, **k):
        raise AssertionError("reproduce_run must not run without --reproduce")

    monkeypatch.setattr(cli, "reproduce_run", forbidden)
    assert cli.main(["diagnose", RUN_URL]) == 3
    assert not (tmp_path / ".deployer-runs").exists()
