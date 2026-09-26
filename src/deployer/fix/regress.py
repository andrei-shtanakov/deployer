"""Defect check and no-regression rule over R's detailed records (F §6.2).

``defect_check_passes`` asks whether the defect's own check now passes for
the corrected instruction; ``regressions`` compares R's offline checks run
fresh on the original bytes and on the fix context and reports every record
that got worse. Both take :class:`~deployer.reproduce.detail.RecordRun`
lists — R's own detailed entry point — never R's folded aggregate output,
which loses the per-record identity this rule needs.

``copy_sources`` records are keyed by **position**, not by subject: R emits
one record per source of an instruction, in source order, and a one-token
replacement keeps the source count and order, so an index into that order
is stable across the fix even when two sources share a subject. Keying by
``(subject, occurrence)`` instead — the first cut of this module — reads as
plausible but breaks on two real Dockerfile shapes: a replacement that
collides with a sibling's *subject* (``COPY old sibling`` -> ``COPY sibling
sibling``) silently swallows a real regression on the sibling, because the
sibling's occurrence-0 key now resolves to the freshly-written record
instead of its own; and an absent source written twice (``COPY old old``)
falsely regresses the untouched occurrence, because both occurrences of the
old subject get remapped to the new one although only one was replaced.
Position sidesteps both: the untouched sibling keeps its own slot whatever
its text, and only the one replaced slot is ever judged against the fix.
"""

from collections import Counter

from deployer.admission.model import DefectClass
from deployer.reproduce.checks import normalize_copy_path
from deployer.reproduce.detail import COPY_SOURCES, CheckRecord, RecordRun

_FROM_ARGS = "syntax_from_args"
# copy_sources: (check_id, ordinal, position) — position is the record's
# index among the *same instruction's* copy_sources records, in source
# order (never the subject: two sources may share one, and a collision
# introduced by the fix itself must not merge their identities).
# Every other check: (check_id, ordinal, subject) — one record per rule per
# instruction, so this triple is already unique with no occurrence needed.
_Key = tuple[str, int | None, int | str]


def defect_check_passes(
    after: list[RecordRun],
    cls: DefectClass,
    ordinal: int,
    replaced_position: int | None,
    new_source: str | None,
) -> str | None:
    """Whether the defect's own check passes for the bound instruction.

    ``missing_copy_source``: the ``copy_sources`` record at
    ``replaced_position`` of the instruction at ``ordinal`` must be
    ``passed`` **and** its subject must equal ``normalize_copy_path(new_source)`` — a
    passing record at that slot with some other subject means the slot was
    not actually rewritten to the intended source. ``from_argument_count``:
    the ``syntax_from_args`` record at ``ordinal`` must be ``passed`` —
    that check's subject is always the fixed condition name, never a
    source, so ``replaced_position``/``new_source`` play no part there. A
    run skipped file-wide, a missing record, or any status but ``passed``
    fails the check; the reason is returned as a string. ``None`` means the
    check passes.
    """
    if cls == "missing_copy_source":
        return _copy_defect_passes(after, ordinal, replaced_position, new_source)
    if cls == "from_argument_count":
        return _from_args_defect_passes(after, ordinal)
    return f"unrecognised defect class {cls!r}"


def regressions(
    before: list[RecordRun],
    after: list[RecordRun],
    ordinal: int,
    replaced_position: int | None,
    new_source: str | None,
) -> list[str]:
    """Every record that got worse from ``before`` to ``after`` (F §6.2).

    Runs are matched by ``check_id`` first (each check id appears at most
    once per side). Within a matched pair of the same ``file_status``,
    records are matched by key (see the module docstring for why
    ``copy_sources`` keys on position); the record at
    ``(copy_sources, ordinal, replaced_position)`` is excluded from this
    comparison entirely — it is expected to have failed before (the absent
    source) and its after-state is judged by :func:`defect_check_passes`,
    not here. A change in the number of ``copy_sources`` records for
    ``ordinal`` is itself a regression line, checked independently of the
    per-key comparison (a source appearing only in ``after`` would
    otherwise never be looked at, since the loop below only walks
    ``before``'s keys).

    A ``passed`` record whose matched successor is anything else is a
    regression line; so is a key present before with no successor at all,
    regardless of that record's own status, since a vanished check is
    itself a loss of coverage.

    Subjects are only comparable between two runs of the same
    ``file_status``: a ``copy_sources`` subject is the raw source under a
    file-wide skip and the normalised source once the check runs, so a run
    that turned from ``skipped`` to ``ran`` has nothing to key-match
    against and is left uncompared (this is progress, not regression). A
    run that turned from ``ran`` to ``skipped`` needs no keying either way:
    every record it held before is itself a regression, one line each.
    """
    after_by_check = {run.check_id: run for run in after}
    lines: list[str] = []
    for before_run in before:
        after_run = after_by_check.get(before_run.check_id)
        if after_run is None:
            lines.extend(_vanished_run_lines(before_run.check_id, before_run.records))
            continue
        if before_run.file_status == "ran" and after_run.file_status == "skipped":
            lines.extend(
                _skipped_after_lines(before_run.check_id, before_run.records, after_run)
            )
            continue
        if before_run.file_status != after_run.file_status:
            continue
        lines.extend(
            _record_lines(
                before_run.check_id,
                before_run.records,
                after_run.records,
                ordinal,
                replaced_position,
            )
        )
    return lines


