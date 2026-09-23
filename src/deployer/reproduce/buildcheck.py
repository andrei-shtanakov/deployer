"""The builder's own ``--check`` (spec §2): run only under ``--reproduce``.

Docker documents no machine-readable form and two text forms; the reader
recognises exactly the shapes below and sends everything else to "not
recognised" — never to a finding.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from deployer import runtime
from deployer.models import ContainerRuntime
from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence

_PROGRESS_RE = re.compile(r"^(\[\+\] |=> |#\d+ )")
_PARSE_RE = re.compile(r"dockerfile parse error on line (\d+): (.*)$")
_HEADER_RE = re.compile(
    r"^(?:WARNING: )?([A-Z][A-Za-z0-9]+)"
    r"(?: - https://docs\.docker\.com/go/dockerfile/rule/[a-z0-9-]+/)?$"
)
_LOCATION_RE = re.compile(r"^\S+:(\d+)$")
_FENCE_RE = re.compile(r"^-+$")
_NUMBERED_RE = re.compile(r"^\s*\d+ \|")
_SUMMARY_RE = re.compile(r"^Check complete, \d+ warnings? (?:has|have) been found!$")
_BUILDX_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_MIN_BUILDX = (0, 15, 0)


@dataclass(frozen=True)
class BuilderSyntax:
    """The builder check's syntax verdict: passed, a line error, or skipped."""

    state: Literal["passed", "error", "skipped"]
    line: int | None
    text: str | None
    reason: str | None


def read_check_output(
    exit_code: int | None, launch_error: str | None, output: str
) -> tuple[BuilderSyntax, list[ReproductionCheck]]:
    """The ordered table of §2; first match wins."""
    if launch_error is not None or exit_code is None:
        return BuilderSyntax("skipped", None, None, launch_error or "not launched"), []
    lines = [ln.rstrip() for ln in output.splitlines()]
    for line in lines:
        match = _PARSE_RE.search(line)
        if match:
            return BuilderSyntax("error", int(match.group(1)), match.group(2), None), []
    content = [ln for ln in lines if ln.strip() and not _PROGRESS_RE.match(ln)]
    blocks, leftover = _lint_blocks(content)
    lint = [
        ReproductionCheck(
            check_id="builder_lint",
            status="observation",
            finding=f"{rule}: {description}",
            location=Location(file="Dockerfile", lines=(line_no, line_no)),
        )
        for rule, description, line_no in blocks
    ]
    if exit_code == 0:
        return BuilderSyntax("passed", None, None, None), lint
    if blocks and not leftover:
        return BuilderSyntax("passed", None, None, None), lint
    return (
        BuilderSyntax("skipped", None, output, "build check output not recognised"),
        [],
    )


def detect_buildx(rt: ContainerRuntime) -> str | None:
    """``docker buildx version`` → ``"0.15.1"``, or ``None``."""
    try:
        proc = runtime.container_run(
            rt, ["buildx", "version"], capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _BUILDX_RE.search(proc.stdout or "") if proc.returncode == 0 else None
    return ".".join(match.groups()) if match else None


def run_builder_check(
    rt: ContainerRuntime, context: Path, dockerfile: str, timeout: int
) -> tuple[BuilderSyntax, list[ReproductionCheck], str | None]:
    """Run ``docker build --check`` when the backend has it; else skipped."""
    if rt.tool != "docker":
        return (
            BuilderSyntax("skipped", None, None, "backend has no build check"),
            [],
            None,
        )
    version = detect_buildx(rt)
    if version is None:
        return BuilderSyntax("skipped", None, None, "buildx not found"), [], None
    if tuple(int(p) for p in version.split(".")) < _MIN_BUILDX:
        reason = f"buildx {version} < 0.15"
        return BuilderSyntax("skipped", None, None, reason), [], version
    args = ["build", "--check", "--file", str(context / dockerfile), str(context)]
    try:
        proc = runtime.container_run(
            rt, args, capture_output=True, text=True, errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return BuilderSyntax("skipped", None, None, "timeout"), [], version
    except OSError as exc:
        return BuilderSyntax("skipped", None, None, str(exc)), [], version
    syntax, lint = read_check_output(
        proc.returncode, None, (proc.stdout or "") + "\n" + (proc.stderr or "")
    )
    return syntax, lint, version


def merge_syntax(
    parser_checks: list[ReproductionCheck], builder: BuilderSyntax, dockerfile: str
) -> list[ReproductionCheck]:
    """Parser vs builder, syntax only (§2)."""
    builder_ev = ReproEvidence(
        kind="output_file", path="check.stdout", text=builder.text
    )
    out: list[ReproductionCheck] = []
    matched = False
    for check in parser_checks:
        if check.status != "failed" or check.location is None:
            out.append(check)
            continue
        if builder.state == "error" and check.location.lines[0] == builder.line:
            out.append(
                check.model_copy(update={"evidence": [*check.evidence, builder_ev]})
            )
            matched = True
        elif builder.state == "passed":
            out.append(
                check.model_copy(
                    update={
                        "status": "inconclusive",
                        "reason": "parser found an error the builder check passed",
                        "evidence": [*check.evidence, builder_ev],
                    }
                )
            )
        else:
            out.append(check)
    if builder.state == "error" and not matched and builder.line is not None:
        out.append(
            ReproductionCheck(
                check_id="builder_syntax",
                status="failed",
                finding=f"line {builder.line}: {builder.text}",
                location=Location(file=dockerfile, lines=(builder.line, builder.line)),
                evidence=[builder_ev],
            )
        )
    if builder.state == "skipped":
        evidence = [builder_ev] if builder.text is not None else []
        out.append(
            ReproductionCheck(
                check_id="builder_check",
                status="skipped",
                reason=builder.reason,
                evidence=evidence,
            )
        )
    return out


def _lint_blocks(lines: list[str]) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Recognised lint blocks and every line that belongs to none."""
    blocks: list[tuple[str, str, int]] = []
    leftover: list[str] = []
    i = 0
    while i < len(lines):
        header = _HEADER_RE.match(lines[i].strip())
        loc = _LOCATION_RE.match(lines[i + 2].strip()) if i + 2 < len(lines) else None
        fence = i + 3 < len(lines) and _FENCE_RE.match(lines[i + 3].strip())
        if header and loc and fence:
            j = i + 4
            while j < len(lines) and _NUMBERED_RE.match(lines[j]):
                j += 1
            if j < len(lines) and _FENCE_RE.match(lines[j].strip()):
                blocks.append(
                    (header.group(1), lines[i + 1].strip(), int(loc.group(1)))
                )
                i = j + 1
                continue
        if not _SUMMARY_RE.match(lines[i].strip()):
            leftover.append(lines[i])
        i += 1
    return blocks, leftover
