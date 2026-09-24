"""§4.3-4.5: own argv, full output, honest cleanup, digests as recorded."""

import json
import subprocess
from pathlib import Path

import pytest

from deployer.models import ContainerRuntime
from deployer.reproduce.build import (
    build_containers_state,
    cleanup_image,
    local_repo_digests,
    repro_tag,
    run_build,
)
from deployer.reproduce.buildline import BuildConfig
from tests.reproduce.conftest import proc

PODMAN = ContainerRuntime(tool="podman")
DOCKER = ContainerRuntime(tool="docker")
CONFIG = BuildConfig("Dockerfile", (("A", "1"),), "linux/amd64", "ci-tag:1")


def test_argv_passes_the_file_by_path_own_tag_force_rm_no_memory(fake_containers):
    fake_containers.responses[("build",)] = proc(1, stdout="out", stderr="err")
    ctx = Path("/w/tries/001/context")
    run = run_build(PODMAN, ctx, CONFIG, repro_tag(35680991093, "001"), 900)
    assert run.argv == [
        "podman",
        "build",
        "--file",
        "/w/tries/001/context/Dockerfile",
        "--build-arg",
        "A=1",
        "--platform",
        "linux/amd64",
        "--tag",
        "localhost/deployer-repro-35680991093-001",
        "--force-rm",
        "/w/tries/001/context",
    ]
    assert "ci-tag:1" not in run.argv and "--memory" not in run.argv
    assert (run.exit_code, run.launch_error, run.stdout, run.stderr) == (
        1,
        None,
        "out",
        "err",
    )


def test_docker_gets_no_force_rm(fake_containers):
    fake_containers.responses[("build",)] = proc(0)
    run = run_build(DOCKER, Path("/c"), CONFIG, "t", 900)
    assert "--force-rm" not in run.argv


def test_timeout_keeps_partial_output(fake_containers):
    exc = subprocess.TimeoutExpired(["podman"], 5, output=b"partial", stderr=None)
    fake_containers.responses[("build",)] = exc
    run = run_build(PODMAN, Path("/c"), CONFIG, "t", 5)
    assert (run.exit_code, run.launch_error, run.stdout) == (None, "timeout", "partial")


def test_missing_executable(fake_containers):
    fake_containers.responses[("build",)] = FileNotFoundError("podman")
    run = run_build(PODMAN, Path("/c"), CONFIG, "t", 5)
    assert (run.exit_code, run.launch_error) == (None, "executable not found")


def test_cleanup_reads_the_return_code(fake_containers):
    assert cleanup_image(PODMAN, "t", built=False) == "not_attempted"
    fake_containers.responses[("rmi",)] = proc(0)
    assert cleanup_image(PODMAN, "t", built=True) == "removed"
    fake_containers.responses[("rmi",)] = proc(1)
    assert cleanup_image(PODMAN, "t", built=True) == "failed"
    fake_containers.responses[("rmi",)] = subprocess.TimeoutExpired(["x"], 1)
    assert cleanup_image(PODMAN, "t", built=True) == "failed"


def test_build_containers_state():
    assert build_containers_state(PODMAN, finished=True) == "removed_by_builder"
    assert build_containers_state(PODMAN, finished=False) == "not_checked"
    assert build_containers_state(DOCKER, finished=False) == "not_applicable"


def test_local_repo_digests(fake_containers):
    fake_containers.responses[
        ("image", "inspect", "--format", "{{json .RepoDigests}}", "a")
    ] = proc(stdout=json.dumps(["docker.io/library/a@sha256:" + "1" * 64]))
    fake_containers.responses[
        ("image", "inspect", "--format", "{{json .RepoDigests}}", "b")
    ] = proc(1)
    assert local_repo_digests(PODMAN, ["a", "b"]) == {
        "a": ["sha256:" + "1" * 64],
        "b": [],
    }


@pytest.mark.parametrize(
    "answer",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        proc(stdout="null"),
        proc(stdout="42"),
        proc(stdout='{"a": 1}'),
    ],
)
def test_local_repo_digests_never_raises_and_is_unknown(fake_containers, answer):
    """Undecodable or non-list output is unknown, never an error (known minor)."""
    fake_containers.responses[("image", "inspect")] = answer
    assert local_repo_digests(PODMAN, ["a"]) == {"a": []}
