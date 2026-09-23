"""§1.4 extraction and §1.3 (b)(c): what the archive changed is caught."""

import io
import os
import tarfile

from deployer.forge import TreeEntry, TreeListing
from deployer.reproduce.restore import (
    extract,
    git_blob_sha,
    listing_conditions,
    make_tarball,
)


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("print(1)\n")
    (src / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)
    os.symlink("pkg/a.py", src / "link.py")
    return src


def _listing(src) -> TreeListing:
    entries = [
        TreeEntry("pkg", "040000", "tree", "t"),
        TreeEntry("pkg/a.py", "100644", "blob", git_blob_sha(b"print(1)\n")),
        TreeEntry("run.sh", "100755", "blob", git_blob_sha(b"#!/bin/sh\n")),
        TreeEntry("link.py", "120000", "blob", git_blob_sha(b"pkg/a.py")),
    ]
    return TreeListing("sha", entries, False)


def test_round_trip_is_exact(tmp_path):
    src = _tree(tmp_path)
    dest = tmp_path / "out"
    assert extract(make_tarball(src, "example-project-d6e330f"), dest) is None
    assert os.access(dest / "run.sh", os.X_OK)
    assert (dest / "link.py").is_symlink()
    assert listing_conditions(dest, _listing(src)) == []


def test_git_blob_sha_matches_git():
    assert git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_missing_extra_mode_and_content_are_named(tmp_path):
    src = _tree(tmp_path)
    dest = tmp_path / "out"
    extract(make_tarball(src, "p"), dest)
    (dest / "pkg" / "a.py").write_text("changed\n")
    os.chmod(dest / "run.sh", 0o644)
    (dest / "extra.txt").write_text("x")
    listing = _listing(src)
    listing.entries.append(TreeEntry("gone.txt", "100644", "blob", "0" * 40))
    assert sorted(listing_conditions(dest, listing)) == sorted(
        [
            "archive differs from tree listing: content of pkg/a.py",
            "archive differs from tree listing: mode of run.sh",
            "archive differs from tree listing: extra extra.txt",
            "archive differs from tree listing: missing gone.txt",
        ]
    )


def test_gitattributes_truncation_and_submodules(tmp_path):
    src = _tree(tmp_path)
    dest = tmp_path / "out"
    extract(make_tarball(src, "p"), dest)
    listing = _listing(src)
    listing.entries.append(TreeEntry("docs/.gitattributes", "100644", "blob", "x"))
    listing.entries.append(TreeEntry("vendor/lib", "160000", "commit", "y"))
    out = listing_conditions(dest, TreeListing("sha", listing.entries, True))
    assert "tree listing truncated" in out
    assert ".gitattributes present: docs/.gitattributes" in out
    assert "submodule entry vendor/lib" in out


def test_directory_entries_are_not_extra_paths(tmp_path):  # Review Focus 3
    src = _tree(tmp_path)
    (src / "empty").mkdir()
    dest = tmp_path / "out"
    extract(make_tarball(src, "p"), dest)
    assert listing_conditions(dest, _listing(src)) == []


def _raw_tar(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_unsafe_or_malformed_archives_are_unavailable(tmp_path):
    assert extract(_raw_tar([("a/x", b""), ("b/y", b"")]), tmp_path / "1") == (
        "archive has 2 top-level entries"
    )
    assert "refused" in (
        extract(_raw_tar([("p/../../evil", b"")]), tmp_path / "2") or ""
    )
    assert extract(b"not a tarball", tmp_path / "3") == "archive unreadable: ReadError"
