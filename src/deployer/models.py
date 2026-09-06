"""Pydantic contracts shared across the deployer pipeline."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"
"""Version stamped on every report this deployer writes.

Compatibility policy: a document with no `schema_version` key reads as
`LEGACY_SCHEMA_VERSION` — the shape that predates versioning — and within a
major version additive fields are compatible, so a new report field does not
force a version bump. Only a breaking change to an existing field does.
"""

LEGACY_SCHEMA_VERSION = "0"
"""What a report written before versioning reads as.

Decided by the reader from the raw document, never by a model default: pydantic
cannot tell an absent JSON key from an argument the caller omitted.
"""

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_EXTRA_SEPARATOR_RUN_RE = re.compile(r"[-_.]+")


def normalize_extra_name(raw: str) -> str:
    """PEP 503/685 extra-name normalization (shared, must not drift).

    Lowercases and collapses every run of ``-``, ``_``, ``.`` into a
    single hyphen, so ``my.extra``, ``my__extra`` and ``my---extra`` all
    canonicalize to ``my-extra``.
    """
    return _EXTRA_SEPARATOR_RUN_RE.sub("-", raw.strip().lower())


class ServiceSpec(BaseModel):
    """Runtime surface the artifact must expose to count as deployed."""

    port: int
    healthcheck_path: str = "/health"


class RunSpec(BaseModel):
    """Job intent: the container must run to completion successfully.

    `expect_stdout` is a verifier-side oracle (substring of stdout); it is
    never shown to the authoring model.
    """

    expect_stdout: str | None = Field(default=None, min_length=1)


_DEP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ServiceDependency(BaseModel):
    """A pinned infra dependency the app service needs next to it."""

    name: str
    image: str
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_service_name(cls, value: str) -> str:
        if value == "app":
            raise ValueError(
                'ServiceDependency.name "app" is reserved for the app service'
            )
        if not _DEP_NAME_RE.fullmatch(value):
            raise ValueError(
                f"ServiceDependency.name must match [a-z][a-z0-9_-]*, got {value!r}"
            )
        return value

    @field_validator("image")
    @classmethod
    def _pinned_image(cls, value: str) -> str:
        """Same rule as base images: tag allowed, digest preferred.

        The tag lives in the reference's LAST path segment: splitting
        the whole value on its first colon would mistake a registry
        port (localhost:5000/redis) for a tag.
        """
        if "@sha256:" in value:
            return value
        _, _, tag = value.rsplit("/", 1)[-1].partition(":")
        if not tag or tag == "latest":
            raise ValueError(
                "ServiceDependency.image must be pinned (a tag or digest, "
                f"never :latest): {value!r}"
            )
        return value


class CISpec(BaseModel):
    """Request for a build-image CI workflow. Presence is the request.

    Deliberately empty: no kind/registry/triggers until a second
    implemented workflow kind exists — a discriminator now would be
    false extensibility. Unknown keys are rejected loudly: a silently
    dropped "kind" would no-op instead of failing the config.
    """

    model_config = ConfigDict(extra="forbid")


class SmokeSpec(BaseModel):
    """Request an ATP smoke test of the built image. Presence is the request.

    `suite` is a path to an ATP suite YAML, resolved relative to the
    `target.json` that declared it — never to the current directory and never
    to the project directory, so a target document stays portable.
    """

    model_config = ConfigDict(extra="forbid")

    suite: str = Field(min_length=1)
    timeout_s: int = Field(default=300, gt=0)


class DeployTarget(BaseModel):
    """Declarative deploy intent: what is wanted, never how."""

    base_image: str | None = None
    service: ServiceSpec | None = None
    run: RunSpec | None = None
    env: dict[str, str] = Field(default_factory=dict)
    memory_limit: str = "512m"
    system_packages: list[str] = Field(default_factory=list)
    extras: list[str] = Field(default_factory=list)
    entrypoint: str | None = Field(default=None, min_length=1)
    dependencies: list[ServiceDependency] = Field(default_factory=list)
    ci: CISpec | None = None
    smoke: SmokeSpec | None = None

    @model_validator(mode="after")
    def _service_and_run_exclusive(self) -> "DeployTarget":
        if self.service is not None and self.run is not None:
            raise ValueError(
                "DeployTarget.service and DeployTarget.run are mutually "
                "exclusive: an artifact is a service or a job, not both"
            )
        return self

    @model_validator(mode="after")
    def _dependencies_require_service(self) -> "DeployTarget":
        if self.dependencies:
            if self.service is None:
                raise ValueError(
                    "DeployTarget.dependencies require a service intent "
                    "(jobs with dependencies are unsupported)"
                )
            names = [d.name for d in self.dependencies]
            if len(names) != len(set(names)):
                raise ValueError("DeployTarget.dependencies names must be unique")
        return self

    @model_validator(mode="after")
    def _ci_incompatible_with_dependencies(self) -> "DeployTarget":
        if self.ci is not None and self.dependencies:
            raise ValueError(
                "DeployTarget.ci with dependencies is unsupported: "
                "compose-aware CI is a later iteration"
            )
        return self

    @model_validator(mode="after")
    def _smoke_is_a_job_intent(self) -> "DeployTarget":
        """ATP's container adapter talks to a job over stdin/stdout.

        `service` is refused rather than supported: the http adapter needs a
        published port, a readiness wait and guaranteed teardown — a separate
        seam. Loosening this later is backward compatible.
        """
        if self.smoke is None:
            return self
        if self.service is not None:
            raise ValueError(
                "DeployTarget.smoke with a service intent is unsupported: "
                "the ATP container adapter drives a job over stdin/stdout"
            )
        if self.run is None:
            raise ValueError(
                "DeployTarget.smoke requires a run intent: the target must "
                "declare that it is a job"
            )
        if self.run.expect_stdout is not None:
            raise ValueError(
                "DeployTarget.smoke with run.expect_stdout is unsupported: "
                "for an ATP agent stdout is the ATPResponse JSON, so a "
                "substring oracle over it is meaningless"
            )
        return self

    @field_validator("extras")
    @classmethod
    def _canonicalize_extras(cls, value: list[str]) -> list[str]:
        """PEP 503/685-normalize, reject empties, dedupe keeping first."""
        canonical: list[str] = []
        for raw in value:
            name = normalize_extra_name(raw)
            if not name:
                raise ValueError("DeployTarget.extras entries must be non-empty")
            if name not in canonical:
                canonical.append(name)
        return canonical


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
    has_poetry_lock: bool = False
    package_manager: Literal["uv", "pip", "poetry"] | None = None
    has_build_system: bool = False
    script_entrypoint: str | None = None
    requirements_files: dict[str, list[str]] = Field(default_factory=dict)
    optional_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    root_modules: list[str] = Field(default_factory=list)
    package_dirs: list[str] = Field(default_factory=list)


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


class BuiltImage(BaseModel):
    """The image L2 built, and what became of it.

    `lifecycle` is the contract a consumer reads: `ephemeral` means the image
    does not outlive the run, so the reference answers "what was tested", not
    "what is on your machine".
    """

    tag: str
    runtime: ContainerRuntime
    lifecycle: Literal["ephemeral"] = "ephemeral"
    cleanup_status: Literal["removed", "failed", "not_attempted"] = "not_attempted"


class VerificationReport(BaseModel):
    """Aggregated check results for one Dockerfile candidate."""

    schema_version: str = SCHEMA_VERSION
    results: list[CheckResult] = Field(default_factory=list)
    hadolint_available: bool = False
    actionlint_available: bool = False
    docker_available: bool = False
    atp_available: bool = False
    image_size_bytes: int | None = None
    runtime: ContainerRuntime | None = None
    runtime_versions: RuntimeVersions | None = None
    built_image: BuiltImage | None = None
    smoke_declared: bool = False
    """Whether the `target` this report was built for declared a smoke
    intent. Stamped once, in `verify_static`, at the report's single point
    of construction — an `atp_smoke` check only ever appears in `results`
    once L2 is reached, so a declared-but-never-reached-L2 smoke case would
    otherwise be indistinguishable from one that never declared smoke."""

    @property
    def passed(self) -> bool:
        return all(r.status is not CheckStatus.FAILED for r in self.results)

    @property
    def atp_smoke_status(self) -> CheckStatus | None:
        """Recorded status of the `atp_smoke` check, or None if absent."""
        for r in self.results:
            if r.check_id == "atp_smoke":
                return r.status
        return None

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
    compose: str | None = None
    ci: str | None = None
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

    schema_version: str = SCHEMA_VERSION
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
    hadolint_status: CheckStatus | None = None
    smoke_declared: bool = False
    """Whether the case's `deploy_target` declared a smoke intent at all.

    Set on every code path through `run_case`, including every early skip,
    so `satisfies_declared_smoke` can tell "declared and never ran" apart
    from "never declared" without re-reading the corpus.
    """
    atp_smoke_status: CheckStatus | None = None
    wall_time_s: float = 0.0
    skip_reason: str = ""
    failure_kinds: list[FailureKind] = Field(default_factory=list)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    external_url: str | None = None
    external_commit: str | None = None


class BenchReport(BaseModel):
    """Aggregate research artifact for one bench run over the corpus."""

    schema_version: str = SCHEMA_VERSION
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
    smoke_declared: bool = False
    """Mirrors `BenchCaseResult.smoke_declared`; carried across promotion so
    `satisfies_declared_smoke` reads a baseline the same way as a fresh run,
    without inferring declared-ness from whether `checks` happens to
    contain an `atp_smoke` entry (it will not, for a case that declared
    smoke but never reached L2)."""
    atp_smoke_status: CheckStatus | None = None
    checks: list[GoldenCheck] = Field(default_factory=list)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    external_url: str | None = None
    external_commit: str | None = None


def satisfies_declared_smoke(
    result: BenchCaseResult | GoldenCase | VerificationReport,
) -> bool:
    """Whether `result` satisfies a declared smoke intent, if it has one.

    True only when the case declared smoke (`smoke_declared`) and its
    recorded `atp_smoke_status` is exactly `PASSED`. A case that never
    declared smoke trivially satisfies it (there is nothing to satisfy);
    SKIPPED, FAILED, an absent check, and a pre-L2 skip on a case that
    *did* declare smoke are all False — a declared smoke intent is green
    only on `atp_smoke: PASSED`, never on "didn't run" or "not applicable".

    Shared by a fresh `BenchCaseResult` (from `run_case`), a promoted
    `GoldenCase` baseline, and a raw `VerificationReport` (from `bench
    verify`), so `--require-atp`, `bench compare` and `bench verify` all
    judge "satisfied" identically instead of each re-deriving it.
    """
    if not result.smoke_declared:
        return True
    return result.atp_smoke_status is CheckStatus.PASSED


class GoldenReport(BaseModel):
    """Committed golden baseline. Never stores hostnames or wall-clock data."""

    schema_version: str = SCHEMA_VERSION
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


class CompareFinding(BaseModel):
    """One regression (or notice) from comparing two bench runs."""

    level: Literal["hard", "important", "advisory"]
    case: str
    metric: str
    detail: str
