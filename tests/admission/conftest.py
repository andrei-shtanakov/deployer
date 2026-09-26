"""A real, issued authoring set in a restored tree, plus named mutations of
it (A §8.3): each mutation targets one step of A §2.4."""

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deployer import runtime as runtime_mod
from deployer.admission.ownership import OwnershipFacts, verify_ownership
from deployer.facts import ProjectFacts
from deployer.forge import FailedRun, load_snapshot
from deployer.provenance import issue, sshsig, trust
from deployer.provenance.model import (
    POINTER,
    RECORD_FILE,
    SET_PARENT,
    SET_ROOT,
    SIGNATURE_FILE,
    SNAPSHOT_FILE,
    Record,
    Snapshot,
    TreeRow,
    set_dir_name,
    sha256_hex,
)
from deployer.reproduce.model import ReproductionSection
from deployer.reproduce.run import reproduce_run, restore_owner_write
from tests.provenance.conftest import make_key, make_repo_with_origin
from tests.reproduce.bundles import BUNDLES, BundleGh, _containers
from tests.reproduce.conftest import FakeContainers

DOCKERFILE = "FROM python:3.12-slim\nCOPY src ./src\n"
REPO = "o/r"
ARTIFACT = "Dockerfile"


def _json_bytes(data: dict[str, Any]) -> bytes:
    """Sorted, compact JSON plus a newline, the canonical shape."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return text.encode() + b"\n"


def confirmed_ownership(head_sha: str, tree: list[TreeRow]) -> OwnershipFacts:
    """A hand-built confirmed ownership whose snapshot lists ``tree``."""
    record = Record(
        format_version="1",
        repo="example/project",
        artifact_path="Dockerfile",
        artifact_sha256="a" * 64,
        source_commit=head_sha,
        snapshot_sha256="b" * 64,
        deployer_version="0.1",
    )
    snapshot = Snapshot(
        format_version="1",
        source_commit=head_sha,
        tree=tree,
        tree_complete=True,
        facts=ProjectFacts(),
    )
    return OwnershipFacts(
        status="confirmed",
        step=None,
        reason=None,
        key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        record_sha256="a" * 64,
        snapshot_sha256="b" * 64,
        record=record,
        snapshot=snapshot,
    )


class AdmissionSet:
    """An issued set copied into ``source`` (the restored tree), with the
    trust dir and keys needed to verify it or to re-sign a mutation."""

    def __init__(self, tmp: Path, origin_url: str = "git@github.com:o/r.git") -> None:
        """Issue a set in a fresh repo whose ``origin`` is ``origin_url`` and
        restore its tree to ``source``."""
        keys = tmp / "keys"
        keys.mkdir()
        self.key, self.pub = make_key(keys)
        self.other_key, _ = make_key(keys, "other")
        self.trust = tmp / "trust"
        trust.add(self.trust, self.pub)
        origin = tmp / "origin"
        origin.mkdir()
        repo = make_repo_with_origin(origin, origin_url)
        pre = issue.preflight(repo, self.key)
        assert isinstance(pre, issue.Preflight)
        (repo / ARTIFACT).write_text(DOCKERFILE)
        out = issue.issue(pre, self.key, "0.1", DOCKERFILE.encode())
        assert out.published, out.reason
        self.source = tmp / "source"
        shutil.copytree(repo, self.source, ignore=shutil.ignore_patterns(".git"))

    @property
    def auth(self) -> Path:
        """``.deployer/authoring`` in the restored tree."""
        return self.source / SET_ROOT

    @property
    def set_dir(self) -> Path:
        """The directory the pointer currently names."""
        return self.auth / (self.auth / POINTER).read_text().strip()

    def verify(self) -> OwnershipFacts:
        """Run A §2.4 over the restored tree as the run's repo and path."""
        return verify_ownership(
            self.source,
            repo=REPO,
            artifact_path=ARTIFACT,
            trust=self.trust,
            checked_roots=(self.source,),
        )

    def apply(self, name: str) -> OwnershipFacts:
        """Perform the named mutation, then verify."""
        _MUTATIONS[name](self)
        return self.verify()

    def load(self, file: str) -> dict[str, Any]:
        """A set file of the current set, as JSON."""
        return json.loads((self.set_dir / file).read_bytes())

    def rewrite(self, file: str, data: dict[str, Any]) -> None:
        """Overwrite a set file in place (no re-signing, no rename)."""
        (self.set_dir / file).write_bytes(_json_bytes(data))

    def resign(
        self,
        record: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
        key: Path | None = None,
    ) -> None:
        """Publish ``record`` (and ``snapshot``, rehashed into it) as a new,
        signed set under its own hash, and repoint to it."""
        old = self.set_dir
        snap_bytes = (
            (old / SNAPSHOT_FILE).read_bytes()
            if snapshot is None
            else _json_bytes(snapshot)
        )
        record = {**record, "snapshot_sha256": sha256_hex(snap_bytes)}
        rec_bytes = _json_bytes(record)
        rec_sha = sha256_hex(rec_bytes)
        new = self.auth / set_dir_name(rec_sha)
        new.mkdir(exist_ok=True)  # the same record re-signed keeps its dir
        (new / RECORD_FILE).write_bytes(rec_bytes)
        (new / SNAPSHOT_FILE).write_bytes(snap_bytes)
        sig = sshsig.sign(rec_bytes, key or self.key)
        (new / SIGNATURE_FILE).write_bytes(sig)
        if old != new:
            shutil.rmtree(old)
        (self.auth / POINTER).write_text(set_dir_name(rec_sha) + "\n")

    def resign_snapshot(self, change: Callable[[dict[str, Any]], None]) -> None:
        """Change the snapshot JSON, rehash it into the record, re-sign."""
        snapshot = self.load(SNAPSHOT_FILE)
        change(snapshot)
        self.resign(self.load(RECORD_FILE), snapshot)


