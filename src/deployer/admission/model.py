"""The ``admission`` section (A §6.2): may a failed CI run enter
``ci-fix-authoring``? A pure decision result, produced from the verified
facts of conditions (1)-(3) (A §2, §3, §4). No I/O, no causal class.

``ADMISSION_VERDICT_SCHEMA_VERSION`` is additive over R's reproduction
schema 1.2 (A §6.2); it is unrelated to the snapshot schema's own "1".
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deployer.admission.ownership import OwnershipFacts

ADMISSION_VERDICT_SCHEMA_VERSION = "1.3"

DefectClass = Literal["missing_copy_source", "from_argument_count"]


class Binding(BaseModel):
    """The state the admission holds for (A §6.2): always present.

    ``artifact_sha256`` is ``None`` only when the artifact's bytes at
    ``head_sha`` could not be read (missing, or behind a symlink): a hash of a
    file that cannot be read is not obtained, so it is absent (A §1). An
    ``admitted`` section always carries it.
    """

    model_config = ConfigDict(extra="forbid")
    repo: str
    head_sha: str
    artifact_path: str
    artifact_sha256: str | None


class Ownership(BaseModel):
    """Condition (1)'s outcome (A §2.4), always present.

    A fingerprint may accompany ``not_confirmed``: a verified signature
    (step 3 passed) with a later failure (e.g. step 4) is still a verified
    signature, not confirmed ownership of the *current* bytes (A §2.1).
    """

    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "not_confirmed"]
    reason: str | None = None
    key_fingerprint: str | None = None
    record_sha256: str | None = None
    snapshot_sha256: str | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.status == "confirmed":
            if self.reason is not None:
                raise ValueError("a confirmed ownership has no reason")
            if self.key_fingerprint is None:
                raise ValueError("a confirmed ownership needs a key_fingerprint")
            if self.record_sha256 is None:
                raise ValueError("a confirmed ownership needs a record_sha256")
            if self.snapshot_sha256 is None:
                raise ValueError("a confirmed ownership needs a snapshot_sha256")
        if self.status == "not_confirmed" and not self.reason:
            raise ValueError("a not_confirmed ownership needs a reason")
        return self


def ownership_from_facts(facts: OwnershipFacts) -> Ownership:
    """Map condition (1)'s verified facts (A §2.4) to the section's
    ``Ownership``."""
    return Ownership(
        status=facts.status,
        reason=facts.reason,
        key_fingerprint=facts.key_fingerprint,
        record_sha256=facts.record_sha256,
        snapshot_sha256=facts.snapshot_sha256,
    )


class Defect(BaseModel):
    """Condition (2): a closed-catalogue defect (A §3), required when
    ``admitted``. ``object`` is the source path for ``missing_copy_source``,
    the FROM instruction text for ``from_argument_count``.

    ``class`` is a Python keyword, so the attribute is ``cls``; the wire
    field is ``class`` (A §6.2), both ways: ``validate_by_name`` (with
    ``validate_by_alias`` kept at its default ``True``) accepts the
    attribute name too, ``serialize_by_alias`` means a plain
    ``model_dump()``/``model_dump_json()`` (no ``by_alias=False`` override)
    always emits ``class``, never ``cls``. This is pydantic's own
    replacement for ``populate_by_name`` (soft-deprecated since 2.11,
    "strictly equivalent" per its docstring to ``validate_by_name=True,
    validate_by_alias=True``); it is also the form pyrefly's pydantic
    support recognises for the synthesised ``__init__`` signature —
    ``populate_by_name`` alone left ``cls=...`` construction unresolvable.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )
    cls: DefectClass = Field(alias="class")
    file: str
    lines: tuple[int, int]
    object: str

    @model_validator(mode="after")
    def _lines_ordered_and_positive(self) -> Self:
        start, end = self.lines
        if start < 1 or end < 1:
            raise ValueError("lines must be positive (1-based)")
        if start > end:
            raise ValueError("lines must be ordered: start <= end")
        return self


class SideLink(BaseModel):
    """One side of the link (A §4.2): the matched row, its extracted object
    (``missing_copy_source`` only, else ``None``), and where the evidence is
    read from."""

    model_config = ConfigDict(extra="forbid")
    row: str
    object: str | None
    evidence_file: str
    evidence_lines: list[int]


class DifferenceDecision(BaseModel):
    """One difference considered (A §4.3) and whether it is allowed."""

    model_config = ConfigDict(extra="forbid")
    name: str
    value: str
    allowed: bool


class Link(BaseModel):
    """Condition (3): both sides bound to the same defect (A §4), required
    when ``admitted``."""

    model_config = ConfigDict(extra="forbid")
    ci: SideLink
    local: SideLink
    differences: list[DifferenceDecision]


class Unmet(BaseModel):
    """One concrete unmet or unconfirmed condition (A §1)."""

    model_config = ConfigDict(extra="forbid")
    condition: Literal[1, 2, 3]
    reason: str


class AdmissionSection(BaseModel):
    """The ``admission`` section (A §6.2). ``binding`` and ``ownership`` are
    always present; ``defect``/``link`` only when ``admitted``; ``unmet`` is
    empty iff ``admitted``."""

    model_config = ConfigDict(extra="forbid")
    verdict: Literal["admitted", "insufficient_grounds"]
    binding: Binding
    ownership: Ownership
    defect: Defect | None = None
    link: Link | None = None
    unmet: list[Unmet] = Field(default_factory=list)

    @model_validator(mode="after")
    def _verdict_shape(self) -> Self:
        if self.verdict == "admitted":
            if self.ownership.status != "confirmed":
                raise ValueError("admitted requires confirmed ownership")
            if self.defect is None or self.link is None:
                raise ValueError("admitted requires a defect and a link")
            if self.unmet:
                raise ValueError("admitted requires an empty unmet list")
            if self.binding.artifact_sha256 is None:
                raise ValueError("admitted requires the binding's artifact_sha256")
        if self.verdict == "insufficient_grounds":
            if not self.unmet:
                raise ValueError("insufficient_grounds requires a non-empty unmet list")
            if self.defect is not None or self.link is not None:
                raise ValueError("insufficient_grounds has no defect or link (A §1)")
        conditions = [item.condition for item in self.unmet]
        if len(conditions) != len(set(conditions)):
            raise ValueError("unmet conditions must be unique")
        return self
