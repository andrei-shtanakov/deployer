"""Two-level deterministic verification of Dockerfile candidates.

L1 (this file's static half): parse, COPY-source existence, base-image pinning,
hadolint at a pinned version. L2 (docker half, Task 5): sandboxed build + run.
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    VerificationReport,
)
from deployer.runtime import container_run

HADOLINT_VERSION = "2.12.0"
DEFAULT_BUILD_TIMEOUT = 600
DEFAULT_HEALTH_TIMEOUT = 30
_ENV_ASSIGNMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_PYTHON_M_PIP = re.compile(r"^\S*python[\d.]*\s+-m\s+pip\s+install\b")

CONTEXT_IGNORE = (
    ".git",
    ".venv",
    ".deployer",
    ".env",
    ".env.*",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
)


@contextmanager
def _isolated_context(project_path: Path) -> Iterator[Path]:
    """Deterministic temp build context: the project minus CONTEXT_IGNORE.

    Restores the MVP invariant "no secrets in the build context, ever" —
    with a remote daemon the context leaves the machine entirely.
    """
    with tempfile.TemporaryDirectory(prefix="deployer-context-") as tmp:
        context = Path(tmp) / "context"
        shutil.copytree(
            project_path,
            context,
            ignore=shutil.ignore_patterns(*CONTEXT_IGNORE),
            symlinks=True,
        )
        yield context


def parse_dockerfile(text: str) -> list[tuple[str, str]]:
    """Split a Dockerfile into (INSTRUCTION, args) pairs.

    Joins backslash line-continuations and drops comments/blank lines.
    """
    logical_lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical_lines.append(buffer + line)
        buffer = ""
    if buffer:
        logical_lines.append(buffer.strip())

    instructions: list[tuple[str, str]] = []
    for line in logical_lines:
        head, _, rest = line.partition(" ")
        instructions.append((head.upper(), rest.strip()))
    return instructions


def _check_parses(instructions: list[tuple[str, str]]) -> CheckResult:
    if not instructions:
        return CheckResult(
            check_id="parses",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="empty Dockerfile",
        )
    if instructions[0][0] != "FROM" or not instructions[0][1]:
        return CheckResult(
            check_id="parses",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="Dockerfile must start with a FROM instruction",
        )
    return CheckResult(check_id="parses", status=CheckStatus.PASSED)


def _check_copy_sources(
    instructions: list[tuple[str, str]], project_path: Path
) -> CheckResult:
    missing: list[str] = []
    for name, args in instructions:
        if name not in ("COPY", "ADD"):
            continue
        tokens = args.split()
        if any(t.startswith("--from=") for t in tokens):
            continue  # copies from a build stage, not the context
        sources = [t for t in tokens if not t.startswith("--")][:-1]
        for src in sources:
            if src.startswith(("http://", "https://")):
                continue
            if any(ch in src for ch in "*?["):
                if not list(project_path.glob(src)):
                    missing.append(src)
            elif not (project_path / src).exists():
                missing.append(src)
    if missing:
        return CheckResult(
            check_id="copy_sources",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=f"COPY/ADD sources not found in project: {', '.join(missing)}",
        )
    return CheckResult(check_id="copy_sources", status=CheckStatus.PASSED)


def _run_commands(run_lines: list[str]) -> list[str]:
    """Split RUN lines into individual shell commands (segments)."""
    commands: list[str] = []
    for line in run_lines:
        for segment in re.split(r"&&|\|\||;|\|", line):
            stripped = segment.strip()
            if not stripped:
                continue
            stripped = _ENV_ASSIGNMENT.sub("", stripped)
            if stripped:
                commands.append(stripped)
    return commands


def _check_base_pinned(instructions: list[tuple[str, str]]) -> CheckResult:
    for name, args in instructions:
        if name != "FROM":
            continue
        tokens = [t for t in args.split() if not t.startswith("--")]
        if not tokens:
            continue
        image = tokens[0]
        if "@sha256:" in image:
            continue
        _, _, tag = image.partition(":")
        if not tag or tag == "latest":
            return CheckResult(
                check_id="base_pinned",
                status=CheckStatus.WARNING,
                message=f"base image '{image}' has no pinned tag; "
                "reproducible builds need a tag (ideally a digest)",
            )
    return CheckResult(check_id="base_pinned", status=CheckStatus.PASSED)


def _check_install_strategy(
    instructions: list[tuple[str, str]], facts: ProjectFacts
) -> CheckResult:
    """Deterministic install-strategy rules, promoted from prompt to check."""
    run_lines = [args for name, args in instructions if name == "RUN"]
    commands = _run_commands(run_lines)
    problems: list[str] = []

    # uv-in-pip-project rule
    if facts.package_manager == "pip":
        for cmd in commands:
            if cmd.startswith(("uv sync", "uv pip")):
                problems.append(
                    "project uses pip (requirements.txt) but Dockerfile invokes uv"
                )
                break

    # pip-in-uv-project rule
    if facts.package_manager == "uv":
        for cmd in commands:
            if cmd.startswith(("pip install", "pip3 install")) or _PYTHON_M_PIP.match(
                cmd
            ):
                problems.append("project uses uv (uv.lock) but Dockerfile invokes pip")
                break

    # no-build-system rule
    if not facts.has_build_system:
        for cmd in commands:
            installs_project = False

            # Check for "pip install ." (bare . token)
            if cmd.startswith(("pip install", "pip3 install")):
                tokens = cmd.split()
                if "." in tokens:
                    installs_project = True

            # Check for "uv sync" without "--no-install-project"
            if cmd.startswith("uv sync") and "--no-install-project" not in cmd:
                installs_project = True

            if installs_project:
                problems.append(
                    "project has no [build-system]: do not install it as a "
                    "package (run sources directly / use --no-install-project)"
                )
                break

    if problems:
        return CheckResult(
            check_id="install_strategy",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="; ".join(problems),
        )
    return CheckResult(check_id="install_strategy", status=CheckStatus.PASSED)


def _check_hadolint(dockerfile: str) -> tuple[CheckResult, bool]:
    """Run hadolint at the pinned version; (result, available_and_comparable)."""
    binary = shutil.which("hadolint")
    if binary is None:
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.SKIPPED,
                message=f"hadolint {HADOLINT_VERSION} not installed; "
                "run is non-comparable",
            ),
            False,
        )
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        if HADOLINT_VERSION not in version:
            return (
                CheckResult(
                    check_id="hadolint",
                    status=CheckStatus.SKIPPED,
                    message=f"hadolint version mismatch (want {HADOLINT_VERSION}, "
                    f"got: {version.strip()}); run is non-comparable",
                ),
                False,
            )
        proc = subprocess.run(
            [binary, "--no-color", "-f", "json", "-"],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=30,
        )
        findings = json.loads(proc.stdout) if proc.stdout.strip() else []
        errors = [f for f in findings if f.get("level") == "error"]
        if errors:
            lines = "; ".join(f"{f['code']}: {f['message']}" for f in errors)
            return (
                CheckResult(
                    check_id="hadolint",
                    status=CheckStatus.FAILED,
                    failure_kind=FailureKind.AUTHORING,
                    message=lines,
                ),
                True,
            )
        if findings:
            lines = "; ".join(f"{f['code']}: {f['message']}" for f in findings)
            return (
                CheckResult(
                    check_id="hadolint", status=CheckStatus.WARNING, message=lines
                ),
                True,
            )
        return (CheckResult(check_id="hadolint", status=CheckStatus.PASSED), True)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.SKIPPED,
                message=f"hadolint execution failed ({exc.__class__.__name__}); "
                "run is non-comparable",
            ),
            False,
        )


def verify_static(
    dockerfile: str, project_path: Path, facts: ProjectFacts | None = None
) -> VerificationReport:
    """Run all L1 static checks against a Dockerfile candidate."""
    instructions = parse_dockerfile(dockerfile)
    results = [_check_parses(instructions)]
    if results[0].status is CheckStatus.PASSED:
        results.append(_check_copy_sources(instructions, project_path))
        results.append(_check_base_pinned(instructions))
        if facts is not None:
            results.append(_check_install_strategy(instructions, facts))
        else:
            results.append(
                CheckResult(
                    check_id="install_strategy",
                    status=CheckStatus.SKIPPED,
                    message="no project facts provided",
                )
            )
    hadolint_result, hadolint_available = _check_hadolint(dockerfile)
    results.append(hadolint_result)
    return VerificationReport(results=results, hadolint_available=hadolint_available)


ENVIRONMENT_MARKERS = (
    "tls handshake",
    "connection refused",
    "connection reset",
    "temporary failure",
    "i/o timeout",
    "toomanyrequests",
    "network is unreachable",
    "no route to host",
    "service unavailable",
    "permission denied (publickey)",
    "host key verification failed",
    "could not resolve hostname",
    "ssh: connect to host",
    "connection timed out",
    "cannot connect to the docker daemon",
    "error during connect",
    "context deadline exceeded",
)


def _classify(output: str) -> FailureKind:
    lowered = output.lower()
    if any(marker in lowered for marker in ENVIRONMENT_MARKERS):
        return FailureKind.ENVIRONMENT
    return FailureKind.AUTHORING


_TRANSPORT_MARKERS = (
    "error during connect",
    "cannot connect to the docker daemon",
    "ssh: ",
    "context deadline exceeded",
)


def _is_transport_failure(output: str) -> bool:
    """Narrow marker check for daemon/SSH-transport loss during a probe loop.

    Deliberately narrower than `_classify`: a legitimately failing
    in-container probe can print a Python traceback containing phrases
    like "connection refused" (the app refusing its own port), which
    would otherwise flip a real authoring failure to ENVIRONMENT.
    """
    lowered = output.lower()
    return any(marker in lowered for marker in _TRANSPORT_MARKERS)


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _build(
    dockerfile: str,
    context_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    tag: str,
    timeout: int,
) -> CheckResult:
    try:
        proc = container_run(
            runtime,
            [
                "build",
                "--memory",
                target.memory_limit,
                "-t",
                tag,
                "-f",
                "-",
                str(context_path),
            ],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        message = (
            f"build timed out after {timeout}s"
            if isinstance(exc, subprocess.TimeoutExpired)
            else f"container runtime command failed: {exc}"
        )
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message=message,
        )
    if proc.returncode != 0:
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=_classify(proc.stdout + "\n" + proc.stderr),
            message=_tail(proc.stderr or proc.stdout),
        )
    return CheckResult(check_id="build", status=CheckStatus.PASSED)


def _image_size(runtime: ContainerRuntime, tag: str) -> int | None:
    """Size of a built image in bytes; None when inspection fails."""
    try:
        proc = container_run(
            runtime,
            ["image", "inspect", "--format", "{{.Size}}", tag],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _run_healthcheck(
    target: DeployTarget, runtime: ContainerRuntime, tag: str, timeout: int
) -> CheckResult:
    assert target.service is not None
    container = f"deployer-check-{uuid.uuid4().hex[:8]}"
    url = f"http://127.0.0.1:{target.service.port}{target.service.healthcheck_path}"
    probe = f"import urllib.request; urllib.request.urlopen('{url}', timeout=2)"
    try:
        started = container_run(
            runtime,
            [
                "run",
                "-d",
                "--name",
                container,
                "--network=none",
                "--memory",
                target.memory_limit,
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if started.returncode != 0:
            return CheckResult(
                check_id="run_healthcheck",
                status=CheckStatus.FAILED,
                failure_kind=_classify(started.stdout + "\n" + started.stderr),
                message=_tail(started.stderr or started.stdout),
            )
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                probe_proc = container_run(
                    runtime,
                    ["exec", container, "python", "-c", probe],
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, remaining),
                )
                if probe_proc.returncode == 0:
                    return CheckResult(
                        check_id="run_healthcheck", status=CheckStatus.PASSED
                    )
                last_error = probe_proc.stdout + "\n" + probe_proc.stderr
            except subprocess.TimeoutExpired:
                # Probe timed out; treat as loop exhaustion
                break
            time.sleep(1)
        logs = container_run(
            runtime, ["logs", container], capture_output=True, text=True, timeout=10
        )
        log_text = (logs.stdout + "\n" + logs.stderr).strip()
        if _is_transport_failure(last_error):
            return CheckResult(
                check_id="run_healthcheck",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.ENVIRONMENT,
                message=(
                    f"healthcheck {url}: container daemon became unreachable "
                    f"mid-poll: {_tail(last_error, 3)}"
                ),
            )
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=(
                f"healthcheck {url} failed within {timeout}s: "
                f"{_tail(last_error, 3)}\ncontainer logs:\n{_tail(log_text)}"
            ),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        message = (
            "container runtime command timed out"
            if isinstance(exc, subprocess.TimeoutExpired)
            else f"container runtime command failed: {exc}"
        )
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message=message,
        )
    finally:
        try:
            container_run(
                runtime, ["rm", "-f", container], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort cleanup; must never clobber the return value


def verify_docker(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> tuple[list[CheckResult], int | None]:
    """L2: real sandboxed build; for service intents, run + loopback healthcheck.

    The healthcheck probes over the container's loopback via `exec python -c`,
    so `--network=none` still works. This assumes a Python base image — true
    for every artifact this MVP authors.
    """
    tag = f"deployer-verify-{uuid.uuid4().hex[:8]}"
    results: list[CheckResult] = []
    image_size: int | None = None
    try:
        with _isolated_context(project_path) as context:
            build_result = _build(
                dockerfile, context, target, runtime, tag, build_timeout
            )
        results.append(build_result)
        if build_result.status is CheckStatus.PASSED:
            image_size = _image_size(runtime, tag)
            if target.service is not None:
                results.append(_run_healthcheck(target, runtime, tag, health_timeout))
    finally:
        try:
            container_run(runtime, ["rmi", "-f", tag], capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort cleanup; must never clobber the return value
    return results, image_size


def verify(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime | None,
    facts: ProjectFacts | None = None,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> VerificationReport:
    """Full verification: L1 static always; L2 docker when available and L1 passed.

    The timeouts bound the L2 build and healthcheck subprocesses (seconds).
    """
    report = verify_static(dockerfile, project_path, facts)
    report.runtime = runtime
    if runtime is None:
        return report
    report.docker_available = True
    if report.passed:
        docker_results, image_size = verify_docker(
            dockerfile,
            project_path,
            target,
            runtime,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        report.results.extend(docker_results)
        report.image_size_bytes = image_size
    return report
