"""The gate (F §2) and its publish-time re-check (F §8.3), over a real chain.

R replays run-1, a real ``ssh-keygen`` key signs a set into R's restored
``source/``, and A's ``prepare`` → ``decide`` → ``render_verdict`` yields an
``admitted`` verdict. The user's clone is a real Git checkout of that tree.
A bundle's ``head_sha`` cannot be a fresh commit's SHA, so the verdict's
``head_sha`` fields (run, binding, restoration) and R's ``source.json`` are
re-pointed at the clone's real ``HEAD``: every other byte of the admitted
document and of R's records is A's and R's.
"""

import dataclasses
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from deployer import runtime as runtime_mod
from deployer.fix import gate as gate_mod
from deployer.fix.document import FixDocument, Input, Publication, StoredFile
from deployer.fix.gate import Admitted, gate, recheck_admission
from deployer.provenance import trust
from deployer.provenance.model import sha256_hex
from tests.fix.conftest import Scenario, admitted_scenario
from tests.fix.conftest import commit_all as _commit_all
from tests.fix.conftest import git as _git
from tests.fix.conftest import restored_at as _restored_at
from tests.reproduce.conftest import FakeContainers

FIX_ID = "0b6f9c1e-3d2a-4c5b-8e7f-1a2b3c4d5e6f"


@pytest.fixture()
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    """run-1 replayed (R's containers faked, as ``fake_containers`` does),
    signed, admitted, and cloned into a clean checkout."""
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    return admitted_scenario("run-1", tmp_path, fake)


def _admitted(s: Scenario) -> Admitted:
    """The gate's result, asserted to be ``Admitted``."""
    result = s.gate()
    assert isinstance(result, Admitted), result
    return result


def _fix_document(s: Scenario, admitted: Admitted) -> FixDocument:
    """A ``fix.json`` as ``deployer fix`` would store it after the gate."""
    verdict = s.fix_dir.parent / "verdict.json"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(json.dumps(s.document()))
    link = admitted.section.link
    assert link is not None
    evidence = [
        s.r.try_dir / link.ci.evidence_file,
        s.r.try_dir / link.local.evidence_file,
    ]
    return FixDocument(
        schema_version="1.0",
        fix_id=FIX_ID,
        status="in_progress",
        input=Input(
            verdict=_stored(verdict),
            root=str(s.r.root),
            try_dir=str(s.r.try_dir.relative_to(s.r.root)),
            source_dir=str(s.r.source.relative_to(s.r.root)),
            evidence=[_stored(path) for path in evidence],
            binding={},
            reproduction_binding={},
            build={},
            workflow_path=".github/workflows/diagnosis-polygon.yml",
            workflow_sha256="0" * 64,
            backend="podman",
            clone=str(s.clone),
            origin=admitted.origin,
            head=admitted.clone_head,
            clean=True,
            target=asdict(admitted.target),
        ),
        publication=Publication(
            worktree=str(s.fix_dir / "worktree"),
            branch="deployer/fix/missing_copy_source/000000000000-001",
            fix_commit=None,
            diff_ok=False,
            base=None,
            pr_url=None,
        ),
    )


def _stored(path: Path) -> StoredFile:
    """``path`` with the SHA-256 of its current bytes."""
    return StoredFile(path=str(path), sha256=sha256_hex(path.read_bytes()))


# --- gate: accepted --------------------------------------------------------


def test_gate_admits_the_clean_clone_at_head(scenario: Scenario) -> None:
    """The whole chain passes: the target is derived from the clone."""
    admitted = _admitted(scenario)
    head = _git(scenario.clone, "rev-parse", "HEAD")
    dockerfile = (scenario.clone / "Dockerfile").read_bytes()
    assert admitted.clone_head == head
    assert admitted.origin == "example/project"
    assert admitted.target.repo == "example/project"
    assert admitted.target.head_sha == head
    assert admitted.target.artifact_path == "Dockerfile"
    assert admitted.target.artifact_sha256 == sha256_hex(dockerfile)
    assert admitted.section.verdict == "admitted"
    assert admitted.ownership.status == "confirmed"
    assert admitted.ownership.snapshot is not None


