"""§7: identity by line span, a weak signature, one state by a fixed order."""

from pathlib import Path

from deployer.forge import load_snapshot
from deployer.reproduce.compare import (
    Side,
    ci_digests,
    ci_instruction,
    ci_signature,
    compare,
    digest_dimension,
    local_instruction,
    local_signature,
)
from deployer.reproduce.dockerfile import parse, syntax_checks
from deployer.reproduce.model import InstructionRef
from deployer.reproduce.shape import job_text

RUNS = Path(__file__).parent.parent / "fixtures" / "runs"


def _text(name: str) -> str:
    return job_text(load_snapshot((RUNS / f"{name}.json").read_text()).jobs[0])


def test_ci_spans_of_the_committed_snapshots():
    assert ci_instruction(_text("authoring")) == InstructionRef(
        kind="span", lines=(11, 11), bound_by="buildkit_error_block"
    )
    environment_ref = ci_instruction(_text("environment"))
    assert environment_ref is not None and environment_ref.lines == (7, 9)
    project_ref = ci_instruction(_text("project"))
    assert project_ref is not None and project_ref.lines == (15, 15)


def test_ci_signatures():
    assert ci_signature(_text("project")) == "FAILED (failures=1)"
    environment_signature = ci_signature(_text("environment"))
    assert environment_signature is not None
    assert environment_signature.startswith("E: Some index files failed")
    assert ci_signature(_text("authoring")) is None  # COPY: no program output


def test_ci_digests_from_resolve_lines():
    digests = ci_digests(_text("project"))
    assert digests["docker.io/library/python:3.12-slim"].startswith("sha256:")


def test_parse_error_takes_precedence_over_the_block():
    text = "Dockerfile:1\n--------------------\n   1 | >>> FROM a extra\n"
    text += "--------------------\nERROR: failed to build: failed to solve: "
    text += "dockerfile parse error on line 1: FROM requires either one or three arguments\n"
    assert ci_instruction(text) == InstructionRef(
        kind="parse", lines=(1, 1), bound_by="buildkit_parse_error"
    )


def test_two_error_blocks_leave_ci_unbound():
    block = (
        "Dockerfile:3\n--------------------\n   3 | >>> RUN x\n--------------------\n"
    )
    assert ci_instruction(block + block) is None


def test_parse_error_precedence_survives_a_duplicated_message():  # fix round 1, finding 2
    text = "Dockerfile:1\n--------------------\n   1 | >>> FROM a extra\n"
    text += "--------------------\nERROR: failed to build: failed to solve: "
    text += "dockerfile parse error on line 1: FROM requires either one or three arguments\n"
    text += "##[error]buildx failed with: ERROR: failed to solve: "
    text += "dockerfile parse error on line 1: FROM requires either one or three arguments\n"
    assert ci_instruction(text) == InstructionRef(
        kind="parse", lines=(1, 1), bound_by="buildkit_parse_error"
    )


RUN3 = (
    "FROM python:3.12-slim\n\nCOPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/\n\n"
    "WORKDIR /app\n\nCOPY pyproject.toml uv.lock ./\nRUN uv sync --frozen --no-install-project\n\n"
    "COPY src/ci_build ./src/ci_build\n\nRUN uv sync --frozen\n\nCOPY tests ./tests\n"
    "RUN uv run --frozen python -m unittest discover -s tests\n"
)
PODMAN_RUN3 = (
    "STEP 8/9: COPY tests ./tests\n--> abc\n"
    "STEP 9/9: RUN uv run --frozen python -m unittest discover -s tests\n"
    "F\nFAIL: test_greeting_text\nFAILED (failures=1)\n"
)
PODMAN_RUN3_ERR = (
    'Error: building at STEP "RUN uv run --frozen python -m unittest discover -s tests": '
    "while running runtime: exit status 1\n"
)


def test_local_podman_binding_and_signature():
    parsed = parse(RUN3)
    ref = local_instruction(PODMAN_RUN3, PODMAN_RUN3_ERR, "podman", parsed, [])
    assert ref == InstructionRef(kind="span", lines=(15, 15), bound_by="step_text")
    assert (
        local_signature(PODMAN_RUN3, PODMAN_RUN3_ERR, "podman") == "FAILED (failures=1)"
    )


def test_ansi_and_multistage_prefix_still_bind():  # Review Focus 4
    out = "\x1b[1m[2/2] STEP 9/9: RUN uv run --frozen python -m unittest discover -s tests\x1b[0m\n"
    out += "FAILED (failures=1)\n"
    ref = local_instruction(out, "Error: exit status 1\n", "podman", parse(RUN3), [])
    assert ref is not None and ref.lines == (15, 15)


