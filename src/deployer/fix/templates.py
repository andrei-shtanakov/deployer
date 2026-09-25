"""Closed tables of local and CI "passed" templates (F §6.3, §7.3, §9).

Four rows: COPY/ADD and FROM, on the local side (Podman) and the CI side
(BuildKit). A row is enabled only together with the test that checks it
against its committed recording (§9): ``Row.recording`` names it, and a row
with ``recording=None`` is disabled and yields ``not_enabled``. The two local
rows are backed by the L-recordings (``tests/fixtures/recordings/local``,
replayed by ``tests/fix/test_local_recordings.py``); the two CI rows are still
hypotheses and stay disabled.

Tests reach a disabled row through a module-private registry (§10 "Test
seam"); the only code that touches it is a context manager in the test tree.
No CLI flag, environment variable or configuration reads it.

The matchers read untrusted text and never raise. Text is split with
:func:`~deployer.admission.templates.split_lines` (``\\n`` only; a final
newline opens no line), each line is stripped at both ends (CRLF reads like
LF) and matched whole. A line longer than :data:`MAX_LINE` is not read at all:
the outcome is ``unknown_format``. Digit groups are ASCII and bounded to nine
digits; no pattern nests quantifiers. ``Outcome.lines`` are 1-based.

Local (Podman) evidence binds the corrected instruction's **own** step line
(§6.3). Podman prefixes the step lines of a multi-stage file with ``[i/n] ``;
it prints a stage's FROM line only once Buildah's FROM check for that stage
has passed (a COPY/ADD line is printed before its step runs), and it skips a
stage nothing depends on, printing nothing for it
(``docs/fix-buildah-from-parse.md``). The FROM line is rebuilt for display
(``$VAR`` expanded, quotes removed); other lines print as written. So the
corrected text must provably occur once among the corrected Dockerfile's
instructions — an identical instruction in a skipped stage would otherwise
lend its absence to a false binding: the whole file must pass the fix-wide
reading checks and every instruction of the kind's family must be in the
modelled form, else ``binding_ambiguous``. A completion line binds only when
it names the build's own tag.

BuildKit FROM evidence is file-wide ("the Dockerfile was parsed"): it binds no
step line to the corrected FROM text. The optional image-pull record needs
that binding, and both builders print the pulled reference normalised
(``docker.io/library/…``), so no pull line binds unambiguously before
recordings show the forms: ``Outcome.image_pull`` is always ``None`` for now,
which never affects the parse evidence (§6.3).

A text matcher cannot tell a builder's own ``STEP``/``#k`` line from the same
text echoed by a ``RUN`` step's output: a build that prints a forged
``STEP k/n: <corrected text>`` line is indistinguishable here. This is a
limit of the text reading, weighed when the recordings backed the rows.

A CI job's text may hold more than one build. Both BuildKit matchers refuse
such a log as ``binding_ambiguous`` when they can see it: ``[internal] load
build definition`` more than once, or ``#k`` numbering restarting (a new, never
seen ``#k`` lower than one already seen — within one build BuildKit numbers new
vertices in increasing order).
"""

import re
from dataclasses import dataclass
from typing import Literal

