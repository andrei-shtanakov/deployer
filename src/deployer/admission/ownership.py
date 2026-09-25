"""Condition (1), ownership: verify an authoring set in the restored tree
(A §2.4 steps 0-6). Preparation layer: file I/O and ssh-keygen only, no
network, no builds.

Every file under the checked tree is read through a component-wise
no-follow walk: a committed symlink anywhere on the path, or a non-regular
file at its end, is refused rather than followed out of the tree.
"""

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from deployer.provenance import sshsig
from deployer.provenance.model import (
    FORMAT_VERSION,
    POINTER,
    RECORD_FILE,
    SET_PARENT,
    SET_ROOT,
    SIGNATURE_FILE,
    SNAPSHOT_FILE,
    Record,
    Snapshot,
    set_dir_name,
    sha256_hex,
)
from deployer.provenance.sshsig import SshSigError
from deployer.provenance.trust import ALLOWED_FILE, REVOKED_FILE, outside

_POINTER_RE = re.compile(rf"{SET_PARENT}/([0-9a-f]{{64}})\n?")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_SMALL = 1024 * 1024  # cap for the pointer, the record and the signature


@dataclass(frozen=True)
class OwnershipFacts:
    """The outcome of A §2.4. ``step``/``reason`` name the first failure;
    the hashes are present once the set files were read, the fingerprint and
    ``record`` once the signature verified, ``snapshot`` only when
    confirmed."""

    status: Literal["confirmed", "not_confirmed"]
    step: int | None
    reason: str | None
    key_fingerprint: str | None
    record_sha256: str | None
    snapshot_sha256: str | None
    record: Record | None
    snapshot: Snapshot | None


@dataclass(frozen=True)
class _SetFiles:
    """The published set as read and parsed at step 1."""

    dir_name: str
    record_bytes: bytes
    snapshot_bytes: bytes
    signature: bytes
    record: Record


def verify_ownership(
    source_dir: Path,
    *,
    repo: str,
    artifact_path: str,
    trust: Path,
    checked_roots: tuple[Path, ...],
) -> OwnershipFacts:
    """Run A §2.4 steps 0-6 in order over ``source_dir`` (the tree restored
    at ``head_sha``); the first failing step ends the check. Never raises."""
    reason = _step0_trust(trust, checked_roots)
    if reason is not None:
        return _refused(0, reason)
    files = _step1_parse(source_dir)
    if isinstance(files, str):
        return _refused(1, files)
    hashes = (sha256_hex(files.record_bytes), sha256_hex(files.snapshot_bytes))
    reason = _step2_dir_name(files)
    if reason is not None:
        return _refused(2, reason, hashes)
    verified = _step3_signature(files, trust)
    if not verified.ok or verified.fingerprint is None:
        return _refused(3, verified.reason or "signature not verified", hashes)
    signed = (verified.fingerprint, files.record)
    reason = _step4_artifact(source_dir, files.record, repo, artifact_path)
    if reason is not None:
        return _refused(4, reason, hashes, signed)
    reason = _step5_snapshot_hash(files)
    if reason is not None:
        return _refused(5, reason, hashes, signed)
    snapshot = _step6_snapshot(files)
    if isinstance(snapshot, str):
        return _refused(6, snapshot, hashes, signed)
    return OwnershipFacts(
        "confirmed", None, None, verified.fingerprint, *hashes, files.record, snapshot
    )


def _refused(
    step: int,
    reason: str,
    hashes: tuple[str, str] | None = None,
    signed: tuple[str, Record] | None = None,
) -> OwnershipFacts:
    """``not_confirmed`` at ``step``, carrying only the values obtained."""
    record_sha, snapshot_sha = hashes if hashes is not None else (None, None)
    fingerprint, record = signed if signed is not None else (None, None)
    return OwnershipFacts(
        "not_confirmed",
        step,
        reason,
        fingerprint,
        record_sha,
        snapshot_sha,
        record,
        None,
    )


