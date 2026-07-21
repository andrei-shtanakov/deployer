"""Runtime resolution matrix: flags -> deployer env -> native env -> local."""

import json

import pytest

from deployer.models import ContainerRuntime
from deployer.runtime import (
    RuntimeConfigError,
    container_run,
    probe_runtime_versions,
    resolve_runtime,
    runtime_env,
)


@pytest.fixture()
def all_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.shutil.which", lambda tool: f"/usr/bin/{tool}"
    )


@pytest.fixture()
def no_tools(monkeypatch) -> None:
    monkeypatch.setattr("deployer.runtime.shutil.which", lambda tool: None)


def test_explicit_tool_and_cli_host(all_tools) -> None:
    rt = resolve_runtime("docker", "ssh://u@h", env={})
    assert rt is not None
    assert rt == ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    assert rt.remote


def test_local_default_prefers_podman(all_tools) -> None:
    rt = resolve_runtime(env={})
    assert rt is not None
    assert rt == ContainerRuntime(tool="podman", host=None, host_source="local")
    assert not rt.remote


def test_docker_detected_when_no_podman(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.shutil.which",
        lambda tool: "/usr/bin/docker" if tool == "docker" else None,
    )
    rt = resolve_runtime(env={})
    assert rt is not None and rt.tool == "docker"


def test_no_tools_means_static_only(no_tools) -> None:
    assert resolve_runtime(env={}) is None


def test_deployer_env_tool_and_host(all_tools) -> None:
    env = {
        "DEPLOYER_CONTAINER_TOOL": "docker",
        "DEPLOYER_CONTAINER_HOST": "ssh://u@h",
    }
    rt = resolve_runtime(env=env)
    assert rt == ContainerRuntime(
        tool="docker", host="ssh://u@h", host_source="deployer_env"
    )


def test_cli_flags_beat_deployer_env(all_tools) -> None:
    env = {
        "DEPLOYER_CONTAINER_TOOL": "podman",
        "DEPLOYER_CONTAINER_HOST": "ssh://env@h",
    }
    rt = resolve_runtime("docker", "ssh://cli@h", env=env)
    assert rt is not None
    assert (rt.tool, rt.host, rt.host_source) == ("docker", "ssh://cli@h", "cli")


def test_native_env_captured_for_selected_tool(all_tools) -> None:
    rt = resolve_runtime("docker", env={"DOCKER_HOST": "tcp://old:2375"})
    assert rt is not None
    assert (rt.host, rt.host_source) == ("tcp://old:2375", "native_env")


def test_native_env_of_other_tool_ignored(all_tools) -> None:
    rt = resolve_runtime("podman", env={"DOCKER_HOST": "ssh://u@h"})
    assert rt == ContainerRuntime(tool="podman", host=None, host_source="local")


def test_explicit_tool_missing_raises(no_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime("docker", env={})


def test_env_tool_invalid_value_raises(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime(env={"DEPLOYER_CONTAINER_TOOL": "nerdctl"})


def test_cli_tool_invalid_value_raises(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime("nerdctl", env={})


def test_cli_host_must_be_ssh(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime("docker", "tcp://h:2375", env={})


def test_deployer_env_host_must_be_ssh(all_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime(env={"DEPLOYER_CONTAINER_HOST": "tcp://h:2375"})


def test_explicit_host_without_any_tool_raises(no_tools) -> None:
    with pytest.raises(RuntimeConfigError):
        resolve_runtime(host_arg="ssh://u@h", env={})


def test_runtime_round_trips_json() -> None:
    rt = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    assert ContainerRuntime.model_validate_json(rt.model_dump_json()) == rt


def test_runtime_env_overlays_docker_host_for_cli_source(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DOCKER_HOST", "tcp://stale:2375")
    rt = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    env = runtime_env(rt)
    assert env["DOCKER_HOST"] == "ssh://u@h"
    assert env["PATH"] == "/usr/bin"  # full os.environ copy, not a minimal dict


def test_runtime_env_overlays_container_host_for_podman(monkeypatch) -> None:
    rt = ContainerRuntime(tool="podman", host="ssh://u@h", host_source="deployer_env")
    assert runtime_env(rt)["CONTAINER_HOST"] == "ssh://u@h"


def test_runtime_env_untouched_for_native_and_local(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://pre@set")
    native = ContainerRuntime(
        tool="docker", host="ssh://pre@set", host_source="native_env"
    )
    assert runtime_env(native)["DOCKER_HOST"] == "ssh://pre@set"
    monkeypatch.delenv("DOCKER_HOST")
    local = ContainerRuntime(tool="docker")
    assert "DOCKER_HOST" not in runtime_env(local)


def test_container_run_prepends_tool_and_injects_env(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return "sentinel"

    monkeypatch.setattr("deployer.runtime.subprocess.run", fake_run)
    rt = ContainerRuntime(tool="docker", host="ssh://u@h", host_source="cli")
    result = container_run(rt, ["build", "-t", "x", "."], capture_output=True)
    assert result == "sentinel"
    assert seen["cmd"] == ["docker", "build", "-t", "x", "."]
    assert seen["env"]["DOCKER_HOST"] == "ssh://u@h"


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    class P:
        returncode: int
        stdout: str
        stderr: str

    p = P()
    p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
    return p


def test_probe_parses_version_json(monkeypatch) -> None:
    payload = json.dumps(
        {
            "Client": {"Version": "27.0.1"},
            "Server": {"Version": "27.0.1", "Os": "linux", "Arch": "amd64"},
        }
    )
    monkeypatch.setattr(
        "deployer.runtime.container_run",
        lambda *a, **k: _fake_proc(0, stdout=payload),
    )
    versions = probe_runtime_versions(ContainerRuntime(tool="docker"))
    assert versions.client_version == "27.0.1"
    assert versions.server_version == "27.0.1"
    assert versions.platform == "linux/amd64"
    assert versions.probe_warning is None


def test_probe_is_best_effort_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.container_run",
        lambda *a, **k: _fake_proc(1, stderr="cannot connect"),
    )
    versions = probe_runtime_versions(ContainerRuntime(tool="docker"))
    assert versions.probe_warning is not None
    assert versions.client_version is None


def test_probe_never_raises_on_garbage(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.runtime.container_run",
        lambda *a, **k: _fake_proc(0, stdout="not json"),
    )
    assert probe_runtime_versions(ContainerRuntime(tool="podman")).probe_warning
