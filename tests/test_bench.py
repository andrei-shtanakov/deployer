"""Bench: models, offline fixture author, corpus loading, orchestration."""

import itertools
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import deployer.bench as bench
from deployer.bench import (
    BenchCase,
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
    verify_corpus,
)
from deployer.models import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
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


def test_fixture_author_renders_sentinels_when_compose_present() -> None:
    from deployer.artifacts import COMPOSE_SENTINEL
    from deployer.bench import FixtureAuthor

    author = FixtureAuthor("FROM x:1", compose="services: {}")
    out = author.generate(ProjectFacts(), DeployTarget())
    assert COMPOSE_SENTINEL in out
    plain = FixtureAuthor("FROM x:1")
    assert COMPOSE_SENTINEL not in plain.generate(ProjectFacts(), DeployTarget())


def test_fixture_author_renders_ci_section() -> None:
    from deployer.artifacts import CI_SENTINEL
    from deployer.bench import FixtureAuthor

    author = FixtureAuthor("FROM x:1", ci="name: ci")
    assert CI_SENTINEL in author.generate(ProjectFacts(), DeployTarget())


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


def test_verify_corpus_forwards_smoke_suite(tmp_path: Path, monkeypatch) -> None:
    """Regression: `verify_corpus` must thread `smoke_suite` through to
    `verify()`, exactly like it already does for `compose` and `ci`.

    Without it, a `smoke` case reaching a real runtime silently falls
    through `verify_docker`'s `elif target.run is not None` branch and runs
    `_run_completes` instead of the ATP check — a false failure on a
    healthy case, not a skip.
    """
    case_dir = _make_case(
        tmp_path,
        "smoke-case",
        target={"run": {}, "smoke": {"suite": "suite.yaml"}},
    )
    (case_dir / "suite.yaml").write_text("test_suite: x\n")

    captured: list[Path | None] = []

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
        captured.append(smoke_suite)
        return VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.bench.verify", spy_verify)
    results = verify_corpus(tmp_path, ContainerRuntime(tool="podman"))

    assert captured == [case_dir / "suite.yaml"]
    assert results[0][1].passed


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


def test_run_case_skips_deps_case_without_fixture_compose(tmp_path: Path) -> None:
    _make_case(
        tmp_path,
        "deps",
        target={
            "service": {"port": 8000, "healthcheck_path": "/health"},
            "dependencies": [{"name": "cache", "image": "redis:7-alpine"}],
        },
        expected={"requires_l2": False},
    )
    case = load_corpus(tmp_path)[0]
    assert case.fixture_dockerfile is not None
    assert case.fixture_compose is None
    result = run_case(
        case,
        FixtureAuthor(case.fixture_dockerfile.read_text()),
        None,
        tmp_path / "out",
        build_timeout=60,
        health_timeout=5,
    )
    assert result.outcome == "skipped"
    assert "fixture.compose.yaml" in result.skip_reason


def test_run_case_skips_ci_case_without_fixture_ci(tmp_path: Path) -> None:
    _make_case(
        tmp_path,
        "ci-target",
        target={"ci": {}},
        expected={"requires_l2": False},
    )
    case = load_corpus(tmp_path)[0]
    assert case.fixture_dockerfile is not None
    assert case.fixture_ci is None
    result = run_case(
        case,
        FixtureAuthor(case.fixture_dockerfile.read_text()),
        None,
        tmp_path / "out",
        build_timeout=60,
        health_timeout=5,
    )
    assert result.outcome == "skipped"
    assert "fixture.ci.yml" in result.skip_reason


def test_run_case_records_smoke_declared_on_l2_skip_without_runtime(
    tmp_path: Path,
) -> None:
    """A case that declares smoke but is skipped before authoring ever
    starts (no runtime resolved) must still record `smoke_declared=True`:
    without this, "declared but never ran" is indistinguishable from
    "never declared" once the case is reduced to a skip."""
    _make_case(tmp_path, "agent", target={"run": {}, "smoke": {"suite": "suite.yaml"}})
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
    assert result.smoke_declared is True
    assert result.atp_smoke_status is None


def test_run_case_records_smoke_not_declared_on_l2_skip_without_runtime(
    tmp_path: Path,
) -> None:
    """The mirror case: a case that never declared smoke records
    `smoke_declared=False` on the same skip path."""
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
    assert result.smoke_declared is False


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


