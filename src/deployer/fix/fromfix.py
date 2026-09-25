"""The closed FROM transformations F1 and F2 (design §4.2, §4.3).

``from_argument_count`` is fixed without a model, and only when the original
FROM unambiguously holds a complete literal image reference — one with an
explicit tag or digest — so the trailing tokens cannot belong to it:

- F1: ``FROM <ref> <token>`` → ``FROM <ref> AS <token>``, when ``<token>`` is
  a stage name both BuildKit and Buildah accept and treat alike
  (``docs/fix-stage-name-grammar.md``) and nothing else names it.
- F2: ``FROM <ref> AS`` (dangling, any case) → ``FROM <ref>``.

Everything else is a no-proposal reason. The replacement keeps every original
byte of the instruction and changes only the listed token.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from deployer.admission.prepare import _as_r_reads
from deployer.fix.binding import Bound
from deployer.fix.reading import comment_reason, join_reason, keyword_reason
from deployer.reproduce.dockerfile import ParsedDockerfile, parse, unread_reason

# The BuildKit ∩ Buildah stage-name grammar, matched against the raw token
# with no case folding (see docs/fix-stage-name-grammar.md for the pinned
# sources): BuildKit lowercases then requires ^[a-z][a-z0-9-_.]*$, Buildah
# keeps the name verbatim, so only an already-lowercase ASCII name means
# the same stage to both.
STAGE_NAME_RE: re.Pattern[str] = re.compile(r"[a-z][a-z0-9_.-]*", re.ASCII)

# The distribution reference grammar
# ([domain[:port]/]path-component(/path-component)*[:tag][@digest]).
_DOMAIN_COMPONENT = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
_DOMAIN = rf"{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})*(?::[0-9]+)?"
_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_TAG = r"[\w][\w.-]{0,127}"
_DIGEST = r"sha256:[0-9a-f]{64}"
_REFERENCE_RE = re.compile(
    rf"(?:(?P<domain>{_DOMAIN})/)?"
    rf"(?P<path>{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*)"
    rf"(?::(?P<tag>{_TAG}))?"
    rf"(?:@(?P<digest>{_DIGEST}))?",
    re.ASCII,
)
_PATH_COMPONENT_RE = re.compile(_PATH_COMPONENT, re.ASCII)
_NAME_MAX = 255
_PLATFORM_FLAG = "--platform="
# Characters that make a ``--from``/``from=`` value or a FROM base depend on
# expansion or quoting — not checkable against a literal token.
_UNRESOLVED = frozenset("$\"'\\")
_GRAMMAR_NOTE = "docs/fix-stage-name-grammar.md"
# Names BuildKit gives a meaning of its own; never proposed as stage names.
_RESERVED_NAMES = frozenset({"scratch", "context"})
# The builders' in-line whitespace: BuildKit splits arguments on
# [\t\v\f\r ]+, and only space or tab separates words within one line.
_BLANK = rb"[ \t]"
# A word boundary on the right: in-line whitespace, a line end, or EOF.
_WORD_END = rb"(?=[ \t\r\n]|\Z)"


@dataclass(frozen=True)
class Reference:
    """A parsed image reference; ``domain`` is ``None`` for Docker Hub names."""

    domain: str | None
    path: str
    tag: str | None
    digest: str | None


@dataclass(frozen=True)
class FromFix:
    """A proposed F1/F2 edit: the corrected instruction bytes and why."""

    transformation: Literal["F1", "F2"]
    replacement: bytes
    conditions: list[str]


def is_stage_name(token: str) -> bool:
    """Whether ``token`` is a stage name BuildKit and Buildah agree on."""
    return STAGE_NAME_RE.fullmatch(token) is not None


def parse_reference(token: str) -> Reference | None:
    """Parse ``token`` by the distribution reference grammar, or ``None``.

    A ``domain:port`` colon is not a tag: ``registry:5000/app`` has no tag.
    The first component is a domain only when Docker would treat it as one
    (it contains ``.`` or ``:``, is ``localhost``, or has uppercase);
    otherwise it is a path component and must satisfy that grammar.
    """
    match = _REFERENCE_RE.fullmatch(token)
    if match is None:
        return None
    domain, path = match.group("domain"), match.group("path")
    if domain is not None and not _is_domain(domain):
        if _PATH_COMPONENT_RE.fullmatch(domain) is None:
            return None
        domain, path = None, f"{domain}/{path}"
    name_length = len(path) + (len(domain) + 1 if domain is not None else 0)
    if name_length > _NAME_MAX:
        return None
    return Reference(
        domain=domain, path=path, tag=match.group("tag"), digest=match.group("digest")
    )


def propose_from(
    parsed: ParsedDockerfile,
    bound: Bound,
    build_args: Sequence[tuple[str, str]],
    dockerfile: bytes,
) -> FromFix | str:
    """F1 or F2 for ``bound``'s FROM, or the no-proposal reason (§4.2).

    ``parsed`` is the whole Dockerfile as R read it (for stage names and
    references to the token); ``build_args`` is R's bound build
    configuration as ``(name, value)`` pairs, the only channel left that
    could select the token — every pair is checked, so a name given twice
    is refused when either value names the token;
    ``dockerfile`` is the raw bytes ``parsed`` was read from, checked for
    continuations the builders would join differently from R.
    """
    instruction = bound.instruction
    unread = unread_reason(parsed)
    if unread is not None:
        return unread
    for reason in (join_reason(dockerfile), keyword_reason(parsed.instructions)):
        if reason is not None:
            return reason
    if instruction.keyword != "FROM":
        return f"the bound instruction is not a FROM ({instruction.keyword})"
    if not 0 <= bound.ordinal < len(parsed.instructions) or (
        parsed.instructions[bound.ordinal] != instruction
    ):
        return "the bound instruction does not match the parsed Dockerfile"
    comment = comment_reason(bound.original)
    if comment is not None:
        return comment
    tokens = instruction.args.split()
    flags, rest = _split_flags(tokens)
    if isinstance(flags, str):
        return flags
    conditions = [f"flags: {' '.join(flags)} (recognised)" if flags else "flags: none"]
    reference_reason = _reference_reason(rest, conditions)
    if reference_reason is not None:
        return reference_reason
    if len(rest) != 2:
        return _count_reason(rest)
    token = rest[1]
    if token.upper() == "AS":
        return _f2(bound, tokens, conditions)
    return _f1(parsed, bound, tokens, token, build_args, conditions)


def _is_domain(component: str) -> bool:
    """Docker's ``splitDockerDomain`` rule for the first name component."""
    return (
        "." in component
        or ":" in component
        or component == "localhost"
        or component != component.lower()
    )


