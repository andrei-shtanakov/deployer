"""The fix document (design §8.1): shape, invariants, atomic save/load, the
writability probe and the input-integrity re-check."""

import os
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from deployer.fix.document import (
    FIX_SCHEMA_VERSION,
    CiAttempt,
    FixDocument,
    Input,
    LastOperation,
    LocalProof,
    Proposal,
    Publication,
    StoredFile,
    check_writable,
    load,
    save,
    verify_inputs,
)
from deployer.provenance.model import sha256_hex

FIX_ID = str(uuid.uuid4())


def _stored_file(path: Path, data: bytes) -> StoredFile:
    """Write ``data`` to ``path`` and return a matching ``StoredFile``."""
    path.write_bytes(data)
    return StoredFile(path=str(path), sha256=sha256_hex(data))


def _input(tmp_path: Path, **overrides: object) -> Input:
    """A minimal, internally consistent ``Input``, its files written under
    ``tmp_path`` so ``verify_inputs`` has real bytes to re-hash."""
    verdict = _stored_file(tmp_path / "verdict.json", b'{"admission": {}}')
    evidence = [_stored_file(tmp_path / "ci.log", b"defect log\n")]
    base: dict[str, object] = dict(
        verdict=verdict,
        root=str(tmp_path),
        try_dir=str(tmp_path / "try"),
        source_dir=str(tmp_path / "try" / "source"),
        evidence=evidence,
        binding={"repo": "o/r", "head_sha": "a" * 40},
        reproduction_binding={"job_id": 1},
        build={"dockerfile": "Dockerfile"},
        workflow_path=".github/workflows/ci.yml",
        workflow_sha256="b" * 64,
        backend="podman",
        clone=str(tmp_path / "clone"),
        origin="git@example.com:o/r.git",
        head="a" * 40,
        clean=True,
        target={"repo": "o/r", "head_sha": "a" * 40},
    )
    base.update(overrides)
    return Input.model_validate(base)


def _proposal(**overrides: object) -> Proposal:
    base: dict[str, object] = dict(
        cls="missing_copy_source",
        file="Dockerfile",
        lines=(2, 2),
        transformation="copy-source",
        original="COPY src ./src",
        replacement="COPY app ./src",
        ordinal=0,
        rationale=[{"kind": "model"}],
        envelope=[{"condition": "a", "result": True}],
    )
    base.update(overrides)
    return Proposal.model_validate(base)


def _local_proof(**overrides: object) -> LocalProof:
    base: dict[str, object] = dict(
        dockerfile_sha256="c" * 64,
        build={"platform": "linux/amd64"},
        backend="podman",
        versions={"podman": "5.0"},
        records_before=[{"instruction": "COPY", "result": "fail"}],
        records_after=[{"instruction": "COPY", "result": "pass"}],
        evidence=[{"file": "build.stdout", "lines": [1]}],
        later_failure=None,
    )
    base.update(overrides)
    return LocalProof.model_validate(base)


def _publication(**overrides: object) -> Publication:
    base: dict[str, object] = dict(
        worktree="/tmp/fix-worktree",
        branch="fix/copy-source",
        fix_commit="d" * 40,
        diff_ok=True,
        base="main",
        pr_url="https://example.com/pr/1",
    )
    base.update(overrides)
    return Publication.model_validate(base)


def _ci_attempt(**overrides: object) -> CiAttempt:
    base: dict[str, object] = dict(
        at="2026-09-25T00:00:00Z",
        considered=[{"run": 1}],
        outcome="ci_confirmed",
        reason=None,
        evidence=[{"file": "ci.log"}],
    )
    base.update(overrides)
    return CiAttempt.model_validate(base)


def _document(tmp_path: Path, **overrides: object) -> FixDocument:
    """A minimal ``in_progress`` document; ``overrides`` replace top-level
    fields, most often ``status`` plus whatever it now requires."""
    base: dict[str, object] = dict(
        schema_version=FIX_SCHEMA_VERSION,
        fix_id=FIX_ID,
        status="in_progress",
        input=_input(tmp_path),
    )
    base.update(overrides)
    return FixDocument.model_validate(base)


# --- shape -------------------------------------------------------------


def test_minimal_in_progress_document_constructs(tmp_path: Path) -> None:
    """The smallest legal document: just the input, freshly started."""
    doc = _document(tmp_path)
    assert doc.status == "in_progress"
    assert doc.stop_reason is None
    assert doc.ci_attempts == []


