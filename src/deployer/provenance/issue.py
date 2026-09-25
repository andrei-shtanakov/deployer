"""Issuing a provenance set (A §5.2): preflight, then publish behind a
pointer.

``preflight`` runs before authoring writes anything and gates on the repo
state; ``issue`` runs after authoring wrote the Dockerfile and publishes an
immutable, signed, excluded set, then atomically repoints
``Dockerfile.current`` at it. ``withdraw`` removes a published set.
"""

import errno
import fcntl
import os
import shutil
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from deployer.facts import analyze_project
from deployer.models import ProjectFacts
from deployer.provenance import gitrepo, sshsig
from deployer.provenance.gitrepo import GitError
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
    TreeRow,
    canonical_bytes,
    set_dir_name,
    sha256_hex,
)
from deployer.provenance.sshsig import SshSigError
from deployer.reproduce import ignore

_ARTIFACT_PATH = "Dockerfile"
_LOCK_FILE_NAME = "deployer-authoring.lock"


@dataclass(frozen=True)
class Preflight:
    """A repo cleared to have a set issued for it; facts are already proven
    to match the committed source."""

    project: Path
    repo: str
    source_commit: str
    tree: list[TreeRow]
    facts: ProjectFacts


@dataclass(frozen=True)
class Issued:
    """The outcome of publishing a set; ``reason`` and ``set_dir`` are
    ``None`` exactly when the other is meaningful."""

    published: bool
    reason: str | None
    set_dir: str | None


def _facts_from_commit(project: Path, commit: str) -> ProjectFacts:
    """Facts of ``commit``'s committed tree, via a throwaway export."""
    with tempfile.TemporaryDirectory() as tmp:
        gitrepo.export_commit(project, commit, Path(tmp))
        return analyze_project(Path(tmp))


def preflight(project: Path, signing_key: Path | None) -> Preflight | str:
    """Gate authoring before any write; a ``str`` result names why no set
    will be issued."""
    if not gitrepo.is_checkout(project):
        return f"{project} is not a Git checkout"
    try:
        top = gitrepo.toplevel(project)
    except GitError as exc:
        return str(exc)
    if project.resolve() != top.resolve():
        return f"{project} is not the repository root ({top})"
    repo = gitrepo.origin_slug(project)
    if repo is None:
        return f"{project} has no origin remote"
    try:
        dirty = gitrepo.dirty_paths(project)
    except GitError as exc:
        return str(exc)
    if dirty:
        return "working tree is dirty: " + ", ".join(dirty)
    if signing_key is None:
        return "no signing key given"
    try:
        sshsig.public_key(signing_key)
    except SshSigError as exc:
        return str(exc)
    try:
        commit = gitrepo.head_commit(project)
        tree = gitrepo.tree_listing(project, commit)
        committed_facts = _facts_from_commit(project, commit)
    except GitError as exc:
        return str(exc)
    working_facts = analyze_project(project)
    if committed_facts != working_facts:
        return (
            "facts of the working tree differ from the facts of "
            f"{commit}: a fact depends on an ignored or uncommitted file"
        )
    return Preflight(project, repo, commit, tree, committed_facts)


def _build(
    pre: Preflight, dockerfile_bytes: bytes, deployer_version: str
) -> tuple[bytes, bytes, str]:
    """The canonical snapshot and record bytes, and the record's own hash."""
    snapshot = Snapshot(
        format_version=FORMAT_VERSION,
        source_commit=pre.source_commit,
        tree=pre.tree,
        tree_complete=True,
        facts=pre.facts,
    )
    snap_bytes = canonical_bytes(snapshot)
    record = Record(
        format_version=FORMAT_VERSION,
        repo=pre.repo,
        artifact_path=_ARTIFACT_PATH,
        artifact_sha256=sha256_hex(dockerfile_bytes),
        source_commit=pre.source_commit,
        snapshot_sha256=sha256_hex(snap_bytes),
        deployer_version=deployer_version,
    )
    rec_bytes = canonical_bytes(record)
    return snap_bytes, rec_bytes, sha256_hex(rec_bytes)


class _PathRefusal(OSError):
    """A provenance path that must not be followed or written through: a
    symlink, a non-directory component, or a non-regular file."""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_CHAIN = (*SET_ROOT.split("/"), SET_PARENT)


