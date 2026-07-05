"""The authoring control loop: deterministic pipeline, LLM inside one step."""

import time
from pathlib import Path
from typing import Protocol

from deployer.facts import analyze_project
from deployer.hints import collect_hints
from deployer.models import (
    AuthoringRun,
    DeployTarget,
    IterationRecord,
    ProjectFacts,
    StopReason,
    VerificationReport,
)
from deployer.verify import (
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_HEALTH_TIMEOUT,
    detect_container_tool,
    verify,
)


class DockerfileAuthor(Protocol):
    """Anything that can draft and repair Dockerfiles from facts + intent."""

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str: ...

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str: ...


def author_dockerfile(
    project_path: Path,
    target: DeployTarget,
    author: DockerfileAuthor,
    *,
    max_iterations: int = 3,
    run_docker: bool = True,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> AuthoringRun:
    """Generate -> verify -> repair until success, budget, or no progress.

    The LLM (author) only ever sees facts and reports and returns text;
    this function owns files, subprocesses, and control flow.
    """
    facts = analyze_project(project_path)
    hints = collect_hints(facts)
    tool = detect_container_tool() if run_docker else None

    iterations: list[IterationRecord] = []
    environment_retries = 0
    hadolint_available = False
    stopped_reason: StopReason = "budget_exhausted"
    llm_error: str | None = None
    prev_signature: str | None = None

    dockerfile: str | None
    try:
        dockerfile = author.generate(facts, target)
    except Exception as exc:
        dockerfile = None
        llm_error = f"{exc.__class__.__name__}: {exc}"
        stopped_reason = "llm_error"

    if dockerfile is not None:
        for index in range(max_iterations):
            start = time.monotonic()
            report = verify(
                dockerfile,
                project_path,
                target,
                tool,
                facts,
                build_timeout=build_timeout,
                health_timeout=health_timeout,
            )
            if report.environment_failures and environment_retries == 0:
                environment_retries += 1
                report = verify(
                    dockerfile,
                    project_path,
                    target,
                    tool,
                    facts,
                    build_timeout=build_timeout,
                    health_timeout=health_timeout,
                )
            iterations.append(
                IterationRecord(
                    index=index,
                    dockerfile=dockerfile,
                    report=report,
                    duration_s=time.monotonic() - start,
                )
            )
            hadolint_available = report.hadolint_available

            if report.environment_failures:
                stopped_reason = "environment_failure"
                break
            if report.passed:
                stopped_reason = "success" if tool is not None else "static_only"
                break
            signature = report.error_signature()
            if signature == prev_signature:
                stopped_reason = "no_progress"
                break
            prev_signature = signature
            if index < max_iterations - 1:
                try:
                    dockerfile = author.repair(facts, target, dockerfile, report)
                except Exception as exc:
                    llm_error = f"{exc.__class__.__name__}: {exc}"
                    stopped_reason = "llm_error"
                    break

    return AuthoringRun(
        project=facts.name or project_path.name,
        target=target,
        iterations=iterations,
        environment_retries=environment_retries,
        docker_available=tool is not None,
        hadolint_available=hadolint_available,
        stopped_reason=stopped_reason,
        success=stopped_reason == "success",
        llm_error=llm_error,
        hints_offered=hints,
    )
