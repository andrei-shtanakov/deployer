"""Integrity of the committed end-to-end acceptance evidence (F §11 stage 5).

The case set is exactly the two real runs; every file matches
``CHECKSUMS.sha256``; each fix document ends ``ci_confirmed`` with its
attempt qualified and positive; each fix commit changes exactly one
Dockerfile instruction; no private key is present. See
``tests/fixtures/e2e/PROVENANCE.md``.
"""

import hashlib
import json
from pathlib import Path

import pytest

E2E = Path(__file__).parent.parent / "fixtures" / "e2e"
CHECKSUMS = E2E / "CHECKSUMS.sha256"
CASES = ["copy", "from"]
CHANGED_LINE = {
    "copy": (
        "-COPY docs/setup.md ./setup.md",
        "+COPY docs/guide/setup.md ./setup.md",
    ),
    "from": ("-FROM python:3.12-slim extra", "+FROM python:3.12-slim AS extra"),
}


def _recorded() -> dict[str, str]:
    """``path -> sha256`` as ``CHECKSUMS.sha256`` records it."""
    entries: dict[str, str] = {}
    for line in CHECKSUMS.read_text().splitlines():
        digest, _, path = line.partition("  ")
        entries[path] = digest
    return entries


def test_the_cases_are_exactly_the_runs() -> None:
    assert sorted(p.name for p in E2E.iterdir() if p.is_dir()) == CASES


def test_checksums_cover_and_match_every_file() -> None:
    files = sorted(
        p.relative_to(E2E).as_posix()
        for p in E2E.rglob("*")
        if p.is_file() and p.name != "CHECKSUMS.sha256"
    )
    recorded = _recorded()
    assert sorted(recorded) == files
    mismatched = [
        path
        for path, digest in recorded.items()
        if hashlib.sha256((E2E / path).read_bytes()).hexdigest() != digest
    ]
    assert mismatched == []


@pytest.mark.parametrize("case", CASES)
def test_admitted_then_ci_confirmed(case: str) -> None:
    verdict = json.loads((E2E / case / "verdict.json").read_text())
    assert verdict["admission"]["verdict"] == "admitted"
    doc = json.loads((E2E / case / "fix" / "fix.json").read_text())
    assert doc["status"] == "ci_confirmed"
    attempt = doc["ci_attempts"][-1]
    assert attempt["outcome"] == "ci_confirmed"
    assert [c["qualification"] for c in attempt["considered"]] == ["qualified"]
    assert all(e["positive"] for e in attempt["evidence"])


@pytest.mark.parametrize("case", CASES)
def test_fix_commit_changes_one_instruction(case: str) -> None:
    patch = (E2E / case / "fix" / "fix-commit.patch").read_text()
    dockerfile = patch.split("diff --git a/Dockerfile b/Dockerfile", 1)[1]
    dockerfile = dockerfile.split("\ndiff --git ", 1)[0].split("\n-- \n", 1)[0]
    changed = [
        line
        for line in dockerfile.splitlines()
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    ]
    assert tuple(changed) == CHANGED_LINE[case]


def test_no_private_key() -> None:
    offenders = [
        p.name
        for p in E2E.rglob("*")
        if p.is_file() and b"PRIVATE KEY" in p.read_bytes()
    ]
    assert offenders == []
