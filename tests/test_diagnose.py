"""diagnose.py: pure classification of a FailedRun snapshot."""

from deployer.diagnose import (
    RULES,
    FailureVerdict,
    classify_failure,
)
from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedStep,
    StepRef,
)
from deployer.models import FailureKind

JOB_ID = 77
COMPLETE = Completeness(logs="present", annotations="present")
LOGS_UNAVAILABLE = Completeness(logs="unavailable", annotations="present")
LOGS_ERROR = Completeness(logs="error", annotations="present")
ANNOTATIONS_ERROR = Completeness(logs="present", annotations="error")
ANNOTATIONS_ABSENT = Completeness(logs="present", annotations="absent")


def job_with(
    *,
    text: str | None = None,
    evidence: list[Evidence] | None = None,
    steps: list[FailedStep] | None = None,
    job_id: int = JOB_ID,
) -> FailedJob:
    """A failed job; ``text`` lands in ``job.evidence`` unbound (source=None)."""
    if evidence is None:
        evidence = [] if text is None else [Evidence(source=None, text=text)]
    return FailedJob(
        job_id=job_id,
        name="test",
        conclusion="failure",
        steps=steps or [],
        evidence=evidence,
    )


def failed_step(number: int, *texts: str, job_id: int = JOB_ID) -> FailedStep:
    ref = StepRef(job_id, number)
    return FailedStep(
        ref=ref,
        name=f"step-{number}",
        conclusion="failure",
        evidence=[Evidence(source=ref, text=text) for text in texts],
    )


# --- Task 9: classify_failure -------------------------------------------------


def test_class_requires_a_citation():
    """A rule that cannot cite contributes an observation, not a class."""
    v = classify_failure(job_with(evidence=[]), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_project_needs_positive_evidence_not_absence_of_markers():
    v = classify_failure(
        job_with(text="FAILED test_x - AssertionError: 1 != 2"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT
    assert v.evidence


def test_similar_message_different_cause_does_not_reuse_the_class():
    """Rule soundness: a citation is necessary, not sufficient."""
    v = classify_failure(
        job_with(text="AssertionError in the runner's own setup"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.kind is not FailureKind.PROJECT


def test_incomplete_evidence_wins_over_a_found_marker():
    v = classify_failure(
        job_with(text="AssertionError: 1 != 2"),
        step=None,
        completeness=LOGS_UNAVAILABLE,
    )
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert "AssertionError" in " ".join(v.observations)


def test_conflicting_causes_for_one_failure_are_unclassified():
    """Not resolved by iteration order."""
    v = classify_failure(
        job_with(text="cannot connect to the docker daemon\nAssertionError: 1 != 2"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED"
    assert "ambiguous" in " ".join(v.observations).lower()


def test_conflict_is_the_same_with_the_evidence_reversed():
    forward = job_with(
        evidence=[
            Evidence(None, "cannot connect to the docker daemon"),
            Evidence(None, "E   AssertionError: 1 != 2"),
        ]
    )
    backward = job_with(evidence=list(reversed(forward.evidence)))
    a = classify_failure(forward, step=None, completeness=COMPLETE)
    b = classify_failure(backward, step=None, completeness=COMPLETE)
    assert a.outcome == b.outcome == "UNCLASSIFIED"
    assert a.kind is b.kind is FailureKind.UNKNOWN
    assert a.evidence == b.evidence == []
    assert a.observations[0].startswith("ambiguous: ")
    assert "environment" in a.observations[0] and "project" in a.observations[0]


def test_module_not_found_is_an_observation_not_a_class():
    """Wrong dependencies and a project defect share this symptom (spec §5)."""
    v = classify_failure(
        job_with(text="E   ModuleNotFoundError: No module named 'flask'"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == ["exception: ModuleNotFoundError: No module named 'flask'"]


def test_no_marker_at_all_says_so():
    v = classify_failure(
        job_with(text="Process completed with exit code 1."),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED"
    assert v.observations == ["no rule matched"]


def test_authoring_marker_is_classified_with_its_evidence():
    text = (
        "ERROR: failed to solve: failed to compute cache key: "
        '"/requirements.txt": not found'
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.evidence == [Evidence(None, text)]


def test_environment_marker_is_classified_with_its_evidence():
    v = classify_failure(
        job_with(text="Temporary failure in name resolution"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_several_markers_of_one_kind_cite_each_item_once():
    e1 = Evidence(None, "no space left on device\nno space left on device")
    e2 = Evidence(None, "toomanyrequests: rate limit exceeded")
    v = classify_failure(job_with(evidence=[e1, e2]), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [e1, e2]


def test_step_verdict_excludes_evidence_bound_to_another_step():
    """A block bound to a (green) sibling step is not this step's evidence."""
    target = failed_step(3)
    other = Evidence(StepRef(JOB_ID, 1), "cannot connect to the docker daemon")
    job = job_with(evidence=[other], steps=[target])
    v = classify_failure(job, step=target, completeness=COMPLETE)
    assert v.where == target.ref
    assert v.outcome == "UNCLASSIFIED" and v.evidence == []


def test_step_verdict_uses_its_own_and_unbound_job_evidence():
    target = failed_step(3, "##[group]Run pytest")
    unbound = Evidence(None, "FAILED tests/test_x.py::test_x - AssertionError: 1 != 2")
    annotation = Evidence(JOB_ID, "failure: Process completed with exit code 1.")
    job = job_with(evidence=[unbound, annotation], steps=[target])
    v = classify_failure(job, step=target, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT
    assert v.evidence == [unbound]
    assert "cited evidence is job-level (no step binding)" in v.observations


def test_step_verdict_cites_step_bound_evidence_without_the_job_level_note():
    target = failed_step(
        2, "Dockerfile parse error on line 3: unknown instruction: FORM"
    )
    v = classify_failure(job_with(steps=[target]), step=target, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.evidence == target.evidence
    assert "cited evidence is job-level (no step binding)" not in v.observations


def test_job_level_verdict_is_addressed_by_job_id():
    v = classify_failure(job_with(text="x"), step=None, completeness=COMPLETE)
    assert v.where == JOB_ID


def test_annotations_error_is_incomplete_even_with_logs_present():
    v = classify_failure(
        job_with(text="cannot connect to the docker daemon"),
        step=None,
        completeness=ANNOTATIONS_ERROR,
    )
    assert v.outcome == "EVIDENCE_UNAVAILABLE" and v.kind is None
    assert v.evidence == []
    assert "annotations fetch error" in v.observations
    assert any("cannot connect to the docker daemon" in o for o in v.observations)


def test_absent_annotations_are_optional_not_incompleteness():
    v = classify_failure(
        job_with(text="cannot connect to the docker daemon"),
        step=None,
        completeness=ANNOTATIONS_ABSENT,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_logs_error_names_what_is_missing():
    v = classify_failure(job_with(evidence=[]), step=None, completeness=LOGS_ERROR)
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert v.observations == ["logs fetch error"]


def test_rules_are_data_and_never_classify_as_unknown():
    assert RULES
    assert all(rule.kind is not FailureKind.UNKNOWN for rule in RULES)
    assert len({rule.name for rule in RULES}) == len(RULES)


def test_verdict_shape():
    v = classify_failure(job_with(evidence=[]), step=None, completeness=COMPLETE)
    assert isinstance(v, FailureVerdict)
    assert (v.where, v.outcome, v.kind) == (JOB_ID, "UNCLASSIFIED", FailureKind.UNKNOWN)