def test_forbids_unknown_top_level_field(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        FixDocument.model_validate(
            {**_document(tmp_path).model_dump(), "bogus": "field"}
        )


def test_fix_id_must_be_uuid4_shaped(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _document(tmp_path, fix_id="not-a-uuid")


def test_fix_id_rejects_uuid_of_another_version(tmp_path: Path) -> None:
    """A syntactically valid UUID that is not version 4 is still rejected."""
    not_v4 = "00000000-0000-1000-8000-000000000000"
    with pytest.raises(ValidationError):
        _document(tmp_path, fix_id=not_v4)


def test_class_round_trips_on_the_wire() -> None:
    """``Proposal.cls`` is accepted by name, emitted as ``class``."""
    proposal = _proposal()
    assert proposal.cls == "missing_copy_source"
    dumped = proposal.model_dump()
    assert dumped["class"] == "missing_copy_source"
    assert "cls" not in dumped
    again = Proposal.model_validate(dumped)
    assert again.cls == "missing_copy_source"
    dumped["cls"] = dumped.pop("class")
    by_attr = Proposal.model_validate(dumped)
    assert by_attr.cls == "missing_copy_source"


def test_class_round_trips_through_document_json(tmp_path: Path) -> None:
    """``class``, not ``cls``, is what actually lands in ``fix.json``."""
    doc = _document(
        tmp_path,
        status="locally_confirmed",
        proposal=_proposal(),
        local_proof=_local_proof(),
        publication=_publication(pr_url=None),
    )
    text = doc.model_dump_json()
    assert '"class":"missing_copy_source"' in text.replace(" ", "")
    assert '"cls"' not in text


def test_class_is_restricted_to_the_closed_defect_catalogue() -> None:
    """``cls``/``class`` accepts only a known ``DefectClass``, the same
    closed catalogue as ``admission.model.Defect``."""
    with pytest.raises(ValidationError):
        _proposal(cls="not_a_real_defect_class")


# --- invariants ----------------------------------------------------------


class TestStopReasonInvariant:
    def test_stopped_without_stop_reason_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="stopped requires a stop_reason"):
            _document(tmp_path, status="stopped")

    def test_stopped_with_stop_reason_is_valid(self, tmp_path: Path) -> None:
        doc = _document(tmp_path, status="stopped", stop_reason="no admission")
        assert doc.stop_reason == "no admission"

    def test_non_stopped_with_stop_reason_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="only stopped carries"):
            _document(tmp_path, status="in_progress", stop_reason="no admission")


class TestConfirmedStatusesRequireProposalAndProof:
    """``locally_confirmed``/``fix_proposed``/``ci_confirmed`` need
    ``proposal``, ``local_proof`` and ``publication.fix_commit``."""

    @pytest.mark.parametrize(
        "status", ["locally_confirmed", "fix_proposed", "ci_confirmed"]
    )
    def test_missing_proposal_raises(self, tmp_path: Path, status: str) -> None:
        with pytest.raises(ValidationError, match="requires a proposal"):
            _document(
                tmp_path,
                status=status,
                local_proof=_local_proof(),
                publication=_publication(),
                ci_attempts=[_ci_attempt()],
            )

    @pytest.mark.parametrize(
        "status", ["locally_confirmed", "fix_proposed", "ci_confirmed"]
    )
    def test_missing_local_proof_raises(self, tmp_path: Path, status: str) -> None:
        with pytest.raises(ValidationError, match="requires a local_proof"):
            _document(
                tmp_path,
                status=status,
                proposal=_proposal(),
                publication=_publication(),
                ci_attempts=[_ci_attempt()],
            )

    @pytest.mark.parametrize(
        "status", ["locally_confirmed", "fix_proposed", "ci_confirmed"]
    )
    def test_missing_publication_raises(self, tmp_path: Path, status: str) -> None:
        with pytest.raises(ValidationError, match="requires a publication fix_commit"):
            _document(
                tmp_path,
                status=status,
                proposal=_proposal(),
                local_proof=_local_proof(),
                ci_attempts=[_ci_attempt()],
            )

    @pytest.mark.parametrize(
        "status", ["locally_confirmed", "fix_proposed", "ci_confirmed"]
    )
    def test_publication_without_fix_commit_raises(
        self, tmp_path: Path, status: str
    ) -> None:
        with pytest.raises(ValidationError, match="requires a publication fix_commit"):
            _document(
                tmp_path,
                status=status,
                proposal=_proposal(),
                local_proof=_local_proof(),
                publication=_publication(fix_commit=None),
                ci_attempts=[_ci_attempt()],
            )

    def test_locally_confirmed_with_all_three_is_valid(self, tmp_path: Path) -> None:
        doc = _document(
            tmp_path,
            status="locally_confirmed",
            proposal=_proposal(),
            local_proof=_local_proof(),
            publication=_publication(pr_url=None),
        )
        assert doc.status == "locally_confirmed"


