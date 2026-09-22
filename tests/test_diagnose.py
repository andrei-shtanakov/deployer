"""diagnose.py: pure classification of a FailedRun snapshot."""

import json

from deployer.diagnose import (
    RULES,
    FailureVerdict,
    RunDiagnosis,
    classify_failure,
    diagnose_run,
    render_verdict,
)
from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
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


def test_python_file_not_found_is_an_observation_not_authoring():
    """Review #1: a Python exception line is never an AUTHORING citation."""
    v = classify_failure(
        job_with(
            text=(
                "E   FileNotFoundError: [Errno 2] No such file or directory: "
                "'tests/data/fixture.json'"
            )
        ),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "exception: FileNotFoundError: [Errno 2] No such file or directory: "
        "'tests/data/fixture.json'"
    ]


def test_docker_copy_no_such_file_is_still_authoring():
    text = (
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.evidence == [Evidence(None, text)]


def test_exception_shaped_environment_line_is_still_environment():
    """The exception-line exclusion is scoped to `no such file` only."""
    v = classify_failure(
        job_with(
            text=(
                "requests.exceptions.ConnectionError: HTTPConnectionPool: "
                "Temporary failure in name resolution"
            )
        ),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_dotted_exception_names_are_observed():
    v = classify_failure(
        job_with(text="requests.exceptions.ConnectionError: boom"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.observations == ["exception: requests.exceptions.ConnectionError: boom"]


def test_pytest_bare_assert_summary_is_project():
    v = classify_failure(
        job_with(text="FAILED tests/test_x.py::test_x - assert 1 == 2"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT


def test_exact_rules_do_not_match_across_a_newline():
    """`E` on one line and `assert x` on the next is not a pytest assert line."""
    v = classify_failure(job_with(text="E\nassert x"), step=None, completeness=COMPLETE)
    assert v.kind is not FailureKind.PROJECT
    assert v.observations == ["no rule matched"]


def test_cited_line_is_the_marker_line_not_a_bare_prefix():
    v = classify_failure(
        job_with(text="E\nAssertionError: split"), step=None, completeness=COMPLETE
    )
    assert v.kind is FailureKind.PROJECT
    assert v.observations == ["assertion error: AssertionError: split"]


# --- buildkit line framing ---------------------------------------------------
# `docker build` frames every RUN-step output line as `#<step> <seconds> `;
# lines below are copied verbatim from real job logs (evidence/job-*.raw.log).


def test_buildkit_framed_assertion_error_is_still_project():
    text = "#16 0.310 AssertionError: 'hello from ci_build' != 'hello from ci-build'"
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT
    assert v.evidence == [Evidence(None, text)]


def test_buildkit_framed_file_not_found_is_still_an_exception_not_authoring():
    """The negative lookahead must see through the buildkit frame too."""
    text = "#12 0.512 E   FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "exception: FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    ]


def test_leading_whitespace_before_pytest_assert_is_still_project():
    v = classify_failure(
        job_with(text="   E   AssertionError: 1 != 2"), step=None, completeness=COMPLETE
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT


def test_buildkit_framed_connection_timeout_is_environment():
    """The prose ENVIRONMENT rules are already unanchored; this is a pin."""
    text = (
        "#11 15.43   Could not connect to 10.255.255.1:80 (10.255.255.1), "
        "connection timed out"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_real_buildkit_copy_not_found_is_authoring():
    """Verbatim from evidence/run-1.log-failed.txt."""
    text = (
        "ERROR: failed to build: failed to solve: failed to compute cache key: "
        'failed to calculate checksum of ref a4efb8b6::t0jk: "/docs/setup.md": '
        "not found"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING


def test_buildkit_framing_does_not_reopen_the_split_line_gap():
    """`E` and `assert x` on separate buildkit-framed lines still do not match."""
    v = classify_failure(
        job_with(text="#12 0.5 E\n#12 0.5 assert x"), step=None, completeness=COMPLETE
    )
    assert v.kind is not FailureKind.PROJECT


def test_step_verdict_cites_a_job_annotation_with_a_marker():
    """Review #6: int-source (annotation) evidence is in the step pool and citable."""
    target = failed_step(3)
    annotation = Evidence(JOB_ID, "failure: Temporary failure in name resolution")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [annotation]
    assert "cited evidence is job-level (no step binding)" in v.observations


def test_job_level_verdict_does_not_carry_the_job_level_note():
    """Review #7: the note is only informative for a step verdict."""
    v = classify_failure(
        job_with(text="cannot connect to the docker daemon"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED"
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


# --- Task 10: diagnose_run ----------------------------------------------------


def run_with(*jobs: FailedJob, completeness: Completeness = COMPLETE) -> FailedRun:
    return FailedRun(
        repo="acme/app",
        run_id=4242,
        attempt=1,
        head_sha="deadbeef",
        url="https://github.com/acme/app/actions/runs/4242",
        jobs=list(jobs),
        completeness=completeness,
    )


def job_project(job_id: int = JOB_ID) -> FailedJob:
    return job_with(
        text="FAILED tests/test_x.py::test_x - AssertionError: 1 != 2", job_id=job_id
    )


def job_environment(job_id: int = JOB_ID + 1) -> FailedJob:
    return job_with(
        text="E: Failed to fetch http://deb.debian.org/debian/x.deb", job_id=job_id
    )


def job_unclassified(job_id: int = JOB_ID + 2) -> FailedJob:
    return job_with(text="Process completed with exit code 1.", job_id=job_id)


def job_failed_no_steps() -> FailedJob:
    return job_with(text="cannot connect to the docker daemon")


def job_failed_with_steps() -> FailedJob:
    return job_with(
        text="cannot connect to the docker daemon",
        steps=[failed_step(2), failed_step(4)],
    )


def test_two_independent_failures_are_not_a_conflict():
    d = diagnose_run(run_with(job_project(), job_environment()))
    assert d.outcome == "CLASSIFIED"
    assert set(d.causes) == {FailureKind.PROJECT, FailureKind.ENVIRONMENT}


def test_job_order_does_not_change_the_result():
    a = diagnose_run(run_with(job_project(), job_unclassified()))
    b = diagnose_run(run_with(job_unclassified(), job_project()))
    assert a.outcome == b.outcome and set(a.causes) == set(b.causes)


def test_precedence_evidence_unavailable_beats_unclassified():
    d = diagnose_run(run_with(job_unclassified(), completeness=LOGS_UNAVAILABLE))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"


def test_unclassified_summary_keeps_established_causes():
    d = diagnose_run(run_with(job_project(), job_unclassified()))
    assert FailureKind.PROJECT in d.causes


def test_empty_diagnosable_set_is_never_classified():
    """A failed run with no diagnosable failed job/step must not satisfy
    "every element is classified" vacuously."""
    d = diagnose_run(run_with())
    assert d.outcome == "UNCLASSIFIED"
    assert d.failures == [] and d.observations


def test_empty_set_with_lost_data_is_evidence_unavailable():
    d = diagnose_run(run_with(completeness=LOGS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"


def test_empty_set_with_nothing_fetched_is_unclassified_not_lost():
    """Forge's worst-of over zero kept jobs reads 'unavailable'/'absent' because
    nothing was fetched; that is not lost data (amended R10-2)."""
    nothing_fetched = Completeness(logs="unavailable", annotations="absent")
    d = diagnose_run(run_with(completeness=nothing_fetched))
    assert d.outcome == "UNCLASSIFIED"
    assert d.observations == ["failed run exposes no failed job or step"]


def test_empty_set_with_annotations_error_is_evidence_unavailable():
    d = diagnose_run(run_with(completeness=ANNOTATIONS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == ["annotations fetch error"]


def test_non_empty_set_with_logs_unavailable_is_still_incomplete():
    d = diagnose_run(run_with(job_project(), completeness=LOGS_UNAVAILABLE))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.observations == ["logs unavailable"]


def test_failed_job_without_failed_steps_keeps_a_job_level_failure():
    d = diagnose_run(run_with(job_failed_no_steps()))
    assert [v.where for v in d.failures] == [JOB_ID]


def test_itemised_steps_do_not_duplicate_the_job_level_error():
    d = diagnose_run(run_with(job_failed_with_steps()))
    assert all(isinstance(v.where, StepRef) for v in d.failures)


def test_itemised_steps_each_get_a_verdict_in_order():
    d = diagnose_run(run_with(job_failed_with_steps()))
    assert [v.where for v in d.failures] == [StepRef(JOB_ID, 2), StepRef(JOB_ID, 4)]


def test_causes_are_sorted_and_distinct_whatever_the_job_order():
    forward = diagnose_run(run_with(job_project(), job_environment(), job_project(9)))
    backward = diagnose_run(run_with(job_project(9), job_environment(), job_project()))
    assert forward.causes == backward.causes
    assert forward.causes == [FailureKind.ENVIRONMENT, FailureKind.PROJECT]


def test_unclassified_summary_is_the_same_whatever_the_job_order():
    a = diagnose_run(run_with(job_project(), job_unclassified()))
    b = diagnose_run(run_with(job_unclassified(), job_project()))
    assert a.outcome == b.outcome == "UNCLASSIFIED"
    assert a.causes == b.causes == [FailureKind.PROJECT]


def test_empty_set_observation_names_the_shape():
    d = diagnose_run(run_with())
    assert d.observations == ["failed run exposes no failed job or step"]
    assert d.causes == []


def test_empty_set_with_lost_data_names_what_is_missing():
    d = diagnose_run(run_with(completeness=LOGS_ERROR))
    assert d.failures == [] and d.causes == []
    assert d.observations == ["logs fetch error"]


def test_incomplete_run_keeps_the_established_causes_as_verdicts():
    """Precedence changes the summary, not what each failure found."""
    d = diagnose_run(run_with(job_project(), completeness=ANNOTATIONS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert [v.outcome for v in d.failures] == ["EVIDENCE_UNAVAILABLE"]
    assert d.causes == []
    assert any("AssertionError" in o for o in d.failures[0].observations)


def test_run_diagnosis_shape():
    run = run_with(job_project())
    d = diagnose_run(run)
    assert isinstance(d, RunDiagnosis)
    assert d.run is run
    assert (d.outcome, d.causes, d.observations) == (
        "CLASSIFIED",
        [FailureKind.PROJECT],
        [],
    )


# --- Task 11: render_verdict ---------------------------------------------


def test_render_verdict_carries_its_own_schema_version_first():
    # job_project() has no itemised steps (job-level `where`); the steps of
    # job_failed_with_steps() give step-level `where` — both shapes at once.
    d = diagnose_run(run_with(job_project(), job_failed_with_steps()))
    document = json.loads(render_verdict(d))

    assert next(iter(document)) == "verdict_schema_version"
    assert document["verdict_schema_version"] == "1.0"
    assert document["outcome"] == "CLASSIFIED"
    assert document["causes"] == ["environment", "project"]
    assert isinstance(document["failures"], list) and document["failures"]
    wheres = [failure["where"] for failure in document["failures"]]
    assert any(isinstance(where, int) for where in wheres)
    assert any(isinstance(where, dict) for where in wheres)
    for where in wheres:
        if isinstance(where, dict):
            assert set(where) == {"job_id", "number"}
    # The nested run keeps its own, distinct schema version.
    assert document["run"]["snapshot_schema_version"] == "1.0"
