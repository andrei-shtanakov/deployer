"""Two-level deterministic verification of Dockerfile candidates.

L1 (this file's static half): parse, COPY-source existence, base-image pinning,
hadolint at a pinned version. L2 (docker half, Task 5): sandboxed build + run.
"""

import json
import shutil
import subprocess
from pathlib import Path

from deployer.models import (
    CheckResult,
    CheckStatus,
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
        image = args.split()[0]
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
