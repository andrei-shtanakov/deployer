"""Pydantic contracts shared across the deployer pipeline."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    system_packages: list[str] = Field(default_factory=list)


class ProjectFacts(BaseModel):
    """Deterministically scanned project facts.

    Missing facts are None, never guessed.
    """

    name: str | None = None
    requires_python: str | None = None
    python_version: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    entrypoints: dict[str, str] = Field(default_factory=dict)
    has_uv_lock: bool = False
    package_manager: Literal["uv", "pip"] | None = None
    has_build_system: bool = False
    requirements_files: dict[str, list[str]] = Field(default_factory=dict)


class SystemDepHint(BaseModel):
    """Curated mapping from a python package to likely apt packages.

    Hints, not facts: a project may still resolve to a wheel needing none.
    """

    python_package: str
    build_packages: list[str] = Field(default_factory=list)
    runtime_packages: list[str] = Field(default_factory=list)


class CheckStatus(StrEnum):
    """Outcome status of a verification check."""

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class FailureKind(StrEnum):
    """Taxonomy of failure causes."""

    AUTHORING = "authoring"
    ENVIRONMENT = "environment"


class CheckResult(BaseModel):
    """Outcome of a single verification check."""

    check_id: str
    status: CheckStatus
    failure_kind: FailureKind | None = None
    message: str = ""

    @model_validator(mode="after")
    def enforce_failure_taxonomy(self) -> "CheckResult":
        if self.status is CheckStatus.FAILED and self.failure_kind is None:
            raise ValueError(
                "CheckResult with status=FAILED must have failure_kind set"
            )
        return self


class VerificationReport(BaseModel):
    """Aggregated check results for one Dockerfile candidate."""

    results: list[CheckResult] = Field(default_factory=list)
    hadolint_available: bool = False
    docker_available: bool = False
    image_size_bytes: int | None = None

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
    "success",
    "budget_exhausted",
    "no_progress",
    "environment_failure",
    "static_only",
    "llm_error",
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
    llm_error: str | None = None
    hints_offered: list[SystemDepHint] = Field(default_factory=list)
