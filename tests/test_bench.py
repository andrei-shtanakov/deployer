"""Bench: models, offline fixture author, corpus loading, orchestration."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from deployer.bench import (
    FixtureAuthor,
    _create_run_dir,
    clone_external,
    compare_runs,
    load_baseline,
    load_corpus,
    load_external,
    normalize_run,
    promote_run,
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
    CompareFinding,
    ContainerRuntime,
    DeployTarget,
    ExpectedOutcome,
    ExternalTarget,
    FailureKind,
    GoldenCase,
    GoldenReport,
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


def test_run_case_aggregates_sorted_deduped_failure_kinds(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(
        tmp_path,
        "svc",
        expected={"requires_l2": False, "expected_success": False},
    )
    case = load_corpus(tmp_path)[0]
    iteration_one = IterationRecord(
        index=0,
        dockerfile="FROM x:1\n",
        report=VerificationReport(
            results=[
                CheckResult(
                    check_id="build",
                    status=CheckStatus.FAILED,
                    failure_kind="authoring",
                ),
                CheckResult(
                    check_id="runtime_check",
                    status=CheckStatus.FAILED,
                    failure_kind="environment",
                ),
            ]
        ),
        duration_s=0.1,
    )
    iteration_two = IterationRecord(
        index=1,
        dockerfile="FROM x:1\n",
        report=VerificationReport(
            results=[
                CheckResult(
                    check_id="build",
                    status=CheckStatus.FAILED,
                    failure_kind="authoring",
                ),
            ]
        ),
        duration_s=0.1,
    )
    fake_run = AuthoringRun(
        project="x",
        target=DeployTarget(),
        iterations=[iteration_one, iteration_two],
        stopped_reason="no_progress",
        success=False,
    )
    monkeypatch.setattr("deployer.bench.author_dockerfile", lambda *a, **k: fake_run)
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "matched"
    assert result.failure_kinds == [FailureKind.AUTHORING, FailureKind.ENVIRONMENT]


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


def test_run_bench_include_external_appends_and_skips_without_fixture(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "a-ok", expected={"requires_l2": False})
    url, pinned = _make_local_git_repo(tmp_path)
    (tmp_path / "external.toml").write_text(
        "[[targets]]\n"
        'name = "ext-demo"\n'
        f'url = "{url}"\n'
        f'commit = "{pinned}"\n'
        "[targets.expected]\n"
        "requires_l2 = false\n"
    )
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )

    def make_author(case):
        if case.fixture_dockerfile is None:
            return None
        return FixtureAuthor(case.fixture_dockerfile.read_text())

    report, run_dir = run_bench(
        tmp_path,
        make_author,
        None,
        label="ext",
        author_backend="fixture",
        runs_root=tmp_path / "runs",
        include_external=True,
    )
    assert [c.case for c in report.cases] == ["a-ok", "ext-demo"]
    synthetic, external = report.cases
    assert synthetic.outcome == "matched"
    assert external.outcome == "skipped"
    assert "fixture" in external.skip_reason


def test_external_target_rejects_path_traversal_name() -> None:
    with pytest.raises(ValidationError):
        ExternalTarget(name="../x", url="https://example.invalid/x.git", commit="a")


def test_external_target_rejects_bare_dotdot_name() -> None:
    with pytest.raises(ValidationError):
        ExternalTarget(name="..", url="https://example.invalid/x.git", commit="a")


def test_external_target_accepts_normal_name() -> None:
    ext = ExternalTarget(
        name="demo-1.2", url="https://example.invalid/x.git", commit="a"
    )
    assert ext.name == "demo-1.2"


def test_load_external_rejects_traversal_name(tmp_path: Path) -> None:
    (tmp_path / "external.toml").write_text(
        "[[targets]]\n"
        'name = "../escape"\n'
        'url = "https://example.invalid/demo.git"\n'
        'commit = "abc123"\n'
    )
    with pytest.raises(ValueError):
        load_external(tmp_path)


def test_create_run_dir_retries_past_same_second_collision(tmp_path: Path) -> None:
    stamp = "20260721-120000"
    label = "unit"
    (tmp_path / f"{stamp}-{label}").mkdir(parents=True)
    (tmp_path / f"{stamp}-{label}-2").mkdir(parents=True)
    run_dir = _create_run_dir(tmp_path, stamp, label)
    assert run_dir == tmp_path / f"{stamp}-{label}-3"
    assert run_dir.is_dir()


def _failed_run(kinds: list[FailureKind]) -> AuthoringRun:
    results = [
        CheckResult(check_id=f"c{i}", status=CheckStatus.FAILED, failure_kind=kind)
        for i, kind in enumerate(kinds)
    ]
    report = VerificationReport(results=results)
    return AuthoringRun(
        project="x",
        target=DeployTarget(),
        iterations=[
            IterationRecord(
                index=0, dockerfile="FROM x:1\n", report=report, duration_s=0.1
            )
        ],
        stopped_reason="no_progress",
        success=False,
    )


def test_expected_failure_kind_must_match(tmp_path: Path, monkeypatch) -> None:
    _make_case(
        tmp_path,
        "svc",
        expected={
            "requires_l2": False,
            "expected_success": False,
            "expected_failure_kind": "authoring",
        },
    )
    case = load_corpus(tmp_path)[0]
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile",
        lambda *a, **k: _failed_run([FailureKind.ENVIRONMENT]),
    )
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "mismatched"  # failed, but with the wrong kind

    monkeypatch.setattr(
        "deployer.bench.author_dockerfile",
        lambda *a, **k: _failed_run([FailureKind.AUTHORING]),
    )
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out2",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "matched"


def test_static_only_counts_as_success_when_l2_not_required(
    tmp_path: Path, monkeypatch
) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    case = load_corpus(tmp_path)[0]

    def static_only_run(*a, **k) -> AuthoringRun:
        report = VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )
        return AuthoringRun(
            project="x",
            target=DeployTarget(),
            iterations=[
                IterationRecord(
                    index=0, dockerfile="FROM x:1\n", report=report, duration_s=0.1
                )
            ],
            stopped_reason="static_only",
            success=False,
        )

    monkeypatch.setattr("deployer.bench.author_dockerfile", static_only_run)
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.success is True  # achieved its expected verification level
    assert result.outcome == "matched"  # expected_success default True now holds


def test_clone_external_carries_url_and_commit(tmp_path: Path) -> None:
    url, pinned = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit=pinned)
    case = clone_external(ext, tmp_path / "scratch")
    assert case.external_url == url
    assert case.external_commit == pinned


def test_run_case_records_external_identity(tmp_path: Path, monkeypatch) -> None:
    _make_case(tmp_path, "svc", expected={"requires_l2": False})
    case = load_corpus(tmp_path)[0].model_copy(
        update={"external_url": "https://x/y.git", "external_commit": "a" * 40}
    )
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    result = run_case(
        case,
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.external_url == "https://x/y.git"
    assert result.external_commit == "a" * 40
    assert result.expected == case.expected


def test_corpus_commit_dirty_suffix(monkeypatch) -> None:
    from deployer.bench import _corpus_commit

    monkeypatch.setattr("deployer.bench._deployer_git_sha", lambda: "abc123")

    def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
        class P:
            returncode: int
            stdout: str
            stderr: str

        p = P()
        p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
        return p

    def fake_run_clean(cmd, **kwargs):
        return _fake_proc(0, stdout="")

    def fake_run_dirty(cmd, **kwargs):
        return _fake_proc(0, stdout=" M src/deployer/bench.py\n")

    monkeypatch.setattr("deployer.bench.subprocess.run", fake_run_clean)
    assert _corpus_commit() == "abc123"
    monkeypatch.setattr("deployer.bench.subprocess.run", fake_run_dirty)
    assert _corpus_commit() == "abc123-dirty"


def test_markdown_includes_failure_kinds_column() -> None:
    report = _report(
        BenchCaseResult(
            case="a",
            outcome="mismatched",
            success=False,
            stopped_reason="no_progress",
            iterations=3,
            failure_kinds=[FailureKind.AUTHORING],
            wall_time_s=1.0,
        )
    )
    md = render_markdown(report)
    assert "| failure kinds |" in md.splitlines()[-3] or "failure kinds" in md
    assert "authoring" in md


def _bench_run_on_disk(tmp_path: Path, monkeypatch) -> Path:
    """Produce a real raw run dir via run_bench with a mocked author loop."""
    _make_case(tmp_path, "a-ok", expected={"requires_l2": False})
    _make_case(tmp_path, "b-skip")  # requires_l2 True -> skipped offline
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    _, run_dir = run_bench(
        tmp_path,
        lambda case: FixtureAuthor("FROM x:1\n"),
        None,
        label="norm",
        author_backend="fixture",
        runs_root=tmp_path / "runs",
    )
    return run_dir


def test_normalize_run_strips_noise(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    report = BenchReport.model_validate_json(
        (run_dir / "bench-report.json").read_text()
    )
    golden = normalize_run(run_dir)
    assert golden.promoted_from_label == "norm"
    assert [c.case for c in golden.cases] == ["a-ok"]  # skipped excluded
    case = golden.cases[0]
    assert case.success is True
    assert case.checks and case.checks[0].check_id == "parses"
    payload = golden.model_dump_json()
    assert "wall_time_s" not in payload
    assert str(tmp_path) not in payload  # no absolute paths anywhere
    assert "message" not in payload  # check messages stripped
    raw_case = next(c for c in report.cases if c.case == "a-ok")
    assert case.hadolint_status == raw_case.hadolint_status  # both sources agree


def test_normalize_run_requires_report(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="bench-report.json"):
        normalize_run(tmp_path / "empty")


def test_normalize_run_records_runtime_facts_without_host(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    report_file = run_dir / "bench-report.json"
    report = BenchReport.model_validate_json(report_file.read_text())
    report.runtime = ContainerRuntime(
        tool="docker", host="ssh://secret-host", host_source="cli"
    )
    report_file.write_text(report.model_dump_json(indent=2))
    golden = normalize_run(run_dir)
    assert golden.runtime_tool == "docker"
    assert golden.runtime_remote is True
    assert "secret-host" not in golden.model_dump_json()


def test_promote_writes_golden_tree(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    golden_dir = promote_run(run_dir, tmp_path)
    assert golden_dir == tmp_path / "golden"
    golden = GoldenReport.model_validate_json((golden_dir / "golden.json").read_text())
    assert golden.promoted_from_label == "norm"
    assert (
        (golden_dir / "cases" / "a-ok" / "Dockerfile")
        .read_text()
        .startswith("FROM x:1")
    )


def test_promote_refuses_mismatch_without_force(tmp_path: Path, monkeypatch) -> None:
    _make_case(
        tmp_path, "bad", expected={"requires_l2": False, "expected_success": False}
    )
    monkeypatch.setattr(
        "deployer.bench.author_dockerfile", lambda *a, **k: _fake_run(True)
    )
    _, run_dir = run_bench(
        tmp_path,
        lambda c: FixtureAuthor("FROM x:1\n"),
        None,
        label="bad",
        author_backend="fixture",
        runs_root=tmp_path / "runs",
    )
    with pytest.raises(ValueError, match="mismatch"):
        promote_run(run_dir, tmp_path)
    promote_run(run_dir, tmp_path, force=True)  # force overrides
    assert (tmp_path / "golden" / "golden.json").is_file()


def test_promote_replaces_previous_golden(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    stale = tmp_path / "golden" / "cases" / "ghost" / "Dockerfile"
    stale.parent.mkdir(parents=True)
    stale.write_text("FROM ghost:1\n")
    promote_run(run_dir, tmp_path)
    assert not stale.exists()


def _golden(*cases: GoldenCase) -> GoldenReport:
    return GoldenReport(
        promoted_from_label="base",
        author_backend="fixture",
        build_timeout_s=600,
        health_timeout_s=30,
        cases=list(cases),
    )


def _gcase(name: str, **overrides: Any) -> GoldenCase:
    defaults: dict[str, Any] = dict(
        case=name,
        success=True,
        stopped_reason="success",
        iterations=1,
        image_size_bytes=100_000_000,
    )
    defaults.update(overrides)
    return GoldenCase(**defaults)


def _rcase(name: str, **overrides: Any) -> BenchCaseResult:
    defaults: dict[str, Any] = dict(
        case=name,
        outcome="matched",
        success=True,
        stopped_reason="success",
        iterations=1,
        image_size_bytes=100_000_000,
        wall_time_s=10.0,
    )
    defaults.update(overrides)
    return BenchCaseResult(**defaults)


def test_compare_green_to_red_is_hard() -> None:
    findings = compare_runs(
        _report(
            _rcase(
                "a", outcome="mismatched", success=False, stopped_reason="no_progress"
            )
        ),
        _golden(_gcase("a")),
    )
    assert [f.level for f in findings][0] == "hard"
    assert findings[0].case == "a"
    assert isinstance(findings[0], CompareFinding)


def test_compare_iteration_growth_is_important() -> None:
    findings = compare_runs(_report(_rcase("a", iterations=3)), _golden(_gcase("a")))
    assert any(f.level == "important" and f.metric == "iterations" for f in findings)


def test_compare_missing_case_is_important_new_case_advisory() -> None:
    findings = compare_runs(_report(_rcase("b")), _golden(_gcase("a")))
    levels = {(f.level, f.metric, f.case) for f in findings}
    assert ("important", "missing_case", "a") in levels
    assert ("advisory", "new_case", "b") in levels


def test_compare_image_growth_threshold() -> None:
    grown = _report(_rcase("a", image_size_bytes=115_000_000))
    assert any(
        f.level == "advisory" and f.metric == "image_size"
        for f in compare_runs(grown, _golden(_gcase("a")))
    )
    small = _report(_rcase("a", image_size_bytes=105_000_000))
    assert not any(
        f.metric == "image_size" for f in compare_runs(small, _golden(_gcase("a")))
    )


def test_compare_wall_time_raw_vs_raw_only() -> None:
    slow = _report(_rcase("a", wall_time_s=20.0))
    raw_baseline = _report(_rcase("a", wall_time_s=10.0))
    assert any(f.metric == "wall_time" for f in compare_runs(slow, raw_baseline))
    assert not any(
        f.metric == "wall_time" for f in compare_runs(slow, _golden(_gcase("a")))
    )


def test_compare_clean_run_has_no_findings() -> None:
    assert compare_runs(_report(_rcase("a")), _golden(_gcase("a"))) == []


def test_load_baseline_golden_and_raw(tmp_path: Path, monkeypatch) -> None:
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    golden = load_baseline("golden", tmp_path)
    assert isinstance(golden, GoldenReport)
    raw = load_baseline(run_dir, tmp_path)
    assert isinstance(raw, BenchReport)
    with pytest.raises(ValueError):
        load_baseline("golden", tmp_path / "nowhere")
