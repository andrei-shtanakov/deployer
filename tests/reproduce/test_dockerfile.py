"""§3.1: line spans and exactly four syntax checks."""

import pytest

from deployer.reproduce.dockerfile import normalise, parse, syntax_checks
from deployer.reproduce.model import ReproEvidence

RUN5 = "FROM python:3.12-slim extra\n\nRUN true\n"


def _failed(checks):
    return [
        (c.check_id, c.location.lines if c.location else None)
        for c in checks
        if c.status == "failed"
    ]


def test_spans_follow_continuations_and_skip_comments():
    text = "# c\nFROM a\n\nRUN one \\\n  # inner comment\n  two\nCMD x\n"
    parsed = parse(text)
    assert [(i.keyword, i.first_line, i.last_line) for i in parsed.instructions] == [
        ("FROM", 2, 2),
        ("RUN", 4, 6),
        ("CMD", 7, 7),
    ]
    assert parsed.instructions[1].text == "RUN one two"


def test_crlf_gives_the_same_lines_as_lf():  # Review Focus 1
    assert parse(RUN5.replace("\n", "\r\n")) == parse(RUN5)
    assert _failed(syntax_checks(parse(RUN5.replace("\n", "\r\n")), "Dockerfile")) == [
        ("syntax_from_args", (1, 1))
    ]


def test_run5_from_with_extra_argument_is_the_only_finding():
    checks = syntax_checks(parse(RUN5), "Dockerfile")
    assert _failed(checks) == [("syntax_from_args", (1, 1))]
    finding = next(c for c in checks if c.status == "failed")
    assert (
        finding.finding == "syntax error at line 1: FROM takes one or three arguments"
    )
    assert finding.evidence == [
        ReproEvidence(kind="log_excerpt", text="FROM python:3.12-slim extra")
    ]


def test_from_forms_that_are_valid():
    for line in ("FROM a", "FROM a AS b", "FROM a as b", "FROM --platform=$P a AS b"):
        assert _failed(syntax_checks(parse(line + "\n"), "Dockerfile")) == []


def test_first_instruction_must_be_from_after_args():
    assert _failed(syntax_checks(parse("ARG V=1\nFROM a\n"), "D")) == []
    checks = syntax_checks(parse("RUN x\nFROM a\n"), "D")
    assert _failed(checks) == [("syntax_first_from", (1, 1))]
    finding = next(c for c in checks if c.status == "failed")
    assert finding.evidence == [ReproEvidence(kind="log_excerpt", text="RUN x")]


def test_unknown_keyword_and_dangling_continuation():
    checks = syntax_checks(parse("FROM a\nRUNN x\nRUN y \\\n"), "D")
    assert sorted(_failed(checks)) == [
        ("syntax_continuation", (3, 3)),
        ("syntax_keyword", (2, 2)),
    ]
    evidence = {c.check_id: c.evidence[0].text for c in checks if c.status == "failed"}
    assert evidence == {"syntax_keyword": "RUNN x", "syntax_continuation": "RUN y"}


def test_parser_directives_and_heredoc_body():
    text = (
        "# syntax=docker/dockerfile:1\nFROM a\nRUN <<EOF\nnot a keyword\nEOF\nCMD x\n"
    )
    parsed = parse(text)
    assert parsed.syntax_directive == "docker/dockerfile:1"
    assert [(i.keyword, i.first_line, i.last_line) for i in parsed.instructions] == [
        ("FROM", 2, 2),
        ("RUN", 3, 5),
        ("CMD", 6, 6),
    ]


def test_external_frontend_turns_findings_into_observations():
    checks = syntax_checks(parse("# syntax=x/y\n" + RUN5), "D")
    assert [c.status for c in checks if c.check_id == "syntax_from_args"] == [
        "observation"
    ]


def test_non_backslash_escape_skips_all_four():
    checks = syntax_checks(parse("# escape=`\nFROM a\n"), "D")
    assert {c.status for c in checks} == {"skipped"}
    assert len(checks) == 4


def test_empty_dockerfile_fails_first_from():
    checks = syntax_checks(parse(""), "D")
    assert _failed(checks) == [("syntax_first_from", (1, 1))]
    finding = next(c for c in checks if c.status == "failed")
    assert finding.evidence == [
        ReproEvidence(kind="log_excerpt", text="empty Dockerfile")
    ]


def test_normalise():
    assert normalise("RUN  a \\\n   b") == "RUN a b"


@pytest.mark.parametrize(
    "line",
    [
        'LABEL marker="<<EOF"',
        "ENV X='<<EOF'",
        'RUN echo "<<EOF"',
    ],
)
def test_a_quoted_or_non_run_heredoc_marker_hides_nothing(line):
    """Only an unquoted `<<` on RUN/COPY/ADD opens a heredoc (review of #77)."""
    checks = syntax_checks(parse(f"FROM a\n{line}\nRUNN x\n"), "Dockerfile")
    assert [
        (c.check_id, c.location.lines if c.location else None)
        for c in checks
        if c.status == "failed"
    ] == [("syntax_keyword", (3, 3))]
