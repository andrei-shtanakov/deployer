"""Acceptance on the real steps-2 recording (spec §9.1), end-to-end synthetic
cases (§9.2) and the reproduce boundary (§8, §9.3; the fix boundary is in
``tests/fix/test_author.py``)."""

import json
from dataclasses import replace

from deployer.diagnose import diagnose_run
from deployer.forge import (
    FailedRun,
    GhError,
    RunRef,
    StepBinding,
    StepRef,
    fetch_failed_run,
)
from deployer.notexecuted import NotExecuted
from deployer.reproduce.shape import job_text, precheck
from tests.step_binding_data import (
    STEPS_2,
    Steps2Replay,
    never_starts_with_a_runner,
    only_the_recognised_job_kept,
    with_assertion_in_fail_fast,
)

ENV = json.loads((STEPS_2 / "environment.json").read_text())
TS = "2026-10-04T09:13:25Z"
NEVER = "waiting-legs (never-starts)"
FAIL_FAST = "parallel-legs (fail-fast)"
PROBE = "AssertionError: probe-fail-fast"
RAN = {
    FAIL_FAST: "MARK-fail-fast",
    "parallel-legs (long-1)": "MARK-long-1",
    "parallel-legs (long-2)": "MARK-long-2",
    "waiting-legs (first-fails)": "MARK-first-fails",
}


def _fetch(gh: Steps2Replay) -> FailedRun:
    run = fetch_failed_run(RunRef(ENV["repo"], ENV["run_id"]), attempt=1, runner=gh)
    assert isinstance(run, FailedRun)
    return run


def _cancelled_line(job_id: int) -> str:
    return (
        f"job {job_id} ({NEVER}) was cancelled before execution "
        "(runner 0, no steps, log 404)"
    )


def _ids() -> dict[str, int]:
    return {r["name"]: r["id"] for r in Steps2Replay().records()}


def test_the_never_started_sibling_is_recognised_and_excluded() -> None:
    by = {j.name: j for j in _fetch(Steps2Replay()).jobs}
    never = by[NEVER]
    assert never.not_executed == NotExecuted(
        "completed", "cancelled", 0, "", 0, TS, TS, 404
    )
    assert never.step_binding == StepBinding("excluded", "cancelled before execution")
    assert never.completeness.logs == "error"


def test_the_four_jobs_that_ran_are_bound_at_their_run_step() -> None:
    run = _fetch(Steps2Replay())
    assert run.archive is not None and run.archive.state == "available"
    for job in run.jobs:
        if job.name == NEVER:
            continue
        assert job.step_binding == StepBinding("bound")
        holders = [
            e
            for s in job.steps
            for e in s.evidence
            if RAN[job.name] in e.text.split("\n")
        ] + [e for e in job.evidence if RAN[job.name] in e.text.split("\n")]
        assert [e.source for e in holders] == [StepRef(job.job_id, 3)]


def test_the_verdicts_are_exactly_the_four_run_steps() -> None:
    run = _fetch(Steps2Replay())
    d = diagnose_run(run)
    ids = {j.name: j.job_id for j in run.jobs}
    assert len(d.failures) == 4  # a set alone would hide a duplicated verdict
    assert {v.where for v in d.failures} == {StepRef(ids[n], 3) for n in RAN}
    for verdict in d.failures:  # no verdict cites another job's or step's evidence
        assert all(e.source == verdict.where for e in verdict.evidence)
    assert d.outcome == "UNCLASSIFIED"
    assert _cancelled_line(ids[NEVER]) in d.observations
    assert not any("logs fetch error" in o for o in d.observations)


def test_a_recognised_diagnostic_line_is_cited_from_its_own_step() -> None:
    """Labelled synthetic (owner, 2026-10-04): every-cited-block-is-own-step passes
    vacuously when nothing is cited, so one leg gets a line an existing rule surely
    observes. It is `AssertionError: probe-fail-fast`, inserted after
    `MARK-fail-fast` in BOTH that job's log and its step-3 archive file, so binding
    still holds."""
    run = _fetch(with_assertion_in_fail_fast())
    by = {j.name: j for j in run.jobs}
    assert by[NEVER].step_binding == StepBinding(
        "excluded", "cancelled before execution"
    )
    assert all(by[n].step_binding == StepBinding("bound") for n in RAN)
    d = diagnose_run(run)
    assert len(d.failures) == 4
    (verdict,) = [v for v in d.failures if v.where == StepRef(by[FAIL_FAST].job_id, 3)]
    assert verdict.evidence, "the rule must cite something"
    assert all(e.source == verdict.where for e in verdict.evidence)
    assert any(PROBE in e.text for e in verdict.evidence)
    others = [v for v in d.failures if v is not verdict]
    assert not any("probe-fail-fast" in e.text for v in others for e in v.evidence)


