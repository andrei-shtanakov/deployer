"""Issuing: preflight gates, exclusion, immutable dirs, atomic pointer,
reuse, withdraw."""

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from deployer.provenance import gitrepo, issue, sshsig
from deployer.provenance.model import POINTER, SET_ROOT, Record, sha256_hex
from tests.provenance.conftest import make_key

DOCKERFILE = "FROM python:3.12-slim\nCOPY src ./src\n"


def _author(repo: Path, text: str = DOCKERFILE) -> None:
    (repo / "Dockerfile").write_text(text)


def _written(pre: issue.Preflight) -> bytes:
    """The bytes the simulated authoring run wrote."""
    return (pre.project / "Dockerfile").read_bytes()


def _pointer(repo: Path) -> str:
    return (repo / SET_ROOT / POINTER).read_text().strip()


def test_issue_publishes_a_verifiable_set(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, pub = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert out.published and out.set_dir == _pointer(repo_with_origin)
    assert out.set_dir is not None
    set_dir = repo_with_origin / SET_ROOT / out.set_dir
    rec = (set_dir / "record.json").read_bytes()
    assert set_dir.name == sha256_hex(rec)
    assert Record.model_validate_json(rec).artifact_sha256 == sha256_hex(
        (repo_with_origin / "Dockerfile").read_bytes()
    )
    sig = (set_dir / "record.json.sig").read_bytes()
    assert sshsig.verify_with_public_key(rec, sig, pub).ok
    assert ".deployer/" in (repo_with_origin / ".dockerignore").read_text()


def test_preflight_refusals(
    repo: Path,
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    tmp_path: Path,
) -> None:
    key, _ = keypair
    reason = issue.preflight(repo, key)
    assert isinstance(reason, str) and "origin" in reason
    reason = issue.preflight(tmp_path, key)
    assert isinstance(reason, str) and "not a Git checkout" in reason
    reason = issue.preflight(repo_with_origin, None)
    assert isinstance(reason, str) and "signing key" in reason
    (repo_with_origin / "Dockerfile").write_text("hand edit\n")  # untracked
    reason = issue.preflight(repo_with_origin, key)
    assert isinstance(reason, str) and "dirty" in reason


def test_preflight_refuses_outside_the_repository_root(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    reason = issue.preflight(repo_with_origin / "src", key)
    assert isinstance(reason, str) and "repository root" in reason


def test_preflight_reports_an_unreadable_signing_key(
    repo_with_origin: Path, tmp_path: Path
) -> None:
    bad_key = tmp_path / "bad"
    bad_key.write_text("not a key\n")
    bad_key.chmod(0o000)
    try:
        reason = issue.preflight(repo_with_origin, bad_key)
    finally:
        bad_key.chmod(0o600)
    assert isinstance(reason, str) and "ssh-keygen" in reason


def test_preflight_reports_ssh_keygen_unusable_without_path(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PATH keeps `git` (preflight needs it first) but drops `ssh-keygen`.
    key, _ = keypair
    git_path = shutil.which("git")
    assert git_path is not None
    git_only = tmp_path / "git_only"
    git_only.mkdir()
    (git_only / "git").symlink_to(git_path)
    monkeypatch.setenv("PATH", str(git_only))
    reason = issue.preflight(repo_with_origin, key)
    assert isinstance(reason, str) and "ssh-keygen" in reason


def test_a_fact_from_an_untracked_ignored_file_blocks_the_set(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    (repo_with_origin / ".gitignore").write_text(".python-version\n")
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_with_origin), "commit", "-qm", "ig"], check=True
    )
    # ignored, but read by facts
    (repo_with_origin / ".python-version").write_text("3.11\n")
    reason = issue.preflight(repo_with_origin, key)
    assert isinstance(reason, str) and "facts" in reason


def test_interrupted_before_the_pointer_keeps_the_old_set(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # Review Focus 3
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1", _written(pre))
    assert first.set_dir is not None
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_with_origin), "commit", "-qm", "a"], check=True
    )
    pre2 = issue.preflight(repo_with_origin, key)
    assert isinstance(pre2, issue.Preflight)
    _author(repo_with_origin, DOCKERFILE + 'CMD ["python"]\n')
    real_rename = os.rename

    def boom(
        src: str | os.PathLike[str], dst: str | os.PathLike[str], **kw: int | None
    ) -> None:
        if str(dst).endswith(POINTER):
            raise OSError("killed")
        return real_rename(src, dst, **kw)

    monkeypatch.setattr(os, "rename", boom)
    try:
        issue.issue(pre2, key, "0.1", _written(pre2))
    except OSError:
        pass
    assert _pointer(repo_with_origin) == first.set_dir  # old set still named, intact
    rec = Record.model_validate_json(
        (repo_with_origin / SET_ROOT / first.set_dir / "record.json").read_bytes()
    )
    assert rec.artifact_sha256 != sha256_hex(
        (repo_with_origin / "Dockerfile").read_bytes()
    )
    # the failed pointer swap leaves no stray temp pointer file behind
    leftovers = list((repo_with_origin / SET_ROOT).glob(".tmp-pointer-*"))
    assert leftovers == []