from deployer.admission.prepare import _as_r_reads
from deployer.admission.templates import split_lines
from deployer.fix.reading import (
    comment_reason,
    heredoc_reason,
    join_reason,
    keyword_reason,
    strict_form_reason,
)
from deployer.reproduce.dockerfile import (
    Instruction,
    ParsedDockerfile,
    opens_heredoc,
    parse,
    unread_reason,
)

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

    ``recording`` is the committed recording directory the row is checked
    against; ``None`` means the row is a hypothesis and is disabled."""

    id: str
    side: Side
    backend: Backend
    kind: Kind
    recording: str | None


LOCAL_RECORDING = "tests/fixtures/recordings/local"
"""The L-recordings backing the two local rows (repository-relative)."""

ROWS: tuple[Row, ...] = (
    Row("copy-passed/podman", "local", "podman", "copy", LOCAL_RECORDING),
    Row("from-parsed/podman", "local", "podman", "from", LOCAL_RECORDING),
    Row("copy-passed/buildkit", "ci", "buildkit", "copy", None),
    Row("from-parsed/buildkit", "ci", "buildkit", "from", None),
)
"""The four rows of §6.3/§7.3: the local ones backed by the L-recordings, the
CI ones still hypotheses."""

# TODO: spec §7.3 says the FROM image-pull result "is recorded when visible";
# ``Outcome.image_pull`` deliberately stays ``None`` until recordings show a
# pull line that binds unambiguously to the corrected FROM (see module doc).

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


def match_local(
    kind: Kind,
    corrected_text: str,
    stdout: str,
    stderr: str,
    *,
    dockerfile: bytes,
    tag: str,
) -> Outcome:
    """Local (Podman) positive evidence for the corrected instruction (§6.3).

    ``dockerfile`` holds the corrected Dockerfile's bytes, the ones built;
    ``tag`` is the tag the build was given (a completion line binds only
    when it names it)."""
    if not _enabled("local", kind):
        return _NOT_ENABLED
    strict = strict_form_reason(dockerfile)
    if strict is not None:
        return _outcome("binding_ambiguous", (), f"Dockerfile: {strict}")
    if _overlong(stdout) or _overlong(stderr) or len(corrected_text) > MAX_LINE:
        return _outcome("unknown_format", (), "a line exceeds the read bound")
    if not _bindable(corrected_text):
        return _outcome("binding_ambiguous", (), "corrected text is not one line")
    unique = _unique_in_dockerfile(kind, corrected_text, dockerfile)
    if unique is not None:
        return unique
    out, err = _lines(stdout), _lines(stderr)
    if kind == "from":
        return _podman_from(corrected_text, out, err)
    return _podman_copy(corrected_text, out, err, _tag_names(tag))


def match_ci(kind: Kind, corrected_text: str, log: str) -> Outcome:
    """CI (BuildKit plain progress) positive evidence (§7.3)."""
    if not _enabled("ci", kind):
        return _NOT_ENABLED
    if _overlong(log) or len(corrected_text) > MAX_LINE:
        return _outcome("unknown_format", (), "a line exceeds the read bound")
    lines = _lines(log)
    if not any(_BK_ANY_RE.fullmatch(line) for line in lines):
        return _outcome("unknown_format", (), "no BuildKit progress lines")
    several = _several_builds(lines)
    if several is not None:
        return several
    if kind == "from":
        return _buildkit_from(lines)
    if not _bindable(corrected_text):
        return _outcome("binding_ambiguous", (), "corrected text is not one line")
    return _buildkit_copy(corrected_text, lines)


_PREFIX = r"(?:\[(?P<stage>[0-9]{1,9}/[0-9]{1,9})\] )?"
_STEP_RE = re.compile(
    _PREFIX + r"STEP (?P<k>[0-9]{1,9})/(?P<n>[0-9]{1,9}): (?P<text>.*)"
)
_COMMIT_RE = re.compile(_PREFIX + r"COMMIT (?P<tag>\S+)")
_TAGGED_RE = re.compile(r"Successfully tagged (?P<tag>\S+)")
_PODMAN_FROM_ARGS = "FROM requires either one argument, or three"
_BK_ANY_RE = re.compile(r"#(?P<k>[0-9]{1,9}) .*")
_BK_DEFINITION_RE = re.compile(r"#[0-9]{1,9} \[internal\] load build definition\b.*")
_BK_HEADER_RE = re.compile(r"#(?P<k>[0-9]{1,9}) \[(?P<bracket>[^\]]*)\] (?P<text>.*)")
_BK_STAGE_RE = re.compile(r"(?:[^\s\]]+ )?[0-9]{1,9}/[0-9]{1,9}")
_BK_DONE_RE = re.compile(r"#(?P<k>[0-9]{1,9}) DONE(?: [0-9]{1,9}(?:\.[0-9]{1,9})?s)?")
_BK_CACHED_RE = re.compile(r"#(?P<k>[0-9]{1,9}) CACHED")
_BK_ERROR_RE = re.compile(r"#(?P<k>[0-9]{1,9}) (?:ERROR|CANCELED)(?:[: ].*)?")
_PARSE_ERROR = "parse error"


def _unique_in_dockerfile(kind: Kind, text: str, dockerfile: bytes) -> Outcome | None:
    """``binding_ambiguous`` unless the corrected text is provably exactly one
    of the corrected Dockerfile's instructions as Podman prints them. Decided
    before any output is read: an identical instruction in a skipped stage
    prints nothing and must not make the one printed line look unique.

    Instructions are read as R reads them (``_as_r_reads``,
    ``dockerfile.parse``), which is sound only where the builders read alike:
    the whole file must pass the fix-wide reading checks (``fix.reading``),
    and every instruction of the kind's family must be in the modelled form
    (``_family_reason``). FROMs are compared by the line Podman rebuilds for
    them (``_from_display``); COPY/ADD by ``Instruction.text``."""
    parsed = parse(_as_r_reads(dockerfile))
    wanted = parse(text).instructions
    if len(wanted) != 1:
        return _outcome("binding_ambiguous", (), "corrected text is not one line")
    reason = _reading_reason(dockerfile, parsed) or _family_reason(kind, parsed)
    if reason is not None:
        return _outcome("binding_ambiguous", (), f"Dockerfile: {reason}")
    key = _from_display if kind == "from" else _instruction_text
    at = [i.first_line for i in parsed.instructions if key(i) == key(wanted[0])]
    if not at:
        return _outcome(
            "binding_ambiguous", (), "Dockerfile: corrected instruction absent"
        )
    if len(at) > 1:
        where = ", ".join(str(line) for line in at)
        return _outcome(
            "binding_ambiguous",
            (),
            f"Dockerfile: corrected instruction repeated at lines {where}",
        )
    return None


def _reading_reason(dockerfile: bytes, parsed: ParsedDockerfile) -> str | None:
    """Where R's reading of the whole file may differ from the builders'
    (the checks ``envelope``/``fromfix`` apply to the bound instruction)."""
    if unread_reason(parsed) is not None:
        return "split not read"
    lines = dockerfile.splitlines()
    spans = (
        b"\n".join(lines[i.first_line - 1 : i.last_line]) for i in parsed.instructions
    )
    return (
        join_reason(dockerfile)
        or keyword_reason(parsed.instructions)
        or heredoc_reason(parsed.instructions)
        or next(filter(None, map(comment_reason, spans)), None)
    )


_FAMILY: dict[Kind, frozenset[str]] = {
    "from": frozenset({"FROM"}),
    "copy": frozenset({"COPY", "ADD"}),
}
_UNRESOLVED = frozenset("$\"'\\")
"""Characters that make an instruction depend on expansion or quoting: Podman
prints FROM rebuilt after ``ProcessWord`` (quotes removed, ``$VAR``
expanded), so its printed line is not provably the written one."""


def _family_reason(kind: Kind, parsed: ParsedDockerfile) -> str | None:
    """Why an instruction of ``kind``'s family is not in the modelled form
    (uniqueness is then unprovable), or ``None``."""
    family = _FAMILY.get(kind, frozenset())
    for instruction in parsed.instructions:
        if instruction.keyword not in family:
            continue
        line, args = instruction.first_line, instruction.args
        if _UNRESOLVED & set(args):
            return (
                f"{instruction.keyword} at line {line} holds a substitution, "
                "quote or escape; uniqueness is unprovable"
            )
        if kind == "copy" and (
            opens_heredoc(instruction.keyword, args) or _body(args).startswith("[")
        ):
            return (
                f"{instruction.keyword} at line {line} is a heredoc or JSON "
                "form; uniqueness is unprovable"
            )
        if kind == "from" and _from_name(args).isdigit():
            return f"FROM at line {line} has a numeric stage name Podman drops"
    return None


def _body(args: str) -> str:
    """COPY/ADD arguments after the leading ``--flag`` words."""
    words = args.split()
    while words and words[0].startswith("--"):
        words.pop(0)
    return " ".join(words)


def _from_name(args: str) -> str:
    """A FROM's ``AS`` stage name, or ``""``."""
    rest = [word for word in args.split() if not word.startswith("--")]
    if len(rest) == 3 and rest[1].upper() == "AS":
        return rest[2]
    return ""