def _step0_trust(trust: Path, checked_roots: tuple[Path, ...]) -> str | None:
    """The trust dir and each trust file ssh-keygen will read resolve
    outside every checked root, and an allowed_signers file exists."""
    if not checked_roots:
        return "no checked roots to place the trust directory against"
    try:
        for path in _trust_paths(trust):
            reason = outside(path, *checked_roots)
            if reason is not None:
                return reason
        if not (trust / ALLOWED_FILE).is_file():
            return f"no {ALLOWED_FILE} file in trust directory {trust}"
    except (OSError, RuntimeError, ValueError) as exc:  # ValueError: a NUL
        return f"trust directory {trust} could not be resolved: {exc}"
    return None


def _trust_paths(trust: Path) -> list[Path]:
    """The trust dir and the files ssh-keygen follows links through: the
    allowed signers, and the revocation list whenever an entry exists."""
    paths = [trust, trust / ALLOWED_FILE]
    if os.path.lexists(trust / REVOKED_FILE):
        paths.append(trust / REVOKED_FILE)
    return paths


def _step1_parse(source_dir: Path) -> _SetFiles | str:
    """The pointer names ``Dockerfile/<hex64>``; that directory holds the
    three files; the record validates and the snapshot is a JSON object of
    a supported ``format_version`` (its structure is step 6's)."""
    try:
        pointer = _read_in_tree(source_dir, f"{SET_ROOT}/{POINTER}", _SMALL)
        match = _POINTER_RE.fullmatch(pointer.decode("utf-8", errors="replace"))
        if match is None:
            return f"{POINTER} does not name {SET_PARENT}/<sha256>"
        base = f"{SET_ROOT}/{set_dir_name(match.group(1))}"
        rec_bytes = _read_in_tree(source_dir, f"{base}/{RECORD_FILE}", _SMALL)
        snap_bytes = _read_in_tree(source_dir, f"{base}/{SNAPSHOT_FILE}")
        sig = _read_in_tree(source_dir, f"{base}/{SIGNATURE_FILE}", _SMALL)
        _supported_object(RECORD_FILE, rec_bytes)
        _supported_object(SNAPSHOT_FILE, snap_bytes)
        record = Record.model_validate_json(rec_bytes)
    except (OSError, ValueError) as exc:  # ValidationError is a ValueError
        return _describe(exc)
    return _SetFiles(match.group(1), rec_bytes, snap_bytes, sig, record)


def _supported_object(name: str, data: bytes) -> None:
    """``data`` is a JSON object with the supported ``format_version``;
    raises ``ValueError`` naming the file otherwise."""
    try:
        parsed = json.loads(data)
    except (ValueError, RecursionError) as exc:  # hostile nesting recurses
        raise ValueError(f"{name} does not parse as JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} is not a JSON object")
    version = parsed.get("format_version")
    if version != FORMAT_VERSION:
        raise ValueError(f"{name} format_version {version!r} is not supported")


def _describe(exc: Exception) -> str:
    """A one-line reason for a read or parse failure."""
    if isinstance(exc, ValidationError):
        return f"{RECORD_FILE} is not a valid record: {_first_error(exc)}"
    return str(exc)


def _first_error(exc: ValidationError) -> str:
    """The first validation error as ``location: message``."""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"]) or "<root>"
    return f"{location}: {first['msg']}"


def _step2_dir_name(files: _SetFiles) -> str | None:
    """The directory name is the SHA-256 of the record's bytes."""
    actual = sha256_hex(files.record_bytes)
    if actual != files.dir_name:
        return f"set directory {files.dir_name} is not the record's hash {actual}"
    return None


def _step3_signature(files: _SetFiles, trust: Path) -> sshsig.Verified:
    """The signature verifies against the trust set, revocations applied."""
    revoked = trust / REVOKED_FILE
    try:
        return sshsig.verify(
            files.record_bytes,
            files.signature,
            trust / ALLOWED_FILE,
            revoked if os.path.lexists(revoked) else None,
        )
    except (OSError, ValueError, SshSigError) as exc:
        return sshsig.Verified(False, None, f"signature not verified: {exc}")


