"""Recognising a job cancelled before execution.

Design: ``docs/superpowers/specs/2026-10-04-forge-not-executed-jobs-design.md``
§2–§3. Pure: no I/O. The recognised form is the one recorded on a real run
(``steps-2``); it is the supported basis for treating a job as cancelled
before execution, not a universal proof. Anything else is not recognised.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Strict

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class NotExecuted:
    """Every basis of the decision, as checked (snapshot 1.6, §3.2).

    The ``int``/``str`` fields are strict on load (§3.3): lax coercion would
    turn ``false`` into ``0`` and ``"404"`` into ``404`` before any check.
    """

    status: Literal["completed"]
    conclusion: Literal["cancelled"]
    runner_id: Annotated[int, Strict()]
    runner_name: Annotated[str, Strict()]
    steps: Annotated[int, Strict()]
    created_at: Annotated[str, Strict()]
    started_at: Annotated[str, Strict()]
    log_status: Annotated[int, Strict()]


def recognise(record: object, log_status: int | None) -> NotExecuted | None:
    """The job's ``NotExecuted`` when its listing record and its own log read
    show the supported form (§2), else ``None``. Missing or ill-typed values
    never match."""
    if not isinstance(record, dict):
        return None
    created, started = record.get("created_at"), record.get("started_at")
    steps = record.get("steps")
    if not (
        record.get("status") == "completed"
        and record.get("conclusion") == "cancelled"
        and _is_int(record.get("runner_id"), 0)
        and record.get("runner_name") == ""
        and isinstance(steps, list)
        and steps == []
        and _same_instant(created, started)
        and _is_int(log_status, 404)
    ):
        return None
    assert isinstance(created, str) and isinstance(started, str)
    return NotExecuted("completed", "cancelled", 0, "", 0, created, started, 404)


def basis_holds(ne: NotExecuted) -> bool:
    """A stored ``NotExecuted`` satisfies §2 on its own values (§3.3)."""
    return (
        ne.status == "completed"
        and ne.conclusion == "cancelled"
        and _is_int(ne.runner_id, 0)
        and ne.runner_name == ""
        and _is_int(ne.steps, 0)
        and _same_instant(ne.created_at, ne.started_at)
        and _is_int(ne.log_status, 404)
    )


def _is_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _same_instant(created: object, started: object) -> bool:
    """Both valid timestamps in the recorded form, and equal (§2)."""
    if not (isinstance(created, str) and isinstance(started, str)):
        return False
    if not (_TIMESTAMP_RE.fullmatch(created) and _TIMESTAMP_RE.fullmatch(started)):
        return False
    try:
        datetime.strptime(created, _TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return created == started
