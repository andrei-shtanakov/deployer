"""Set planning, check-only exclusion proof, and issuing without ever
editing an ignore file (design doc §5.2)."""

import subprocess
from pathlib import Path

import pytest

from deployer.provenance import issue
from deployer.provenance.model import POINTER, SET_ROOT

DOCKERFILE = "FROM python:3.12-slim\nCOPY src ./src\n"


def _author(repo: Path, text: str = DOCKERFILE) -> None:
    """Write ``text`` as the Dockerfile authoring just produced."""
    (repo / "Dockerfile").write_text(text)


def _written(pre: issue.Preflight) -> bytes:
    """The bytes the simulated authoring run wrote."""
    return (pre.project / "Dockerfile").read_bytes()


def _commit_all(repo: Path, message: str) -> None:
    """Stage and commit every change in ``repo``."""
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)


def _file_listing(repo: Path) -> set[str]:
    """Relative paths of every regular, non-symlinked file under ``repo``,
    used for a before/after no-write comparison."""
    return {
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def _forbid_ignore_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to append to an ignore file."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-edit mode must never write an ignore file")

    monkeypatch.setattr(issue, "_append_at", boom)


def test_plan_matches_issue(repo_with_origin: Path, keypair: tuple[Path, str]) -> None:
    """``plan_set``'s bytes and paths match exactly what ``issue`` writes
    and publishes for the same Dockerfile."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    dockerfile = _written(pre)
    plan = issue.plan_set(pre, dockerfile, "0.1")
    out = issue.issue(pre, key, "0.1", dockerfile)
    assert out.published and out.set_dir is not None
    assert plan.record_sha256 == Path(out.set_dir).name
    set_dir = repo_with_origin / SET_ROOT / out.set_dir
    assert plan.record_bytes == (set_dir / "record.json").read_bytes()
    assert plan.snapshot_bytes == (set_dir / "snapshot.json").read_bytes()
    assert plan.paths == (
        f"{SET_ROOT}/{POINTER}",
        f"{SET_ROOT}/{out.set_dir}/record.json",
        f"{SET_ROOT}/{out.set_dir}/snapshot.json",
        f"{SET_ROOT}/{out.set_dir}/record.json.sig",
    )


def test_exclusion_proven_never_writes(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    """No ``.dockerignore`` exists yet: ``exclusion_proven`` refuses, and
    creates nothing and modifies nothing in the repo."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    plan = issue.plan_set(pre, _written(pre), "0.1")
    before = _file_listing(repo_with_origin)
    reason = issue.exclusion_proven(repo_with_origin, plan.paths)
    assert reason is not None and "not excluded" in reason
    assert _file_listing(repo_with_origin) == before
    assert not (repo_with_origin / ".dockerignore").exists()
    assert not (repo_with_origin / ".containerignore").exists()


def test_narrow_rule_is_not_enough(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule that excludes only the pointer file does not prove the whole
    set excluded. ``edit_ignore=True`` still widens the file to
    ``.deployer/`` (today's behaviour); ``edit_ignore=False`` refuses and
    leaves the file byte-identical."""
    key, _ = keypair
    (repo_with_origin / ".dockerignore").write_text(
        ".deployer/authoring/Dockerfile.current\n"
    )
    _commit_all(repo_with_origin, "narrow rule")
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    dockerfile = _written(pre)
    plan = issue.plan_set(pre, dockerfile, "0.1")
    reason = issue.exclusion_proven(repo_with_origin, plan.paths)
    assert reason is not None and "not excluded" in reason

    before = (repo_with_origin / ".dockerignore").read_bytes()
    _forbid_ignore_writes(monkeypatch)
    out = issue.issue(pre, key, "0.1", dockerfile, edit_ignore=False)
    assert not out.published
    assert (repo_with_origin / ".dockerignore").read_bytes() == before

    monkeypatch.undo()
    out_edit = issue.issue(pre, key, "0.1", dockerfile)
    assert out_edit.published
    assert ".deployer/" in (repo_with_origin / ".dockerignore").read_text()


def test_no_edit_mode_issues_when_proven(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.dockerignore`` already excludes ``.deployer/``: no-edit mode
    publishes without touching the ignore file."""
    key, _ = keypair
    (repo_with_origin / ".dockerignore").write_text(".deployer/\n")
    _commit_all(repo_with_origin, "wide rule")
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    dockerfile = _written(pre)
    before = (repo_with_origin / ".dockerignore").read_bytes()
    _forbid_ignore_writes(monkeypatch)
    out = issue.issue(pre, key, "0.1", dockerfile, edit_ignore=False)
    assert out.published and out.set_dir is not None
    assert (repo_with_origin / ".dockerignore").read_bytes() == before
