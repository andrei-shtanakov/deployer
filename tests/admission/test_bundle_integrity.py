"""Integrity of the committed admission bundles (A §8.2, §8.3 level P).

Independent of the replay: the case set is exactly A's list; every file
matches ``CHECKSUMS.sha256``, recorded when
``tests/fixtures/admission/make_admission_bundle.py`` built the bundles, so
an edit to a set, a key, a trust store or a vendored tree is visible here
before it can quietly change what a case proves; every case's ``tree/``
equals its ``tree-listing.json`` by Git blob SHA; every case names its
provenance; and no private key is committed (only the public halves are:
signing needs a private key, the replay never does).

After a deliberate change, regenerate everything (bundles and checksums) with
``uv run python tests/fixtures/admission/make_admission_bundle.py`` (see its
docstring for ``--key``/``--unknown-key``).
"""

import hashlib
import json
from pathlib import Path

import pytest

from deployer.reproduce.restore import git_blob_sha

BUNDLES = Path(__file__).parent.parent / "fixtures" / "admission"
CHECKSUMS = BUNDLES / "CHECKSUMS.sha256"
NOT_CHECKSUMMED = {"CHECKSUMS.sha256", "make_admission_bundle.py"}
CASES = sorted(p.name for p in BUNDLES.iterdir() if (p / "expected.json").is_file())
# A §8.2 and §8.3 level P, as the plan lists them: a bundle that goes missing
# fails here instead of silently dropping out of every parametrized check.
SPEC_CASES = sorted(
    [
        "admit-run-1",
        "admit-run-5",
        "trust-inside",
        "trust-symlink",
        "no-set",
        "pointer-missing-dir",
        "record-format",
        "snapshot-format",
        "dir-not-hash",
        "bad-signature",
        "unknown-key",
        "revoked-key",
        "hand-edit",
        "foreign-repo",
        "foreign-path",
        "snapshot-edited",
        "source-commit-differs",
        "tree-incomplete",
        "row-missing-field",
        "row-non-string",
        "duplicate-path",
        "dollar-var",
        "syntax-directive",
    ]
)


def _recorded() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in CHECKSUMS.read_text().splitlines():
        digest, _, path = line.partition("  ")
        entries[path] = digest
    return entries


def _bundle_files() -> list[str]:
    return sorted(
        rel
        for p in BUNDLES.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and "__pycache__" not in p.parts
        and (rel := p.relative_to(BUNDLES).as_posix()) not in NOT_CHECKSUMMED
    )


def test_the_bundles_are_exactly_the_spec_cases() -> None:
    assert CASES == SPEC_CASES


def test_checksums_cover_exactly_the_bundle_files() -> None:
    assert sorted(_recorded()) == _bundle_files()


def test_every_bundle_file_matches_its_recorded_checksum() -> None:
    changed = [
        path
        for path, digest in _recorded().items()
        if hashlib.sha256((BUNDLES / path).read_bytes()).hexdigest() != digest
    ]
    assert changed == []


@pytest.mark.parametrize("case", CASES)
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
        if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
    }
    assert actual == expected


def test_every_bundle_names_its_provenance() -> None:
    assert [c for c in CASES if not (BUNDLES / c / "PROVENANCE.md").is_file()] == []


def test_no_private_key_is_committed() -> None:
    marker = b"PRIVATE " + b"KEY-----"
    found = [
        p.relative_to(BUNDLES).as_posix()
        for p in BUNDLES.rglob("*")
        if p.is_file() and marker in p.read_bytes()
    ]
    assert found == []
