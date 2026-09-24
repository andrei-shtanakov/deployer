"""Issuing a provenance set (A §5.2): preflight, then publish behind a
pointer.

``preflight`` runs before authoring writes anything and gates on the repo
state; ``issue`` runs after authoring wrote the Dockerfile and publishes an
immutable, signed, excluded set, then atomically repoints
``Dockerfile.current`` at it. ``withdraw`` removes a published set.
"""

import os
import shutil
import tempfile
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
    repo = gitrepo.origin_slug(project)
    if repo is None:
        return f"{project} has no origin remote"
    dirty = gitrepo.dirty_paths(project)
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


def _ensure_pattern(project: Path, file: str | None, create: bool) -> None:
    """Append ``.deployer/`` to ``file``, or create it, unless some rule in
    it already excludes ``.deployer/``."""
    if file is None:
        if create:
            (project / ".dockerignore").write_text(".deployer/\n")
        return
    rules = ignore.load_rules(project, file)
    if ignore.excluded_by(rules, ".deployer") is not None:
        return
    path = project / file
    text = path.read_text() if path.is_file() else ""
    sep = "" if text == "" or text.endswith("\n") else "\n"
    with path.open("a") as f:
        f.write(f"{sep}.deployer/\n")


def ensure_excluded(project: Path, paths: list[str]) -> str | None:
    """Prove every path in ``paths`` is excluded from both build contexts;
    the reason it could not be proven, or ``None`` once it is."""
    _ensure_pattern(project, ignore.ci_ignore_file(project, _ARTIFACT_PATH), True)
    if (project / ".containerignore").is_file():
        _ensure_pattern(project, ".containerignore", False)
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
    target: Path, rec_bytes: bytes, snap_bytes: bytes, signing_key: Path
) -> str | None:
    """``None`` if the existing set matches exactly, else why it does not."""
    matches = (target / RECORD_FILE).read_bytes() == rec_bytes and (
        target / SNAPSHOT_FILE
    ).read_bytes() == snap_bytes
    if matches:
        existing_sig = (target / SIGNATURE_FILE).read_bytes()
        pub = sshsig.public_key(signing_key)
        matches = sshsig.verify_with_public_key(rec_bytes, existing_sig, pub).ok
    if matches:
        return None
    return f"existing set {target.name} does not match; not written"


def _write_set(target: Path, rec_bytes: bytes, snap_bytes: bytes, sig: bytes) -> None:
    """Write the three set files under a temp dir, then atomically rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = target.parent / f".tmp-{target.name}-{os.getpid()}"
    tmp_dir.mkdir()
    (tmp_dir / RECORD_FILE).write_bytes(rec_bytes)
    (tmp_dir / SNAPSHOT_FILE).write_bytes(snap_bytes)
    (tmp_dir / SIGNATURE_FILE).write_bytes(sig)
    os.rename(tmp_dir, target)


def _replace_pointer(project: Path, rec_sha: str) -> None:
    """Atomically point ``Dockerfile.current`` at the given set."""
    set_root = project / SET_ROOT
    set_root.mkdir(parents=True, exist_ok=True)
    tmp = set_root / f".tmp-pointer-{os.getpid()}"
    tmp.write_text(set_dir_name(rec_sha) + "\n")
    os.replace(tmp, set_root / POINTER)


def _prune_other_sets(project: Path, rec_sha: str) -> None:
    """Remove every other directory under ``SET_ROOT/Dockerfile/``."""
    parent = project / SET_ROOT / SET_PARENT
    for child in parent.iterdir():
        if child.name != rec_sha:
            shutil.rmtree(child, ignore_errors=True)


def _publish(
    project: Path,
    rec_sha: str,
    rec_bytes: bytes,
    snap_bytes: bytes,
    sig: bytes,
    signing_key: Path,
) -> Issued:
    """Write or reuse the set directory, then atomically move the pointer."""
    target = project / SET_ROOT / set_dir_name(rec_sha)
    if target.exists():
        reason = _check_reuse(target, rec_bytes, snap_bytes, signing_key)
        if reason is not None:
            return Issued(False, reason, None)
    else:
        _write_set(target, rec_bytes, snap_bytes, sig)
    _replace_pointer(project, rec_sha)
    _prune_other_sets(project, rec_sha)
    return Issued(True, None, set_dir_name(rec_sha))


def issue(pre: Preflight, signing_key: Path, deployer_version: str) -> Issued:
    """Publish a signed, excluded, immutable provenance set for the
    Dockerfile authoring just wrote, then atomically repoint
    ``Dockerfile.current`` at it."""
    dockerfile_bytes = (pre.project / _ARTIFACT_PATH).read_bytes()
    snap_bytes, rec_bytes, rec_sha = _build(pre, dockerfile_bytes, deployer_version)
    paths = [f"{SET_ROOT}/{POINTER}"] + [
        f"{SET_ROOT}/{set_dir_name(rec_sha)}/{name}"
        for name in (RECORD_FILE, SNAPSHOT_FILE, SIGNATURE_FILE)
    ]
    reason = ensure_excluded(pre.project, paths)
    if reason is not None:
        return Issued(False, reason, None)
    try:
        sig = sshsig.sign(rec_bytes, signing_key)
    except SshSigError as exc:
        return Issued(False, str(exc), None)
    return _publish(pre.project, rec_sha, rec_bytes, snap_bytes, sig, signing_key)


def withdraw(project: Path) -> bool:
    """Remove ``Dockerfile.current`` first, then every set directory under
    ``SET_ROOT/Dockerfile/``; return whether anything was removed."""
    removed = False
    pointer = project / SET_ROOT / POINTER
    if pointer.exists():
        pointer.unlink()
        removed = True
    parent = project / SET_ROOT / SET_PARENT
    if parent.is_dir():
        for child in parent.iterdir():
            shutil.rmtree(child, ignore_errors=True)
            removed = True
    return removed
