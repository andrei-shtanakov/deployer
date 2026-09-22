"""Offline replay of the live acceptance runs (spec §8.1 / §8.2).

Each fixture under ``tests/fixtures/runs/`` is the anonymised ``FailedRun``
snapshot of one real ``workflow_dispatch`` run of the authored polygon
workflow (2026-09-22): repository identity rewritten to ``example/project``
(``repo``, ``url`` and the runner's checkout paths); run/job ids, SHAs, branch
names, step numbers and the rest of the log text kept. The classifier is pure,
so replaying the snapshot IS the live diagnosis — these tests fail if the
catalogue or the evidence discipline regresses.

One of the three establishes a class; runs 1 and 2 do NOT, and both are the
honest result rather than a regression. Run 1's line is buildkit's COPY/ADD
shape, which the owner's evidence rule moved to ``SYMPTOMS`` on 2026-09-22: a
path the COPY never had right and a file the project moved after the
Dockerfile was authored print the identical sentence, so the snapshot cannot
establish AUTHORING. Run 2's apt failure is against ``10.255.255.1``, an
address the polygon workflow itself put in ``sources.list``, and the owner's
check (1) of the same day says a tool's framing names the SOURCE of the
message and not its cause: a wrong address yields the same network error. In
both cases the expectation was corrected and the catalogue was NOT widened to
keep the run green.
"""

from pathlib import Path

import pytest

from deployer.diagnose import Outcome, diagnose_run
from deployer.forge import SNAPSHOT_SCHEMA_VERSION, load_snapshot
from deployer.models import FailureKind

FIXTURES = Path(__file__).parent / "fixtures" / "runs"

# Expected outcome per fixture, and the text the live log really carried: the
# citation for a class, the observation for the one that establishes none.
CASES: list[tuple[str, Outcome, FailureKind | None, str]] = [
    (
        "authoring",
        "UNCLASSIFIED",
        None,
        "symptom: copy/add source not found: ",
    ),
    (
        "environment",
        "UNCLASSIFIED",
        None,
        "symptom: fetch failure: ",
    ),
    (
        "project",
        "CLASSIFIED",
        FailureKind.PROJECT,
        "AssertionError: 'hello from ci_build'",
    ),
]


def _load(name: str):
    return load_snapshot((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(("name", "outcome", "kind", "marker"), CASES)
def test_live_run_replays_to_its_outcome(
    name: str, outcome: Outcome, kind: FailureKind | None, marker: str
) -> None:
    diagnosis = diagnose_run(_load(name))
    assert diagnosis.outcome == outcome
    (verdict,) = diagnosis.failures
    if kind is None:
        assert diagnosis.causes == []
        assert verdict.kind is FailureKind.UNKNOWN
        assert verdict.evidence == [], "nothing established, nothing cited"
        assert any(o.startswith(marker) for o in verdict.observations)
        return
    assert diagnosis.causes == [kind]
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


def test_each_live_run_establishes_only_what_it_carries() -> None:
    """What the live runs prove after the two evidence checks: one class, and
    two honest UNCLASSIFIED.

    Before the checks this read "three runs, three classes". Two of those
    classes came from markers that two different causes print, so each was a
    wrong diagnosis on one of them — the count went down because the answers
    got truer, and that is the assertion worth keeping."""
    kinds = set()
    for name, outcome, _, _ in CASES:
        causes = diagnose_run(_load(name)).causes
        if outcome == "UNCLASSIFIED":
            assert causes == [], f"{name}: a class the evidence does not carry"
            continue
        assert causes, f"{name}: no cause established"
        kinds.add(causes[0])
    assert kinds == {FailureKind.PROJECT}
