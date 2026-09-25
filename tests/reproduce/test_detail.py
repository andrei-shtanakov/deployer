"""R's detailed check records (F §6.2) and the parity of R's aggregate output."""

import json
from pathlib import Path

import pytest

from deployer.reproduce import dockerfile, ignore
from deployer.reproduce.detail import (
    CheckRecord,
    CheckRun,
    copy_source_records,
    syntax_records,
)
from tests.reproduce.detail_cases import (
    GOLDEN,
    SYNTHETIC,
    UNSUPPORTED_IGNORE,
    golden,
    make_context,
    tree_cases,
)

EXPECTED = json.loads(GOLDEN.read_text())


def _json(value: object) -> object:
    """Tuples read back from JSON as lists: compare in that one form."""
    return json.loads(json.dumps(value))


def _expected(key: str) -> tuple[object, object]:
    entry = EXPECTED[key]
    return entry["copy_sources"], entry["syntax"]


@pytest.mark.parametrize(("key", "tree", "path"), tree_cases())
def test_outputs_unchanged_for_trees(key: str, tree: Path, path: Path) -> None:
    assert _json(golden(tree, path.read_text())) == _json(_expected(key))


@pytest.mark.parametrize("case", sorted(SYNTHETIC))
def test_outputs_unchanged(case: str, tmp_path: Path) -> None:
    ctx = make_context(tmp_path / case, case)
    assert _json(golden(ctx, SYNTHETIC[case])) == _json(_expected(f"synthetic:{case}"))


def test_golden_covers_every_case() -> None:
    keys = {key for key, _, _ in tree_cases()} | {f"synthetic:{c}" for c in SYNTHETIC}
    assert keys == set(EXPECTED)
    assert len(tree_cases()) >= 51


def _copy_run(tmp_path: Path, text: str, ignore_text: str = "") -> CheckRun:
    ctx = tmp_path / "ctx"
    ctx.mkdir(parents=True)
    (ctx / "present.txt").write_text("x\n")
    if ignore_text:
        (ctx / ".dockerignore").write_text(ignore_text)
    rules = ignore.load_rules(ctx, ignore.ci_ignore_file(ctx, "Dockerfile"))
    return copy_source_records(dockerfile.parse(text), ctx, "Dockerfile", rules)


def _by_subject(run: CheckRun) -> dict[str, CheckRecord]:
    return {r.subject: r for r in run.records}


def test_one_record_per_source(tmp_path: Path) -> None:
    run = _copy_run(tmp_path, "FROM a:1\nCOPY present.txt missing.txt /d/\n")
    assert run.file_status == "ran"
    assert [(r.subject, r.status) for r in run.records] == [
        ("present.txt", "passed"),
        ("missing.txt", "failed"),
    ]
    missing = _by_subject(run)["missing.txt"]
    assert missing.finding is not None
    assert missing.finding.finding == "source missing.txt absent from the context"
    assert missing.reason == "source missing.txt absent from the context"
    assert missing.ordinal == 1 and missing.lines == (2, 2)


def test_passed_sources_survive_a_failure(tmp_path: Path) -> None:
    run = _copy_run(tmp_path, "FROM a:1\nCOPY present.txt missing.txt /d/\n")
    present = _by_subject(run)["present.txt"]
    assert present.status == "passed"
    assert present.reason is None and present.finding is None
    assert present.ordinal == 1 and present.lines == (2, 2)


def test_copy_skips_and_subjects(tmp_path: Path) -> None:
    text = (
        "FROM a:1\nADD https://x/y /d\nCOPY ./sub/../present.txt $X /d/\n"
        "COPY --from=b /a /b\nCOPY *.txt /d/\n"
    )
    run = _copy_run(tmp_path, text)
    assert [(r.ordinal, r.subject, r.status, r.reason) for r in run.records] == [
        (1, "https://x/y", "skipped", "ADD at line 2: remote ADD source https://x/y"),
        (2, "present.txt", "passed", None),
        (2, "$X", "skipped", "COPY at line 3: source pattern not modelled: $X"),
        (
            3,
            "*",
            "skipped",
            "COPY at line 4: --from source is checked in its stage or image, "
            "not the context",
        ),
        (4, "*.txt", "passed", None),
    ]


