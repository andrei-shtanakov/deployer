"""The fix directory, the linked worktree, the full-diff check and the commit
(design §3, §5.1, §5.3).

Every ``deployer fix`` gets a new sequenced directory
``attempt-<n>/fixes/<NNN>/`` (numbered like R's tries), and a linked Git
worktree of the clone at ``head_sha`` on a new branch, placed outside the
clone. The clone's own ``HEAD``, branch and working tree are never switched
or touched.

Git runs through one chokepoint, :func:`_run`, which never raises: the
public functions turn a failure into a reason, except :func:`commit`, which
raises :class:`CommitError` for the caller to map. No user-configured
program runs on an agent's work: every command points ``core.hooksPath`` at
the null device (no ``post-checkout``, ``pre-commit``,
``prepare-commit-msg``, ``commit-msg`` or ``post-commit``), turns off
``core.fsmonitor``, auto-gc and auto-maintenance, and empties the
``clean``/``smudge``/``process`` command of every configured filter driver
(:func:`_guards`). The worktree therefore holds raw blobs; the local proof
reads R's ``source/``, not the worktree.

The commit stages blobs, not paths: :func:`allowed_diff_problem` returns a
:class:`Vetted` naming every allowed change with the blob of the exact bytes
it checked, and :func:`commit` re-reads the status and re-hashes the bytes
before writing exactly those blobs into the index — ``git add`` never runs.
"""

import os
import re
import stat
import subprocess
import uuid
from collections.abc import Mapping, Sequence
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
    set_dir_name,
)

BRANCH_FMT = "deployer/fix/{cls}/{head12}-{seq}"
DEPLOYER_NAME = "deployer"
DEPLOYER_EMAIL = "deployer@localhost"

_TIMEOUT_S = 60
_MKDIR_TRIES = 5
_SEQ_RE = re.compile(r"[0-9]+")
_POINTER_PATH = f"{SET_ROOT}/{POINTER}"
_SET_PREFIX = f"{SET_ROOT}/{SET_PARENT}/"
_SET_FILES = frozenset({RECORD_FILE, SIGNATURE_FILE, SNAPSHOT_FILE})
_SET_NAME_RE = re.compile(r"[0-9a-f]{64}")
_FILTER_KEY_RE = re.compile(r"filter\.(.+)\.[^.]+")
_MODIFIED = frozenset({" M", "M ", "MM"})
_ADDED = frozenset({"??", "A ", "AM"})
_DELETED = frozenset({" D", "D "})
_REGULAR_MODE = "100644"
_ABSENT = "000000"
_STATUS = ("status", "--porcelain=v1", "-z", "--untracked-files=all")
_BASE_GUARDS = (
    f"core.hooksPath={os.devnull}",
    "core.fsmonitor=false",
    "gc.auto=0",
    "maintenance.auto=false",
)
_IDENTITY = {
    "GIT_AUTHOR_NAME": DEPLOYER_NAME,
    "GIT_AUTHOR_EMAIL": DEPLOYER_EMAIL,
    "GIT_COMMITTER_NAME": DEPLOYER_NAME,
    "GIT_COMMITTER_EMAIL": DEPLOYER_EMAIL,
}
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
class Vetted:
    """The changes :func:`allowed_diff_problem` admitted, for :func:`commit`.

    ``head`` is the worktree's ``HEAD`` when checked. ``changes`` holds
    ``(path, blob_sha)`` per allowed change: the ``--no-filters`` blob of
    the exact bytes checked for an added or modified path, ``None`` for a
    deletion.
    """

    head: str
    changes: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class _Result:
    """One git invocation's outcome; ``code`` is ``None`` if it could not run."""

    code: int | None
    stdout: bytes
    error: str


def new_fix_dir(attempt_dir: Path) -> FixDir:
    """Create ``attempt_dir/fixes/<NNN>`` (§5.1) and generate its ``fix_id``.

    Numbered like R's tries: one above the highest existing all-digit name,
    created with an exclusive ``mkdir``, so an existing directory is never
    reused or overwritten. A concurrent creator taking the number first is
    retried (up to five times) with a fresh listing. ``fix_id`` is a fresh
    UUID4. Raises :class:`FixDirError` when no directory can be created.
    """
    fixes = attempt_dir / "fixes"
    try:
        fixes.mkdir(parents=True, exist_ok=True)
        for _ in range(_MKDIR_TRIES):
            seq = _highest(fixes) + 1
            path = fixes / f"{seq:03d}"
            try:
                path.mkdir()
            except FileExistsError:
                continue
            return FixDir(path=path, seq=seq, fix_id=str(uuid.uuid4()))
    except OSError as exc:
        raise FixDirError(
            f"cannot create a fix directory under {fixes}: {exc}"
        ) from exc
    raise FixDirError(
        f"cannot create a fix directory under {fixes}: "
        f"lost the race {_MKDIR_TRIES} times"
    )


