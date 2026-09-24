"""Issuing: preflight gates, exclusion, immutable dirs, atomic pointer,
reuse, withdraw."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from deployer.provenance import gitrepo, issue, sshsig
from deployer.provenance.model import POINTER, SET_ROOT, Record, sha256_hex
from tests.provenance.conftest import make_key

DOCKERFILE = "FROM python:3.12-slim\nCOPY src ./src\n"


def _author(repo: Path, text: str = DOCKERFILE) -> None:
    (repo / "Dockerfile").write_text(text)


def _pointer(repo: Path) -> str:
    return (repo / SET_ROOT / POINTER).read_text().strip()


def test_issue_publishes_a_verifiable_set(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, pub = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    out = issue.issue(pre, key, "0.1")
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
    first = issue.issue(pre, key, "0.1")
    assert first.set_dir is not None
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_with_origin), "commit", "-qm", "a"], check=True
    )
    pre2 = issue.preflight(repo_with_origin, key)
    assert isinstance(pre2, issue.Preflight)
    _author(repo_with_origin, DOCKERFILE + 'CMD ["python"]\n')
    real_replace = os.replace

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(dst).endswith(POINTER):
            raise OSError("killed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    try:
        issue.issue(pre2, key, "0.1")
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
    first = issue.issue(pre, key, "0.1")
    assert first.set_dir is not None
    set_dir = repo_with_origin / SET_ROOT / first.set_dir
    mtimes = {p.name: p.stat().st_mtime_ns for p in set_dir.iterdir()}
    again = issue.issue(pre, key, "0.1")
    assert again.published and again.set_dir == first.set_dir
    assert {p.name: p.stat().st_mtime_ns for p in set_dir.iterdir()} == mtimes
    (set_dir / "snapshot.json").write_text("{}")  # tampered
    assert not issue.issue(pre, key, "0.1").published


def test_withdraw_removes_pointer_then_dirs(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    issue.issue(pre, key, "0.1")
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
    assert "exclusion" in (issue.issue(pre, key, "0.1").reason or "")
    # refused before writing: the unsupported file is left untouched
    assert (repo_with_origin / ".dockerignore").read_bytes() == before


def test_reuse_refuses_without_raising_when_the_set_dir_is_incomplete(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1")
    assert first.set_dir is not None
    set_dir = repo_with_origin / SET_ROOT / first.set_dir
    for child in set_dir.iterdir():
        child.unlink()
    out = issue.issue(pre, key, "0.1")
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
    first = issue.issue(pre, key, "0.1")
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
    second = issue.issue(pre2, key, "0.1")
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
    first = issue.issue(pre, key, "0.1")
    assert first.set_dir is not None
    sig_path = repo_with_origin / SET_ROOT / first.set_dir / "record.json.sig"
    sig_path.write_bytes(b"not a signature")
    assert not issue.issue(pre, key, "0.1").published


def test_reuse_refuses_a_signature_from_a_different_key(
    repo_with_origin: Path, keypair: tuple[Path, str], tmp_path: Path
) -> None:
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1")
    assert first.published
    other_key, _ = make_key(tmp_path, "other")
    assert not issue.issue(pre, other_key, "0.1").published
