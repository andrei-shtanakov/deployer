"""Verdict schema 1.1 → 1.2 → 1.3, each additive: 1.2 only with a
``reproduction`` key, 1.3 only with an ``admission`` key as well."""

import json
from pathlib import Path

import pytest

from deployer.admission.model import AdmissionSection, Ownership, Unmet
from deployer.admission.model import Binding as AdmissionBinding
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


def _admission() -> AdmissionSection:
    return AdmissionSection(
        verdict="insufficient_grounds",
        binding=AdmissionBinding(
            repo="o/r",
            head_sha="a" * 40,
            artifact_path="Dockerfile",
            artifact_sha256="b" * 64,
        ),
        ownership=Ownership(status="not_confirmed", reason="no set"),
        unmet=[Unmet(condition=1, reason="no set")],
    )


def test_verdict_with_admission_is_1_3(authoring_snapshot: FailedRun) -> None:
    section = ReproductionSection(status="refused", refusal="x", try_dir="t")
    admission = _admission()
    doc = json.loads(
        render_verdict(diagnose_run(authoring_snapshot), section, admission)
    )
    assert doc["verdict_schema_version"] == "1.3"
    assert doc["admission"] == admission.model_dump(mode="json")
    assert doc["reproduction"]["try_dir"] == "t"


def test_verdict_without_admission_stays_1_2(authoring_snapshot: FailedRun) -> None:
    section = ReproductionSection(status="refused", refusal="x", try_dir="t")
    doc = json.loads(render_verdict(diagnose_run(authoring_snapshot), section))
    assert doc["verdict_schema_version"] == "1.2" and "admission" not in doc


def test_admission_without_reproduction_is_rejected(
    authoring_snapshot: FailedRun,
) -> None:
    with pytest.raises(ValueError, match="reproduction"):
        render_verdict(diagnose_run(authoring_snapshot), None, _admission())
