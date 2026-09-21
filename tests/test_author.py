from pathlib import Path

import pytest

from deployer.artifacts import render_artifact_response
from deployer.author import author_dockerfile
from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    ServiceDependency,
    ServiceSpec,
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
    # parse_artifact_response strips the single-artifact text; content
    # otherwise unchanged.
    assert run.iterations[0].dockerfile == GOOD.strip()


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
        compose=None,
        ci=None,
        smoke_suite=None,
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
        compose=None,
        ci=None,
        smoke_suite=None,
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
        compose=None,
        ci=None,
        smoke_suite=None,
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
        compose=None,
        ci=None,
        smoke_suite=None,
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


def test_author_forwards_smoke_suite_to_both_verify_calls(
    hello_service: Path, monkeypatch, tmp_path: Path
) -> None:
    """Regression: environment retry must not drop the smoke suite path —
    both verify calls (main + retry) get the same resolved suite path.
    """
    from deployer.models import FailureKind

    suite_path = tmp_path / "suite.yaml"
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
        smoke_suite=suite_path,
    )
    assert len(captured) == 2  # main call + environment-retry call
    assert all(c == suite_path for c in captured)
    assert run.stopped_reason == "success"


def test_run_records_effective_config(hello_service: Path) -> None:
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        ScriptedAuthor(GOOD),  # the file's existing always-good stub
        max_iterations=2,
        runtime=None,
        build_timeout=123,
        health_timeout=7,
    )
    assert run.build_timeout_s == 123
    assert run.health_timeout_s == 7
    assert run.max_iterations == 2
    assert run.author_info is None  # stub has no .info()
    assert run.deployer_version  # installed package metadata


