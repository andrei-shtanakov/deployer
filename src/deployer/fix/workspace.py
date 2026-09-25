"""The fix directory, the linked worktree, the full-diff check and the commit
(design §3, §5.1, §5.3).

Every ``deployer fix`` gets a new sequenced directory
``attempt-<n>/fixes/<NNN>/`` (numbered like R's tries: never reused), and a
linked Git worktree of the clone at ``head_sha`` on a new branch, placed
outside the clone. The clone's own ``HEAD``, branch and working tree are
never switched or touched.

Git runs through one chokepoint, :func:`_run`, which never raises: the
public functions turn a failure into a reason, except :func:`commit`, which
raises :class:`CommitError` for the caller to map. Every command runs with
``core.hooksPath`` pointed at the null device, so none of the user's hooks
(``post-checkout`` on ``worktree add``; ``pre-commit``,
``prepare-commit-msg``, ``commit-msg``, ``post-commit`` on the commit) run on
an agent's work; ``commit`` also passes ``--no-verify`` so the intent is
explicit even where the override is not honoured.
"""

import os
import re
import stat
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from deployer.fix.binding import Bound, link_problem
from deployer.provenance.model import (
    POINTER,
    RECORD_FILE,
    SET_PARENT,
    SET_ROOT,
    SIGNATURE_FILE,
    SNAPSHOT_FILE,
)

BRANCH_FMT = "deployer/fix/{cls}/{head12}-{seq}"
FALLBACK_NAME = "deployer"
FALLBACK_EMAIL = "deployer@localhost"

_TIMEOUT_S = 60
_POINTER_PATH = f"{SET_ROOT}/{POINTER}"
_SET_PREFIX = f"{SET_ROOT}/{SET_PARENT}/"
_SET_FILES = frozenset({RECORD_FILE, SIGNATURE_FILE, SNAPSHOT_FILE})
_SET_NAME_RE = re.compile(r"[0-9a-f]{64}")
_MODIFIED = frozenset({" M", "M ", "MM"})
_ADDED = frozenset({"??", "A ", "AM"})
_DELETED = frozenset({" D", "D "})
_REGULAR_MODE = "100644"
_ABSENT = "000000"
# Variables that would point git at another repository, index or work tree
# than the one named by ``-C``; inherited (e.g. from a hook) they would
# silently redirect every command.
_REDIRECTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


class FixDirError(Exception):
    """The fix directory could not be created; the message says why."""


class CommitError(Exception):
    """The fix commit could not be made; the message names the git step."""


@dataclass(frozen=True)
class FixDir:
    """A new fix directory: its path, its sequence number and its ``fix_id``."""

    path: Path
    seq: int
    fix_id: str

    @property
    def worktree(self) -> Path:
        """Where the linked worktree lives: ``<fix dir>/worktree``."""
        return self.path / "worktree"


@dataclass(frozen=True)
class _Result:
    """One git invocation's outcome; ``code`` is ``None`` if it could not run."""

    code: int | None
    stdout: bytes
    error: str


def new_fix_dir(attempt_dir: Path) -> FixDir:
    """Create ``attempt_dir/fixes/<NNN>`` (§5.1) and generate its ``fix_id``.

    Numbered like R's tries: one above the highest existing number, created
    with an exclusive ``mkdir``, so an existing directory is never reused or
    overwritten. ``fix_id`` is a fresh UUID4. Raises :class:`FixDirError` when
    the directory cannot be created.
    """
    fixes = attempt_dir / "fixes"
    try:
        fixes.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name) for p in fixes.iterdir() if p.name.isdigit()]
        seq = max(existing, default=0) + 1
        path = fixes / f"{seq:03d}"
        path.mkdir()
    except OSError as exc:
        raise FixDirError(
            f"cannot create a fix directory under {fixes}: {exc}"
        ) from exc
    return FixDir(path=path, seq=seq, fix_id=str(uuid.uuid4()))


def branch_name(cls: str, head_sha: str, seq: int) -> str:
    """The fix branch for defect class ``cls``, ``head_sha`` and fix ``seq``."""
    return BRANCH_FMT.format(
        cls=cls.replace("_", "-"), head12=head_sha[:12], seq=f"{seq:03d}"
    )


