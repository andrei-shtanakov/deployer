"""Closed tables of local and CI "passed" templates (F §6.3, §7.3, §9).

Four rows: COPY/ADD and FROM, on the local side (Podman) and the CI side
(BuildKit). Every matcher is a **hypothesis** until a real recording backs its
row: no passing corrected build has been recorded, so the shapes read here are
the spec's wording, not what Podman or BuildKit are known to print. A row
ships **disabled** (``recording=None``) and is enabled only together with the
test that checks it against its committed recording (§9). A disabled row
yields ``not_enabled``.

Tests reach the happy path through a module-private registry (§10 "Test
seam"); the only code that touches it is a context manager in the test tree.
No CLI flag, environment variable or configuration reads it.

The matchers read untrusted text and never raise. Text is split with
:func:`~deployer.admission.templates.split_lines` (``\\n`` only; a final
newline opens no line), each line is stripped at both ends (CRLF reads like
LF) and matched whole. A line longer than :data:`MAX_LINE` is not read at all:
the outcome is ``unknown_format``. Digit groups are ASCII and bounded to nine
digits; no pattern nests quantifiers. ``Outcome.lines`` are 1-based.

FROM evidence is file-wide ("the Dockerfile was parsed"): it binds no step line
to the corrected FROM text. The optional image-pull record needs that binding,
and both builders print the pulled reference normalised (``docker.io/library/
…``), so no pull line binds unambiguously before recordings show the forms:
``Outcome.image_pull`` is always ``None`` for now, which never affects the parse
evidence (§6.3).
"""

import re
from dataclasses import dataclass
from typing import Literal

from deployer.admission.templates import split_lines

Side = Literal["local", "ci"]
Backend = Literal["podman", "buildkit"]
Kind = Literal["copy", "from"]
Evidence = Literal[
    "passed", "not_confirmed", "binding_ambiguous", "not_enabled", "unknown_format"
]

MAX_LINE = 16_384
"""Longest line a matcher reads; a longer one makes the text ``unknown_format``."""


@dataclass(frozen=True)
class Row:
    """One closed row: a side, a backend, a kind and its backing recording.

    ``recording`` is the committed recording the row is checked against;
    ``None`` means the row is a hypothesis and is disabled."""

    id: str
    side: Side
    backend: Backend
    kind: Kind
    recording: str | None


ROWS: tuple[Row, ...] = (
    Row("copy-passed/podman", "local", "podman", "copy", None),
    Row("from-parsed/podman", "local", "podman", "from", None),
    Row("copy-passed/buildkit", "ci", "buildkit", "copy", None),
    Row("from-parsed/buildkit", "ci", "buildkit", "from", None),
)
"""The four rows of §6.3/§7.3 — all hypotheses, none backed by a recording."""

_TEST_REGISTRY: list[Row] = []
# Test-only: rows injected by the test seam. Never written from ``src/``.


@dataclass(frozen=True)
class Outcome:
    """A matcher's verdict: ``lines`` are the evidence lines (1-based, in the
    stream ``detail`` names when it is not stdout / the CI log), ``detail``
    the reason when not ``passed``, ``image_pull`` the FROM image-pull
    record when one binds unambiguously (never today: see the module doc)."""

    evidence: Evidence
    lines: tuple[int, ...]
    detail: str | None
    image_pull: str | None


_NOT_ENABLED = Outcome("not_enabled", (), "templates not enabled", None)


def enabled_rows() -> tuple[Row, ...]:
    """Production rows backed by a recording, plus rows injected by tests."""
    recorded = tuple(row for row in ROWS if row.recording is not None)
    return recorded + tuple(_TEST_REGISTRY)


def match_local(kind: Kind, corrected_text: str, stdout: str, stderr: str) -> Outcome:
    """Local (Podman) positive evidence for the corrected instruction (§6.3)."""
    if not _enabled("local", kind):
        return _NOT_ENABLED
    if _overlong(stdout) or _overlong(stderr) or len(corrected_text) > MAX_LINE:
        return _outcome("unknown_format", (), "a line exceeds the read bound")
    out, err = _lines(stdout), _lines(stderr)
    if kind == "from":
        return _podman_from(out, err)
    if not _bindable(corrected_text):
        return _outcome("binding_ambiguous", (), "corrected text is not one line")
    return _podman_copy(corrected_text, out, err)