class TestPublishedStatusesRequirePrUrl:
    """``fix_proposed``/``ci_confirmed`` also need ``publication.pr_url``."""

    @pytest.mark.parametrize("status", ["fix_proposed", "ci_confirmed"])
    def test_missing_pr_url_raises(self, tmp_path: Path, status: str) -> None:
        with pytest.raises(ValidationError, match="requires a publication pr_url"):
            _document(
                tmp_path,
                status=status,
                proposal=_proposal(),
                local_proof=_local_proof(),
                publication=_publication(pr_url=None),
                ci_attempts=[_ci_attempt()],
            )

    def test_fix_proposed_with_pr_url_is_valid(self, tmp_path: Path) -> None:
        doc = _document(
            tmp_path,
            status="fix_proposed",
            proposal=_proposal(),
            local_proof=_local_proof(),
            publication=_publication(),
            ci_attempts=[_ci_attempt(outcome="ci_confirmation_insufficient")],
        )
        assert doc.status == "fix_proposed"


class TestCiConfirmedNeedsLastAttemptPositive:
    def test_empty_ci_attempts_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="last ci_attempts outcome"):
            _document(
                tmp_path,
                status="ci_confirmed",
                proposal=_proposal(),
                local_proof=_local_proof(),
                publication=_publication(),
                ci_attempts=[],
            )

    def test_last_attempt_insufficient_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="last ci_attempts outcome"):
            _document(
                tmp_path,
                status="ci_confirmed",
                proposal=_proposal(),
                local_proof=_local_proof(),
                publication=_publication(),
                ci_attempts=[
                    _ci_attempt(),
                    _ci_attempt(
                        outcome="ci_confirmation_insufficient",
                        reason="stale evidence",
                    ),
                ],
            )

    def test_last_attempt_positive_is_valid(self, tmp_path: Path) -> None:
        doc = _document(
            tmp_path,
            status="ci_confirmed",
            proposal=_proposal(),
            local_proof=_local_proof(),
            publication=_publication(),
            ci_attempts=[
                _ci_attempt(outcome="ci_confirmation_insufficient", reason="stale"),
                _ci_attempt(),
            ],
        )
        assert doc.status == "ci_confirmed"


# --- verify_inputs ---------------------------------------------------------


def test_verify_inputs_all_intact(tmp_path: Path) -> None:
    doc = _document(tmp_path)
    assert verify_inputs(doc) is None


def test_verify_inputs_changed_verdict(tmp_path: Path) -> None:
    doc = _document(tmp_path)
    Path(doc.input.verdict.path).write_bytes(b"tampered")
    reason = verify_inputs(doc)
    assert reason is not None
    assert doc.input.verdict.path in reason


def test_verify_inputs_missing_evidence_file(tmp_path: Path) -> None:
    doc = _document(tmp_path)
    evidence_path = Path(doc.input.evidence[0].path)
    evidence_path.unlink()
    reason = verify_inputs(doc)
    assert reason is not None
    assert str(evidence_path) in reason


def test_verify_inputs_stored_path_is_a_directory(tmp_path: Path) -> None:
    """A stored path that is now a directory returns a reason, not an
    exception (``Path.read_bytes`` raises ``IsADirectoryError``, an
    ``OSError``)."""
    doc = _document(tmp_path)
    evidence_path = Path(doc.input.evidence[0].path)
    evidence_path.unlink()
    evidence_path.mkdir()
    reason = verify_inputs(doc)
    assert reason is not None
    assert str(evidence_path) in reason


def _refuse_whole_reads(monkeypatch: pytest.MonkeyPatch, big: Path) -> None:
    """Make ``Path.read_bytes`` raise for ``big``: only streaming may read it."""
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self == big:
            raise AssertionError(f"{self} was read whole")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


def test_verify_inputs_streams_a_large_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-MB evidence file is hashed in chunks, never read whole."""
    big = tmp_path / "build.stdout"
    data = os.urandom(5 * 1024 * 1024 + 17)
    doc = _document(
        tmp_path, input=_input(tmp_path, evidence=[_stored_file(big, data)])
    )
    _refuse_whole_reads(monkeypatch, big)
    assert verify_inputs(doc) is None


def test_verify_inputs_streams_and_detects_a_large_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-byte change at the end of a multi-MB file is caught by
    streaming too."""
    big = tmp_path / "build.stdout"
    data = os.urandom(5 * 1024 * 1024)
    doc = _document(
        tmp_path, input=_input(tmp_path, evidence=[_stored_file(big, data)])
    )
    big.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    _refuse_whole_reads(monkeypatch, big)
    reason = verify_inputs(doc)
    assert reason is not None and "has changed" in reason


