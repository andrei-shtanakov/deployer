"""Where R's reading of a Dockerfile and the builders' may differ.

R (``reproduce.dockerfile.parse``) and the builders (BuildKit, Buildah) read
most Dockerfiles alike, but not all. A fix built on R's reading is only sound
where the two agree, so each check here names a form on which they may
disagree and returns the refusal reason, or ``None`` when the form is absent.
Shared by the FROM transformations (``fromfix``) and the COPY envelope
(``envelope``) so the rules have one implementation.
"""

import re
from collections.abc import Iterable

from deployer.reproduce.dockerfile import Instruction, opens_heredoc

_BOM = b"\xef\xbb\xbf"
# Any CR not part of a CRLF line ending.
_LONE_CR_RE = re.compile(rb"\r(?!\n)")
# C0 controls other than tab, LF and CR; the C1 NEL; Unicode line and
# paragraph separators. Each is a line break or blank to some reader.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x0c\x0e-\x1f\x85\u2028\u2029]")
# A parser-directive-shaped comment (``# escape=``, ``# syntax=``, …).
_DIRECTIVE_RE = re.compile(r"[ \t]*#\s*[A-Za-z][A-Za-z0-9_-]*\s*=")


def strict_form_reason(dockerfile: bytes) -> str | None:
    """Refuse a Dockerfile outside the strict form R and the builders share.

    One whitelist gate, applied file-wide before any other reading check
    (Ruling O): rather than patching each divergence between R's reading
    and Buildah's/BuildKit's, only a plain form is read at all. The file
    must have no BOM, be valid UTF-8, end lines with LF or CRLF only, hold
    no control character but tab (nor NEL, U+2028, U+2029), hold no ``<<``
    anywhere, have no parser-directive-shaped comment before the first
    instruction, and put only spaces or tabs after a continuation
    backslash. Total: returns the first failing rule, or ``None``.
    """
    if dockerfile.startswith(_BOM):
        return "a byte-order mark is not modelled"
    try:
        text = dockerfile.decode("utf-8")
    except UnicodeDecodeError as error:
        return f"invalid UTF-8 at byte {error.start} is not modelled"
    if _LONE_CR_RE.search(dockerfile) is not None:
        return "a carriage return outside a CRLF line ending is not modelled"
    control = _CONTROL_RE.search(text)
    if control is not None:
        return f"control character {control.group()!r} is not modelled"
    if "<<" in text:
        return "'<<' (a possible heredoc) is not modelled"
    return _lines_reason(text.split("\n"))


def _lines_reason(lines: list[str]) -> str | None:
    """The directive and continuation rules of ``strict_form_reason``."""
    in_preamble = True
    for number, raw in enumerate(lines, start=1):
        line = raw.removesuffix("\r")
        body = line.strip(" \t")
        if in_preamble and body and not body.startswith("#"):
            in_preamble = False
        if in_preamble and _DIRECTIVE_RE.match(line) is not None:
            return f"line {number}: a parser directive is not modelled"
        content = line.rstrip()
        if content.endswith("\\") and line.rstrip(" \t") != content:
            return (
                f"line {number}: a line continuation followed by a blank other "
                "than space or tab is not modelled"
            )
    return None


def join_reason(data: bytes) -> str | None:
    """Refuse any continuation R and the builders may join differently.

    R joins continuation lines with a space; BuildKit joins them with no
    separator, so ``img:1\\<newline>extra`` reads as ``img:1extra`` and
    ``--from=ext\\<newline>ra`` as ``--from=extra`` there. Only
    continuations after a space or tab read the same in both, so every
    physical line of ``data`` is checked.

    Buildah ends a continuation only at ``\\[ \\t]*$``; R strips every
    whitespace character, so a backslash followed by another blank (a form
    feed, a vertical tab, …) continues the line in R and not in the
    builders: refused too. Lines split on ``\\n``/``\\r``/``\\r\\n`` only,
    so CRLF endings read as before.
    """
    for number, line in enumerate(data.splitlines(), start=1):
        content = line.rstrip(b" \t")
        if content.endswith(b"\\") and content[-2:-1] not in (b" ", b"\t"):
            return (
                f"line {number}: a line continuation without a preceding "
                "blank is not modelled"
            )
        text = line.decode("utf-8", errors="replace").rstrip()
        if text.endswith("\\") and not content.endswith(b"\\"):
            return (
                f"line {number}: a line continuation followed by a blank other "
                "than space or tab is not modelled"
            )
    return None


def heredoc_reason(instructions: Iterable[Instruction]) -> str | None:
    """Refuse a file in which any instruction opens a heredoc by R's rule.

    R opens a heredoc on any unquoted ``<<WORD`` in RUN/COPY/ADD arguments
    and reads the rest of the file up to the delimiter as its body; Buildah
    needs a whole shell word matching ``^(\\d*)<<``, so ``RUN true # x<<EOF``
    opens none there and the following instructions stay real. Independent
    of the instruction's family: one heredoc can hide any later instruction.
    """
    for instruction in instructions:
        if opens_heredoc(instruction.keyword, instruction.args):
            line = instruction.first_line
            return f"a heredoc at line {line} is not modelled"
    return None


def comment_reason(original: bytes) -> str | None:
    """Refuse a comment line inside an instruction's continuation.

    Its bytes could hold the token being located, so locating the token
    would not be unambiguous.
    """
    lines = original.splitlines()
    if any(line.lstrip(b" \t").startswith(b"#") for line in lines[1:]):
        return "a comment inside the instruction's continuation is not modelled"
    return None


def keyword_reason(instructions: Iterable[Instruction]) -> str | None:
    """Refuse when R's keyword split disagrees with the builders'.

    R splits keyword from arguments on the first space only, so
    ``COPY<TAB>--from=x`` reads as one keyword; the builders split on any
    blank. Such an instruction's words are not trusted.
    """
    for instruction in instructions:
        if any(char.isspace() for char in instruction.keyword):
            line = instruction.first_line
            return f"keyword at line {line} is not separated by a space"
    return None
