"""Container runtime resolution and the single subprocess chokepoint."""

import os
import shutil
from collections.abc import Mapping
from typing import Literal, cast

from deployer.models import ContainerRuntime

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
