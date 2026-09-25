"""The preparation layer (A §6.1) over R's real try: a committed bundle is
replayed through R (as R §8.A does), then :func:`prepare` gathers the
verified facts from what R left for that attempt."""

import dataclasses
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from deployer import runtime as runtime_mod
from deployer.admission import decide, prepare
from deployer.admission.ownership import verify_ownership
from deployer.forge import FailedRun, load_snapshot
from deployer.models import ContainerRuntime
from deployer.provenance.model import TreeRow
from deployer.reproduce import shape as shape_mod
from deployer.reproduce.build import run_build
from deployer.reproduce.buildline import BuildConfig, parse_build_line
from deployer.reproduce.model import ReproductionSection
from deployer.reproduce.run import TryDirError, _make_writable, reproduce_run
from deployer.reproduce.shape import job_text
from tests.admission.conftest import REPO, AdmissionSet
from tests.reproduce.bundles import BUNDLES, BundleGh, _containers
from tests.reproduce.conftest import FakeContainers, proc


@dataclass(frozen=True)
class Replayed:
    """R's attempted try of one bundle under ``root``."""

    bundle: Path
    run: FailedRun
    section: ReproductionSection
    root: Path

    @property
    def try_dir(self) -> Path:
        """The try directory R wrote."""
        assert self.section.try_dir is not None
        return self.root / self.section.try_dir

    @property
    def attempt_dir(self) -> Path:
        """The attempt directory holding ``source/`` and ``source.json``."""
        return self.try_dir.parent.parent

    @property
    def source(self) -> Path:
        """The tree R restored at ``head_sha``."""
        return self.attempt_dir / "source"

    def unlock(self) -> Path:
        """Make R's read-only ``source/`` writable for a targeted mutation."""
        _make_writable(self.source)
        return self.source


@pytest.fixture()
def fake_containers(monkeypatch: pytest.MonkeyPatch) -> FakeContainers:
    """R's container calls answered by a fake, as in R §8.A."""
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    return fake


def _replay(case: str, tmp_path: Path, fake: FakeContainers) -> Replayed:
    """Replay ``case`` through R under ``tmp_path / "work"``."""
    bundle = BUNDLES / case
    rt, env = _containers(bundle, fake)
    run = load_snapshot((bundle / "snapshot.json").read_text())
    root = tmp_path / "work"
    section = reproduce_run(
        run,
        gh=BundleGh(bundle, tmp_path / "bundle"),
        rt=rt,
        runtime_error=None,
        env=env,
        root=root,
        build_timeout=60,
    )
    assert section.status == "attempted"
    return Replayed(bundle, run, section, root)


