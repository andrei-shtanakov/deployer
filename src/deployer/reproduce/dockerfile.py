"""A Dockerfile reader with line spans and a closed list of syntax checks (§3.1).

Not a frontend: it checks exactly four things and says so. "No finding" means
"no finding among checks 1-4", never "valid syntax".
"""

import re
from dataclasses import dataclass

from deployer.reproduce.model import ReproductionCheck

KEYWORDS = frozenset(
    {
        "ADD",
        "ARG",
        "CMD",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "FROM",
        "HEALTHCHECK",
        "LABEL",
        "MAINTAINER",
        "ONBUILD",
        "RUN",
        "SHELL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
)
_DIRECTIVE_RE = re.compile(r"^#\s*(syntax|escape|check)\s*=\s*(\S+)\s*$", re.IGNORECASE)
_HEREDOC_RE = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")
# Heredocs exist only on these instructions, and only outside quotes: a `<<`
# inside a LABEL value or a quoted RUN argument opens nothing.
_HEREDOC_KEYWORDS = frozenset({"RUN", "COPY", "ADD"})
_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
_WS_RE = re.compile(r"\s+")
_CHECK_IDS = (
    "syntax_first_from",
    "syntax_from_args",
    "syntax_keyword",
    "syntax_continuation",
)


@dataclass(frozen=True)
class Instruction:
    """One instruction and the 1-based source lines it spans."""

    keyword: str
    args: str
    first_line: int
    last_line: int

    @property
    def text(self) -> str:
        """``KEYWORD args`` with whitespace collapsed, for matching."""
        return normalise(f"{self.keyword} {self.args}")


@dataclass(frozen=True)
class ParsedDockerfile:
    """Instructions with spans, the directives read, and a dangling ``\\``."""

    instructions: list[Instruction]
    syntax_directive: str | None
    escape_directive: str | None
    dangling_continuation: bool


def normalise(text: str) -> str:
    """Drop backslash-newline continuations and collapse whitespace."""
    return _WS_RE.sub(" ", text.replace("\\\r\n", " ").replace("\\\n", " ")).strip()


def parse(text: str) -> ParsedDockerfile:
    """Split into instructions with spans; CRLF reads exactly like LF."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    directives: dict[str, str] = {}
    instructions: list[Instruction] = []
    in_directives = True
    buffer: list[str] = []
    first = 0
    heredoc: tuple[str, bool] | None = None
    heredoc_owner_index: int | None = None
    last_content = 0
    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if heredoc is not None and heredoc_owner_index is not None:
            delimiter, dash = heredoc
            body = raw.lstrip("\t") if dash else raw
            owner = instructions[heredoc_owner_index]
            instructions[heredoc_owner_index] = Instruction(
                owner.keyword, owner.args, owner.first_line, number
            )
            if body == delimiter:
                heredoc, heredoc_owner_index = None, None
            continue
        if in_directives:
            match = _DIRECTIVE_RE.match(stripped)
            if match and not buffer:
                directives.setdefault(match.group(1).lower(), match.group(2))
                continue
            in_directives = False
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if buffer and (not stripped or stripped.startswith("#")):
            continue  # blank line or comment inside a continuation
        if not buffer:
            first = number
        last_content = number
        if stripped.endswith("\\"):
            buffer.append(stripped[:-1])
            continue
        buffer.append(stripped)
        logical = " ".join(part.strip() for part in buffer if part.strip())
        buffer = []
        keyword, _, rest = logical.partition(" ")
        instruction = Instruction(keyword.upper(), rest.strip(), first, number)
        instructions.append(instruction)
        heredoc_match = _heredoc_match(keyword, rest)
        if heredoc_match:
            heredoc = (heredoc_match.group(3), heredoc_match.group(1) == "-")
            heredoc_owner_index = len(instructions) - 1
    dangling = bool(buffer)
    if buffer:
        logical = " ".join(part.strip() for part in buffer if part.strip())
        keyword, _, rest = logical.partition(" ")
        instructions.append(
            Instruction(keyword.upper(), rest.strip(), first, last_content)
        )
    return ParsedDockerfile(
        instructions=instructions,
        syntax_directive=directives.get("syntax"),
        escape_directive=directives.get("escape"),
        dangling_continuation=dangling,
    )


def unread_reason(parsed: ParsedDockerfile) -> str | None:
    """Why the document as a whole is not read, or ``None``.

    A non-default ``# escape=`` changes line continuation, so the split into
    instructions itself is untrusted; every check built on that split —
    syntax, sources, exactness — must say so rather than trust it.
    """
    if parsed.escape_directive not in (None, "\\"):
        return f"escape directive not modelled: {parsed.escape_directive}"
    return None


def opens_heredoc(keyword: str, args: str) -> bool:
    """Whether an instruction's arguments open a heredoc.

    Only RUN, COPY and ADD take heredocs, and only an unquoted ``<<WORD``
    opens one: ``LABEL x="<<EOF"`` or a quoted path containing ``<<`` does
    not. The parser and the source checks share this one rule.
    """
    return _heredoc_match(keyword, args) is not None


def _heredoc_match(keyword: str, args: str) -> re.Match[str] | None:
    if keyword.upper() not in _HEREDOC_KEYWORDS:
        return None
    return _HEREDOC_RE.search(_QUOTED_RE.sub("", args))


def syntax_checks(parsed: ParsedDockerfile, dockerfile: str) -> list[ReproductionCheck]:
    """The four checks of §3.1; one passed check per clean rule.

    A fold over :func:`deployer.reproduce.detail.syntax_records`.
    """
    # Imported here: ``detail`` builds on this module's parser and rules.
    from deployer.reproduce.detail import fold_syntax, syntax_records

    return fold_syntax(syntax_records(parsed, dockerfile))


def _from_args_ok(args: str) -> bool:
    tokens = args.split()
    if tokens and tokens[0].startswith("--platform="):
        tokens = tokens[1:]
    return len(tokens) == 1 or (len(tokens) == 3 and tokens[1].upper() == "AS")
