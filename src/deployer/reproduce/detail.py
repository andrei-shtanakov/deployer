"""R's offline checks as detailed records, one per unit checked (F §6.2).

R's aggregate output (``checks.copy_source_checks``,
``dockerfile.syntax_checks``) cannot be projected per instruction: a failure
hides the passes of other sources and a clean rule is one file-level
``passed``. The producers here run the same logic and emit one
:class:`CheckRecord` per ``(check_id, instruction, subject)``; R's aggregate
lists are folds over them (:func:`fold_copy_sources`, :func:`fold_syntax`).

The rule helpers stay where R defined them (``checks.py``,
``dockerfile.py``) and are imported from there; the two aggregate functions
import this module inside their bodies, so the import graph has no cycle.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from deployer.reproduce.checks import (
    _REMOTE_PREFIXES,
    _check_source,
    _context_paths,
    _has_unmodelled_chars,
    _skip,
    local_copy_sources,
    normalize_copy_path,
)
from deployer.reproduce.dockerfile import (
    _CHECK_IDS,
    KEYWORDS,
    Instruction,
    ParsedDockerfile,
    _from_args_ok,
    unread_reason,
)
from deployer.reproduce.ignore import IgnoreRules
from deployer.reproduce.model import (
    CheckStatus,
    Location,
    ReproductionCheck,
    ReproEvidence,
)

COPY_SOURCES = "copy_sources"
SYNTAX_CHECK_IDS = _CHECK_IDS
UNREADABLE_SUBJECT = "*"
FileStatus = Literal["ran", "skipped"]
# One syntax problem: the span R reports, its message, the evidence text.
_Problem = tuple[tuple[int, int], str, str]


@dataclass(frozen=True)
class CheckRecord:
    """One unit a check ran on (or could not run on) — F §6.2.

    ``ordinal`` is the instruction's index in ``parsed.instructions`` and
    ``lines`` its ``(first_line, last_line)``; both are ``None`` only for a
    file-wide record with no instruction (an empty ``syntax_first_from``).
    ``subject`` is the normalised source for ``copy_sources`` (the raw source
    when unmodelled, ``"*"`` when the instruction's sources cannot be read)
    and the rule's condition name for a syntax check. ``reason`` is set for
    every status but ``passed``; ``finding`` is the exact check R emits for a
    failed or observation unit.
    """

    check_id: str
    ordinal: int | None
    lines: tuple[int, int] | None
    subject: str
    status: CheckStatus
    reason: str | None
    finding: ReproductionCheck | None


@dataclass(frozen=True)
class RecordRun:
    """One check over one Dockerfile: its file-level status and its records.

    ``file_status`` is part of the result, never inferred from ``records``:
    a Dockerfile without COPY/ADD gives ``ran`` with no records, or
    ``skipped`` with R's file-wide ``file_reason``.
    """

    check_id: str
    file_status: FileStatus
    file_reason: str | None
    records: list[CheckRecord]


def copy_source_records(
    parsed: ParsedDockerfile, context: Path, dockerfile: str, rules: IgnoreRules
) -> RecordRun:
    """One ``copy_sources`` record per source of every COPY/ADD."""
    file_reason = _copy_file_reason(parsed, rules)
    if file_reason is not None:
        return RecordRun(
            COPY_SOURCES,
            "skipped",
            file_reason,
            [
                _record(COPY_SOURCES, ordinal, inst, subject, "skipped", file_reason)
                for ordinal, inst in _copy_instructions(parsed)
                for subject in _raw_subjects(inst)
            ],
        )
    files = _context_paths(context)
    records = [
        record
        for ordinal, inst in _copy_instructions(parsed)
        for record in _source_records(ordinal, inst, files, context, dockerfile, rules)
    ]
    return RecordRun(COPY_SOURCES, "ran", None, records)


def syntax_records(parsed: ParsedDockerfile, dockerfile: str) -> list[RecordRun]:
    """One run per syntax check id, in R's order; one record per instruction."""
    unread = unread_reason(parsed)
    status: CheckStatus = "observation" if parsed.syntax_directive else "failed"
    runs: list[RecordRun] = []
    for check_id in SYNTAX_CHECK_IDS:
        units = list(_syntax_units(check_id, parsed))
        if unread is not None:
            records = [
                _record(
                    check_id, ordinal, inst, _condition(check_id), "skipped", unread
                )
                for ordinal, inst, _ in units
            ]
            runs.append(RecordRun(check_id, "skipped", unread, records))
            continue
        records = [
            _syntax_record(check_id, ordinal, inst, problem, status, dockerfile)
            for ordinal, inst, problem in units
        ]
        runs.append(RecordRun(check_id, "ran", None, records))
    return runs


def fold_copy_sources(run: RecordRun) -> list[ReproductionCheck]:
    """R's aggregate ``copy_sources`` list, exactly as before the records."""
    if run.file_status == "skipped":
        return [
            ReproductionCheck(
                check_id=run.check_id, status="skipped", reason=run.file_reason
            )
        ]
    findings = [r.finding for r in run.records if r.status == "failed" and r.finding]
    checked = any(r.status in ("passed", "failed") for r in run.records)
    if not findings and checked:
        findings = [ReproductionCheck(check_id=run.check_id, status="passed")]
    skipped = [
        ReproductionCheck(check_id=run.check_id, status="skipped", reason=r.reason)
        for r in run.records
        if r.status == "skipped"
    ]
    return findings + skipped


def fold_syntax(runs: list[RecordRun]) -> list[ReproductionCheck]:
    """R's aggregate syntax list: per check id its findings, else one pass."""
    checks: list[ReproductionCheck] = []
    for run in runs:
        if run.file_status == "skipped":
            checks.append(
                ReproductionCheck(
                    check_id=run.check_id, status="skipped", reason=run.file_reason
                )
            )
            continue
        findings = [r.finding for r in run.records if r.finding is not None]
        checks.extend(
            findings or [ReproductionCheck(check_id=run.check_id, status="passed")]
        )
    return checks


def _copy_file_reason(parsed: ParsedDockerfile, rules: IgnoreRules) -> str | None:
    """R's file-wide skip reason for ``copy_sources``, or ``None``."""
    unread = unread_reason(parsed)
    if unread is not None:
        return f"Dockerfile not fully read ({unread})"
    if rules.unsupported is not None:
        return f"ignore pattern not modelled: {rules.unsupported}"
    return None


def _copy_instructions(parsed: ParsedDockerfile) -> Iterator[tuple[int, Instruction]]:
    for ordinal, inst in enumerate(parsed.instructions):
        if inst.keyword in ("COPY", "ADD"):
            yield ordinal, inst


def _raw_subjects(inst: Instruction) -> list[str]:
    """Sources as written, or ``"*"`` when the instruction cannot be read."""
    sources, why_skipped = local_copy_sources(inst)
    return [UNREADABLE_SUBJECT] if why_skipped is not None else sources


def _source_records(
    ordinal: int,
    inst: Instruction,
    files: list[str],
    context: Path,
    dockerfile: str,
    rules: IgnoreRules,
) -> list[CheckRecord]:
    """The records of one COPY/ADD — the loop body of R's ``copy_sources``."""
    sources, why_skipped = local_copy_sources(inst)
    if why_skipped is not None:
        return [_skip_record(ordinal, inst, UNREADABLE_SUBJECT, why_skipped)]
    records: list[CheckRecord] = []
    for raw in sources:
        if inst.keyword == "ADD" and raw.startswith(_REMOTE_PREFIXES):
            records.append(_skip_record(ordinal, inst, raw, f"remote ADD source {raw}"))
            continue
        # The alphabet is checked on the source AS WRITTEN: normalising
        # first would let `$SRC/../x` collapse to `x` and pass.
        if _has_unmodelled_chars(raw):
            records.append(
                _skip_record(ordinal, inst, raw, f"source pattern not modelled: {raw}")
            )
            continue
        source = normalize_copy_path(raw)
        found = _check_source(inst, source, files, context, dockerfile, rules)
        if not found:
            records.append(_record(COPY_SOURCES, ordinal, inst, source, "passed"))
            continue
        finding = found[0]
        records.append(
            _record(
                COPY_SOURCES,
                ordinal,
                inst,
                source,
                "failed",
                finding.finding,
                finding,
            )
        )
    return records


def _skip_record(
    ordinal: int, inst: Instruction, subject: str, why: str
) -> CheckRecord:
    """A skipped source, with the exact reason R's ``_skip`` writes."""
    reason = _skip(inst, why).reason
    return _record(COPY_SOURCES, ordinal, inst, subject, "skipped", reason)


def _syntax_units(
    check_id: str, parsed: ParsedDockerfile
) -> Iterator[tuple[int | None, Instruction | None, _Problem | None]]:
    """Every instruction a syntax rule applies to, with R's problem or None."""
    instructions = parsed.instructions
    if check_id == "syntax_first_from":
        head = next(
            ((k, i) for k, i in enumerate(instructions) if i.keyword != "ARG"), None
        )
        if head is None:
            problem = ((1, 1), "the first instruction is not FROM", "empty Dockerfile")
            yield None, None, problem
            return
        ordinal, inst = head
        bad = inst.keyword != "FROM"
        yield (
            ordinal,
            inst,
            (
                (_span(inst), "the first instruction is not FROM", inst.text)
                if bad
                else None
            ),
        )
    elif check_id == "syntax_from_args":
        for ordinal, inst in enumerate(instructions):
            if inst.keyword != "FROM":
                continue
            bad = not _from_args_ok(inst.args)
            yield (
                ordinal,
                inst,
                (
                    (_span(inst), "FROM takes one or three arguments", inst.text)
                    if bad
                    else None
                ),
            )
    elif check_id == "syntax_keyword":
        for ordinal, inst in enumerate(instructions):
            bad = inst.keyword not in KEYWORDS
            yield (
                ordinal,
                inst,
                (
                    (_span(inst), f"unknown instruction {inst.keyword}", inst.text)
                    if bad
                    else None
                ),
            )
    elif check_id == "syntax_continuation" and instructions:
        last = instructions[-1]
        yield (
            len(instructions) - 1,
            last,
            (
                (
                    (last.last_line, last.last_line),
                    "line continuation ends the file",
                    last.text,
                )
                if parsed.dangling_continuation
                else None
            ),
        )


def _syntax_record(
    check_id: str,
    ordinal: int | None,
    inst: Instruction | None,
    problem: _Problem | None,
    status: CheckStatus,
    dockerfile: str,
) -> CheckRecord:
    """A syntax unit; a problem becomes exactly the check R emits for it."""
    subject = _condition(check_id)
    if problem is None:
        return _record(check_id, ordinal, inst, subject, "passed")
    span, message, text = problem
    finding = ReproductionCheck(
        check_id=check_id,
        status=status,
        finding=f"syntax error at line {span[0]}: {message}",
        location=Location(file=dockerfile, lines=span),
        evidence=[ReproEvidence(kind="log_excerpt", text=text)],
    )
    return _record(check_id, ordinal, inst, subject, status, message, finding)


def _condition(check_id: str) -> str:
    """The rule's condition name: ``syntax_from_args`` -> ``from_args``."""
    return check_id.removeprefix("syntax_")


def _span(inst: Instruction) -> tuple[int, int]:
    return (inst.first_line, inst.last_line)


def _record(
    check_id: str,
    ordinal: int | None,
    inst: Instruction | None,
    subject: str,
    status: CheckStatus,
    reason: str | None = None,
    finding: ReproductionCheck | None = None,
) -> CheckRecord:
    lines = _span(inst) if inst is not None else None
    return CheckRecord(check_id, ordinal, lines, subject, status, reason, finding)