def _split_flags(tokens: list[str]) -> tuple[list[str] | str, list[str]]:
    """The recognised leading flags and the arguments, or a reason.

    Exactly R's FROM check: only an inline ``--platform=<value>`` in first
    position is skipped; any other ``--`` flag is unrecognised.
    """
    if not tokens or not tokens[0].startswith("--"):
        return [], tokens
    flag = tokens[0]
    if not flag.startswith(_PLATFORM_FLAG):
        return f"unrecognised flag {flag!r}", []
    if flag == _PLATFORM_FLAG:
        return "empty --platform= value", []
    return [flag], tokens[1:]


def _reference_reason(rest: list[str], conditions: list[str]) -> str | None:
    """Why ``rest[0]`` is not a complete literal reference, or ``None``."""
    if not rest:
        return "FROM has no reference"
    ref = rest[0]
    if "$" in ref:
        return f"reference {ref!r} contains a substitution"
    parsed_ref = parse_reference(ref)
    if parsed_ref is None:
        return f"{ref!r} is not a valid reference"
    if parsed_ref.tag is None and parsed_ref.digest is None:
        return f"reference {ref!r} has no tag or digest; the next token may be one"
    explicit = (
        f"tag {parsed_ref.tag!r}"
        if parsed_ref.digest is None
        else f"digest {parsed_ref.digest!r}"
    )
    conditions.append(f"{ref!r} is a complete literal reference ({explicit})")
    return None


def _count_reason(rest: list[str]) -> str:
    """The no-proposal reason for any argument count other than two."""
    if len(rest) == 1:
        return "FROM has one argument; nothing to fix"
    if len(rest) == 3 and rest[1].upper() == "AS":
        return "FROM already reads <ref> AS <name>; nothing to fix"
    if len(rest) == 3:
        return f"3 arguments with a middle other than AS ({rest[1]!r})"
    return f"{len(rest)} arguments; only F1/F2 forms are fixed"


def _f2(bound: Bound, tokens: list[str], conditions: list[str]) -> FromFix | str:
    """F2: delete the dangling ``AS`` and the whitespace before it."""
    conditions.append(f"the last argument {tokens[-1]!r} is a dangling AS")
    pattern = rb"(?<=[^ \t\r\n\\])" + _BLANK + rb"+(?i:as)" + _WORD_END
    matches = list(re.finditer(pattern, bound.original))
    if len(matches) != 1:
        return "the dangling AS is not located unambiguously in the original bytes"
    start, end = matches[0].span()
    replacement = bound.original[:start] + bound.original[end:]
    return _checked("F2", bound, replacement, tokens[:-1], conditions)


