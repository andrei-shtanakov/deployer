"""Container runtime resolution and the single subprocess chokepoint."""

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any, Literal, cast

from deployer.models import ContainerRuntime, RuntimeVersions

NATIVE_HOST_ENV = {"docker": "DOCKER_HOST", "podman": "CONTAINER_HOST"}
_DETECTION_ORDER = ("podman", "docker")


class RuntimeConfigError(Exception):
    """Explicitly-invalid runtime configuration; the CLI maps this to exit 2."""


def _validate_ssh(host: str, origin: str) -> None:
    if not host.startswith("ssh://"):
        raise RuntimeConfigError(f"{origin} must be an ssh:// URL, got {host!r}")


def _resolve_tool(
    tool_arg: str | None, env: Mapping[str, str]
) -> Literal["docker", "podman"] | None:
    if tool_arg is not None:
        if tool_arg not in NATIVE_HOST_ENV:
            raise RuntimeConfigError(
                f"--container-tool must be 'docker' or 'podman', got {tool_arg!r}"
            )
        if shutil.which(tool_arg) is None:
            raise RuntimeConfigError(f"--container-tool {tool_arg}: not found on PATH")
        return cast(Literal["docker", "podman"], tool_arg)
    env_tool = env.get("DEPLOYER_CONTAINER_TOOL")
    if env_tool:
        if env_tool not in NATIVE_HOST_ENV:
            raise RuntimeConfigError(
                "DEPLOYER_CONTAINER_TOOL must be 'docker' or 'podman', "
                f"got {env_tool!r}"
            )
        if shutil.which(env_tool) is None:
            raise RuntimeConfigError(
                f"DEPLOYER_CONTAINER_TOOL {env_tool}: not found on PATH"
            )
        return cast(Literal["docker", "podman"], env_tool)
    for tool in _DETECTION_ORDER:
        if shutil.which(tool):
            return tool
    return None


def resolve_runtime(
    tool_arg: str | None = None,
    host_arg: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ContainerRuntime | None:
    """Resolve the container runtime; None means implicit static-only.

    Raises RuntimeConfigError for explicitly-invalid configuration
    (requested tool missing, malformed host, host without any tool).
    """
    if env is None:
        env = os.environ
    tool = _resolve_tool(tool_arg, env)
    if tool is None:
        if host_arg or env.get("DEPLOYER_CONTAINER_HOST"):
            raise RuntimeConfigError(
                "container host given but no container tool found on PATH"
            )
        return None
    if host_arg:
        _validate_ssh(host_arg, "--container-host")
        return ContainerRuntime(tool=tool, host=host_arg, host_source="cli")
    deployer_host = env.get("DEPLOYER_CONTAINER_HOST")
    if deployer_host:
        _validate_ssh(deployer_host, "DEPLOYER_CONTAINER_HOST")
        return ContainerRuntime(
            tool=tool, host=deployer_host, host_source="deployer_env"
        )
    native_host = env.get(NATIVE_HOST_ENV[tool])
    if native_host:
        return ContainerRuntime(tool=tool, host=native_host, host_source="native_env")
    return ContainerRuntime(tool=tool)


def runtime_env(runtime: ContainerRuntime) -> dict[str, str]:
    """Process env for container CLI calls. Never log the result.

    Starts from a full os.environ copy (PATH, HOME, SSH_AUTH_SOCK and
    docker/podman config vars must survive or SSH agent auth breaks) and
    overlays the tool-native host var only for deployer-chosen hosts.
    """
    env = os.environ.copy()
    if runtime.host_source in ("cli", "deployer_env") and runtime.host is not None:
        env[NATIVE_HOST_ENV[runtime.tool]] = runtime.host
    return env


def container_run(
    runtime: ContainerRuntime, args: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """The single chokepoint for every container CLI invocation."""
    return subprocess.run([runtime.tool, *args], env=runtime_env(runtime), **kwargs)


def probe_runtime_versions(runtime: ContainerRuntime) -> RuntimeVersions:
    """Best-effort `<tool> version` probe; never raises, never blocks a run."""
    try:
        proc = container_run(
            runtime,
            ["version", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return RuntimeVersions(
                probe_warning=detail[-1] if detail else "version probe failed"
            )
        data = json.loads(proc.stdout)
        client = (data.get("Client") or {}).get("Version")
        server_block = data.get("Server") or {}
        server = server_block.get("Version")
        os_name = server_block.get("Os") or ""
        arch = server_block.get("Arch") or ""
        platform = f"{os_name}/{arch}" if os_name and arch else None
        return RuntimeVersions(
            client_version=client, server_version=server, platform=platform
        )
    except (
        subprocess.TimeoutExpired,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        AttributeError,
    ) as exc:
        return RuntimeVersions(probe_warning=f"{exc.__class__.__name__}: {exc}")


def compose_available(runtime: ContainerRuntime) -> bool:
    """Whether `<tool> compose` resolves to a working provider."""
    try:
        proc = container_run(
            runtime,
            ["compose", "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0
