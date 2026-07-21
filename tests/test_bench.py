"""Bench: models, offline fixture author, corpus loading, orchestration."""

import json
import subprocess
from pathlib import Path

import pytest

from deployer.bench import (
    FixtureAuthor,
    clone_external,
    load_corpus,
    load_external,
    render_markdown,
    run_bench,
    run_case,
)
from deployer.models import (
    AuthoringRun,
    BenchCaseResult,
    BenchReport,
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    ExpectedOutcome,
    ExternalTarget,
    IterationRecord,
    ProjectFacts,
    VerificationReport,
)


def test_expected_outcome_defaults() -> None:
    expected = ExpectedOutcome()
    assert expected.expected_success is True
    assert expected.max_iterations == 3
    assert expected.requires_l2 is True
    assert expected.expected_failure_kind is None
    assert expected.capabilities == []


def test_bench_report_success_rate_ignores_skipped() -> None:
    report = _report(
        BenchCaseResult(case="a", outcome="matched", success=True),
        BenchCaseResult(case="b", outcome="mismatched", success=False),
        BenchCaseResult(case="c", outcome="skipped", skip_reason="no runtime"),
    )
    assert report.success_rate == 0.5
    assert report.all_matched is False


def test_bench_report_all_skipped_has_no_rate() -> None:
    report = _report(BenchCaseResult(case="a", outcome="skipped"))
    assert report.success_rate is None
    assert report.all_matched is True


def test_bench_report_round_trips_json() -> None:
    report = _report(BenchCaseResult(case="a", outcome="matched", success=True))
    assert BenchReport.model_validate_json(report.model_dump_json()) == report


def _report(*cases: BenchCaseResult) -> BenchReport:
    return BenchReport(
        label="t",
        author_backend="fixture",
        build_timeout_s=600,
        health_timeout_s=30,
        cases=list(cases),
    )


def test_fixture_author_replays_dockerfile_verbatim() -> None:
    author = FixtureAuthor("FROM python:3.12-slim\n")
    facts = ProjectFacts()
    target = DeployTarget()
    generated = author.generate(facts, target)
    assert generated == "FROM python:3.12-slim\n"
    repaired = author.repair(facts, target, generated, _passing_report())
    assert repaired == generated


def test_fixture_author_info() -> None:
    info = FixtureAuthor("FROM x:1\n").info()
    assert info.backend == "fixture"
    assert info.model_id is None
    assert info.prompt_sha256 is not None and len(info.prompt_sha256) == 64


def _passing_report():
    from deployer.models import VerificationReport

    return VerificationReport()


def _make_case(
    root: Path,
    name: str,
    *,
    target: dict | None = None,
    expected: dict | None = None,
    fixture: str | None = "FROM python:3.12-slim\n",
) -> Path:
    case = root / "synthetic" / name
    (case / "project").mkdir(parents=True)
    (case / "project" / "main.py").write_text("print('hi')\n")
    if target is not None:
        (case / "target.json").write_text(json.dumps(target))
    if expected is not None:
        (case / "expected.json").write_text(json.dumps(expected))
    if fixture is not None:
        (case / "fixture.Dockerfile").write_text(fixture)
    return case


def test_load_corpus_reads_case_files(tmp_path: Path) -> None:
    _make_case(
        tmp_path,
        "svc",
        target={"service": {"port": 8000, "healthcheck_path": "/health"}},
        expected={"capabilities": ["service"], "max_iterations": 2},
    )
    cases = load_corpus(tmp_path)
    assert len(cases) == 1
    case = cases[0]
    assert case.name == "svc"
    assert case.target.service is not None and case.target.service.port == 8000
    assert case.expected.max_iterations == 2
    assert case.fixture_dockerfile is not None


def test_load_corpus_defaults_when_files_absent(tmp_path: Path) -> None:
    _make_case(tmp_path, "bare", fixture=None)
    case = load_corpus(tmp_path)[0]
    assert case.target == DeployTarget()
    assert case.expected == ExpectedOutcome()
    assert case.fixture_dockerfile is None


def test_load_corpus_sorted_and_filtered(tmp_path: Path) -> None:
    _make_case(tmp_path, "b-two")
    _make_case(tmp_path, "a-one")
    assert [c.name for c in load_corpus(tmp_path)] == ["a-one", "b-two"]
    assert [c.name for c in load_corpus(tmp_path, "a-*")] == ["a-one"]


def test_load_corpus_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path / "nope")


def test_load_corpus_case_without_project_raises(tmp_path: Path) -> None:
    (tmp_path / "synthetic" / "broken").mkdir(parents=True)
    with pytest.raises(ValueError, match="broken"):
        load_corpus(tmp_path)


def _fake_run(success: bool) -> AuthoringRun:
    report = VerificationReport(
        results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)],
        image_size_bytes=123_000_000 if success else None,
    )
    return AuthoringRun(
        project="x",
        target=DeployTarget(),
        iterations=[
            IterationRecord(
                index=0, dockerfile="FROM x:1\n", report=report, duration_s=0.1
            )
        ],
        stopped_reason="success" if success else "no_progress",
        success=success,
    )