def _highest(fixes: Path) -> int:
    """The highest all-ASCII-digit entry name under ``fixes``, or 0."""
    return max(
        (int(p.name) for p in fixes.iterdir() if _SEQ_RE.fullmatch(p.name)),
        default=0,
    )


def branch_name(cls: str, head_sha: str, seq: int) -> str:
    """The fix branch for defect class ``cls``, ``head_sha`` and fix ``seq``."""
    return BRANCH_FMT.format(
        cls=cls.replace("_", "-"), head12=head_sha[:12], seq=f"{seq:03d}"
    )


def outside(path: Path, clone: Path) -> bool:
    """Whether ``path`` lies outside ``clone``.

    ``False`` when ``path`` is the clone itself or anywhere inside it. The
    resolved real paths are compared, so a symlink cannot smuggle a path
    in; and every existing ancestor of the resolved ``path`` is compared by
    ``(st_dev, st_ino)`` with the clone, so a spelling the filesystem
    treats as the same directory (a case variant on a case-insensitive
    volume) is inside too.
    """
    real_path = Path(os.path.realpath(path))
    real_clone = Path(os.path.realpath(clone))
    if real_path == real_clone or real_clone in real_path.parents:
        return False
    try:
        clone_id = _file_id(real_clone)
    except OSError:
        return True
    for ancestor in (real_path, *real_path.parents):
        try:
            if _file_id(ancestor) == clone_id:
                return False
        except OSError:
            continue
    return True


def _file_id(path: Path) -> tuple[int, int]:
    """``(st_dev, st_ino)`` of ``path``, following symlinks."""
    info = os.stat(path)
    return info.st_dev, info.st_ino


def add_worktree(clone: Path, path: Path, branch: str, head_sha: str) -> str | None:
    """Add a linked worktree of ``clone`` at ``path`` on a new ``branch`` at
    ``head_sha`` (§5.1); the reason it could not, or ``None``.

    Refused when ``path`` is not :func:`outside` the clone or already exists,
    when ``head_sha`` names no commit, when ``refs/heads/<branch>`` is not a
    valid ref name or already exists. The clone's ``HEAD`` and working tree
    are not touched. The new worktree's ``HEAD`` is re-read and must equal
    the resolved ``head_sha``.
    """
    if not outside(path, clone):
        return f"worktree path {path} is not outside the clone {clone}"
    if os.path.lexists(path):
        return f"worktree path {path} already exists"
    guards = _guards(clone)
    if isinstance(guards, str):
        return guards
    resolved = _run(
        clone, "rev-parse", "--verify", "--quiet", f"{head_sha}^{{commit}}", g=guards
    )
    if resolved.code != 0:
        return f"{head_sha} is not a commit in the clone"
    sha = resolved.stdout.decode().strip()
    ref = f"refs/heads/{branch}"
    if _run(clone, "check-ref-format", ref, g=guards).code != 0:
        return f"{branch!r} is not a valid branch name"
    exists = _run(clone, "rev-parse", "--verify", "--quiet", ref, g=guards)
    if exists.code != 1:
        return (
            f"branch {branch} already exists"
            if exists.code == 0
            else f"cannot check branch {branch}: {exists.error}"
        )
    added = _run(clone, "worktree", "add", "-b", branch, str(path), sha, g=guards)
    if added.code != 0:
        return f"git worktree add failed: {added.error}"
    head = _run(path, "rev-parse", "HEAD", g=guards)
    if head.code != 0 or head.stdout.decode().strip() != sha:
        return f"the worktree at {path} is not at {sha}"
    return None