def test_gate_accepts_an_https_origin_in_other_letter_case(
    scenario: Scenario,
) -> None:
    """Review Focus 2: GitHub owner/name ignore case; so does the gate."""
    url = "HTTPS://GitHub.com/Example/Project"
    _git(scenario.clone, "remote", "set-url", "origin", url)
    admitted = _admitted(scenario)
    assert admitted.origin == "Example/Project"
    assert admitted.target.repo == "Example/Project"


# --- gate: the clone as a whole (§2 step 1) --------------------------------


def test_gate_refuses_a_directory_that_is_not_a_checkout(
    scenario: Scenario, tmp_path: Path
) -> None:
    """A plain directory is no clone."""
    plain = tmp_path / "plain"
    plain.mkdir()
    reason = scenario.gate(clone=plain)
    assert isinstance(reason, str) and "not a Git checkout" in reason


def test_gate_refuses_a_path_below_the_repository_root(scenario: Scenario) -> None:
    """The clone is checked at its toplevel, not from a subdirectory."""
    reason = scenario.gate(clone=scenario.clone / "src")
    assert isinstance(reason, str) and "not the repository root" in reason


def test_gate_refuses_a_clone_without_origin(scenario: Scenario) -> None:
    """The target's repo comes from ``origin``; none means no admission."""
    _git(scenario.clone, "remote", "remove", "origin")
    reason = scenario.gate()
    assert isinstance(reason, str) and "no parseable origin" in reason


def test_gate_refuses_untracked_only_dirt(scenario: Scenario) -> None:
    """An untracked file alone makes the clone unclean."""
    (scenario.clone / "notes.txt").write_text("scratch\n")
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "untracked changes: notes.txt" in reason


def test_gate_refuses_head_other_than_the_admitted_head(scenario: Scenario) -> None:
    """A clean clone one commit past the admitted ``head_sha`` is refused."""
    document = scenario.document()
    _commit_all(scenario.clone, "later")
    reason = scenario.gate(document)
    assert isinstance(reason, str)
    assert "is not the admitted head_sha" in reason


def test_gate_refuses_dockerfile_bytes_other_than_the_bound_ones(
    scenario: Scenario,
) -> None:
    """``HEAD`` equal and tree clean, but the Dockerfile is not the bound
    one: ``accept_for_fix`` refuses on the artifact hash."""
    dockerfile = scenario.clone / "Dockerfile"
    dockerfile.write_bytes(dockerfile.read_bytes() + b"# edited\n")
    _commit_all(scenario.clone, "edit the Dockerfile")
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "binding artifact_sha256" in reason


def test_gate_refuses_a_dockerfile_behind_a_symlink(scenario: Scenario) -> None:
    """The clone's Dockerfile is read without following symlinks."""
    dockerfile = scenario.clone / "Dockerfile"
    real = scenario.clone / "Dockerfile.real"
    dockerfile.rename(real)
    dockerfile.symlink_to(real.name)
    _commit_all(scenario.clone, "link the Dockerfile")
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "Dockerfile is not read" in reason and "symlink" in reason


# --- gate: admission and trust (§2 steps 4-5) ------------------------------


def test_gate_refuses_a_verdict_without_admission(scenario: Scenario) -> None:
    """A document without an admission binding never opens the gate."""
    document = scenario.document()
    del document["admission"]
    reason = scenario.gate(document)
    assert isinstance(reason, str) and "no admission binding" in reason


def test_gate_refuses_a_revoked_key(scenario: Scenario) -> None:
    """The trust set is re-read now: a key revoked since admission blocks."""
    trust.revoke(scenario.trust, scenario.pub)
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "ownership not confirmed at step 3" in reason


