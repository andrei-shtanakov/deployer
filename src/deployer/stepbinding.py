"""Step binding from the per-attempt log archive.

Design: ``docs/superpowers/specs/2026-10-03-forge-archive-step-binding-design.md``.
Pure functions over texts already read: no I/O and no ``gh``. ``forge`` reads
the archive and the logs, and asks this module which job-log lines the
runner's own per-step files place under which step.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StepSpan:
    """Job-log lines ``[start, end)`` the runner filed under API step ``number``.

    Indices are 0-based, as ``str.splitlines`` numbers the job log's lines.
    """

    number: int
    start: int
    end: int


COMPARE_TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z ")
"""The runner timestamp removed for comparison only (spec §3.2): exactly one
space after it, and only where a line starts with one."""
_BOM = "\ufeff"

StepBindingState = Literal[
    "bound",
    "no_archive",
    "unverifiable",
    "unmatched",
    "ambiguous",
    "malformed",
    "excluded",
]


@dataclass(frozen=True)
class StepFile:
    """One per-step file: its API step number and its strict UTF-8 text."""

    number: int
    text: str


@dataclass(frozen=True)
class StepDirectory:
    """One archive directory's step files, in increasing ``number``, unique."""

    name: str
    files: tuple[StepFile, ...]


@dataclass(frozen=True)
class JobBinding:
    """What binding decided for one job; ``spans`` only when ``bound``."""

    state: StepBindingState
    reason: str | None = None
    spans: tuple[StepSpan, ...] = ()


def comparison_lines(text: str) -> list[str]:
    """The text as compared (spec §3.1–§3.2): one leading BOM dropped, split on
    ``\\n`` only (a final ``\\n`` opens no line), and each line's runner
    timestamp removed where it starts with one. Nothing else is normalised."""
    body = text.removeprefix(_BOM)
    if not body:
        return []
    lines = body.split("\n")
    if body.endswith("\n"):
        lines.pop()
    return [COMPARE_TIMESTAMP_RE.sub("", line, count=1) for line in lines]


def bind_jobs(
    logs: Mapping[int, str],
    api_steps: Mapping[int, Sequence[int]],
    directories: Sequence[StepDirectory],
) -> dict[int, JobBinding]:
    """Bind each population job's log to at most one step directory (spec §4).

    The full match relation is computed first, by content. A pair counts only
    when the directory matches this one job and the job this one directory;
    any other match makes every job it touches ``ambiguous``. Only then is the
    structure of each candidate checked; a failure is ``malformed`` and frees
    nothing for another job. Names and order prove nothing.
    """
    by_text: dict[tuple[str, ...], list[int]] = {}
    for job_id, text in logs.items():
        by_text.setdefault(tuple(comparison_lines(text)), []).append(job_id)
    jobs_of = [
        by_text.get(
            tuple(line for f in d.files for line in comparison_lines(f.text)), []
        )
        for d in directories
    ]
    result: dict[int, JobBinding] = {}
    for job_id, text in logs.items():
        found = [i for i, jobs in enumerate(jobs_of) if job_id in jobs]
        if not found:
            result[job_id] = JobBinding(
                "unmatched", "no step directory's text equals this job's log"
            )
        elif len(found) > 1:
            result[job_id] = JobBinding(
                "ambiguous", f"{len(found)} step directories match this job's log"
            )
        elif len(jobs_of[found[0]]) > 1:
            result[job_id] = JobBinding(
                "ambiguous", "the matching step directory matches another job too"
            )
        else:
            result[job_id] = _structure(
                text, api_steps.get(job_id, ()), directories[found[0]]
            )
    return result


def _structure(
    log: str, numbers: Sequence[int], directory: StepDirectory
) -> JobBinding:
    """Spec §4.3 on a matched pair: ``bound`` with spans, or ``malformed``."""
    if len(set(numbers)) != len(numbers):
        return JobBinding("malformed", "the jobs listing repeats a step number")
    unknown = [f.number for f in directory.files if f.number not in set(numbers)]
    if unknown:
        return JobBinding(
            "malformed",
            f"{len(unknown)} step file number(s) not in the listing, e.g. {unknown[0]}",
        )
    open_ended = [f.number for f in directory.files if not f.text.endswith("\n")]
    if open_ended:
        return JobBinding(
            "malformed",
            f"{len(open_ended)} step file(s) lack a final newline, e.g. {open_ended[0]}",
        )
    if not _breaks_agree(log):
        return JobBinding("malformed", "a line break other than \\n inside the job log")
    spans: list[StepSpan] = []
    start = 0
    for step_file in directory.files:
        end = start + len(comparison_lines(step_file.text))
        spans.append(StepSpan(step_file.number, start, end))
        start = end
    return JobBinding("bound", None, tuple(spans))


def _breaks_agree(log: str) -> bool:
    """Spec §4.3: one BOM-dropped view, split by ``str.splitlines`` and on
    ``\\n`` (one trailing ``\\r`` off each line), gives the same lines, so
    comparison line ``i`` is forge's block line ``i``."""
    view = log.removeprefix(_BOM)
    lines = view.split("\n")
    if view.endswith("\n"):
        lines.pop()
    return view.splitlines() == [line.removesuffix("\r") for line in lines]
