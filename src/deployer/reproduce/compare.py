"""CI versus local (spec §7): identity by Dockerfile span, one ordered state."""

import re
from dataclasses import dataclass

from deployer.reproduce.dockerfile import KEYWORDS, ParsedDockerfile, normalise
from deployer.reproduce.model import (
    Comparison,
    Dimension,
    InstructionRef,
    ReproductionCheck,
    SignatureMatch,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BLOCK_HEAD_RE = re.compile(r"^[^\s:]+:(\d+)$")
_MARKED_RE = re.compile(r"^\s*(\d+) \| >>>")
_PARSE_RE = re.compile(r"dockerfile parse error on line (\d+):")
_STEP_ERROR_RE = re.compile(r"^#(\d+) ERROR: ")
_STEP_RE = re.compile(r"^(?:\[\d+/\d+\] )?STEP \d+/\d+: (.*)$")
_BUILDING_AT_RE = re.compile(r'building at STEP "(.*?)": ')
_RESOLVE_RE = re.compile(r"resolve (\S+)@(sha256:[0-9a-f]{64})")
_PODMAN_ERROR_RE = re.compile(r"^Error: (\S+) ")


@dataclass(frozen=True)
class Side:
    """One side's bound instruction, program-output signature and builder error."""

    instruction: InstructionRef | None
    signature: str | None
    builder_error: str | None


def ci_instruction(job_text: str) -> InstructionRef | None:
    """BuildKit's parse error, else its single ``Dockerfile:N`` block (§7.1)."""
    parses = _PARSE_RE.findall(job_text)
    distinct = set(parses)
    if len(distinct) == 1:
        line = int(next(iter(distinct)))
        return InstructionRef(
            kind="parse", lines=(line, line), bound_by="buildkit_parse_error"
        )
    spans = _error_blocks(job_text.splitlines())
    if len(spans) != 1:
        return None
    return InstructionRef(kind="span", lines=spans[0], bound_by="buildkit_error_block")


def ci_signature(job_text: str) -> str | None:
    """Last program-output line of the failing BuildKit step, framing stripped."""
    return _buildkit_signature(job_text.splitlines())


def ci_digests(job_text: str) -> dict[str, str]:
    """Image → digest from BuildKit's ``resolve <image>@sha256:…`` lines."""
    return {m.group(1): m.group(2) for m in _RESOLVE_RE.finditer(job_text)}


def local_instruction(
    stdout: str,
    stderr: str,
    backend: str,
    parsed: ParsedDockerfile,
    parser_findings: list[ReproductionCheck],
) -> InstructionRef | None:
    """Bind the local failure to a span, or to a parse line by keyword (§7.1)."""
    if backend == "docker":
        return ci_instruction(stdout + "\n" + stderr)
    out = _clean(stdout).splitlines()
    err = _clean(stderr)
    at = _BUILDING_AT_RE.findall(err)
    steps = [m.group(1) for ln in out if (m := _STEP_RE.match(ln.strip()))]
    candidates = [
        text
        for text in (at[-1] if at else None, steps[-1] if steps else None)
        if text is not None
    ]
    for text in candidates:
        span = _match_instruction(text, parsed)
        if span is not None:
            return InstructionRef(kind="span", lines=span, bound_by="step_text")
    if candidates:
        return None
    return _parse_binding(err, parser_findings, parsed)


def local_signature(stdout: str, stderr: str, backend: str) -> str | None:
    """Last program-output line after the last STEP (Podman) or BuildKit's."""
    if backend == "docker":
        return _buildkit_signature((stdout + "\n" + stderr).splitlines())
    lines = _clean(stdout).splitlines()
    last_step = max(
        (i for i, ln in enumerate(lines) if _STEP_RE.match(ln.strip())), default=None
    )
    if last_step is None:
        return None
    body = [
        ln.strip()
        for ln in lines[last_step + 1 :]
        if ln.strip() and not ln.startswith("--> ")
    ]
    return body[-1] if body else None


def dimension(ci: str | None, local: str | None) -> Dimension:
    """``same``/``differs`` only with values from both sides."""
    if ci is None or local is None:
        return "unknown"
    return "same" if ci == local else "differs"


def digest_dimension(
    ci: dict[str, str], local: dict[str, list[str]], images: list[str]
) -> Dimension:
    """``same`` iff every image's CI digest is recorded locally; else unknown."""
    if not images:
        return "unknown"
    for image in images:
        digest = _ci_digest_for(image, ci)
        if digest is None or digest not in local.get(image, []):
            return "unknown"
    return "same"


def compare(
    *,
    exit_code: int | None,
    launch_error: str | None,
    ci: Side,
    local: Side,
    parsed: ParsedDockerfile,
    dimensions: dict[str, Dimension],
    values: dict[str, tuple[str | None, str | None]],
) -> Comparison:
    """The first state of §7.3 that applies."""
    base = {
        "ci_instruction": ci.instruction,
        "dimensions": dimensions,
        "values": values,
    }
    if launch_error is not None and launch_error != "timeout":
        return Comparison(
            state="not_attempted",
            reason=f"build could not start: {launch_error}",
            **base,
        )
    if launch_error == "timeout":
        return Comparison(state="inconclusive", reason="build did not finish", **base)
    if exit_code == 0:
        return Comparison(state="not_reproduced", **base)
    if ci.instruction is None:
        return Comparison(state="inconclusive", reason="CI instruction unbound", **base)
    if local.instruction is None:
        return Comparison(state="inconclusive", reason="local failure unbound", **base)
    if (ci.instruction.kind, ci.instruction.lines) != (
        local.instruction.kind,
        local.instruction.lines,
    ):
        return Comparison(state="different_failure", **base)
    match = _signature_match(ci, local, parsed, values.get("backend"))
    if match == "unavailable":
        return Comparison(
            state="inconclusive",
            reason="signature unavailable",
            signature_match=match,
            **base,
        )
    if match == "unequal":
        return Comparison(
            state="same_instruction_different_output", signature_match=match, **base
        )
    state = (
        "reproduced"
        if all(d == "same" for d in dimensions.values())
        else "reproduced_with_differences"
    )
    return Comparison(state=state, signature_match=match, **base)


def _signature_match(
    ci: Side,
    local: Side,
    parsed: ParsedDockerfile,
    backends: tuple[str | None, str | None] | None,
) -> SignatureMatch:
    same_backend = backends is not None and backends[0] == backends[1]
    assert ci.instruction is not None
    first = ci.instruction.lines[0]
    keyword = next(
        (i.keyword for i in parsed.instructions if i.first_line == first), None
    )
    if ci.instruction.kind == "span" and keyword == "RUN":
        if ci.signature is None or local.signature is None:
            return "unavailable"
        return "equal" if ci.signature == local.signature else "unequal"
    if not same_backend:
        return "not_compared"
    if ci.builder_error is None or local.builder_error is None:
        return "unavailable"
    return "equal" if ci.builder_error == local.builder_error else "unequal"


def _error_blocks(lines: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if not _BLOCK_HEAD_RE.match(line.strip()):
            continue
        marked: list[int] = []
        for follow in lines[i + 2 :]:
            if follow.strip().startswith("---"):
                break
            m = _MARKED_RE.match(follow)
            if m:
                marked.append(int(m.group(1)))
        if marked:
            spans.append((min(marked), max(marked)))
    return spans


def _buildkit_signature(lines: list[str]) -> str | None:
    clean = [_clean(ln) for ln in lines]
    errors = [
        (i, m.group(1)) for i, ln in enumerate(clean) if (m := _STEP_ERROR_RE.match(ln))
    ]
    if not errors:
        return None
    index, step = errors[-1]
    prefix = re.compile(rf"^#{step} \d+\.\d+ (.*)$")
    body = [
        m.group(1).strip()
        for ln in clean[:index]
        if (m := prefix.match(ln)) and m.group(1).strip()
    ]
    return body[-1] if body else None


def _match_instruction(text: str, parsed: ParsedDockerfile) -> tuple[int, int] | None:
    wanted = normalise(text)
    head, _, rest = wanted.partition(" ")
    wanted = f"{head.upper()} {rest}".strip()
    hits = [
        (i.first_line, i.last_line) for i in parsed.instructions if i.text == wanted
    ]
    return hits[0] if len(hits) == 1 else None


def _parse_binding(
    err: str, findings: list[ReproductionCheck], parsed: ParsedDockerfile
) -> InstructionRef | None:
    lines = [ln.strip() for ln in err.splitlines() if ln.strip().startswith("Error: ")]
    if not lines:
        return None
    m = _PODMAN_ERROR_RE.match(lines[-1])
    keyword = m.group(1) if m else None
    syntax = [
        f for f in findings if f.status == "failed" and f.check_id.startswith("syntax_")
    ]
    if keyword not in KEYWORDS or len(syntax) != 1 or syntax[0].location is None:
        return None
    line = syntax[0].location.lines[0]
    at_line = next((i for i in parsed.instructions if i.first_line == line), None)
    if at_line is None or at_line.keyword != keyword:
        return None
    return InstructionRef(
        kind="parse", lines=(line, line), bound_by="parser_finding_keyword"
    )


def _ci_digest_for(image: str, ci: dict[str, str]) -> str | None:
    for key, digest in ci.items():
        if (
            key == image
            or key.endswith("/" + image)
            or key.endswith("/library/" + image)
        ):
            return digest
    return None


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text)