# --- save/load --------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    doc = _document(
        tmp_path,
        status="fix_proposed",
        proposal=_proposal(),
        local_proof=_local_proof(),
        publication=_publication(),
        ci_attempts=[_ci_attempt()],
        last_operation=LastOperation(
            command="publish", at="2026-09-25T00:00:00Z", result="ok"
        ),
    )
    path = tmp_path / "fix.json"
    save(doc, path)
    loaded = load(path)
    assert loaded == doc


def test_save_is_atomic(tmp_path: Path) -> None:
    """A temp file, never the target itself, is written and fsynced."""
    doc = _document(tmp_path)
    path = tmp_path / "fix.json"
    save(doc, path)
    assert not list(tmp_path.glob("fix.json.tmp-*"))
    assert load(path) == doc


def test_save_leaves_old_file_intact_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``os.replace`` leaves the old ``fix.json`` untouched and no
    stray temp file behind."""
    first = _document(tmp_path)
    path = tmp_path / "fix.json"
    save(first, path)
    original_bytes = path.read_bytes()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    second = _document(tmp_path, status="stopped", stop_reason="no admission")
    with pytest.raises(OSError, match="replace failed"):
        save(second, path)

    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob("fix.json.tmp-*"))


def test_save_revalidates_and_refuses_a_model_copy_mutation(
    tmp_path: Path,
) -> None:
    """Validators run only at construction; ``model_copy(update=...)`` (this
    repo's usual update idiom) skips them. ``save`` must catch a document
    that mutation left invalid before writing anything."""
    good = _document(
        tmp_path,
        status="locally_confirmed",
        proposal=_proposal(),
        local_proof=_local_proof(),
        publication=_publication(pr_url=None),
    )
    path = tmp_path / "fix.json"
    save(good, path)
    original_bytes = path.read_bytes()

    # locally_confirmed's publication has no pr_url, so fix_proposed (which
    # requires one) is now an invalid combination construction would refuse.
    broken = good.model_copy(update={"status": "fix_proposed"})
    with pytest.raises(ValidationError, match="requires a publication pr_url"):
        save(broken, path)

    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob("fix.json.tmp-*"))


def test_save_revalidates_and_refuses_a_nested_attribute_mutation(
    tmp_path: Path,
) -> None:
    """A nested mutation (plain attribute assignment on ``publication``, not
    even ``model_copy``) is caught too: pydantic never revalidates a
    mutated sub-model on its own."""
    doc = _document(
        tmp_path,
        status="fix_proposed",
        proposal=_proposal(),
        local_proof=_local_proof(),
        publication=_publication(),
        ci_attempts=[_ci_attempt(outcome="ci_confirmation_insufficient")],
    )
    path = tmp_path / "fix.json"
    save(doc, path)
    original_bytes = path.read_bytes()

    assert doc.publication is not None
    doc.publication.pr_url = None
    with pytest.raises(ValidationError, match="requires a publication pr_url"):
        save(doc, path)

    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob("fix.json.tmp-*"))


def test_load_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load(tmp_path / "does-not-exist.json")


def test_load_incomplete_json_raises_validation_error(tmp_path: Path) -> None:
    """Syntactically valid JSON that doesn't match the schema."""
    path = tmp_path / "fix.json"
    path.write_text("{}")
    with pytest.raises(ValidationError):
        load(path)


def test_load_malformed_json_raises_validation_error(tmp_path: Path) -> None:
    """Syntactically invalid JSON is also a ``ValidationError``, not a
    ``json.JSONDecodeError``: ``model_validate_json`` parses it itself."""
    path = tmp_path / "fix.json"
    path.write_text("{")
    with pytest.raises(ValidationError):
        load(path)


# --- check_writable -----------------------------------------------------


def test_check_writable_ok(tmp_path: Path) -> None:
    assert check_writable(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_check_writable_read_only_dir_returns_reason(tmp_path: Path) -> None:
    read_only = tmp_path / "ro"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        reason = check_writable(read_only)
    finally:
        read_only.chmod(0o700)  # allow pytest to clean up tmp_path
    assert reason is not None
    assert str(read_only) in reason