def _step4_artifact(
    source_dir: Path, record: Record, repo: str, artifact_path: str
) -> str | None:
    """The record covers exactly the artifact bytes at ``head_sha``, for the
    run's repository and path."""
    try:
        artifact = _read_in_tree(source_dir, artifact_path)
    except (OSError, ValueError) as exc:  # ValueError: not encodable for the OS
        return f"artifact {exc}"
    actual = sha256_hex(artifact)
    if record.artifact_sha256 != actual:
        return (
            f"artifact_sha256 {record.artifact_sha256} differs from "
            f"{artifact_path} at head ({actual})"
        )
    if record.repo != repo:
        return f"record repo {record.repo!r} is not the run's {repo!r}"
    if record.artifact_path != artifact_path:
        return (
            f"record artifact_path {record.artifact_path!r} is not the run's "
            f"{artifact_path!r}"
        )
    return None


def _step5_snapshot_hash(files: _SetFiles) -> str | None:
    """The snapshot's bytes are the ones the record signed."""
    actual = sha256_hex(files.snapshot_bytes)
    if files.record.snapshot_sha256 != actual:
        return (
            f"snapshot_sha256 {files.record.snapshot_sha256} differs from "
            f"{SNAPSHOT_FILE} ({actual})"
        )
    return None


def _step6_snapshot(files: _SetFiles) -> Snapshot | str:
    """The snapshot is well formed and consistent with the record; hash
    equality does not replace these checks."""
    try:
        snapshot = Snapshot.model_validate_json(files.snapshot_bytes)
    except ValidationError as exc:
        return f"{SNAPSHOT_FILE} is not a well-formed snapshot: {_first_error(exc)}"
    if not snapshot.tree_complete:
        return f"{SNAPSHOT_FILE} tree listing is not complete"
    paths = [row.path for row in snapshot.tree]
    if len(paths) != len(set(paths)):
        return f"{SNAPSHOT_FILE} tree listing has duplicate paths"
    if snapshot.source_commit != files.record.source_commit:
        return (
            f"source_commit differs: record {files.record.source_commit}, "
            f"snapshot {snapshot.source_commit}"
        )
    return snapshot


class _Unreadable(OSError):
    """A path in the checked tree that is not read: missing, not a plain
    relative path, behind a symlink, or not a regular file."""


def _read_in_tree(root: Path, rel: str, max_bytes: int | None = None) -> bytes:
    """The bytes of regular file ``rel`` under ``root``, opened one
    component at a time without following any symlink; a file longer than
    ``max_bytes`` is refused."""
    if "\x00" in rel:
        raise _Unreadable(f"{rel!r} contains a NUL byte")
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _Unreadable(f"{rel} is not a plain relative path")
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for depth, name in enumerate(parts[:-1], start=1):
            sub = "/".join(parts[:depth])
            next_fd = _open_at(dir_fd, name, _DIR_FLAGS, sub, "a directory")
            os.close(dir_fd)
            dir_fd = next_fd
        fd = _open_at(dir_fd, parts[-1], _FILE_FLAGS, rel, "a regular file")
    finally:
        os.close(dir_fd)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise _Unreadable(f"{rel} is not a regular file; not read")
    with os.fdopen(fd, "rb") as f:
        data = f.read() if max_bytes is None else f.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise _Unreadable(f"{rel} exceeds {max_bytes} bytes; not read")
    return data


def _open_at(dir_fd: int, name: str, flags: int, rel: str, kind: str) -> int:
    """``openat`` with no-follow flags, mapping refusals to ``_Unreadable``."""
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise _Unreadable(f"{rel} is missing") from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _Unreadable(
                f"{rel} is a symlink or not {kind}; not followed"
            ) from None
        raise