def test_run_with_runtime_survives_json_round_trip(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import AuthoringRun, RuntimeVersions

    def passing_verify(
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
        return VerificationReport(
            results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
        )

    monkeypatch.setattr("deployer.author.verify", passing_verify)
    # Avoid a real subprocess/SSH probe against the fake host below.
    monkeypatch.setattr(
        "deployer.author.probe_runtime_versions",
        lambda runtime: RuntimeVersions(client_version="1.0"),
    )
    run = author_dockerfile(
        hello_service,
        DeployTarget(),
        ScriptedAuthor(GOOD),
        runtime=ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli"),
    )
    round_tripped = AuthoringRun.model_validate_json(run.model_dump_json())
    assert round_tripped == run


COMPOSE_TARGET = DeployTarget(
    service=ServiceSpec(port=8000),
    dependencies=[ServiceDependency(name="cache", image="redis:7-alpine")],
)


class _ComposeAuthor:
    """Returns a valid sentinel response; repair returns it unchanged."""

    def __init__(self, dockerfile: str, compose: str) -> None:
        self.response = render_artifact_response(dockerfile, compose)

    def generate(self, facts, target):
        return self.response

    def repair(self, facts, target, artifact_text, report):
        return self.response


def test_author_records_compose_artifact(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    author = _ComposeAuthor("FROM python:3.12-slim\nCOPY main.py .", "services: {}")
    run = author_dockerfile(tmp_path, COMPOSE_TARGET, author, runtime=None)
    assert run.iterations[0].compose == "services: {}"
    assert run.iterations[0].dockerfile.startswith("FROM python:3.12-slim")


def test_author_forwards_compose_to_both_verify_calls(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: environment retry on a dependencies target must not drop
    the compose artifact — both verify calls (main + retry) get the same
    compose text parsed from the author's response.
    """
    from deployer.models import FailureKind

    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")
    compose_text = "services: {}"
    response = render_artifact_response(
        "FROM python:3.12-slim\nCOPY main.py .", compose_text
    )

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
        compose=None,
        ci=None,
        smoke_suite=None,
    ):
        captured.append({"compose": compose})
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
        tmp_path,
        COMPOSE_TARGET,
        ScriptedAuthor(response),
        runtime=ContainerRuntime(tool="podman"),
    )
    assert len(captured) == 2  # main call + environment-retry call
    assert all(c["compose"] == compose_text for c in captured)
    assert run.stopped_reason == "success"


def test_author_parse_failure_becomes_artifact_format_finding(
    tmp_path: Path,
) -> None:
    class _Broken:
        def generate(self, facts, target):
            return "FROM python:3.12-slim"  # no sentinels despite deps

        def repair(self, facts, target, artifact_text, report):
            return "FROM python:3.12-slim"  # still broken -> no_progress

    run = author_dockerfile(tmp_path, COMPOSE_TARGET, _Broken(), runtime=None)
    first = run.iterations[0].report
    assert [r.check_id for r in first.results] == ["artifact_format"]
    assert first.results[0].failure_kind == "authoring"
    assert run.stopped_reason == "no_progress"
    assert run.iterations[0].compose is None


def test_author_single_artifact_contract_unchanged(tmp_path: Path) -> None:
    class _Plain:
        def generate(self, facts, target):
            return "FROM python:3.12-slim"

        def repair(self, facts, target, artifact_text, report):
            return "FROM python:3.12-slim"

    run = author_dockerfile(tmp_path, DeployTarget(), _Plain(), runtime=None)
    assert run.iterations[0].compose is None
    assert run.stopped_reason == "static_only"


CI_TARGET = DeployTarget.model_validate_json('{"ci": {}}')


def test_author_records_ci_artifact(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("if __name__ == '__main__':\n    pass\n")

    class _CIAuthor:
        def generate(self, facts, target):
            return render_artifact_response(
                "FROM python:3.12-slim\nCOPY main.py .", ci="name: ci"
            )

        def repair(self, facts, target, artifact_text, report, /):
            return self.generate(facts, target)

    run = author_dockerfile(tmp_path, CI_TARGET, _CIAuthor(), runtime=None)
    assert run.iterations[0].ci == "name: ci"
    assert run.iterations[0].compose is None


def test_author_ci_parse_failure_is_artifact_format(tmp_path: Path) -> None:
    class _Broken:
        def generate(self, facts, target):
            return "FROM python:3.12-slim"  # no ci section despite target.ci

        def repair(self, facts, target, artifact_text, report, /):
            return "FROM python:3.12-slim"

    run = author_dockerfile(tmp_path, CI_TARGET, _Broken(), runtime=None)
    assert [r.check_id for r in run.iterations[0].report.results] == ["artifact_format"]
    assert run.iterations[0].ci is None


# --- stop-reason priority: UNKNOWN and PROJECT stop the loop instead of
# --- being handed to repair(), per the owner's ruling that repair is only
# --- permitted when every remaining failed check is AUTHORING. ---


class _SpyAuthor:
    """Author double whose repair() calls are counted.

    Unlike `ScriptedAuthor`, its output content never matters here: these
    tests replace `verify` with a scripted sequence of reports (via
    `author_loop`), so what `generate`/`repair` return is irrelevant beyond
    being a parseable Dockerfile.
    """

    def __init__(self) -> None:
        self.repair_calls = 0

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        return GOOD

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        self.repair_calls += 1
        return GOOD


@pytest.fixture
def spy_author() -> _SpyAuthor:
    return _SpyAuthor()


@pytest.fixture
def author_loop(monkeypatch):
    """Runs `author_dockerfile` against a scripted sequence of reports.

    Stubs `deployer.author.verify` so each iteration returns the next
    report from `reports` (the last one repeats if `verify` is called more
    times than `reports` supplies — e.g. the built-in ENVIRONMENT retry).
    This isolates failure-kind/stop-reason handling from the real L1/L2
    checks, which can't be made to report arbitrary `FailureKind`s directly.
    """

    def _run(
        project_path: Path,
        target: DeployTarget,
        *,
        author,
        reports: list[VerificationReport],
        max_iterations: int = 3,
    ):
        queue = list(reports)

        def fake_verify(
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
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr("deployer.author.verify", fake_verify)
        return author_dockerfile(
            project_path,
            target,
            author,
            runtime=None,
            max_iterations=max_iterations,
        )

    return _run


def report_with(*kinds: FailureKind) -> VerificationReport:
    """A report with one FAILED check per failure kind given."""
    return VerificationReport(
        results=[
            CheckResult(
                check_id=f"check_{kind.value}",
                status=CheckStatus.FAILED,
                failure_kind=kind,
                message=f"{kind.value} failure",
            )
            for kind in kinds
        ]
    )


def passing() -> VerificationReport:
    return VerificationReport(
        results=[CheckResult(check_id="parses", status=CheckStatus.PASSED)]
    )


def test_unknown_failure_stops_without_repair(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    """The loop broke only on ENVIRONMENT, so UNKNOWN fell through to repair."""
    run = author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[report_with(FailureKind.UNKNOWN)],
    )
    assert run.stopped_reason == "unknown_failure"
    assert spy_author.repair_calls == 0


def test_project_failure_stops_without_repair(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    run = author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[report_with(FailureKind.PROJECT)],
    )
    assert run.stopped_reason == "project_failure"
    assert spy_author.repair_calls == 0


def test_unknown_beside_project_stops_on_unknown(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    """With ENVIRONMENT absent, UNKNOWN still outranks PROJECT."""
    run = author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[report_with(FailureKind.UNKNOWN, FailureKind.PROJECT)],
    )
    assert run.stopped_reason == "unknown_failure"
    assert spy_author.repair_calls == 0


def test_authoring_beside_unknown_does_not_permit_repair(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    """Editing the shared artifact can also affect the unestablished cause,
    so "that failure was not addressed" cannot be promised."""
    run = author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[report_with(FailureKind.AUTHORING, FailureKind.UNKNOWN)],
    )
    assert run.stopped_reason == "unknown_failure"
    assert spy_author.repair_calls == 0


def test_all_authoring_still_repairs(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[report_with(FailureKind.AUTHORING), passing()],
    )
    assert spy_author.repair_calls == 1


def test_stop_priority_environment_before_unknown_before_project(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    run = author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[
            report_with(
                FailureKind.ENVIRONMENT, FailureKind.UNKNOWN, FailureKind.PROJECT
            )
        ],
    )
    assert run.stopped_reason == "environment_failure"


def test_all_failures_are_kept_in_the_report(
    hello_service: Path, author_loop, spy_author: _SpyAuthor
) -> None:
    run = author_loop(
        hello_service,
        DeployTarget(),
        author=spy_author,
        reports=[report_with(FailureKind.AUTHORING, FailureKind.UNKNOWN)],
    )
    kinds = {
        r.failure_kind for r in run.iterations[-1].report.results if r.failure_kind
    }
    assert kinds == {FailureKind.AUTHORING, FailureKind.UNKNOWN}