@contextmanager
def _root_fd(project: Path) -> Generator[int]:
    """The project root, opened once; every provenance operation is
    relative to it. The root itself is user-chosen, so it may be a link."""
    fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _open_component(parent_fd: int, name: str, rel: str, create: bool) -> int | None:
    """Open directory ``name`` under ``parent_fd`` without following a
    symlink, creating it first when ``create``; ``None`` if it is missing
    and may not be created. A symlink raises ``_PathRefusal`` naming
    ``rel``; so does any non-directory when ``create``, while without it a
    plain file counts as missing (nothing provenance-shaped lives there)."""
    if create:
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if create:
            raise
        return None
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        if not create and not _is_symlink_at(parent_fd, name):
            return None  # a plain file: no provenance below it to remove
        raise _PathRefusal(
            f"{rel} is a symlink or not a directory; provenance not touched"
        ) from exc


def _is_symlink_at(dir_fd: int, name: str) -> bool:
    """Whether ``name`` under ``dir_fd`` is a symlink (``lstat``-style)."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


@contextmanager
def _open_chain(root_fd: int, create: bool) -> Generator[list[int]]:
    """Walk ``.deployer`` -> ``authoring`` -> ``Dockerfile`` one component
    at a time; the fds opened, in order, all closed on exit.

    Without ``create`` the walk stops at the first missing component, so
    the list may be shorter than the chain. Held fds pin the real
    directories: a later swap of any path component for a symlink cannot
    redirect an operation made relative to them.
    """
    fds: list[int] = []
    try:
        parent = root_fd
        for depth, name in enumerate(_CHAIN, start=1):
            rel = "/".join(_CHAIN[:depth])
            fd = _open_component(parent, name, rel, create)
            if fd is None:
                break
            fds.append(fd)
            parent = fd
        yield fds
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _open_regular(dir_fd: int, name: str, flags: int) -> int:
    """Open file ``name`` under ``dir_fd`` never following a final symlink;
    anything but a regular file raises ``_PathRefusal``."""
    try:
        fd = os.open(name, flags | _FILE_FLAGS, 0o644, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _PathRefusal(f"{name} is a symlink; not followed") from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _PathRefusal(f"{name} is not a regular file; not followed")
    return fd


def _read_at(dir_fd: int, name: str) -> bytes:
    """The bytes of regular file ``name`` under ``dir_fd``, read through a
    no-follow fd."""
    with os.fdopen(_open_regular(dir_fd, name, os.O_RDONLY), "rb") as f:
        return f.read()


def _write_new_at(dir_fd: int, name: str, data: bytes) -> None:
    """Create ``name`` under ``dir_fd`` exclusively (never through an
    existing entry or symlink) and write ``data``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(_open_regular(dir_fd, name, flags), "wb") as f:
        f.write(data)


def _append_at(dir_fd: int, name: str, text: str, create: bool) -> None:
    """Append ``text`` to regular file ``name`` under ``dir_fd`` through a
    no-follow fd, creating it when ``create``."""
    flags = os.O_WRONLY | os.O_APPEND | (os.O_CREAT if create else 0)
    with os.fdopen(_open_regular(dir_fd, name, flags), "a") as f:
        f.write(text)


def _entry_exists(dir_fd: int, name: str) -> bool:
    """Whether ``name`` exists under ``dir_fd``, a symlink included."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _ignore_file_refusal(root_fd: int, files: tuple[str, ...]) -> str | None:
    """A reason if an ignore file is not a plain root-level entry.

    Writes are fd-anchored and can never go through a symlink; this check
    exists for the exclusion *proof*, which reads by path: a symlinked
    ignore file would prove exclusion from a file that is not the one in
    the build context.
    """
    for name in files:
        if "/" in name:
            return f"{name} is not at the project root; not modified"
        try:
            st = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return f"{name} is a symlink; provenance not touched"
    return None


def _ensure_pattern(
    project: Path, root_fd: int, file: str | None, create: bool
) -> None:
    """Append ``.deployer/`` to ``file``, or create it, unless some rule in
    it already excludes ``.deployer/``; every read and write goes through
    a no-follow fd relative to the project root."""
    if file is None:
        if create:
            _append_at(root_fd, ".dockerignore", ".deployer/\n", create=True)
        return
    rules = ignore.load_rules(project, file)
    if ignore.excluded_by(rules, ".deployer") is not None:
        return
    text = _read_at(root_fd, file).decode(errors="replace")
    sep = "" if text == "" or text.endswith("\n") else "\n"
    _append_at(root_fd, file, f"{sep}.deployer/\n", create=False)


def _refusal_for_unsupported(project: Path, file: str | None) -> str | None:
    """A refusal reason if ``file``'s current rules have an unsupported
    pattern; ``None`` if ``file`` doesn't exist yet or has none."""
    if file is None:
        return None
    rules = ignore.load_rules(project, file)
    if rules.unsupported is not None:
        return f"exclusion not provable: unsupported pattern in {file}"
    return None


