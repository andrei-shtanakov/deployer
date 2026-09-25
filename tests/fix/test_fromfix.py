"""The closed FROM transformations F1/F2 (design §4.2) and their grammars
(§4.3 stage names, the distribution reference grammar)."""

from collections.abc import Mapping

import pytest

from deployer.admission.model import Defect
from deployer.admission.prepare import _as_r_reads
from deployer.fix.binding import Bound, bind_instruction, link_problem, splice
from deployer.fix.fromfix import (
    STAGE_NAME_RE,
    FromFix,
    Reference,
    is_stage_name,
    parse_reference,
    propose_from,
)
from deployer.reproduce.dockerfile import normalise, parse

_DIGEST = "sha256:" + "a" * 64


def _propose(
    dockerfile: bytes, line: int = 1, build_args: Mapping[str, str] | None = None
) -> tuple[FromFix | str, Bound]:
    """Bind the FROM at ``line`` like Task 4 does and propose for it."""
    text = _as_r_reads(dockerfile)
    parsed = parse(text)
    instruction = next(i for i in parsed.instructions if i.first_line == line)
    defect = Defect(
        cls="from_argument_count",
        file="Dockerfile",
        lines=(instruction.first_line, instruction.last_line),
        object=normalise(f"{instruction.keyword} {instruction.args}"),
    )
    bound = bind_instruction(dockerfile, defect)
    assert isinstance(bound, Bound)
    return propose_from(
        parsed, bound, tuple((build_args or {}).items()), dockerfile
    ), bound


# --- parse_reference -------------------------------------------------------


def test_reference_with_tag() -> None:
    """``python:3.12-slim`` is a path with a tag."""
    assert parse_reference("python:3.12-slim") == Reference(
        domain=None, path="python", tag="3.12-slim", digest=None
    )


def test_reference_port_colon_is_not_a_tag() -> None:
    """``registry:5000/app``: the colon belongs to the domain, no tag."""
    assert parse_reference("registry:5000/app") == Reference(
        domain="registry:5000", path="app", tag=None, digest=None
    )


def test_reference_port_and_tag() -> None:
    """``registry:5000/app:1`` has both a port and a tag."""
    assert parse_reference("registry:5000/app:1") == Reference(
        domain="registry:5000", path="app", tag="1", digest=None
    )


def test_reference_digest() -> None:
    """``app@sha256:<64 hex>`` carries a digest and no tag."""
    assert parse_reference(f"app@{_DIGEST}") == Reference(
        domain=None, path="app", tag=None, digest=_DIGEST
    )


@pytest.mark.parametrize(
    "token",
    [
        "Python:3",  # uppercase path component
        "app:",  # empty tag
        "app@sha256:abc",  # short digest
        "app@md5:" + "a" * 64,  # unmodelled algorithm
        "-slim",
        "a//b",
        "app:-x",  # tag must start with a word character
        "app:" + "a" * 129,  # tag longer than 128
        "",
        "$IMAGE:1",
    ],
)
def test_reference_invalid(token: str) -> None:
    """Tokens outside the reference grammar give ``None``."""
    assert parse_reference(token) is None


def test_reference_nested_path_and_domain() -> None:
    """A dotted domain and a multi-component path with tag and digest."""
    ref = parse_reference(f"ghcr.io/org/team/app:v1@{_DIGEST}")
    assert ref == Reference(
        domain="ghcr.io", path="org/team/app", tag="v1", digest=_DIGEST
    )


def test_reference_first_component_without_dot_is_a_path() -> None:
    """``library/python:3``: Docker treats ``library`` as a path, not a domain."""
    assert parse_reference("library/python:3") == Reference(
        domain=None, path="library/python", tag="3", digest=None
    )


# --- stage-name grammar ----------------------------------------------------


@pytest.mark.parametrize(
    ("token", "accepted"),
    [
        ("extra", True),
        ("build-1", True),
        ("a.b_c", True),
        ("a", True),
        ("as", True),  # the grammar accepts it; F1 refuses it separately
        ("AS", False),
        ("As", False),
        ("aS", False),
        ("Builder", False),  # BuildKit lowercases, Buildah keeps the case
        ("EXTRA", False),
        ("1stage", False),
        ("0", False),
        ("-slim", False),
        ("_x", False),
        (".x", False),
        ("a$b", False),
        ("a\\b", False),
        ('"a"', False),
        ("\u212a", False),  # Kelvin sign: Go lowercases it to "k"
        ("caf\u00e9", False),
        ("extra\n", False),
        ("", False),
    ],
)
def test_stage_name_grammar(token: str, accepted: bool) -> None:
    """The BuildKit ∩ Buildah table of docs/fix-stage-name-grammar.md."""
    assert is_stage_name(token) is accepted
    assert (STAGE_NAME_RE.fullmatch(token) is not None) is accepted