def _remove_pointer(s: AdmissionSet) -> None:
    """No set is published."""
    (s.auth / POINTER).unlink()


def _pointer_to_missing_dir(s: AdmissionSet) -> None:
    """The pointer names a directory that does not exist."""
    (s.auth / POINTER).write_text(f"{SET_PARENT}/{'0' * 64}\n")


def _record_unknown_format(s: AdmissionSet) -> None:
    """The record declares an unsupported format_version."""
    s.rewrite(RECORD_FILE, {**s.load(RECORD_FILE), "format_version": "2"})


def _snapshot_unknown_format(s: AdmissionSet) -> None:
    """The snapshot declares an unsupported format_version."""
    s.rewrite(SNAPSHOT_FILE, {**s.load(SNAPSHOT_FILE), "format_version": "2"})


def _rename_dir(s: AdmissionSet) -> None:
    """The directory name is no longer the record's hash."""
    other = "f" * 64
    s.set_dir.rename(s.auth / set_dir_name(other))
    (s.auth / POINTER).write_text(set_dir_name(other) + "\n")


def _bad_signature(s: AdmissionSet) -> None:
    """A well-formed signature by the trusted key over other bytes."""
    (s.set_dir / SIGNATURE_FILE).write_bytes(sshsig.sign(b"other bytes\n", s.key))


def _unknown_key(s: AdmissionSet) -> None:
    """The record signed by a key the trust set does not hold."""
    s.resign(s.load(RECORD_FILE), key=s.other_key)


def _revoked_and_removed(s: AdmissionSet) -> None:
    """``deployer trust revoke``: the key leaves allowed_signers and is
    listed as revoked."""
    trust.revoke(s.trust, s.pub)


def _revoked_only(s: AdmissionSet) -> None:
    """The key stays allowed but is listed in revoked_keys: only ``-r``
    refuses it."""
    body = " ".join(s.pub.split()[:2])
    (s.trust / trust.REVOKED_FILE).write_text(f"{body}\n")


def _hand_edit_dockerfile(s: AdmissionSet) -> None:
    """The Dockerfile is edited after authoring; the set is intact."""
    (s.source / ARTIFACT).write_text(DOCKERFILE + "RUN echo edited\n")


def _foreign_repo_resigned(s: AdmissionSet) -> None:
    """A validly signed record for another repository."""
    s.resign({**s.load(RECORD_FILE), "repo": "x/y"})


def _foreign_path_resigned(s: AdmissionSet) -> None:
    """A validly signed record for another artifact path."""
    s.resign({**s.load(RECORD_FILE), "artifact_path": "docker/Dockerfile"})


