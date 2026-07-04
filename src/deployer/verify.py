"""Two-level deterministic verification of Dockerfile candidates.

L1 (this file's static half): parse, COPY-source existence, base-image pinning,
hadolint at a pinned version. L2 (docker half, Task 5): sandboxed build + run.
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from deployer.models import (
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    VerificationReport,
)

HADOLINT_VERSION = "2.12.0"


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


def verify_static(dockerfile: str, project_path: Path) -> VerificationReport:
    """Run all L1 static checks against a Dockerfile candidate."""
    instructions = parse_dockerfile(dockerfile)
    results = [_check_parses(instructions)]
    if results[0].status is CheckStatus.PASSED:
        results.append(_check_copy_sources(instructions, project_path))
        results.append(_check_base_pinned(instructions))
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
)


def detect_container_tool() -> str | None:
    """Prefer rootless-friendly podman; fall back to docker."""
    for tool in ("podman", "docker"):
        if shutil.which(tool):
            return tool
    return None


def _classify(stderr: str) -> FailureKind:
    lowered = stderr.lower()
    if any(marker in lowered for marker in ENVIRONMENT_MARKERS):
        return FailureKind.ENVIRONMENT
    return FailureKind.AUTHORING


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _build(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    tool: str,
    tag: str,
    timeout: int,
) -> CheckResult:
    try:
        proc = subprocess.run(
            [
                tool,
                "build",
                "--memory",
                target.memory_limit,
                "-t",
                tag,
                "-f",
                "-",
                str(project_path),
            ],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message=f"build timed out after {timeout}s",
        )
    if proc.returncode != 0:
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=_classify(proc.stderr),
            message=_tail(proc.stderr),
        )
    return CheckResult(check_id="build", status=CheckStatus.PASSED)


def _run_healthcheck(
    target: DeployTarget, tool: str, tag: str, timeout: int
) -> CheckResult:
    assert target.service is not None
    container = f"deployer-check-{uuid.uuid4().hex[:8]}"
    url = f"http://127.0.0.1:{target.service.port}{target.service.healthcheck_path}"
    probe = f"import urllib.request; urllib.request.urlopen('{url}', timeout=2)"
    try:
        started = subprocess.run(
            [
                tool,
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
                failure_kind=_classify(started.stderr),
                message=_tail(started.stderr),
            )
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                probe_proc = subprocess.run(
                    [tool, "exec", container, "python", "-c", probe],
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, remaining),
                )
                if probe_proc.returncode == 0:
                    return CheckResult(
                        check_id="run_healthcheck", status=CheckStatus.PASSED
                    )
                last_error = probe_proc.stderr
            except subprocess.TimeoutExpired:
                # Probe timed out; treat as loop exhaustion
                break
            time.sleep(1)
        logs = subprocess.run(
            [tool, "logs", container], capture_output=True, text=True, timeout=10
        )
        log_text = (logs.stdout + "\n" + logs.stderr).strip()
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=(
                f"healthcheck {url} failed within {timeout}s: "
                f"{_tail(last_error, 3)}\ncontainer logs:\n{_tail(log_text)}"
            ),
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message="container runtime command timed out",
        )
    finally:
        subprocess.run([tool, "rm", "-f", container], capture_output=True, timeout=30)


def verify_docker(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    tool: str,
    *,
    build_timeout: int = 600,
    health_timeout: int = 30,
) -> list[CheckResult]:
    """L2: real sandboxed build; for service intents, run + loopback healthcheck.

    The healthcheck probes over the container's loopback via `exec python -c`,
    so `--network=none` still works. This assumes a Python base image — true
    for every artifact this MVP authors.
    """
    tag = f"deployer-verify-{uuid.uuid4().hex[:8]}"
    results: list[CheckResult] = []
    try:
        build_result = _build(
            dockerfile, project_path, target, tool, tag, build_timeout
        )
        results.append(build_result)
        if build_result.status is CheckStatus.PASSED and target.service is not None:
            results.append(_run_healthcheck(target, tool, tag, health_timeout))
    finally:
        subprocess.run([tool, "rmi", "-f", tag], capture_output=True, timeout=60)
    return results


def verify(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    tool: str | None,
) -> VerificationReport:
    """Full verification: L1 static always; L2 docker when available and L1 passed."""
    report = verify_static(dockerfile, project_path)
    if tool is None:
        return report
    report.docker_available = True
    if report.passed:
        report.results.extend(verify_docker(dockerfile, project_path, target, tool))
    return report