def test_run_case_skips_l2_case_without_runtime(tmp_path: Path) -> None:
    _make_case(tmp_path, "svc")
    case = load_corpus(tmp_path)[0]
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "skipped"
    assert "runtime" in result.skip_reason


def test_run_case_skips_when_author_missing(tmp_path: Path) -> None:
    _make_case(tmp_path, "svc", fixture=None)
    case = load_corpus(tmp_path)[0]
    result = run_case(
        case,
        None,
        ContainerRuntime(tool="docker"),
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "skipped"
    assert "fixture" in result.skip_reason


def test_run_case_runs_in_scratch_and_writes_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    (tmp_path / "synthetic" / "svc" / "project" / ".env").write_text("S=1\n")
    case = load_corpus(tmp_path)[0]
    seen: dict = {}

    def fake_author_dockerfile(project_path, target, author, **kwargs):
        seen["project_path"] = Path(project_path)
        seen["kwargs"] = kwargs
        return _fake_run(True)

    monkeypatch.setattr("deployer.bench.author_dockerfile", fake_author_dockerfile)
    out = tmp_path / "out"
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        out,
        build_timeout=99,
        health_timeout=9,
    )
    assert seen["project_path"] != case.project_dir  # scratch copy, not corpus
    assert not (seen["project_path"] / ".env").exists()  # CONTEXT_IGNORE applied
    assert seen["kwargs"]["max_iterations"] == case.expected.max_iterations
    assert seen["kwargs"]["build_timeout"] == 99
    assert result.outcome == "matched" and result.success
    assert result.iterations == 1
    assert result.image_size_bytes == 123_000_000
    assert (out / "authoring-run.json").is_file()
    assert (out / "Dockerfile").read_text() == "FROM x:1\n\n"
    assert not (case.project_dir / ".deployer").exists()  # corpus untouched


def test_run_case_mismatch_when_expectation_violated(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    case = load_corpus(tmp_path)[0]
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile",
        lambda *a, **k: _fake_run(False),
    )
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "mismatched"
    assert result.stopped_reason == "no_progress"


def test_run_bench_aggregates_and_writes_reports(tmp_path: Path, monkeypatch) -> None:
    _make_case(tmp_path, "a-ok", expected={"requires_l2": False})
    _make_case(tmp_path, "b-l2")  # requires_l2 default True -> skipped (no runtime)
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    report, run_dir = run_bench(
        tmp_path,
        lambda case: FixtureAuthor("FROM x:1\n"),
        None,
        label="unit",
        author_backend="fixture",
        runs_root=tmp_path / "runs",
    )
    assert [c.outcome for c in report.cases] == ["matched", "skipped"]
    assert report.label == "unit"
    assert run_dir.name.endswith("-unit")
    assert (run_dir / "bench-report.json").is_file()
    md = (run_dir / "bench-report.md").read_text()
    assert "a-ok" in md and "skipped" in md


def test_run_bench_no_matching_cases_raises(tmp_path: Path) -> None:
    _make_case(tmp_path, "only")
    with pytest.raises(ValueError, match="no corpus cases"):
        run_bench(
            tmp_path,
            lambda c: None,
            None,
            label="x",
            author_backend="fixture",
            pattern="zzz*",
            runs_root=tmp_path / "runs",
        )


def test_render_markdown_has_table_and_metadata() -> None:
    report = _report(
        BenchCaseResult(
            case="a",
            outcome="matched",
            success=True,
            stopped_reason="success",
            iterations=2,
            image_size_bytes=45_600_000,
            wall_time_s=12.5,
        )
    )
    md = render_markdown(report)
    assert "| a | matched | success | 2 | 45.6 | 12.5 |" in md
    assert "author: fixture" in md


def _make_local_git_repo(root: Path) -> tuple[str, str]:
    repo = root / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "uploadpack.allowAnySHA1InWant", "true"],
        cwd=repo,
        check=True,
    )
    (repo / "main.py").write_text("print('v1')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    env_commit = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", *env_commit, "commit", "-qm", "v1"], cwd=repo, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "main.py").write_text("print('v2')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", *env_commit, "commit", "-qm", "v2"], cwd=repo, check=True)
    return str(repo), pinned


def test_load_external_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_external(tmp_path) == []


def test_load_external_parses_entries(tmp_path: Path) -> None:
    (tmp_path / "external.toml").write_text(
        "[[targets]]\n"
        'name = "demo"\n'
        'url = "https://example.invalid/demo.git"\n'
        'commit = "abc123"\n'
        "[targets.expected]\n"
        "expected_success = false\n"
    )
    targets = load_external(tmp_path)
    assert len(targets) == 1
    assert targets[0].name == "demo"
    assert targets[0].expected.expected_success is False


def test_clone_external_checks_out_pinned_commit(tmp_path: Path) -> None:
    url, pinned = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit=pinned)
    case = clone_external(ext, tmp_path / "scratch")
    assert case.name == "demo"
    assert (case.project_dir / "main.py").read_text() == "print('v1')\n"
    assert case.fixture_dockerfile is None


def test_clone_external_bad_commit_raises(tmp_path: Path) -> None:
    url, _ = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit="0" * 40)
    with pytest.raises(RuntimeError, match="demo"):
        clone_external(ext, tmp_path / "scratch")
