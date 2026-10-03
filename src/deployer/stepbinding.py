"""Step binding from the per-attempt log archive.

Design: ``docs/superpowers/specs/2026-10-03-forge-archive-step-binding-design.md``.
Pure functions over texts already read: no I/O and no ``gh``. ``forge`` reads
the archive and the logs, and asks this module which job-log lines the
runner's own per-step files place under which step.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StepSpan:
    """Job-log lines ``[start, end)`` the runner filed under API step ``number``.

    Indices are 0-based, as ``str.splitlines`` numbers the job log's lines.
    """

    number: int
    start: int
    end: int