def test_reissuing_the_same_record_reuses_without_writing(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1", _written(pre))
    assert first.set_dir is not None
    set_dir = repo_with_origin / SET_ROOT / first.set_dir
    mtimes = {p.name: p.stat().st_mtime_ns for p in set_dir.iterdir()}
    again = issue.issue(pre, key, "0.1", _written(pre))
    assert again.published and again.set_dir == first.set_dir
    assert {p.name: p.stat().st_mtime_ns for p in set_dir.iterdir()} == mtimes
    (set_dir / "snapshot.json").write_text("{}")  # tampered
    assert not issue.issue(pre, key, "0.1", _written(pre)).published


def test_withdraw_removes_pointer_then_dirs(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    issue.issue(pre, key, "0.1", _written(pre))
    assert issue.withdraw(repo_with_origin)
    assert not (repo_with_origin / SET_ROOT / POINTER).exists()
    assert not any((repo_with_origin / SET_ROOT / "Dockerfile").iterdir())


def test_exclusion_not_provable_blocks_the_set(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    (repo_with_origin / ".dockerignore").write_text("[ab]\n")  # unmodelled pattern
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_with_origin), "commit", "-qm", "d"], check=True
    )
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    before = (repo_with_origin / ".dockerignore").read_bytes()
    assert "exclusion" in (issue.issue(pre, key, "0.1", _written(pre)).reason or "")
    # refused before writing: the unsupported file is left untouched
    assert (repo_with_origin / ".dockerignore").read_bytes() == before


def test_reuse_refuses_without_raising_when_the_set_dir_is_incomplete(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1", _written(pre))
    assert first.set_dir is not None
    set_dir = repo_with_origin / SET_ROOT / first.set_dir
    for child in set_dir.iterdir():
        child.unlink()
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert not out.published
    assert out.reason is not None and "does not match" in out.reason


def test_preflight_reports_a_git_error_from_dirty_paths(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, _ = keypair

    def boom(path: Path) -> list[str]:
        raise gitrepo.GitError("boom")

    monkeypatch.setattr(issue.gitrepo, "dirty_paths", boom)
    reason = issue.preflight(repo_with_origin, key)
    assert isinstance(reason, str) and "boom" in reason


def test_prune_skips_stray_files_and_live_tmp_dirs(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1", _written(pre))
    assert first.published
    parent = repo_with_origin / SET_ROOT / "Dockerfile"
    (parent / "stray.txt").write_text("x")
    (parent / ".tmp-x-1").mkdir()
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_with_origin), "commit", "-qm", "s"], check=True
    )
    pre2 = issue.preflight(repo_with_origin, key)
    assert isinstance(pre2, issue.Preflight)
    _author(repo_with_origin, DOCKERFILE + 'CMD ["python"]\n')
    second = issue.issue(pre2, key, "0.1", _written(pre2))
    assert second.published
    assert (parent / "stray.txt").is_file()
    assert (parent / ".tmp-x-1").is_dir()