def test_gate_refuses_a_trust_dir_inside_source(scenario: Scenario) -> None:
    """A trust dir inside R's restored ``source/`` is refused (step 0)."""
    inside = scenario.r.source / "trust"
    shutil.copytree(scenario.trust, inside)
    scenario.trust = inside
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "ownership not confirmed at step 0" in reason


def test_gate_refuses_a_trust_dir_inside_the_fix_dir(scenario: Scenario) -> None:
    """``extra_roots`` are checked roots: a trust dir in the fix dir fails."""
    inside = scenario.fix_dir / "trust"
    shutil.copytree(scenario.trust, inside)
    scenario.trust = inside
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "ownership not confirmed at step 0" in reason


def test_gate_refuses_a_trust_dir_inside_the_clone(scenario: Scenario) -> None:
    """The clone is a checked root (inside ``.git``, so the tree stays clean)."""
    inside = scenario.clone / ".git" / "trust"
    shutil.copytree(scenario.trust, inside)
    scenario.trust = inside
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "ownership not confirmed at step 0" in reason


@pytest.mark.parametrize("try_dir", [None, "", "/abs/tries/001", 7])
def test_gate_refuses_a_missing_or_absolute_try_dir(
    scenario: Scenario, try_dir: object
) -> None:
    """R's try dir must be a relative path under ``root``."""
    document = scenario.document()
    document["reproduction"]["try_dir"] = try_dir
    reason = scenario.gate(document)
    assert isinstance(reason, str) and "reproduction.try_dir" in reason


def test_gate_turns_a_missing_clone_into_a_reason(
    scenario: Scenario, tmp_path: Path
) -> None:
    """Totality: a clone path that does not exist is a reason, not a raise."""
    reason = scenario.gate(clone=tmp_path / "absent")
    assert isinstance(reason, str)


# --- recheck_admission (§8.3) ----------------------------------------------


def test_recheck_passes_on_the_stored_inputs(scenario: Scenario) -> None:
    """Right after the gate, the stored inputs pass the re-check."""
    doc = _fix_document(scenario, _admitted(scenario))
    assert recheck_admission(doc, scenario.env) is None


def test_recheck_ignores_a_moved_clone_head(scenario: Scenario) -> None:
    """The user's own clone ``HEAD`` is not checked at publication."""
    doc = _fix_document(scenario, _admitted(scenario))
    (scenario.clone / "later.txt").write_text("later\n")
    _commit_all(scenario.clone, "the user moves on")
    assert recheck_admission(doc, scenario.env) is None


def test_recheck_ignores_a_dirty_clone(scenario: Scenario) -> None:
    """Nor is the clone's cleanliness."""
    doc = _fix_document(scenario, _admitted(scenario))
    (scenario.clone / "Dockerfile").write_text("FROM scratch\n")
    (scenario.clone / "notes.txt").write_text("scratch\n")
    assert recheck_admission(doc, scenario.env) is None


def test_recheck_refuses_a_revoked_key(scenario: Scenario) -> None:
    """A key revoked between admission and publication blocks it."""
    doc = _fix_document(scenario, _admitted(scenario))
    trust.revoke(scenario.trust, scenario.pub)
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "ownership not confirmed at step 3" in reason


def test_recheck_refuses_a_changed_verdict(scenario: Scenario) -> None:
    """The stored verdict is re-hashed before it is re-read."""
    doc = _fix_document(scenario, _admitted(scenario))
    Path(doc.input.verdict.path).write_text("{}")
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "has changed" in reason


def test_recheck_refuses_a_changed_evidence_file(scenario: Scenario) -> None:
    """So is every stored evidence file."""
    doc = _fix_document(scenario, _admitted(scenario))
    ci_log = Path(doc.input.evidence[0].path)
    ci_log.write_bytes(ci_log.read_bytes() + b"appended\n")
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "has changed" in reason