def test_podman_parse_error_binds_only_with_keyword_and_single_finding():
    parsed = parse("FROM python:3.12-slim extra\n\nRUN true\n")
    findings = [c for c in syntax_checks(parsed, "Dockerfile") if c.status == "failed"]
    err = "Error: FROM requires either one argument, or three: FROM <source> [AS <name>]\n"
    assert local_instruction("", err, "podman", parsed, findings) == InstructionRef(
        kind="parse", lines=(1, 1), bound_by="parser_finding_keyword"
    )
    down = "Error: Cannot connect to Podman. Please verify your connection\n"
    assert local_instruction("", down, "podman", parsed, findings) is None


def test_podman_copy_building_at_with_trailing_colons_binds_the_copy():  # fix round 1, finding 1
    parsed = parse("FROM python:3.12-slim\n\nCOPY src/ ./src/\n")
    err = (
        'Error: building at STEP "COPY src/ ./src/": checking on sources under '
        '"/var/tmp/buildah926185718": copier: stat: "/src": no such file or directory\n'
    )
    out = "STEP 2/2: COPY src/ ./src/\n"
    ref = local_instruction(out, err, "podman", parsed, [])
    assert ref == InstructionRef(kind="span", lines=(3, 3), bound_by="step_text")


def test_local_instruction_falls_back_to_step_line_when_building_at_mismatches():
    parsed = parse("FROM python:3.12-slim\n\nRUN true\n")
    err = 'Error: building at STEP "something unmatched": oops\n'
    out = "STEP 2/2: RUN true\n"
    ref = local_instruction(out, err, "podman", parsed, [])
    assert ref == InstructionRef(kind="span", lines=(3, 3), bound_by="step_text")


def _side(lines, sig=None):
    ref = (
        None
        if lines is None
        else InstructionRef(kind="span", lines=lines, bound_by="step_text")
    )
    return Side(ref, sig, None)


def _cmp(
    exit_code=1, launch_error=None, ci=None, local=None, backends=("docker", "podman")
):
    return compare(
        exit_code=exit_code,
        launch_error=launch_error,
        ci=ci or _side((15, 15), "FAILED (failures=1)"),
        local=local or _side((15, 15), "FAILED (failures=1)"),
        parsed=parse(RUN3),
        dimensions={"backend": "differs", "host_arch": "unknown"},
        values={"backend": backends},
    )


def test_state_order():
    assert (
        _cmp(exit_code=None, launch_error="executable not found").state
        == "not_attempted"
    )
    assert _cmp(exit_code=None, launch_error="timeout").reason == "build did not finish"
    assert _cmp(exit_code=0, local=_side(None)).state == "not_reproduced"
    assert _cmp(local=_side(None)).reason == "local failure unbound"
    assert _cmp(ci=_side(None)).reason == "CI instruction unbound"
    assert _cmp(local=_side((12, 12), "x")).state == "different_failure"
    assert _cmp(local=_side((15, 15), None)).reason == "signature unavailable"
    assert _cmp(local=_side((15, 15), "FAILED (errors=1)")).state == (
        "same_instruction_different_output"
    )
    assert _cmp().state == "reproduced_with_differences"


def test_reproduced_is_unreachable_while_ci_arch_is_unknown():
    result = _cmp()
    assert result.state != "reproduced"
    assert result.dimensions["host_arch"] == "unknown"


def test_copy_across_backends_is_not_compared():
    parsed = parse(RUN3)
    ci = Side(
        InstructionRef(kind="span", lines=(10, 10), bound_by="buildkit_error_block"),
        None,
        "ERROR: a",
    )
    local = Side(
        InstructionRef(kind="span", lines=(10, 10), bound_by="step_text"),
        None,
        "Error: b",
    )
    result = compare(
        exit_code=125,
        launch_error=None,
        ci=ci,
        local=local,
        parsed=parsed,
        dimensions={"backend": "differs"},
        values={},
    )
    assert (result.state, result.signature_match) == (
        "reproduced_with_differences",
        "not_compared",
    )


def test_digest_dimension_never_claims_differs():
    images = ["python:3.12-slim"]
    assert (
        digest_dimension(
            {"python:3.12-slim": "sha256:a"}, {"python:3.12-slim": ["sha256:a"]}, images
        )
        == "same"
    )
    assert (
        digest_dimension(
            {"python:3.12-slim": "sha256:a"}, {"python:3.12-slim": ["sha256:b"]}, images
        )
        == "unknown"
    )
    assert digest_dimension({}, {"python:3.12-slim": ["sha256:a"]}, images) == "unknown"
