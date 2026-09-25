"""The admission section model (A §1, §6.2): shape and invariants."""

import pytest
from pydantic import ValidationError

from deployer.admission.model import (
    AdmissionSection,
    Binding,
    Defect,
    DifferenceDecision,
    Link,
    Ownership,
    SideLink,
    Unmet,
    ownership_from_facts,
)
from deployer.provenance.model import sha256_hex
from tests.admission.conftest import ARTIFACT, REPO, AdmissionSet

_BINDING = Binding(
    repo=REPO,
    head_sha="a" * 40,
    artifact_path=ARTIFACT,
    artifact_sha256="b" * 64,
)
_CONFIRMED = Ownership(
    status="confirmed",
    key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    record_sha256="c" * 64,
    snapshot_sha256="d" * 64,
)
_NOT_CONFIRMED = Ownership(status="not_confirmed", reason="step 1: missing")
_DEFECT = Defect(
    cls="missing_copy_source",
    file="Dockerfile",
    lines=(2, 2),
    object="src",
)
_LINK = Link(
    ci=SideLink(
        row="copy-missing/buildkit",
        object="src",
        evidence_file="ci.log",
        evidence_lines=[1, 2],
    ),
    local=SideLink(
        row="copy-missing/podman",
        object="src",
        evidence_file="build.stderr",
        evidence_lines=[1],
    ),
    differences=[DifferenceDecision(name="restoration", value="exact", allowed=True)],
)


def _admitted(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        verdict="admitted",
        binding=_BINDING,
        ownership=_CONFIRMED,
        defect=_DEFECT,
        link=_LINK,
        unmet=[],
    )
    base.update(overrides)
    return base


def _refused(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        verdict="insufficient_grounds",
        binding=_BINDING,
        ownership=_NOT_CONFIRMED,
        unmet=[Unmet(condition=1, reason="step 1: missing")],
    )
    base.update(overrides)
    return base


def test_valid_admitted_section_constructs() -> None:
    """An admitted section with confirmed ownership, a defect and a link."""
    section = AdmissionSection.model_validate(_admitted())
    assert section.verdict == "admitted"
    assert section.unmet == []


def test_valid_refused_section_constructs() -> None:
    """A refused section with a non-empty, unique unmet list."""
    section = AdmissionSection.model_validate(_refused())
    assert section.verdict == "insufficient_grounds"
    assert section.defect is None
    assert section.link is None


@pytest.mark.parametrize(
    "cls",
    [Binding, Ownership, Defect, SideLink, DifferenceDecision, Link, Unmet],
)
def test_every_model_forbids_unknown_fields(cls: type) -> None:
    """``extra="forbid"`` rejects a field none of the shapes declare."""
    with pytest.raises(ValidationError):
        cls.model_validate({"bogus": "field"})


def test_admission_section_forbids_unknown_fields() -> None:
    """The top-level section also forbids an unknown field."""
    with pytest.raises(ValidationError):
        AdmissionSection.model_validate({**_admitted(), "bogus": "field"})


