"""Replay acceptance over the committed admission bundles (A §8.2, §8.3
level P).

Each case under ``tests/fixtures/admission`` is replayed through R, then
:func:`prepare` → :func:`decide` with ``DEPLOYER_TRUST_DIR`` set as its
``expected.json`` says; the verdict, ownership, unmet conditions and defect
must equal ``expected.json``, and ``render_verdict`` → JSON →
:func:`accept_for_fix` against the case's own target must accept exactly the
two admitted cases. The data is read only: the carry-forward checks mutate
the replayed ``admit-run-1`` in the working root, never the bundle.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deployer.admission import decide, prepare
from deployer.admission.consumer import Accepted, Target, accept_for_fix
from deployer.admission.decide import VerifiedFacts
from deployer.admission.model import AdmissionSection
from deployer.diagnose import diagnose_run, render_verdict
from tests.admission.conftest import Replayed, replay_case
from tests.admission.test_bundle_integrity import SPEC_CASES
from tests.reproduce.conftest import FakeContainers

ADMISSION = Path(__file__).parent.parent / "fixtures" / "admission"
CASES = sorted(p.name for p in ADMISSION.iterdir() if (p / "expected.json").is_file())
ADMITTED = ("admit-run-1", "admit-run-5")


@dataclass(frozen=True)
class Outcome:
    """One case replayed and decided: R's try, the facts, the section."""

    replayed: Replayed
    facts: VerifiedFacts
    section: AdmissionSection


def _expected(case: str) -> dict[str, Any]:
    """The case's ``expected.json``."""
    return json.loads((ADMISSION / case / "expected.json").read_text())


def _trust_env(r: Replayed, trust: dict[str, str], tmp_path: Path) -> dict[str, str]:
    """``DEPLOYER_TRUST_DIR`` as ``expected.json``'s ``trust`` names it."""
    kind, rel = trust["kind"], trust["path"]
    if kind == "dir":
        path = ADMISSION / rel
    elif kind == "inside_tree":
        path = r.source / rel
    else:
        assert kind == "symlink_into_tree", kind
        path = tmp_path / "trust-link"
        path.symlink_to(r.source / rel, target_is_directory=True)
    assert path.resolve().is_dir(), path
    return {"DEPLOYER_TRUST_DIR": str(path)}


def _decided(case: str, tmp_path: Path, fake: FakeContainers) -> Outcome:
    """Replay ``case`` through R, then prepare and decide it."""
    r = replay_case(case, tmp_path, fake, ADMISSION)
    env = _trust_env(r, _expected(case)["trust"], tmp_path)
    facts = prepare(r.run, r.section, r.root, env)
    return Outcome(r, facts, decide(facts))


def _document(o: Outcome) -> Any:
    """The rendered verdict document, parsed back from JSON."""
    r = o.replayed
    return json.loads(render_verdict(diagnose_run(r.run), r.section, o.section))


def _target(facts: VerifiedFacts) -> Target:
    """The case's own target: the binding ``prepare`` computed."""
    b = facts.binding
    assert b.artifact_sha256 is not None
    return Target(b.repo, b.head_sha, b.artifact_path, b.artifact_sha256)


def _accept(o: Outcome, target: Target) -> object:
    """``accept_for_fix`` of the case's rendered document for ``target``."""
    return accept_for_fix(_document(o), o.replayed.try_dir, target)


def test_the_replayed_cases_are_exactly_the_spec_cases() -> None:
    """A bundle that goes missing fails here, not silently in the params."""
    assert CASES == SPEC_CASES


@pytest.mark.parametrize("case", SPEC_CASES)
def test_bundle_replays_to_its_expected_outcome(
    tmp_path: Path, fake_containers: FakeContainers, case: str
) -> None:
    """Verdict, ownership, unmet, defect and acceptance as ``expected.json``."""
    want = _expected(case)
    o = _decided(case, tmp_path, fake_containers)
    assert o.section.verdict == want["verdict"], o.section.unmet
    ownership = o.facts.ownership
    got_ownership = {"status": ownership.status, "step": ownership.step}
    assert got_ownership == want["ownership"], ownership.reason
    assert o.section.ownership.status == want["ownership"]["status"]
    reasons = {u.condition: u.reason for u in o.section.unmet}
    assert sorted(reasons) == sorted(u["condition"] for u in want["unmet"])
    assert len(reasons) == len(o.section.unmet)
    for unmet in want["unmet"]:
        for part in unmet["reason_contains"]:
            assert part in reasons[unmet["condition"]]
    if "defect" in want:
        d = o.section.defect
        assert d is not None
        got = {"class": d.cls, "file": d.file, "lines": list(d.lines)}
        assert got == want["defect"]
    else:
        assert o.section.defect is None
    result = _accept(o, _target(o.facts))
    assert isinstance(result, Accepted) is want["accepted"], result


