"""The template table, A §4.1: closed data, one row per class and backend.

Every row is backed by a committed real recording (``Row.recording``);
adding a row widens automatic admission and needs a spec change and review.

The matchers read untrusted CI or local text and never raise: each returns a
match, ``None`` (the row's diagnostic is absent) or ``"ambiguous"`` (it is
present but does not bind to exactly one instruction and object). Text is
split on ``\\n`` only and each line is stripped of whitespace at both ends
(which drops CRLF's ``\\r``, so CRLF reads like LF); each stripped line is
matched whole (``re.fullmatch``), never by substring. Digit groups are ASCII
``[0-9]`` bounded to nine digits, so no number read here can overflow
``int()``.
``evidence_lines`` are 1-based line numbers in the text as given. CI text is
expected as R produces it (``shape.job_text``: ANSI codes and timestamps
already stripped by ``forge``).

R's ``compare._error_blocks`` (private, reproduce package) is the authority on
the ``Dockerfile:<N>`` blocks; this module only locates their lines for
evidence and refuses when the two readings disagree.
"""

import re
from dataclasses import dataclass
from typing import Literal

from deployer.reproduce.compare import _error_blocks
from deployer.reproduce.dockerfile import normalise

Ambiguous = Literal["ambiguous"]
AMBIGUOUS: Ambiguous = "ambiguous"
_FIXTURES = "tests/fixtures/reproduction"


@dataclass(frozen=True)
class Row:
    """One closed row: a class, a side, a backend and its pinned recording."""

    id: str
    cls: Literal["missing_copy_source", "from_argument_count"]
    side: Literal["ci", "local"]
    backend: Literal["buildkit", "podman"]
    recording: str


ROWS: tuple[Row, ...] = (
    Row(
        "copy-missing/buildkit",
        "missing_copy_source",
        "ci",
        "buildkit",
        f"{_FIXTURES}/run-1/snapshot.json",
    ),
    Row(
        "copy-missing/podman",
        "missing_copy_source",
        "local",
        "podman",
        f"{_FIXTURES}/run-1/local.stderr",
    ),
    Row(
        "from-args/buildkit",
        "from_argument_count",
        "ci",
        "buildkit",
        f"{_FIXTURES}/run-5/snapshot.json",
    ),
    Row(
        "from-args/podman",
        "from_argument_count",
        "local",
        "podman",
        f"{_FIXTURES}/run-5/local.stderr",
    ),
)
"""Exactly the four rows of A §4.1 (recordings: A §4.1, pinned by A §8.1)."""


@dataclass(frozen=True)
class CopyMatch:
    """A ``copy-missing/*`` match. ``lines`` is the Dockerfile span (CI only),
    ``step_text`` the instruction text, ``path`` the object ``<P>`` without
    its leading ``/`` (not yet normalised: that is the binding's job)."""

    row: str
    lines: tuple[int, int] | None
    step_text: str | None
    path: str
    evidence_lines: tuple[int, ...]


@dataclass(frozen=True)
class FromMatch:
    """A ``from-args/*`` match; ``line`` is the Dockerfile line (CI only)."""

    row: str
    line: int | None
    evidence_lines: tuple[int, ...]


@dataclass(frozen=True)
class _Block:
    """A located ``Dockerfile:<N>`` block: span, ``>>>`` text, text lines."""

    span: tuple[int, int]
    text: str
    evidence: tuple[int, ...]


_CHECKSUM = "failed to calculate checksum of ref"
_BLOCK_HEAD_RE = re.compile(r"[^\s:]+:[0-9]{1,9}")
_FENCE_RE = re.compile(r"-{3,}")
_MARKED_RE = re.compile(r"\s*([0-9]{1,9}) \| >>> ?(.*)")
_HEADER_RE = re.compile(r"#(?P<k>[0-9]{1,9}) \[[^\]]*\] (?P<instr>.+)")
_STEP_BOUND_RE = re.compile(
    r"#(?P<k>[0-9]{1,9}) ERROR: failed to calculate checksum of ref (?P<ref>\S+): "
    r'"/(?P<p>[^"]+)": not found'
)
_SUMMARY_RE = re.compile(
    r"ERROR: (?:.*: )?failed to calculate checksum of ref (?P<ref>\S+): "
    r'"/(?P<p>[^"]+)": not found'
)
_PODMAN_COPY_RE = re.compile(
    r'Error: building at STEP "(?P<step>(?:COPY|ADD) [^"]*)": checking on sources '
    r'under "[^"]*": copier: stat: "/(?P<p>[^"]+)": no such file or directory'
)
_FROM_CI_RE = re.compile(
    r"ERROR: (?:.*: )?dockerfile parse error on line (?P<n>[0-9]{1,9}): "
    r"FROM requires either one or three arguments"
)
_FROM_PODMAN_RE = re.compile(r"Error: FROM requires either one argument, or three: .*")


