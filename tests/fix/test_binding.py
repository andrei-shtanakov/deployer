"""Binding the admitted instruction (design §3.1): matching, cross-checks,
exact-byte extraction, splicing and the post-edit link check."""

import dataclasses

import pytest

from deployer.admission.model import Defect, DefectClass
from deployer.fix.binding import Bound, bind_instruction, link_problem, splice
from deployer.reproduce.dockerfile import Instruction, ParsedDockerfile, parse

_DOCKERFILE = (
    b"FROM python:3.12-slim extra\n"
    b"COPY app.py other.py /app/\n"
    b"RUN pip install -r requirements.txt\n"
)


def _defect(cls: DefectClass, lines: tuple[int, int], obj: str) -> Defect:
    """A ``Defect`` built with the attribute names, like the model's tests."""
    return Defect(cls=cls, file="Dockerfile", lines=lines, object=obj)


def test_unique_match_missing_copy_source() -> None:
    """A single spanning instruction, cross-checked source, binds."""
    defect = _defect("missing_copy_source", (2, 2), "app.py")
    bound = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(bound, Bound)
    assert bound.ordinal == 1
    assert bound.lines == (2, 2)
    assert bound.original == b"COPY app.py other.py /app/\n"
    assert bound.instruction.keyword == "COPY"


def test_unique_match_from_argument_count() -> None:
    """The FROM cross-check compares normalised instruction text."""
    defect = _defect("from_argument_count", (1, 1), "FROM python:3.12-slim extra")
    bound = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(bound, Bound)
    assert bound.ordinal == 0
    assert bound.original == b"FROM python:3.12-slim extra\n"


def test_zero_matches_returns_reason() -> None:
    """No instruction spans the defect's lines."""
    defect = _defect("missing_copy_source", (99, 99), "app.py")
    result = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(result, str)
    assert "0 instructions" in result


def test_several_matches_returns_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two instructions sharing a span is refused, not picked arbitrarily."""
    duplicate = Instruction(keyword="RUN", args="x", first_line=5, last_line=5)
    fake = ParsedDockerfile(
        instructions=[duplicate, duplicate],
        syntax_directive=None,
        escape_directive=None,
        dangling_continuation=False,
    )
    import deployer.fix.binding as binding_mod

    monkeypatch.setattr(binding_mod, "parse", lambda text: fake)
    defect = _defect("missing_copy_source", (5, 5), "x")
    result = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(result, str)
    assert "2 instructions" in result


def test_cross_check_failure_missing_copy_source() -> None:
    """The named object is not a normalised source of the bound instruction."""
    defect = _defect("missing_copy_source", (2, 2), "not-a-source.py")
    result = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(result, str)
    assert "cross-check failed" in result


def test_cross_check_failure_from_argument_count() -> None:
    """The object text does not equal the instruction's normalised text."""
    defect = _defect("from_argument_count", (1, 1), "FROM python:3.12-slim")
    result = bind_instruction(_DOCKERFILE, defect)
    assert isinstance(result, str)
    assert "cross-check failed" in result


def test_crlf_dockerfile_binds_and_splices() -> None:
    """A CRLF Dockerfile binds with its endings intact and splices cleanly."""
    dockerfile = b"FROM x\r\nCOPY a b\r\nRUN c\r\n"
    defect = _defect("missing_copy_source", (2, 2), "a")
    bound = bind_instruction(dockerfile, defect)
    assert isinstance(bound, Bound)
    assert bound.original == b"COPY a b\r\n"
    corrected = splice(dockerfile, bound, b"COPY z b\r\n")
    assert corrected == b"FROM x\r\nCOPY z b\r\nRUN c\r\n"
    assert link_problem(dockerfile, corrected, bound) is None


def test_lone_cr_binds_with_its_own_span() -> None:
    """A lone ``\\r`` (no following ``\\n``) is a line break, like R's rule."""
    dockerfile = b"FROM x\rCOPY a b\rRUN c\n"
    parsed = parse(dockerfile.decode("utf-8", errors="replace"))
    assert [(i.first_line, i.last_line) for i in parsed.instructions] == [
        (1, 1),
        (2, 2),
        (3, 3),
    ]
    defect = _defect("missing_copy_source", (2, 2), "a")
    bound = bind_instruction(dockerfile, defect)
    assert isinstance(bound, Bound)
    # The original bytes keep the lone `\r` that ends this line, not a `\n`.
    assert bound.original == b"COPY a b\r"
    corrected = splice(dockerfile, bound, b"COPY z b\r")
    assert corrected == b"FROM x\rCOPY z b\rRUN c\n"


def test_form_feed_does_not_split_the_line() -> None:
    """A ``\\x0c`` inside an instruction is not a line break for R or bytes."""
    dockerfile = b"FROM base\nRUN echo hi\x0cthere\n"
    parsed = parse(dockerfile.decode("utf-8", errors="replace"))
    run = parsed.instructions[1]
    assert (run.first_line, run.last_line) == (2, 2)
    defect = _defect("from_argument_count", (2, 2), run.text)
    bound = bind_instruction(dockerfile, defect)
    assert isinstance(bound, Bound)
    assert bound.lines == (2, 2)
    assert bound.original == b"RUN echo hi\x0cthere\n"


def test_link_problem_none_for_a_clean_splice() -> None:
    """A same-line-count replacement inside the span is unproblematic."""
    defect = _defect("missing_copy_source", (2, 2), "a")
    bound = bind_instruction(_dockerfile_abc(), defect)
    assert isinstance(bound, Bound)
    corrected = splice(_dockerfile_abc(), bound, b"COPY z b\n")
    assert link_problem(_dockerfile_abc(), corrected, bound) is None


def test_link_problem_reports_an_added_line() -> None:
    """A replacement that adds a line shifts a later instruction's span."""
    original = _dockerfile_abc()
    defect = _defect("missing_copy_source", (2, 2), "a")
    bound = bind_instruction(original, defect)
    assert isinstance(bound, Bound)
    corrected = splice(original, bound, b"COPY z b\n\n")
    reason = link_problem(original, corrected, bound)
    assert reason is not None
    assert "outside the bound span" in reason


def test_link_problem_reports_a_change_outside_the_span() -> None:
    """A byte edit outside the bound span is refused even if the span
    itself was replaced correctly."""
    original = _dockerfile_abc()
    defect = _defect("missing_copy_source", (2, 2), "a")
    bound = bind_instruction(original, defect)
    assert isinstance(bound, Bound)
    corrected = splice(original, bound, b"COPY z b\n")
    tampered = corrected.replace(b"RUN c\n", b"RUN cX\n")
    reason = link_problem(original, tampered, bound)
    assert reason is not None


def _dockerfile_abc() -> bytes:
    """A minimal three-instruction Dockerfile for the ``link_problem`` tests."""
    return b"FROM x\nCOPY a b\nRUN c\n"


def test_bound_is_frozen() -> None:
    """``Bound`` is an immutable record of the binding."""
    defect = _defect("missing_copy_source", (2, 2), "a")
    bound = bind_instruction(_dockerfile_abc(), defect)
    assert isinstance(bound, Bound)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bound.ordinal = 99  # type: ignore[misc]