def _from_display(instruction: Instruction) -> str:
    """The FROM line as Podman rebuilds it for display: flags, the base, and
    `` AS <name>`` — ``AS`` uppercased, the name lowercased (refuse-only: two
    FROMs differing only there compare equal)."""
    words = instruction.args.split()
    flags = [word for word in words if word.startswith("--")]
    rest = [word for word in words if not word.startswith("--")]
    name = _from_name(instruction.args)
    shown = [*flags, *rest[:1], "AS", name.lower()] if name else [*flags, *rest]
    return " ".join(["FROM", *shown])


def _instruction_text(instruction: Instruction) -> str:
    """``Instruction.text``, R's normalised reading."""
    return instruction.text


def _podman_copy(
    text: str, out: list[str], err: list[str], tags: frozenset[str]
) -> Outcome:
    """COPY/ADD: the corrected step line (``[i/n] `` prefix or none) exactly
    once, then the same stage's ``STEP k+1/m``, or — after the final stage's
    last step — a completion naming the build's own tag. An error bound to
    the step, or an error line before the next marker, is not confirmed."""
    bound = _step_bound_error(text, err)
    if bound is not None:
        return bound
    found = _step_line(text, out)
    if isinstance(found, Outcome):
        return found
    number, step = found
    return _after_step(out, number, step, tags)