def match_copy_ci(text: str) -> CopyMatch | None | Ambiguous:
    """``copy-missing/buildkit`` over CI job text (A §4.1): the single
    COPY/ADD block, its one step header, the one step-bound checksum line,
    and only same-ref, same-path summary repeats besides."""
    lines = _lines(text)
    if not any(_CHECKSUM in line for line in lines):
        return None
    block = _copy_block(lines)
    if block is None:
        return AMBIGUOUS
    headers = _step_headers(lines, block.text)
    if headers is None:
        return AMBIGUOUS
    step, header_lines = headers
    bound = _step_bound(lines, step)
    if bound is None:
        return AMBIGUOUS
    bound_line, ref, path = bound
    repeats = _summary_repeats(lines, bound_line, ref, path)
    if repeats is None:
        return AMBIGUOUS
    evidence = (*block.evidence, *header_lines, bound_line, *repeats)
    return CopyMatch(
        row="copy-missing/buildkit",
        lines=block.span,
        step_text=block.text,
        path=path,
        evidence_lines=tuple(sorted(set(evidence))),
    )


def match_copy_local(stderr: str) -> CopyMatch | None | Ambiguous:
    """``copy-missing/podman`` over Podman's stderr: exactly one
    ``Error: building at STEP "<COPY|ADD …>": … copier: stat: "/<P>": no such
    file or directory`` line."""
    hits = [
        (number, m)
        for number, line in enumerate(_lines(stderr), start=1)
        if (m := _PODMAN_COPY_RE.fullmatch(line))
    ]
    if not hits:
        return None
    if len(hits) != 1:
        return AMBIGUOUS
    number, m = hits[0]
    return CopyMatch(
        row="copy-missing/podman",
        lines=None,
        step_text=m.group("step"),
        path=m.group("p"),
        evidence_lines=(number,),
    )


def match_from_ci(text: str) -> FromMatch | None | Ambiguous:
    """``from-args/buildkit`` over CI job text: parse-error lines for the FROM
    argument count, naming exactly one distinct line ``<N>``."""
    hits = [
        (number, int(m.group("n")))
        for number, line in enumerate(_lines(text), start=1)
        if (m := _FROM_CI_RE.fullmatch(line))
    ]
    if not hits:
        return None
    named = {n for _, n in hits}
    if len(named) != 1:
        return AMBIGUOUS
    return FromMatch(
        row="from-args/buildkit",
        line=named.pop(),
        evidence_lines=tuple(number for number, _ in hits),
    )


def match_from_local(stdout: str, stderr: str) -> FromMatch | None | Ambiguous:
    """``from-args/podman``: one ``Error: FROM requires either one argument,
    or three: …`` stderr line and no ``STEP`` line in stdout. Evidence lines
    are numbered in ``stderr``."""
    # Fail closed: any line mentioning a STEP means the build got past the
    # parse, so this row does not apply however the step prefix is printed.
    if any("STEP " in line for line in _lines(stdout)):
        return None
    hits = [
        number
        for number, line in enumerate(_lines(stderr), start=1)
        if _FROM_PODMAN_RE.fullmatch(line)
    ]
    if not hits:
        return None
    if len(hits) != 1:
        return AMBIGUOUS
    return FromMatch(row="from-args/podman", line=None, evidence_lines=(hits[0],))


def split_lines(text: str) -> list[str]:
    """The one line rule of admission evidence: ``text`` split on ``\\n``
    only, so element ``i`` is text line ``i + 1`` whatever other break
    characters (``\\r``, form feed, ``\\u2028`` …) the text holds. The
    consumer gate (A §7) counts evidence lines with this same rule."""
    return text.split("\n")


