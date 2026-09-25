"""The gate (F §2) and its publish-time re-check (F §8.3), over a real chain.

R replays run-1, a real ``ssh-keygen`` key signs a set into R's restored
``source/``, and A's ``prepare`` → ``decide`` → ``render_verdict`` yields an
``admitted`` verdict. The user's clone is a real Git checkout of that tree.
A bundle's ``head_sha`` cannot be a fresh commit's SHA, so the verdict's
``head_sha`` fields (run, binding, restoration) are re-pointed at the clone's
real ``HEAD``: every other byte of the admitted document is A's.
"""

import copy
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from deployer import runtime as runtime_mod
from deployer.admission import decide, prepare
from deployer.diagnose import diagnose_run, render_verdict
from deployer.fix.document import FixDocument, Input, Publication, StoredFile
from deployer.fix.gate import Admitted, gate, recheck_admission
from deployer.provenance import trust
from deployer.provenance.model import sha256_hex
from tests.admission.conftest import Replayed, replay_case
from tests.admission.test_end_to_end import _issue_into_source
from tests.provenance.conftest import make_key
from tests.reproduce.conftest import FakeContainers

ORIGIN = "git@github.com:example/project.git"
FIX_ID = "0b6f9c1e-3d2a-4c5b-8e7f-1a2b3c4d5e6f"


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in ``repo``, raising on failure; its stdout, stripped."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    """Stage everything, commit, and return the new ``HEAD``."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _at_head(document: dict[str, Any], head: str) -> dict[str, Any]:
    """``document`` with every ``head_sha`` A compares re-pointed at ``head``."""
    moved = copy.deepcopy(document)
    moved["run"]["head_sha"] = head
    moved["admission"]["binding"]["head_sha"] = head
    moved["reproduction"]["restoration"]["sha"] = head
    return moved


@dataclass
class Scenario:
    """An admitted verdict, R's tree, a clean clone at the admitted head, the
    trust dir and the planned fix dir."""

    r: Replayed
    original: dict[str, Any]
    clone: Path
    trust: Path
    pub: str
    fix_dir: Path

    @property
    def env(self) -> dict[str, str]:
        """The environment naming the trust dir."""
        return {"DEPLOYER_TRUST_DIR": str(self.trust)}

    @property
    def extra_roots(self) -> tuple[Path, Path]:
        """The planned fix dir and its worktree."""
        return (self.fix_dir, self.fix_dir / "worktree")

    def document(self) -> dict[str, Any]:
        """The verdict bound to the clone's current ``HEAD``."""
        return _at_head(self.original, _git(self.clone, "rev-parse", "HEAD"))

    def gate(
        self, document: dict[str, Any] | None = None, clone: Path | None = None
    ) -> Admitted | str:
        """Run the gate over this scenario."""
        return gate(
            self.document() if document is None else document,
            self.r.root,
            self.clone if clone is None else clone,
            self.env,
            self.extra_roots,
        )


@pytest.fixture()
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    """run-1 replayed (R's containers faked, as ``fake_containers`` does),
    signed, admitted, and cloned into a clean checkout."""
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    r = replay_case("run-1", tmp_path, fake)
    keys = tmp_path / "keys"
    keys.mkdir()
    key, pub = make_key(keys)
    trust_dir = tmp_path / "trust"
    trust.add(trust_dir, pub)
    env = {"DEPLOYER_TRUST_DIR": str(trust_dir)}
    unsigned = prepare(r.run, r.section, r.root, env)
    _issue_into_source(r, unsigned.head_listing, key)
    section = decide(prepare(r.run, r.section, r.root, env))
    assert section.verdict == "admitted", section.unmet
    original = json.loads(render_verdict(diagnose_run(r.run), r.section, section))
    clone = tmp_path / "clone"
    shutil.copytree(r.source, clone)
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    _commit_all(clone, "admitted tree")
    _git(clone, "remote", "add", "origin", ORIGIN)
    fix_dir = tmp_path / "fixes" / "001"
    return Scenario(r, original, clone, trust_dir, pub, fix_dir)


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
