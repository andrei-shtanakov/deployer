"""The committed corpus itself: parses, and every fixture verifies green."""

from pathlib import Path

import pytest

from deployer.bench import load_corpus
from deployer.facts import analyze_project
from deployer.models import ContainerRuntime
from deployer.runtime import resolve_runtime
from deployer.verify import verify

CORPUS = Path(__file__).parent.parent / "corpus"
EXPECTED_CASES = [
    "no-build-system",
    "pip-requirements",
    "service-healthcheck",
    "slow-build",
    "system-deps-psycopg2",
    "uv-minimal",
]


def test_corpus_parses_and_is_complete() -> None:
    cases = load_corpus(CORPUS)
    assert [c.name for c in cases] == EXPECTED_CASES
    for case in cases:
        assert case.fixture_dockerfile is not None, case.name
        assert not (case.project_dir / "Dockerfile").exists(), case.name
        assert not (case.project_dir / "fixture.Dockerfile").exists(), case.name


def test_corpus_static_checks_pass_for_every_fixture() -> None:
    for case in load_corpus(CORPUS):
        assert case.fixture_dockerfile is not None
        report = verify(
            case.fixture_dockerfile.read_text(),
            case.project_dir,
            case.target,
            None,
            analyze_project(case.project_dir),
        )
        assert report.passed, f"{case.name}: {report.model_dump_json(indent=2)}"


@pytest.fixture(scope="module")
def runtime() -> ContainerRuntime:
    found = resolve_runtime()
    if found is None:
        pytest.skip("no container runtime available")
    return found


@pytest.mark.docker
@pytest.mark.parametrize("name", EXPECTED_CASES)
def test_corpus_fixture_verifies_end_to_end(name: str, runtime) -> None:
    case = {c.name: c for c in load_corpus(CORPUS)}[name]
    assert case.fixture_dockerfile is not None
    report = verify(
        case.fixture_dockerfile.read_text(),
        case.project_dir,
        case.target,
        runtime,
        analyze_project(case.project_dir),
    )
    assert report.passed, f"{name}: {report.model_dump_json(indent=2)}"


@pytest.mark.docker
def test_bench_run_offline_single_case_end_to_end(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    from deployer.cli import main

    monkeypatch.chdir(tmp_path)
    code = main(
        [
            "bench",
            "run",
            "--corpus",
            str(CORPUS),
            "--filter",
            "service-healthcheck",
            "--label",
            "smoke",
        ]
    )
    assert code == 0
    runs = list((tmp_path / ".deployer-runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "cases" / "service-healthcheck" / "authoring-run.json").is_file()
