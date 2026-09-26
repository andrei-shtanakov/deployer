"""The fix directory, the worktree, the full-diff check and the commit
(design §3, §5.1, §5.3) against real local Git — no network."""

import hashlib
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from deployer.admission.model import Defect
from deployer.fix import workspace
from deployer.fix.binding import Bound, bind_instruction
from deployer.fix.workspace import (
    DEPLOYER_EMAIL,
    DEPLOYER_NAME,
    CommitError,
    FixDir,
    FixDirError,
    Vetted,
    add_worktree,
    allowed_diff_problem,
    branch_name,
    commit,
    new_fix_dir,
    outside,
)
from deployer.provenance import gitrepo
from tests.fix.conftest import partial_clone

_DOCKERFILE = b"FROM python:3.12-slim\nCOPY app.py /app/\nRUN echo hi\n"
_CORRECTED = b"FROM python:3.12-slim\nCOPY main.py /app/\nRUN echo hi\n"
_SET_DIR = ".deployer/authoring/Dockerfile"
_POINTER = ".deployer/authoring/Dockerfile.current"
_SET_FILES = ("record.json", "record.json.sig", "snapshot.json")
_OLD_SETS = (hashlib.sha256(b"a").hexdigest(), hashlib.sha256(b"b").hexdigest())
_TIMEOUT_S = 60
_NEW_SET = hashlib.sha256(b"new").hexdigest()
_POINTER_BYTES = f"Dockerfile/{_NEW_SET}\n".encode()


@pytest.fixture(autouse=True)
def _hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's global and system git config out of every test."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)


def _git(repo: Path, *args: str) -> str:
    """Run git against ``repo``; its stdout, raising on failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )
    return proc.stdout.decode()


def _write(root: Path, rel: str, data: bytes) -> None:
    """Write ``data`` at ``root/rel``, creating parents."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _make_clone(
    root: Path, identity: bool = True, extra: tuple[tuple[str, bytes], ...] = ()
) -> Path:
    """A committed clone with a Dockerfile, two old provenance sets and
    ``extra`` files."""
    clone = root / "clone"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q", "-b", "main")
    if identity:
        _git(clone, "config", "user.name", "Clone User")
        _git(clone, "config", "user.email", "clone@example.com")
    _write(clone, "Dockerfile", _DOCKERFILE)
    _write(clone, "main.py", b"print('hi')\n")
    _write(clone, ".dockerignore", b".deployer/\n")
    _write(clone, _POINTER, _OLD_SETS[0].encode() + b"\n")
    for name in _OLD_SETS:
        for file in _SET_FILES:
            _write(clone, f"{_SET_DIR}/{name}/{file}", f"{name}/{file}\n".encode())
    for rel, data in extra:
        _write(clone, rel, data)
    _git(clone, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
        env=env,
    )
    return clone


def _bound() -> Bound:
    """The COPY instruction bound for ``missing_copy_source`` of ``app.py``."""
    defect = Defect(
        cls="missing_copy_source", file="Dockerfile", lines=(2, 2), object="app.py"
    )
    bound = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(bound, Bound)
    return bound


def _apply_fix(worktree: Path, dockerfile: bytes = _CORRECTED) -> None:
    """What steps 5 of §5.3 leave on disk: the edit and the re-issued set."""
    _write(worktree, "Dockerfile", dockerfile)
    _write(worktree, _POINTER, _POINTER_BYTES)
    for name in _OLD_SETS:
        shutil.rmtree(worktree / _SET_DIR / name)
    for file in _SET_FILES:
        _write(worktree, f"{_SET_DIR}/{_NEW_SET}/{file}", f"new/{file}\n".encode())


def _check(worktree: Path, expected: str = _NEW_SET) -> Vetted | str:
    """``allowed_diff_problem`` for the fixture's Dockerfile and plan."""
    return allowed_diff_problem(worktree, "Dockerfile", _bound(), expected)


def _vetted(worktree: Path) -> Vetted:
    """The vetted changes, asserting the check passed."""
    vetted = _check(worktree)
    assert isinstance(vetted, Vetted), vetted
    return vetted


