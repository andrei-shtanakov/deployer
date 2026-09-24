"""The local-git chokepoint for authoring provenance (A §5.1)."""

import io
import re
import subprocess
import tarfile
from pathlib import Path

from deployer.provenance.model import TreeRow

_TIMEOUT_S = 60
_SLUG_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


class GitError(Exception):
    """A git command failed; the message names it."""


def _git(path: Path, *args: str) -> bytes:
    """Run ``git <args>`` against the checkout at ``path``; return stdout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} could not run: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise GitError(f"git {args[0]} failed: {stderr}")
    return proc.stdout


def is_checkout(path: Path) -> bool:
    """Whether ``path`` is inside a git work tree."""
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except GitError:
        return False


def toplevel(path: Path) -> Path:
    """The absolute root of the git work tree containing ``path``.

    Raises ``GitError`` (via ``_git``) when ``path`` is not inside a work
    tree; callers that already know it is (``is_checkout`` passed) still
    need to handle that, since a race is always possible.
    """
    return Path(_git(path, "rev-parse", "--show-toplevel").decode().strip())


def origin_slug(path: Path) -> str | None:
    """``owner/name`` from the ``origin`` remote, or ``None`` if absent/unparseable."""
    try:
        url = _git(path, "remote", "get-url", "origin").decode().strip()
    except GitError:
        return None
    match = _SLUG_RE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def dirty_paths(path: Path) -> list[str]:
    """Paths with staged, unstaged or untracked changes, from porcelain status."""
    out = _git(path, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    paths: list[str] = []
    entries = out.decode(errors="replace").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        paths.append(name)
        if "R" in status or "C" in status:
            i += 1  # the rename/copy source follows as its own field
    return paths


def head_commit(path: Path) -> str:
    """The commit SHA that ``HEAD`` points at."""
    return _git(path, "rev-parse", "HEAD").decode().strip()


def tree_listing(path: Path, commit: str) -> list[TreeRow]:
    """The recursive tree of ``commit``, one ``TreeRow`` per entry."""
    out = _git(path, "ls-tree", "-r", "-t", "--full-tree", "-z", commit)
    rows: list[TreeRow] = []
    for record in out.decode(errors="replace").split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        mode, kind, sha = meta.split()
        rows.append(TreeRow(path=name, mode=mode, type=kind, sha=sha))
    return rows


def export_commit(path: Path, commit: str, dest: Path) -> None:
    """Extract ``commit``'s tree into ``dest`` via ``git archive``."""
    data = _git(path, "archive", "--format=tar", commit)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
    except tarfile.TarError as exc:
        raise GitError(f"cannot export {commit}: {exc}") from exc
