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
    ci_file = ignore.ci_ignore_file(project, _ARTIFACT_PATH)
    local_file = ignore.local_ignore_file(project, _ARTIFACT_PATH, "podman")
    for file in (ci_file, local_file):
        reason = _refusal_for_unsupported(project, file)
        if reason is not None:
            return reason
    _ensure_pattern(project, ci_file, True)
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
    """``None`` if the existing set matches exactly, else why it does not.

    A missing file, a non-directory ``target``, or any other read failure
    all mean the same thing here: the existing set cannot be trusted as a
    match, so it is refused rather than raising.
    """
    reason = f"existing set {target.name} does not match; not written"
    if not target.is_dir():
        return reason
    try:
        matches = (target / RECORD_FILE).read_bytes() == rec_bytes and (
            target / SNAPSHOT_FILE
        ).read_bytes() == snap_bytes
        if matches:
            existing_sig = (target / SIGNATURE_FILE).read_bytes()
            pub = sshsig.public_key(signing_key)
            matches = sshsig.verify_with_public_key(rec_bytes, existing_sig, pub).ok
    except OSError:
        matches = False
    return None if matches else reason


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
    """Atomically point ``Dockerfile.current`` at the given set.

    On failure the temp pointer file is unlinked on a best-effort basis
    before the error is re-raised, so a crashed run leaves no stray
    ``.tmp-pointer-*`` file behind alongside the untouched old pointer.
    """
    set_root = project / SET_ROOT
    set_root.mkdir(parents=True, exist_ok=True)
    tmp = set_root / f".tmp-pointer-{os.getpid()}"
    tmp.write_text(set_dir_name(rec_sha) + "\n")
    try:
        os.replace(tmp, set_root / POINTER)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _is_prunable_set_dir(child: Path, rec_sha: str) -> bool:
    """Whether ``child`` is a finished set directory safe to remove: a real
    directory, not the one just published, and not another run's
    in-progress ``.tmp-*`` staging directory."""
    if not child.is_dir():
        return False
    if child.name == rec_sha:
        return False
    return not child.name.startswith(".tmp-")


def _prune_other_sets(project: Path, rec_sha: str) -> None:
    """Remove every other set directory under ``SET_ROOT/Dockerfile/``.

    Non-directory entries and any live ``.tmp-*`` staging directory (another
    run in flight) are left alone; a leftover crashed-run tmp dir is
    harmless since it is already excluded under ``.deployer/``. A real
    removal failure is left to raise: the pointer is already correct by the
    time this runs, so the stale directories are cosmetic, not correctness
    risk.
    """
    parent = project / SET_ROOT / SET_PARENT
    for child in parent.iterdir():
        if _is_prunable_set_dir(child, rec_sha):
            shutil.rmtree(child)


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
    ``SET_ROOT/Dockerfile/``; return whether anything was removed.

    Like ``_prune_other_sets``, this leaves non-directory entries and any
    live ``.tmp-*`` staging directory alone.
    """
    removed = False
    pointer = project / SET_ROOT / POINTER
    if pointer.exists():
        pointer.unlink()
        removed = True
    parent = project / SET_ROOT / SET_PARENT
    if parent.is_dir():
        for child in parent.iterdir():
            if not _is_prunable_set_dir(child, rec_sha=""):
                continue
            shutil.rmtree(child)
            removed = True
    return removed
