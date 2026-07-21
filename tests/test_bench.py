"""Bench: models, offline fixture author, corpus loading, orchestration."""

import json
from pathlib import Path

import pytest

from deployer.bench import FixtureAuthor, load_corpus
from deployer.models import (
    BenchCaseResult,
    BenchReport,
    DeployTarget,
    ExpectedOutcome,
    ProjectFacts,
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
