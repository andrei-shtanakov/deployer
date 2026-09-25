"""The closed COPY/ADD envelope for ``missing_copy_source`` (design §4.1).

The model may only choose a replacement source from a list this module
computes deterministically: regular files of R's ``head_sha`` listing that
lie in the effective build context, are written in a form R and the builders
read alike, and collide with nothing else in the instruction.
:func:`eligible_sources` builds that list or returns why no list is
established; :func:`apply_source` writes a chosen source into the bound
instruction's exact bytes, changing only the absent source's token.

Listing paths are compared with sources as context-relative paths, the same
reading admission uses when it proves a source absent.
"""

import posixpath
import re
from dataclasses import dataclass

from deployer.admission.decide import GLOB_CHARS, LINK_MODES, REMOTE_PREFIXES
from deployer.admission.prepare import _as_r_reads
from deployer.fix.binding import Bound
from deployer.fix.reading import comment_reason, join_reason, keyword_reason
from deployer.provenance.model import TreeRow
from deployer.reproduce.checks import _is_modelled_source, _norm, _sources
from deployer.reproduce.dockerfile import opens_heredoc, parse, unread_reason
from deployer.reproduce.ignore import IgnoreRules, excluded_by

REGULAR_MODES = frozenset({"100644", "100755"})
_DEPLOYER_DIR = ".deployer/"
_DOT = "./"
_LEADING_FLAGS_RE = re.compile(r"((?:--\S+\s+)*)")
# A whole token: preceded and followed by in-line whitespace, a line end,
# or the start/end of the bytes.
_BEFORE = rb"(?<![^ \t\r\n])"
_AFTER = rb"(?![^ \t\r\n])"

_READ_ALIKE = "1 read alike, one token changes"
_REGULAR = "2 regular file"
_CONTEXT = "3 effective build context"
_COLLISIONS = "4 collisions"
_FLOOR = "5 basename floor"
_ANY = "candidates"


@dataclass(frozen=True)
class Candidates:
    """The eligible replacement sources and the conditions that produced them.

    ``eligible`` holds context-relative listing paths, sorted. Each entry of
    ``conditions`` is ``{"condition": str, "ok": bool, "detail": str}``.
    """

    eligible: tuple[str, ...]
    conditions: list[dict]


@dataclass(frozen=True)
class _Located:
    """The absent source as written, and the instruction's other sources."""

    raw: str
    others: list[str]
    span: tuple[int, int]


def eligible_sources(
    bound: Bound,
    absent: str,
    listing: list[TreeRow],
    ci_rules: IgnoreRules,
    local_rules: IgnoreRules,
    *,
    dockerfile: bytes,
    artifact_path: str = "Dockerfile",
) -> Candidates | str:
    """The replacement sources §4.1 admits for ``absent``, or why none are.

    ``absent`` is A's normalised source (``defect.object``); ``listing`` is
    R's ``head_sha`` tree; ``ci_rules``/``local_rules`` are R's effective
    ignore files on each side; ``dockerfile`` is the raw file ``bound`` was
    bound in, read for document-wide forms (``# escape=``);
    ``artifact_path`` is the Dockerfile's context-relative path, never a
    candidate, like anything under ``.deployer/``. A returned
    string is the failing condition and its detail — the caller reports it
    as ``fix method not established``. The basename floor is applied here,
    before any model call.
    """
    reason = _document_reason(bound, dockerfile) or _bound_reason(bound)
    if reason is not None:
        return f"{_READ_ALIKE}: {reason}"
    located = _locate(bound, absent)
    if isinstance(located, str):
        return f"{_READ_ALIKE}: {located}"
    conditions = [
        _ok(_READ_ALIKE, f"only the token {located.raw!r} may change"),
    ]
    for side, rules in (("CI", ci_rules), ("local", local_rules)):
        if rules.unsupported is not None:
            return (
                f"{_CONTEXT}: the {side} ignore file {rules.file} has an "
                f"unmodelled pattern {rules.unsupported!r}; exclusion is unprovable"
            )
    regular = _regular_files(listing)
    conditions.append(_ok(_REGULAR, f"{len(regular)} regular files in the listing"))
    others_reason = _others_reason(located.others, regular) or _destination_reason(
        bound, located.others
    )
    if others_reason is not None:
        return f"{_COLLISIONS}: {others_reason}"
    in_context = [
        path
        for path in regular
        if path not in (absent, artifact_path)
        and not path.startswith(_DEPLOYER_DIR)
        and _form_reason(path) is None
        and excluded_by(ci_rules, path) is None
        and excluded_by(local_rules, path) is None
    ]
    conditions.append(
        _ok(
            _CONTEXT,
            f"{len(in_context)} in the modelled form, excluded by neither, "
            f"not {artifact_path!r} or under {_DEPLOYER_DIR!r}",
        )
    )
    eligible = [path for path in in_context if not _collides(path, located.others)]
    conditions.append(
        _ok(_COLLISIONS, f"{len(eligible)} with no duplicate or destination conflict")
    )
    if not eligible:
        return f"{_ANY}: no eligible replacement source for {absent!r}"
    name = posixpath.basename(absent)
    same_name = [path for path in eligible if posixpath.basename(path) == name]
    if len(same_name) > 1:
        return (
            f"{_FLOOR}: {len(same_name)} eligible files named {name!r}: "
            f"{', '.join(same_name)}"
        )
    conditions.append(_ok(_FLOOR, f"{len(same_name)} eligible file(s) named {name!r}"))
    return Candidates(eligible=tuple(eligible), conditions=conditions)


