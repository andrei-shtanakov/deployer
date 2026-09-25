"""Binding the admitted instruction to exact bytes (design §3.1).

A's ``defect`` names a class, a line span and an object — not an ordinal or
exact bytes. This module turns that into a :class:`Bound`: the one
instruction whose span matches, cross-checked against ``defect.object``, and
the exact byte slice of the Dockerfile those lines occupy. ``splice``
replaces that slice; ``link_problem`` re-parses a corrected Dockerfile and
says whether it still differs from the original in nothing but that one
span.
"""

from dataclasses import dataclass

from deployer.admission.model import Defect
from deployer.reproduce.checks import _norm, _sources
from deployer.reproduce.dockerfile import Instruction, parse


@dataclass(frozen=True)
class Bound:
    """One Dockerfile instruction bound to a defect, with its exact bytes."""

    ordinal: int
    lines: tuple[int, int]
    original: bytes
    instruction: Instruction


def bind_instruction(dockerfile: bytes, defect: Defect) -> Bound | str:
    """Bind ``defect`` to exactly one instruction of ``dockerfile`` (§3.1).

    ``dockerfile`` is decoded ``"utf-8"``, ``errors="replace"`` — the same
    rule admission's ``prepare._as_r_reads`` applies before R's
    ``dockerfile.parse``, so line numbers agree with R's. Exactly one
    instruction must span ``defect.lines``; it is then cross-checked against
    ``defect.object`` (``missing_copy_source``: one of its normalised
    sources; ``from_argument_count``: its normalised text). Any failure
    returns the reason (``fix method not established``, per the caller).
    """
    text = dockerfile.decode("utf-8", errors="replace")
    parsed = parse(text)
    matches = [
        (ordinal, instruction)
        for ordinal, instruction in enumerate(parsed.instructions)
        if (instruction.first_line, instruction.last_line) == defect.lines
    ]
    if len(matches) != 1:
        return (
            f"{len(matches)} instructions span lines {defect.lines}, "
            "expected exactly one"
        )
    ordinal, instruction = matches[0]
    cross_check_reason = _cross_check(defect, instruction)
    if cross_check_reason is not None:
        return cross_check_reason
    first, last = defect.lines
    lines = dockerfile.splitlines(keepends=True)
    original = b"".join(lines[first - 1 : last])
    return Bound(
        ordinal=ordinal, lines=defect.lines, original=original, instruction=instruction
    )


def _cross_check(defect: Defect, instruction: Instruction) -> str | None:
    """The §3.1 cross-check for ``defect.cls``, or ``None`` if it holds."""
    if defect.cls == "missing_copy_source":
        sources, why = _sources(instruction)
        if why is not None:
            return f"cross-check failed: sources not readable ({why})"
        normalised = {_norm(source) for source in sources}
        if defect.object not in normalised:
            return (
                f"cross-check failed: {defect.object!r} is not a normalised "
                f"source of the instruction at line {instruction.first_line}"
            )
        return None
    if defect.cls == "from_argument_count":
        if instruction.text != defect.object:
            return (
                "cross-check failed: instruction text "
                f"{instruction.text!r} != defect object {defect.object!r}"
            )
        return None
    return f"cross-check failed: unrecognised defect class {defect.cls!r}"


def splice(dockerfile: bytes, bound: Bound, replacement: bytes) -> bytes:
    """Replace ``bound``'s exact line span in ``dockerfile`` with bytes."""
    first, last = bound.lines
    lines = dockerfile.splitlines(keepends=True)
    return b"".join(lines[: first - 1]) + replacement + b"".join(lines[last:])


def link_problem(original: bytes, corrected: bytes, bound: Bound) -> str | None:
    """Whether ``corrected`` still admits only the change §3.1 allows.

    Re-parses both sides and checks, in order: the instruction count is
    unchanged; every other instruction's ``text`` and span are unchanged;
    the bytes outside the bound span are identical; the bound span's line
    range is unchanged. The first violated check's reason is returned, or
    ``None`` once ``corrected`` passes them all.
    """
    before = parse(original.decode("utf-8", errors="replace"))
    after = parse(corrected.decode("utf-8", errors="replace"))
    if not 0 <= bound.ordinal < len(before.instructions):
        return "bound ordinal is out of range for the original Dockerfile"
    if len(before.instructions) != len(after.instructions):
        return (
            "instruction count changed: "
            f"{len(before.instructions)} -> {len(after.instructions)}"
        )
    for ordinal, (before_inst, after_inst) in enumerate(
        zip(before.instructions, after.instructions)
    ):
        if ordinal == bound.ordinal:
            continue
        before_span = (before_inst.first_line, before_inst.last_line)
        after_span = (after_inst.first_line, after_inst.last_line)
        if before_inst.text != after_inst.text or before_span != after_span:
            return f"instruction {ordinal} changed outside the bound span"
    first, last = bound.lines
    lines = original.splitlines(keepends=True)
    prefix = b"".join(lines[: first - 1])
    suffix = b"".join(lines[last:])
    if (
        not corrected.startswith(prefix)
        or not corrected.endswith(suffix)
        or len(corrected) < len(prefix) + len(suffix)
    ):
        return "bytes outside the bound span changed"
    changed = after.instructions[bound.ordinal]
    if (changed.first_line, changed.last_line) != bound.lines:
        return "the bound instruction's line range changed"
    return None