def test_run_case_final_return_records_declared_smoke_passed(
    tmp_path: Path, monkeypatch
) -> None:
    """The ordinary success path: a case that declares smoke and whose
    `atp_smoke` PASSED must reach the final (non-skip) return with both
    facts recorded, not just the pre-existing `atp_smoke_status`."""
    case = _make_case(
        tmp_path,
        "agent",
        target={"run": {}, "smoke": {"suite": "suite.yaml"}},
        expected={"requires_l2": False},
    )
    (case / "suite.yaml").write_text("test_suite: x\n")
    run = _fake_run(True)
    run.iterations[-1].report.results.append(
        CheckResult(check_id="atp_smoke", status=CheckStatus.PASSED)
    )
    monkeypatch.setattr("deployer.bench.author_dockerfile", lambda *a, **k: run)

    result = run_case(
        load_corpus(tmp_path)[0],
        FixtureAuthor("FROM x:1\n"),
        None,
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )
    assert result.outcome == "matched"
    assert result.smoke_declared is True
    assert result.atp_smoke_status is CheckStatus.PASSED


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


def test_clone_external_raises_clone_error(tmp_path: Path) -> None:
    from deployer.bench import CloneError

    url, _ = _make_local_git_repo(tmp_path)
    ext = ExternalTarget(name="demo", url=url, commit="0" * 40)
    with pytest.raises(CloneError):
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
    header_row = next(line for line in md.splitlines() if line.startswith("| case "))
    assert "| failure kinds |" in header_row
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


def test_committed_golden_atp_agent_records_smoke_fields() -> None:
    """The committed golden baseline predates `smoke_declared`/
    `atp_smoke_status`; both must be backfilled onto its `atp-agent` entry
    so the smoke invariant in `compare_runs` actually fires against the
    real baseline instead of silently no-op'ing on a case that declared
    smoke. The two fields must also agree with the `atp_smoke` entry the
    case already recorded in `checks`, so they can't drift apart again."""
    golden_path = Path(__file__).parent.parent / "corpus" / "golden" / "golden.json"
    golden = GoldenReport.model_validate_json(golden_path.read_text())
    case = next(c for c in golden.cases if c.case == "atp-agent")
    assert case.smoke_declared is True
    assert case.atp_smoke_status is CheckStatus.PASSED
    atp_smoke_check = next(c for c in case.checks if c.check_id == "atp_smoke")
    assert atp_smoke_check.status is case.atp_smoke_status


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


def test_compare_skipped_candidate_case_is_missing() -> None:
    """A case present in the candidate but skipped there is as good as absent."""
    findings = compare_runs(
        _report(_rcase("a", outcome="skipped", success=False)), _golden(_gcase("a"))
    )
    assert ("important", "missing_case", "a") in {
        (f.level, f.metric, f.case) for f in findings
    }


def test_compare_atp_skipped_missing_case_is_important() -> None:
    """A candidate case dropped because `atp_smoke` itself reported SKIPPED
    (no `atp` on PATH) is an unknown result, not a pass: the seam was never
    checked on this machine, so it must stay `important` and `bench compare`
    (exit 1 on any important/hard finding, see `_cmd_bench_compare`) must not
    read this as green."""
    findings = compare_runs(
        _report(
            _rcase(
                "a",
                outcome="skipped",
                success=False,
                skip_reason=f"{bench.ATP_SKIPPED_PREFIX} atp not installed",
            )
        ),
        _golden(_gcase("a")),
    )
    assert ("important", "missing_case", "a") in {
        (f.level, f.metric, f.case) for f in findings
    }
    assert not any(
        f.level == "advisory" and f.metric == "missing_case" for f in findings
    )


def test_compare_non_atp_skip_reason_keeps_missing_case_important() -> None:
    """Only the atp_smoke-SKIPPED marker is exempt; any other skip reason
    (or the case never running at all) must still be `important`."""
    findings = compare_runs(
        _report(
            _rcase(
                "a",
                outcome="skipped",
                success=False,
                skip_reason="case requires L2 but no container runtime resolved",
            )
        ),
        _golden(_gcase("a")),
    )
    assert ("important", "missing_case", "a") in {
        (f.level, f.metric, f.case) for f in findings
    }


