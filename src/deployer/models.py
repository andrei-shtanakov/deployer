"""Pydantic contracts shared across the deployer pipeline."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ServiceSpec(BaseModel):
    """Runtime surface the artifact must expose to count as deployed."""

    port: int
    healthcheck_path: str = "/health"


class DeployTarget(BaseModel):
    """Declarative deploy intent: what is wanted, never how."""

    base_image: str | None = None
    service: ServiceSpec | None = None
    env: dict[str, str] = Field(default_factory=dict)
    memory_limit: str = "512m"


class ProjectFacts(BaseModel):
    """Deterministically scanned project facts. Missing facts are None, never guessed."""

    name: str | None = None
    requires_python: str | None = None
    python_version: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    entrypoints: dict[str, str] = Field(default_factory=dict)
    has_uv_lock: bool = False


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class FailureKind(StrEnum):
    AUTHORING = "authoring"
    ENVIRONMENT = "environment"


class CheckResult(BaseModel):
    """Outcome of a single verification check."""

    check_id: str
    status: CheckStatus
    failure_kind: FailureKind | None = None
    message: str = ""


class VerificationReport(BaseModel):
    """Aggregated check results for one Dockerfile candidate."""

    results: list[CheckResult] = Field(default_factory=list)
    hadolint_available: bool = False
    docker_available: bool = False

    @property
    def passed(self) -> bool:
        return all(r.status is not CheckStatus.FAILED for r in self.results)

    @property
    def environment_failures(self) -> list[CheckResult]:
        return [
            r
            for r in self.results
            if r.status is CheckStatus.FAILED
            and r.failure_kind is FailureKind.ENVIRONMENT
        ]

    def error_signature(self) -> str:
        """Normalized fingerprint of failures, for no-progress detection."""
        parts = [
            f"{r.check_id}:{r.message.splitlines()[0] if r.message else ''}"
            for r in self.results
            if r.status is CheckStatus.FAILED
        ]
        return "|".join(sorted(parts))


class IterationRecord(BaseModel):
    """One generate/repair attempt and its verification outcome."""

    index: int
    dockerfile: str
    report: VerificationReport
    duration_s: float


StopReason = Literal[
    "success", "budget_exhausted", "no_progress", "environment_failure", "static_only"
]


class AuthoringRun(BaseModel):
    """Research artifact: the full record of one authoring loop."""

    project: str
    target: DeployTarget
    iterations: list[IterationRecord] = Field(default_factory=list)
    environment_retries: int = 0
    docker_available: bool = False
    hadolint_available: bool = False
    stopped_reason: StopReason
    success: bool
