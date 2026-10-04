"""Diagnose with recognised jobs (spec §5, §6, §6.1, §3.3)."""

import json
from dataclasses import replace

from deployer.diagnose import diagnose_run
from deployer.forge import (
    AnnotationsState,
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
    FailedStep,
    LogsState,
    StepInfo,
    StepRef,
    dump_snapshot,
    load_snapshot,
)
from deployer.notexecuted import NotExecuted

TS = "2026-10-04T09:13:25Z"
NE = NotExecuted("completed", "cancelled", 0, "", 0, TS, TS, 404)
CANCELLED = "job 9 (job-9) was cancelled before execution (runner 0, no steps, log 404)"
IGNORED = "job 9: stored not_executed contradicts the job; ignored"
ALL = "every non-green job was cancelled before execution; no job log holds the failure"


def _recognised(
    annotations: AnnotationsState = "absent", evidence: list[Evidence] | None = None
) -> FailedJob:
    return FailedJob(
        9,
        "job-9",
        "cancelled",
        [],
        evidence or [],
        Completeness("error", annotations),
        all_steps=[],
        not_executed=NE,
    )


def _ran(logs: LogsState = "present") -> FailedJob:
    ref = StepRef(1, 2)
    step = FailedStep(ref, "Run a", "failure", [])
    text = "AssertionError: boom"
    return FailedJob(
        1,
        "job-1",
        "failure",
        [step],
        [Evidence(ref, text)] if logs == "present" else [],
        Completeness(logs, "absent"),
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, "Run a", "failure"),
        ],
    )


def _run(
    *jobs: FailedJob,
    logs: LogsState = "present",
    annotations: AnnotationsState = "absent",
) -> FailedRun:
    return FailedRun(
        "o/r",
        1,
        1,
        "sha",
        "url",
        list(jobs),
        Completeness(logs, annotations),
    )


def test_a_recognised_job_gets_no_verdict_and_does_not_degrade_the_run() -> None:
    d = diagnose_run(_run(_recognised(), _ran()))
    assert [v.where for v in d.failures] == [StepRef(1, 2)]
    assert d.outcome == "UNCLASSIFIED"
    assert d.observations == [CANCELLED]


def test_a_real_lost_log_beside_an_exclusion_is_reported_alone() -> None:
    d = diagnose_run(_run(_recognised(), _ran(logs="error"), logs="error"))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == [CANCELLED, "job 1: logs fetch error"]


def test_a_recognised_job_s_annotations_error_still_counts() -> None:
    d = diagnose_run(
        _run(_recognised(annotations="error"), _ran(), annotations="error")
    )
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert [v.where for v in d.failures] == [StepRef(1, 2)]
    assert d.observations == [CANCELLED, "job 9: annotations fetch error"]


def test_annotation_evidence_of_a_recognised_job_is_kept_but_not_cited() -> None:
    job = _recognised(evidence=[Evidence(9, "The job was cancelled", "failure")])
    d = diagnose_run(_run(job, _ran()))
    assert d.run.jobs[0].evidence == job.evidence
    assert all(e.source != 9 for v in d.failures for e in v.evidence)


def test_every_kept_job_recognised_is_explicitly_unavailable() -> None:
    d = diagnose_run(_run(_recognised(), logs="unavailable"))
    assert d.failures == []
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == [CANCELLED, ALL]


def test_every_kept_job_recognised_still_reports_an_annotations_error() -> None:
    d = diagnose_run(
        _run(_recognised(annotations="error"), logs="unavailable", annotations="error")
    )
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == [CANCELLED, ALL, "job 9: annotations fetch error"]


def test_zero_kept_jobs_is_unchanged() -> None:
    d = diagnose_run(_run(logs="unavailable"))
    assert d.outcome == "UNCLASSIFIED"
    assert d.observations == ["failed run exposes no failed job or step"]


def test_a_dispatcher_shaped_sibling_is_evaluated_normally() -> None:
    dispatcher = replace(_recognised(), not_executed=None)
    d = diagnose_run(_run(dispatcher, _ran(), logs="error"))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert ALL not in d.observations
    assert {v.where for v in d.failures} == {9, StepRef(1, 2)}


def test_a_contradicting_stored_basis_is_ignored_with_a_note() -> None:
    contradicted = replace(_recognised(), conclusion="failure")
    d = diagnose_run(_run(contradicted, _ran(), logs="error"))
    assert "job 9: stored not_executed contradicts the job; ignored" in d.observations
    assert 9 in {v.where for v in d.failures}
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert "job 9: logs fetch error" in d.observations


def test_a_1_5_snapshot_diagnoses_exactly_as_before() -> None:
    original = _run(_ran(), replace(_recognised(), not_executed=None), logs="error")
    document = json.loads(dump_snapshot(original))
    document["snapshot_schema_version"] = "1.5"
    for job in document["jobs"]:
        del job["not_executed"]
    loaded = load_snapshot(json.dumps(document))
    before, after = diagnose_run(original), diagnose_run(loaded)
    assert after.failures == before.failures
    assert after.outcome == before.outcome == "EVIDENCE_UNAVAILABLE"
    assert after.observations == before.observations


def test_a_stored_basis_contradicted_by_log_status_is_ignored() -> None:
    ne = replace(NE, log_status=410)
    d = diagnose_run(_run(replace(_recognised(), not_executed=ne), _ran()))
    assert IGNORED in d.observations
    assert 9 in {v.where for v in d.failures}


def test_a_stored_basis_contradicted_by_log_evidence_is_ignored() -> None:
    job = _recognised(evidence=[Evidence(None, "x")])
    d = diagnose_run(_run(job, _ran()))
    assert IGNORED in d.observations
    assert 9 in {v.where for v in d.failures}