def test_compare_present_candidate_smoke_unsatisfied_is_important() -> None:
    """A case present in both baseline and candidate is not automatically
    comparable on smoke: a baseline that declared smoke can end up
    unsatisfied in the candidate (SKIPPED here) without the case count
    differing at all, so `missing_case` alone cannot catch this — it never
    fires when the case is present in both."""
    findings = compare_runs(
        _report(_rcase("a", smoke_declared=True, atp_smoke_status="skipped")),
        _golden(_gcase("a", smoke_declared=True, atp_smoke_status="passed")),
    )
    assert ("important", "atp_smoke", "a") in {
        (f.level, f.metric, f.case) for f in findings
    }


def test_compare_present_candidate_smoke_satisfied_has_no_atp_finding() -> None:
    """The mirror case: candidate's `atp_smoke` PASSED too, so there is
    nothing to flag."""
    findings = compare_runs(
        _report(_rcase("a", smoke_declared=True, atp_smoke_status="passed")),
        _golden(_gcase("a", smoke_declared=True, atp_smoke_status="passed")),
    )
    assert not any(f.metric == "atp_smoke" for f in findings)


def test_compare_backend_mismatch_is_comparability_advisory() -> None:
    candidate = _report(_rcase("a"))
    candidate.author_backend = "anthropic"
    findings = compare_runs(candidate, _golden(_gcase("a")))
    comparability = [f for f in findings if f.metric == "comparability"]
    assert len(comparability) == 1
    assert comparability[0].level == "advisory"
    assert comparability[0].case == "-"
    assert "author_backend" in comparability[0].detail
    assert (
        "fixture" in comparability[0].detail and "anthropic" in comparability[0].detail
    )


def test_compare_identical_metadata_has_no_comparability_findings() -> None:
    findings = compare_runs(_report(_rcase("a")), _golden(_gcase("a")))
    assert not any(f.metric == "comparability" for f in findings)


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


def test_compare_hadolint_skipped_candidate_has_no_finding() -> None:
    """Skipped/absent hadolint on the candidate is not a worsening."""
    findings = compare_runs(
        _report(_rcase("a", hadolint_status=CheckStatus.SKIPPED)),
        _golden(_gcase("a", hadolint_status=CheckStatus.PASSED)),
    )
    assert not any(f.metric == "hadolint" for f in findings)


def test_compare_hadolint_warning_candidate_has_finding() -> None:
    findings = compare_runs(
        _report(_rcase("a", hadolint_status=CheckStatus.WARNING)),
        _golden(_gcase("a", hadolint_status=CheckStatus.PASSED)),
    )
    assert any(f.level == "advisory" and f.metric == "hadolint" for f in findings)


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


def test_compare_failure_kind_appearing_from_empty_baseline() -> None:
    """Candidate failure kinds appearing from empty baseline is a finding."""
    findings = compare_runs(
        _report(_rcase("a", failure_kinds=[FailureKind.AUTHORING])),
        _golden(_gcase("a", failure_kinds=[])),
    )
    failure_kind_findings = [f for f in findings if f.metric == "failure_kind"]
    assert len(failure_kind_findings) == 1
    assert failure_kind_findings[0].level == "important"
    assert "[] -> " in failure_kind_findings[0].detail


def test_compare_no_failure_kind_finding_when_baseline_improves() -> None:
    """Candidate with no failure kinds is not a regression, no finding."""
    findings = compare_runs(
        _report(_rcase("a", failure_kinds=[])),
        _golden(_gcase("a", failure_kinds=[FailureKind.AUTHORING])),
    )
    assert not any(f.metric == "failure_kind" for f in findings)


def test_promote_golden_path_is_file_raises_value_error(
    tmp_path: Path, monkeypatch
) -> None:
    """promote_run raises ValueError if golden path exists as a file."""
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    golden_file = tmp_path / "golden"
    golden_file.write_text("not a directory\n")
    with pytest.raises(ValueError, match="golden path .* exists and is not"):
        promote_run(run_dir, tmp_path)