def match_ci(kind: Kind, corrected_text: str, log: str) -> Outcome:
    """CI (BuildKit plain progress) positive evidence (§7.3)."""
    if not _enabled("ci", kind):
        return _NOT_ENABLED
    if _overlong(log) or len(corrected_text) > MAX_LINE:
        return _outcome("unknown_format", (), "a line exceeds the read bound")
    lines = _lines(log)
    if not any(_BK_ANY_RE.fullmatch(line) for line in lines):
        return _outcome("unknown_format", (), "no BuildKit progress lines")
    if kind == "from":
        return _buildkit_from(lines)
    if not _bindable(corrected_text):
        return _outcome("binding_ambiguous", (), "corrected text is not one line")
    return _buildkit_copy(corrected_text, lines)


_STEP_RE = re.compile(r"STEP (?P<k>[0-9]{1,9})/(?P<n>[0-9]{1,9}): (?P<text>.*)")
_COMPLETION_RE = re.compile(r"Successfully tagged \S+")
_PODMAN_FROM_ARGS_RE = re.compile(
    r"Error: FROM requires either one argument, or three: .*"
)
_BK_ANY_RE = re.compile(r"#[0-9]{1,9} .*")
_BK_HEADER_RE = re.compile(r"#(?P<k>[0-9]{1,9}) \[(?P<bracket>[^\]]*)\] (?P<text>.*)")
_BK_STAGE_RE = re.compile(r"(?:[^\s\]]+ )?[0-9]{1,9}/[0-9]{1,9}")
_BK_DONE_RE = re.compile(r"#(?P<k>[0-9]{1,9}) DONE(?: [0-9]{1,9}(?:\.[0-9]{1,9})?s)?")
_BK_CACHED_RE = re.compile(r"#(?P<k>[0-9]{1,9}) CACHED")
_BK_ERROR_RE = re.compile(r"#(?P<k>[0-9]{1,9}) (?:ERROR|CANCELED)(?:[: ].*)?")
_PARSE_ERROR = "parse error"


def _podman_copy(text: str, out: list[str], err: list[str]) -> Outcome:
    """COPY/ADD: the ``STEP k/n: <text>`` line exactly once, then the next
    ``STEP (k+1)/n`` or a completion line, with no error in between and no
    Podman error bound to the step itself."""
    bound_error = f'Error: building at STEP "{text}"'
    errors = [n for n, line in enumerate(err, 1) if line.startswith(bound_error)]
    if errors:
        return _outcome("not_confirmed", tuple(errors), "stderr: step-bound error")
    hits = [
        (n, m)
        for n, line in enumerate(out, 1)
        if (m := _STEP_RE.fullmatch(line)) and m.group("text") == text
    ]
    if not hits:
        return _outcome("not_confirmed", (), "corrected step line absent")
    if len(hits) > 1:
        return _outcome(
            "binding_ambiguous", tuple(n for n, _ in hits), "step line repeated"
        )
    number, step = hits[0]
    return _after_step(out, number, int(step.group("k")), int(step.group("n")))


def _after_step(out: list[str], number: int, k: int, n: int) -> Outcome:
    """The first marker after the step line decides: an error, the same
    sequence's next step, a completion — anything else is ambiguous."""
    for later, line in enumerate(out[number:], number + 1):
        if line.lower().startswith("error"):
            return _outcome("not_confirmed", (number, later), "error after step")
        if _COMPLETION_RE.fullmatch(line):
            return _outcome("passed", (number, later), None)
        step = _STEP_RE.fullmatch(line)
        if step is None:
            continue
        if int(step.group("k")) == k + 1 and int(step.group("n")) == n:
            return _outcome("passed", (number, later), None)
        return _outcome(
            "binding_ambiguous", (number, later), "next step not of this sequence"
        )
    return _outcome("binding_ambiguous", (number,), "no next step or completion")


def _podman_from(out: list[str], err: list[str]) -> Outcome:
    """FROM: file-wide — a build-stage step exists and no parse error in
    either stream. No step line is bound to the corrected FROM text."""
    for stream, lines in (("stdout", out), ("stderr", err)):
        parse = [n for n, line in enumerate(lines, 1) if _is_podman_parse_error(line)]
        if parse:
            return _outcome("not_confirmed", tuple(parse), f"{stream}: parse error")
    steps = [n for n, line in enumerate(out, 1) if _STEP_RE.fullmatch(line)]
    if not steps:
        return _outcome("not_confirmed", (), "no build-stage step")
    return _outcome("passed", (steps[0],), None)