def _snapshot(clone: Path) -> tuple[str, str, str]:
    """The clone's status, ``HEAD`` and current branch."""
    return (
        _git(clone, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        _git(clone, "rev-parse", "HEAD"),
        _git(clone, "symbolic-ref", "HEAD"),
    )


def _prepared(tmp_path: Path, base: Path | None = None) -> tuple[Path, FixDir, str]:
    """A clone, a new fix dir and a worktree at the clone's ``HEAD``."""
    clone = _make_clone(tmp_path / "work")
    attempt = (base or tmp_path / "runs") / "attempt-1"
    fix = new_fix_dir(attempt)
    head = _git(clone, "rev-parse", "HEAD").strip()
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    return clone, fix, branch


# --- the fix directory -----------------------------------------------------


def test_new_fix_dir_numbers_and_never_reuses(tmp_path: Path) -> None:
    """``001``, then ``002``, then ``003``; numbering follows the highest
    existing directory, exactly as R's ``_new_try`` does."""
    attempt = tmp_path / "attempt-1"
    first, second = new_fix_dir(attempt), new_fix_dir(attempt)
    assert (first.path.name, first.seq) == ("001", 1)
    assert (second.path.name, second.seq) == ("002", 2)
    assert first.path.parent == attempt / "fixes"
    (first.path / "fix.json").write_text("{}")
    third = new_fix_dir(attempt)
    assert third.path.name == "003"
    assert (first.path / "fix.json").read_text() == "{}"
    assert first.worktree == first.path / "worktree"


def test_new_fix_dir_after_a_removed_highest_keeps_counting(tmp_path: Path) -> None:
    """Numbering follows the highest existing number, like R's tries."""
    attempt = tmp_path / "attempt-1"
    (attempt / "fixes" / "007").mkdir(parents=True)
    assert new_fix_dir(attempt).path.name == "008"


def test_new_fix_dir_fix_id_is_a_fresh_uuid4(tmp_path: Path) -> None:
    """Each fix dir gets its own UUID4 ``fix_id``."""
    a, b = new_fix_dir(tmp_path), new_fix_dir(tmp_path)
    assert uuid.UUID(a.fix_id).version == 4
    assert a.fix_id != b.fix_id


def test_new_fix_dir_unwritable_raises(tmp_path: Path) -> None:
    """A path that cannot hold a directory is a typed error, not an OSError."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(FixDirError, match="cannot create a fix directory"):
        new_fix_dir(blocker)


def test_new_fix_dir_ignores_non_ascii_digit_names(tmp_path: Path) -> None:
    """A ``²`` entry (``str.isdigit`` but not ``int``-able) is not a number."""
    (tmp_path / "fixes" / "²").mkdir(parents=True)
    assert new_fix_dir(tmp_path).path.name == "001"


def test_new_fix_dir_retries_a_lost_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale listing (another creator took ``001``) is retried; five
    losses in a row are a typed error."""
    (tmp_path / "fixes" / "001").mkdir(parents=True)
    real = workspace._highest
    calls: list[int] = []

    def stale_once(fixes: Path) -> int:
        calls.append(1)
        return 0 if len(calls) == 1 else real(fixes)

    monkeypatch.setattr(workspace, "_highest", stale_once)
    assert new_fix_dir(tmp_path).path.name == "002"
    assert len(calls) == 2
    monkeypatch.setattr(workspace, "_highest", lambda fixes: 0)
    with pytest.raises(FixDirError, match="lost the race 5 times"):
        new_fix_dir(tmp_path)


def test_new_fix_dir_two_concurrent_calls(tmp_path: Path) -> None:
    """Two callers released together get two different directories."""
    for round_ in range(10):
        attempt = tmp_path / f"attempt-{round_}"
        barrier = threading.Barrier(2)

        def create(
            attempt: Path = attempt, barrier: threading.Barrier = barrier
        ) -> str:
            barrier.wait()
            return new_fix_dir(attempt).path.name

        with ThreadPoolExecutor(max_workers=2) as pool:
            names = sorted(pool.map(lambda _: create(), range(2)))
        assert names == ["001", "002"]


def test_branch_name_format() -> None:
    """``deployer/fix/<class, dashes>/<head12>-<NNN>``."""
    head = "0123456789abcdef0123"
    assert (
        branch_name("missing_copy_source", head, 2)
        == "deployer/fix/missing-copy-source/0123456789ab-002"
    )


# --- outside ---------------------------------------------------------------


def test_outside_compares_real_paths(tmp_path: Path) -> None:
    """Inside, the clone itself and a symlink into it are not outside."""
    clone = tmp_path / "clone"
    (clone / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(clone / "sub")
    assert outside(tmp_path / "elsewhere" / "wt", clone)
    assert not outside(clone, clone)
    assert not outside(clone / "sub" / "wt", clone)
    assert not outside(link / "wt", clone)
    assert not outside(tmp_path / "x" / ".." / "clone" / "wt", clone)
    assert outside(tmp_path / "clone-sibling", clone)


def test_outside_catches_a_case_variant_of_the_clone(tmp_path: Path) -> None:
    """On a case-insensitive volume ``.../CLONE/wt`` is inside ``.../clone``;
    ``add_worktree`` refuses it and nothing lands in the clone."""
    clone = _make_clone(tmp_path / "work")
    variant = clone.parent / "CLONE"
    if not variant.exists():
        pytest.skip("case-sensitive filesystem")
    assert not outside(variant / "wt", clone)
    assert not outside(variant, clone)
    head = _git(clone, "rev-parse", "HEAD").strip()
    reason = add_worktree(clone, variant / "wt", "deployer/fix/x/y-001", head)
    assert isinstance(reason, str) and "not outside the clone" in reason
    assert not (clone / "wt").exists()


# --- add_worktree ----------------------------------------------------------


def test_add_worktree_leaves_the_clone_untouched(tmp_path: Path) -> None:
    """The worktree is at ``head_sha`` on the new branch; the clone is not."""
    clone = _make_clone(tmp_path / "work")
    before = _snapshot(clone)
    head = before[1].strip()
    fix = new_fix_dir(tmp_path / "runs" / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    assert _git(fix.worktree, "rev-parse", "HEAD").strip() == head
    assert _git(fix.worktree, "symbolic-ref", "HEAD").strip() == f"refs/heads/{branch}"
    assert _snapshot(clone) == before


def test_add_worktree_refuses_a_path_inside_the_clone(tmp_path: Path) -> None:
    """Directly or through a symlink, the clone is not a place for it."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    link = tmp_path / "link"
    link.symlink_to(clone)
    for path in (clone / "wt", link / "wt"):
        reason = add_worktree(clone, path, "deployer/fix/x/y-001", head)
        assert isinstance(reason, str) and "not outside the clone" in reason
    assert "deployer/fix/x/y-001" not in _git(clone, "branch", "--list")


def test_add_worktree_refuses_an_existing_branch(tmp_path: Path) -> None:
    """A branch name already in use is refused before git is asked."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    _git(clone, "branch", "deployer/fix/taken/abc-001")
    reason = add_worktree(clone, tmp_path / "wt", "deployer/fix/taken/abc-001", head)
    assert reason == "branch deployer/fix/taken/abc-001 already exists"
    assert not (tmp_path / "wt").exists()


def test_add_worktree_refuses_bad_inputs(tmp_path: Path) -> None:
    """An unknown commit, a bad branch name and an existing path."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    unknown = add_worktree(clone, tmp_path / "wt", "b", "f" * 40)
    assert unknown is not None and "is not a commit" in unknown
    bad = add_worktree(clone, tmp_path / "wt", "bad..name", head)
    assert bad is not None and "not a valid branch name" in bad
    (tmp_path / "exists").mkdir()
    taken = add_worktree(clone, tmp_path / "exists", "b", head)
    assert taken is not None and "already exists" in taken


def test_add_worktree_checks_the_full_ref_name(tmp_path: Path) -> None:
    """Ruling 9: ``@{-1}`` passes ``check-ref-format --branch`` (it expands
    to the previous branch) but is no valid ``refs/heads/`` name."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    _git(clone, "switch", "-q", "-c", "previous")
    _git(clone, "switch", "-q", "main")
    reason = add_worktree(clone, tmp_path / "wt", "@{-1}", head)
    assert reason == "'@{-1}' is not a valid branch name"


def test_a_second_fix_dir_gets_002_and_its_own_branch(tmp_path: Path) -> None:
    """Review focus 4: two fixes on one attempt coexist."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    attempt = tmp_path / "runs" / "attempt-1"
    branches: list[str] = []
    for expected in ("001", "002"):
        fix = new_fix_dir(attempt)
        assert fix.path.name == expected
        branch = branch_name("missing_copy_source", head, fix.seq)
        assert add_worktree(clone, fix.worktree, branch, head) is None
        branches.append(branch)
    assert branches[0] != branches[1]
    assert branches[1].endswith("-002")
    listed = _git(clone, "branch", "--list", "deployer/*")
    assert all(branch in listed for branch in branches)


# --- allowed_diff_problem --------------------------------------------------


def test_the_allowed_diff_passes_unstaged(tmp_path: Path) -> None:
    """Exactly §3's changes, left unstaged, are allowed."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    blob = _git(fix.worktree, "hash-object", "--no-filters", "Dockerfile").strip()
    assert ("Dockerfile", blob) in vetted.changes
    deleted = [path for path, sha in vetted.changes if sha is None]
    assert len(deleted) == 6 and all(p.startswith(_SET_DIR) for p in deleted)
    assert len(vetted.changes) == 11


def test_the_allowed_diff_passes_staged(tmp_path: Path) -> None:
    """Staging does not change the verdict (both columns are read)."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _git(fix.worktree, "add", "-A")
    assert isinstance(_check(fix.worktree), Vetted)


def test_an_extra_changed_file_is_refused(tmp_path: Path) -> None:
    """A modified, staged or untracked extra path is named."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _write(fix.worktree, "main.py", b"print('changed')\n")
    assert _check(fix.worktree) == ("main.py: status ' M' is not an allowed change")
    _git(fix.worktree, "add", "main.py")
    reason = _check(fix.worktree)
    assert reason == "main.py: status 'M ' is not an allowed change"


def test_an_untracked_extra_file_is_refused(tmp_path: Path) -> None:
    """A new file outside the set, even in a spaced ü path, is named."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _write(fix.worktree, "dir with space ü/x.txt", b"x\n")
    reason = _check(fix.worktree)
    assert reason == "dir with space ü/x.txt: status '??' is not an allowed change"


def test_an_ignore_file_change_is_refused(tmp_path: Path) -> None:
    """No ignore-file edit, ever (§5.2)."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _write(fix.worktree, ".dockerignore", b".deployer/\n.git/\n")
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and reason.startswith(".dockerignore:")


def test_a_rename_is_refused(tmp_path: Path) -> None:
    """A staged rename is refused, naming a path of it."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _git(fix.worktree, "mv", "main.py", "renamed.py")
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and "R" in reason
    assert "renamed.py" in reason or "main.py" in reason


def test_a_dockerfile_change_outside_the_span_is_refused(tmp_path: Path) -> None:
    """``link_problem`` sees a change on another instruction."""
    _, fix, _ = _prepared(tmp_path)
    outside_span = _CORRECTED.replace(b"RUN echo hi", b"RUN echo ho")
    _apply_fix(fix.worktree, outside_span)
    reason = _check(fix.worktree)
    assert reason == "Dockerfile: instruction 2 changed outside the bound span"


def test_a_dockerfile_mode_change_is_refused(tmp_path: Path) -> None:
    """A chmod on the Dockerfile, on disk or only staged, is refused."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    (fix.worktree / "Dockerfile").chmod(0o755)
    reason = _check(fix.worktree)
    assert reason == "Dockerfile: mode change 100644 -> 100755"
    (fix.worktree / "Dockerfile").chmod(0o644)
    _git(fix.worktree, "update-index", "--chmod=+x", "Dockerfile")
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and "mode change" in reason
    (fix.worktree / "Dockerfile").chmod(0o755)
    reason = _check(fix.worktree)
    assert reason == "Dockerfile: mode change 100644 -> 100755"


def test_an_executable_new_set_file_is_refused(tmp_path: Path) -> None:
    """New set files must be regular and non-executable."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    record = fix.worktree / _SET_DIR / _NEW_SET / "record.json"
    record.chmod(0o755)
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and reason.startswith(
        f"{_SET_DIR}/{_NEW_SET}/record"
    )


def test_a_missing_pointer_change_is_refused(tmp_path: Path) -> None:
    """The pointer must be modified."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _git(fix.worktree, "checkout", "--", _POINTER)
    reason = _check(fix.worktree)
    assert reason == f"{_POINTER}: expected a modification, found none"


def test_an_old_set_left_in_place_is_refused(tmp_path: Path) -> None:
    """Every file of every other set dir must be deleted."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    _git(fix.worktree, "checkout", "--", f"{_SET_DIR}/{_OLD_SETS[1]}")
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and reason.startswith(f"{_SET_DIR}/{_OLD_SETS[1]}/")
    assert reason.endswith("an old set file is not deleted")


def test_two_new_sets_are_refused(tmp_path: Path) -> None:
    """Exactly one new set directory."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    other = hashlib.sha256(b"other").hexdigest()
    for file in _SET_FILES:
        _write(fix.worktree, f"{_SET_DIR}/{other}/{file}", b"x\n")
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and "exactly one new provenance set" in reason


def test_an_incomplete_or_foreign_set_file_is_refused(tmp_path: Path) -> None:
    """A missing set file, or a file that is not one of the three."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    (fix.worktree / _SET_DIR / _NEW_SET / "record.json.sig").unlink()
    reason = _check(fix.worktree)
    assert isinstance(reason, str) and "missing record.json.sig" in reason
    _write(fix.worktree, f"{_SET_DIR}/{_NEW_SET}/extra.txt", b"x\n")
    reason = _check(fix.worktree)
    assert reason == f"{_SET_DIR}/{_NEW_SET}/extra.txt: not a file of a provenance set"


def test_the_new_set_must_be_the_planned_one(tmp_path: Path) -> None:
    """Ruling 7: the new set directory is named ``expected_record_sha``."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    planned = hashlib.sha256(b"planned").hexdigest()
    reason = _check(fix.worktree, expected=planned)
    assert reason == (
        f"expected exactly one new provenance set, {planned}; found {_NEW_SET}"
    )


def test_the_pointer_must_name_the_planned_set(tmp_path: Path) -> None:
    """Ruling 7: the pointer's bytes are exactly ``Dockerfile/<sha>\\n``."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    for wrong in (f"Dockerfile/{_OLD_SETS[0]}\n", f"Dockerfile/{_NEW_SET}\r\n"):
        _write(fix.worktree, _POINTER, wrong.encode())
        reason = _check(fix.worktree)
        assert reason == f"{_POINTER}: does not name the planned set {_NEW_SET}"


def test_a_stray_file_under_the_set_parent_may_not_be_deleted(
    tmp_path: Path,
) -> None:
    """Ruling 8: only ``<64 hex>/<set file>`` deletions; a stray file that
    A's ``issue`` leaves alone is allowed to stay."""
    stray = f"{_SET_DIR}/README"
    clone = _make_clone(tmp_path / "work", extra=((stray, b"notes\n"),))
    head = _git(clone, "rev-parse", "HEAD").strip()
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    _apply_fix(fix.worktree)
    assert isinstance(_check(fix.worktree), Vetted)
    (fix.worktree / stray).unlink()
    assert _check(fix.worktree) == f"{stray}: not a file of a provenance set"


# --- commit and the whole flow ---------------------------------------------


def test_the_whole_flow_commits_and_leaves_the_clone_untouched(
    tmp_path: Path,
) -> None:
    """Review focus 3: under a spaced ü path; the clone's status, ``HEAD``
    and branch unchanged; the commit holds exactly §3's changes."""
    base = tmp_path / "dir with space ü"
    clone = _make_clone(tmp_path / "work")
    before = _snapshot(clone)
    head = before[1].strip()
    fix = new_fix_dir(base / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    committed = commit(
        fix.worktree, "fix(deployer): remove the admitted defect", vetted
    )
    assert committed.index_synced and committed.detail is None
    sha = committed.sha
    assert _git(clone, "rev-parse", f"refs/heads/{branch}").strip() == sha
    assert _git(fix.worktree, "rev-parse", "HEAD^").strip() == head
    assert _git(fix.worktree, "status", "--porcelain").strip() == ""
    changed = _git(fix.worktree, "diff", "--name-status", "--no-renames", head, sha)
    rows = sorted(line.split("\t") for line in changed.splitlines())
    expected = [["M", "Dockerfile"], ["M", _POINTER]]
    expected += [["A", f"{_SET_DIR}/{_NEW_SET}/{f}"] for f in _SET_FILES]
    expected += [["D", f"{_SET_DIR}/{n}/{f}"] for n in _OLD_SETS for f in _SET_FILES]
    assert rows == sorted(expected)
    assert _git(fix.worktree, "show", f"{sha}:Dockerfile").encode() == _CORRECTED
    tree = _git(fix.worktree, "ls-tree", "-r", sha, "--", "Dockerfile", _SET_DIR)
    assert all(line.startswith("100644 blob ") for line in tree.splitlines())
    assert _snapshot(clone) == before


def test_commit_is_always_the_deployer_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling J: a clone with ``Clone User`` configured, and an environment
    naming someone else, still commits as ``deployer`` in both roles."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "env@example.com")
    sha = commit(fix.worktree, "m", vetted).sha
    who = _git(fix.worktree, "log", "-1", "--format=%an <%ae>|%cn <%ce>", sha)
    ident = f"{DEPLOYER_NAME} <{DEPLOYER_EMAIL}>"
    assert who.strip() == f"{ident}|{ident}"


def test_commit_without_any_configured_identity(tmp_path: Path) -> None:
    """No ``user.*`` anywhere: the commit still succeeds as ``deployer``."""
    clone = _make_clone(tmp_path / "work", identity=False)
    head = _git(clone, "rev-parse", "HEAD").strip()
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("from_argument_count", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    _apply_fix(fix.worktree)
    sha = commit(fix.worktree, "m", _vetted(fix.worktree)).sha
    who = _git(fix.worktree, "log", "-1", "--format=%an <%ae>", sha)
    assert who.strip() == f"{DEPLOYER_NAME} <{DEPLOYER_EMAIL}>"


def test_a_file_rewritten_after_the_check_is_refused(tmp_path: Path) -> None:
    """Ruling K: the blob committed is the blob checked, or nothing."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    head = _git(fix.worktree, "rev-parse", "HEAD").strip()
    _write(fix.worktree, "Dockerfile", _CORRECTED + b"RUN evil\n")
    with pytest.raises(CommitError, match="Dockerfile: changed since it was checked"):
        commit(fix.worktree, "m", vetted)
    assert _git(fix.worktree, "rev-parse", "HEAD").strip() == head


def test_a_path_added_after_the_check_is_refused(tmp_path: Path) -> None:
    """The status path set is re-read; a new path, or one reverted and
    staged, stops the commit."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    _write(fix.worktree, "main.py", b"evil\n")
    with pytest.raises(CommitError, match="differ from the vetted ones"):
        commit(fix.worktree, "m", vetted)
    _git(fix.worktree, "add", "main.py")
    _write(fix.worktree, "main.py", b"print('hi')\n")
    with pytest.raises(CommitError, match="main.py"):
        commit(fix.worktree, "m", vetted)


def test_commit_never_runs_user_programs(tmp_path: Path) -> None:
    """Ruling 2: spy clean/smudge/process filters (one with a dotted name,
    one ``required``) and a spy fsmonitor never run during add, check or
    commit; the committed bytes are the raw bytes checked."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    marker = tmp_path / "marker"
    # Every spy terminates on its own: clean/smudge read stdin to EOF, the
    # process spy exits at once (it would otherwise wait for a handshake).
    spy = f"sh -c 'echo ran >> \"{marker}\"; tr a-z A-Z'"
    process_spy = f"sh -c 'echo ran >> \"{marker}\"; exit 1'"
    for name in ("spy", "a.b"):
        for key in ("clean", "smudge"):
            _git(clone, "config", f"filter.{name}.{key}", spy)
        _git(clone, "config", f"filter.{name}.process", process_spy)
        _git(clone, "config", f"filter.{name}.required", "true")
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 1\n")
    monitor.chmod(0o755)
    _git(clone, "config", "core.fsmonitor", str(monitor))
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text(
        "* filter=spy\n*.json filter=a.b\n"
    )
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    assert not marker.exists()
    assert (fix.worktree / "Dockerfile").read_bytes() == _DOCKERFILE
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    assert not marker.exists()
    sha = commit(fix.worktree, "m", vetted).sha
    assert not marker.exists()
    blob = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "blob", f"{sha}:Dockerfile"],
        check=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    ).stdout
    assert blob == _CORRECTED


_HOOK_EVENTS = (
    "post-checkout",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "post-index-change",
    "reference-transaction",
)


def _identity_env() -> dict[str, str]:
    """A test-side identity for helper commits."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e"}
    return env | {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def test_hooks_do_not_run(tmp_path: Path) -> None:
    """No file hook and no ``hook.*`` config hook runs, on any event,
    during add, check or commit (round 2, finding 2)."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    marker = tmp_path / "hook-ran"
    hooks = clone / ".git" / "hooks"
    for hook in _HOOK_EVENTS:
        (hooks / hook).write_text(f"#!/bin/sh\necho file:{hook} >> '{marker}'\n")
        (hooks / hook).chmod(0o755)
    _git(clone, "config", "hook.spy.command", f"echo config >> '{marker}'")
    for event in _HOOK_EVENTS:
        _git(clone, "config", "--add", "hook.spy.event", event)
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    _apply_fix(fix.worktree)
    sha = commit(fix.worktree, "m", _vetted(fix.worktree)).sha
    assert len(sha) == 40
    assert not marker.exists()
    # The spy itself works: plain git runs it.
    _git(fix.worktree, "commit", "--allow-empty", "-q", "-m", "probe")
    assert marker.exists()


def test_a_prepare_commit_msg_config_hook_cannot_tamper(tmp_path: Path) -> None:
    """Probe ``p_hooktamper``: a config hook that rewrites the message and
    stages ``evil.txt`` never runs; the commit is the vetted one."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    hook = tmp_path / "h.sh"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'INJECTED' > \"$1\"\n"
        "b=$(echo evil | git hash-object -w --stdin)\n"
        "git update-index --add --cacheinfo 100644,$b,evil.txt\n"
    )
    hook.chmod(0o755)
    _git(clone, "config", "hook.t.command", str(hook))
    _git(clone, "config", "hook.t.event", "prepare-commit-msg")
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    _apply_fix(fix.worktree)
    sha = commit(fix.worktree, "the real message", _vetted(fix.worktree)).sha
    assert _git(fix.worktree, "log", "-1", "--format=%B", sha).strip() == (
        "the real message"
    )
    assert "evil.txt" not in _git(fix.worktree, "ls-tree", "-r", "--name-only", sha)
    assert "evil.txt" not in _git(fix.worktree, "ls-files")


def test_a_post_commit_config_hook_cannot_amend(tmp_path: Path) -> None:
    """Probe ``p_postcommit``: an amending ``post-commit`` config hook never
    runs; the returned sha is the branch tip and holds no unvetted file."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    hook = tmp_path / "h.sh"
    hook.write_text(
        "#!/bin/sh\n"
        '[ -n "$AMENDED" ] && exit 0\n'
        "b=$(echo evil | git hash-object -w --stdin)\n"
        "git update-index --add --cacheinfo 100644,$b,evil.txt\n"
        "AMENDED=1 git commit -q --amend --no-edit\n"
    )
    hook.chmod(0o755)
    _git(clone, "config", "hook.t.command", str(hook))
    _git(clone, "config", "hook.t.event", "post-commit")
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    _apply_fix(fix.worktree)
    sha = commit(fix.worktree, "msg", _vetted(fix.worktree)).sha
    assert _git(clone, "rev-parse", f"refs/heads/{branch}").strip() == sha
    assert "evil.txt" not in _git(fix.worktree, "ls-tree", "-r", "--name-only", sha)
    ident = f"{DEPLOYER_NAME} <{DEPLOYER_EMAIL}>"
    who = _git(fix.worktree, "log", "-1", "--format=%an <%ae>|%cn <%ce>", sha)
    assert who.strip() == f"{ident}|{ident}"


def test_an_onbranch_filter_and_hook_never_run(tmp_path: Path) -> None:
    """Probe ``p_onbranch``: a filter and a hook included only on
    ``deployer/**`` branches are read inside the new worktree and
    neutralised before any file is checked out."""
    clone = _make_clone(tmp_path / "work")
    head = _git(clone, "rev-parse", "HEAD").strip()
    marker = tmp_path / "marker"
    spy = tmp_path / "spy.sh"
    spy.write_text(f"#!/bin/sh\necho ran:$1 >> '{marker}'\ncat\n")
    spy.chmod(0o755)
    include = tmp_path / "inc.cfg"
    include.write_text(
        f'[filter "evil"]\n\tsmudge = {spy} smudge\n\tclean = {spy} clean\n'
        "\trequired = true\n"
        f'[hook "evil"]\n\tcommand = {spy} hook\n'
        "\tevent = post-checkout\n\tevent = post-index-change\n"
        "\tevent = reference-transaction\n"
    )
    _git(clone, "config", "includeIf.onbranch:deployer/**.path", str(include))
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    fix = new_fix_dir(tmp_path / "attempt-1")
    branch = branch_name("missing_copy_source", head, fix.seq)
    assert add_worktree(clone, fix.worktree, branch, head) is None
    assert not marker.exists()
    assert (fix.worktree / "Dockerfile").read_bytes() == _DOCKERFILE
    _apply_fix(fix.worktree)
    sha = commit(fix.worktree, "m", _vetted(fix.worktree)).sha
    assert not marker.exists()
    assert _git(fix.worktree, "show", f"{sha}:Dockerfile").encode() == _CORRECTED


def test_commit_refuses_a_branch_that_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``update-ref`` is compare-and-swap: a branch moved after the
    re-check is not overwritten."""
    _, fix, branch = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    monkeypatch.setattr(workspace, "_recheck", lambda *args: None)
    moved = (
        subprocess.run(
            [
                "git",
                "-C",
                str(fix.worktree),
                "commit-tree",
                "HEAD^{tree}",
                "-p",
                "HEAD",
            ],
            input=b"elsewhere",
            check=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
            env=_identity_env(),
        )
        .stdout.decode()
        .strip()
    )
    _git(fix.worktree, "update-ref", f"refs/heads/{branch}", moved)
    with pytest.raises(CommitError, match="git update-ref failed"):
        commit(fix.worktree, "m", vetted)
    assert _git(fix.worktree, "rev-parse", f"refs/heads/{branch}").strip() == moved


def test_commit_leaves_no_temporary_index(tmp_path: Path) -> None:
    """The temporary index lives in the fix directory and is removed; the
    worktree's own index ends in sync with the new commit."""
    _, fix, _ = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    sha = commit(fix.worktree, "m", _vetted(fix.worktree)).sha
    assert [p.name for p in fix.path.iterdir()] == ["worktree"]
    assert _git(fix.worktree, "diff", "--cached", "--name-only", sha) == ""


def test_commit_with_nothing_vetted_raises(tmp_path: Path) -> None:
    """An empty change set is a typed error, not a silent no-op."""
    _, fix, _ = _prepared(tmp_path)
    head = _git(fix.worktree, "rev-parse", "HEAD").strip()
    with pytest.raises(CommitError, match="nothing to commit"):
        commit(fix.worktree, "m", Vetted(head=head, changes=()))


def test_commit_outside_a_repository_raises(tmp_path: Path) -> None:
    """Git failures surface as ``CommitError``."""
    (tmp_path / "plain").mkdir()
    with pytest.raises(CommitError, match="git rev-parse failed"):
        commit(tmp_path / "plain", "m", Vetted(head="0" * 40, changes=()))


def test_a_tree_with_an_unvetted_entry_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``diff-tree`` verifies the built tree: an entry slipped into the
    temporary index stops the commit before ``commit-tree``."""
    _, fix, branch = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    before = _git(fix.worktree, "rev-parse", f"refs/heads/{branch}")
    evil = (
        subprocess.run(
            ["git", "-C", str(fix.worktree), "hash-object", "-w", "--stdin"],
            input=b"evil\n",
            check=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
        .stdout.decode()
        .strip()
    )
    real_run = workspace._run

    def slipping_run(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if args[:2] == ("update-index", "--add"):
            args = (*args, "--cacheinfo", f"100644,{evil},evil.txt")
        return real_run(cwd, *args, **kwargs)

    monkeypatch.setattr(workspace, "_run", slipping_run)
    with pytest.raises(CommitError, match="built tree differs"):
        commit(fix.worktree, "m", vetted)
    assert _git(fix.worktree, "rev-parse", f"refs/heads/{branch}") == before


def test_an_unusable_fix_dir_is_a_commit_error(tmp_path: Path) -> None:
    """An ``OSError`` creating the temporary index is a ``CommitError``,
    and the branch does not move."""
    _, fix, branch = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    before = _git(fix.worktree, "rev-parse", f"refs/heads/{branch}")
    fix.path.chmod(0o555)
    try:
        with pytest.raises(CommitError, match="cannot use a temporary index"):
            commit(fix.worktree, "m", vetted)
    finally:
        fix.path.chmod(0o755)
    assert _git(fix.worktree, "rev-parse", f"refs/heads/{branch}") == before


def test_a_failed_index_sync_still_returns_the_commit(tmp_path: Path) -> None:
    """Once the branch points at the vetted commit, a held ``index.lock``
    only makes the sync fail: reported, not raised."""
    _, fix, branch = _prepared(tmp_path)
    _apply_fix(fix.worktree)
    vetted = _vetted(fix.worktree)
    lock = Path(_git(fix.worktree, "rev-parse", "--git-path", "index.lock").strip())
    lock = lock if lock.is_absolute() else fix.worktree / lock
    lock.write_text("")
    try:
        committed = commit(fix.worktree, "m", vetted)
    finally:
        lock.unlink()
    assert not committed.index_synced
    assert committed.detail is not None and "index.lock" in committed.detail
    tip = _git(fix.worktree, "rev-parse", f"refs/heads/{branch}").strip()
    assert tip == committed.sha
    assert _git(fix.worktree, "rev-parse", "HEAD^").strip() == vetted.head


# Ruling U: replace refs never substitute an object ---------------------------


def _replaced_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose commit ``A`` (``Dockerfile`` = ``real``) is replaced by
    ``B`` (``forged``) through a real ``refs/replace/<A>``; returns ``A``."""
    repo = tmp_path / "replaced"
    repo.mkdir()

    def run(*args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x"}
        env.update(GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        env.pop("GIT_NO_REPLACE_OBJECTS", None)
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    run("init", "-q")
    (repo / "Dockerfile").write_text("real\n")
    run("add", "Dockerfile")
    run("commit", "-q", "-m", "a")
    a = run("rev-parse", "HEAD")
    (repo / "Dockerfile").write_text("forged\n")
    run("commit", "-q", "-am", "b")
    run("replace", a, run("rev-parse", "HEAD"))
    assert run("cat-file", "blob", f"{a}:Dockerfile") == "forged"  # git honours it
    return repo, a


def test_guarded_read_ignores_replace_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every fix-side read (``workspace._run``: confirm, publish, workspace)
    returns the object named, even with an inherited replace-ref base."""
    repo, a = _replaced_repo(tmp_path)
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace/")
    guards = workspace._guards(repo)
    assert not isinstance(guards, str)
    result = workspace._run(repo, "cat-file", "blob", f"{a}:Dockerfile", g=guards)
    assert (result.code, result.stdout) == (0, b"real\n")


def test_gitrepo_read_ignores_replace_refs(tmp_path: Path) -> None:
    """``provenance.gitrepo._git`` (``blob_bytes``) ignores replace refs too."""
    repo, a = _replaced_repo(tmp_path)
    assert gitrepo.blob_bytes(repo, a, "Dockerfile") == b"real\n"


# Partial clones: no lazy fetch, and refused outright --------------------------


def test_guarded_env_forbids_lazy_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared guarded environment carries ``GIT_NO_LAZY_FETCH=1``, and
    ``workspace._run`` passes it to git even over an inherited ``0``."""
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    assert gitrepo.no_replace_env({})["GIT_NO_LAZY_FETCH"] == "1"
    seen: dict[str, str] = {}

    def fake_run(*_: Any, env: dict[str, str], **__: Any) -> Any:
        seen.update(env)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    workspace._run(Path("."), "status", g=())
    assert seen["GIT_NO_LAZY_FETCH"] == "1"
    assert seen["GIT_NO_REPLACE_OBJECTS"] == "1"


def _plain_repo(tmp_path: Path) -> Path:
    """An empty non-partial repository."""
    repo = tmp_path / "plain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=60)
    return repo


@pytest.mark.parametrize(
    ("key", "value", "refused"),
    [
        ("extensions.partialClone", "origin", True),
        ("remote.origin.promisor", "true", True),
        ("remote.origin.promisor", "false", False),
    ],
    ids=["ext", "promisor", "promisor-off"],
)
def test_partial_clone_config(
    tmp_path: Path, key: str, value: str, refused: bool
) -> None:
    repo = _plain_repo(tmp_path)
    assert workspace.partial_clone_problem(repo) is None
    _git(repo, "config", key, value)
    reason = workspace.partial_clone_problem(repo)
    assert (reason is not None) == refused
    if refused:
        assert reason is not None and "is a partial clone" in reason


def test_real_partial_clone_refused_by_guards(tmp_path: Path) -> None:
    """``_guards`` — which every publish/confirm/workspace git command reads
    first — refuses a real partial clone and a linked worktree of it."""
    source = _plain_repo(tmp_path)
    (source / "Dockerfile").write_text("FROM scratch\n")
    _git(source, "add", "Dockerfile")
    _git(source, "config", "user.name", "t")
    _git(source, "config", "user.email", "t@x")
    _git(source, "commit", "-qm", "a")
    partial = tmp_path / "partial"
    partial_clone(source, partial)
    reason = workspace._guards(partial)
    assert isinstance(reason, str) and "is a partial clone" in reason
    linked = tmp_path / "linked"
    _git(partial, "worktree", "add", "-q", "--detach", str(linked))
    reason = workspace._guards(linked)
    assert isinstance(reason, str) and "is a partial clone" in reason
