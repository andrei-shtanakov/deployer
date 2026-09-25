"""Orchestration: order of refusals, storage per try, the manifest."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from deployer.forge import GhError
from deployer.models import ContainerRuntime
from deployer.reproduce.restore import git_blob_sha, make_tarball
from deployer.reproduce.run import TryDirError, reproduce_run
from tests.reproduce.conftest import proc
from tests.reproduce.test_shape import SHA, WORKFLOW, _run

PODMAN = ContainerRuntime(tool="podman")
DOCKER = ContainerRuntime(tool="docker")
DOCKERFILE = "FROM python:3.12-slim\nCOPY docs/setup.md ./setup.md\n"
CONNECTIONS = json.dumps([{"URI": "ssh://core@127.0.0.1:1/x", "Default": True}])


class TreeGh:
    """Serves a directory as GitHub would: tarball bytes and a tree listing."""

    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.fail: GhError | None = None

    def api(self, argv, *, timeout):
        entries = []
        for p in sorted(self.tree.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.tree).as_posix()
                entries.append(
                    {
                        "path": rel,
                        "mode": "100644",
                        "type": "blob",
                        "sha": git_blob_sha(p.read_bytes()),
                    }
                )
        return json.dumps({"sha": SHA, "tree": entries, "truncated": False})

    def api_bytes(self, argv, *, timeout):
        if self.fail is not None:
            raise self.fail
        return make_tarball(self.tree, "example-project-d6e330f")


@pytest.fixture()
def tree(tmp_path) -> Path:
    t = tmp_path / "tree"
    (t / ".github/workflows").mkdir(parents=True)
    (t / ".github/workflows/diagnosis-polygon.yml").write_text(WORKFLOW)
    (t / "Dockerfile").write_text(DOCKERFILE)
    return t


def _go(tmp_path, tree, fake_containers, **kw):
    fake_containers.responses[("system", "connection", "list")] = proc(
        stdout=CONNECTIONS
    )
    fake_containers.responses[("version",)] = proc(
        stdout=json.dumps({"Client": {"Version": "5.7.0"}})
    )
    fake_containers.responses[("image", "inspect")] = proc(1)
    args = dict(
        gh=TreeGh(tree),
        rt=PODMAN,
        runtime_error=None,
        env={},
        root=tmp_path / "work",
        build_timeout=60,
    )
    args.update(kw)
    return reproduce_run(_run(), **args)


def test_attempted_run_writes_source_try_and_manifest(tmp_path, tree, fake_containers):
    fake_containers.responses[("build",)] = proc(
        125,
        stdout=(
            "STEP 1/2: FROM python:3.12-slim\nSTEP 2/2: COPY docs/setup.md ./setup.md\n"
        ),
        stderr=(
            'Error: building at STEP "COPY docs/setup.md ./setup.md": no such file\n'
        ),
    )
    section = _go(tmp_path, tree, fake_containers)
    assert section.status == "attempted"
    assert section.restoration is not None and section.restoration.state == "exact"
    assert section.try_dir == (".deployer-runs/1/reproduction/attempt-1/tries/001")
    base = tmp_path / "work" / section.try_dir
    assert (base / "context" / "Dockerfile").is_file()
    assert (base / "build.stderr").read_text().startswith("Error: building")
    assert json.loads((base / "manifest.json").read_text())["status"] == "attempted"
    assert (
        json.loads((base.parent.parent / "source.json").read_text())["head_sha"] == SHA
    )
    assert [c.finding for c in section.checks if c.status == "failed"] == [
        "source docs/setup.md absent from the context"
    ]
    assert section.build is not None and section.build.failed_instruction is not None
    assert section.build.failed_instruction.lines == (2, 2)
    assert section.comparison is not None
    assert section.comparison.state in ("inconclusive", "reproduced_with_differences")
    # Podman has no `--check`: nothing was launched, so neither file exists.
    assert not (base / "check.stdout").exists()
    assert not (base / "check.stderr").exists()
    # Review Focus 3: recorded argv is try-dir-relative, never the host's
    # absolute layout (spec §6's example: "context/Dockerfile").
    assert "context/Dockerfile" in section.build.argv
    assert not any(Path(token).is_absolute() for token in section.build.argv)


def test_source_is_read_only_and_context_is_writable(tmp_path, tree, fake_containers):
    """Spec §1.5: ``source/`` is read-only after restoration; ``context/`` —
    the build receives ``source/`` "as is" — must still be writable."""
    fake_containers.responses[("build",)] = proc(1)
    section = _go(tmp_path, tree, fake_containers)
    base = tmp_path / "work" / section.try_dir
    source_dir = base.parent.parent / "source"
    source_file = source_dir / "Dockerfile"
    context_file = base / "context" / "Dockerfile"
    assert source_file.is_file() and context_file.is_file()
    assert not (source_file.stat().st_mode & 0o222)
    assert not (source_dir.stat().st_mode & 0o222)
    assert context_file.stat().st_mode & 0o200


def test_docker_check_writes_raw_output_when_launched(tmp_path, tree, fake_containers):
    """Review Focus 1: check.stdout/check.stderr hold the launched process's
    raw streams, and only exist when the check actually ran."""
    fake_containers.responses[("context", "inspect")] = proc(
        stdout="unix:///var/run/docker.sock"
    )
    fake_containers.responses[("version",)] = proc(
        stdout=json.dumps({"Client": {"Version": "27.0.0"}})
    )
    fake_containers.responses[("image", "inspect")] = proc(1)
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.15.1 abc\n"
    )
    fake_containers.responses[("build", "--check")] = proc(
        0, stdout="check out text", stderr="check err text"
    )
    fake_containers.responses[("build",)] = proc(0, stdout="built ok")
    section = reproduce_run(
        _run(),
        gh=TreeGh(tree),
        rt=DOCKER,
        runtime_error=None,
        env={},
        root=tmp_path / "work",
        build_timeout=60,
    )
    assert section.status == "attempted"
    assert section.try_dir is not None
    base = tmp_path / "work" / section.try_dir
    assert (base / "check.stdout").read_text() == "check out text"
    assert (base / "check.stderr").read_text() == "check err text"


def test_second_try_is_002_and_reuses_source(tmp_path, tree, fake_containers):
    # Review Focus 5
    fake_containers.responses[("build",)] = proc(1)
    first = _go(tmp_path, tree, fake_containers)
    gh = TreeGh(tree)
    gh.fail = GhError("must not be called", 500)
    second = _go(tmp_path, tree, fake_containers, gh=gh)
    assert first.try_dir is not None and first.try_dir.endswith("/001")
    assert second.try_dir is not None and second.try_dir.endswith("/002")
    assert (tmp_path / "work" / first.try_dir / "manifest.json").is_file()


def test_source_json_for_another_sha_is_a_try_dir_error(
    tmp_path, tree, fake_containers
):
    fake_containers.responses[("build",)] = proc(1)
    section = _go(tmp_path, tree, fake_containers)
    source = tmp_path / "work" / section.try_dir / ".." / ".." / "source.json"
    data = json.loads(source.read_text())
    source.write_text(json.dumps({**data, "head_sha": "0" * 40}))
    with pytest.raises(TryDirError):
        _go(tmp_path, tree, fake_containers)


def test_corrupt_source_json_is_a_try_dir_error(tmp_path, tree, fake_containers):
    """Review Focus 2: a truncated/corrupt source.json must not crash with a
    traceback — it is exit 2 (TryDirError) like the wrong-sha case."""
    fake_containers.responses[("build",)] = proc(1)
    section = _go(tmp_path, tree, fake_containers)
    source = tmp_path / "work" / section.try_dir / ".." / ".." / "source.json"
    source.write_text("{not valid json")
    with pytest.raises(TryDirError):
        _go(tmp_path, tree, fake_containers)


def test_copytree_failure_is_a_try_dir_error(
    tmp_path, tree, fake_containers, monkeypatch
):
    """Review Focus 2: an OSError preparing the try context must not crash
    with a traceback."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", boom)
    fake_containers.responses[("build",)] = proc(1)
    with pytest.raises(TryDirError, match="cannot prepare the try context"):
        _go(tmp_path, tree, fake_containers)


