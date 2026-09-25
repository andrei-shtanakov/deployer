"""Tests for the defect check and no-regression rule (design §6.2, Task 8).

``copy_sources`` records are keyed by position, not by subject — see the
"fix round 1" section below for the two reproduced bugs that ruling fixes.
"""

from typing import cast

import pytest

from deployer.admission.model import DefectClass
from deployer.fix.regress import defect_check_passes, regressions
from deployer.reproduce.detail import COPY_SOURCES, CheckRecord, FileStatus, RecordRun
from deployer.reproduce.model import CheckStatus

_FROM_ARGS = "syntax_from_args"
_KEYWORD = "syntax_keyword"


def _rec(
    check_id: str,
    ordinal: int | None,
    subject: str,
    status: CheckStatus,
    reason: str | None = None,
) -> CheckRecord:
    """A minimal hand-built record; ``lines``/``finding`` are unused here."""
    lines = (1, 1) if ordinal is not None else None
    return CheckRecord(check_id, ordinal, lines, subject, status, reason, None)


def _run(
    check_id: str,
    file_status: FileStatus,
    records: list[CheckRecord],
    file_reason: str | None = None,
) -> RecordRun:
    """A minimal hand-built run."""
    return RecordRun(check_id, file_status, file_reason, records)


# --- defect_check_passes -----------------------------------------------


def test_defect_check_passes_missing_copy_source_ok() -> None:
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "b", "passed")])]
    assert defect_check_passes(after, "missing_copy_source", 0, 0, "b") is None


def test_defect_check_passes_missing_copy_source_failed() -> None:
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [_rec(COPY_SOURCES, 0, "b", "failed", "source not found: b")],
        )
    ]
    reason = defect_check_passes(after, "missing_copy_source", 0, 0, "b")
    assert reason == (
        "copy_sources record for ordinal 0 position 0 is failed: source not found: b"
    )


def test_defect_check_passes_missing_copy_source_subject_mismatch() -> None:
    # The slot at `replaced_position` passed, but not for the intended
    # source: a model or splice bug wrote something else there.
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "c", "passed")])]
    reason = defect_check_passes(after, "missing_copy_source", 0, 0, "b")
    assert reason == (
        "copy_sources record for ordinal 0 position 0 has subject 'c', expected 'b'"
    )


def test_defect_check_passes_file_wide_skip_not_passed() -> None:
    after = [_run(COPY_SOURCES, "skipped", [], "ignore pattern not modelled: *.log")]
    reason = defect_check_passes(after, "missing_copy_source", 0, 0, "b")
    assert reason == (
        "copy_sources skipped file-wide: ignore pattern not modelled: *.log"
    )


def test_defect_check_passes_missing_record() -> None:
    # Only one record at ordinal 0 (position 0); position 1 is out of range.
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "c", "passed")])]
    reason = defect_check_passes(after, "missing_copy_source", 0, 1, "b")
    assert reason == "no copy_sources record at position 1 for ordinal 0"


def test_defect_check_passes_missing_copy_source_no_position() -> None:
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "b", "passed")])]
    reason = defect_check_passes(after, "missing_copy_source", 0, None, "b")
    assert reason == "no replaced position given for a missing_copy_source defect"


def test_defect_check_passes_missing_copy_source_no_new_source() -> None:
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "b", "passed")])]
    reason = defect_check_passes(after, "missing_copy_source", 0, 0, None)
    assert reason == "no replacement source given for a missing_copy_source defect"


def test_defect_check_passes_from_argument_count_ok() -> None:
    # "F1 with syntax_from_args passed".
    after = [_run(_FROM_ARGS, "ran", [_rec(_FROM_ARGS, 2, "from_args", "passed")])]
    assert defect_check_passes(after, "from_argument_count", 2, None, None) is None


@pytest.mark.parametrize("status", ["skipped", "observation", "failed", "inconclusive"])
def test_defect_check_passes_from_argument_count_not_passed(
    status: CheckStatus,
) -> None:
    after = [_run(_FROM_ARGS, "ran", [_rec(_FROM_ARGS, 2, "from_args", status, "why")])]
    reason = defect_check_passes(after, "from_argument_count", 2, None, None)
    assert reason is not None
    assert status in reason


