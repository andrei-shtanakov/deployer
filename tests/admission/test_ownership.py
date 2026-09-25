"""Ownership verification, A §2.4 steps 0-6 (negative cases: A §8.3)."""

import os
import shutil
from pathlib import Path

import pytest

from deployer.admission.ownership import verify_ownership
from deployer.provenance import sshsig
from deployer.provenance.model import (
    POINTER,
    RECORD_FILE,
    SNAPSHOT_FILE,
    Record,
    Snapshot,
    sha256_hex,
)
from tests.admission.conftest import ARTIFACT, REPO, AdmissionSet


@pytest.mark.parametrize(
    ("step", "mutate"),
    [
        (1, "remove_pointer"),
        (1, "pointer_to_missing_dir"),
        (1, "record_unknown_format"),
        (1, "snapshot_unknown_format"),
        (2, "rename_dir"),
        (3, "bad_signature"),
        (3, "unknown_key"),
        (3, "revoked_key"),
        (4, "hand_edit_dockerfile"),  # Review Focus 1
        (4, "foreign_repo_resigned"),
        (4, "foreign_path_resigned"),
        (5, "snapshot_edited"),
        (6, "source_commit_differs_resigned"),
        (6, "tree_incomplete_resigned"),
        (6, "row_missing_field_resigned"),
        (6, "row_non_string_field_resigned"),
        (6, "duplicate_path_resigned"),
    ],
)
def test_each_step_refuses(step: int, mutate: str, admission_set: AdmissionSet) -> None:
    """One targeted mutation stops the check at its step, with a reason."""
    facts = admission_set.apply(mutate)
    assert facts.status == "not_confirmed" and facts.step == step
    assert facts.reason
    assert (facts.key_fingerprint is None) == (step <= 3)
    assert facts.snapshot is None


def test_happy_path_confirms_with_fingerprint(admission_set: AdmissionSet) -> None:
    """An untouched set confirms, with every obtained value reported."""
    facts = admission_set.verify()
    assert facts.status == "confirmed"
    assert facts.step is None and facts.reason is None
    assert facts.key_fingerprint == sshsig.fingerprint_of(admission_set.pub)
    set_dir = admission_set.set_dir
    assert facts.record_sha256 == set_dir.name
    snap_bytes = (set_dir / SNAPSHOT_FILE).read_bytes()
    assert facts.snapshot_sha256 == sha256_hex(snap_bytes)
    assert isinstance(facts.record, Record) and facts.record.repo == REPO
    assert isinstance(facts.snapshot, Snapshot) and facts.snapshot.tree_complete


def test_step4_keeps_the_verified_hashes(admission_set: AdmissionSet) -> None:
    """A verified signature over a stale record still reports its values."""
    facts = admission_set.apply("hand_edit_dockerfile")
    assert "artifact_sha256" in (facts.reason or "")
    assert facts.record_sha256 == admission_set.set_dir.name
    assert facts.record is not None and facts.snapshot_sha256 is not None


def test_step1_failure_reports_no_hashes(admission_set: AdmissionSet) -> None:
    """Nothing is reported that was not obtained."""
    facts = admission_set.apply("remove_pointer")
    assert facts.record_sha256 is None and facts.snapshot_sha256 is None
    assert facts.record is None
    assert POINTER in (facts.reason or "")


def _copy_trust_into(admission_set: AdmissionSet, target: Path) -> Path:
    """Copy the trust files to ``target`` (inside the tree); return it."""
    shutil.copytree(admission_set.trust, target)
    return target


def test_trust_inside_the_checked_tree_is_step_0(
    admission_set: AdmissionSet, tmp_path: Path
) -> None:
    """A trust dir resolving into the checked tree is refused, direct or via
    a symlink placed outside it."""
    inside = _copy_trust_into(admission_set, admission_set.source / "trust")
    link = tmp_path / "trust-link"
    link.symlink_to(inside, target_is_directory=True)
    for trust_dir in (inside, link):
        facts = verify_ownership(
            admission_set.source,
            repo=REPO,
            artifact_path=ARTIFACT,
            trust=trust_dir,
            checked_roots=(admission_set.source,),
        )
        assert facts.status == "not_confirmed" and facts.step == 0
        assert "inside" in (facts.reason or "")