def apply_source(bound: Bound, absent: str, new: str) -> bytes | str:
    """``bound.original`` with the absent source's token replaced by ``new``.

    Precondition: valid only after :func:`eligible_sources` has admitted
    ``bound`` for ``absent`` (it alone checks the document and the listing)
    and ``new`` came from that call's :attr:`Candidates.eligible`; this
    function does not re-check collisions, the listing or ignore rules.

    ``new`` is a context-relative path; it is written in the original's notation
    — a leading ``./`` iff the absent source had one. The absent token must
    occur exactly once in the raw bytes as a whole whitespace-delimited
    token; only those bytes change. The result is re-read as R reads it and
    must hold the same keyword, flags, destination and other sources.
    Anything else returns the reason.
    """
    reason = _bound_reason(bound)
    if reason is not None:
        return reason
    located = _locate(bound, absent)
    if isinstance(located, str):
        return located
    form_reason = _form_reason(new)
    if form_reason is not None:
        return f"replacement {new!r}: {form_reason}"
    written = _DOT + new if located.raw.startswith(_DOT) else new
    start, end = located.span
    replacement = (
        bound.original[:start] + written.encode("ascii") + bound.original[end:]
    )
    return _checked(bound, located.raw, written, replacement)


def _ok(condition: str, detail: str) -> dict:
    """One satisfied condition entry."""
    return {"condition": condition, "ok": True, "detail": detail}


def _document_reason(bound: Bound, dockerfile: bytes) -> str | None:
    """Document-wide forms R does not read, or a ``bound`` not from it."""
    parsed = parse(_as_r_reads(dockerfile))
    unread = unread_reason(parsed)
    if unread is not None:
        return unread
    instructions = parsed.instructions
    first, last = bound.lines
    span = b"".join(dockerfile.splitlines(keepends=True)[first - 1 : last])
    if (
        not 0 <= bound.ordinal < len(instructions)
        or instructions[bound.ordinal] != bound.instruction
        or span != bound.original
    ):
        return "the bound instruction does not match the Dockerfile"
    return None


def _bound_reason(bound: Bound) -> str | None:
    """A form of the bound instruction R and the builders may read apart."""
    instruction = bound.instruction
    split = keyword_reason([instruction])
    if split is not None:
        return split
    if instruction.keyword == "ADD":
        return (
            "ADD replacement not supported in this slice: archive extraction "
            "depends on content"
        )
    if instruction.keyword != "COPY":
        return f"the bound instruction is not COPY ({instruction.keyword})"
    for reason in (
        join_reason(bound.original),
        comment_reason(bound.original),
    ):
        if reason is not None:
            return reason
    args = instruction.args.strip()
    if opens_heredoc(instruction.keyword, args):
        return "a heredoc is not modelled"
    lead = _LEADING_FLAGS_RE.match(args)
    body = args[lead.end(1) :] if lead else args
    if body.startswith("["):
        return "the JSON (exec) form is not modelled"
    if set("'\"") & set(args):
        return "a quote in the instruction's arguments is not modelled"
    if "\\" in args:
        return "a backslash escape in the instruction's arguments is not modelled"
    stray = [token for token in body.split() if token.startswith("--")]
    if stray:
        return (
            f"{stray[0]!r} after the first source reads as a flag in R and as a "
            "source in the builders"
        )
    return None