def allowed_diff_problem(
    worktree: Path, dockerfile: str, bound: Bound, expected_record_sha: str
) -> Vetted | str:
    """Check the worktree's full diff against §3: the vetted changes, or the
    reason they are not allowed.

    Reads ``git status --porcelain=v1 -z --untracked-files=all`` (both the
    staged and the unstaged column), which must show exactly: ``dockerfile``
    modified, changed only within ``bound``'s span (``link_problem`` on
    ``HEAD``'s blob against the working-tree bytes); the pointer modified to
    name exactly the planned set; the three files of exactly one new set
    directory, named ``expected_record_sha``, untracked or added; and every
    file of every other set directory in ``HEAD`` deleted. Anything else —
    another path, a rename, a copy, a type or mode change, an ignore-file
    edit, a symlink or executable new file, a stray file deleted under the
    set parent — is refused with a reason naming the path.
    """
    guards = _guards(worktree)
    if isinstance(guards, str):
        return guards
    head = _run(worktree, "rev-parse", "HEAD", g=guards)
    if head.code != 0:
        return f"git rev-parse HEAD failed: {head.error}"
    status = _run(worktree, *_STATUS, g=guards)
    if status.code != 0:
        return f"git status failed: {status.error}"
    entries = _status_entries(status.stdout)
    if isinstance(entries, str):
        return entries
    sorted_entries = _classify(entries, dockerfile)
    if isinstance(sorted_entries, str):
        return sorted_entries
    added, deleted = sorted_entries
    reason = _set_problem(worktree, added, deleted, expected_record_sha, guards)
    if reason is None:
        reason = _mode_problem(worktree, _set_files(expected_record_sha), guards)
    if reason is not None:
        return reason
    blobs = _checked_blobs(worktree, dockerfile, bound, expected_record_sha, guards)
    if isinstance(blobs, str):
        return blobs
    changes = sorted([*blobs.items(), *((path, None) for path in deleted)])
    return Vetted(head=head.stdout.decode().strip(), changes=tuple(changes))


def commit(worktree: Path, message: str, vetted: Vetted) -> str:
    """Commit exactly ``vetted``'s blobs; the new commit's sha.

    Re-reads the status (its path set must equal ``vetted``'s) and
    ``HEAD`` (unchanged), re-hashes every added or modified path's bytes
    (each blob must equal the vetted one), then writes those blobs into the
    index with ``update-index --cacheinfo`` — the ``HEAD`` entry's mode for
    a modified path, ``100644`` for a new one — and removes each deletion
    with ``update-index --force-remove``. The staged diff against ``HEAD``
    must then name exactly ``vetted``'s paths. ``git add`` never runs.

    Author and committer are always ``deployer <deployer@localhost>``,
    passed explicitly for both roles, whatever the clone's configuration or
    the environment says. Hooks do not run (``--no-verify`` plus the
    chokepoint's ``core.hooksPath`` override) and the commit is not
    GPG/SSH-signed (``--no-gpg-sign``): an agent commit must not depend on
    the user's hooks or an interactive signer; the provenance set carries
    its own signature. Raises :class:`CommitError`.
    """
    guards = _guards(worktree)
    if isinstance(guards, str):
        raise CommitError(guards)
    _recheck(worktree, vetted, guards)
    modes = _head_modes(worktree, guards)
    cacheinfo: list[str] = []
    removed: list[str] = []
    for path, blob in vetted.changes:
        if blob is None:
            removed.append(path)
            continue
        written = _hash_file(worktree, path, guards, write=True)
        if written != blob:
            raise CommitError(f"{path}: changed since it was checked")
        mode = modes.get(path, _REGULAR_MODE)
        cacheinfo += ["--cacheinfo", f"{mode},{blob},{path}"]
    _must(_run(worktree, "update-index", "--add", *cacheinfo, g=guards), "stage")
    if removed:
        removal = _run(
            worktree, "update-index", "--force-remove", "--", *removed, g=guards
        )
        _must(removal, "unstage deletions")
    staged = _run(
        worktree,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-renames",
        "HEAD",
        g=guards,
    )
    _must(staged, "diff --cached")
    if _paths(staged.stdout) != {path for path, _ in vetted.changes}:
        raise CommitError("the staged changes differ from the vetted ones")
    made = _run(
        worktree,
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        message,
        g=guards,
        env=_IDENTITY,
    )
    _must(made, "commit")
    head = _run(worktree, "rev-parse", "HEAD", g=guards)
    _must(head, "rev-parse")
    return head.stdout.decode().strip()


