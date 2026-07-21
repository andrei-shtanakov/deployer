from pathlib import Path

import pytest

from deployer.author import author_dockerfile
from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    ProjectFacts,
    VerificationReport,
)

GOOD = (
    'FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\nCMD ["python", "main.py"]\n'
)
BAD_COPY = "FROM python:3.12-slim\nCOPY nope.py .\n"
NO_FROM = "RUN echo broken\n"


@pytest.fixture(autouse=True)
def _no_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


class ScriptedAuthor:
    """Returns queued Dockerfiles: first for generate(), rest for repair()."""

    def __init__(self, *dockerfiles: str) -> None:
        self._queue = list(dockerfiles)
        self.repair_calls = 0

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        return self._queue.pop(0)

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        self.repair_calls += 1
        return self._queue.pop(0)


def test_success_on_first_iteration(hello_service: Path) -> None:
    run = author_dockerfile(
        hello_service, DeployTarget(), ScriptedAuthor(GOOD), runtime=None
    )
    assert run.stopped_reason == "static_only"
    assert run.success is False  # static-only never counts as full success
    assert len(run.iterations) == 1
    assert run.iterations[0].dockerfile == GOOD


def test_repair_path_fixes_bad_copy(hello_service: Path) -> None:
    author = ScriptedAuthor(BAD_COPY, GOOD)
    run = author_dockerfile(hello_service, DeployTarget(), author, runtime=None)
    assert author.repair_calls == 1
    assert len(run.iterations) == 2
    assert run.stopped_reason == "static_only"


def test_no_progress_early_stop(hello_service: Path) -> None:
    author = ScriptedAuthor(BAD_COPY, BAD_COPY, GOOD)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, max_iterations=5, runtime=None
    )
    assert run.stopped_reason == "no_progress"
    assert len(run.iterations) == 2  # third (good) candidate never attempted


def test_budget_exhausted_returns_failed_run(hello_service: Path) -> None:
    author = ScriptedAuthor(NO_FROM, BAD_COPY, NO_FROM)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, max_iterations=3, runtime=None
    )
    assert run.stopped_reason == "budget_exhausted"
    assert run.success is False
    assert len(run.iterations) == 3


class ExplodingAuthor:
    """An author whose generate() or repair() raises, like an LLM/API outage."""

    def __init__(self, explode_on: str) -> None:
        self._explode_on = explode_on

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        if self._explode_on == "generate":
            raise RuntimeError("api down")
        return BAD_COPY

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        raise RuntimeError("rate limited")


def test_generate_error_yields_llm_error_run(hello_service: Path) -> None:
    run = author_dockerfile(
        hello_service, DeployTarget(), ExplodingAuthor("generate"), runtime=None
    )
    assert run.stopped_reason == "llm_error"
    assert run.iterations == []
    assert run.success is False
    assert "RuntimeError" in (run.llm_error or "")


def test_repair_error_preserves_completed_iterations(hello_service: Path) -> None:
    run = author_dockerfile(
        hello_service, DeployTarget(), ExplodingAuthor("repair"), runtime=None
    )
    assert run.stopped_reason == "llm_error"
    assert len(run.iterations) == 1  # the failed BAD_COPY iteration is preserved
    assert run.success is False


def test_environment_failure_retries_once_without_consuming_iteration(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import FailureKind

    calls = {"n": 0}

    def flaky_verify(
        dockerfile,
        project_path,
        target,
        runtime,
        facts=None,
        *,
        build_timeout,
        health_timeout,
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            return VerificationReport(
                results=[
                    CheckResult(
                        check_id="build",
                        status=CheckStatus.FAILED,
                        failure_kind=FailureKind.ENVIRONMENT,
                        message="toomanyrequests: rate limited",
                    )
                ]
            )
        return VerificationReport(
            results=[CheckResult(check_id="build", status=CheckStatus.PASSED)],
            docker_available=True,
        )

    monkeypatch.setattr("deployer.author.verify", flaky_verify)
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        ScriptedAuthor(GOOD),
        runtime=ContainerRuntime(tool="docker"),
    )
    assert run.environment_retries == 1
    assert len(run.iterations) == 1
    assert run.stopped_reason == "success"
    assert run.success is True


def test_hints_offered_recorded_and_facts_passed(
    hello_service: Path, monkeypatch, tmp_path: Path
) -> None:
    from deployer.models import SystemDepHint

    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("psycopg2\n")
    (project / "main.py").write_text("print('hi')\n")

    captured: dict = {}

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
        captured["facts"] = facts
        return VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.author.verify", spy_verify)
    run = author_dockerfile(project, DeployTarget(), ScriptedAuthor(GOOD), runtime=None)
    assert captured["facts"] is not None
    assert captured["facts"].package_manager == "pip"
    assert [h.python_package for h in run.hints_offered] == ["psycopg2"]
    assert isinstance(run.hints_offered[0], SystemDepHint)


def test_second_environment_failure_stops_run(hello_service: Path, monkeypatch) -> None:
    from deployer.models import FailureKind

    calls = {"n": 0}

    def env_fail_verify(
        dockerfile,
        project_path,
        target,
        runtime,
        facts=None,
        *,
        build_timeout,
        health_timeout,
    ):
        calls["n"] += 1
        return VerificationReport(
            results=[
                CheckResult(
                    check_id="build",
                    status=CheckStatus.FAILED,
                    failure_kind=FailureKind.ENVIRONMENT,
                    message=f"flake {calls['n']}",
                )
            ]
        )

    monkeypatch.setattr("deployer.author.verify", env_fail_verify)
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        ScriptedAuthor(GOOD),
        runtime=ContainerRuntime(tool="docker"),
    )
    assert run.stopped_reason == "environment_failure"
    assert run.environment_retries == 1
    assert run.success is False
    assert len(run.iterations) == 1


def test_author_forwards_timeouts_to_both_verify_calls(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import FailureKind

    captured: list[dict] = []

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
        captured.append(
            {"build_timeout": build_timeout, "health_timeout": health_timeout}
        )
        if len(captured) == 1:  # first call: environment flake -> triggers retry
            return VerificationReport(
                results=[
                    CheckResult(
                        check_id="build",
                        status=CheckStatus.FAILED,
                        failure_kind=FailureKind.ENVIRONMENT,
                        message="connection reset",
                    )
                ]
            )
        return VerificationReport(
            results=[CheckResult(check_id="build", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.author.verify", spy_verify)
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        ScriptedAuthor(GOOD),
        runtime=ContainerRuntime(tool="podman"),
        build_timeout=1200,
        health_timeout=45,
    )
    assert len(captured) == 2  # main call + environment-retry call
    assert all(c == {"build_timeout": 1200, "health_timeout": 45} for c in captured)
    assert run.stopped_reason == "success"
