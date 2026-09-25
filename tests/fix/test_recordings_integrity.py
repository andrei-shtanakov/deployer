"""Integrity of the committed L- and C-recordings (F §9, §11 stages 3–4).

The case set is exactly the recorded one; every file matches
``CHECKSUMS.sha256``, written when the recordings were made, so an edit to a
recorded stream is visible here before it can change what a template row is
checked against; every case carries the files the replay reads; no private
key is present. Re-recording is a data change for the owner's review (see
``tests/fixtures/recordings/PROVENANCE.md``).
"""

import hashlib
import json
from pathlib import Path

import pytest

RECORDINGS = Path(__file__).parent.parent / "fixtures" / "recordings"
LOCAL = RECORDINGS / "local"
CI = RECORDINGS / "ci"
CHECKSUMS = RECORDINGS / "CHECKSUMS.sha256"
NOT_CHECKSUMMED = {"CHECKSUMS.sha256", "record_local.py", "record_ci.py"}
LOCAL_CASES = [
    "l1-copy-cold",
    "l2-copy-warm",
    "l3-from-run5",
    "l4-from-bad-later",
    "l5-stages-same-image",
    "l6-copy-later-failure",
    "l7-stages-both-built",
    "l8-from-bad-after-built-stage",
    "l9-from-bad-in-skipped-stage",
]
CI_CASES = [
    "c1-copy-done",
    "c2-copy-rerun",
    "c2b-copy-rerun-reread",
    "c3-from-parsed",
    "c4-copy-later-failure",
    "c5-copy-recurred",
    "c6-copy-cached",
    "c7-copy-done-padded",
    "c8-from-bad-in-skipped-stage",
    "c9-copy-done-padded-run",
]
CI_CASE_FILES = (
    "checks.json",
    "environment.json",
    "expected.json",
    "gh-calls.json",
    "tree/Dockerfile",
    "tree/.github/workflows/diagnosis-polygon.yml",
)
CASE_FILES = (
    "argv.json",
    "build.exit",
    "build.stderr",
    "build.stdout",
    "checks.json",
    "environment.json",
    "expected.json",
    "tree/Dockerfile",
)


def _recorded() -> dict[str, str]:
    """``path -> sha256`` as ``CHECKSUMS.sha256`` records it."""
    entries: dict[str, str] = {}
    for line in CHECKSUMS.read_text().splitlines():
        digest, _, path = line.partition("  ")
        entries[path] = digest
    return entries


def _files() -> list[str]:
    """Every regular file the checksums must cover."""
    return sorted(
        rel
        for p in RECORDINGS.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and (rel := p.relative_to(RECORDINGS).as_posix()) not in NOT_CHECKSUMMED
    )


def test_the_local_cases_are_exactly_the_recorded_ones() -> None:
    assert sorted(p.name for p in LOCAL.iterdir() if p.is_dir()) == LOCAL_CASES


def test_the_ci_cases_are_exactly_the_recorded_ones() -> None:
    assert sorted(p.name for p in CI.iterdir() if p.is_dir()) == CI_CASES


@pytest.mark.parametrize("case", CI_CASES)
def test_ci_case_has_its_files(case: str) -> None:
    missing = [name for name in CI_CASE_FILES if not (CI / case / name).is_file()]
    assert missing == []


@pytest.mark.parametrize("case", CI_CASES)
def test_ci_calls_are_recorded_verbatim_shapes(case: str) -> None:
    """Every recorded call has an argv and exactly one of stdout / error."""
    calls = json.loads((CI / case / "gh-calls.json").read_text())
    assert calls
    assert all(isinstance(c["argv"], list) for c in calls)
    assert all(("stdout" in c) != ("error" in c) for c in calls)


def test_checksums_cover_exactly_the_files() -> None:
    assert sorted(_recorded()) == _files()


def test_every_file_matches_its_checksum() -> None:
    mismatched = [
        path
        for path, digest in _recorded().items()
        if hashlib.sha256((RECORDINGS / path).read_bytes()).hexdigest() != digest
    ]
    assert mismatched == []


@pytest.mark.parametrize("case", LOCAL_CASES)
def test_case_has_its_files(case: str) -> None:
    missing = [name for name in CASE_FILES if not (LOCAL / case / name).is_file()]
    assert missing == []


@pytest.mark.parametrize("case", LOCAL_CASES)
def test_expected_answers_every_check(case: str) -> None:
    checks = json.loads((LOCAL / case / "checks.json").read_text())
    expected = json.loads((LOCAL / case / "expected.json").read_text())["checks"]
    asked = [(c["kind"], c["corrected"]) for c in checks]
    assert [(e["kind"], e["corrected"]) for e in expected] == asked


def test_provenance_present() -> None:
    assert "verbatim" in (RECORDINGS / "PROVENANCE.md").read_text()


def test_no_private_key() -> None:
    offenders = [
        p.relative_to(RECORDINGS).as_posix()
        for p in RECORDINGS.rglob("*")
        if p.is_file() and b"PRIVATE KEY" in p.read_bytes()
    ]
    assert offenders == []
