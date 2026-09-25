"""Set planning, check-only exclusion proof, and issuing without ever
editing an ignore file (design doc §5.2)."""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from deployer.provenance import issue
from deployer.provenance.model import POINTER, SET_ROOT

_IGNORE_FILE_NAMES = (".dockerignore", ".containerignore", "Dockerfile.dockerignore")

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


def _ignore_snapshot(repo: Path) -> dict[str, tuple[str, object]]:
    """A snapshot of every known ignore file name: ``("missing", None)``,
    ``("symlink", target)`` or ``("file", bytes)``; used to prove a refusal
    left every one of them exactly as it found them."""
    snapshot: dict[str, tuple[str, object]] = {}
    for name in _IGNORE_FILE_NAMES:
        path = repo / name
        if path.is_symlink():
            snapshot[name] = ("symlink", os.readlink(path))
        elif path.is_file():
            snapshot[name] = ("file", path.read_bytes())
        else:
            snapshot[name] = ("missing", None)
    return snapshot


def _setup_containerignore_without_rule(repo: Path, tmp_path: Path) -> None:
    """``.containerignore`` exists but does not exclude ``.deployer/``."""
    (repo / ".containerignore").write_text("*.log\n")


def _setup_containerignore_unsupported(repo: Path, tmp_path: Path) -> None:
    """``.containerignore`` has an unmodelled (bracket) pattern."""
    (repo / ".containerignore").write_text("[ab]\n")


def _setup_symlinked_dockerignore(repo: Path, tmp_path: Path) -> None:
    """``.dockerignore`` is a symlink escaping the repository."""
    outside = tmp_path / "outside.ignore"
    outside.write_text("")
    os.symlink(outside, repo / ".dockerignore")


def _setup_dockerfile_specific_narrow_rule(repo: Path, tmp_path: Path) -> None:
    """``Dockerfile.dockerignore`` excludes only the pointer file."""
    (repo / "Dockerfile.dockerignore").write_text(
        ".deployer/authoring/Dockerfile.current\n"
    )


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


@pytest.mark.parametrize(
    "setup",
    [
        _setup_containerignore_without_rule,
        _setup_containerignore_unsupported,
        _setup_symlinked_dockerignore,
        _setup_dockerfile_specific_narrow_rule,
    ],
    ids=[
        "containerignore-without-rule",
        "containerignore-unsupported",
        "symlinked-dockerignore",
        "dockerfile-specific-narrow-rule",
    ],
)
def test_no_edit_mode_refuses_without_writing(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Path, Path], None],
) -> None:
    """Every no-edit refusal case leaves every ignore file exactly as it
    found it (byte-identical, symlinks unswapped) and the repo's file
    listing unchanged."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    setup(repo_with_origin, tmp_path)
    _author(repo_with_origin)
    dockerfile = _written(pre)
    before_listing = _file_listing(repo_with_origin)
    before_ignores = _ignore_snapshot(repo_with_origin)
    _forbid_ignore_writes(monkeypatch)
    out = issue.issue(pre, key, "0.1", dockerfile, edit_ignore=False)
    assert not out.published and out.reason is not None
    assert _file_listing(repo_with_origin) == before_listing
    assert _ignore_snapshot(repo_with_origin) == before_ignores


def test_no_edit_mode_reports_an_unreadable_ignore_file(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.dockerignore`` that cannot be read is a check failure, not a
    write failure: the reason says "checked", never "written", and nothing
    is written."""
    key, _ = keypair
    ignore_file = repo_with_origin / ".dockerignore"
    ignore_file.write_text(".deployer/\n")
    _commit_all(repo_with_origin, "ignore")
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    dockerfile = _written(pre)
    before = ignore_file.read_bytes()
    ignore_file.chmod(0o000)
    _forbid_ignore_writes(monkeypatch)
    try:
        out = issue.issue(pre, key, "0.1", dockerfile, edit_ignore=False)
    finally:
        ignore_file.chmod(0o644)
    assert not out.published
    assert out.reason is not None
    assert "exclusion could not be checked" in out.reason
    assert "written" not in out.reason
    assert ignore_file.read_bytes() == before


def test_exclusion_proven_reports_a_read_failure_instead_of_raising(
    repo_with_origin: Path,
    keypair: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exclusion_proven`` is total: an ``OSError`` while reading an
    ignore file (here, simulating the file vanishing between the presence
    check and the read) becomes a reason, never an exception."""
    key, _ = keypair
    (repo_with_origin / ".dockerignore").write_text(".deployer/\n")
    _commit_all(repo_with_origin, "ignore")
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    plan = issue.plan_set(pre, _written(pre), "0.1")

    def boom(context: Path, file: str | None) -> None:
        raise OSError("vanished")

    monkeypatch.setattr(issue.ignore, "load_rules", boom)
    reason = issue.exclusion_proven(repo_with_origin, plan.paths)
    assert reason == "exclusion could not be checked: vanished"


def test_exclusion_proven_names_the_missing_ignore_file(
    repo_with_origin: Path, keypair: tuple[Path, str]
) -> None:
    """With no ignore file at all, the reason names which side has none
    instead of printing the Python ``None`` as a filename."""
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    plan = issue.plan_set(pre, _written(pre), "0.1")
    reason = issue.exclusion_proven(repo_with_origin, plan.paths)
    assert reason is not None
    assert "no CI ignore file" in reason
    assert "by None" not in reason