def test_write_failure_after_try_dir_exists_is_a_try_dir_error(
    tmp_path, tree, fake_containers, monkeypatch
):
    """Review Focus: a disk-full/permission failure writing build.stdout (the
    try directory already exists by then) must not crash with a traceback —
    it is exit 2 (TryDirError), like the other §6 try-directory failures."""
    fake_containers.responses[("build",)] = proc(1)
    original_write_text = Path.write_text

    def boom(self, *args, **kwargs):
        if self.name == "build.stdout":
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(TryDirError, match="cannot write.*build.stdout"):
        _go(tmp_path, tree, fake_containers)


def _chmod_failing_under(monkeypatch, part: str) -> None:
    """Make ``os.chmod`` raise for any path containing ``part``."""
    import os

    original_chmod = os.chmod

    def boom(path, mode, *args, **kwargs):
        if part in Path(path).parts:
            raise OSError("operation not permitted")
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", boom)


def test_chmod_failure_making_source_read_only_is_a_try_dir_error(
    tmp_path, tree, fake_containers, monkeypatch
):
    """A permission change that fails on the restored source/ exits 2 (§6),
    not as an uncaught traceback."""
    _chmod_failing_under(monkeypatch, "source")
    fake_containers.responses[("build",)] = proc(1)
    with pytest.raises(TryDirError, match="cannot make source/ read-only"):
        _go(tmp_path, tree, fake_containers)