def test_recheck_uses_the_stored_target(scenario: Scenario) -> None:
    """The stored target, not the clone's, is what the verdict must bind."""
    doc = _fix_document(scenario, _admitted(scenario))
    wrong = {**doc.input.target, "artifact_sha256": "0" * 64}
    doc = doc.model_copy(
        update={"input": doc.input.model_copy(update={"target": wrong})}
    )
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "binding artifact_sha256" in reason


def test_recheck_refuses_a_trust_dir_inside_the_worktree(scenario: Scenario) -> None:
    """The stored worktree is a checked root at publication too."""
    doc = _fix_document(scenario, _admitted(scenario))
    inside = scenario.fix_dir / "worktree" / "trust"
    shutil.copytree(scenario.trust, inside)
    scenario.trust = inside
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "ownership not confirmed at step 0" in reason


def test_recheck_refuses_without_a_publication(scenario: Scenario) -> None:
    """With no worktree recorded, the checked roots are unknown: refused."""
    doc = _fix_document(scenario, _admitted(scenario))
    doc = doc.model_copy(update={"publication": None})
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "no fix worktree" in reason


def test_recheck_turns_a_non_json_verdict_into_a_reason(scenario: Scenario) -> None:
    """Totality: a stored verdict that is not JSON (hash re-recorded) is a
    reason, not a raise."""
    doc = _fix_document(scenario, _admitted(scenario))
    path = Path(doc.input.verdict.path)
    path.write_bytes(b"\xff not json")
    stored = _stored(path)
    doc = doc.model_copy(
        update={"input": doc.input.model_copy(update={"verdict": stored})}
    )
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "UnicodeDecodeError" in reason


# --- fix round 1: regressions from the task review's probes ----------------


def _with_input(doc: FixDocument, **changes: Any) -> FixDocument:
    """``doc`` with ``input`` fields replaced (no re-validation, as T14 may)."""
    return doc.model_copy(update={"input": doc.input.model_copy(update=changes)})


def test_gate_refuses_a_forged_binding_over_an_unsigned_edit(
    scenario: Scenario,
) -> None:
    """The review's false admission: an unsigned Dockerfile edit committed in
    the clone, the verdict's artifact hash forged to match. Refused."""
    dockerfile = scenario.clone / "Dockerfile"
    dockerfile.write_bytes(dockerfile.read_bytes() + b"RUN echo unsigned\n")
    _commit_all(scenario.clone, "unsigned edit")
    document = scenario.document()
    forged = sha256_hex(dockerfile.read_bytes())
    document["admission"]["binding"]["artifact_sha256"] = forged
    assert isinstance(scenario.gate(document), str)


def test_gate_refuses_when_r_restored_another_head(scenario: Scenario) -> None:
    """Clause 3: R's ``source.json`` must record the target's ``head_sha``;
    a verdict re-pointed at a later commit (same bytes) is refused."""
    _commit_all(scenario.clone, "later, same Dockerfile")
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "R's restoration is not the target's" in reason


def test_gate_refuses_bytes_other_than_r_restored(scenario: Scenario) -> None:
    """Clause 2: R's restored ``source/<artifact_path>`` must hash to the
    target's bytes, even when R's head and the forged binding agree."""
    dockerfile = scenario.clone / "Dockerfile"
    dockerfile.write_bytes(dockerfile.read_bytes() + b"RUN echo unsigned\n")
    head = _commit_all(scenario.clone, "unsigned edit")
    _restored_at(scenario.r.attempt_dir, head)
    document = scenario.document()
    forged = sha256_hex(dockerfile.read_bytes())
    document["admission"]["binding"]["artifact_sha256"] = forged
    reason = scenario.gate(document)
    assert isinstance(reason, str)
    assert "R's restored Dockerfile" in reason and "is not the target's" in reason


