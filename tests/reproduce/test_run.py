"""Orchestration: order of refusals, storage per try, the manifest."""

import json
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
