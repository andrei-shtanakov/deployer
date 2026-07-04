from pathlib import Path

import pytest

from deployer.author import author_dockerfile
from deployer.models import (
    CheckResult,
    CheckStatus,
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
        hello_service, DeployTarget(), ScriptedAuthor(GOOD), run_docker=False
    )
    assert run.stopped_reason == "static_only"
    assert run.success is False  # static-only never counts as full success
    assert len(run.iterations) == 1
    assert run.iterations[0].dockerfile == GOOD


def test_repair_path_fixes_bad_copy(hello_service: Path) -> None:
    author = ScriptedAuthor(BAD_COPY, GOOD)
    run = author_dockerfile(hello_service, DeployTarget(), author, run_docker=False)
    assert author.repair_calls == 1
    assert len(run.iterations) == 2
    assert run.stopped_reason == "static_only"


def test_no_progress_early_stop(hello_service: Path) -> None:
    author = ScriptedAuthor(BAD_COPY, BAD_COPY, GOOD)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, max_iterations=5, run_docker=False
    )
    assert run.stopped_reason == "no_progress"
    assert len(run.iterations) == 2  # third (good) candidate never attempted


def test_budget_exhausted_returns_failed_run(hello_service: Path) -> None:
    author = ScriptedAuthor(NO_FROM, BAD_COPY, NO_FROM)
    run = author_dockerfile(
        hello_service, DeployTarget(), author, max_iterations=3, run_docker=False
    )
    assert run.stopped_reason == "budget_exhausted"
    assert run.success is False
    assert len(run.iterations) == 3


def test_environment_failure_retries_once_without_consuming_iteration(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import FailureKind

    calls = {"n": 0}

    def flaky_verify(dockerfile, project_path, target, tool):
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
    monkeypatch.setattr("deployer.author.detect_container_tool", lambda: "docker")
    run = author_dockerfile(hello_service, DeployTarget(), ScriptedAuthor(GOOD))
    assert run.environment_retries == 1
    assert len(run.iterations) == 1
    assert run.stopped_reason == "success"
    assert run.success is True