def _copy_defect_passes(
    after: list[RecordRun],
    ordinal: int,
    position: int | None,
    new_source: str | None,
) -> str | None:
    """``defect_check_passes`` for ``missing_copy_source``."""
    run = next((r for r in after if r.check_id == COPY_SOURCES), None)
    if run is None:
        return f"no {COPY_SOURCES} run in the after checks"
    if run.file_status == "skipped":
        return f"{COPY_SOURCES} skipped file-wide: {run.file_reason}"
    if position is None:
        return "no replaced position given for a missing_copy_source defect"
    if new_source is None:
        return "no replacement source given for a missing_copy_source defect"
    at_ordinal = [r for r in run.records if r.ordinal == ordinal]
    if not 0 <= position < len(at_ordinal):
        return f"no {COPY_SOURCES} record at position {position} for ordinal {ordinal}"
    record = at_ordinal[position]
    if record.status != "passed":
        return (
            f"{COPY_SOURCES} record for ordinal {ordinal} position {position} "
            f"is {record.status}: {record.reason}"
        )
    expected = normalize_copy_path(new_source)
    if record.subject != expected:
        return (
            f"{COPY_SOURCES} record for ordinal {ordinal} position {position} "
            f"has subject {record.subject!r}, expected {expected!r}"
        )
    return None


def _from_args_defect_passes(after: list[RecordRun], ordinal: int) -> str | None:
    """``defect_check_passes`` for ``from_argument_count``."""
    run = next((r for r in after if r.check_id == _FROM_ARGS), None)
    if run is None:
        return f"no {_FROM_ARGS} run in the after checks"
    if run.file_status == "skipped":
        return f"{_FROM_ARGS} skipped file-wide: {run.file_reason}"
    matches = [record for record in run.records if record.ordinal == ordinal]
    if not matches:
        return f"no {_FROM_ARGS} record for ordinal {ordinal}"
    failing = [record for record in matches if record.status != "passed"]
    if not failing:
        return None
    record = failing[0]
    return (
        f"{_FROM_ARGS} record for ordinal {ordinal} subject "
        f"{record.subject!r} is {record.status}: {record.reason}"
    )


def _keyed(check_id: str, records: list[CheckRecord]) -> dict[_Key, CheckRecord]:
    """Key every record of one run: position for ``copy_sources``, subject
    for everything else (see the module docstring)."""
    if check_id != COPY_SOURCES:
        return {
            (check_id, record.ordinal, record.subject): record for record in records
        }
    positions: Counter[int | None] = Counter()
    keyed: dict[_Key, CheckRecord] = {}
    for record in records:
        position = positions[record.ordinal]
        positions[record.ordinal] += 1
        keyed[(check_id, record.ordinal, position)] = record
    return keyed


def _record_lines(
    check_id: str,
    before_records: list[CheckRecord],
    after_records: list[CheckRecord],
    ordinal: int,
    replaced_position: int | None,
) -> list[str]:
    """The regression lines of one matched pair of runs (same file_status)."""
    lines: list[str] = []
    if check_id == COPY_SOURCES:
        lines.extend(_copy_count_change_lines(before_records, after_records, ordinal))
    excluded: _Key | None = (
        (check_id, ordinal, replaced_position)
        if check_id == COPY_SOURCES and replaced_position is not None
        else None
    )
    before_keyed = _keyed(check_id, before_records)
    after_keyed = _keyed(check_id, after_records)
    for key, before_record in before_keyed.items():
        if key == excluded:
            continue
        after_record = after_keyed.get(key)
        if after_record is None:
            lines.append(
                f"{check_id} ordinal={before_record.ordinal} "
                f"subject={before_record.subject!r}: present before, "
                "missing after"
            )
            continue
        if before_record.status == "passed" and after_record.status != "passed":
            lines.append(
                f"{check_id} ordinal={before_record.ordinal} "
                f"subject={before_record.subject!r}: passed before, "
                f"{after_record.status} after ({after_record.reason})"
            )
    return lines


def _copy_count_change_lines(
    before_records: list[CheckRecord], after_records: list[CheckRecord], ordinal: int
) -> list[str]:
    """A regression line if ``ordinal``'s source count changed either way."""
    before_count = sum(1 for r in before_records if r.ordinal == ordinal)
    after_count = sum(1 for r in after_records if r.ordinal == ordinal)
    if before_count == after_count:
        return []
    return [
        f"{COPY_SOURCES} ordinal={ordinal}: source count changed "
        f"{before_count} -> {after_count}"
    ]


def _vanished_run_lines(check_id: str, records: list[CheckRecord]) -> list[str]:
    """Every record of a check absent entirely from ``after`` (defensive:
    both sides always run the same closed set of checks in practice)."""
    return [
        f"{check_id} ordinal={record.ordinal} subject={record.subject!r}: "
        "present before, missing after (no such check in the after run)"
        for record in records
    ]


def _skipped_after_lines(
    check_id: str, records: list[CheckRecord], after_run: RecordRun
) -> list[str]:
    """Every record of a run that flipped from ``ran`` to ``skipped``."""
    return [
        f"{check_id} ordinal={record.ordinal} subject={record.subject!r}: "
        f"skipped file-wide after ({after_run.file_reason})"
        for record in records
    ]