class TestOwnershipInvariants:
    """Ownership's confirmed/not_confirmed shape (A §2.4, §6.2)."""

    def test_confirmed_with_reason_raises(self) -> None:
        """A confirmed ownership carries no reason."""
        with pytest.raises(ValidationError, match="confirmed ownership has no reason"):
            Ownership(
                status="confirmed",
                reason="should not be here",
                key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                record_sha256="c" * 64,
                snapshot_sha256="d" * 64,
            )

    def test_confirmed_without_fingerprint_raises(self) -> None:
        """A confirmed ownership needs a key_fingerprint."""
        with pytest.raises(ValidationError, match="needs a key_fingerprint"):
            Ownership(
                status="confirmed",
                record_sha256="c" * 64,
                snapshot_sha256="d" * 64,
            )

    def test_confirmed_without_record_sha256_raises(self) -> None:
        """A confirmed ownership needs a record_sha256."""
        with pytest.raises(ValidationError, match="needs a record_sha256"):
            Ownership(
                status="confirmed",
                key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                snapshot_sha256="d" * 64,
            )

    def test_confirmed_without_snapshot_sha256_raises(self) -> None:
        """A confirmed ownership needs a snapshot_sha256."""
        with pytest.raises(ValidationError, match="needs a snapshot_sha256"):
            Ownership(
                status="confirmed",
                key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                record_sha256="c" * 64,
            )

    def test_not_confirmed_without_reason_raises(self) -> None:
        """A not_confirmed ownership needs a reason."""
        with pytest.raises(ValidationError, match="needs a reason"):
            Ownership(status="not_confirmed")

    def test_not_confirmed_with_empty_reason_raises(self) -> None:
        """An empty reason does not count as set."""
        with pytest.raises(ValidationError, match="needs a reason"):
            Ownership(status="not_confirmed", reason="")

    def test_not_confirmed_may_carry_a_fingerprint(self) -> None:
        """A verified signature with a later failure (e.g. step 4) is still
        a verified signature (A §2.1)."""
        ownership = Ownership(
            status="not_confirmed",
            reason="step 4: artifact_sha256 differs",
            key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            record_sha256="c" * 64,
            snapshot_sha256="d" * 64,
        )
        assert (
            ownership.key_fingerprint
            == "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )


class TestDefectInvariants:
    """``Defect.lines`` must be ordered and positive (A §6.2)."""

    def test_zero_start_line_raises(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            Defect(
                cls="from_argument_count", file="Dockerfile", lines=(0, 1), object=""
            )

    def test_negative_line_raises(self) -> None:
        with pytest.raises(ValidationError, match="positive"):
            Defect(
                cls="from_argument_count", file="Dockerfile", lines=(-1, 1), object=""
            )

    def test_out_of_order_lines_raises(self) -> None:
        with pytest.raises(ValidationError, match="ordered"):
            Defect(
                cls="from_argument_count", file="Dockerfile", lines=(3, 1), object=""
            )

    def test_single_line_span_is_valid(self) -> None:
        """``start == end`` is ordered."""
        defect = Defect(
            cls="from_argument_count", file="Dockerfile", lines=(1, 1), object="FROM"
        )
        assert defect.lines == (1, 1)

    def test_unknown_class_rejected(self) -> None:
        """The catalogue is closed (A §3)."""
        with pytest.raises(ValidationError):
            Defect(
                cls="wrong_argument_count",  # type: ignore[arg-type]
                file="Dockerfile",
                lines=(1, 1),
                object="",
            )


class TestDefectClassAlias:
    """``class`` is the wire field (A §6.2 line 308); ``cls`` is the
    Python-side attribute, since ``class`` is a keyword."""

    def test_constructs_from_the_wire_key(self) -> None:
        """``model_validate`` accepts ``{"class": ...}``."""
        defect = Defect.model_validate(
            {
                "class": "missing_copy_source",
                "file": "Dockerfile",
                "lines": (2, 2),
                "object": "src",
            }
        )
        assert defect.cls == "missing_copy_source"

    def test_dump_emits_class_never_cls(self) -> None:
        """A plain ``model_dump_json`` uses the alias, not the attribute
        name, even with no ``by_alias`` argument."""
        dumped = _DEFECT.model_dump_json()
        assert '"class"' in dumped
        assert '"cls"' not in dumped

        dumped_dict = _DEFECT.model_dump()
        assert "class" in dumped_dict
        assert "cls" not in dumped_dict

    def test_class_alias_stays_hidden_inside_a_section(self) -> None:
        """The alias holds nested inside ``AdmissionSection``."""
        section = AdmissionSection.model_validate(_admitted())
        dumped = section.model_dump_json()
        assert '"class":"missing_copy_source"' in dumped
        assert '"cls"' not in dumped


class TestUnmetInvariants:
    """``Unmet.condition`` is one of the three closed conditions."""

    def test_unknown_condition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Unmet(condition=4, reason="not a real condition")  # type: ignore[arg-type]


class TestAdmissionSectionInvariants:
    """The verdict/unmet/defect/link shape (A §1, §6.2, §7)."""

    def test_admitted_requires_confirmed_ownership(self) -> None:
        with pytest.raises(ValidationError, match="confirmed ownership"):
            AdmissionSection.model_validate(_admitted(ownership=_NOT_CONFIRMED))

    def test_admitted_requires_defect(self) -> None:
        with pytest.raises(ValidationError, match="defect and a link"):
            AdmissionSection.model_validate(_admitted(defect=None))

    def test_admitted_requires_link(self) -> None:
        with pytest.raises(ValidationError, match="defect and a link"):
            AdmissionSection.model_validate(_admitted(link=None))

    def test_admitted_requires_empty_unmet(self) -> None:
        with pytest.raises(ValidationError, match="empty unmet"):
            AdmissionSection.model_validate(
                _admitted(unmet=[Unmet(condition=2, reason="stray")])
            )

    def test_insufficient_grounds_requires_non_empty_unmet(self) -> None:
        with pytest.raises(ValidationError, match="non-empty unmet"):
            AdmissionSection.model_validate(_refused(unmet=[]))

    def test_insufficient_grounds_forbids_defect(self) -> None:
        """A §1: no evidence is invented to fill the schema."""
        with pytest.raises(ValidationError, match="no defect or link"):
            AdmissionSection.model_validate(_refused(defect=_DEFECT))

    def test_insufficient_grounds_forbids_link(self) -> None:
        with pytest.raises(ValidationError, match="no defect or link"):
            AdmissionSection.model_validate(_refused(link=_LINK))

    def test_duplicate_unmet_conditions_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            AdmissionSection.model_validate(
                _refused(
                    unmet=[
                        Unmet(condition=1, reason="a"),
                        Unmet(condition=1, reason="b"),
                    ]
                )
            )


def test_admitted_section_round_trips_through_json() -> None:
    """A full ``admitted`` section (``Defect``, ``Link``, ``SideLink``,
    ``DifferenceDecision`` all present) survives ``model_dump_json`` /
    ``model_validate_json``, alias and all."""
    section = AdmissionSection.model_validate(_admitted())
    dumped = section.model_dump_json()
    assert '"class"' in dumped
    assert '"cls"' not in dumped

    restored = AdmissionSection.model_validate_json(dumped)
    assert restored == section
    assert restored.defect is not None
    assert restored.defect.cls == "missing_copy_source"


def test_step4_refusal_serialises_with_its_fingerprint(
    admission_set: AdmissionSet,
) -> None:
    """Task 7's ``hand_edit_dockerfile`` mutation (A §2.4 step 4: the
    signature verified, the current bytes do not match) maps to an
    ``insufficient_grounds`` section that round-trips with its fingerprint."""
    facts = admission_set.apply("hand_edit_dockerfile")
    assert facts.status == "not_confirmed"
    assert facts.step == 4
    assert facts.key_fingerprint is not None

    ownership = ownership_from_facts(facts)
    assert ownership.key_fingerprint == facts.key_fingerprint

    current_bytes = (admission_set.source / ARTIFACT).read_bytes()
    section = AdmissionSection(
        verdict="insufficient_grounds",
        binding=Binding(
            repo=REPO,
            head_sha="e" * 40,
            artifact_path=ARTIFACT,
            artifact_sha256=sha256_hex(current_bytes),
        ),
        ownership=ownership,
        unmet=[Unmet(condition=1, reason=facts.reason or "")],
    )

    restored = AdmissionSection.model_validate_json(section.model_dump_json())
    assert restored == section
    assert restored.ownership.key_fingerprint == facts.key_fingerprint


def test_artifact_hash_not_obtained_is_absent_and_never_admitted() -> None:
    """A §1: the hash of an unreadable artifact is absent, not invented; a
    section without it cannot be ``admitted``."""
    unread = _BINDING.model_copy(update={"artifact_sha256": None})
    refused = AdmissionSection.model_validate(_refused(binding=unread))
    assert refused.model_dump(mode="json")["binding"]["artifact_sha256"] is None
    with pytest.raises(ValidationError, match="artifact_sha256"):
        AdmissionSection.model_validate(_admitted(binding=unread))


@pytest.mark.parametrize(
    "field,value",
    [
        ("key_fingerprint", ""),
        ("key_fingerprint", "SHA256:short"),
        ("record_sha256", ""),
        ("record_sha256", "A" * 64),
        ("snapshot_sha256", "b" * 63),
    ],
)
def test_confirmed_ownership_values_must_be_well_formed(field: str, value: str) -> None:
    """An empty or malformed hash or fingerprint is not an obtained value."""
    data = _CONFIRMED.model_dump() | {field: value}
    with pytest.raises(ValidationError):
        Ownership.model_validate(data)


@pytest.mark.parametrize("head_sha", ["", "abc", "g" * 40, "a" * 41])
def test_binding_head_sha_must_be_a_full_object_name(head_sha: str) -> None:
    data = _BINDING.model_dump() | {"head_sha": head_sha}
    with pytest.raises(ValidationError):
        Binding.model_validate(data)
