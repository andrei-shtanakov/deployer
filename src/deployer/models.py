"""Pydantic contracts shared across the deployer pipeline."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


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


class ContainerRuntime(BaseModel):
    """Where and with which CLI the L2 sandbox runs.

    `host_source` records how the host was chosen so reports never lie
    about where a run happened (a pre-set DOCKER_HOST is captured, not
    silently inherited).
    """

    tool: Literal["docker", "podman"]
    host: str | None = None
    host_source: Literal["cli", "deployer_env", "native_env", "local"] = "local"

    @property
    def remote(self) -> bool:
        return self.host is not None


class RuntimeVersions(BaseModel):
    """Best-effort engine/CLI versions; failures are warnings, never fatal."""

    client_version: str | None = None
    server_version: str | None = None
    platform: str | None = None
    probe_warning: str | None = None


class AuthorInfo(BaseModel):
    """Which author produced a run — required for comparable bench data."""

    backend: str
    model_id: str | None = None
    prompt_sha256: str | None = None


class ExpectedOutcome(BaseModel):
    """What a corpus case is expected to do under the authoring loop."""

    expected_success: bool = True
    max_iterations: int = 3
    requires_l2: bool = True
    expected_failure_kind: FailureKind | None = None
    capabilities: list[str] = Field(default_factory=list)
    notes: str = ""


class ExternalTarget(BaseModel):
    """A pinned real-world project consumed by the bench via cloning."""

    name: str
    url: str
    commit: str
    target: DeployTarget = Field(default_factory=DeployTarget)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)

    @field_validator("name")
    @classmethod
    def _reject_path_traversal(cls, value: str) -> str:
        """Keep `name` a bare path segment: no traversal, no separators.

        The regex alone would still admit ".." (every char is in the
        allowed set), so it must be rejected explicitly alongside ".".
        """
        if not _SAFE_NAME_RE.fullmatch(value) or value in (".", ".."):
            raise ValueError(
                "ExternalTarget.name must match [A-Za-z0-9._-]+ and must "
                f'not be "." or "..": {value!r}'
            )
        return value


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
    runtime: ContainerRuntime | None = None
    runtime_versions: RuntimeVersions | None = None

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
    runtime: ContainerRuntime | None = None
    build_timeout_s: int | None = None
    health_timeout_s: int | None = None
    max_iterations: int | None = None
    runtime_versions: RuntimeVersions | None = None
    author_info: AuthorInfo | None = None
    deployer_version: str | None = None
    deployer_git_sha: str | None = None


class BenchCaseResult(BaseModel):
    """One corpus case's outcome within a bench run."""

    case: str
    outcome: Literal["matched", "mismatched", "skipped"]
    success: bool = False
    stopped_reason: StopReason | None = None
    iterations: int = 0
    image_size_bytes: int | None = None
    wall_time_s: float = 0.0
    skip_reason: str = ""
    failure_kinds: list[FailureKind] = Field(default_factory=list)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    external_url: str | None = None
    external_commit: str | None = None


class BenchReport(BaseModel):
    """Aggregate research artifact for one bench run over the corpus."""

    label: str
    author_backend: str
    corpus_commit: str | None = None
    deployer_version: str | None = None
    runtime: ContainerRuntime | None = None
    runtime_versions: RuntimeVersions | None = None
    build_timeout_s: int
    health_timeout_s: int
    cases: list[BenchCaseResult] = Field(default_factory=list)

    @property
    def success_rate(self) -> float | None:
        ran = [c for c in self.cases if c.outcome != "skipped"]
        if not ran:
            return None
        return round(sum(1 for c in ran if c.success) / len(ran), 3)

    @property
    def all_matched(self) -> bool:
        return all(c.outcome != "mismatched" for c in self.cases)


class GoldenCheck(BaseModel):
    """One check outcome in a golden baseline; messages are stripped as noise."""

    check_id: str
    status: CheckStatus
    failure_kind: FailureKind | None = None


class GoldenCase(BaseModel):
    """Normalized per-case baseline: comparable facts only, no noise."""

    case: str
    success: bool
    stopped_reason: StopReason | None = None
    iterations: int = 0
    failure_kinds: list[FailureKind] = Field(default_factory=list)
    image_size_bytes: int | None = None
    hadolint_status: CheckStatus | None = None
    checks: list[GoldenCheck] = Field(default_factory=list)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    external_url: str | None = None
    external_commit: str | None = None


class GoldenReport(BaseModel):
    """Committed golden baseline. Never stores hostnames or wall-clock data."""

    promoted_from_label: str
    corpus_commit: str | None = None
    deployer_version: str | None = None
    author_backend: str
    runtime_tool: str | None = None
    runtime_remote: bool = False
    runtime_platform: str | None = None
    build_timeout_s: int
    health_timeout_s: int
    cases: list[GoldenCase] = Field(default_factory=list)