def test_reuse_refuses_a_tampered_signature(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1", _written(pre))
    assert first.set_dir is not None
    sig_path = repo_with_origin / SET_ROOT / first.set_dir / "record.json.sig"
    sig_path.write_bytes(b"not a signature")
    assert not issue.issue(pre, key, "0.1", _written(pre)).published


def test_reuse_refuses_a_signature_from_a_different_key(
    repo_with_origin: Path, keypair: tuple[Path, str], tmp_path: Path
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1", _written(pre))
    assert first.published
    other_key, _ = make_key(tmp_path, "other")
    assert not issue.issue(pre, other_key, "0.1", _written(pre)).published


def test_reuse_refuses_when_the_key_becomes_unusable(
    repo_with_origin: Path, keypair: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    assert issue.issue(pre, key, "0.1", _written(pre)).published

    def broken(_key: Path) -> str:
        raise sshsig.SshSigError("ssh-keygen -y failed: gone")

    monkeypatch.setattr(sshsig, "public_key", broken)
    assert not issue.issue(pre, key, "0.1", _written(pre)).published


def test_concurrent_issue_calls_serialize_and_leave_one_consistent_set(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two `author` processes racing to publish must not interleave: the
    reuse-versus-write decision, the pointer swap and the prune all run
    under one lock per repository. Reproduced deterministically by
    hooking `_replace_pointer` in the first call to prove a second,
    concurrent `issue()` for the same repo blocks until the first
    releases the lock (Review Focus, PR #85)."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    real_replace_pointer = issue._replace_pointer
    second_result: list[issue.Issued] = []
    second_thread: list[threading.Thread] = []
    triggered = False

    def hook(auth_fd: int, rec_sha: str) -> None:
        nonlocal triggered
        if triggered:
            # the second call's own pointer swap: no more hooking
            real_replace_pointer(auth_fd, rec_sha)
            return
        triggered = True
        _author(repo_with_origin, DOCKERFILE + 'CMD ["python"]\n')

        def run_second() -> None:
            second_result.append(issue.issue(pre, key, "0.2", _written(pre)))

        thread = threading.Thread(target=run_second)
        second_thread.append(thread)
        thread.start()
        # the first call still holds the lock at this point, so the
        # second must still be blocked trying to acquire it
        thread.join(timeout=0.2)
        assert thread.is_alive()
        real_replace_pointer(auth_fd, rec_sha)

    monkeypatch.setattr(issue, "_replace_pointer", hook)
    first = issue.issue(pre, key, "0.1", _written(pre))
    thread = second_thread[0]
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first.published
    assert second_result and second_result[0].published

    pointer = _pointer(repo_with_origin)
    set_dir = repo_with_origin / SET_ROOT / pointer
    assert set_dir.is_dir()
    for name in ("record.json", "snapshot.json", "record.json.sig"):
        assert (set_dir / name).is_file()
    rec_bytes = (set_dir / "record.json").read_bytes()
    assert set_dir.name == sha256_hex(rec_bytes)
    rec = Record.model_validate_json(rec_bytes)
    assert rec.artifact_sha256 == sha256_hex(
        (repo_with_origin / "Dockerfile").read_bytes()
    )
    remaining = list((repo_with_origin / SET_ROOT / "Dockerfile").iterdir())
    assert len(remaining) == 1


def test_lock_file_is_not_dirty_after_issuing(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert out.published
    lock_path = gitrepo.git_path(repo_with_origin, issue._LOCK_FILE_NAME)
    assert lock_path.is_file()
    dirty = gitrepo.dirty_paths(repo_with_origin)
    assert not any(issue._LOCK_FILE_NAME in p for p in dirty)


def test_issue_refuses_when_the_lock_cannot_be_acquired(
    repo_with_origin: Path, keypair: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)

    def boom(path: Path, name: str) -> Path:
        raise gitrepo.GitError("no .git here")

    monkeypatch.setattr(issue.gitrepo, "git_path", boom)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert not out.published
    assert out.reason is not None and "lock" in out.reason


def test_withdraw_still_removes_when_the_lock_cannot_be_acquired(
    repo_with_origin: Path, keypair: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §5.3: a normally completed run leaves no old confirmation, so a
    lock failure must not keep the previous set (PR #85 review)."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    assert issue.issue(pre, key, "0.1", _written(pre)).published

    def boom(path: Path, name: str) -> Path:
        raise gitrepo.GitError("no .git here")

    monkeypatch.setattr(issue.gitrepo, "git_path", boom)
    assert issue.withdraw(repo_with_origin) is True
    assert not (repo_with_origin / SET_ROOT / POINTER).exists()
    assert not any((repo_with_origin / SET_ROOT / "Dockerfile").iterdir())


def test_a_stale_run_does_not_replace_the_set_of_a_newer_dockerfile(
    repo_with_origin: Path, keypair: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run A read its Dockerfile, then run B rewrote and published before A
    took the lock: A must publish nothing (PR #85 review)."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    real_acquire = issue._acquire_publication_lock
    newer = DOCKERFILE + 'CMD ["python"]\n'

    def b_publishes_first(project: Path) -> int:
        monkeypatch.setattr(issue, "_acquire_publication_lock", real_acquire)
        _author(repo_with_origin, newer)
        assert issue.issue(pre, key, "0.1", _written(pre)).published
        return real_acquire(project)

    monkeypatch.setattr(issue, "_acquire_publication_lock", b_publishes_first)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert not out.published and "not this run's output" in (out.reason or "")
    rec = Record.model_validate_json(
        (
            repo_with_origin / SET_ROOT / _pointer(repo_with_origin) / "record.json"
        ).read_bytes()
    )
    assert rec.artifact_sha256 == sha256_hex(newer.encode())


def test_bytes_replaced_before_issue_are_not_signed(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    """The run wrote A; the file holds B by the time issue() runs: nothing
    is signed for B (PR #85 review)."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    written = _written(pre)
    _author(repo_with_origin, DOCKERFILE + "USER nobody\n")
    out = issue.issue(pre, key, "0.1", written)
    assert not out.published
    assert not (repo_with_origin / SET_ROOT / POINTER).exists()


@pytest.mark.parametrize("link", [".deployer", SET_ROOT, f"{SET_ROOT}/Dockerfile"])
def test_withdraw_refuses_a_symlinked_provenance_path(
    repo_with_origin: Path, tmp_path: Path, link: str
) -> None:
    """A symlink anywhere on the provenance path is never followed: the data
    it points at survives, and the refusal raises (PR #85 review)."""
    victim = tmp_path / "data"
    (victim / "keep").mkdir(parents=True)
    (victim / "keep" / "precious.txt").write_text("x")
    path = repo_with_origin / link
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(victim, path)
    with pytest.raises(OSError, match="symlink"):
        issue.withdraw(repo_with_origin)
    assert (victim / "keep" / "precious.txt").read_text() == "x"


def test_issue_refuses_a_symlinked_ignore_file(
    repo_with_origin: Path, keypair: tuple[Path, str], tmp_path: Path
) -> None:
    """A committed escaping symlink already fails preflight's export; this is
    the remaining window, a symlink appearing while authoring runs."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    outside = tmp_path / "outside.ignore"
    outside.write_text("")
    os.symlink(outside, repo_with_origin / ".dockerignore")
    _author(repo_with_origin)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert not out.published and "symlink" in (out.reason or "")
    assert outside.read_text() == ""


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)


def test_withdraw_parent_swapped_after_the_walk_removes_only_the_real_set(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU (PR #85 review): once the walk is done, swapping the set
    parent for a symlink cannot redirect the removal outside the project."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert out.set_dir is not None
    sha = Path(out.set_dir).name
    victim = tmp_path / "victim"
    (victim / sha).mkdir(parents=True)
    (victim / sha / "precious.txt").write_text("x")
    parent = repo_with_origin / SET_ROOT / "Dockerfile"
    moved = repo_with_origin / SET_ROOT / "Dockerfile-real"
    real_rmtree = shutil.rmtree

    def swap_then_rmtree(path: str, dir_fd: int | None = None) -> None:
        if not moved.exists():
            parent.rename(moved)
            os.symlink(victim, parent)
        real_rmtree(path, dir_fd=dir_fd)

    monkeypatch.setattr(shutil, "rmtree", swap_then_rmtree)
    assert issue.withdraw(repo_with_origin) is True
    assert moved.is_dir()  # the swap happened
    assert (victim / sha / "precious.txt").read_text() == "x"
    assert not (moved / sha).exists()  # the held real set dir was removed


def test_publish_parent_swapped_after_the_walk_writes_nothing_outside(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU (PR #85 review): swapping ``SET_ROOT`` for a symlink right
    before the set write cannot make the write land outside the project."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    (repo_with_origin / SET_ROOT).mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    set_root = repo_with_origin / SET_ROOT
    moved = repo_with_origin / ".deployer" / "authoring-real"
    real_write_set = issue._write_set

    def swap_then_write(*args: Any) -> None:
        set_root.rename(moved)
        os.symlink(victim, set_root)
        real_write_set(*args)

    monkeypatch.setattr(issue, "_write_set", swap_then_write)
    try:
        issue.issue(pre, key, "0.1", _written(pre))
    except OSError:
        pass
    assert moved.is_dir()  # the swap happened
    assert sorted(p.name for p in victim.iterdir()) == ["keep.txt"]
    assert (victim / "keep.txt").read_text() == "x"
    # the writes landed in the held real directory instead
    assert (moved / POINTER).is_file()


def test_ignore_file_swapped_for_a_symlink_before_the_append_is_refused(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU (PR #85 review): an ignore file replaced by a symlink after
    the exclusion check but before the append is not written through."""
    key, _ = keypair
    (repo_with_origin / ".dockerignore").write_text("*.pyc\n")
    _commit_all(repo_with_origin, "ignore")
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    outside = tmp_path / "outside.ignore"
    outside.write_text("keep\n")
    ignore_file = repo_with_origin / ".dockerignore"
    real_excluded_by = issue.ignore.excluded_by
    swapped = False

    def swap_then_check(*args: Any) -> Any:
        nonlocal swapped
        result = real_excluded_by(*args)
        if not swapped:
            swapped = True
            ignore_file.rename(repo_with_origin / ".dockerignore.real")
            os.symlink(outside, ignore_file)
        return result

    monkeypatch.setattr(issue.ignore, "excluded_by", swap_then_check)
    out = issue.issue(pre, key, "0.1", _written(pre))
    assert swapped
    assert not out.published and "symlink" in (out.reason or "")
    assert outside.read_text() == "keep\n"


def test_a_symlinked_dockerfile_under_the_lock_is_not_this_runs_output(
    repo_with_origin: Path, keypair: tuple[Path, str], tmp_path: Path
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    outside = tmp_path / "Dockerfile"
    outside.write_text(DOCKERFILE)
    os.symlink(outside, repo_with_origin / "Dockerfile")
    out = issue.issue(pre, key, "0.1", DOCKERFILE.encode())
    assert not out.published and "not this run's output" in (out.reason or "")
    assert not (repo_with_origin / SET_ROOT / POINTER).exists()


def test_withdraw_unlinks_a_symlinked_pointer_without_following_it(
    repo_with_origin: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "target"
    outside.write_text("x")
    (repo_with_origin / SET_ROOT).mkdir(parents=True)
    os.symlink(outside, repo_with_origin / SET_ROOT / POINTER)
    assert issue.withdraw(repo_with_origin) is True
    assert not (repo_with_origin / SET_ROOT / POINTER).is_symlink()
    assert outside.read_text() == "x"


def test_an_unwritable_ignore_file_refuses_instead_of_raising(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    """PR #85 review minor: a write error while ensuring exclusion is a
    refusal, not a traceback."""
    key, _ = keypair
    ignore_file = repo_with_origin / ".dockerignore"
    ignore_file.write_text("*.pyc\n")
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_with_origin), "commit", "-qm", "i"], check=True
    )
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    ignore_file.chmod(0o444)
    try:
        out = issue.issue(pre, key, "0.1", _written(pre))
    finally:
        ignore_file.chmod(0o644)
    assert not out.published and "exclusion could not be written" in (out.reason or "")