def test_no_build_system_is_a_job_case() -> None:
    corpus = Path(__file__).parent.parent / "corpus"
    case = next(c for c in load_corpus(corpus) if c.name == "no-build-system")
    assert case.target.service is None
    assert case.target.run is not None
    assert case.target.run.expect_stdout == "hello from no-build-system"


def test_no_build_system_main_has_guard() -> None:
    from deployer.facts import analyze_project

    project = (
        Path(__file__).parent.parent
        / "corpus"
        / "synthetic"
        / "no-build-system"
        / "project"
    )
    assert analyze_project(project).script_entrypoint == "main.py"


def test_extras_job_case_shape() -> None:
    corpus = Path(__file__).parent.parent / "corpus"
    case = next(c for c in load_corpus(corpus) if c.name == "extras-job")
    assert case.target.extras == ["cli"]
    assert case.target.run is not None
    assert case.target.run.expect_stdout == "hello from extras-job"


def test_extras_job_facts() -> None:
    from deployer.facts import analyze_project

    project = (
        Path(__file__).parent.parent / "corpus" / "synthetic" / "extras-job" / "project"
    )
    facts = analyze_project(project)
    assert "cli" in facts.optional_dependencies
    assert facts.script_entrypoint == "main.py"
    assert facts.has_build_system is False


def test_entrypoint_override_case_shape() -> None:
    corpus = Path(__file__).parent.parent / "corpus"
    case = next(c for c in load_corpus(corpus) if c.name == "entrypoint-override")
    assert case.target.entrypoint == "app.py"
    assert case.target.service is not None
    assert case.target.service.port == 8000


def test_entrypoint_override_facts() -> None:
    from deployer.facts import analyze_project

    project = (
        Path(__file__).parent.parent
        / "corpus"
        / "synthetic"
        / "entrypoint-override"
        / "project"
    )
    facts = analyze_project(project)
    assert facts.script_entrypoint == "main.py"  # the decoy wins the fact
    assert "app.py" in facts.root_modules


def _write_external_toml(root: Path, names: list[str]) -> None:
    """Write external.toml with one entry per name, all clonable.

    Every entry points at the same local upstream repo (only its `name`
    differs), mirroring `test_run_bench_include_external_appends_...`.
    """
    url, pinned = _make_local_git_repo(root)
    entries = "".join(
        "[[targets]]\n"
        f'name = "{name}"\n'
        f'url = "{url}"\n'
        f'commit = "{pinned}"\n'
        "[targets.expected]\n"
        "requires_l2 = false\n"
        for name in names
    )
    (root / "external.toml").write_text(entries)


def test_bench_filter_skips_nonmatching_external_without_cloning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_case(tmp_path, "case-a", expected={"requires_l2": False})
    _write_external_toml(tmp_path, ["ext-match", "ext-other"])

    cloned: list[str] = []
    real_clone = bench.clone_external

    def tracking_clone(ext: ExternalTarget, dest_root: Path) -> BenchCase:
        cloned.append(ext.name)
        return real_clone(ext, dest_root)

    monkeypatch.setattr(bench, "clone_external", tracking_clone)
    report, _ = bench.run_bench(
        tmp_path,
        make_author=lambda case: None,  # every case skips (no fixture author)
        runtime=None,
        label="t",
        author_backend="fixture",
        pattern="ext-match",
        runs_root=tmp_path / "runs",
        include_external=True,
    )
    assert cloned == ["ext-match"]
    assert [c.case for c in report.cases] == ["ext-match"]


def test_bench_filter_no_match_anywhere_raises(tmp_path: Path) -> None:
    _make_case(tmp_path, "case-a", expected={"requires_l2": False})
    _write_external_toml(tmp_path, ["ext-a"])
    with pytest.raises(ValueError, match="no corpus cases match"):
        bench.run_bench(
            tmp_path,
            make_author=lambda case: None,
            runtime=None,
            label="t",
            author_backend="fixture",
            pattern="zzz-*",
            runs_root=tmp_path / "runs",
            include_external=True,
        )


def test_bench_filter_synthetic_only_still_raises(tmp_path: Path) -> None:
    _make_case(tmp_path, "case-a", expected={"requires_l2": False})
    with pytest.raises(ValueError, match="no corpus cases match"):
        bench.run_bench(
            tmp_path,
            make_author=lambda case: None,
            runtime=None,
            label="t",
            author_backend="fixture",
            pattern="zzz-*",
            runs_root=tmp_path / "runs",
            include_external=False,
        )