def _run(
    cwd: Path,
    *args: str,
    g: Sequence[str],
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> _Result:
    """Run ``git -C cwd <args>`` with the ``-c`` overrides ``g``; never raises."""
    environ = {k: v for k, v in os.environ.items() if k not in _REDIRECTING_ENV}
    environ.update(env or {})
    overrides = [arg for item in g for arg in ("-c", item)]
    command = ["git", *overrides, "-C", str(cwd), *args]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=_TIMEOUT_S,
            env=environ,
            input=stdin,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _Result(code=None, stdout=b"", error=f"could not run: {exc}")
    error = proc.stderr.decode(errors="replace").strip()
    return _Result(code=proc.returncode, stdout=proc.stdout, error=error)


def _guards(repo: Path) -> tuple[str, ...] | str:
    """The ``-c`` overrides for every command against ``repo``: the base
    guards plus, per configured filter driver, empty ``clean``, ``smudge``
    and ``process`` commands and ``required=false``; or the reason the
    drivers could not be read."""
    listed = _run(repo, "config", "-z", "--get-regexp", r"^filter\.", g=_BASE_GUARDS)
    if listed.code not in (0, 1):
        return f"cannot read the filter configuration: {listed.error}"
    names: set[str] = set()
    for entry in listed.stdout.split(b"\0"):
        key = entry.split(b"\n", 1)[0].decode(errors="replace")
        match = _FILTER_KEY_RE.fullmatch(key)
        if match is None:
            continue
        name = match.group(1)
        if "=" in name:
            return f"cannot neutralise the filter driver {name!r}"
        names.add(name)
    neutral = [
        f"filter.{name}.{key}={value}"
        for name in sorted(names)
        for key, value in (
            ("clean", ""),
            ("smudge", ""),
            ("process", ""),
            ("required", "false"),
        )
    ]
    return (*_BASE_GUARDS, *neutral)


def _must(result: _Result, step: str) -> None:
    """Raise :class:`CommitError` naming ``step`` unless ``result`` succeeded."""
    if result.code != 0:
        raise CommitError(f"git {step} failed: {result.error}")


def _recheck(worktree: Path, vetted: Vetted, guards: Sequence[str]) -> None:
    """``HEAD`` and the status path set are still those that were vetted."""
    head = _run(worktree, "rev-parse", "HEAD", g=guards)
    _must(head, "rev-parse")
    if head.stdout.decode().strip() != vetted.head:
        raise CommitError("HEAD moved since the diff was checked")
    status = _run(worktree, *_STATUS, g=guards)
    _must(status, "status")
    entries = _status_entries(status.stdout)
    if isinstance(entries, str):
        raise CommitError(entries)
    if not vetted.changes:
        raise CommitError("nothing to commit")
    now = {path for _, path in entries}
    if now != {path for path, _ in vetted.changes}:
        extra = sorted(now ^ {path for path, _ in vetted.changes})
        raise CommitError(f"the changed paths differ from the vetted ones: {extra}")


def _head_modes(worktree: Path, guards: Sequence[str]) -> dict[str, str]:
    """Path to mode for every blob in ``HEAD``."""
    listed = _run(worktree, "ls-tree", "-r", "-z", "HEAD", g=guards)
    _must(listed, "ls-tree")
    modes: dict[str, str] = {}
    for record in listed.stdout.split(b"\0"):
        if record:
            meta, _, raw_path = record.partition(b"\t")
            modes[_decode(raw_path)] = meta.split()[0].decode()
    return modes


def _hash_file(
    worktree: Path, path: str, guards: Sequence[str], write: bool = False
) -> str:
    """The ``--no-filters`` blob sha of ``path``'s bytes on disk; written to
    the object store when ``write``. Raises :class:`CommitError`."""
    try:
        data = (worktree / path).read_bytes()
    except OSError as exc:
        raise CommitError(f"{path}: cannot read it: {exc}") from exc
    return _hash_bytes(worktree, data, guards, write=write)


def _hash_bytes(
    worktree: Path, data: bytes, guards: Sequence[str], write: bool = False
) -> str:
    """The ``--no-filters`` blob sha of ``data``. Raises :class:`CommitError`."""
    flags = ["-w"] if write else []
    hashed = _run(
        worktree,
        "hash-object",
        *flags,
        "--no-filters",
        "--stdin",
        g=guards,
        stdin=data,
    )
    _must(hashed, "hash-object")
    return hashed.stdout.decode().strip()


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


def _paths(raw: bytes) -> set[str]:
    """The NUL-separated paths in ``raw``."""
    return {_decode(p) for p in raw.split(b"\0") if p}


def _set_files(record_sha: str) -> list[str]:
    """The three file paths of the set named ``record_sha``."""
    return [f"{_SET_PREFIX}{record_sha}/{file}" for file in sorted(_SET_FILES)]


def _classify(
    entries: list[tuple[str, str]], dockerfile: str
) -> tuple[dict[str, set[str]], set[str]] | str:
    """Sort status entries into added set files by set name and deleted
    set files, requiring the two modifications; the reason for the first
    entry §3 does not allow."""
    modified: set[str] = set()
    added: dict[str, set[str]] = {}
    deleted: set[str] = set()
    for code, path in entries:
        if path in (dockerfile, _POINTER_PATH):
            if code not in _MODIFIED:
                return f"{path}: status {code!r}, expected a modification"
            modified.add(path)
            continue
        name, file = _set_parts(path)
        if name is None or (code not in _ADDED and code not in _DELETED):
            return f"{path}: status {code!r} is not an allowed change"
        if not _is_set_file(path):
            return f"{path}: not a file of a provenance set"
        if code in _ADDED:
            added.setdefault(name, set()).add(file)
        else:
            deleted.add(path)
    for required in (dockerfile, _POINTER_PATH):
        if required not in modified:
            return f"{required}: expected a modification, found none"
    return added, deleted


def _set_parts(path: str) -> tuple[str | None, str]:
    """``(set name, rest)`` for a path under the set parent, else ``None``."""
    if not path.startswith(_SET_PREFIX):
        return None, ""
    name, _, rest = path[len(_SET_PREFIX) :].partition("/")
    return name, rest


def _is_set_file(path: str) -> bool:
    """Whether ``path`` is ``<set parent>/<64 hex>/<one of the set files>``."""
    name, file = _set_parts(path)
    return (
        name is not None and bool(_SET_NAME_RE.fullmatch(name)) and (file in _SET_FILES)
    )


def _set_problem(
    worktree: Path,
    added: dict[str, set[str]],
    deleted: set[str],
    expected_record_sha: str,
    guards: Sequence[str],
) -> str | None:
    """Exactly the planned set, complete, and every other ``HEAD`` set file
    gone."""
    if list(added) != [expected_record_sha]:
        names = ", ".join(sorted(added)) or "none"
        return (
            "expected exactly one new provenance set, "
            f"{expected_record_sha}; found {names}"
        )
    files = added[expected_record_sha]
    if files != _SET_FILES:
        missing = ", ".join(sorted(_SET_FILES - files))
        return f"{_SET_PREFIX}{expected_record_sha}/: incomplete set, missing {missing}"
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
        g=guards,
    )
    if listed.code != 0:
        return f"git ls-tree failed: {listed.error}"
    # A's ``issue`` prunes every other set directory and leaves stray
    # non-directory entries alone; only ``<64 hex>/<set file>`` paths may be
    # deleted (``_classify``), so only those are expected to be.
    expected = {
        p
        for p in _paths(listed.stdout)
        if _is_set_file(p) and _set_parts(p)[0] != expected_record_sha
    }
    for path in sorted(expected - deleted):
        return f"{path}: an old set file is not deleted"
    for path in sorted(deleted - expected):
        return f"{path}: deleted, but not a file of an old set"
    return None


