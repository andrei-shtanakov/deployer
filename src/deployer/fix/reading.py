"""Where R's reading of a Dockerfile and the builders' may differ.

R (``reproduce.dockerfile.parse``) and the builders (BuildKit, Buildah) read
most Dockerfiles alike, but not all. A fix built on R's reading is only sound
where the two agree, so each check here names a form on which they may
disagree and returns the refusal reason, or ``None`` when the form is absent.
Shared by the FROM transformations (``fromfix``) and the COPY envelope
(``envelope``) so the rules have one implementation.
"""

from collections.abc import Iterable

from deployer.reproduce.dockerfile import Instruction


def join_reason(data: bytes) -> str | None:
    """Refuse any continuation R and the builders may join differently.

    R joins continuation lines with a space; BuildKit joins them with no
    separator, so ``img:1\\<newline>extra`` reads as ``img:1extra`` and
    ``--from=ext\\<newline>ra`` as ``--from=extra`` there. Only
    continuations after a space or tab read the same in both, so every
    physical line of ``data`` is checked.
    """
    for number, line in enumerate(data.splitlines(), start=1):
        content = line.rstrip(b" \t")
        if content.endswith(b"\\") and content[-2:-1] not in (b" ", b"\t"):
            return (
                f"line {number}: a line continuation without a preceding "
                "blank is not modelled"
            )
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
