"""The whole admission chain with real signing (A §8.2, §7): a real
``ssh-keygen`` key, :func:`issue.issue` publishing a set into the tree R
restored at ``head_sha``, then :func:`prepare` → :func:`decide` →
``render_verdict`` → :func:`accept_for_fix` → ``Accepted``.

The set is issued with a hand-built :class:`issue.Preflight`: the real
preflight needs a checkout whose ``HEAD`` is the run's ``head_sha``, which a
committed bundle cannot give. Everything after preflight — canonical bytes,
signing, exclusion, publication, the pointer — is the real ``issue``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deployer.admission import decide, prepare
from deployer.admission.consumer import Accepted, Target, accept_for_fix
from deployer.diagnose import diagnose_run, render_verdict
from deployer.facts import analyze_project
from deployer.provenance import issue, trust
from deployer.provenance.model import TreeRow
from tests.admission.conftest import Replayed, replay_case
from tests.provenance.conftest import make_key
from tests.reproduce.conftest import FakeContainers


def _issue_into_source(r: Replayed, listing: list[TreeRow], key: Path) -> None:
    """Publish a real signed set for ``Dockerfile`` into R's restored tree.

    ``issue`` takes its lock and writes its exclusions under ``.git``, so the
    tree is made a Git directory for the call and restored to a plain tree
    after it.
    """
    source = r.unlock()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    pre = issue.Preflight(
        project=source,
        repo=r.run.repo,
        source_commit=r.run.head_sha,
        tree=listing,
        facts=analyze_project(source),
    )
    out = issue.issue(pre, key, "0.1", (source / "Dockerfile").read_bytes())
    assert out.published, out.reason
    shutil.rmtree(source / ".git")


@pytest.mark.parametrize(
    ("case", "cls"),
    [("run-1", "missing_copy_source"), ("run-5", "from_argument_count")],
)
def test_signed_run_is_admitted_and_accepted(
    tmp_path: Path, fake_containers: FakeContainers, case: str, cls: str
) -> None:
    """Issue → restore → prepare → decide → render → accept, per bundle."""
    r = replay_case(case, tmp_path, fake_containers)
    keys = tmp_path / "keys"
    keys.mkdir()
    key, pub = make_key(keys)
    trust_dir = tmp_path / "trust"
    trust.add(trust_dir, pub)
    env = {"DEPLOYER_TRUST_DIR": str(trust_dir)}
    unsigned = prepare(r.run, r.section, r.root, env)
    assert unsigned.ownership.status == "not_confirmed"
    _issue_into_source(r, unsigned.head_listing, key)

    facts = prepare(r.run, r.section, r.root, env)
    assert facts.ownership.status == "confirmed", facts.ownership.reason
    section = decide(facts)
    assert section.verdict == "admitted", section.unmet
    assert section.defect is not None and section.defect.cls == cls
    document = json.loads(render_verdict(diagnose_run(r.run), r.section, section))
    binding = facts.binding
    assert binding.artifact_sha256 is not None
    target = Target(
        binding.repo, binding.head_sha, binding.artifact_path, binding.artifact_sha256
    )

    result = accept_for_fix(document, r.try_dir, target)
    assert isinstance(result, Accepted), result
    assert result.section == section
