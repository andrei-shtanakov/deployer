"""Verdict schema 1.1 → 1.2, additive only when a ``reproduction`` key exists."""

import json
from pathlib import Path

import pytest

from deployer.diagnose import diagnose_run, render_verdict
from deployer.forge import FailedRun, load_snapshot
from deployer.reproduce.model import ReproductionSection

RUNS = Path(__file__).parent.parent / "fixtures" / "runs"


@pytest.fixture()
def authoring_snapshot() -> FailedRun:
    return load_snapshot((RUNS / "authoring.json").read_text())


def test_verdict_without_reproduction_is_unchanged(authoring_snapshot):
    doc = json.loads(render_verdict(diagnose_run(authoring_snapshot)))
    assert doc["verdict_schema_version"] == "1.1" and "reproduction" not in doc


def test_verdict_with_reproduction_is_1_2(authoring_snapshot):
    section = ReproductionSection(status="refused", refusal="event x not supported")
    doc = json.loads(render_verdict(diagnose_run(authoring_snapshot), section))
    assert doc["verdict_schema_version"] == "1.2"
    assert doc["reproduction"]["refusal"] == "event x not supported"