def test_golden_without_schema_version_reads_as_legacy_v0(
    tmp_path: Path, monkeypatch
) -> None:
    """A golden file predating versioning loads as v0, not as the current version.

    The key must be absent from the document, so the reader — not the model
    default — is what decides the legacy reading.
    """
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    golden_file = tmp_path / "golden" / "golden.json"
    raw = json.loads(golden_file.read_text())
    raw.pop("schema_version", None)
    golden_file.write_text(json.dumps(raw, indent=2))

    golden = load_baseline("golden", tmp_path)

    assert golden.schema_version == LEGACY_SCHEMA_VERSION


def test_raw_bench_report_without_schema_version_reads_as_legacy_v0(
    tmp_path: Path, monkeypatch
) -> None:
    """The legacy rule covers raw run reports, not just the golden baseline."""
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    report_file = run_dir / "bench-report.json"
    raw = json.loads(report_file.read_text())
    raw.pop("schema_version", None)
    report_file.write_text(json.dumps(raw, indent=2))

    baseline = load_baseline(run_dir, tmp_path)

    assert baseline.schema_version == LEGACY_SCHEMA_VERSION


def test_committed_golden_baseline_still_loads() -> None:
    """Regression: versioning must not orphan the golden already in the repo.

    Guards the choice of a defaulted field over a required one — a required
    `schema_version` would have broken `bench compare` against the committed
    baseline the moment it was added.

    Asserts loadability and a readable version, deliberately not a specific
    one: the committed baseline reads as v0 today, but the next promotion
    rewrites it as current, and that must not be a test failure.
    """
    corpus_root = Path(__file__).resolve().parent.parent / "corpus"
    baseline = load_baseline("golden", corpus_root)
    assert baseline.schema_version in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
    assert baseline.cases


def test_compare_does_not_report_schema_version_as_a_difference() -> None:
    """v1 is additive over v0, so a version gap is not a comparability finding.

    Pinned because the behaviour is implemented by absence: `schema_version`
    is simply not among the fields `_comparability_findings` diffs. Adding it
    there would make every run against the committed golden noisy.
    """
    candidate = _report(_rcase("a"))
    baseline = _golden(_gcase("a"))
    baseline.schema_version = LEGACY_SCHEMA_VERSION

    findings = compare_runs(candidate, baseline)

    assert not [f for f in findings if "schema_version" in f.detail]


def test_promoting_a_legacy_run_writes_a_current_version_golden(
    tmp_path: Path, monkeypatch
) -> None:
    """A golden is newly written here, so it carries this deployer's version.

    The legacy reading applies to the document on disk, not to what promotion
    then produces from it.
    """
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    report_file = run_dir / "bench-report.json"
    raw = json.loads(report_file.read_text())
    raw.pop("schema_version", None)
    report_file.write_text(json.dumps(raw, indent=2))

    golden = normalize_run(run_dir)

    assert golden.schema_version == SCHEMA_VERSION


def test_legacy_authoring_run_is_not_read_as_current_version(
    tmp_path: Path, monkeypatch
) -> None:
    """A pre-versioning authoring-run.json must not claim to be v1.

    Normalization reads these files back (`bench.py:501`), so the legacy rule
    has to cover them too: a defaulted field would silently upgrade an old
    document instead of marking it legacy.
    """
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    run_file = next(run_dir.glob("cases/*/authoring-run.json"))
    raw = json.loads(run_file.read_text())
    raw.pop("schema_version", None)
    run_file.write_text(json.dumps(raw, indent=2))

    loaded = bench._load_authoring_run(run_file)

    assert loaded.schema_version == LEGACY_SCHEMA_VERSION