def test_job_text_is_unchanged_by_binding() -> None:
    """Capped (binding runs) and not-attempted (no binding) read the same text."""
    run = _fetch(Steps2Replay())
    plain = _fetch(Steps2Replay(capped=False))
    assert {j.job_id: job_text(j) for j in run.jobs} == {
        j.job_id: job_text(j) for j in plain.jobs
    }


def test_a_real_error_beside_the_exclusion_stays_visible() -> None:
    ids = _ids()
    long1 = ids["parallel-legs (long-1)"]
    run = _fetch(Steps2Replay(logs={long1: GhError("x (HTTP 502)", 502)}))
    d = diagnose_run(run)
    assert {j.name: j.step_binding for j in run.jobs}[NEVER] == StepBinding(
        "excluded", "cancelled before execution"
    )
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert f"job {long1}: logs fetch error" in d.observations
    assert f"job {ids[NEVER]}: logs fetch error" not in d.observations
    assert _cancelled_line(ids[NEVER]) in d.observations


def test_the_recognised_job_s_annotations_error_is_reported() -> None:
    ids = _ids()
    gh = Steps2Replay(annotations={ids[NEVER]: GhError("y (HTTP 502)", 502)})
    run = _fetch(gh)
    never = {j.name: j for j in run.jobs}[NEVER]
    assert never.not_executed is not None
    assert never.step_binding == StepBinding("excluded", "cancelled before execution")
    assert run.completeness.annotations == "error"
    d = diagnose_run(run)
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert ids[NEVER] not in {
        v.where if isinstance(v.where, int) else v.where.job_id for v in d.failures
    }
    assert f"job {ids[NEVER]}: annotations fetch error" in d.observations
    assert _cancelled_line(ids[NEVER]) in d.observations


def test_every_kept_job_recognised() -> None:
    run = _fetch(Steps2Replay(records=only_the_recognised_job_kept))
    assert [j.name for j in run.jobs] == [NEVER]
    assert run.archive is None
    assert run.completeness.logs == "unavailable"
    d = diagnose_run(run)
    assert d.failures == [] and d.outcome == "EVIDENCE_UNAVAILABLE"
    assert (
        "every non-green job was cancelled before execution; "
        "no job log holds the failure"
    ) in d.observations


def test_excluded_under_every_archive_state() -> None:
    absent = Steps2Replay(archive=b"PK\x05\x06" + b"\x00" * 18)  # an empty ZIP
    refused = Steps2Replay(archive=b"not a zip")
    unavailable = Steps2Replay(archive=GhError("z (HTTP 404)", 404))
    not_attempted = Steps2Replay(capped=False)
    states = []
    for gh in (absent, refused, unavailable, not_attempted):
        run = _fetch(gh)
        states.append(None if run.archive is None else run.archive.state)
        by = {j.name: j for j in run.jobs}
        assert by.pop(NEVER).step_binding == StepBinding(
            "excluded", "cancelled before execution"
        )
        others = {
            None if j.step_binding is None else j.step_binding.state
            for j in by.values()
        }
        assert others == ({None} if gh is not_attempted else {"no_archive"})
    assert states == ["absent", "refused", "unavailable", None]


def test_reproduce_precheck_refuses_the_same_with_and_without_the_field() -> None:
    run = _fetch(Steps2Replay())
    stripped = replace(run, jobs=[replace(j, not_executed=None) for j in run.jobs])
    assert precheck(run) == precheck(stripped)
    assert getattr(precheck(run), "reason", None) == "checkout SHA not established"


def test_a_dispatcher_shaped_job_is_not_recognised() -> None:
    """Synthetic dispatcher shape (§9.2): a runner, no steps, no log."""
    run = _fetch(Steps2Replay(records=never_starts_with_a_runner))
    by = {j.name: j for j in run.jobs}
    assert by[NEVER].not_executed is None
    assert all(
        j.step_binding is not None and j.step_binding.state == "unverifiable"
        for j in run.jobs
    )
    d = diagnose_run(run)
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert by[NEVER].job_id in {v.where for v in d.failures}
    assert not any("cancelled before execution" in o for o in d.observations)