def _lines(text: str) -> list[str]:
    """Stripped lines numbered by :func:`split_lines`."""
    return [line.strip() for line in split_lines(text)]


def _copy_block(lines: list[str]) -> _Block | None:
    """Step 1: R's single ``Dockerfile:<N>`` block, a COPY or ADD."""
    spans = _r_blocks(lines)
    if spans is None:
        return None
    blocks = _locate_blocks(lines)
    if len(spans) != 1 or [b.span for b in blocks] != spans:
        return None
    block = blocks[0]
    keyword = block.text.partition(" ")[0].upper()
    return block if keyword in ("COPY", "ADD") else None


def _r_blocks(lines: list[str]) -> list[tuple[int, int]] | None:
    """R's ``_error_blocks``, or ``None`` when its own ``int()`` fails on an
    over-long ``>>>`` line number (it reads ``\\d+`` unbounded)."""
    try:
        return _error_blocks(lines)
    except ValueError:
        return None


def _locate_blocks(lines: list[str]) -> list[_Block]:
    """Every ``<file>:<N>`` + fence block with ``>>>`` lines, as R reads them,
    with the text lines from its head to its closing fence."""
    blocks: list[_Block] = []
    for head, line in enumerate(lines):
        if not _BLOCK_HEAD_RE.fullmatch(line):
            continue
        if head + 1 >= len(lines) or not _FENCE_RE.fullmatch(lines[head + 1]):
            continue
        block = _read_block(lines, head)
        if block is not None:
            blocks.append(block)
    return blocks


def _read_block(lines: list[str], head: int) -> _Block | None:
    """The block starting at ``head``: its ``>>>`` rows up to the next fence."""
    marked: list[tuple[int, str]] = []
    end = len(lines) - 1
    for index in range(head + 2, len(lines)):
        if lines[index].startswith("---"):
            end = index
            break
        m = _MARKED_RE.fullmatch(lines[index])
        if m:
            marked.append((int(m.group(1)), m.group(2)))
    if not marked:
        return None
    numbers = [n for n, _ in marked]
    return _Block(
        span=(min(numbers), max(numbers)),
        text=_instruction_key("\n".join(t for _, t in marked)),
        evidence=tuple(range(head + 1, end + 2)),
    )


def _step_headers(
    lines: list[str], instruction: str
) -> tuple[str, tuple[int, ...]] | None:
    """Step 2: the one step number whose header carries ``instruction``.
    BuildKit may reprint a step's header; one number may appear repeatedly."""
    hits = [
        (m.group("k"), number)
        for number, line in enumerate(lines, start=1)
        if (m := _HEADER_RE.fullmatch(line))
        and _instruction_key(m.group("instr")) == instruction
    ]
    steps = {k for k, _ in hits}
    if len(steps) != 1:
        return None
    return steps.pop(), tuple(number for _, number in hits)


def _step_bound(lines: list[str], step: str) -> tuple[int, str, str] | None:
    """Step 3: exactly one ``#<k> ERROR: failed to calculate checksum …``
    line for that step: its line number, ``<ref>`` and ``<P>``."""
    hits = [
        (number, m.group("ref"), m.group("p"))
        for number, line in enumerate(lines, start=1)
        if (m := _STEP_BOUND_RE.fullmatch(line)) and m.group("k") == step
    ]
    return hits[0] if len(hits) == 1 else None


def _summary_repeats(
    lines: list[str], bound_line: int, ref: str, path: str
) -> tuple[int, ...] | None:
    """Step 4: every other checksum line is an unprefixed ``ERROR:`` summary
    with the same ``<ref>`` and ``<P>``; anything else is another candidate."""
    repeats: list[int] = []
    for number, line in enumerate(lines, start=1):
        if number == bound_line or _CHECKSUM not in line:
            continue
        m = _SUMMARY_RE.fullmatch(line)
        if m is None or (m.group("ref"), m.group("p")) != (ref, path):
            return None
        repeats.append(number)
    return tuple(repeats)


def _instruction_key(text: str) -> str:
    """Normalised instruction text with an upper-case keyword, as R compares."""
    head, _, rest = normalise(text).partition(" ")
    return f"{head.upper()} {rest}".strip()
