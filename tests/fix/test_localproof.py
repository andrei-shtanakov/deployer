"""The local proof (F §6) over R's replayed run-1 and run-5, containers faked.

No real build runs: :class:`FakeContainers` answers every container call,
and the build's stdout/stderr are synthetic where a test needs a "passed"
template (enabled only through ``enable_for_test``).
"""

import dataclasses
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from deployer import runtime as runtime_mod
from deployer.admission.model import Defect, DefectClass
from deployer.fix.binding import Bound, bind_instruction
from deployer.fix.localproof import (
    LocalResult,
    build_config,
    fix_tag,
    local_proof,
)
from deployer.fix.workspace import new_fix_dir
from deployer.models import ContainerRuntime
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.run import _make_writable
from tests.admission.conftest import Replayed, replay_case
from tests.fix.conftest import enable_for_test
from tests.reproduce.conftest import FakeContainers, proc

FIX_ID = "0b6f9c1e-3d2a-4c5b-8e7f-1a2b3c4d5e6f"
CI_TAG = "ghcr.io/example/project:ci"
ABSENT = "docs/setup.md"
NEW = "tests/test_greeting.py"
COPY_TEXT = f"COPY {NEW} ./setup.md"
FROM_TEXT = "FROM python:3.12-slim AS extra"
PODMAN = ContainerRuntime(tool="podman")


@dataclass
class Case:
    """A replayed run, its bound defect, the corrected bytes and a fix dir."""

    r: Replayed
    fake: FakeContainers
    bound: Bound
    cls: DefectClass
    corrected: bytes
    position: int | None
    new_source: str | None
    fix_dir: Path

    def run(
        self,
        *,
        rt: ContainerRuntime = PODMAN,
        env: dict[str, str] | None = None,
        corrected: bytes | None = None,
        fix_dir: Path | None = None,
        fix_id: str = FIX_ID,
        build: BuildConfig | None = None,
    ) -> LocalResult:
        """Run the local proof with this case's defaults."""
        return local_proof(
            self.r.section,
            self.r.source,
            self.fix_dir if fix_dir is None else fix_dir,
            self.corrected if corrected is None else corrected,
            self.bound,
            self.cls,
            self.position,
            self.new_source,
            rt,
            env or {},
            build or BuildConfig("Dockerfile", (), None, CI_TAG),
            fix_id,
            60,
        )

    def builds(self) -> list[list[str]]:
        """Every build call the fake saw."""
        return [call for call in self.fake.calls if call[:1] == ["build"]]

    def set_build(self, code: int, stdout: str = "", stderr: str = "") -> None:
        """Script the fake's build answer."""
        self.fake.responses[("build",)] = proc(code, stdout=stdout, stderr=stderr)


def _bind(r: Replayed, cls: DefectClass, needle: str) -> tuple[bytes, Bound]:
    """The original Dockerfile and the instruction holding ``needle``, bound."""
    original = (r.source / "Dockerfile").read_bytes()
    lines = original.decode().splitlines()
    number = next(n for n, line in enumerate(lines, 1) if needle in line)
    obj = ABSENT if cls == "missing_copy_source" else lines[number - 1]
    defect = Defect.model_validate(
        {"class": cls, "file": "Dockerfile", "lines": (number, number), "object": obj}
    )
    bound = bind_instruction(original, defect)
    assert isinstance(bound, Bound), bound
    return original, bound


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeContainers:
    """R's and the fix's container calls answered by one fake."""
    containers = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", containers)
    return containers


@pytest.fixture()
def copy_case(tmp_path: Path, fake: FakeContainers) -> Case:
    """run-1: ``COPY docs/setup.md`` rewritten to an existing source."""
    r = replay_case("run-1", tmp_path, fake)
    fake.calls.clear()
    original, bound = _bind(r, "missing_copy_source", ABSENT)
    corrected = original.replace(ABSENT.encode(), NEW.encode())
    fix_dir = tmp_path / "fix"
    fix_dir.mkdir()
    return Case(r, fake, bound, "missing_copy_source", corrected, 0, NEW, fix_dir)