def _snapshot_edited(s: AdmissionSet) -> None:
    """The snapshot is reformatted, still parseable; record unchanged."""
    snapshot = s.load(SNAPSHOT_FILE)
    (s.set_dir / SNAPSHOT_FILE).write_text(json.dumps(snapshot, indent=2))


def _source_commit_differs(s: AdmissionSet) -> None:
    """Snapshot and record name different source commits."""
    s.resign_snapshot(lambda snap: snap.update(source_commit="0" * 40))


def _tree_incomplete(s: AdmissionSet) -> None:
    """The snapshot marks its tree listing incomplete."""
    s.resign_snapshot(lambda snap: snap.update(tree_complete=False))


def _row_missing_field(s: AdmissionSet) -> None:
    """A tree row lacks its ``mode`` field."""
    s.resign_snapshot(lambda snap: snap["tree"][0].pop("mode"))


def _row_non_string_field(s: AdmissionSet) -> None:
    """A tree row carries a non-string ``mode``."""
    s.resign_snapshot(lambda snap: snap["tree"][0].update(mode=100644))


def _duplicate_path(s: AdmissionSet) -> None:
    """The tree listing names one path twice."""
    s.resign_snapshot(lambda snap: snap["tree"].append(dict(snap["tree"][0])))


_MUTATIONS: dict[str, Callable[[AdmissionSet], None]] = {
    "remove_pointer": _remove_pointer,
    "pointer_to_missing_dir": _pointer_to_missing_dir,
    "record_unknown_format": _record_unknown_format,
    "snapshot_unknown_format": _snapshot_unknown_format,
    "rename_dir": _rename_dir,
    "bad_signature": _bad_signature,
    "unknown_key": _unknown_key,
    "revoked_and_removed_key": _revoked_and_removed,
    "revoked_only_key": _revoked_only,
    "hand_edit_dockerfile": _hand_edit_dockerfile,
    "foreign_repo_resigned": _foreign_repo_resigned,
    "foreign_path_resigned": _foreign_path_resigned,
    "snapshot_edited": _snapshot_edited,
    "source_commit_differs_resigned": _source_commit_differs,
    "tree_incomplete_resigned": _tree_incomplete,
    "row_missing_field_resigned": _row_missing_field,
    "row_non_string_field_resigned": _row_non_string_field,
    "duplicate_path_resigned": _duplicate_path,
}


@pytest.fixture()
def admission_set(tmp_path: Path) -> AdmissionSet:
    """A freshly issued, verifiable set in a restored tree."""
    return AdmissionSet(tmp_path)


@dataclass(frozen=True)
class Replayed:
    """R's attempted try of one bundle under ``root``."""

    bundle: Path
    run: FailedRun
    section: ReproductionSection
    root: Path

    @property
    def try_dir(self) -> Path:
        """The try directory R wrote."""
        assert self.section.try_dir is not None
        return self.root / self.section.try_dir

    @property
    def attempt_dir(self) -> Path:
        """The attempt directory holding ``source/`` and ``source.json``."""
        return self.try_dir.parent.parent

    @property
    def source(self) -> Path:
        """The tree R restored at ``head_sha``."""
        return self.attempt_dir / "source"

    def unlock(self) -> Path:
        """Make R's read-only ``source/`` writable for a targeted mutation."""
        restore_owner_write(self.source)
        return self.source


@pytest.fixture()
def fake_containers(monkeypatch: pytest.MonkeyPatch) -> FakeContainers:
    """R's container calls answered by a fake, as in R §8.A."""
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    return fake


def replay_case(
    case: str, tmp_path: Path, fake: FakeContainers, bundles: Path = BUNDLES
) -> Replayed:
    """Replay ``case`` (a bundle under ``bundles``, R's reproduction bundles
    by default) through R under ``tmp_path / "work"``."""
    bundle = bundles / case
    rt, env = _containers(bundle, fake)
    run = load_snapshot((bundle / "snapshot.json").read_text())
    root = tmp_path / "work"
    section = reproduce_run(
        run,
        gh=BundleGh(bundle, tmp_path / "bundle"),
        rt=rt,
        runtime_error=None,
        env=env,
        root=root,
        build_timeout=60,
    )
    assert section.status == "attempted"
    return Replayed(bundle, run, section, root)
