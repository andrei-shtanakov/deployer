"""Offline replay of the live acceptance runs (spec §8.1 / §8.2).

Each fixture under ``tests/fixtures/runs/`` is the anonymised ``FailedRun``
snapshot of one real ``workflow_dispatch`` run of the authored polygon
workflow (2026-09-22): repository identity rewritten to ``example/project``
(``repo``, ``url`` and the runner's checkout paths); run/job ids, SHAs, branch
names, step numbers and the rest of the log text kept. The reading layer is
pure, so replaying the snapshot IS the live reading — these tests fail if the
observation table or the evidence discipline regresses.

The layer reports what it saw and asserts no cause. Every live run reads
``UNCLASSIFIED`` with the observation its log really carried: run 1 buildkit's
COPY/ADD shape, run 2 apt's timeout, run 3 the assertion. What the live runs
still prove is that the three failures are told apart by their observations,
with the block each one was read from cited — not that any of them has a
class.
"""

from pathlib import Path

import pytest

from deployer.diagnose import Outcome, diagnose_run
from deployer.forge import load_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "runs"

# Expected outcome per fixture, and the head of the observation the live log
# really produced. The fixture names are the scenario names of spec §6.4, not
# claims about a cause.
CASES: list[tuple[str, Outcome, str]] = [
    ("authoring", "UNCLASSIFIED", "copy/add source not found: "),
    ("environment", "UNCLASSIFIED", "connection timed out: "),
    ("project", "UNCLASSIFIED", "assertion error: "),
]


def _load(name: str):
    return load_snapshot((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(("name", "outcome", "observation"), CASES)
def test_live_run_replays_to_its_reading(
    name: str, outcome: Outcome, observation: str
) -> None:
    diagnosis = diagnose_run(_load(name))
    assert diagnosis.outcome == outcome
    assert diagnosis.causes == []
    (verdict,) = diagnosis.failures
    assert verdict.kind is None
    cited = [o for o in verdict.observations if o.startswith(observation)]
    assert cited, verdict.observations
    line = cited[0].removeprefix(observation)
    assert any(line in e.text for e in verdict.evidence), "observation without citation"


@pytest.mark.parametrize("name", [c[0] for c in CASES])
def test_fixture_is_complete_and_anonymised(name: str) -> None:
    snapshot = _load(name)
    # The committed fixtures are 1.2 documents: historic, loaded as such.
    assert snapshot.snapshot_schema_version == "1.2"
    assert snapshot.completeness.logs == "present"
    assert snapshot.repo == "example/project"
    raw = (FIXTURES / f"{name}.json").read_text()
    assert "andrei" not in raw.lower()
    # The weak proxy above passed while the real repository name survived 12x
    # per fixture in the runner's checkout paths.
    assert "work/deployer" not in raw
    assert "\x1b" not in raw


def test_the_live_runs_are_told_apart_by_their_observations_not_by_a_class() -> None:
    """What the live runs prove: three different failures, three different
    sets of observation names, and no cause asserted over any of them."""
    names_per_run = []
    for name, _, _ in CASES:
        diagnosis = diagnose_run(_load(name))
        assert diagnosis.causes == [], f"{name}: a cause this layer must not assert"
        (verdict,) = diagnosis.failures
        names_per_run.append(
            frozenset(o.split(": ", 1)[0] for o in verdict.observations if ": " in o)
        )
    assert len(set(names_per_run)) == len(CASES), names_per_run
