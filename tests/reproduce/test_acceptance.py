"""§8.A: every committed bundle replays to its expected result, offline.

A bundle is a directory under ``tests/fixtures/reproduction/<case>/`` holding
``expected.json``; ``make_bundle.py`` there builds the base ones. Its inputs:

- ``snapshot.json`` — the run, read with :func:`load_snapshot`;
- ``tree/`` + ``tree-listing.json`` — served as GitHub would: the tarball is
  ``tree/`` as is (minus untracked ``__pycache__``/``.DS_Store`` noise) under
  one top-level directory, the listing is the file's
  text (the Git tree API body: ``sha``, ``truncated``, ``tree`` entries);
- ``local.stdout``, ``local.stderr``, ``local.exit`` — what the fake
  ``podman build`` returns; ``local.timeout`` containing ``true`` makes it
  raise ``TimeoutExpired`` instead;
- ``endpoint.json`` — ``{"tool": "podman"|"docker", "connections": <the JSON
  list of `podman system connection list --format json`>, "env": {<the
  environment variables reproduction sees>}}``.

``expected.json`` keys (every key but ``status`` and ``refusal`` optional;
an absent key is not asserted):

- ``status``, ``refusal`` — exact;
- ``restoration`` — ``{"state", "unmet": [substrings, each must occur in one
  unmet condition]}``, or null for a section that carries none (a refusal
  before the tree is judged); ``unmet_count`` — the exact number of unmet
  conditions;
- ``failed_checks`` — exact ``[check_id, finding]`` list of failed checks;
- ``checks`` — ``{check_id: status}`` of the first check with that id;
- ``exit_code``, ``launch_error``, ``build_containers`` — the build's fields;
- ``build_failed_instruction`` — ``{"kind", "lines", "bound_by"}`` or null;
- ``ci_instruction`` — the same shape for the CI side;
- ``signature_match``, ``comparison``, ``comparison_reason`` — the §7 result;
- ``dimensions`` — a subset of the comparison's dimensions, exact per key.

A bundle whose ``expected.json`` has no ``comparison`` asserts that the
section carries neither a build nor a comparison.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from deployer.forge import load_snapshot
from deployer.reproduce.model import InstructionRef
from deployer.reproduce.run import reproduce_run
from tests.reproduce.bundles import BUNDLES, BundleGh, _containers
from tests.reproduce.conftest import FakeContainers

CASES = sorted(p.name for p in BUNDLES.iterdir() if (p / "expected.json").is_file())
SPEC_CASES = {
    "run-1",
    "run-2",
    "run-3",
    "run-5",
    "snapshot-1.2",
    "event-pr",
    "checkout-sha-other",
    "checkout-sha-missing",
    "checkout-ref",
    "several-failed-jobs",
    "matrix",
    "shell-chain",
    "context-subdir",
    "endpoint-env",
    "endpoint-remote",
    "generating-step",
    "gitattributes",
    "archive-mismatch",
    "copy-git",
    "run-mount",
    "containerignore",
    "local-success",
    "timeout",
    "backend-down",
    "unbound-ci",
    "other-line",
    "other-output",
    "signature-missing",
}


def _ref(ref: InstructionRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {"kind": ref.kind, "lines": list(ref.lines), "bound_by": ref.bound_by}


@pytest.mark.parametrize("case", CASES)
def test_bundle_replays_to_expected(
    case: str, tmp_path: Path, fake_containers: FakeContainers
) -> None:
    bundle = BUNDLES / case
    rt, env = _containers(bundle, fake_containers)
    snapshot = load_snapshot((bundle / "snapshot.json").read_text())
    section = reproduce_run(
        snapshot,
        gh=BundleGh(bundle, tmp_path / "bundle"),
        rt=rt,
        runtime_error=None,
        env=env,
        root=tmp_path / "work",
        build_timeout=60,
    )
    expected = json.loads((bundle / "expected.json").read_text())
    assert section.status == expected["status"]
    assert section.refusal == expected["refusal"]
    _assert_restoration(section.restoration, expected)
    _assert_checks(section.checks, expected)
    if "comparison" not in expected:
        assert section.build is None and section.comparison is None
        return
    _assert_build(section.build, expected)
    comparison = section.comparison
    assert comparison is not None
    assert comparison.state == expected["comparison"]
    assert comparison.reason == expected.get("comparison_reason")
    assert comparison.state != "reproduced"  # §7.3: unreachable in this slice
    if "signature_match" in expected:
        assert comparison.signature_match == expected["signature_match"]
    if "ci_instruction" in expected:
        assert _ref(comparison.ci_instruction) == expected["ci_instruction"]
    for name, value in expected.get("dimensions", {}).items():
        assert comparison.dimensions[name] == value, name


def _assert_restoration(restoration: Any, expected: dict[str, Any]) -> None:
    if "restoration" not in expected:
        return
    if expected["restoration"] is None:
        assert restoration is None
        return
    assert restoration is not None
    assert restoration.state == expected["restoration"]["state"]
    for condition in expected["restoration"]["unmet"]:
        assert any(condition in u for u in restoration.unmet), (
            condition,
            restoration.unmet,
        )
    if "unmet_count" in expected:
        assert len(restoration.unmet) == expected["unmet_count"], restoration.unmet


def _assert_checks(checks: list[Any], expected: dict[str, Any]) -> None:
    if "failed_checks" in expected:
        got = [[c.check_id, c.finding] for c in checks if c.status == "failed"]
        assert got == expected["failed_checks"]
    for check_id, status in expected.get("checks", {}).items():
        found = next((c for c in checks if c.check_id == check_id), None)
        assert found is not None, check_id
        assert found.status == status, check_id


def _assert_build(build: Any, expected: dict[str, Any]) -> None:
    assert build is not None
    for key in ("exit_code", "launch_error", "build_containers"):
        if key in expected:
            assert getattr(build, key) == expected[key], key
    if "build_failed_instruction" in expected:
        assert _ref(build.failed_instruction) == expected["build_failed_instruction"]


def test_every_case_of_the_spec_is_committed() -> None:
    assert SPEC_CASES <= set(CASES)