def _mode_problem(
    worktree: Path, new_files: list[str], guards: Sequence[str]
) -> str | None:
    """No mode change on a tracked path, in the index or the working tree;
    every added path, staged or on disk, a regular non-executable file."""
    for args in (("diff", "--cached", "HEAD"), ("diff",)):
        diff = _run(worktree, *args, "--raw", "-z", "--no-renames", g=guards)
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


def _checked_blobs(
    worktree: Path,
    dockerfile: str,
    bound: Bound,
    expected_record_sha: str,
    guards: Sequence[str],
) -> dict[str, str] | str:
    """Read each added or modified path's bytes once, check the Dockerfile
    and the pointer content, and hash exactly those bytes."""
    paths = [dockerfile, _POINTER_PATH, *_set_files(expected_record_sha)]
    data: dict[str, bytes] = {}
    for path in paths:
        try:
            data[path] = (worktree / path).read_bytes()
        except OSError as exc:
            return f"{path}: cannot read it: {exc}"
    pointer = f"{set_dir_name(expected_record_sha)}\n".encode()
    if data[_POINTER_PATH] != pointer:
        return f"{_POINTER_PATH}: does not name the planned set {expected_record_sha}"
    original = _run(worktree, "cat-file", "blob", f"HEAD:{dockerfile}", g=guards)
    if original.code != 0:
        return f"{dockerfile}: cannot read it at HEAD: {original.error}"
    problem = link_problem(original.stdout, data[dockerfile], bound)
    if problem is not None:
        return f"{dockerfile}: {problem}"
    try:
        return {path: _hash_bytes(worktree, raw, guards) for path, raw in data.items()}
    except CommitError as exc:
        return str(exc)