# --- F1 --------------------------------------------------------------------


def test_f1_run5() -> None:
    """R's run-5: ``FROM python:3.12-slim extra`` gains ``AS``."""
    dockerfile = b"FROM python:3.12-slim extra\nRUN true\n"
    result, bound = _propose(dockerfile)
    assert isinstance(result, FromFix)
    assert result.transformation == "F1"
    assert result.replacement == b"FROM python:3.12-slim AS extra\n"
    assert result.conditions
    assert (
        link_problem(dockerfile, splice(dockerfile, bound, result.replacement), bound)
        is None
    )


def test_f1_keeps_every_byte_crlf_platform_and_continuation() -> None:
    """Flags, spacing, CRLF and a continuation are kept byte for byte."""
    dockerfile = b"from  --platform=$BUILDPLATFORM \\\r\n\tpython:3.12\t extra\r\n"
    result, _ = _propose(dockerfile)
    assert isinstance(result, FromFix)
    assert result.replacement == (
        b"from  --platform=$BUILDPLATFORM \\\r\n\tpython:3.12\t AS extra\r\n"
    )


def test_f1_digest_reference() -> None:
    """A digest alone makes the reference complete."""
    dockerfile = f"FROM app@{_DIGEST} extra".encode()
    result, _ = _propose(dockerfile)
    assert isinstance(result, FromFix)
    assert result.replacement == f"FROM app@{_DIGEST} AS extra".encode()


def test_f1_port_and_tag() -> None:
    """``registry:5000/app:1 extra`` is complete: F1."""
    result, _ = _propose(b"FROM registry:5000/app:1 extra\n")
    assert isinstance(result, FromFix)
    assert result.replacement == b"FROM registry:5000/app:1 AS extra\n"


# --- F2 --------------------------------------------------------------------


@pytest.mark.parametrize("dangling", ["AS", "as", "As"])
def test_f2_dangling_as(dangling: str) -> None:
    """A dangling ``AS`` in any case is removed with the space before it."""
    dockerfile = f"FROM python:3.12-slim {dangling}\nRUN true\n".encode()
    result, bound = _propose(dockerfile)
    assert isinstance(result, FromFix)
    assert result.transformation == "F2"
    assert result.replacement == b"FROM python:3.12-slim\n"
    assert (
        link_problem(dockerfile, splice(dockerfile, bound, result.replacement), bound)
        is None
    )


def test_f2_keeps_trailing_bytes() -> None:
    """Only the ``AS`` token and the whitespace before it go."""
    result, _ = _propose(b"FROM --platform=linux/amd64 python:3 \t AS  \n")
    assert isinstance(result, FromFix)
    assert result.replacement == b"FROM --platform=linux/amd64 python:3  \n"


# --- refusals --------------------------------------------------------------


@pytest.mark.parametrize(
    ("dockerfile", "reason"),
    [
        (b"FROM python 3.12\n", "no tag or digest"),
        (b"FROM python builder\n", "no tag or digest"),
        (b"FROM registry:5000/app extra\n", "no tag or digest"),
        (b"FROM python:3.12 -slim\n", "stage-name grammar"),
        (b"FROM python:3.12 Extra\n", "stage-name grammar"),
        (b"FROM python:3.12 as\n", "F2"),  # sanity: handled by F2, not F1
        (b"FROM python:3.12 a b c\n", "argument"),
        (b"FROM python:3.12 x y\n", "argument"),
        (b"FROM --foo=x python:3.12 extra\n", "flag"),
        (b"FROM $IMAGE:1 extra\n", "substitution"),
        (b"FROM python:$TAG extra\n", "substitution"),
        (b"FROM Python:3 extra\n", "not a valid reference"),
    ],
)
def test_refusals(dockerfile: bytes, reason: str) -> None:
    """Each no-proposal branch of design §4.2 names its reason."""
    result, _ = _propose(dockerfile)
    if reason == "F2":
        assert isinstance(result, FromFix) and result.transformation == "F2"
        return
    assert isinstance(result, str)
    assert reason in result