def test_chmod_failure_making_context_writable_is_a_try_dir_error(
    tmp_path, tree, fake_containers, monkeypatch
):
    """A permission change that fails on the try's context/ exits 2 (§6).

    ``copytree`` itself chmods while copying (already a TryDirError), so the
    failure is injected into the writable pass that runs after the copy.
    """
    from deployer.reproduce import run as run_mod

    def boom(root: Path) -> None:
        raise OSError("operation not permitted")

    monkeypatch.setattr(run_mod, "_make_writable", boom)
    fake_containers.responses[("build",)] = proc(1)
    with pytest.raises(TryDirError, match="cannot make context/ writable"):
        _go(tmp_path, tree, fake_containers)


def test_precheck_refusal_creates_nothing(tmp_path, tree, fake_containers):
    section = reproduce_run(
        replace(_run(), event="pull_request"),
        gh=TreeGh(tree),
        rt=PODMAN,
        runtime_error=None,
        env={},
        root=tmp_path / "work",
        build_timeout=60,
    )
    assert (section.status, section.refusal) == (
        "refused",
        "event pull_request not supported",
    )
    assert not (tmp_path / "work").exists()


def test_archive_http_error_is_unavailable(tmp_path, tree, fake_containers):
    gh = TreeGh(tree)
    gh.fail = GhError("gh: Not Found (HTTP 404)", 404)
    section = _go(tmp_path, tree, fake_containers, gh=gh)
    assert section.status == "unavailable"
    assert section.refusal is not None
    assert section.refusal.startswith("archive fetch failed")


def test_no_runtime_is_refused_with_checks(tmp_path, tree, fake_containers):
    section = _go(
        tmp_path,
        tree,
        fake_containers,
        rt=None,
        runtime_error="no container tool found",
    )
    assert (section.status, section.refusal) == (
        "refused",
        "no container runtime: no container tool found",
    )
    assert any(c.status == "failed" for c in section.checks)


def test_endpoint_refusal_keeps_checks(tmp_path, tree, fake_containers):
    section = _go(tmp_path, tree, fake_containers, env={"DOCKER_HOST": "tcp://x:1"})
    assert section.refusal == "endpoint set by DOCKER_HOST not confirmed local"
    assert section.build is None and section.checks


@pytest.mark.parametrize(
    "content",
    [
        "null",
        "[]",
        '"text"',
        json.dumps(
            {
                "head_sha": SHA,
                "listing": {"sha": "x", "entries": [1], "truncated": False},
            }
        ),
        json.dumps({"head_sha": SHA, "listing": None}),
    ],
)
def test_a_well_formed_but_wrong_source_json_is_a_try_dir_error(
    tmp_path, tree, fake_containers, content
):
    """Valid JSON of the wrong shape is corruption too: exit 2 (review of #79)."""
    fake_containers.responses[("build",)] = proc(1)
    section = _go(tmp_path, tree, fake_containers)
    source = tmp_path / "work" / section.try_dir / ".." / ".." / "source.json"
    source.write_text(content)
    with pytest.raises(TryDirError, match="cannot read"):
        _go(tmp_path, tree, fake_containers)


def test_write_text_is_utf8_with_untranslated_newlines(tmp_path: Path) -> None:
    """T11 fix round 1: the shared writer does not depend on the locale (a
    Latin-1 locale cannot carry ``€``) and keeps ``\\r\\n`` as written. Run in
    a child under a Latin-1 locale: the file encoding is fixed at startup."""
    import os
    import subprocess
    import sys

    path = tmp_path / "ci.log"
    code = (
        "import locale, sys\n"
        "from pathlib import Path\n"
        "from deployer.reproduce.run import _write_text\n"
        "if locale.getencoding().upper().replace('-', '') != 'ISO88591':\n"
        "    sys.exit(77)\n"
        "_write_text(Path(sys.argv[1]), 'ł € ok\\r\\nnext')\n"
    )
    env = {**os.environ, "LC_ALL": "en_US.ISO8859-1", "PYTHONUTF8": "0"}
    done = subprocess.run(
        [sys.executable, "-c", code, str(path)], env=env, capture_output=True
    )
    if done.returncode == 77:
        pytest.skip("no Latin-1 locale on this host")
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    assert path.read_bytes() == "ł € ok\r\nnext".encode()


def test_unencodable_text_is_a_try_dir_error(tmp_path: Path) -> None:
    """T11 fix round 1: a lone surrogate cannot be encoded even as UTF-8; it
    is a try-directory write failure (exit 2), not a traceback."""
    from deployer.reproduce.run import _write_text

    with pytest.raises(TryDirError, match="cannot write"):
        _write_text(tmp_path / "ci.log", "bad \udc80 byte")
