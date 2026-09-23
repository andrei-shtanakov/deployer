"""Result types of the ``reproduction`` section (spec §6). No causal class."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

CheckStatus = Literal["passed", "failed", "skipped", "observation", "inconclusive"]
EvidenceKind = Literal[
    "path_absent", "ignore_file", "log_excerpt", "output_file", "tree_listing"
]
BoundBy = Literal[
    "buildkit_error_block",
    "buildkit_parse_error",
    "step_text",
    "parser_finding_keyword",
]
Dimension = Literal["same", "differs", "unknown"]
ComparisonState = Literal[
    "not_attempted",
    "inconclusive",
    "not_reproduced",
    "different_failure",
    "same_instruction_different_output",
    "reproduced",
    "reproduced_with_differences",
]
SignatureMatch = Literal["equal", "unequal", "unavailable", "not_compared"]
SectionStatus = Literal["attempted", "refused", "unavailable", "not_requested"]


class ReproEvidence(BaseModel):
    """Typed evidence: an absence is an assertion checked against a listing."""

    kind: EvidenceKind
    path: str | None = None
    listing: str | None = None
    text: str | None = None


class Location(BaseModel):
    """A Dockerfile span, context-relative ``file``, 1-based inclusive lines."""

    file: str
    lines: tuple[int, int]


class ReproductionCheck(BaseModel):
    """One check result; its own type because ``CheckResult`` demands a class."""

    check_id: str
    status: CheckStatus
    finding: str | None = None
    reason: str | None = None
    location: Location | None = None
    evidence: list[ReproEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.status == "failed":
            if not self.finding:
                raise ValueError("a failed check needs a finding")
            if not self.evidence:
                raise ValueError("a failed check needs at least one evidence entry")
        if self.status in ("skipped", "inconclusive") and not self.reason:
            raise ValueError(f"a {self.status} check needs a reason")
        return self


class Restoration(BaseModel):
    """How exact the restored tree is (§1.3)."""

    state: Literal["exact", "approximation", "unavailable"]
    sha: str
    unmet: list[str] = Field(default_factory=list)


class Binding(BaseModel):
    """The failed job, the build step and what it builds (§1.2)."""

    job_id: int
    workflow_job: str
    build_step: int
    dockerfile: str
    context: str = "."


class Environment(BaseModel):
    """The local side as detected (§2, §4.2)."""

    backend: Literal["docker", "podman"]
    backend_version: str | None
    endpoint: str
    endpoint_source: str
    buildx_version: str | None
    host_arch: str
    syntax_directive: str | None


class InstructionRef(BaseModel):
    """An instruction identity: a source line span, or a parse failure line."""

    kind: Literal["span", "parse"]
    lines: tuple[int, int]
    bound_by: BoundBy


class BuildResult(BaseModel):
    """The adapter's build (§4.3) and its cleanup (§4.4)."""

    argv: list[str]
    exit_code: int | None
    launch_error: str | None
    failed_instruction: InstructionRef | None
    signature: str | None
    stdout: str
    stderr: str
    image_cleanup: Literal["removed", "failed", "not_attempted"]
    build_containers: Literal["removed_by_builder", "not_checked", "not_applicable"]

    @model_validator(mode="after")
    def _exit_iff_no_launch_error(self) -> Self:
        if (self.exit_code is None) != (self.launch_error is not None):
            raise ValueError("exit_code is null iff launch_error is set")
        return self


class Comparison(BaseModel):
    """One CI-vs-local state from the ordered list of §7.3."""

    state: ComparisonState
    reason: str | None = None
    ci_instruction: InstructionRef | None = None
    signature_match: SignatureMatch | None = None
    dimensions: dict[str, Dimension] = Field(default_factory=dict)
    values: dict[str, tuple[str | None, str | None]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reason_iff_unfinished(self) -> Self:
        needs = self.state in ("not_attempted", "inconclusive")
        if needs != (self.reason is not None):
            raise ValueError("reason is set iff state is not_attempted/inconclusive")
        return self


class ReproductionSection(BaseModel):
    """The ``reproduction`` section of the verdict and of each try's manifest."""

    status: SectionStatus
    try_dir: str | None = None
    refusal: str | None = None
    restoration: Restoration | None = None
    binding: Binding | None = None
    environment: Environment | None = None
    checks: list[ReproductionCheck] = Field(default_factory=list)
    build: BuildResult | None = None
    comparison: Comparison | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        if self.status in ("refused", "unavailable"):
            if not self.refusal:
                raise ValueError(f"a {self.status} section needs a refusal")
            if self.build is not None or self.comparison is not None:
                raise ValueError(f"a {self.status} section has no build/comparison")
        if self.status == "attempted":
            if self.refusal is not None:
                raise ValueError("an attempted section has no refusal")
            if self.build is None or self.comparison is None:
                raise ValueError("an attempted section has a build and a comparison")
        return self
