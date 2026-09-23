"""§2: two documented lint forms, an ordered table, the every-line rule."""

from pathlib import Path

import pytest

from deployer.models import ContainerRuntime
from deployer.reproduce.buildcheck import (
    BuilderSyntax,
    merge_syntax,
    read_check_output,
    run_builder_check,
)
from deployer.reproduce.model import Location, ReproductionCheck, ReproEvidence
from tests.reproduce.conftest import proc

OUT = Path(__file__).parent.parent / "fixtures" / "reproduction" / "check-outputs"


def _recorded(name: str) -> tuple[int, str]:
    text = (OUT / name).read_text()
    if text.startswith("# exit="):
        first, _, rest = text.partition("\n")
        return int(first.removeprefix("# exit=")), rest
    return 1, text  # the two documentation copies: nonzero per Docker's docs


@pytest.mark.parametrize("name", ["docs-lint.txt", "warning-prefixed-lint.txt"])
def test_both_documented_lint_forms_are_row_4(name):
    code, text = _recorded(name)
    syntax, lint = read_check_output(code, None, text)
    assert syntax.state == "passed"
    assert [c.status for c in lint] == ["observation"]


def test_parse_error_is_row_2():
    code, text = _recorded("parse-error.txt")
    syntax, _ = read_check_output(code, None, text)
    assert (syntax.state, syntax.line) == ("error", 1)


@pytest.mark.parametrize("name", ["builder-unreachable.txt", "lint-then-error.txt"])
def test_unrecognised_and_mixed_output_is_row_5(name):
    code, text = _recorded(name)
    syntax, _ = read_check_output(code, None, text)
    assert syntax.state == "skipped"
    assert syntax.reason == "build check output not recognised"


def test_rows_1_and_3():
    assert read_check_output(None, "timeout", "")[0].state == "skipped"
    assert read_check_output(0, None, "")[0].state == "passed"


def test_podman_has_no_check(fake_containers):
    syntax, lint, buildx = run_builder_check(
        ContainerRuntime(tool="podman"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, syntax.reason, lint, buildx) == (
        "skipped",
        "backend has no build check",
        [],
        None,
    )
    assert fake_containers.calls == []


def test_old_buildx_is_skipped(fake_containers):
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.14.1 abc\n"
    )
    syntax, _, buildx = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, buildx) == ("skipped", "0.14.1")


def _parser_finding(line: int) -> ReproductionCheck:
    return ReproductionCheck(
        check_id="syntax_from_args",
        status="failed",
        finding=f"syntax error at line {line}: FROM takes one or three arguments",
        location=Location(file="Dockerfile", lines=(line, line)),
        evidence=[ReproEvidence(kind="log_excerpt", text="parser")],
    )


def test_merge_same_line_cites_both():
    merged = merge_syntax(
        [_parser_finding(1)], BuilderSyntax("error", 1, "x", None), "Dockerfile"
    )
    failed = [c for c in merged if c.status == "failed"]
    assert len(failed) == 1 and len(failed[0].evidence) == 2


def test_merge_builder_only_adds_its_finding():
    merged = merge_syntax([], BuilderSyntax("error", 3, "bad", None), "Dockerfile")
    assert [c.finding for c in merged if c.status == "failed"] == ["line 3: bad"]


def test_merge_parser_only_with_builder_passed_is_inconclusive():
    merged = merge_syntax(
        [_parser_finding(1)], BuilderSyntax("passed", None, None, None), "Dockerfile"
    )
    assert [c.status for c in merged if c.check_id == "syntax_from_args"] == [
        "inconclusive"
    ]


def test_merge_builder_skipped_keeps_the_parser_result_and_says_so():
    merged = merge_syntax(
        [_parser_finding(1)],
        BuilderSyntax("skipped", None, None, "backend has no build check"),
        "Dockerfile",
    )
    assert [c.status for c in merged] == ["failed", "skipped"]
    assert merged[1].check_id == "builder_check"