def _step_bound_error(text: str, err: list[str]) -> Outcome | None:
    """``not_confirmed`` when stderr holds Podman's error bound to the
    corrected step (``Error: building at STEP "<text>"``)."""
    bound_error = f'Error: building at STEP "{text}"'
    errors = [n for n, line in enumerate(err, 1) if line.startswith(bound_error)]
    if errors:
        return _outcome("not_confirmed", tuple(errors), "stderr: step-bound error")
    return None


def _step_line(text: str, out: list[str]) -> tuple[int, re.Match[str]] | Outcome:
    """The one step line carrying ``text`` (any stage), or the outcome when
    it is absent (``not_confirmed``) or repeated (``binding_ambiguous``)."""
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
    return hits[0]


def _after_step(
    out: list[str], number: int, step: re.Match[str], tags: frozenset[str]
) -> Outcome:
    """The first marker after the step line decides: an error, the same
    stage's next step, a completion bound to the build's own tag after the
    final stage's last step — anything else is ambiguous."""
    stage, k, m = step.group("stage"), int(step.group("k")), int(step.group("n"))
    for later, line in enumerate(out[number:], number + 1):
        lines = (number, later)
        if line.lower().startswith("error"):
            return _outcome("not_confirmed", lines, "error after step")
        nxt = _STEP_RE.fullmatch(line)
        if nxt is not None:
            same = nxt.group("stage") == stage and int(nxt.group("n")) == m
            if same and int(nxt.group("k")) == k + 1:
                return _outcome("passed", lines, None)
            if k == m and not _final(stage):
                detail = "next stage start does not prove it"
                return _outcome("binding_ambiguous", lines, detail)
            return _outcome("binding_ambiguous", lines, "next step not of this stage")
        done = _completion(line, stage)
        if done is None:
            continue
        if k != m or not _final(stage):
            return _outcome("binding_ambiguous", lines, "completion before step m")
        if done not in tags:
            return _outcome("binding_ambiguous", lines, "completion of another tag")
        return _outcome("passed", lines, None)
    return _outcome("binding_ambiguous", (number,), "no next step or completion")