def _buildkit_copy(text: str, lines: list[str]) -> Outcome:
    """COPY/ADD: exactly one stage header ``#k [...] <text>`` and ``#k DONE``;
    ``#k CACHED`` or an error is not confirmed; a missing or repeated ``k``
    is ambiguous."""
    headers = [
        (n, m)
        for n, line in enumerate(lines, 1)
        if (m := _BK_HEADER_RE.fullmatch(line))
    ]
    hits = [(n, m) for n, m in headers if _is_stage(m) and m.group("text") == text]
    if not hits:
        return _outcome("not_confirmed", (), "corrected step header absent")
    if len(hits) > 1:
        return _outcome(
            "binding_ambiguous", tuple(n for n, _ in hits), "step header repeated"
        )
    number, header = hits[0]
    k = header.group("k")
    reused = [n for n, m in headers if m.group("k") == k and n != number]
    if reused:
        return _outcome("binding_ambiguous", (number, *reused), f"#{k} repeated")
    return _buildkit_result(lines, number, k)


def _buildkit_result(lines: list[str], number: int, k: str) -> Outcome:
    """The result lines of step ``k``: an error or ``CACHED`` → not
    confirmed; exactly one ``DONE`` → passed; none or several → ambiguous."""
    done: list[int] = []
    cached: list[int] = []
    errors: list[int] = []
    for n, line in enumerate(lines, 1):
        for regex, bucket in (
            (_BK_DONE_RE, done),
            (_BK_CACHED_RE, cached),
            (_BK_ERROR_RE, errors),
        ):
            m = regex.fullmatch(line)
            if m and m.group("k") == k:
                bucket.append(n)
    if errors:
        return _outcome("not_confirmed", (number, *errors), f"#{k} failed")
    if cached:
        return _outcome("not_confirmed", (number, *cached), f"#{k} CACHED")
    if len(done) != 1:
        return _outcome("binding_ambiguous", (number, *done), f"#{k} DONE not once")
    return _outcome("passed", (number, done[0]), None)


def _buildkit_from(lines: list[str]) -> Outcome:
    """FROM: file-wide — a build-stage header exists and no ``dockerfile
    parse error``. Definition, context and frontend loading are not stages."""
    parse = [n for n, line in enumerate(lines, 1) if _is_buildkit_parse_error(line)]
    if parse:
        return _outcome("not_confirmed", tuple(parse), "dockerfile parse error")
    stages = [
        n
        for n, line in enumerate(lines, 1)
        if (m := _BK_HEADER_RE.fullmatch(line)) and _is_stage(m)
    ]
    if not stages:
        return _outcome("not_confirmed", (), "no build-stage header")
    return _outcome("passed", (stages[0],), None)


def _enabled(side: Side, kind: Kind) -> bool:
    """Whether an enabled row covers this side and kind."""
    return any(row.side == side and row.kind == kind for row in enabled_rows())


def _lines(text: str) -> list[str]:
    """Stripped lines numbered by :func:`split_lines`."""
    return [line.strip() for line in split_lines(text)]


def _overlong(text: str) -> bool:
    """Whether any line of ``text`` exceeds :data:`MAX_LINE`."""
    return any(len(line) > MAX_LINE for line in split_lines(text))


def _bindable(text: str) -> bool:
    """A corrected text can bind to a line only if it is one stripped,
    non-empty line."""
    return bool(text) and text == text.strip() and "\n" not in text


def _is_stage(header: re.Match[str]) -> bool:
    """A header's bracket names a build stage step (``[name i/n]``/``[i/n]``)."""
    return _BK_STAGE_RE.fullmatch(header.group("bracket")) is not None


def _is_podman_parse_error(line: str) -> bool:
    """Podman parse-error hypothesis: the FROM argument-count error or any line
    naming a parse error (broad on purpose: it can only refuse)."""
    if _PODMAN_FROM_ARGS_RE.fullmatch(line):
        return True
    return _PARSE_ERROR in line.lower()


def _is_buildkit_parse_error(line: str) -> bool:
    """BuildKit's ``dockerfile parse error`` anywhere on the line."""
    return f"dockerfile {_PARSE_ERROR}" in line.lower()


def _outcome(evidence: Evidence, lines: tuple[int, ...], detail: str | None) -> Outcome:
    """An outcome with no image-pull record (see the module doc)."""
    return Outcome(evidence, lines, detail, None)