def test_file_wide_skip_propagates(tmp_path: Path) -> None:
    text = "# escape=`\nFROM a b\nCOPY present.txt $X /d/\nCOPY --from=x /a /b\n"
    run = _copy_run(tmp_path, text)
    reason = "Dockerfile not fully read (escape directive not modelled: `)"
    assert run.file_status == "skipped" and run.file_reason == reason
    assert [(r.ordinal, r.subject, r.status, r.reason) for r in run.records] == [
        (1, "present.txt", "skipped", reason),
        (1, "$X", "skipped", reason),
        (2, "*", "skipped", reason),
    ]
    runs = syntax_records(dockerfile.parse(text), "Dockerfile")
    syntax_reason = "escape directive not modelled: `"
    assert [r.check_id for r in runs] == [
        "syntax_first_from",
        "syntax_from_args",
        "syntax_keyword",
        "syntax_continuation",
    ]
    expected_ordinals = {
        "syntax_first_from": [0],
        "syntax_from_args": [0],
        "syntax_keyword": [0, 1, 2],
        "syntax_continuation": [2],
    }
    for syntax_run in runs:
        assert syntax_run.file_status == "skipped"
        assert syntax_run.file_reason == syntax_reason
        assert [r.ordinal for r in syntax_run.records] == expected_ordinals[
            syntax_run.check_id
        ]
        assert {(r.status, r.reason) for r in syntax_run.records} == {
            ("skipped", syntax_reason)
        }


def test_unsupported_ignore_propagates(tmp_path: Path) -> None:
    text = "FROM a:1\nCOPY present.txt missing.txt /d/\nCOPY <<EOF /d\nx\nEOF\n"
    run = _copy_run(tmp_path, text, UNSUPPORTED_IGNORE)
    reason = "ignore pattern not modelled: [ab]"
    assert run.file_status == "skipped" and run.file_reason == reason
    assert [(r.subject, r.status, r.reason) for r in run.records] == [
        ("present.txt", "skipped", reason),
        ("missing.txt", "skipped", reason),
        ("*", "skipped", reason),
    ]


def _syntax_run(text: str, check_id: str) -> CheckRun:
    runs = syntax_records(dockerfile.parse(text), "Dockerfile")
    return next(r for r in runs if r.check_id == check_id)


def test_syntax_per_instruction() -> None:
    run = _syntax_run("FROM a b\nFROM c:1\n", "syntax_from_args")
    assert run.file_status == "ran"
    assert [(r.ordinal, r.lines, r.subject, r.status) for r in run.records] == [
        (0, (1, 1), "from_args", "failed"),
        (1, (2, 2), "from_args", "passed"),
    ]
    failed = run.records[0]
    assert failed.finding is not None
    assert failed.finding.finding == (
        "syntax error at line 1: FROM takes one or three arguments"
    )
    assert failed.reason == "FROM takes one or three arguments"
    directive = _syntax_run(
        "# syntax=docker/dockerfile:1\nFROM a b\nFROM c:1\n", "syntax_from_args"
    )
    assert [r.status for r in directive.records] == ["observation", "passed"]


def test_syntax_keyword_and_continuation_records() -> None:
    text = "FROM a\nRUNN x\nRUN y \\\n"
    keyword = _syntax_run(text, "syntax_keyword")
    assert [(r.ordinal, r.status) for r in keyword.records] == [
        (0, "passed"),
        (1, "failed"),
        (2, "passed"),
    ]
    continuation = _syntax_run(text, "syntax_continuation")
    assert [(r.ordinal, r.status) for r in continuation.records] == [(2, "failed")]
    clean = _syntax_run("FROM a\nRUN y\n", "syntax_continuation")
    assert [(r.ordinal, r.status) for r in clean.records] == [(1, "passed")]


def test_first_from_on_first_non_arg() -> None:
    run = _syntax_run("ARG V=1\nRUN x\nFROM a\n", "syntax_first_from")
    assert [(r.ordinal, r.subject, r.status) for r in run.records] == [
        (1, "first_from", "failed")
    ]
    ok = _syntax_run("ARG V=1\nFROM a\n", "syntax_first_from")
    assert [(r.ordinal, r.status) for r in ok.records] == [(1, "passed")]


def test_empty_file_first_from() -> None:
    runs = syntax_records(dockerfile.parse(""), "Dockerfile")
    first = next(r for r in runs if r.check_id == "syntax_first_from")
    assert len(first.records) == 1
    record = first.records[0]
    assert record.ordinal is None and record.lines is None
    assert record.status == "failed"
    assert record.finding is not None and record.finding.location is not None
    assert record.finding.location.lines == (1, 1)
    assert all(not r.records for r in runs if r.check_id != "syntax_first_from")


def test_no_copy_ran_vs_skipped(tmp_path: Path) -> None:
    text = "FROM a:1\nRUN true\n"
    assert _copy_run(tmp_path / "a", text) == CheckRun("copy_sources", "ran", None, [])
    assert _copy_run(tmp_path / "b", text, UNSUPPORTED_IGNORE) == CheckRun(
        "copy_sources", "skipped", "ignore pattern not modelled: [ab]", []
    )