def ensure_excluded(project: Path, paths: list[str]) -> str | None:
    """Prove every path in ``paths`` is excluded from both build contexts;
    the reason it could not be proven, or ``None`` once it is."""
    with _root_fd(project) as root_fd:
        try:
            return _ensure_excluded_at(project, root_fd, paths)
        except _PathRefusal as exc:
            return str(exc)


def _ensure_excluded_at(project: Path, root_fd: int, paths: list[str]) -> str | None:
    """``ensure_excluded`` with ignore-file writes anchored at ``root_fd``."""
    ci_file = ignore.ci_ignore_file(project, _ARTIFACT_PATH)
    local_file = ignore.local_ignore_file(project, _ARTIFACT_PATH, "podman")
    ignore_files = tuple(
        f for f in (ci_file or ".dockerignore", ".containerignore") if f is not None
    )
    reason = _ignore_file_refusal(root_fd, ignore_files)
    if reason is not None:
        return reason
    for file in (ci_file, local_file):
        reason = _refusal_for_unsupported(project, file)
        if reason is not None:
            return reason
    _ensure_pattern(project, root_fd, ci_file, True)
    if (project / ".containerignore").is_file():
        _ensure_pattern(project, root_fd, ".containerignore", False)
    for file in (
        ignore.ci_ignore_file(project, _ARTIFACT_PATH),
        ignore.local_ignore_file(project, _ARTIFACT_PATH, "podman"),
    ):
        rules = ignore.load_rules(project, file)
        if rules.unsupported is not None:
            return f"exclusion not provable: unsupported pattern in {file}"
        for path in paths:
            if ignore.excluded_by(rules, path) is None:
                return f"exclusion not provable: {path} is not excluded by {file}"
    return None


def _check_reuse(
    parent_fd: int, name: str, rec_bytes: bytes, snap_bytes: bytes, signing_key: Path
) -> str | None:
    """``None`` if the existing set ``name`` under ``parent_fd`` matches
    exactly, else why it does not.

    A missing file, a non-directory or symlinked ``name``, or any other
    read failure all mean the same thing here: the existing set cannot be
    trusted as a match, so it is refused rather than raising.
    """
    reason = f"existing set {name} does not match; not written"
    try:
        set_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError:
        return reason
    try:
        matches = _read_at(set_fd, RECORD_FILE) == rec_bytes and (
            _read_at(set_fd, SNAPSHOT_FILE) == snap_bytes
        )
        if matches:
            existing_sig = _read_at(set_fd, SIGNATURE_FILE)
            pub = sshsig.public_key(signing_key)
            matches = sshsig.verify_with_public_key(rec_bytes, existing_sig, pub).ok
    except (OSError, SshSigError):
        matches = False
    finally:
        os.close(set_fd)
    return None if matches else reason


