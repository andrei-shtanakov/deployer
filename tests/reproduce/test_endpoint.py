"""§4.2: only a confirmed-local endpoint receives the unfiltered context."""

import json

import pytest

from deployer.models import ContainerRuntime
from deployer.reproduce.endpoint import Endpoint, confirm_local, is_local
from deployer.reproduce.shape import Refusal
from tests.reproduce.conftest import proc

PODMAN = ContainerRuntime(tool="podman")
DOCKER = ContainerRuntime(tool="docker")
MACHINE = "ssh://core@127.0.0.1:56907/run/user/501/podman/podman.sock"


@pytest.mark.parametrize(
    "var",
    [
        "DEPLOYER_CONTAINER_HOST",
        "DOCKER_HOST",
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
        "DOCKER_CONTEXT",
    ],
)
def test_any_override_refuses_before_detection(var, fake_containers):
    assert confirm_local(PODMAN, {var: "x"}) == Refusal(
        f"endpoint set by {var} not confirmed local"
    )
    assert fake_containers.calls == []


def test_podman_machine_on_loopback_is_local(fake_containers):
    fake_containers.responses[("system", "connection", "list")] = proc(
        stdout=json.dumps(
            [
                {"Name": "root", "URI": "ssh://root@127.0.0.1:1/x", "Default": False},
                {"Name": "default", "URI": MACHINE, "Default": True},
            ]
        )
    )
    assert confirm_local(PODMAN, {}) == Endpoint(MACHINE, "podman_default_connection")


def test_podman_without_connections_uses_the_local_socket(fake_containers):
    fake_containers.responses[("system", "connection", "list")] = proc(stdout="[]")
    assert confirm_local(PODMAN, {}) == Endpoint("unix://", "podman_local_socket")


def test_remote_default_connection_refuses(fake_containers):
    remote = "ssh://core@10.0.0.5/run/podman/podman.sock"
    fake_containers.responses[("system", "connection", "list")] = proc(
        stdout=json.dumps([{"Name": "d", "URI": remote, "Default": True}])
    )
    assert confirm_local(PODMAN, {}) == Refusal(
        f"endpoint {remote} not confirmed local"
    )


def test_docker_active_context(fake_containers):
    fake_containers.responses[("context", "inspect")] = proc(
        stdout="unix:///var/run/docker.sock\n"
    )
    assert confirm_local(DOCKER, {}) == Endpoint(
        "unix:///var/run/docker.sock", "docker_active_context"
    )


def test_detection_failure_refuses(fake_containers):
    fake_containers.responses[("context", "inspect")] = proc(1, stderr="boom")
    assert confirm_local(DOCKER, {}) == Refusal("endpoint detection failed: boom")


def test_runtime_with_a_host_refuses(fake_containers):
    remote = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    assert confirm_local(remote, {}) == Refusal(
        "endpoint set by --container-host not confirmed local"
    )


@pytest.mark.parametrize(
    ("uri", "local"),
    [
        ("unix:///run/x.sock", True),
        ("tcp://localhost:2375", True),
        ("ssh://u@[::1]:22/x", True),
        ("tcp://10.0.0.5:2375", False),
        ("npipe:////./pipe/docker", False),
    ],
)
def test_is_local(uri, local):
    assert is_local(uri) is local
