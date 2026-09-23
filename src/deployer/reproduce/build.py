"""The build adapter (spec §4.3), its cleanup (§4.4) and local digests (§4.5).

Its own argv through ``runtime.container_run``: the Dockerfile by path so the
Dockerfile-specific ignore file applies, its own tag, ``--force-rm`` on
Podman, no memory limit. Full output is kept; no class is attached.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from deployer import runtime
from deployer.models import ContainerRuntime
from deployer.reproduce.buildline import BuildConfig

_RMI_TIMEOUT_S = 60
_INSPECT_TIMEOUT_S = 15


@dataclass(frozen=True)
class BuildRun:
    """What the build did; ``exit_code`` is ``None`` iff it did not finish."""

    argv: list[str]
    exit_code: int | None
    launch_error: str | None
    stdout: str
    stderr: str


def repro_tag(run_id: int, seq: str) -> str:
    """The adapter's own, fully qualified tag; CI's ``-t`` is never used."""
    return f"localhost/deployer-repro-{run_id}-{seq}"


def run_build(
    rt: ContainerRuntime, context: Path, config: BuildConfig, tag: str, timeout: int
) -> BuildRun:
    """Build ``context`` with ``config``; never raises for build outcomes."""
    args = ["build", "--file", str(context / config.dockerfile)]
    for key, value in config.build_args:
        args += ["--build-arg", f"{key}={value}"]
    if config.platform is not None:
        args += ["--platform", config.platform]
    args += ["--tag", tag]
    if rt.tool == "podman":
        args.append("--force-rm")
    args.append(str(context))
    argv = [rt.tool, *args]
    try:
        result = runtime.container_run(
            rt,
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return BuildRun(argv, None, "timeout", _text(exc.output), _text(exc.stderr))
    except FileNotFoundError:
        return BuildRun(argv, None, "executable not found", "", "")
    except OSError as exc:
        return BuildRun(argv, None, f"{exc.__class__.__name__}: {exc}", "", "")
    return BuildRun(
        argv, result.returncode, None, result.stdout or "", result.stderr or ""
    )


def cleanup_image(
    rt: ContainerRuntime, tag: str, built: bool
) -> Literal["removed", "failed", "not_attempted"]:
    """Remove the adapter's tag and read the return code."""
    if not built:
        return "not_attempted"
    try:
        result = runtime.container_run(
            rt, ["rmi", "-f", tag], capture_output=True, timeout=_RMI_TIMEOUT_S
        )
    except (subprocess.TimeoutExpired, OSError):
        return "failed"
    return "removed" if result.returncode == 0 else "failed"


def build_containers_state(
    rt: ContainerRuntime, finished: bool
) -> Literal["removed_by_builder", "not_checked", "not_applicable"]:
    """Podman's working containers: removed by ``--force-rm`` only if it finished.

    Docker/BuildKit creates no user-visible build containers.
    """
    if rt.tool == "docker":
        return "not_applicable"
    return "removed_by_builder" if finished else "not_checked"


def local_repo_digests(rt: ContainerRuntime, images: list[str]) -> dict[str, list[str]]:
    """``sha256:…`` digests the local store records per image; ``[]`` if unknown."""
    out: dict[str, list[str]] = {}
    for image in images:
        args = ["image", "inspect", "--format", "{{json .RepoDigests}}", image]
        try:
            result = runtime.container_run(
                rt, args, capture_output=True, text=True, timeout=_INSPECT_TIMEOUT_S
            )
            values = json.loads(result.stdout) if result.returncode == 0 else []
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            values = []
        out[image] = [
            str(value).split("@", 1)[1] for value in values or [] if "@" in str(value)
        ]
    return out


def _text(value: bytes | str | None) -> str:
    """Normalise ``TimeoutExpired``'s output/stderr, which may be bytes or text."""
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )
