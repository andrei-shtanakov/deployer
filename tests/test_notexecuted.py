"""Recognising a job cancelled before execution (spec §2, §3.2), pure."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from deployer.notexecuted import NotExecuted, basis_holds, recognise

CASE = Path(__file__).parent / "fixtures" / "step-binding" / "steps-2"
TS = "2026-10-04T09:13:25Z"


def _never_starts() -> dict[str, Any]:
    for call in json.loads((CASE / "gh-calls.json").read_text()):
        if "/jobs?" in call["argv"][-1] and "stdout" in call:
            for job in json.loads(call["stdout"])["jobs"]:
                if job["name"] == "waiting-legs (never-starts)":
                    return job
    raise AssertionError("never-starts not recorded")


def test_the_recorded_form_is_recognised_with_every_basis() -> None:
    assert recognise(_never_starts(), 404) == NotExecuted(
        "completed", "cancelled", 0, "", 0, TS, TS, 404
    )


def test_the_name_proves_nothing() -> None:
    record = _never_starts()
    record["name"] = "parallel-legs (fail-fast)"
    assert recognise(record, 404) is not None


def _without(record: dict[str, Any], key: str) -> dict[str, Any]:
    record = copy.deepcopy(record)
    del record[key]
    return record


def _with(key: str, value: object) -> dict[str, Any]:
    record = _never_starts()
    record[key] = value
    return record


@pytest.mark.parametrize(
    ("record", "log_status"),
    [
        (_with("status", "in_progress"), 404),
        (_with("conclusion", "failure"), 404),
        (_with("runner_id", 1), 404),
        (_with("runner_id", "0"), 404),
        (_with("runner_id", False), 404),
        (_with("runner_name", "GitHub Actions 1"), 404),
        (_with("runner_name", None), 404),
        (_without(_never_starts(), "runner_name"), 404),
        (_with("steps", [{"number": 1, "name": "x", "conclusion": None}]), 404),
        (_without(_never_starts(), "steps"), 404),
        (_with("started_at", "2026-10-04T09:13:26Z"), 404),
        (_without(_never_starts(), "started_at"), 404),
        (_without(_never_starts(), "created_at"), 404),
        ({**_never_starts(), "created_at": "", "started_at": ""}, 404),
        ({**_never_starts(), "created_at": "unknown", "started_at": "unknown"}, 404),
        ({**_never_starts(), "created_at": None, "started_at": None}, 404),
        ({**_never_starts(), "created_at": 0, "started_at": 0}, 404),
        (
            {
                **_never_starts(),
                "created_at": "2026-10-04T09:13:25.000Z",
                "started_at": "2026-10-04T09:13:25.000Z",
            },
            404,
        ),
        (
            {
                **_never_starts(),
                "created_at": "2026-10-04T09:13:25+00:00",
                "started_at": "2026-10-04T09:13:25+00:00",
            },
            404,
        ),
        (
            {
                **_never_starts(),
                "created_at": "2026-02-30T09:13:25Z",
                "started_at": "2026-02-30T09:13:25Z",
            },
            404,
        ),
        (_never_starts(), None),
        (_never_starts(), 410),
        (_never_starts(), 502),
        ("not a record", 404),
    ],
)
def test_any_broken_signal_is_not_recognised(
    record: object, log_status: int | None
) -> None:
    assert recognise(record, log_status) is None


def test_a_dispatcher_shape_is_not_recognised() -> None:
    record = _never_starts()
    record["runner_id"] = 1000028882
    record["runner_name"] = "GitHub Actions 1000028882"
    assert recognise(record, 404) is None


def test_basis_holds_on_the_recorded_values_only() -> None:
    good = NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 404)
    assert basis_holds(good)
    for bad in (
        NotExecuted("completed", "cancelled", 1, "", 0, TS, TS, 404),
        NotExecuted("completed", "cancelled", 0, "x", 0, TS, TS, 404),
        NotExecuted("completed", "cancelled", 0, "", 1, TS, TS, 404),
        NotExecuted(
            "completed", "cancelled", 0, "", 0, TS, "2026-10-04T09:13:26Z", 404
        ),
        NotExecuted("completed", "cancelled", 0, "", 0, "", "", 404),
        NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 410),
    ):
        assert not basis_holds(bad), bad