def test_gate_refuses_a_record_signing_other_bytes(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clause 1: the signed record's ``artifact_sha256`` must be the
    target's. Unreachable past ownership step 4 without a stand-in, so the
    confirmed facts are replayed with another record hash."""
    real = gate_mod.verify_ownership

    def other_record(source_dir: Path, **kwargs: Any) -> Any:
        facts = real(source_dir, **kwargs)
        assert facts.record is not None
        record = facts.record.model_copy(update={"artifact_sha256": "0" * 64})
        return dataclasses.replace(facts, record=record)

    monkeypatch.setattr(gate_mod, "verify_ownership", other_record)
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "signed record's artifact_sha256" in reason


def test_recheck_refuses_bytes_other_than_r_restored(scenario: Scenario) -> None:
    """The R-state binding protects publication too."""
    doc = _fix_document(scenario, _admitted(scenario))
    (scenario.r.source / "Dockerfile").write_text("FROM scratch\n")
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "R's restored Dockerfile" in reason


@pytest.mark.parametrize("document", [[], None, "x"])
def test_gate_refuses_a_non_mapping_document(scenario: Scenario, document: Any) -> None:
    """Totality: a JSON array, ``null`` or string verdict is a reason."""
    reason = gate(
        document, scenario.r.root, scenario.clone, scenario.env, scenario.extra_roots
    )
    assert isinstance(reason, str) and "not a JSON object" in reason


def test_gate_refuses_a_try_dir_climbing_out_of_root(scenario: Scenario) -> None:
    """``..`` in ``reproduction.try_dir`` is refused, even when it lands back
    on the same directory."""
    document = scenario.document()
    rel = document["reproduction"]["try_dir"]
    document["reproduction"]["try_dir"] = f"../{scenario.r.root.name}/{rel}"
    reason = scenario.gate(document)
    assert isinstance(reason, str) and "'..' component" in reason


def test_gate_refuses_an_origin_of_another_repository(scenario: Scenario) -> None:
    """An ``origin`` naming another repository does not bind."""
    url = "git@github.com:evil/project.git"
    _git(scenario.clone, "remote", "set-url", "origin", url)
    reason = scenario.gate()
    assert isinstance(reason, str) and "binding repo" in reason


def test_gate_admits_a_clone_with_only_ignored_files(scenario: Scenario) -> None:
    """Ignored files are not dirt (as A's provenance preflight reads it)."""
    (scenario.clone / ".gitignore").write_text("*.log\n")
    head = _commit_all(scenario.clone, "ignore logs")
    _restored_at(scenario.r.attempt_dir, head)
    (scenario.clone / "x.log").write_text("x")
    _admitted(scenario)


@pytest.mark.parametrize("where", ["source", "clone"])
def test_gate_refuses_a_trust_symlink_into_a_checked_root(
    scenario: Scenario, tmp_path: Path, where: str
) -> None:
    """A trust dir reached through a symlink is judged by its real path."""
    base = scenario.r.source if where == "source" else scenario.clone / ".git"
    inside = base / "trust2"
    shutil.copytree(scenario.trust, inside)
    link = tmp_path / "trustlink"
    link.symlink_to(inside)
    scenario.trust = link
    reason = scenario.gate()
    assert isinstance(reason, str)
    assert "ownership not confirmed at step 0" in reason


def test_gate_turns_an_unreadable_try_dir_into_a_reason(scenario: Scenario) -> None:
    """Totality: a try dir without permissions is a reason, not a raise."""
    os.chmod(scenario.r.try_dir, 0)
    try:
        reason = scenario.gate()
    finally:
        os.chmod(scenario.r.try_dir, 0o755)
    assert isinstance(reason, str)


@pytest.mark.parametrize("data", [b"[1,2]", b"null", b"[" * 200_000 + b"]" * 200_000])
def test_recheck_refuses_a_non_object_verdict(scenario: Scenario, data: bytes) -> None:
    """Totality: an array, ``null`` or a pathologically nested verdict is a
    reason, not a raise."""
    doc = _fix_document(scenario, _admitted(scenario))
    path = Path(doc.input.verdict.path)
    path.write_bytes(data)
    reason = recheck_admission(_with_input(doc, verdict=_stored(path)), scenario.env)
    assert reason is not None


def test_recheck_refuses_a_verdict_over_the_size_cap(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored verdict above :data:`MAX_VERDICT_BYTES` is not read."""
    doc = _fix_document(scenario, _admitted(scenario))
    monkeypatch.setattr(gate_mod, "MAX_VERDICT_BYTES", 16)
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "exceeds 16 bytes" in reason


@pytest.mark.parametrize("field", ["source_dir", "try_dir"])
def test_recheck_refuses_a_missing_stored_directory(
    scenario: Scenario, field: str
) -> None:
    """A stored directory that does not exist is a reason."""
    doc = _fix_document(scenario, _admitted(scenario))
    reason = recheck_admission(_with_input(doc, **{field: "nope/x"}), scenario.env)
    assert reason is not None


def test_recheck_refuses_evidence_it_did_not_hash(scenario: Scenario) -> None:
    """The evidence ``accept_for_fix`` reads must be among the re-hashed
    inputs: with none stored, a tampered ``ci.log`` is caught."""
    doc = _with_input(_fix_document(scenario, _admitted(scenario)), evidence=[])
    ci_log = scenario.r.try_dir / "ci.log"
    ci_log.write_bytes(ci_log.read_bytes() + b"tampered\n")
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "not among the stored hashed inputs" in reason


@pytest.mark.parametrize("field", ["try_dir", "source_dir"])
@pytest.mark.parametrize("prefix", ["../work/", "./", "a/../"])
def test_recheck_refuses_dot_components(
    scenario: Scenario, field: str, prefix: str
) -> None:
    """Stored ``try_dir``/``source_dir`` have no ``.`` or ``..`` component."""
    doc = _fix_document(scenario, _admitted(scenario))
    value = prefix + getattr(doc.input, field)
    reason = recheck_admission(_with_input(doc, **{field: value}), scenario.env)
    assert reason is not None and "component" in reason


@pytest.mark.parametrize("field", ["root", "clone", "worktree"])
def test_recheck_refuses_a_relative_anchor(scenario: Scenario, field: str) -> None:
    """``root``, ``clone`` and the worktree are stored absolute."""
    doc = _fix_document(scenario, _admitted(scenario))
    if field == "worktree":
        assert doc.publication is not None
        publication = doc.publication.model_copy(update={"worktree": "rel/wt"})
        doc = doc.model_copy(update={"publication": publication})
    else:
        doc = _with_input(doc, **{field: "rel/path"})
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and f"stored {field}" in reason
    assert "is not absolute" in reason


def test_recheck_refuses_an_absolute_stored_try_dir(scenario: Scenario) -> None:
    """``try_dir`` is relative to ``root``; an absolute one is refused."""
    doc = _fix_document(scenario, _admitted(scenario))
    doc = _with_input(doc, try_dir=str(scenario.r.try_dir))
    reason = recheck_admission(doc, scenario.env)
    assert reason is not None and "is absolute" in reason


@pytest.mark.parametrize(
    "source_dir",
    [
        "elsewhere/source",
        ".deployer-runs/35680991093/reproduction/attempt-2/source",
        ".deployer-runs/35680991093/reproduction/attempt-1/tries/source",
    ],
)
def test_recheck_refuses_a_source_dir_not_of_the_try_dirs_attempt(
    scenario: Scenario, source_dir: str
) -> None:
    """``source_dir`` is the try dir's attempt ``source/``, as ``gate``
    derives it; any other stored ``source_dir`` is refused."""
    doc = _fix_document(scenario, _admitted(scenario))
    assert doc.input.source_dir == str(Path(doc.input.try_dir).parent.parent / "source")
    reason = recheck_admission(_with_input(doc, source_dir=source_dir), scenario.env)
    assert reason is not None and "not the try dir's attempt source" in reason