def test_refusal_collision_case_insensitive() -> None:
    """An existing stage named like the token (any case) → no proposal."""
    dockerfile = b"FROM alpine:3 AS Extra\nFROM python:3.12 extra\n"
    result, _ = _propose(dockerfile, line=2)
    assert isinstance(result, str)
    assert "collides" in result


@pytest.mark.parametrize(
    "other",
    [
        b"COPY --from=extra /a /b\n",
        b"COPY --from=EXTRA /a /b\n",
        b"RUN --mount=type=cache,from=extra,target=/x true\n",
        b"FROM extra\n",
        b"FROM --platform=linux/amd64 extra AS final\n",
        b"ONBUILD COPY --from=extra /a /b\n",
    ],
)
def test_refusal_token_referenced(other: bytes) -> None:
    """A name used as a reference or a ``--from`` anywhere → no proposal."""
    dockerfile = b"FROM python:3.12-slim extra\n" + other
    result, _ = _propose(dockerfile)
    assert isinstance(result, str)
    assert "referenced" in result


def test_refusal_unresolvable_from_value() -> None:
    """A ``--from=$X`` could name the token: not checkable → no proposal."""
    dockerfile = b"FROM python:3.12-slim extra\nCOPY --from=$STAGE /a /b\n"
    result, _ = _propose(dockerfile)
    assert isinstance(result, str)
    assert "substitution" in result


def test_refusal_build_arg_names_token() -> None:
    """A build arg of R's configuration whose value is the token."""
    result, _ = _propose(
        b"FROM python:3.12-slim extra\n", build_args={"STAGE": "extra"}
    )
    assert isinstance(result, str)
    assert "build arg" in result


def test_refusal_earlier_value_of_a_duplicate_build_arg() -> None:
    """Ruling S: a name given twice is checked for both values, so an
    earlier value naming the token refuses even when the last does not."""
    dockerfile = b"FROM python:3.12-slim extra\n"
    parsed = parse(_as_r_reads(dockerfile))
    bound = bind_instruction(
        dockerfile,
        Defect.model_validate(
            {
                "class": "from_argument_count",
                "file": "Dockerfile",
                "lines": (1, 1),
                "object": parsed.instructions[0].text,
            }
        ),
    )
    assert isinstance(bound, Bound)
    pairs = (("STAGE", "extra"), ("STAGE", "other"))
    result = propose_from(parsed, bound, pairs, dockerfile)
    assert result == "build arg 'STAGE' names 'extra'"


def test_unrelated_build_arg_allows_f1() -> None:
    """A build arg with another value does not block F1."""
    result, _ = _propose(b"FROM python:3.12-slim extra\n", build_args={"PY": "3.12"})
    assert isinstance(result, FromFix)


def test_refusal_not_from() -> None:
    """A bound instruction that is not FROM is refused."""
    dockerfile = b"FROM python:3.12\nRUN a b\n"
    parsed = parse(_as_r_reads(dockerfile))
    bound = Bound(
        ordinal=1,
        lines=(2, 2),
        original=b"RUN a b\n",
        instruction=parsed.instructions[1],
    )
    result = propose_from(parsed, bound, (), dockerfile)
    assert isinstance(result, str)
    assert "not a FROM" in result


def test_refusal_single_argument() -> None:
    """A one-argument FROM has nothing to fix."""
    result, _ = _propose(b"FROM python:3.12\n")
    assert isinstance(result, str)


def test_refusal_comment_inside_continuation() -> None:
    """A comment line inside the instruction could hold the token's bytes."""
    dockerfile = b"FROM python:3.12 \\\n# x extra\n  extra\n"
    result, _ = _propose(dockerfile)
    assert isinstance(result, str)
    assert "comment" in result


# --- fix round 1 regressions -----------------------------------------------


