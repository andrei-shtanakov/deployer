"""Ownership by content (spec §3, §4) on ``steps-1`` and its derivations."""

import json

from deployer.stepbinding import (
    StepDirectory,
    StepFile,
    bind_jobs,
    comparison_lines,
)
from tests import step_binding_data as data

TS = "2026-10-03T10:08:54.1234567Z "


def _steps1() -> tuple[dict[int, str], dict[int, list[int]], dict[str, int]]:
    records = data.job_records()
    ids = {name: record["id"] for name, record in records.items()}
    logs = {ids[name]: text for name, text in data.job_logs().items()}
    numbers = {r["id"]: [s["number"] for s in r["steps"]] for r in records.values()}
    return logs, numbers, ids


def test_comparison_lines_drop_one_bom_split_on_newline_and_strip_timestamps() -> None:
    text = f"\ufeff{TS}a\n{TS}\ufeffb\r\n{TS}c"
    assert comparison_lines(text) == ["a", "\ufeffb\r", "c"]
    assert comparison_lines(f"{TS}a\n") == ["a"]
    assert comparison_lines("") == []
    assert comparison_lines("2026-10-03T10:08:54Z a\n") == ["2026-10-03T10:08:54Z a"]


def test_every_recorded_job_is_bound_and_every_line_lands_at_its_owner() -> None:
    logs, numbers, ids = _steps1()
    result = bind_jobs(logs, numbers, data.directories())
    assert {r.state for r in result.values()} == {"bound"}
    for job_id, binding in result.items():
        assert binding.spans[0].start == 0
        assert all(a.end == b.start for a, b in zip(binding.spans, binding.spans[1:]))
        assert binding.spans[-1].end == len(comparison_lines(logs[job_id]))
    observed = json.loads((data.STEPS_1 / "observed.json").read_text())["lines"]
    for row in observed:
        index = row["job_log_line"] - 1
        spans = result[ids[row["job"]]].spans
        (owner,) = [s.number for s in spans if s.start <= index < s.end]
        assert owner == row["zip_step"], row


def test_renumbering_that_reorders_the_files_is_unmatched() -> None:
    logs, numbers, ids = _steps1()
    entries = data.renamed(
        data.archive_entries(), "s1-plain-fail/3_", "s1-plain-fail/7_"
    )
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "unmatched"
    assert result[ids["s2-named"]].state == "bound"


def test_a_number_outside_the_api_listing_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    entries = data.renamed(
        data.archive_entries(), "s1-plain-fail/9_", "s1-plain-fail/99_"
    )
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "malformed"
    assert "99" in (result[ids["s1-plain-fail"]].reason or "")


def test_a_repeated_api_step_number_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    numbers[ids["s1-plain-fail"]] = [*numbers[ids["s1-plain-fail"]], 4]
    result = bind_jobs(logs, numbers, data.directories())
    assert result[ids["s1-plain-fail"]].state == "malformed"


def test_identical_job_logs_make_both_jobs_ambiguous() -> None:
    logs, numbers, ids = _steps1()
    logs[ids["s5-spoof"]] = logs[ids["s6-dupes"]]
    result = bind_jobs(logs, numbers, data.directories())
    assert result[ids["s5-spoof"]].state == "ambiguous"
    assert result[ids["s6-dupes"]].state == "ambiguous"
    assert result[ids["s1-plain-fail"]].state == "bound"


def test_two_directories_matching_one_job_make_it_ambiguous() -> None:
    logs, numbers, ids = _steps1()
    entries = data.archive_entries()
    copy = [
        ("copy-of-s1/" + name.partition("/")[2], body)
        for name, body in entries
        if name.startswith("s1-plain-fail/")
    ]
    result = bind_jobs(logs, numbers, data.directories(entries + copy))
    assert result[ids["s1-plain-fail"]].state == "ambiguous"


def test_a_missing_step_file_is_unmatched() -> None:
    logs, numbers, ids = _steps1()
    entries = data.without(data.archive_entries(), "s2-named/3_")
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s2-named"]].state == "unmatched"
    assert result[ids["s3-two-failures"]].state == "bound"


def test_a_step_file_without_a_final_newline_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    entries = data.edited(
        data.archive_entries(), "s1-plain-fail/9_", lambda b: b.removesuffix(b"\n")
    )
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "malformed"


def test_a_foreign_line_break_in_the_job_log_is_malformed() -> None:
    logs, numbers, ids = _steps1()
    job = ids["s1-plain-fail"]
    logs[job] = logs[job].replace("MARK-s1-b", "MARK-s1-b\x85x", 1)
    entries = data.edited(
        data.archive_entries(),
        "s1-plain-fail/4_",
        lambda b: b.replace(b"MARK-s1-b", "MARK-s1-b\x85x".encode(), 1),
    )
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[job].state == "malformed"


def test_an_archive_of_another_runner_matches_nothing() -> None:
    logs, numbers, _ = _steps1()
    result = bind_jobs(
        logs, numbers, data.directories(data.foreign_runner(data.archive_entries()))
    )
    assert {r.state for r in result.values()} == {"unmatched"}


def test_an_untimestamped_line_is_compared_as_is() -> None:
    log = f"{TS}##[group]Run a\n  continuation\n{TS}##[endgroup]\n{TS}out\n"
    files = (StepFile(3, log),)
    result = bind_jobs({1: log}, {1: [1, 3]}, [StepDirectory("any", files)])
    assert result[1].state == "bound"


def test_the_directory_name_proves_nothing() -> None:
    logs, numbers, ids = _steps1()
    entries = data.renamed(data.archive_entries(), "s1-plain-fail/", "zz/")
    result = bind_jobs(logs, numbers, data.directories(entries))
    assert result[ids["s1-plain-fail"]].state == "bound"
