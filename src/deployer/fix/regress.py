"""Defect check and no-regression rule over R's detailed records (F §6.2).

``defect_check_passes`` asks whether the defect's own check now passes for
the corrected instruction; ``regressions`` compares R's offline checks run
fresh on the original bytes and on the fix context and reports every record
that got worse. Both take :class:`~deployer.reproduce.detail.RecordRun`
lists — R's own detailed entry point — never R's folded aggregate output,
which loses the per-record identity this rule needs.
"""

from collections import Counter

from deployer.admission.model import DefectClass
from deployer.reproduce.detail import COPY_SOURCES, CheckRecord, RecordRun

_FROM_ARGS = "syntax_from_args"
# (check_id, ordinal, subject, occurrence). ``occurrence`` disambiguates two
# records that share the first three fields — `COPY a a /d/` or
# `COPY app ./app /d/` give two records with the same `(check_id, ordinal,
# subject)` — by their position among records sharing that triple, in
# record order. Never let one silently overwrite the other in a dict.
_Key = tuple[str, int | None, str, int]


def defect_check_passes(
    after: list[RecordRun], cls: DefectClass, ordinal: int, new_source: str | None
) -> str | None:
    """Whether the defect's own check passes for the bound instruction.

    ``missing_copy_source``: the ``copy_sources`` record of ``new_source``
    at ``ordinal`` must be ``passed``. ``from_argument_count``: the
    ``syntax_from_args`` record at ``ordinal`` must be ``passed`` — that
    check's subject is always the fixed condition name, never a source, so
    ``new_source`` plays no part in the lookup. A run skipped file-wide, a
    missing record, or any status but ``passed`` (``skipped``,
    ``observation``, ``failed``, ``inconclusive``) fails the check; the
    reason is returned as a string. ``None`` means the check passes.
    """
    if cls == "missing_copy_source":
        check_id = COPY_SOURCES
        want_subject = new_source
    elif cls == "from_argument_count":
        check_id = _FROM_ARGS
        want_subject = None
    else:
        return f"unrecognised defect class {cls!r}"
    run = next((r for r in after if r.check_id == check_id), None)
    if run is None:
        return f"no {check_id} run in the after checks"
    if run.file_status == "skipped":
        return f"{check_id} skipped file-wide: {run.file_reason}"
    matches = [
        record
        for record in run.records
        if record.ordinal == ordinal
        and (want_subject is None or record.subject == want_subject)
    ]
    if not matches:
        subject_note = f" subject {want_subject!r}" if want_subject else ""
        return f"no {check_id} record for ordinal {ordinal}{subject_note}"
    failing = [record for record in matches if record.status != "passed"]
    if not failing:
        return None
    record = failing[0]
    return (
        f"{check_id} record for ordinal {ordinal} subject "
        f"{record.subject!r} is {record.status}: {record.reason}"
    )


def regressions(
    before: list[RecordRun],
    after: list[RecordRun],
    ordinal: int,
    absent: str | None,
    new_source: str | None,
) -> list[str]:
    """Every record that got worse from ``before`` to ``after`` (F §6.2).

    Runs are matched by ``check_id`` first (each check id appears at most
    once per side). Within a pair of runs, records are matched by key —
    ``(check_id, ordinal, subject, occurrence)`` — except that the absent
    source's key, at the bound instruction's ``ordinal`` and keeping its
    occurrence index, is looked up under ``new_source`` in ``after``
    instead of under its own subject: the fix replaced that text, so it no
    longer exists in ``after`` under its old name. A ``passed`` record
    whose matched successor is anything else is a regression line; so is a
    key present before with no successor at all — regardless of that
    record's own status, since a vanished check is itself a loss of
    coverage.

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
                absent,
                new_source,
            )
        )
    return lines


def _keyed(records: list[CheckRecord]) -> dict[_Key, CheckRecord]:
    """Key every record by its full identity, in record order."""
    seen: Counter[tuple[str, int | None, str]] = Counter()
    keyed: dict[_Key, CheckRecord] = {}
    for record in records:
        triple = (record.check_id, record.ordinal, record.subject)
        occurrence = seen[triple]
        seen[triple] += 1
        keyed[(*triple, occurrence)] = record
    return keyed


def _mapped_key(
    key: _Key, ordinal: int, absent: str | None, new_source: str | None
) -> _Key:
    """The after-side key to look ``key`` up under.

    Only the absent source of the bound instruction is remapped. Every
    other key — another source, a different instruction, a syntax check's
    fixed condition subject — is looked up unchanged.
    """
    check_id, key_ordinal, subject, occurrence = key
    if (
        check_id == COPY_SOURCES
        and key_ordinal == ordinal
        and absent is not None
        and subject == absent
        and new_source is not None
    ):
        return (check_id, key_ordinal, new_source, occurrence)
    return key


def _record_lines(
    check_id: str,
    before_records: list[CheckRecord],
    after_records: list[CheckRecord],
    ordinal: int,
    absent: str | None,
    new_source: str | None,
) -> list[str]:
    """The regression lines of one matched pair of runs (same file_status)."""
    before_keyed = _keyed(before_records)
    after_keyed = _keyed(after_records)
    lines: list[str] = []
    for key, before_record in before_keyed.items():
        after_key = _mapped_key(key, ordinal, absent, new_source)
        after_record = after_keyed.get(after_key)
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
