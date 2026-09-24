"""Confirm the container endpoint is on this machine (spec §4.2).

The restored context is built unfiltered — CI's builder saw any tracked
``.env`` too — so it may only reach a builder on this host. Overrides are
refused, not judged; the detected endpoint must be a unix socket or loopback.
"""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from deployer import runtime
from deployer.models import ContainerRuntime
from deployer.reproduce.shape import Refusal

ENV_OVERRIDES = (
    "DEPLOYER_CONTAINER_HOST",
    "DOCKER_HOST",
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
    "DOCKER_CONTEXT",
)
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TIMEOUT_S = 15


@dataclass(frozen=True)
class Endpoint:
    """The endpoint the builder will use and how it was chosen."""

    uri: str
    source: str


def is_local(uri: str) -> bool:
    """A unix socket, or ssh/tcp to a loopback host."""
    parsed = urlparse(uri)
    if parsed.scheme == "unix":
        return True
    return parsed.scheme in ("ssh", "tcp") and (parsed.hostname or "") in _LOOPBACK


def confirm_local(rt: ContainerRuntime, env: Mapping[str, str]) -> Endpoint | Refusal:
    """The actual endpoint if it is confirmed local, else a named refusal."""
    for name in ENV_OVERRIDES:
        if env.get(name):
            return Refusal(f"endpoint set by {name} not confirmed local")
    if rt.host is not None:
        return Refusal("endpoint set by --container-host not confirmed local")
    detected = _detect(rt)
    if isinstance(detected, Refusal):
        return detected
    if not is_local(detected.uri):
        return Refusal(f"endpoint {detected.uri} not confirmed local")
    return detected


def _detect(rt: ContainerRuntime) -> Endpoint | Refusal:
    if rt.tool == "docker":
        args = ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    else:
        args = ["system", "connection", "list", "--format", "json"]
    try:
        proc = runtime.container_run(
            rt, args, capture_output=True, text=True, timeout=_TIMEOUT_S
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Refusal(f"endpoint detection failed: {exc.__class__.__name__}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return Refusal(f"endpoint detection failed: {detail}")
    if rt.tool == "docker":
        return Endpoint(proc.stdout.strip(), "docker_active_context")
    try:
        connections = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return Refusal("endpoint detection failed: connection list not JSON")
    defaults = [c for c in connections if isinstance(c, dict) and c.get("Default")]
    if not connections:
        return Endpoint("unix://", "podman_local_socket")
    if len(defaults) != 1:
        return Refusal("endpoint detection failed: no single default connection")
    return Endpoint(str(defaults[0].get("URI", "")), "podman_default_connection")