def test_legacy_rule_reaches_reports_nested_in_an_authoring_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A legacy document must not read as v0 outside and v1 inside.

    `VerificationReport` is both a document of its own and a record nested in
    `AuthoringRun.iterations[*].report`; marking only the root would let the
    nested one silently claim the current version.
    """
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    run_file = next(run_dir.glob("cases/*/authoring-run.json"))
    raw = json.loads(run_file.read_text())
    raw.pop("schema_version", None)
    for iteration in raw["iterations"]:
        iteration["report"].pop("schema_version", None)
    run_file.write_text(json.dumps(raw, indent=2))

    loaded = bench._load_authoring_run(run_file)

    assert loaded.schema_version == LEGACY_SCHEMA_VERSION
    assert loaded.iterations[-1].report.schema_version == LEGACY_SCHEMA_VERSION


def test_report_with_non_object_root_is_a_value_error(
    tmp_path: Path, monkeypatch
) -> None:
    """A corrupt report must still exit 2, not raise AttributeError.

    The CLI catches only ValueError around `load_baseline` (`cli.py:430`), so
    a non-object JSON root has to arrive as one.
    """
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    (tmp_path / "golden" / "golden.json").write_text("null")

    with pytest.raises(ValueError):
        load_baseline("golden", tmp_path)


def test_baseline_of_an_unknown_major_version_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """Reading a document from a future major must fail, not compare as usual.

    Within a major, additive fields keep documents compatible, so a minor
    bump stays readable; an unknown major means the shape is not understood.
    """
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    golden_file = tmp_path / "golden" / "golden.json"
    raw = json.loads(golden_file.read_text())
    raw["schema_version"] = "2.0"
    golden_file.write_text(json.dumps(raw, indent=2))

    with pytest.raises(ValueError, match="schema_version"):
        load_baseline("golden", tmp_path)


def test_baseline_of_a_later_minor_version_still_reads(
    tmp_path: Path, monkeypatch
) -> None:
    """Additive-within-major is the stated policy, so a minor bump is readable."""
    run_dir = _bench_run_on_disk(tmp_path, monkeypatch)
    promote_run(run_dir, tmp_path)
    golden_file = tmp_path / "golden" / "golden.json"
    raw = json.loads(golden_file.read_text())
    raw["schema_version"] = "1.7"
    golden_file.write_text(json.dumps(raw, indent=2))

    assert load_baseline("golden", tmp_path).schema_version == "1.7"


def test_load_corpus_resolves_the_smoke_suite_beside_target_json(
    tmp_path: Path,
) -> None:
    case = _make_case(
        tmp_path, "agent", target={"run": {}, "smoke": {"suite": "suite.yaml"}}
    )
    (case / "suite.yaml").write_text("test_suite: x\n")

    loaded = load_corpus(tmp_path)[0]

    assert loaded.smoke_suite == case / "suite.yaml"


def test_skipped_smoke_makes_the_case_skipped_not_successful(
    tmp_path: Path, monkeypatch
) -> None:
    """A run where the seam never executed must not read as a pass.

    The clock is faked because the telemetry assertion below must not depend on
    how fast the machine is: everything this test exercises is monkeypatched, so
    real elapsed time rounds to 0.0 on a quick runner and a `> 0` assertion goes
    flaky. A clock that advances a fixed step per call makes the same intent —
    the measured wall time survives the skip — deterministic.
    """
    ticks = itertools.count(start=100.0, step=0.25)
    monkeypatch.setattr("deployer.bench.time.monotonic", lambda: next(ticks))
    case = _make_case(
        tmp_path, "agent", target={"run": {}, "smoke": {"suite": "suite.yaml"}}
    )
    (case / "suite.yaml").write_text("test_suite: x\n")
    run = _fake_run(True)
    run.iterations[-1].report.results.append(
        CheckResult(
            check_id="atp_smoke",
            status=CheckStatus.SKIPPED,
            message="atp 2.1.0 not installed; run is non-comparable",
        )
    )
    monkeypatch.setattr("deployer.bench.author_dockerfile", lambda *a, **k: run)

    result = run_case(
        load_corpus(tmp_path)[0],
        FixtureAuthor("FROM x:1\n"),
        ContainerRuntime(tool="docker"),
        tmp_path / "out",
        build_timeout=600,
        health_timeout=30,
    )

    assert result.outcome == "skipped"
    assert "atp" in result.skip_reason
    assert result.iterations == 1  # run telemetry survives the skip
    assert result.wall_time_s == 0.25  # measured from the faked clock, not dropped
    assert result.smoke_declared is True
    assert result.atp_smoke_status is CheckStatus.SKIPPED
