"""Integrity of the committed reproduction bundles (spec §8.A).

Two checks, independent of the replay in ``test_acceptance.py``:

- every bundle file matches ``CHECKSUMS.sha256``, recorded when the bundles
  were built, so an edit to a snapshot, a recording or a vendored tree is
  visible here before it can quietly change what a case proves;
- every bundle's ``tree/`` equals its ``tree-listing.json`` by Git blob SHA,
  except ``archive-mismatch``, whose listing names a file the tree lacks on
  purpose (see its ``PROVENANCE.md``).

After a deliberate change, regenerate the checksums from
``tests/fixtures/reproduction`` with::

    find . -type f ! -name CHECKSUMS.sha256 ! -name make_bundle.py \\
        ! -path './check-outputs/*' | LC_ALL=C sort | sed 's|^\\./||' \\
        | xargs shasum -a 256 > CHECKSUMS.sha256
"""

import hashlib
import json
from pathlib import Path

import pytest

from deployer.reproduce.restore import git_blob_sha

BUNDLES = Path(__file__).parent.parent / "fixtures" / "reproduction"
CHECKSUMS = BUNDLES / "CHECKSUMS.sha256"
LISTING_DIFFERS_ON_PURPOSE = {"archive-mismatch"}
CASES = sorted(p.name for p in BUNDLES.iterdir() if (p / "expected.json").is_file())


def _recorded() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in CHECKSUMS.read_text().splitlines():
        digest, _, path = line.partition("  ")
        entries[path] = digest
    return entries


def _bundle_files() -> list[str]:
    return sorted(
        p.relative_to(BUNDLES).as_posix()
        for case in CASES
        for p in (BUNDLES / case).rglob("*")
        if p.is_file() and not p.is_symlink()
    )


def test_checksums_cover_exactly_the_bundle_files() -> None:
    assert sorted(_recorded()) == _bundle_files()


def test_every_bundle_file_matches_its_recorded_checksum() -> None:
    changed = [
        path
        for path, digest in _recorded().items()
        if hashlib.sha256((BUNDLES / path).read_bytes()).hexdigest() != digest
    ]
    assert changed == []


@pytest.mark.parametrize(
    "case", [c for c in CASES if c not in LISTING_DIFFERS_ON_PURPOSE]
)
def test_tree_matches_its_listing_by_blob_sha(case: str) -> None:
    listing = json.loads((BUNDLES / case / "tree-listing.json").read_text())
    tree = BUNDLES / case / "tree"
    expected = {
        e["path"]: e["sha"]
        for e in listing["tree"]
        if e["type"] == "blob" and e["mode"] != "120000"
    }
    actual = {
        p.relative_to(tree).as_posix(): git_blob_sha(p.read_bytes())
        for p in tree.rglob("*")
        if p.is_file() and not p.is_symlink()
    }
    assert actual == expected


def test_every_bundle_names_its_provenance() -> None:
    assert [c for c in CASES if not (BUNDLES / c / "PROVENANCE.md").is_file()] == []
