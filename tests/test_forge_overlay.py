"""The step overlay over forge's blocks (spec §5): ``job_text`` never moves."""

from typing import Any

import pytest

from deployer.forge import COMPLETE_BY_CONSTRUCTION, StepRef, build_failed_job
from deployer.reproduce.shape import job_text
from deployer.stepbinding import StepSpan

TS = "2026-10-03T10:08:54.1234567Z "
JOB = 7


def _log(
    lines: list[str], *, newline: str = "\n", final: bool = True, bom: bool = False
) -> str:
    body = newline.join(TS + line for line in lines) + (newline if final else "")
    return ("\ufeff" if bom else "") + body


BOUNDARY_BLANKS = [
    "##[group]Run a",
    "a-cmd",
    "##[endgroup]",
    "a-out",
    "",
    "",
    "##[group]Run b",
    "b-cmd",
    "##[endgroup]",
    "b-out",
]
BOUNDARY_SPANS = (StepSpan(3, 0, 5), StepSpan(4, 5, 10))

EMPTY_OUTPUT = [
    "##[group]Run a",
    "a-cmd",
    "##[endgroup]",
    "a-out",
    "##[group]Run b",
    "b-cmd",
    "##[endgroup]",
    "##[group]Run c",
    "c-cmd",
    "##[endgroup]",
    "c-out",
]
EMPTY_SPANS = (StepSpan(3, 0, 4), StepSpan(4, 4, 7), StepSpan(5, 7, 11))

UNCLOSED = ["##[group]Run a", "a-cmd", "a-out", "b-out-1", "b-out-2"]
UNCLOSED_SPANS = (StepSpan(3, 0, 3), StepSpan(4, 3, 5))

BLANK_BLOCK = ["##[group]Run a", "a-cmd", "##[endgroup]", "", "  ", "##[group]Run b"]
BLANK_SPANS = (StepSpan(3, 0, 4), StepSpan(4, 4, 6))

CASES = {
    "blank lines at a boundary": (_log(BOUNDARY_BLANKS), BOUNDARY_SPANS),
    "a step with empty output": (_log(EMPTY_OUTPUT), EMPTY_SPANS),
    "an unclosed group across a boundary": (_log(UNCLOSED), UNCLOSED_SPANS),
    "a blank-only block the splitter drops": (_log(BLANK_BLOCK), BLANK_SPANS),
    "no final newline": (_log(BOUNDARY_BLANKS, final=False), BOUNDARY_SPANS),
    "a CRLF log": (_log(BOUNDARY_BLANKS, newline="\r\n"), BOUNDARY_SPANS),
    "a leading BOM": (_log(BOUNDARY_BLANKS, bom=True), BOUNDARY_SPANS),
}


def _record(spans: tuple[StepSpan, ...]) -> dict[str, Any]:
    steps = [
        {"number": s.number, "name": f"s{s.number}", "conclusion": "failure"}
        for s in spans
    ]
    return {"name": "j", "conclusion": "failure", "steps": steps}


@pytest.mark.parametrize("case", sorted(CASES))
def test_job_text_is_the_same_bound_and_unbound(case: str) -> None:
    log, spans = CASES[case]
    unbound = build_failed_job(_record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION)
    bound = build_failed_job(
        _record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION, spans=spans
    )
    assert job_text(bound) == job_text(unbound)
    assert all(e.source is None for e in unbound.evidence)
    assert {e.source for e in bound.evidence} <= {StepRef(JOB, s.number) for s in spans}
    assert all(not s.evidence for s in bound.steps)


def _sources_of(line: str, log: str, spans: tuple[StepSpan, ...]) -> list[object]:
    bound = build_failed_job(
        _record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION, spans=spans
    )
    return [e.source for e in bound.evidence if line in e.text.split("\n")]


def test_a_block_cut_at_a_boundary_keeps_every_line_with_its_step() -> None:
    log, spans = CASES["blank lines at a boundary"]
    assert _sources_of("a-out", log, spans) == [StepRef(JOB, 3)]
    assert _sources_of("b-out", log, spans) == [StepRef(JOB, 4)]
    bound = build_failed_job(
        _record(spans), JOB, log, [], COMPLETE_BY_CONSTRUCTION, spans=spans
    )
    texts = [(e.source, e.text) for e in bound.evidence]
    assert (StepRef(JOB, 3), "a-out\n") in texts
    assert (StepRef(JOB, 4), "") in texts  # the second blank line, kept


def test_an_unclosed_group_is_cut_between_its_steps() -> None:
    log, spans = CASES["an unclosed group across a boundary"]
    assert _sources_of("a-out", log, spans) == [StepRef(JOB, 3)]
    assert _sources_of("b-out-1", log, spans) == [StepRef(JOB, 4)]