def outside(path: Path, clone: Path) -> bool:
    """Whether ``path`` lies outside ``clone``, comparing resolved real paths.

    Symlinks are resolved on both sides, so a link cannot smuggle a path
    into the clone. ``False`` when ``path`` is the clone itself or anywhere
    inside it.
    """
    real_path = Path(os.path.realpath(path))
    real_clone = Path(os.path.realpath(clone))
    return real_path != real_clone and real_clone not in real_path.parents


def add_worktree(clone: Path, path: Path, branch: str, head_sha: str) -> str | None:
    """Add a linked worktree of ``clone`` at ``path`` on a new ``branch`` at
    ``head_sha`` (§5.1); the reason it could not, or ``None``.

    Refused when ``path`` is not :func:`outside` the clone or already exists,
    when ``head_sha`` names no commit, when ``branch`` is not a valid branch
    name or already exists. The clone's ``HEAD`` and working tree are not
    touched. The new worktree's ``HEAD`` is re-read and must equal the
    resolved ``head_sha``.
    """
    if not outside(path, clone):
        return f"worktree path {path} is not outside the clone {clone}"
    if os.path.lexists(path):
        return f"worktree path {path} already exists"
    resolved = _run(clone, "rev-parse", "--verify", "--quiet", f"{head_sha}^{{commit}}")
    if resolved.code != 0:
        return f"{head_sha} is not a commit in the clone"
    sha = resolved.stdout.decode().strip()
    if _run(clone, "check-ref-format", "--branch", branch).code != 0:
        return f"{branch!r} is not a valid branch name"
    exists = _run(clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if exists.code != 1:
        return (
            f"branch {branch} already exists"
            if exists.code == 0
            else f"cannot check branch {branch}: {exists.error}"
        )
    added = _run(clone, "worktree", "add", "-b", branch, str(path), sha)
    if added.code != 0:
        return f"git worktree add failed: {added.error}"
    head = _run(path, "rev-parse", "HEAD")
    if head.code != 0 or head.stdout.decode().strip() != sha:
        return f"the worktree at {path} is not at {sha}"
    return None


def allowed_diff_problem(worktree: Path, dockerfile: str, bound: Bound) -> str | None:
    """Check the worktree's full diff against §3; the reason, or ``None``.

    Reads ``git status --porcelain=v1 -z --untracked-files=all`` (both the
    staged and the unstaged column), which must show exactly: ``dockerfile``
    modified, changed only within ``bound``'s span (``link_problem`` on
    ``HEAD``'s blob against the working-tree bytes); the pointer modified;
    the three files of exactly one new set directory, untracked or added;
    and every file of every other set directory in ``HEAD`` deleted.
    Anything else — another path, a rename, a copy, a type or mode change,
    an ignore-file edit, a symlink or executable new file, or a clean filter
    that would commit different bytes than are on disk — is refused with a
    reason naming the path.
    """
    status = _run(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.code != 0:
        return f"git status failed: {status.error}"
    entries = _status_entries(status.stdout)
    if isinstance(entries, str):
        return entries
    reason = _classify(entries, dockerfile)
    if isinstance(reason, str):
        return reason
    added, deleted = reason
    new_files = [
        f"{_SET_PREFIX}{name}/{file}"
        for name, files in sorted(added.items())
        for file in sorted(files)
    ]
    return (
        _set_problem(worktree, added, deleted)
        or _mode_problem(worktree, new_files)
        or _filter_problem(worktree, [dockerfile, _POINTER_PATH, *new_files])
        or _dockerfile_problem(worktree, dockerfile, bound)
    )


def commit(worktree: Path, message: str) -> str:
    """Stage exactly the changed paths and commit them; the new commit's sha.

    Call it only after :func:`allowed_diff_problem` returned ``None``: the
    paths staged (``git add -A -- <paths>``, literal pathspecs) are those
    ``git status`` reports, which that check has just limited to §3's.

    Author and committer are passed explicitly for both roles: the clone's
    effective ``user.name`` and ``user.email`` when both are configured,
    else ``deployer <deployer@localhost>`` — never a mix, and never an
    identity inherited from the environment. Hooks do not run
    (``--no-verify`` plus the chokepoint's ``core.hooksPath`` override) and
    the commit is not GPG/SSH-signed (``--no-gpg-sign``): an agent commit
    must not depend on the user's hooks or on an interactive signer; the
    provenance set carries its own signature. Raises :class:`CommitError`.
    """
    status = _run(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.code != 0:
        raise CommitError(f"git status failed: {status.error}")
    entries = _status_entries(status.stdout)
    if isinstance(entries, str):
        raise CommitError(entries)
    paths = [path for _, path in entries]
    if not paths:
        raise CommitError("nothing to commit")
    staged = _run(worktree, "--literal-pathspecs", "add", "-A", "--", *paths)
    if staged.code != 0:
        raise CommitError(f"git add failed: {staged.error}")
    made = _run(
        worktree,
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        message,
        env=_identity_env(worktree),
    )
    if made.code != 0:
        raise CommitError(f"git commit failed: {made.error}")
    head = _run(worktree, "rev-parse", "HEAD")
    if head.code != 0:
        raise CommitError(f"git rev-parse failed: {head.error}")
    return head.stdout.decode().strip()


def _run(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> _Result:
    """Run ``git -C cwd <args>`` without hooks; never raises."""
    environ = {k: v for k, v in os.environ.items() if k not in _REDIRECTING_ENV}
    environ.update(env or {})
    command = ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(cwd), *args]
    try:
        proc = subprocess.run(
            command, capture_output=True, timeout=_TIMEOUT_S, env=environ
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _Result(code=None, stdout=b"", error=f"could not run: {exc}")
    error = proc.stderr.decode(errors="replace").strip()
    return _Result(code=proc.returncode, stdout=proc.stdout, error=error)


def _identity_env(worktree: Path) -> dict[str, str]:
    """Author and committer variables for the fix commit (see ``commit``)."""
    name = _run(worktree, "config", "--get", "user.name")
    email = _run(worktree, "config", "--get", "user.email")
    configured_name = name.stdout.decode(errors="replace").strip()
    configured_email = email.stdout.decode(errors="replace").strip()
    if name.code == 0 and email.code == 0 and configured_name and configured_email:
        who, mail = configured_name, configured_email
    else:
        who, mail = FALLBACK_NAME, FALLBACK_EMAIL
    return {
        "GIT_AUTHOR_NAME": who,
        "GIT_AUTHOR_EMAIL": mail,
        "GIT_COMMITTER_NAME": who,
        "GIT_COMMITTER_EMAIL": mail,
    }


def _status_entries(raw: bytes) -> list[tuple[str, str]] | str:
    """``(XY, path)`` per porcelain v1 ``-z`` entry; a rename's or copy's
    source path follows as its own entry, reported with the same code."""
    fields = raw.split(b"\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if len(field) < 4 or field[2:3] != b" ":
            return f"unparseable git status entry {field!r}"
        code, path = field[:2].decode(errors="replace"), _decode(field[3:])
        entries.append((code, path))
        if "R" in code or "C" in code:
            if i < len(fields) and fields[i]:
                entries.append((code, _decode(fields[i])))
            i += 1
    return entries


def _decode(raw: bytes) -> str:
    """A path as git printed it, decoded like the filesystem would."""
    return os.fsdecode(raw)


def _classify(
    entries: list[tuple[str, str]], dockerfile: str
) -> tuple[dict[str, set[str]], set[str]] | str:
    """Sort status entries into added set files by set name and deleted
    paths, requiring the two modifications; the reason for the first entry
    §3 does not allow."""
    modified: set[str] = set()
    added: dict[str, set[str]] = {}
    deleted: set[str] = set()
    for code, path in entries:
        if path in (dockerfile, _POINTER_PATH):
            if code not in _MODIFIED:
                return f"{path}: status {code!r}, expected a modification"
            modified.add(path)
        elif path.startswith(_SET_PREFIX) and code in _ADDED:
            name, _, file = path[len(_SET_PREFIX) :].partition("/")
            if not _SET_NAME_RE.fullmatch(name) or file not in _SET_FILES:
                return f"{path}: not a file of a provenance set"
            added.setdefault(name, set()).add(file)
        elif path.startswith(_SET_PREFIX) and code in _DELETED:
            deleted.add(path)
        else:
            return f"{path}: status {code!r} is not an allowed change"
    for required in (dockerfile, _POINTER_PATH):
        if required not in modified:
            return f"{required}: expected a modification, found none"
    return added, deleted


def _set_problem(
    worktree: Path, added: dict[str, set[str]], deleted: set[str]
) -> str | None:
    """Exactly one complete new set, and every other ``HEAD`` set file gone."""
    if len(added) != 1:
        names = ", ".join(sorted(added)) or "none"
        return f"expected exactly one new provenance set, found {names}"
    [(name, files)] = added.items()
    if files != _SET_FILES:
        missing = ", ".join(sorted(_SET_FILES - files))
        return f"{_SET_PREFIX}{name}/: incomplete set, missing {missing}"
    listed = _run(
        worktree,
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        "HEAD",
        "--",
        _SET_PREFIX,
    )
    if listed.code != 0:
        return f"git ls-tree failed: {listed.error}"
    in_head = {_decode(p) for p in listed.stdout.split(b"\0") if p}
    expected = {p for p in in_head if not p.startswith(f"{_SET_PREFIX}{name}/")}
    for path in sorted(expected - deleted):
        return f"{path}: an old set file is not deleted"
    for path in sorted(deleted - expected):
        return f"{path}: deleted, but not a file of an old set"
    return None


def _mode_problem(worktree: Path, new_files: list[str]) -> str | None:
    """No mode change on a tracked path, in the index or the working tree;
    every added path, staged or on disk, a regular non-executable file."""
    for args in (("diff", "--cached", "HEAD"), ("diff",)):
        diff = _run(worktree, *args, "--raw", "-z", "--no-renames")
        if diff.code != 0:
            return f"git {' '.join(args)} failed: {diff.error}"
        fields = [f for f in diff.stdout.split(b"\0") if f]
        for meta, raw_path in zip(fields[::2], fields[1::2]):
            old_mode, new_mode = meta.decode().lstrip(":").split()[:2]
            if _ABSENT not in (old_mode, new_mode) and old_mode != new_mode:
                return f"{_decode(raw_path)}: mode change {old_mode} -> {new_mode}"
            if old_mode == _ABSENT and new_mode != _REGULAR_MODE:
                return f"{_decode(raw_path)}: added with mode {new_mode}"
    for path in new_files:
        try:
            mode = os.lstat(worktree / path).st_mode
        except OSError as exc:
            return f"{path}: cannot stat: {exc}"
        if not stat.S_ISREG(mode) or mode & 0o111:
            return f"{path}: not a regular non-executable file"
    return None


def _filter_problem(worktree: Path, paths: list[str]) -> str | None:
    """No path whose committed blob would differ from its bytes on disk
    (a clean filter or end-of-line conversion)."""
    for path in paths:
        filtered = _run(worktree, "hash-object", "--", path)
        raw = _run(worktree, "hash-object", "--no-filters", "--", path)
        if filtered.code != 0 or raw.code != 0:
            return f"{path}: git hash-object failed: {filtered.error or raw.error}"
        if filtered.stdout != raw.stdout:
            return f"{path}: a git filter would change the committed bytes"
    return None


def _dockerfile_problem(worktree: Path, dockerfile: str, bound: Bound) -> str | None:
    """``link_problem`` on ``HEAD``'s Dockerfile blob against the disk bytes."""
    original = _run(worktree, "cat-file", "blob", f"HEAD:{dockerfile}")
    if original.code != 0:
        return f"{dockerfile}: cannot read it at HEAD: {original.error}"
    try:
        corrected = (worktree / dockerfile).read_bytes()
    except OSError as exc:
        return f"{dockerfile}: cannot read it: {exc}"
    problem = link_problem(original.stdout, corrected, bound)
    return None if problem is None else f"{dockerfile}: {problem}"
