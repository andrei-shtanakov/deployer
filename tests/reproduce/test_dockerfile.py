"""§3.1: line spans and exactly four syntax checks."""

from deployer.reproduce.dockerfile import normalise, parse, syntax_checks

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


def test_from_forms_that_are_valid():
    for line in ("FROM a", "FROM a AS b", "FROM a as b", "FROM --platform=$P a AS b"):
        assert _failed(syntax_checks(parse(line + "\n"), "Dockerfile")) == []


def test_first_instruction_must_be_from_after_args():
    assert _failed(syntax_checks(parse("ARG V=1\nFROM a\n"), "D")) == []
    assert _failed(syntax_checks(parse("RUN x\nFROM a\n"), "D")) == [
        ("syntax_first_from", (1, 1))
    ]


def test_unknown_keyword_and_dangling_continuation():
    checks = syntax_checks(parse("FROM a\nRUNN x\nRUN y \\\n"), "D")
    assert sorted(_failed(checks)) == [
        ("syntax_continuation", (3, 3)),
        ("syntax_keyword", (2, 2)),
    ]


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
    assert _failed(syntax_checks(parse(""), "D")) == [("syntax_first_from", (1, 1))]


def test_normalise():
    assert normalise("RUN  a \\\n   b") == "RUN a b"