@pytest.fixture()
def from_case(tmp_path: Path, fake: FakeContainers) -> Case:
    """run-5: ``FROM python:3.12-slim extra`` gains ``AS``."""
    r = replay_case("run-5", tmp_path, fake)
    fake.calls.clear()
    original, bound = _bind(r, "from_argument_count", "FROM python")
    corrected = original.replace(b"slim extra", b"slim AS extra")
    fix_dir = tmp_path / "fix"
    fix_dir.mkdir()
    return Case(r, fake, bound, "from_argument_count", corrected, None, None, fix_dir)


def _copy_stdout(later: str = "Successfully tagged localhost/x") -> str:
    """Podman-shaped output passing the corrected COPY step."""
    return (
        "STEP 1/12: FROM python:3.12-slim\n"
        f"STEP 7/12: {COPY_TEXT}\n"
        "STEP 8/12: RUN uv sync --frozen\n"
        f"{later}\n"
    )


def _tree_hash(root: Path) -> str:
    """Paths, modes, bytes and link targets of every entry under ``root``."""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in sorted(os.walk(root)):
        dirnames.sort()
        for name in sorted([*dirnames, *filenames]):
            path = Path(dirpath) / name
            info = path.lstat()
            digest.update(f"{path.relative_to(root)}\0{info.st_mode}\0".encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _tag(argv: list[str]) -> str:
    """The value after ``--tag`` in a build argv."""
    return argv[argv.index("--tag") + 1]


def test_endpoint_override_env_refused(copy_case: Case) -> None:
    """``DOCKER_HOST`` set → R's refusal, no build."""
    result = copy_case.run(env={"DOCKER_HOST": "tcp://10.0.0.1:2375"})
    assert not result.ok
    assert result.reason == (
        "no local confirmation: endpoint set by DOCKER_HOST not confirmed local"
    )
    assert copy_case.builds() == []


def test_container_host_refused(copy_case: Case) -> None:
    """``--container-host`` → R's refusal, no build."""
    rt = ContainerRuntime(tool="podman", host="ssh://u@h", host_source="cli")
    result = copy_case.run(rt=rt)
    assert result.reason == (
        "no local confirmation: endpoint set by --container-host not confirmed local"
    )
    assert copy_case.builds() == []


def test_docker_backend_refused(copy_case: Case) -> None:
    """A ``--container-tool docker`` run is refused before any container call."""
    result = copy_case.run(rt=ContainerRuntime(tool="docker"))
    assert result.reason == "no local confirmation: local backend differs from R's"
    assert copy_case.fake.calls == []


def test_build_uses_fix_tag_never_ci(copy_case: Case) -> None:
    """The build argv carries ``localhost/deployer-fix-<fix_id>`` only."""
    copy_case.set_build(0)
    result = copy_case.run()
    [argv] = copy_case.builds()
    assert _tag(argv) == fix_tag(FIX_ID) == f"localhost/deployer-fix-{FIX_ID}"
    assert CI_TAG not in argv
    assert not any("deployer-repro" in token for token in argv)
    assert argv[argv.index("--file") + 1].endswith("fix/context/Dockerfile")
    assert result.proof.build["tag"] == fix_tag(FIX_ID)
    assert result.proof.build["argv"][-1] == "context"


def test_two_attempts_same_seq_get_different_tags(copy_case: Case) -> None:
    """Fix dirs ``attempt-1/fixes/001`` and ``attempt-2/fixes/001`` share the
    run and the sequence, yet their builds are tagged apart."""
    copy_case.set_build(0)
    base = copy_case.fix_dir.parent / "runs"
    first = new_fix_dir(base / "attempt-1")
    second = new_fix_dir(base / "attempt-2")
    assert first.seq == second.seq == 1
    copy_case.run(fix_dir=first.path, fix_id=first.fix_id)
    copy_case.run(fix_dir=second.path, fix_id=second.fix_id)
    tags = [_tag(argv) for argv in copy_case.builds()]
    assert tags == [fix_tag(first.fix_id), fix_tag(second.fix_id)]
    assert tags[0] != tags[1]


@pytest.mark.parametrize(("rmi_code", "state"), [(0, "removed"), (1, "failed")])
def test_rmi_after_successful_build(copy_case: Case, rmi_code: int, state: str) -> None:
    """``rmi -f <tag>`` follows a successful build; its result is recorded."""
    copy_case.set_build(0)
    copy_case.fake.responses[("rmi",)] = proc(rmi_code)
    result = copy_case.run()
    assert ["rmi", "-f", fix_tag(FIX_ID)] in copy_case.fake.calls
    assert result.proof.build["image_cleanup"] == state
    assert result.proof.build["build_containers"] == "removed_by_builder"


def test_failed_build_not_cleaned(copy_case: Case) -> None:
    """No image to remove after a failed build: ``not_attempted``, no rmi."""
    copy_case.set_build(1)
    result = copy_case.run()
    assert result.proof.build["image_cleanup"] == "not_attempted"
    assert not any(call[:1] == ["rmi"] for call in copy_case.fake.calls)


def test_default_rows_confirm(copy_case: Case) -> None:
    """The local rows are recording-backed: a passing build confirms without
    the seam."""
    copy_case.set_build(0, stdout=_copy_stdout())
    result = copy_case.run()
    assert result.ok, result.reason
    assert result.proof.evidence[0]["evidence"] == "passed"


@pytest.mark.parametrize(
    ("done", "ok"),
    [
        (f"COMMIT {fix_tag(FIX_ID)}", True),
        (f"Successfully tagged {fix_tag(FIX_ID)}:latest", True),
        ("Successfully tagged localhost/deployer-fix-other:latest", False),
    ],
    ids=["commit", "tagged", "foreign"],
)
def test_completion_bound_to_fix_tag(copy_case: Case, done: str, ok: bool) -> None:
    """A completion after the last step binds only to the fix build's tag."""
    copy_case.set_build(0, stdout=f"STEP 12/12: {COPY_TEXT}\n{done}\n")
    result = copy_case.run()
    assert result.ok is ok
    if not ok:
        assert result.reason == "no local confirmation: completion of another tag"


def test_seam_passes_and_records_evidence(copy_case: Case) -> None:
    """Seam on, a synthetic passing stdout → ok; evidence names the files."""
    stdout = _copy_stdout()
    copy_case.set_build(0, stdout=stdout, stderr="warn\n")
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert result.ok and result.reason is None
    assert result.proof.later_failure is None
    [evidence] = result.proof.evidence
    assert evidence["evidence"] == "passed"
    assert evidence["file"] == "build.stdout"
    assert evidence["lines"] == [2, 3]
    assert (copy_case.fix_dir / "build.stdout").read_text() == stdout
    assert (copy_case.fix_dir / "build.stderr").read_text() == "warn\n"
    assert result.proof.build["stdout"] == "build.stdout"
    assert result.proof.build["stderr"] == "build.stderr"
    expected = hashlib.sha256(copy_case.corrected).hexdigest()
    assert result.proof.dockerfile_sha256 == expected
    assert result.proof.backend == "podman"
    assert result.proof.versions["client_version"] == "5.7.0"
    written = (copy_case.fix_dir / "context" / "Dockerfile").read_bytes()
    assert written == copy_case.corrected


def test_later_failure_recorded_after_passed(copy_case: Case) -> None:
    """The corrected step passed, a later RUN failed → ok, failure recorded."""
    copy_case.set_build(
        1,
        stdout=_copy_stdout("STEP 9/12: RUN useradd --create-home appuser"),
        stderr='Error: building at STEP "RUN uv sync --frozen": exit status 1\n',
    )
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert result.ok
    assert result.proof.later_failure == {
        "exit_code": 1,
        "what": "another_instruction",
        "image_pull": None,
        "claim": "the diagnosed source error is removed locally",
    }


def test_failed_step_is_not_a_later_failure(copy_case: Case) -> None:
    """An error bound to the corrected step: not confirmed, nothing later."""
    copy_case.set_build(
        1,
        stdout=f"STEP 7/12: {COPY_TEXT}\n",
        stderr=f'Error: building at STEP "{COPY_TEXT}": no such file\n',
    )
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert not result.ok
    assert result.reason == "no local confirmation: stderr: step-bound error"
    assert result.proof.later_failure is None
    assert result.proof.evidence[0]["file"] == "build.stderr"


def test_timeout_never_confirms(copy_case: Case) -> None:
    """A timeout does not confirm, even with passing text captured."""
    copy_case.fake.responses[("build",)] = subprocess.TimeoutExpired(
        ["podman"], 60, output=_copy_stdout()
    )
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert not result.ok
    assert result.reason == "no local confirmation: the build timed out"
    assert result.proof.build["launch_error"] == "timeout"
    assert result.proof.build["build_containers"] == "not_checked"
    assert result.proof.evidence == []


def test_launch_error_never_confirms(copy_case: Case) -> None:
    """A build that could not launch does not confirm."""
    copy_case.fake.responses[("build",)] = FileNotFoundError("podman")
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert result.reason == (
        "no local confirmation: the build did not run: executable not found"
    )


def test_regression_refused_before_build(copy_case: Case) -> None:
    """A corrected Dockerfile that also breaks another source → no build."""
    broken = copy_case.corrected.replace(b"COPY src/ci_build", b"COPY src/missing_pkg")
    result = copy_case.run(corrected=broken)
    assert not result.ok
    assert result.reason is not None
    assert result.reason.startswith("no local confirmation: regression: ")
    assert "'src/ci_build'" in result.reason
    assert copy_case.builds() == []
    assert result.proof.records_before and result.proof.records_after


def test_defect_check_failure_refused_before_build(copy_case: Case) -> None:
    """The original bytes as "corrected": the defect check fails, no build."""
    original = (copy_case.r.source / "Dockerfile").read_bytes()
    result = copy_case.run(corrected=original)
    assert result.reason is not None
    assert result.reason.startswith("no local confirmation: defect check: ")
    assert copy_case.builds() == []


def test_source_untouched(copy_case: Case) -> None:
    """R's ``source/`` is byte- and mode-identical before and after."""
    before = _tree_hash(copy_case.r.source)
    copy_case.set_build(0, stdout=_copy_stdout())
    with enable_for_test("copy-passed/podman"):
        assert copy_case.run().ok
    assert _tree_hash(copy_case.r.source) == before


def test_never_raises(copy_case: Case) -> None:
    """An existing ``context/`` makes the copy fail: a not-ok result."""
    (copy_case.fix_dir / "context").mkdir()
    result = copy_case.run()
    assert not result.ok
    assert result.reason is not None
    assert result.reason.startswith("no local confirmation: FileExistsError")
    assert copy_case.builds() == []


def test_from_fix_passes_with_seam(from_case: Case) -> None:
    """run-5: FROM parsed (file-wide), a later failure is unattributed."""
    from_case.set_build(
        1,
        stdout=f"STEP 1/10: {FROM_TEXT}\nSTEP 2/10: COPY --from=x /uv /bin/\n",
        stderr="Error: some later failure\n",
    )
    with enable_for_test("from-parsed/podman"):
        result = from_case.run()
    assert result.ok, result.reason
    assert result.proof.later_failure is not None
    assert result.proof.later_failure["what"] == "unattributed"
    assert result.proof.evidence[0]["corrected_text"] == FROM_TEXT


def test_from_fix_default_rows_confirm(from_case: Case) -> None:
    """run-5 with no seam: the corrected FROM's own step line confirms."""
    from_case.set_build(0, stdout=f"STEP 1/10: {FROM_TEXT}\n")
    result = from_case.run()
    assert result.ok, result.reason
    assert result.proof.evidence[0]["lines"] == [1]


def test_from_fix_other_step_is_not_evidence(from_case: Case) -> None:
    """run-5: a build-stage step that is not the corrected FROM's proves
    nothing (the old file-wide rule, refuted by l9)."""
    from_case.set_build(0, stdout="STEP 1/10: FROM python:3.12-slim\n")
    result = from_case.run()
    assert result.reason == "no local confirmation: corrected step line absent"


def test_build_config_from_stored_input() -> None:
    """``Input.build`` rebuilds R's config; the stored tag is dropped."""
    stored = {
        "dockerfile": "docker/Dockerfile",
        "build_args": [["A", "1"]],
        "platform": "linux/amd64",
        "tag": CI_TAG,
    }
    assert build_config(stored) == BuildConfig(
        "docker/Dockerfile", (("A", "1"),), "linux/amd64", None
    )
    assert isinstance(build_config({"dockerfile": ""}), str)
    assert isinstance(build_config({"dockerfile": "D", "build_args": [["A"]]}), str)


def _link_dockerfile(case: Case, outside: Path) -> None:
    """Replace R's ``source/Dockerfile`` by an absolute symlink to
    ``outside``, which holds the original bytes."""
    source = case.r.source
    _make_writable(source)
    outside.write_bytes((source / "Dockerfile").read_bytes())
    (source / "Dockerfile").unlink()
    (source / "Dockerfile").symlink_to(outside)


def test_symlinked_dockerfile_refused_outside_untouched(
    copy_case: Case, tmp_path: Path
) -> None:
    """An absolute-symlink Dockerfile: not ok, no build, the outside file
    keeps its bytes, and "before" read the original bytes."""
    outside = tmp_path / "outside_target"
    _link_dockerfile(copy_case, outside)
    original = outside.read_bytes()
    copy_case.set_build(0, stdout=_copy_stdout())
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert not result.ok
    assert result.reason == (
        "no local confirmation: dockerfile 'Dockerfile' is not a regular file"
    )
    assert outside.read_bytes() == original
    assert copy_case.builds() == []
    copy_run = result.proof.records_before[0]
    subjects = [record["subject"] for record in copy_run["records"]]
    assert ABSENT in subjects and NEW not in subjects


def test_symlinked_ancestor_refused(copy_case: Case, tmp_path: Path) -> None:
    """``docker/Dockerfile`` whose ``docker/`` is a symlink to an outside
    directory: refused, the outside file untouched."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    target = outside_dir / "Dockerfile"
    target.write_bytes((copy_case.r.source / "Dockerfile").read_bytes())
    original = target.read_bytes()
    _make_writable(copy_case.r.source)
    (copy_case.r.source / "docker").symlink_to(outside_dir)
    copy_case.set_build(0, stdout=_copy_stdout())
    build = BuildConfig("docker/Dockerfile", (), None, None)
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run(build=build)
    assert result.reason == (
        "no local confirmation: dockerfile ancestor 'docker' is not a real directory"
    )
    assert target.read_bytes() == original
    assert copy_case.builds() == []


def test_escaping_dockerfile_path_refused(copy_case: Case) -> None:
    """``../../x`` is refused by ``build_config`` and by the proof itself."""
    assert build_config({"dockerfile": "../../x"}) == (
        "dockerfile path '../../x' leaves the context"
    )
    assert build_config({"dockerfile": "/etc/x"}) == (
        "dockerfile path '/etc/x' leaves the context"
    )
    result = copy_case.run(build=BuildConfig("../../x", (), None, None))
    assert result.reason == (
        "no local confirmation: dockerfile path '../../x' leaves the context"
    )
    assert not (copy_case.fix_dir / "context").exists()


def test_r_backend_docker_refused(copy_case: Case) -> None:
    """R recorded ``docker``, the local backend is Podman: refused."""
    section = copy_case.r.section
    assert section.environment is not None
    environment = section.environment.model_copy(update={"backend": "docker"})
    copy_case.r = dataclasses.replace(
        copy_case.r, section=section.model_copy(update={"environment": environment})
    )
    copy_case.set_build(0, stdout=_copy_stdout())
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert result.reason == "no local confirmation: local backend differs from R's"
    assert copy_case.fake.calls == []


def test_rmi_failure_does_not_block_ok(copy_case: Case) -> None:
    """Seam on, ``rmi`` raising: still ok, cleanup recorded as failed."""
    copy_case.set_build(0, stdout=_copy_stdout())
    copy_case.fake.responses[("rmi",)] = OSError("boom")
    with enable_for_test("copy-passed/podman"):
        result = copy_case.run()
    assert result.ok
    assert result.proof.build["image_cleanup"] == "failed"


def test_build_config_refuses_other_context() -> None:
    """A stored context other than ``.`` is refused, never dropped."""
    assert build_config({"dockerfile": "Dockerfile", "context": "app"}) == (
        "stored build context 'app' is not '.'"
    )
    assert isinstance(
        build_config({"dockerfile": "Dockerfile", "context": "."}), BuildConfig
    )


def test_build_config_round_trip() -> None:
    """``BuildConfig`` dumped as ``Input.build`` stores it, then rebuilt."""
    config = BuildConfig(
        "docker/Dockerfile", (("A", "1"), ("B", "x=y")), "linux/arm64", CI_TAG
    )
    stored = TypeAdapter(BuildConfig).dump_python(config, mode="json")
    assert stored["build_args"] == [["A", "1"], ["B", "x=y"]]
    assert build_config(stored) == dataclasses.replace(config, tag=None)
