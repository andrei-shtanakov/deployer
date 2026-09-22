"""Offline replay of the live acceptance runs (spec §8.1 / §8.2).

Each fixture under ``tests/fixtures/runs/`` is the anonymised ``FailedRun``
snapshot of one real ``workflow_dispatch`` run of the authored polygon
workflow (2026-09-22): repository identity rewritten to ``example/project``
(``repo``, ``url`` and the runner's checkout paths); run/job ids, SHAs, branch
names, step numbers and the rest of the log text kept. The classifier is pure,
so replaying the snapshot IS the live diagnosis — these tests fail if the
catalogue or the evidence discipline regresses.
"""

from pathlib import Path

import pytest

from deployer.diagnose import diagnose_run
from deployer.forge import SNAPSHOT_SCHEMA_VERSION, load_snapshot
from deployer.models import FailureKind

FIXTURES = Path(__file__).parent / "fixtures" / "runs"

# Expected class per fixture, fixed before the runs were dispatched; the
# marker is the line the live log really carried.
CASES = [
    ("authoring", FailureKind.AUTHORING, '"/docs/setup.md": not found'),
    ("environment", FailureKind.ENVIRONMENT, "connection timed out"),
    ("project", FailureKind.PROJECT, "AssertionError: 'hello from ci_build'"),
]


def _load(name: str):
    return load_snapshot((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(("name", "kind", "marker"), CASES)
def test_live_run_replays_to_its_class(
    name: str, kind: FailureKind, marker: str
) -> None:
    diagnosis = diagnose_run(_load(name))
    assert diagnosis.outcome == "CLASSIFIED"
    assert diagnosis.causes == [kind]
    (verdict,) = diagnosis.failures
    assert verdict.kind is kind
    assert any(marker in e.text for e in verdict.evidence), "class without citation"


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_fixture_is_complete_and_anonymised(name: str) -> None:
    snapshot = _load(name)
    assert snapshot.snapshot_schema_version == SNAPSHOT_SCHEMA_VERSION
    assert snapshot.completeness.logs == "present"
    assert snapshot.repo == "example/project"
    raw = (FIXTURES / f"{name}.json").read_text()
    assert "andrei" not in raw.lower()
    # The weak proxy above passed while the real repository name survived 12x
    # per fixture in the runner's checkout paths.
    assert "work/deployer" not in raw
    assert "\x1b" not in raw


def test_the_three_live_classes_are_distinct() -> None:
    """The discrimination the live runs prove: three runs, three classes."""
    kinds = set()
    for name, _, _ in CASES:
        causes = diagnose_run(_load(name)).causes
        assert causes, f"{name}: no cause established"
        kinds.add(causes[0])
    assert kinds == {
        FailureKind.AUTHORING,
        FailureKind.ENVIRONMENT,
        FailureKind.PROJECT,
    }
