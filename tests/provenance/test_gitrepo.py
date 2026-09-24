import os
import subprocess
from pathlib import Path

import pytest

from deployer.provenance import gitrepo


def _git(repo: Path, *args: str) -> None:
    """Run a git command against ``repo``, raising on failure."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:o/r.git", "o/r"),
        ("git@github.com:o/r", "o/r"),
        ("https://github.com/o/r.git", "o/r"),
        ("https://github.com/o/r", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
    ],
)
def test_origin_slug_forms(repo: Path, url: str, slug: str) -> None:  # Review Focus 2
    _git(repo, "remote", "add", "origin", url)
    assert gitrepo.origin_slug(repo) == slug


def test_no_origin_and_not_a_checkout(repo: Path, tmp_path: Path) -> None:
    assert gitrepo.origin_slug(repo) is None
    assert gitrepo.is_checkout(repo) and not gitrepo.is_checkout(tmp_path / "nope")


def test_toplevel_on_root_and_subdir(repo: Path) -> None:
    assert gitrepo.toplevel(repo) == repo.resolve()
    assert gitrepo.toplevel(repo / "src") == repo.resolve()


def test_toplevel_raises_outside_a_checkout(tmp_path: Path) -> None:
    with pytest.raises(gitrepo.GitError):
        gitrepo.toplevel(tmp_path / "nope")


def test_dirty_paths_cover_staged_unstaged_untracked(repo: Path) -> None:
    assert gitrepo.dirty_paths(repo) == []
    (repo / "src" / "m.py").write_text("x = 2\n")
    (repo / "new.txt").write_text("n")
    (repo / "staged.txt").write_text("s")
    _git(repo, "add", "staged.txt")
    assert sorted(gitrepo.dirty_paths(repo)) == ["new.txt", "src/m.py", "staged.txt"]


def test_listing_and_export(repo: Path, tmp_path: Path) -> None:
    head = gitrepo.head_commit(repo)
    rows = gitrepo.tree_listing(repo, head)
    assert {r.path for r in rows} == {"pyproject.toml", "src", "src/m.py"}
    dest = tmp_path / "export"
    gitrepo.export_commit(repo, head, dest)
    assert (dest / "src" / "m.py").read_text() == "x = 1\n"


def test_export_rejects_absolute_symlink(repo: Path, tmp_path: Path) -> None:
    os.symlink("/etc/passwd", repo / "evil")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evil symlink")
    head = gitrepo.head_commit(repo)
    with pytest.raises(gitrepo.GitError):
        gitrepo.export_commit(repo, head, tmp_path / "export")