def test_missing_allowed_signers_is_step_0(admission_set: AdmissionSet) -> None:
    """A trust dir without an allowed_signers file trusts nobody."""
    (admission_set.trust / "allowed_signers").unlink()
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 0


def test_missing_ssh_keygen_is_not_confirmed(
    admission_set: AdmissionSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ssh-keygen on PATH: not confirmed at step 3, never a crash
    (Review Focus 4)."""
    monkeypatch.setenv("PATH", "")
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 3
    assert facts.key_fingerprint is None


def _relocate_behind_link(path: Path, outside: Path) -> None:
    """Move ``path`` to ``outside`` and leave a symlink to it in its place,
    so a reader that follows links would still see valid content."""
    shutil.move(path, outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())


@pytest.mark.parametrize(
    "rel",
    [
        ".deployer",
        ".deployer/authoring",
        ".deployer/authoring/Dockerfile",
        f".deployer/authoring/{POINTER}",
        "SET",
        "SET/record.json",
        "SET/snapshot.json",
        "SET/record.json.sig",
    ],
)
def test_a_symlink_in_the_set_path_is_step_1(
    rel: str, admission_set: AdmissionSet, tmp_path: Path
) -> None:
    """Every component of the set is read without following a link."""
    set_rel = admission_set.set_dir.relative_to(admission_set.source).as_posix()
    target = admission_set.source / rel.replace("SET", set_rel)
    _relocate_behind_link(target, tmp_path / "elsewhere")
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 1
    assert "symlink" in (facts.reason or "")


def test_a_fifo_set_file_is_step_1(admission_set: AdmissionSet) -> None:
    """A non-regular set file is refused without blocking on it."""
    record = admission_set.set_dir / RECORD_FILE
    record.unlink()
    os.mkfifo(record)
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 1
    assert "regular file" in (facts.reason or "")


def test_a_symlinked_artifact_is_step_4(
    admission_set: AdmissionSet, tmp_path: Path
) -> None:
    """An artifact behind a link is not read, even when its target holds the
    signed bytes; the reason names the path."""
    _relocate_behind_link(admission_set.source / ARTIFACT, tmp_path / "Dockerfile")
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 4
    assert ARTIFACT in (facts.reason or "") and "symlink" in (facts.reason or "")


def test_a_non_regular_artifact_is_step_4(admission_set: AdmissionSet) -> None:
    """A directory at the artifact path is not an artifact."""
    artifact = admission_set.source / ARTIFACT
    artifact.unlink()
    artifact.mkdir()
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 4
    assert "regular file" in (facts.reason or "")


def test_a_missing_artifact_is_step_4(admission_set: AdmissionSet) -> None:
    """A missing artifact is named, not raised."""
    (admission_set.source / ARTIFACT).unlink()
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 4
    assert "missing" in (facts.reason or "")


@pytest.mark.parametrize("path", ["../Dockerfile", "/etc/passwd", "a//b", "./x"])
def test_an_artifact_path_leaving_the_tree_is_step_4(
    path: str, admission_set: AdmissionSet
) -> None:
    """The run's artifact path is only ever read as a plain relative path."""
    facts = verify_ownership(
        admission_set.source,
        repo=REPO,
        artifact_path=path,
        trust=admission_set.trust,
        checked_roots=(admission_set.source,),
    )
    assert facts.status == "not_confirmed" and facts.step == 4


def test_a_malformed_record_is_step_1(admission_set: AdmissionSet) -> None:
    """A record that does not validate is a step-1 failure."""
    (admission_set.set_dir / RECORD_FILE).write_bytes(b"not json\n")
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 1


def test_a_deeply_nested_record_is_step_1(admission_set: AdmissionSet) -> None:
    """Hostile nesting in a committed set file is a refusal, never a crash."""
    (admission_set.set_dir / RECORD_FILE).write_bytes(b"[" * 200_000)
    facts = admission_set.verify()
    assert facts.status == "not_confirmed" and facts.step == 1