def test_defect_check_passes_from_argument_count_file_wide_skip() -> None:
    after = [_run(_FROM_ARGS, "skipped", [], "Dockerfile not fully read (bad utf8)")]
    reason = defect_check_passes(after, "from_argument_count", 0, None, None)
    assert reason == (
        "syntax_from_args skipped file-wide: Dockerfile not fully read (bad utf8)"
    )


def test_defect_check_passes_unrecognised_class() -> None:
    bogus = cast(DefectClass, "bogus")
    reason = defect_check_passes([], bogus, 0, None, None)
    assert reason == "unrecognised defect class 'bogus'"


# --- regressions ---------------------------------------------------------


def test_regressions_none_when_nothing_changed() -> None:
    before = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "a", "passed")])]
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "a", "passed")])]
    assert regressions(before, after, 0, None, None) == []


def test_regressions_replaced_position_excluded_from_the_rule() -> None:
    # position 0 goes failed -> passed: not itself a regression (it is the
    # fix), and defect_check_passes, not regressions, judges it.
    before = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "old", "failed", "x")])]
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "new", "passed")])]
    assert regressions(before, after, 0, 0, "new") == []
    assert defect_check_passes(after, "missing_copy_source", 0, 0, "new") is None


def test_regressions_on_another_source() -> None:
    # The bound instruction has two sources: position 0 (absent, replaced
    # by "new") and position 1 (untouched, but broken by the fix anyway).
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "old", "failed", "x"),
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
            ],
        )
    ]
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "new", "passed"),
                _rec(COPY_SOURCES, 0, "sibling", "failed", "source not found: sibling"),
            ],
        )
    ]
    lines = regressions(before, after, 0, 0, "new")
    assert len(lines) == 1
    assert "subject='sibling'" in lines[0]
    assert "failed after" in lines[0]


def test_regressions_disappeared_record() -> None:
    before = [
        _run(
            _KEYWORD,
            "ran",
            [
                _rec(_KEYWORD, 0, "keyword", "passed"),
                _rec(_KEYWORD, 1, "keyword", "passed"),
            ],
        )
    ]
    after = [_run(_KEYWORD, "ran", [_rec(_KEYWORD, 0, "keyword", "passed")])]
    lines = regressions(before, after, 0, None, None)
    assert len(lines) == 1
    assert "ordinal=1" in lines[0]
    assert "missing after" in lines[0]


def test_regressions_file_wide_skip_after_regresses_every_record() -> None:
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "a", "passed"),
                _rec(COPY_SOURCES, 1, "b", "failed", "source not found: b"),
            ],
        )
    ]
    after = [_run(COPY_SOURCES, "skipped", [], "ignore pattern not modelled: *.log")]
    lines = regressions(before, after, 0, None, None)
    assert len(lines) == 2
    assert all("skipped file-wide after" in line for line in lines)


def test_regressions_skipped_to_ran_is_not_a_regression() -> None:
    # Subjects are raw before (file-wide skip) and normalised after (ran):
    # not comparable, and running again is progress, not a regression.
    before = [
        _run(
            COPY_SOURCES,
            "skipped",
            [_rec(COPY_SOURCES, 0, "./app", "skipped", "r")],
            "r",
        )
    ]
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "app", "passed")])]
    assert regressions(before, after, 0, None, None) == []


def test_regressions_run_missing_entirely_after() -> None:
    before = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "a", "passed")])]
    lines = regressions(before, [], 0, None, None)
    assert len(lines) == 1
    assert "missing after" in lines[0]