def _write_set(
    parent_fd: int, name: str, rec_bytes: bytes, snap_bytes: bytes, sig: bytes
) -> None:
    """Write the three set files into a temp dir under ``parent_fd``, then
    atomically rename it to ``name`` — all relative to the held fds."""
    tmp_name = f".tmp-{name}-{os.getpid()}"
    os.mkdir(tmp_name, 0o755, dir_fd=parent_fd)
    tmp_fd = os.open(tmp_name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        _write_new_at(tmp_fd, RECORD_FILE, rec_bytes)
        _write_new_at(tmp_fd, SNAPSHOT_FILE, snap_bytes)
        _write_new_at(tmp_fd, SIGNATURE_FILE, sig)
    finally:
        os.close(tmp_fd)
    os.rename(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _replace_pointer(auth_fd: int, rec_sha: str) -> None:
    """Atomically point ``Dockerfile.current`` (under ``auth_fd``) at the
    given set, via ``renameat`` of an exclusively created temp file.

    On failure the temp pointer file is unlinked on a best-effort basis
    before the error is re-raised, so a crashed run leaves no stray
    ``.tmp-pointer-*`` file behind alongside the untouched old pointer.
    """
    tmp = f".tmp-pointer-{os.getpid()}"
    _unlink_quietly(auth_fd, tmp)  # a leftover of a crashed run with our pid
    _write_new_at(auth_fd, tmp, (set_dir_name(rec_sha) + "\n").encode())
    try:
        os.rename(tmp, POINTER, src_dir_fd=auth_fd, dst_dir_fd=auth_fd)
    except OSError:
        _unlink_quietly(auth_fd, tmp)
        raise


def _unlink_quietly(dir_fd: int, name: str) -> None:
    """Best-effort ``unlinkat``; never follows, never raises."""
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _prunable_names(parent_fd: int, keep: str) -> list[str]:
    """Finished set directories under ``parent_fd`` safe to remove: real
    directories (not symlinks), not ``keep``, and not another run's
    in-progress ``.tmp-*`` staging directory."""
    with os.scandir(parent_fd) as entries:
        return [
            e.name
            for e in entries
            if e.is_dir(follow_symlinks=False)
            and e.name != keep
            and not e.name.startswith(".tmp-")
        ]


def _prune_other_sets(parent_fd: int, keep: str) -> bool:
    """Remove every set directory under ``parent_fd`` except ``keep``;
    whether any was removed.

    Non-directory entries and any live ``.tmp-*`` staging directory (another
    run in flight) are left alone; a leftover crashed-run tmp dir is
    harmless since it is already excluded under ``.deployer/``. Removal is
    ``rmtree(name, dir_fd=parent_fd)`` (symlink-attack-safe), so it stays
    inside the held directory. A real removal failure is left to raise.
    """
    names = _prunable_names(parent_fd, keep)
    for name in names:
        shutil.rmtree(name, dir_fd=parent_fd)
    return bool(names)


def _acquire_publication_lock(project: Path) -> int:
    """Take this repository's exclusive advisory publication lock; its fd.

    Serializes the publish/withdraw sequence (existence/reuse check, set
    write, pointer swap, prune) across concurrent ``issue()``/``withdraw()``
    calls for the same repository. The lock file lives at
    ``.git/deployer-authoring.lock`` (via ``gitrepo.git_path``), so it never
    appears in the work tree or in ``dirty_paths``. Raises ``GitError`` or
    ``OSError`` when the lock cannot be taken. The lock file is never
    written; ``O_NOFOLLOW`` keeps a symlink at its name from creating a
    file elsewhere (the ``.git`` location itself is Git's to resolve).

    POSIX-only (``fcntl.flock``); this project targets macOS and Linux,
    not Windows, so no Windows locking path is provided.
    """
    lock_path = gitrepo.git_path(project, _LOCK_FILE_NAME)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd


def _release_publication_lock(fd: int) -> None:
    """Release and close a lock taken by ``_acquire_publication_lock``."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _current_artifact_sha(root_fd: int) -> str | None:
    """The hash of the Dockerfile on disk now, or ``None`` if unreadable or
    not a regular file (a symlink is never this run's output)."""
    try:
        return sha256_hex(_read_at(root_fd, _ARTIFACT_PATH))
    except OSError:
        return None


def _publish(
    project: Path,
    rec_sha: str,
    rec_bytes: bytes,
    snap_bytes: bytes,
    sig: bytes,
    signing_key: Path,
    artifact_sha: str,
) -> Issued:
    """Write or reuse the set directory, then atomically move the pointer.

    The whole sequence — artifact re-check, existence/reuse check,
    write-or-reuse, pointer swap and prune — runs under the publication
    lock, so a concurrent ``issue()`` for the same repository cannot
    interleave with this one. Under the lock the Dockerfile on disk is
    hashed again: if another authoring run rewrote it after this run read
    it, this run is stale and publishes nothing. Concurrent authoring of
    one repository can therefore end with no confirmation, never with a
    pointer naming a set for bytes that are not on disk. A failure to
    acquire the lock itself becomes ``Issued(False, reason, None)``; a
    failure once the lock is held propagates as before.

    Every write is relative to fds from one no-follow walk of the
    provenance chain, so swapping a path component mid-run cannot redirect
    it outside the project.
    """
    try:
        fd = _acquire_publication_lock(project)
    except (GitError, OSError) as exc:
        return Issued(False, f"could not acquire the publication lock: {exc}", None)
    try:
        with _root_fd(project) as root_fd:
            if _current_artifact_sha(root_fd) != artifact_sha:
                return Issued(
                    False,
                    "Dockerfile on disk is not this run's output; not published",
                    None,
                )
            try:
                with _open_chain(root_fd, create=True) as fds:
                    return _publish_at(
                        fds, rec_sha, rec_bytes, snap_bytes, sig, signing_key
                    )
            except _PathRefusal as exc:
                return Issued(False, str(exc), None)
    finally:
        _release_publication_lock(fd)


def _publish_at(
    fds: list[int],
    rec_sha: str,
    rec_bytes: bytes,
    snap_bytes: bytes,
    sig: bytes,
    signing_key: Path,
) -> Issued:
    """Reuse or write the set, repoint and prune, relative to the chain."""
    _, auth_fd, parent_fd = fds
    if _entry_exists(parent_fd, rec_sha):
        reason = _check_reuse(parent_fd, rec_sha, rec_bytes, snap_bytes, signing_key)
        if reason is not None:
            return Issued(False, reason, None)
    else:
        _write_set(parent_fd, rec_sha, rec_bytes, snap_bytes, sig)
    _replace_pointer(auth_fd, rec_sha)
    _prune_other_sets(parent_fd, rec_sha)
    return Issued(True, None, set_dir_name(rec_sha))


def issue(
    pre: Preflight, signing_key: Path, deployer_version: str, dockerfile: bytes
) -> Issued:
    """Publish a signed, excluded, immutable provenance set for the
    Dockerfile authoring just wrote, then atomically repoint
    ``Dockerfile.current`` at it.

    ``dockerfile`` is the exact bytes this authoring run wrote. The record
    is built from them, never from a re-read of the file, and publication
    refuses (under the lock) unless the file on disk still holds exactly
    these bytes: only this run's own output is ever signed.
    """
    dockerfile_bytes = dockerfile
    snap_bytes, rec_bytes, rec_sha = _build(pre, dockerfile_bytes, deployer_version)
    paths = [f"{SET_ROOT}/{POINTER}"] + [
        f"{SET_ROOT}/{set_dir_name(rec_sha)}/{name}"
        for name in (RECORD_FILE, SNAPSHOT_FILE, SIGNATURE_FILE)
    ]
    try:
        reason = ensure_excluded(pre.project, paths)
    except OSError as exc:
        reason = f"exclusion could not be written: {exc}"
    if reason is not None:
        return Issued(False, reason, None)
    try:
        sig = sshsig.sign(rec_bytes, signing_key)
    except SshSigError as exc:
        return Issued(False, str(exc), None)
    return _publish(
        pre.project,
        rec_sha,
        rec_bytes,
        snap_bytes,
        sig,
        signing_key,
        sha256_hex(dockerfile_bytes),
    )


def withdraw(project: Path) -> bool:
    """Remove ``Dockerfile.current`` first, then every set directory under
    ``SET_ROOT/Dockerfile/``; return whether anything was removed.

    Runs under the publication lock like ``_publish`` when the lock can be
    taken. When it cannot, the removal still happens, unlocked: removing a
    confirmation can only ever lose one, never create a false one, and
    spec §5.3 forbids a normally completed run from leaving an old one
    behind. A removal that itself fails raises ``OSError`` so the caller
    can report it instead of completing as if nothing were left; so does
    a symlinked or non-directory component of the provenance chain.

    Removals are relative to fds from one no-follow walk of the chain; a
    missing component means nothing below it to remove. Like
    ``_prune_other_sets``, this leaves non-directory entries and any live
    ``.tmp-*`` staging directory alone.
    """
    try:
        fd: int | None = _acquire_publication_lock(project)
    except (GitError, OSError):
        fd = None
    try:
        with _root_fd(project) as root_fd, _open_chain(root_fd, create=False) as fds:
            return _withdraw_at(fds)
    finally:
        if fd is not None:
            _release_publication_lock(fd)


def _withdraw_at(fds: list[int]) -> bool:
    """Unlink the pointer, then remove the set dirs, relative to ``fds``."""
    removed = False
    if len(fds) >= 2:
        try:
            os.unlink(POINTER, dir_fd=fds[1])
            removed = True
        except FileNotFoundError:
            pass
    if len(fds) == 3:
        removed = _prune_other_sets(fds[2], keep="") or removed
    return removed