def test_admitted_cases_are_refused_against_each_others_target(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """``admit-run-1``'s document does not open ``admit-run-5``'s target,
    nor the reverse."""
    one = _decided("admit-run-1", tmp_path / "one", fake_containers)
    five = _decided("admit-run-5", tmp_path / "five", fake_containers)
    assert isinstance(_accept(one, _target(one.facts)), Accepted)
    assert isinstance(_accept(five, _target(five.facts)), Accepted)
    assert not isinstance(_accept(one, _target(five.facts)), Accepted)
    assert not isinstance(_accept(five, _target(one.facts)), Accepted)


@pytest.mark.parametrize("case", ADMITTED)
def test_admitted_document_names_its_class_and_resolves_its_evidence(
    tmp_path: Path, fake_containers: FakeContainers, case: str
) -> None:
    """The rendered document carries ``"class"``; every evidence line
    resolves (``Accepted`` checks each against its file under the try)."""
    o = _decided(case, tmp_path, fake_containers)
    document = _document(o)
    assert '"class"' in json.dumps(document)
    assert (
        document["admission"]["defect"]["class"] == _expected(case)["defect"]["class"]
    )
    link = o.section.link
    assert link is not None
    assert link.ci.evidence_lines and link.local.evidence_lines
    result = accept_for_fix(document, o.replayed.try_dir, _target(o.facts))
    assert isinstance(result, Accepted), result


@pytest.mark.parametrize(
    "build_arg",
    [
        ["--build-arg", "BUILDKIT_SYNTAX=docker/dockerfile:1.7"],
        ["--build-arg=BUILDKIT_SYNTAX=docker/dockerfile:1.7"],
    ],
)
def test_admit_run_1_with_a_ci_frontend_build_arg_is_refused_at_3(
    tmp_path: Path, fake_containers: FakeContainers, build_arg: list[str]
) -> None:
    """A CI build line switching the frontend is an unknown dialect at (3),
    and the signed, otherwise admissible run is not admitted."""
    r = replay_case("admit-run-1", tmp_path, fake_containers, ADMISSION)
    assert r.section.build is not None
    argv = r.section.build.argv
    build = r.section.build.model_copy(
        update={"argv": [*argv[:2], *build_arg, *argv[2:]]}
    )
    section = r.section.model_copy(update={"build": build})
    env = _trust_env(r, _expected("admit-run-1")["trust"], tmp_path)
    facts = prepare(r.run, section, r.root, env)
    assert facts.ownership.status == "confirmed", facts.ownership.reason
    admission = decide(facts)
    assert admission.verdict == "insufficient_grounds"
    reasons = {u.condition: u.reason for u in admission.unmet}
    assert "unknown dialect: # syntax=docker/dockerfile:1.7 (CI)" in reasons[3]
    document = json.loads(render_verdict(diagnose_run(r.run), section, admission))
    result = accept_for_fix(document, r.try_dir, _target(facts))
    assert not isinstance(result, Accepted)


def test_admit_run_1_with_a_truncated_head_listing_is_refused_at_2(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """R's stored listing marked truncated (restoration still ``exact``):
    the completeness flag alone refuses absence at (2)."""
    r = replay_case("admit-run-1", tmp_path, fake_containers, ADMISSION)
    assert r.section.restoration is not None
    assert r.section.restoration.state == "exact"
    meta = r.attempt_dir / "source.json"
    source_json = json.loads(meta.read_text())
    source_json["listing"]["truncated"] = True
    meta.write_text(json.dumps(source_json))
    env = _trust_env(r, _expected("admit-run-1")["trust"], tmp_path)
    facts = prepare(r.run, r.section, r.root, env)
    assert facts.head_listing_complete is False
    assert facts.ownership.status == "confirmed", facts.ownership.reason
    admission = decide(facts)
    assert admission.verdict == "insufficient_grounds"
    reasons = {u.condition: u.reason for u in admission.unmet}
    assert list(reasons) == [2]
    assert "head_sha listing incomplete; absence not provable" in reasons[2]
