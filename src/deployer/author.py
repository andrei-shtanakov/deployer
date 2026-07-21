"""The authoring control loop: deterministic pipeline, LLM inside one step."""

import importlib.metadata
import subprocess
import time
from pathlib import Path
from typing import Protocol

from deployer.facts import analyze_project
from deployer.hints import collect_hints
from deployer.models import (
    AuthoringRun,
    ContainerRuntime,
    DeployTarget,
    IterationRecord,
    ProjectFacts,
    StopReason,
    VerificationReport,
)
from deployer.runtime import probe_runtime_versions
from deployer.verify import DEFAULT_BUILD_TIMEOUT, DEFAULT_HEALTH_TIMEOUT, verify


def _deployer_version() -> str | None:
    try:
        return importlib.metadata.version("deployer")
    except importlib.metadata.PackageNotFoundError:
        return None


def _deployer_git_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


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
    runtime: ContainerRuntime | None = None,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> AuthoringRun:
    """Generate -> verify -> repair until success, budget, or no progress.

    The LLM (author) only ever sees facts and reports and returns text;
    this function owns files, subprocesses, and control flow. The timeouts
    bound each iteration's L2 build/healthcheck subprocesses (seconds).
    """
    facts = analyze_project(project_path)
    hints = collect_hints(facts)

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
                runtime,
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
                    runtime,
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
                stopped_reason = "success" if runtime is not None else "static_only"
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

    runtime_versions = probe_runtime_versions(runtime) if runtime is not None else None
    info_method = getattr(author, "info", None)
    author_info = info_method() if callable(info_method) else None

    return AuthoringRun(
        project=facts.name or project_path.name,
        target=target,
        iterations=iterations,
        environment_retries=environment_retries,
        docker_available=runtime is not None,
        hadolint_available=hadolint_available,
        stopped_reason=stopped_reason,
        success=stopped_reason == "success",
        llm_error=llm_error,
        hints_offered=hints,
        runtime=runtime,
        build_timeout_s=build_timeout,
        health_timeout_s=health_timeout,
        max_iterations=max_iterations,
        runtime_versions=runtime_versions,
        author_info=author_info,
        deployer_version=_deployer_version(),
        deployer_git_sha=_deployer_git_sha(),
    )