def _completion(line: str, stage: str | None) -> str | None:
    """The tag a completion line names (``[i/n] COMMIT <tag>`` of the same
    stage prefix, or ``Successfully tagged <tag>``), else ``None``."""
    commit = _COMMIT_RE.fullmatch(line)
    if commit is not None:
        return commit.group("tag") if commit.group("stage") == stage else ""
    tagged = _TAGGED_RE.fullmatch(line)
    return None if tagged is None else tagged.group("tag")


def _final(stage: str | None) -> bool:
    """Whether a step's ``[i/n]`` prefix names the final stage (no prefix: a
    single-stage file, whose only stage is final)."""
    if stage is None:
        return True
    index, _, count = stage.partition("/")
    return index == count


def _tag_names(tag: str) -> frozenset[str]:
    """The names a completion line may give the build's own tag: the tag
    itself and, when it has no explicit tag part, ``<tag>:latest`` (as Podman
    prints ``Successfully tagged``). An empty tag binds nothing."""
    if not tag:
        return frozenset()
    last = tag.rsplit("/", 1)[-1]
    if ":" in last or "@" in tag:
        return frozenset({tag})
    return frozenset({tag, f"{tag}:latest"})


def _podman_from(text: str, out: list[str], err: list[str]) -> Outcome:
    """FROM: no Podman parse error in either stream, no error bound to the
    step, and the corrected FROM's own step line ``STEP 1/m: <text>``
    (prefixed or not) exactly once. Buildah prints that line only after the
    stage's FROM check passed, so it needs no next marker; a skipped stage
    prints none (``docs/fix-buildah-from-parse.md``)."""
    for stream, lines in (("stdout", out), ("stderr", err)):
        parse_errors = [
            n for n, line in enumerate(lines, 1) if _is_podman_parse_error(line)
        ]
        if parse_errors:
            detail = f"{stream}: parse error"
            return _outcome("not_confirmed", tuple(parse_errors), detail)
    bound = _step_bound_error(text, err)
    if bound is not None:
        return bound
    found = _step_line(text, out)
    if isinstance(found, Outcome):
        return found
    number, step = found
    if int(step.group("k")) != 1:
        return _outcome("binding_ambiguous", (number,), "FROM step is not step 1")
    return _outcome("passed", (number,), None)


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
    """The result lines of step ``k`` after its header: an error or ``CACHED`` → not
    confirmed; exactly one ``DONE`` → passed; none or several → ambiguous."""
    done: list[int] = []
    cached: list[int] = []
    errors: list[int] = []
    for n, line in enumerate(lines[number:], number + 1):
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


def _several_builds(lines: list[str]) -> Outcome | None:
    """``binding_ambiguous`` when the log visibly holds more than one build:
    the build definition loaded more than once, or ``#k`` numbering restarting
    (a new ``#k`` lower than the highest seen). ``None`` otherwise."""
    loads = [n for n, line in enumerate(lines, 1) if _BK_DEFINITION_RE.fullmatch(line)]
    if len(loads) > 1:
        return _outcome("binding_ambiguous", tuple(loads), "several builds in log")
    seen: set[int] = set()
    highest = 0
    for n, line in enumerate(lines, 1):
        m = _BK_ANY_RE.fullmatch(line)
        if m is None:
            continue
        k = int(m.group("k"))
        if k not in seen and k < highest:
            return _outcome("binding_ambiguous", (n,), f"#{k} numbering restarts")
        seen.add(k)
        highest = max(highest, k)
    return None


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
    """Podman parse-error hypothesis: the FROM argument-count message anywhere
    (bare or wrapped in ``Error: building at STEP "FROM …": …``) or any line
    naming a parse error (broad on purpose: it can only refuse)."""
    if _PODMAN_FROM_ARGS in line:
        return True
    return _PARSE_ERROR in line.lower()


def _is_buildkit_parse_error(line: str) -> bool:
    """BuildKit's ``dockerfile parse error`` anywhere on the line."""
    return f"dockerfile {_PARSE_ERROR}" in line.lower()


def _outcome(evidence: Evidence, lines: tuple[int, ...], detail: str | None) -> Outcome:
    """An outcome with no image-pull record (see the module doc)."""
    return Outcome(evidence, lines, detail, None)