def test_regressions_syntax_from_args_passed_is_not_a_regression() -> None:
    # "F1 with syntax_from_args passed": the defect check itself, over the
    # same records this rule consumes.
    before = [
        _run(_FROM_ARGS, "ran", [_rec(_FROM_ARGS, 0, "from_args", "failed", "x")])
    ]
    after = [_run(_FROM_ARGS, "ran", [_rec(_FROM_ARGS, 0, "from_args", "passed")])]
    assert regressions(before, after, 0, None, None) == []
    assert defect_check_passes(after, "from_argument_count", 0, None, None) is None


# --- fix round 1: the two reproduced bugs and their siblings ------------


def test_regressions_false_pass_swallowed_by_a_subject_collision() -> None:
    """`COPY old sibling` -> `COPY sibling sibling`: keying by subject would
    map the untouched position (1) to the freshly-written position (0) —
    both now say "sibling" — and hide a real regression on position 1.
    Keying by position instead compares each slot to its own successor."""
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "old", "failed", "x"),
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
            ],
        )
    ]
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
                _rec(COPY_SOURCES, 0, "sibling", "failed", "source not found: sibling"),
            ],
        )
    ]
    lines = regressions(before, after, 0, 0, "sibling")
    assert len(lines) == 1
    assert "failed after" in lines[0]
    # The defect's own slot (position 0) still reads as fixed.
    assert defect_check_passes(after, "missing_copy_source", 0, 0, "sibling") is None


def test_regressions_collision_with_no_real_regression() -> None:
    """Same collision as above, but position 1 genuinely still passes: no
    regression line, despite both records now sharing a subject."""
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "old", "failed", "x"),
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
            ],
        )
    ]
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
            ],
        )
    ]
    assert regressions(before, after, 0, 0, "sibling") == []
    assert defect_check_passes(after, "missing_copy_source", 0, 0, "sibling") is None


def test_regressions_false_fail_absent_source_written_twice() -> None:
    """`COPY old old` -> `COPY new old`: keying by subject would remap
    *both* occurrences of "old" to "new", and the untouched second "old"
    would falsely read as missing after. Position keeps them apart."""
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "old", "failed", "x"),
                _rec(COPY_SOURCES, 0, "old", "passed"),
            ],
        )
    ]
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "new", "passed"),
                _rec(COPY_SOURCES, 0, "old", "passed"),
            ],
        )
    ]
    assert regressions(before, after, 0, 0, "new") == []
    assert defect_check_passes(after, "missing_copy_source", 0, 0, "new") is None


def test_regressions_absent_twice_in_different_notations() -> None:
    """`COPY old ./old /d/` -> `COPY new ./old /d/`: the second source
    normalises to the same subject as the absent one but is written
    differently and is not the one replaced. Position, not subject
    equality, decides what is compared to what."""
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "old", "failed", "x"),
                _rec(COPY_SOURCES, 0, "old", "passed"),
            ],
        )
    ]
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "new", "passed"),
                _rec(COPY_SOURCES, 0, "old", "passed"),
            ],
        )
    ]
    assert regressions(before, after, 0, 0, "new") == []
    assert defect_check_passes(after, "missing_copy_source", 0, 0, "new") is None


def test_regressions_source_count_decrease_is_a_regression() -> None:
    before = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "old", "failed", "x"),
                _rec(COPY_SOURCES, 0, "sibling", "passed"),
            ],
        )
    ]
    after = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "new", "passed")])]
    lines = regressions(before, after, 0, 0, "new")
    assert any("source count changed 2 -> 1" in line for line in lines)


def test_regressions_source_count_increase_is_a_regression() -> None:
    # A source appearing only in `after` is never visited by the per-key
    # loop (it only walks `before`'s keys) — the dedicated count check is
    # the only thing that catches this shape.
    before = [_run(COPY_SOURCES, "ran", [_rec(COPY_SOURCES, 0, "old", "failed", "x")])]
    after = [
        _run(
            COPY_SOURCES,
            "ran",
            [
                _rec(COPY_SOURCES, 0, "new", "passed"),
                _rec(COPY_SOURCES, 0, "extra", "passed"),
            ],
        )
    ]
    lines = regressions(before, after, 0, 0, "new")
    assert lines == ["copy_sources ordinal=0: source count changed 1 -> 2"]
