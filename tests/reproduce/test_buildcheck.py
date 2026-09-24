"""§2: two documented lint forms, an ordered table, the every-line rule."""

import subprocess
from pathlib import Path

import pytest

from deployer.models import ContainerRuntime
from deployer.reproduce.buildcheck import (
    BuilderSyntax,
    CheckRun,
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
    assert syntax.text == text


def test_rows_1_and_3():
    assert read_check_output(None, "timeout", "")[0].state == "skipped"
    assert read_check_output(0, None, "")[0].state == "passed"


def test_podman_has_no_check(fake_containers):
    syntax, lint, buildx, check_run = run_builder_check(
        ContainerRuntime(tool="podman"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, syntax.reason, lint, buildx, check_run) == (
        "skipped",
        "backend has no build check",
        [],
        None,
        None,
    )
    assert fake_containers.calls == []


def test_old_buildx_is_skipped(fake_containers):
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.14.1 abc\n"
    )
    syntax, _, buildx, check_run = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, buildx, check_run) == ("skipped", "0.14.1", None)


def test_build_check_timeout_is_skipped(fake_containers):
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.15.1 abc\n"
    )
    fake_containers.responses[("build", "--check")] = subprocess.TimeoutExpired(
        cmd="docker build --check", timeout=60
    )
    syntax, _, buildx, check_run = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, syntax.reason, buildx, check_run) == (
        "skipped",
        "timeout",
        "0.15.1",
        None,
    )


def test_launched_check_returns_its_raw_stdout_and_stderr(fake_containers):
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.15.1 abc\n"
    )
    fake_containers.responses[("build", "--check")] = proc(
        0, stdout="out text", stderr="err text"
    )
    syntax, _, _, check_run = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    assert syntax.state == "passed"
    assert check_run == CheckRun("out text", "err text")


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
    assert merged[1].evidence == []


def test_merge_builder_skipped_with_raw_output_attaches_it_as_evidence():
    merged = merge_syntax(
        [],
        BuilderSyntax(
            "skipped", None, "raw output here", "build check output not recognised"
        ),
        "Dockerfile",
    )
    skipped = [c for c in merged if c.check_id == "builder_check"][0]
    assert skipped.status == "skipped"
    assert skipped.evidence == [
        ReproEvidence(kind="output_file", path="check.stdout", text="raw output here")
    ]


def test_lint_observations_point_at_the_checked_dockerfile():
    code, text = _recorded("docs-lint.txt")
    _, lint = read_check_output(code, None, text, "docker/Dockerfile.release")
    assert [c.location.file for c in lint if c.location] == [
        "docker/Dockerfile.release"
    ]


def test_a_check_that_cannot_launch_is_skipped(fake_containers):
    """The OSError path of run_builder_check (known minor, now covered)."""
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.15.1 abc\n"
    )
    fake_containers.responses[("build", "--check")] = OSError("exec format error")
    syntax, lint, buildx, raw = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    assert (syntax.state, lint, buildx, raw) == ("skipped", [], "0.15.1", None)
    assert "exec format error" in (syntax.reason or "")


def test_builder_evidence_names_the_stream_that_held_the_diagnostic(fake_containers):
    """A parse error printed on stderr is cited as check.stderr (review of #78)."""
    fake_containers.responses[("buildx", "version")] = proc(
        stdout="github.com/docker/buildx v0.15.1 abc\n"
    )
    fake_containers.responses[("build", "--check")] = proc(
        1, stdout="", stderr="dockerfile parse error on line 1: bad\n"
    )
    syntax, _, _, _ = run_builder_check(
        ContainerRuntime(tool="docker"), Path("/c"), "Dockerfile", 60
    )
    merged = merge_syntax([], syntax, "Dockerfile")
    assert [e.path for c in merged for e in c.evidence] == ["check.stderr"]