def _f1(
    parsed: ParsedDockerfile,
    bound: Bound,
    tokens: list[str],
    token: str,
    build_args: Sequence[tuple[str, str]],
    conditions: list[str],
) -> FromFix | str:
    """F1: insert ``AS`` before a stage-name token nothing else names."""
    if not is_stage_name(token):
        return f"{token!r} is outside the stage-name grammar ({_GRAMMAR_NOTE})"
    conditions.append(f"{token!r} matches the stage-name grammar ({_GRAMMAR_NOTE})")
    conditions.append(f"{token!r} is not AS in any case")
    if token in _RESERVED_NAMES:
        return f"{token!r} is a name BuildKit reserves"
    conditions.append(
        f"{token!r} is not a reserved name ({', '.join(sorted(_RESERVED_NAMES))})"
    )
    usage_reason = _usage_reason(parsed, bound.ordinal, token)
    if usage_reason is not None:
        return usage_reason
    conditions.append(f"{token!r} collides with no stage name (any case)")
    conditions.append(f"no FROM references {token!r} (any case)")
    conditions.append(f"no --from= or mount from= names {token!r} (any case)")
    for name, value in sorted(build_args):
        if value.lower() == token:
            return f"build arg {name!r} names {token!r}"
    conditions.append(f"no build arg of the bound build configuration names {token!r}")
    # The token must follow other content on its own physical line: a token
    # opening a continuation line would be glued to the previous line by
    # BuildKit's join, so it is refused rather than modelled.
    pattern = (
        rb"[^ \t\r\n\\]"
        + _BLANK
        + rb"+("
        + re.escape(token.encode("ascii"))
        + rb")"
        + _WORD_END
    )
    matches = list(re.finditer(pattern, bound.original))
    if len(matches) != 1:
        return (
            f"{token!r} is not located unambiguously after a blank on the "
            "same line in the original bytes"
        )
    conditions.append(
        f"{token!r} located once, after a blank on the same line, in the original bytes"
    )
    at = matches[0].start(1)
    replacement = bound.original[:at] + b"AS " + bound.original[at:]
    expected = [*tokens[:-1], "AS", token]
    return _checked("F1", bound, replacement, expected, conditions)


def _usage_reason(parsed: ParsedDockerfile, ordinal: int, token: str) -> str | None:
    """Why ``token`` takes part in a stage name or reference, or ``None``.

    Compared case-insensitively (BuildKit folds stage names); any value that
    depends on expansion or quoting cannot be checked and is refused.
    """
    for index, instruction in enumerate(parsed.instructions):
        # The whole line, not R's args: R's keyword split is on a space only.
        keyword, *words = f"{instruction.keyword} {instruction.args}".split()
        if keyword.upper() == "FROM" and index != ordinal:
            reason = _from_usage(words, token)
            if reason is not None:
                return reason
        for word in words:
            if word.lower().startswith("--mount=") and _UNRESOLVED & set(word):
                return f"--mount value {word!r} is quoted or escaped"
        for value in _from_values(words):
            if _UNRESOLVED & set(value):
                return f"--from/from= value {value!r} contains a substitution"
            if value.lower() == token:
                line = instruction.first_line
                return f"{token!r} is referenced by --from/from= at line {line}"
    return None


def _from_usage(words: list[str], token: str) -> str | None:
    """A FROM's base or stage name that names or may name ``token``."""
    rest = [word for word in words if not word.startswith("--")]
    if not rest:
        return None
    base = rest[0]
    if _UNRESOLVED & set(base):
        return f"FROM base {base!r} contains a substitution"
    if base.lower() == token:
        return f"{token!r} is referenced by FROM {base}"
    if len(rest) == 3 and rest[1].upper() == "AS":
        name = rest[2]
        if _UNRESOLVED & set(name):
            return f"stage name {name!r} contains a substitution"
        if name.lower() == token:
            return f"{token!r} collides with the stage name {name!r}"
    return None


def _from_values(words: list[str]) -> list[str]:
    """Every ``--from=`` value and every ``from=`` of a ``--mount=``."""
    values: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered.startswith("--from="):
            values.append(word[len("--from=") :])
        elif lowered.startswith("--mount="):
            values.extend(
                option[len("from=") :]
                for option in word[len("--mount=") :].split(",")
                if option.lower().startswith("from=")
            )
    return values


def _checked(
    transformation: Literal["F1", "F2"],
    bound: Bound,
    replacement: bytes,
    expected: list[str],
    conditions: list[str],
) -> FromFix | str:
    """Re-read ``replacement`` with R's parser; it must be ``expected``.

    A guard against a wrong splice under R's reading only: it does not
    show that BuildKit or Buildah read the replacement the same way. The
    builders' reading rests on the byte-level rules checked before it.
    """
    reread = parse(_as_r_reads(replacement)).instructions
    same_lines = len(replacement.splitlines()) == len(bound.original.splitlines())
    if (
        len(reread) != 1
        or reread[0].keyword != "FROM"
        or reread[0].args.split() != expected
        or not same_lines
    ):
        return f"{transformation} does not re-read as the intended FROM"
    conditions.append(
        "under R's parser (not the builders'), the replacement re-reads as the "
        "intended FROM on the same lines"
    )
    return FromFix(
        transformation=transformation, replacement=replacement, conditions=conditions
    )