def _locate(bound: Bound, absent: str) -> _Located | str:
    """The one source written for ``absent``, in a notation §4.1 keeps."""
    sources, why = _sources(bound.instruction)
    if why is not None:
        return f"sources not readable ({why})"
    written = [source for source in sources if _norm(source) == absent]
    if len(written) != 1:
        return f"{len(written)} sources normalise to {absent!r}, expected exactly one"
    raw = written[0]
    if raw not in (absent, _DOT + absent) or _form_reason(absent) is not None:
        return f"the notation of {raw!r} is not modelled"
    span = _token_span(bound.original, raw)
    if isinstance(span, str):
        return span
    at = sources.index(raw)
    others = sources[:at] + sources[at + 1 :]
    return _Located(raw=raw, others=others, span=span)


def _token_span(original: bytes, raw: str) -> tuple[int, int] | str:
    """The byte span of ``raw`` as the one whole token in ``original``.

    Whole means delimited by space, tab, a line end or the ends of the
    bytes — the builders' blanks. A token R splits off on any other
    whitespace (``\\x0c``, ``\\x1c``, ...) is not found, and a token written
    twice (``COPY app.py app.py``) is ambiguous: both are refused.
    """
    pattern = _BEFORE + re.escape(raw.encode("ascii")) + _AFTER
    matches = list(re.finditer(pattern, original))
    if len(matches) != 1:
        return (
            f"{raw!r} occurs {len(matches)} times as a whole token in the "
            "instruction's bytes, expected exactly once"
        )
    return matches[0].span()


def _destination_reason(bound: Bound, others: list[str]) -> str | None:
    """With several sources the destination must be a directory (``/``)."""
    destination = bound.instruction.args.split()[-1]
    if others and not destination.endswith("/"):
        return (
            f"several sources with destination {destination!r} not ending in "
            "'/'; the builder rejects that form"
        )
    return None


def _regular_files(listing: list[TreeRow]) -> list[str]:
    """Regular files of ``listing`` with no symlink or submodule ancestor."""
    links = {row.path for row in listing if row.mode in LINK_MODES}
    return sorted(
        row.path
        for row in listing
        if row.mode in REGULAR_MODES
        and row.type == "blob"
        and not links & set(_ancestors(row.path))
    )


def _ancestors(path: str) -> list[str]:
    """Every proper ancestor directory of ``path``."""
    parts = path.split("/")
    return ["/".join(parts[:n]) for n in range(1, len(parts))]


def _form_reason(path: str) -> str | None:
    """Why ``path`` is not written in the modelled literal form, or ``None``.

    R's alphabet (no ``$``, ``\\``, whitespace or control characters), no
    glob character, not remote-looking, not flag-looking, and already in
    R's normalised form (no ``./``, ``..``, ``//``, leading or trailing
    ``/``) — so the written token names exactly this path.
    """
    if not _is_modelled_source(path):
        return "outside the closed alphabet"
    if GLOB_CHARS & set(path):
        return "a glob character"
    if path.startswith(REMOTE_PREFIXES):
        return "reads as a remote source"
    if path.startswith("-"):
        return "reads as a flag"
    if _norm(path) != path or path == ".":
        return "not in R's normalised form"
    return None


def _others_reason(others: list[str], regular: list[str]) -> str | None:
    """Every other source must be an unambiguous regular file (§4.1 (4))."""
    files = set(regular)
    for other in others:
        if _form_reason(_strip_dot(other)) is not None or _norm(other) not in files:
            return (
                f"the other source {other!r} is not an unambiguous regular file of "
                "the listing; a destination conflict is not disproven"
            )
    return None


def _strip_dot(source: str) -> str:
    """``source`` without one leading ``./``."""
    return source[len(_DOT) :] if source.startswith(_DOT) else source


def _collides(path: str, others: list[str]) -> bool:
    """(a) a duplicate source, or (b) a destination name conflict."""
    normalised = [_norm(other) for other in others]
    if path in normalised:
        return True
    name = posixpath.basename(path)
    return bool(others) and any(posixpath.basename(n) == name for n in normalised)


def _checked(bound: Bound, raw: str, written: str, replacement: bytes) -> bytes | str:
    """Re-read ``replacement`` as R does; only ``raw`` may have changed."""
    reread = parse(_as_r_reads(replacement)).instructions
    tokens = bound.instruction.args.split()
    if tokens.count(raw) != 1:
        return f"{raw!r} is not exactly one argument of the instruction"
    expected = [written if token == raw else token for token in tokens]
    sources, _ = _sources(bound.instruction)
    expected_sources = [written if source == raw else source for source in sources]
    same_lines = len(replacement.splitlines()) == len(bound.original.splitlines())
    if (
        len(reread) != 1
        or reread[0].keyword != bound.instruction.keyword
        or reread[0].args.split() != expected
        or _sources(reread[0]) != (expected_sources, None)
        or not same_lines
    ):
        return "the replacement does not re-read as the intended instruction"
    return replacement