@pytest.mark.parametrize(
    ("dockerfile", "reason"),
    [
        # BuildKit joins continuations with no separator: img:1extra.
        (b"FROM python:3.12-slim\\\nextra\n", "continuation"),
        (b"FROM img:1 extra\\\n\n", "continuation"),
        (b"FROM --platform=linux/amd64\\\nimg:1 extra\n", "continuation"),
        # The token opening a continuation line is refused, not modelled.
        (b"FROM python:3.12-slim \\\nextra\n", "same line"),
        (b"FROM img:1 \\\r\n\textra\r\n", "same line"),
    ],
)
def test_refusal_continuation_join(dockerfile: bytes, reason: str) -> None:
    """Critical 1: no F1 whose bytes BuildKit would join differently."""
    result, _ = _propose(dockerfile)
    assert isinstance(result, str)
    assert reason in result


@pytest.mark.parametrize(
    "other",
    [
        b"COPY\t--from=extra /a /b\n",
        b"FROM\textra\n",
        b"RUN\t--mount=type=bind,from=extra true\n",
        b"RUN\x0b--mount=type=bind,from=extra true\n",
    ],
)
def test_refusal_keyword_not_space_separated(other: bytes) -> None:
    """Critical 2: R's keyword split on a space only hides references."""
    result, _ = _propose(b"FROM img:1 extra\n" + other)
    assert isinstance(result, str)
    assert "not separated by a space" in result


@pytest.mark.parametrize(
    "mount",
    [
        b'"from=extra"',
        b"'from=extra'",
        b"from=ext\\ra",
        b'"from=other"',
    ],
)
def test_refusal_quoted_mount(mount: bytes) -> None:
    """Critical 3: BuildKit reads mounts as CSV, quotes and escapes included."""
    dockerfile = (
        b"FROM python:3.12-slim extra\nRUN --mount=type=bind,"
        + mount
        + b",target=/x true\n"
    )
    result, _ = _propose(dockerfile)
    assert isinstance(result, str)
    assert "quoted or escaped" in result


def test_mount_from_other_stage_allows_f1() -> None:
    """An unquoted mount naming another stage does not block F1."""
    dockerfile = b"FROM python:3.12-slim extra\nRUN --mount=from=other,target=/x true\n"
    result, _ = _propose(dockerfile)
    assert isinstance(result, FromFix)


def test_guard_condition_says_r_reading_only() -> None:
    """Important 4: the re-read condition claims R's reading, no more."""
    result, _ = _propose(b"FROM python:3.12-slim extra\n")
    assert isinstance(result, FromFix)
    guard = result.conditions[-1]
    assert "R's parser" in guard and "not the builders'" in guard


@pytest.mark.parametrize("token", ["scratch", "context"])
def test_refusal_reserved_names(token: str) -> None:
    """Minor 5: names BuildKit gives a meaning of its own."""
    result, _ = _propose(f"FROM img:1 {token}\n".encode())
    assert isinstance(result, str)
    assert "reserves" in result


def test_f2_with_trailing_continuation() -> None:
    """F2 after a blank-preceded continuation keeps the continuation."""
    result, _ = _propose(b"FROM img:1 aS \\\n\n")
    assert isinstance(result, FromFix)
    assert result.replacement == b"FROM img:1 \\\n"


def test_f2_refused_when_as_opens_continuation_line() -> None:
    """A dangling AS alone on a continuation line is not located."""
    result, _ = _propose(b"FROM img:1 \\\n  AS\n")
    assert isinstance(result, str)


# --- fix round 2 regressions -----------------------------------------------


@pytest.mark.parametrize(
    "other",
    [
        b"COPY --from=ext\\\nra /a /b\n",
        b"RUN --mount=type=bind,from=ext\\\nra true\n",
        b"ONBUILD COPY --from=ext\\\nra /a /b\n",
        b"FROM ext\\\nra\n",
    ],
)
def test_refusal_join_in_other_instruction(other: bytes) -> None:
    """BuildKit's no-separator join rebuilds ``extra`` in another instruction."""
    result, _ = _propose(b"FROM img:1 extra\n" + other)
    assert isinstance(result, str)
    assert "line 2: a line continuation" in result


def test_blank_continuation_elsewhere_allows_f1() -> None:
    """A continuation after a blank anywhere reads alike: F1 still proposed."""
    result, _ = _propose(b"FROM img:1 extra\nRUN a \\\n  b\n")
    assert isinstance(result, FromFix)


def test_refusal_escape_directive() -> None:
    """A non-default ``# escape=`` makes R's split untrusted: no proposal."""
    result, _ = _propose(b"# escape=`\nFROM img:1 extra \\\n\n", line=2)
    assert isinstance(result, str)
    assert "escape directive" in result