def _env(tmp_path: Path) -> dict[str, str]:
    """A trust directory outside the checked roots (A §2.3)."""
    trust = tmp_path / "trust"
    trust.mkdir(exist_ok=True)
    return {"DEPLOYER_TRUST_DIR": str(trust)}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_run_1_facts_are_rs_own(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Rulings 1-6 over the replayed run-1 try."""
    r = _replay("run-1", tmp_path, fake_containers)
    facts = prepare(r.run, r.section, r.root, _env(tmp_path))
    source_json = json.loads((r.attempt_dir / "source.json").read_text())
    # (1) the head listing is R's source.json listing for the same attempt
    stored = [TreeRow(**e) for e in source_json["listing"]["entries"]]
    assert facts.head_listing == stored
    bundle_listing = json.loads((r.bundle / "tree-listing.json").read_text())
    assert facts.head_listing == [TreeRow(**e) for e in bundle_listing["tree"]]
    assert facts.head_listing_complete is True
    # (3) ci_text is the job R compared; ci.log is exactly that text
    assert r.section.binding is not None
    job = next(j for j in r.run.jobs if j.job_id == r.section.binding.job_id)
    assert facts.ci_text == job_text(job)
    assert facts.ci_evidence_file == "ci.log"
    written = (r.try_dir / facts.ci_evidence_file).read_bytes()
    assert written == facts.ci_text.encode()
    # (2) no ignore file on either side: R compared (None, None)
    assert r.section.comparison is not None
    assert r.section.comparison.values["ignore_file"] == (None, None)
    assert facts.ignore_hashes == ((None, None), (None, None))
    # (6) the binding
    dockerfile = (r.bundle / "tree" / "Dockerfile").read_bytes()
    assert facts.binding.repo == r.run.repo
    assert facts.binding.head_sha == r.run.head_sha
    assert facts.binding.artifact_path == "Dockerfile"
    assert facts.binding.artifact_sha256 == _sha(dockerfile)
    # (4) ownership comes from verify_ownership itself (no set: step 1)
    assert facts.ownership == verify_ownership(
        r.source,
        repo=r.run.repo,
        artifact_path="Dockerfile",
        trust=tmp_path / "trust",
        checked_roots=(r.source, r.root),
    )
    assert facts.ownership.status == "not_confirmed"
    # (5) the local directive is R's environment's; R records none for CI
    assert r.section.environment is not None
    assert facts.syntax_directive_local == r.section.environment.syntax_directive
    assert facts.syntax_directive_ci is None
    # the local side reads R's own build output files
    assert facts.local_stdout == (r.bundle / "local.stdout").read_text()
    assert facts.local_stderr == (r.bundle / "local.stderr").read_text()
    assert facts.reproduction == r.section
    # without a set only ownership is unmet: the rest of run-1 holds
    section = decide(facts)
    assert section.verdict == "insufficient_grounds"
    assert [u.condition for u in section.unmet] == [1]


def test_ignore_hashes_over_rs_effective_paths(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Ruling 2: podman's ``.containerignore`` locally, ``.dockerignore`` in
    CI, each hashed from the restored tree."""
    r = _replay("containerignore", tmp_path, fake_containers)
    facts = prepare(r.run, r.section, r.root, _env(tmp_path))
    assert r.section.comparison is not None
    ci_path, local_path = r.section.comparison.values["ignore_file"]
    assert (ci_path, local_path) == (".dockerignore", ".containerignore")
    tree = r.bundle / "tree"
    assert facts.ignore_hashes == (
        (".dockerignore", _sha((tree / ".dockerignore").read_bytes())),
        (".containerignore", _sha((tree / ".containerignore").read_bytes())),
    )


def test_symlinked_ignore_file_has_no_hash(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """A present path the no-follow reader refuses: its hash is not obtained
    (A §1), so ``ignore_file: same`` cannot be concluded."""
    r = _replay("containerignore", tmp_path, fake_containers)
    source = r.unlock()
    (source / ".dockerignore").unlink()
    os.symlink(".containerignore", source / ".dockerignore")
    facts = prepare(r.run, r.section, r.root, _env(tmp_path))
    (ci_path, ci_sha), (local_path, local_sha) = facts.ignore_hashes
    assert (ci_path, ci_sha) == (".dockerignore", None)
    assert local_path == ".containerignore" and local_sha is not None


def test_truncated_listing_passes_through_and_is_not_admitted(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Ruling 1: R's listing, marked truncated, is used as stored and
    reported incomplete; the decision refuses absence over it (2), besides
    R's approximate restoration (3)."""
    bundle = tmp_path / "bundle-src"
    shutil.copytree(BUNDLES / "run-1", bundle)
    listing = json.loads((bundle / "tree-listing.json").read_text())
    listing["truncated"] = True
    (bundle / "tree-listing.json").write_text(json.dumps(listing))
    rt, env = _containers(bundle, fake_containers)
    run = load_snapshot((bundle / "snapshot.json").read_text())
    root = tmp_path / "work"
    section = reproduce_run(
        run,
        gh=BundleGh(bundle, tmp_path / "gh"),
        rt=rt,
        runtime_error=None,
        env=env,
        root=root,
        build_timeout=60,
    )
    facts = prepare(run, section, root, _env(tmp_path))
    assert facts.head_listing == [TreeRow(**e) for e in listing["tree"]]
    assert facts.head_listing_complete is False
    admission = decide(facts)
    assert admission.verdict == "insufficient_grounds"
    reasons = {u.condition: u.reason for u in admission.unmet}
    assert "head_sha listing incomplete; absence not provable" in reasons[2]
    assert "restoration approximation, not exact" in reasons[3]


def test_source_json_of_another_sha_is_a_try_dir_error(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Ruling 7: R's records unreadable or foreign → TryDirError, as R."""
    r = _replay("run-1", tmp_path, fake_containers)
    meta = r.attempt_dir / "source.json"
    data = json.loads(meta.read_text())
    meta.write_text(json.dumps({**data, "head_sha": "f" * 40}))
    with pytest.raises(TryDirError, match="names f"):
        prepare(r.run, r.section, r.root, _env(tmp_path))
    meta.write_text("[]")
    with pytest.raises(TryDirError, match="not a JSON object"):
        prepare(r.run, r.section, r.root, _env(tmp_path))
    meta.unlink()
    with pytest.raises(TryDirError, match="cannot read"):
        prepare(r.run, r.section, r.root, _env(tmp_path))


def test_ci_log_write_failure_is_a_try_dir_error(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Ruling 7: ``ci.log`` goes through R's write helper."""
    r = _replay("run-1", tmp_path, fake_containers)
    (r.try_dir / "ci.log").mkdir()
    with pytest.raises(TryDirError, match="cannot write"):
        prepare(r.run, r.section, r.root, _env(tmp_path))


def test_symlinked_dockerfile_has_no_artifact_hash(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Ruling 6: the artifact is read without following links; a hash not
    obtained is absent (A §1) and the verdict is not ``admitted``."""
    r = _replay("run-1", tmp_path, fake_containers)
    source = r.unlock()
    (source / "Dockerfile").rename(source / "Dockerfile.real")
    os.symlink("Dockerfile.real", source / "Dockerfile")
    facts = prepare(r.run, r.section, r.root, _env(tmp_path))
    assert facts.binding.artifact_sha256 is None
    assert facts.parsed.instructions == []
    section = decide(facts)
    assert section.verdict == "insufficient_grounds"
    assert section.binding.artifact_sha256 is None


def test_trust_dir_inside_the_working_root_is_refused(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """``root`` is a checked root: a trust dir under it fails step 0."""
    r = _replay("run-1", tmp_path, fake_containers)
    trust = r.root / "trust"
    trust.mkdir()
    facts = prepare(r.run, r.section, r.root, {"DEPLOYER_TRUST_DIR": str(trust)})
    assert facts.ownership.status == "not_confirmed"
    assert facts.ownership.step == 0


def test_not_attempted_section_is_not_prepared(tmp_path: Path) -> None:
    """Ruling 7: no try, no admission."""
    run = load_snapshot((BUNDLES / "run-1" / "snapshot.json").read_text())
    refused = ReproductionSection(status="refused", refusal="event x")
    with pytest.raises(ValueError, match="attempted"):
        prepare(run, refused, tmp_path, _env(tmp_path))


def test_end_to_end_issued_set_is_confirmed(
    tmp_path: Path, fake_containers: FakeContainers, admission_set: AdmissionSet
) -> None:
    """A §8.2: ``provenance.issue`` in a temporary Git repository → its tree
    as the restored ``source/`` → the preparation layer → confirmed."""
    r = _replay("run-1", tmp_path, fake_containers)
    source = r.unlock()
    shutil.rmtree(source)
    shutil.copytree(admission_set.source, source)
    run = dataclasses.replace(r.run, repo=REPO)
    env = {"DEPLOYER_TRUST_DIR": str(admission_set.trust)}
    facts = prepare(run, r.section, r.root, env)
    assert facts.ownership.status == "confirmed", facts.ownership.reason
    assert facts.ownership == verify_ownership(
        source,
        repo=REPO,
        artifact_path="Dockerfile",
        trust=admission_set.trust,
        checked_roots=(source, r.root),
    )
    artifact = (admission_set.source / "Dockerfile").read_bytes()
    assert facts.binding.artifact_sha256 == _sha(artifact)
    record = facts.ownership.record
    assert record is not None and record.artifact_sha256 == _sha(artifact)


@pytest.mark.parametrize(
    "build_arg",
    [
        ["--build-arg", "BUILDKIT_SYNTAX=docker/dockerfile:1.7"],
        ["--build-arg=BUILDKIT_SYNTAX=docker/dockerfile:1.7"],
    ],
)
def test_ci_frontend_build_arg_is_a_ci_dialect(
    tmp_path: Path, fake_containers: FakeContainers, build_arg: list[str]
) -> None:
    """T11 fix round 1: ``BUILDKIT_SYNTAX`` switches CI's frontend (Podman
    ignores it), so it is CI's dialect and the link is refused at (3)."""
    r = _replay("run-1", tmp_path, fake_containers)
    assert r.section.build is not None
    argv = r.section.build.argv
    build = r.section.build.model_copy(
        update={"argv": [*argv[:2], *build_arg, *argv[2:]]}
    )
    section = r.section.model_copy(update={"build": build})
    facts = prepare(r.run, section, r.root, _env(tmp_path))
    assert facts.syntax_directive_ci == "docker/dockerfile:1.7"
    reasons = {u.condition: u.reason for u in decide(facts).unmet}
    assert "unknown dialect: # syntax=docker/dockerfile:1.7 (CI)" in reasons[3]


def test_rs_argv_carries_both_build_arg_forms_as_pairs(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """R's parser accepts ``--build-arg=K=V`` and ``--build-arg K=V``; the
    argv R records carries each as the pair ``prepare`` reads."""
    config = parse_build_line(
        "docker build --build-arg=BUILDKIT_SYNTAX=a --build-arg BUILDKIT_SYNTAX=b ."
    )
    assert isinstance(config, BuildConfig)
    fake_containers.responses[("build",)] = proc(0)
    run = run_build(
        ContainerRuntime(tool="podman"), tmp_path, config, "localhost/t", 60
    )
    assert ["--build-arg", "BUILDKIT_SYNTAX=a"] == run.argv[4:6]
    assert ["--build-arg", "BUILDKIT_SYNTAX=b"] == run.argv[6:8]


def test_no_frontend_build_arg_means_no_ci_dialect(
    tmp_path: Path, fake_containers: FakeContainers
) -> None:
    """Another build arg (or a key merely prefixed alike) is not a switch."""
    r = _replay("run-1", tmp_path, fake_containers)
    assert r.section.build is not None
    argv = r.section.build.argv
    extra = ["--build-arg", "BUILDKIT_SYNTAXX=x", "--build-arg", "V=1"]
    build = r.section.build.model_copy(update={"argv": [*argv[:2], *extra]})
    section = r.section.model_copy(update={"build": build})
    facts = prepare(r.run, section, r.root, _env(tmp_path))
    assert facts.syntax_directive_ci is None


def test_unencodable_ci_text_is_a_try_dir_error(
    tmp_path: Path, fake_containers: FakeContainers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T11 fix round 1: ``ci.log`` that cannot be encoded is R's try-dir
    exit 2, not a traceback out of ``diagnose``."""
    r = _replay("run-1", tmp_path, fake_containers)
    monkeypatch.setattr(shape_mod, "job_text", lambda job: "bad \udc80 byte")
    with pytest.raises(TryDirError, match="cannot write"):
        prepare(r.run, r.section, r.root, _env(tmp_path))
