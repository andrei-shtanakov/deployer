"""diagnose.py: pure classification of a FailedRun snapshot."""

import json

from deployer.diagnose import (
    RULES,
    SYMPTOMS,
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
# What forge records for a job whose log came back and that had no
# annotations: the default a hand-built job carries.
READ_COMPLETELY = ANNOTATIONS_ABSENT


def job_with(
    *,
    text: str | None = None,
    evidence: list[Evidence] | None = None,
    steps: list[FailedStep] | None = None,
    job_id: int = JOB_ID,
    completeness: Completeness = READ_COMPLETELY,
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
        completeness=completeness,
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
    annotation = Evidence(JOB_ID, "Process completed with exit code 1.", "failure")
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
    annotation = Evidence(JOB_ID, "Temporary failure in name resolution", "failure")
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
    job = job_project()
    d = diagnose_run(run_with(job, completeness=ANNOTATIONS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert [v.outcome for v in d.failures] == ["CLASSIFIED"]
    assert d.causes == [FailureKind.PROJECT]
    assert any("AssertionError" in o for o in d.failures[0].observations)


def test_a_job_read_incompletely_is_evidence_unavailable_on_its_own():
    """Per-failure incompleteness still wins over that failure's markers."""
    job = job_project()
    unreadable = job_with(
        text=job.evidence[0].text, completeness=ANNOTATIONS_ERROR, job_id=5
    )
    d = diagnose_run(run_with(unreadable))
    assert [v.outcome for v in d.failures] == ["EVIDENCE_UNAVAILABLE"]
    assert d.causes == []
    assert d.observations == ["job 5: annotations fetch error"]


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
    assert document["run"]["snapshot_schema_version"] == "1.2"


# --- final review: I1 pin, I2 apt-warning mitigation ------------------------


def _fail_fast_matrix() -> tuple[FailedJob, FailedJob]:
    """Job 1 fails readably; job 2 is cancelled and its log endpoint errors."""
    readable = job_with(
        text='ERROR: failed to compute cache key: "/docs/setup.md": not found',
        job_id=1,
    )
    unreadable = job_with(job_id=2, completeness=LOGS_ERROR)
    return readable, unreadable


def test_sibling_job_log_error_does_not_erase_an_established_cause():
    """Spec §4: established causes of individual failures are never lost.

    The common shape is a fail-fast matrix. `Completeness` used to be one
    worst-of value for the whole run, handed to every `classify_failure`, so
    job 1's complete and unambiguously AUTHORING evidence was dropped with
    it. Each verdict now depends only on how ITS job was read; the run
    outcome still reports that something could not be looked at.
    """
    readable, unreadable = _fail_fast_matrix()
    d = diagnose_run(run_with(readable, unreadable, completeness=LOGS_ERROR))

    assert [v.outcome for v in d.failures] == [
        "CLASSIFIED",
        "EVIDENCE_UNAVAILABLE",
    ]
    established = d.failures[0]
    assert established.kind is FailureKind.AUTHORING
    assert any("/docs/setup.md" in e.text for e in established.evidence)
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.causes == [FailureKind.AUTHORING]


def test_the_unreadable_sibling_is_named_in_the_run_observations():
    """The operator has to know WHICH job was not read, not only that one was."""
    d = diagnose_run(run_with(*_fail_fast_matrix(), completeness=LOGS_ERROR))
    assert d.observations == ["job 2: logs fetch error"]


def test_a_readable_sibling_pair_is_unaffected():
    """Two complete jobs diagnose exactly as they did before per-job state."""
    readable, _ = _fail_fast_matrix()
    d = diagnose_run(run_with(readable, job_project(job_id=2)))
    assert d.outcome == "CLASSIFIED"
    assert [v.outcome for v in d.failures] == ["CLASSIFIED", "CLASSIFIED"]
    assert d.causes == [FailureKind.AUTHORING, FailureKind.PROJECT]


def test_the_unreadable_sibling_may_come_first():
    """Whose log failed must not depend on the order jobs were listed in."""
    readable, unreadable = _fail_fast_matrix()
    forward = diagnose_run(run_with(readable, unreadable, completeness=LOGS_ERROR))
    backward = diagnose_run(run_with(unreadable, readable, completeness=LOGS_ERROR))
    assert forward.outcome == backward.outcome == "EVIDENCE_UNAVAILABLE"
    assert forward.causes == backward.causes == [FailureKind.AUTHORING]
    assert forward.observations == backward.observations


def test_recovered_apt_warning_does_not_dilute_an_authoring_verdict():
    """The review's probe. A retried-and-recovered apt fetch is one of the
    most common lines in a Debian-based build; before the exclusion it paired
    with the real COPY failure into `ambiguous:` and lost the class."""
    text = (
        "#8 12.1 W: Failed to fetch http://deb.debian.org/debian/x.deb  "
        "Connection timed out [IP: 1.2.3.4 80] [retrying]\n"
        "#8 30.2 apt-get update succeeded on retry\n"
        'ERROR: failed to solve: failed to compute cache key: "/x": not found'
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING


def test_apt_error_line_is_still_environment():
    """The negative twin, verbatim from live acceptance run 2: apt's `E: `
    prefix is a real failure and must keep firing."""
    text = (
        "#11 15.89 E: Failed to fetch http://10.255.255.1/debian/x.deb  "
        "Connection timed out [IP: 10.255.255.1 80]"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_a_warning_line_does_not_mask_the_same_marker_elsewhere():
    """The exclusion is per match, not per rule: a genuine fetch failure
    later in the log still establishes ENVIRONMENT."""
    text = (
        "W: Failed to fetch http://deb.debian.org/x.deb [retrying]\n"
        "E: Failed to fetch http://deb.debian.org/x.deb\n"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_a_warning_shaped_line_establishes_no_class_at_all():
    """Flipped in round 3. The filter used to answer apt's retry noise only,
    so an AUTHORING marker on a `W: ` line still established AUTHORING. A
    warning is not the failure whatever it mentions: apt reports a problem it
    recovered from with the same `W: ` prefix for every subject."""
    text = 'W: failed to compute cache key: "/x": not found'
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"warning-shaped: copy/add source not found: {text}"]


def test_a_warning_annotation_naming_a_missing_file_is_not_authoring():
    """The live shape of the same class: a step that failed for its own
    reason, plus a `warning: ` annotation about an optional file the build
    carried on without. Read as AUTHORING it made `deployer diagnose` exit 0
    with a class nobody had evidence for. It is now doubly excluded -- by its
    level, and because a bare `no such file` names no cause at any level."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        "no such file optional-cache.json; continuing without cache",
        "warning",
    )
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: no such file: warning: {annotation.text}"]


def test_a_failure_annotation_naming_an_artifact_defect_is_authoring():
    """The positive twin by level: `failure` is a failing annotation, so a
    marker that does name a defect of the artifact establishes the class.

    The marker changed with the catalogue audit: the sentence this test used
    to carry (`no such file: /app/src/main.py`) is a symptom, and a symptom
    establishes nothing at any level -- see the demotion tests below."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory",
        "failure",
    )
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.evidence == [annotation]


def test_an_unprefixed_log_line_naming_an_artifact_defect_is_authoring():
    """The positive twin by shape: a plain build-log line carries no level,
    and an unlevelled line is failure evidence."""
    text = (
        "#8 0.42 ERROR: failed to solve: failed to compute cache key: "
        '"/app/main.py": not found'
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING


# --- PR #72 round 2, finding 1: warning-level annotations ---------------------


def _step_with_a_warning_annotation(level: str) -> FailedJob:
    """One failed step whose log says only that the step exited non-zero, plus
    a job annotation at `level` naming a recovered network problem."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, "Connection timed out; retry succeeded", level)
    return job_with(evidence=[annotation], steps=[target])


def test_a_warning_level_annotation_does_not_establish_environment():
    """A `warning` annotation is GitHub saying it noticed something, not a
    failure: the step's only failure evidence is the exit code, so the
    honest verdict is UNCLASSIFIED with the warning kept as an observation.
    The level is rendered back into the observation for the operator."""
    job = _step_with_a_warning_annotation("warning")
    v = classify_failure(job, step=job.steps[0], completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "warning-shaped: connection timed out: warning: Connection timed out; "
        "retry succeeded"
    ]


def test_a_notice_level_annotation_does_not_establish_environment():
    """The other non-failure level GitHub emits."""
    job = _step_with_a_warning_annotation("notice")
    v = classify_failure(job, step=job.steps[0], completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_the_run_of_a_warning_annotation_is_unclassified_not_classified():
    """The exit-code-3 door: `deployer diagnose` must not report exit 0 with
    ENVIRONMENT on a run whose only network line was a recovered warning."""
    d = diagnose_run(run_with(_step_with_a_warning_annotation("warning")))
    assert d.outcome == "UNCLASSIFIED"
    assert d.causes == []


def test_an_error_level_annotation_is_still_environment_evidence():
    """The positive twin of the level test: `error`, like `failure`, is
    failure evidence and keeps establishing a class."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, "Temporary failure in name resolution", "error")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [annotation]


def test_a_warning_annotation_does_not_mask_a_real_log_line():
    """Per piece of evidence, not per run: an unprefixed log line naming the
    same problem still establishes ENVIRONMENT."""
    target = failed_step(1, "curl: (6) Could not resolve host: pypi.org")
    annotation = Evidence(JOB_ID, "Connection timed out; retry succeeded", "warning")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [target.evidence[0]]


# --- PR #72 round 5, finding 1: warning-shaped is judged on the matched line --


def test_a_warning_line_mid_block_does_not_mask_the_block():
    """The review's probe. A single Evidence block mixes a plain line, a
    `W: ` line, and the step's own exit-code line; the `W: ` line is not at
    the block's start. Judged on the matched line itself, the timeout marker
    sits on the warning line and establishes nothing; judged on the block's
    start (the old bug) it would not either, but for the wrong reason -- the
    assertion below pins the intended reason via the observation text naming
    that exact line. (Round 7: a log block carries no annotation level, so
    apt's `W: ` is the whole of the per-line rule.)"""
    text = (
        "Starting checks\n"
        "W: Connection timed out; retrying\n"
        "Process completed with exit code 1."
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "warning-shaped: connection timed out: W: Connection timed out; retrying"
    ]


def test_a_warning_shaped_first_line_does_not_mask_a_real_marker_later_in_the_block():
    """The regression this fix must not introduce: a block that DOES start
    with a warning-shaped line still lets a later, genuinely failing line
    establish a class -- the exclusion is judged per matched line, never by
    whether the block as a whole happens to open with a warning shape."""
    text = (
        "W: something noticed; continuing\n"
        "E: Failed to fetch http://deb.debian.org/debian/x.deb  "
        "Connection timed out [IP: 1.2.3.4 80]"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_an_annotation_that_is_still_a_single_line_stays_unclassified():
    """The commonest annotation shape must keep working: a single-line
    `warning` annotation is judged by its level like any other."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, "Connection timed out; retry succeeded", "warning")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


# --- PR #72 round 6: an annotation's level spans its whole message ----------


def test_a_multiline_warning_annotation_is_warning_shaped_throughout():
    """The round 6 finding: a multi-line annotation message used to leave its
    later lines looking level-less, so one of them could establish a class.
    An annotation's level is a property of the whole message, not of
    whichever line a rule happens to match -- and since round 7 it is a
    field, so no line of the message can contradict it."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        "Recovered network issue\nConnection timed out; retry succeeded",
        "warning",
    )
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert any(o.startswith("warning-shaped:") for o in v.observations)
    d = diagnose_run(run_with(job_with(evidence=[annotation], steps=[target])))
    assert d.outcome == "UNCLASSIFIED"
    assert d.causes == []


def test_a_multiline_failure_annotation_still_establishes_a_class():
    """The positive twin: a `failure` level is failure evidence for every
    line of the message, exactly as `warning` is warning-shaped for every
    line."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        "Recovered network issue\nConnection timed out; retry succeeded",
        "failure",
    )
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [annotation]


# --- PR #72 round 7: an annotation's level is data, never a text prefix ------


def test_an_annotation_opening_with_an_empty_line_is_still_warning_shaped():
    """Round 7, finding 1. The level was read off the start of the evidence
    text, so a message whose first line is empty pushed it out of reach and a
    recovered timeout was read as ENVIRONMENT -- a run reported CLASSIFIED,
    exit 0, off a warning. A level carried as data cannot be hidden by any
    text at all."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, "\nConnection timed out; retry succeeded", "warning")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "warning-shaped: connection timed out: "
        "warning: Connection timed out; retry succeeded"
    ]
    d = diagnose_run(run_with(job_with(evidence=[annotation], steps=[target])))
    assert d.outcome == "UNCLASSIFIED"
    assert d.causes == []


def test_a_python_exception_in_an_annotation_is_an_observation_not_authoring():
    """Round 7, finding 2. The `no such file` rule skips exception-shaped
    lines, but the rendered `failure: ` prefix stood between the line start
    and the exception name, so the identical sentence was an observation as a
    log line and AUTHORING as an annotation. The rule now reads the raw
    message; the level is rendered back only for the operator."""
    target = failed_step(1, "Process completed with exit code 1.")
    message = "FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    annotation = Evidence(JOB_ID, message, "failure")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"exception: failure: {message}"]


def test_the_same_exception_line_reads_the_same_as_a_log_line():
    """The twin of the test above: the log block carries no level, and the
    verdict is the same observation. Rendering, not classification, is the
    only thing the level changes."""
    message = "FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    v = classify_failure(job_with(text=message), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == [f"exception: {message}"]


def test_an_unknown_annotation_level_is_failure_evidence():
    """Only `warning` and `notice` are GitHub saying it noticed something;
    any other level -- `failure`, `error`, or one this catalogue has not
    seen -- is failure evidence, so an unknown level never silently
    suppresses a marker."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, "Temporary failure in name resolution", "fatal")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [annotation]


# --- PR #72 round 7, follow-up: a log line's own warning shapes -------------


def test_a_warning_prefixed_log_line_is_warning_shaped():
    """Round 7 dropped `warning: `/`notice: ` from the per-line rule as an
    artefact of forge's old rendering. That was true of annotations and
    wrong about logs: compilers, pip and shell tooling all emit `warning: `
    lines of their own, and one of them naming a problem it recovered from
    must not establish a class any more than the annotation did."""
    text = (
        "Starting\n"
        "warning: Could not resolve host: proxy.internal; using the direct route\n"
        "Process completed with exit code 1."
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "warning-shaped: host unresolvable: warning: Could not resolve host: "
        "proxy.internal; using the direct route"
    ]


def test_a_notice_prefixed_log_line_is_warning_shaped():
    """The other noticed-not-fatal shape, on a log line rather than a level."""
    text = "notice: Connection timed out; the retry succeeded"
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"warning-shaped: connection timed out: {text}"]


def test_a_warning_log_line_is_warning_shaped_under_buildkit_framing():
    """`docker build` frames every RUN-step line as `#<step> <seconds> `, so
    the warning shape is read after `_LINE_PREFIX`, like every other line
    rule -- not at the raw start of the line."""
    text = "#8 0.42 warning: Could not resolve host: proxy.internal"
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == [f"warning-shaped: host unresolvable: {text}"]


# --- PR #72: the owner's unified rule ---------------------------------------
# The three bullets live verbatim above `RULES`. What they cost here: every
# demoted marker needs a row proving it no longer names a cause, and every
# kept AUTHORING rule needs a negative twin -- the same words in a context
# with a different cause, which must not yield the class.


def _verdict(text: str) -> FailureVerdict:
    """The job-level verdict over one unbound, completely read log block."""
    return classify_failure(job_with(text=text), step=None, completeness=COMPLETE)


def test_a_bare_no_such_file_line_is_a_symptom_not_authoring():
    """Demotion. A wrong invocation, a missing dependency and a project defect
    all print this; the authored artifact is one candidate among several. It
    is recorded so the operator sees it, and it names no cause."""
    text = "#8 0.42 cp: cannot stat '/app/main.py': No such file or directory"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: no such file: {text}"]


def test_exec_format_error_is_a_symptom_not_authoring():
    """Demotion. An amd64 image on an arm64 runner is an environment mismatch
    as readily as a wrong `--platform` in the Dockerfile."""
    text = "standard_init_linux.go:228: exec user process caused: exec format error"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: exec format error: {text}"]


def test_executable_file_not_found_outside_the_exec_shape_is_a_symptom():
    """Demotion. Only docker's own `exec:` line says the IMAGE's entrypoint is
    the thing that is missing; the bare sentence says nothing of the kind."""
    text = "E   RuntimeError: executable file not found in the sandbox"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "exception: RuntimeError: executable file not found in the sandbox",
        f"symptom: executable not found: {text}",
    ]


def test_a_plain_shell_stat_line_is_not_a_copy_failure():
    """Negative twin of the COPY/ADD rules: the same words without docker's
    own framing are a shell reporting a missing file, whoever's fault it is."""
    text = "stat app.py: no such file or directory"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == [f"symptom: no such file: {text}"]


def test_cat_cannot_open_a_config_is_not_a_copy_failure():
    """The same twin in the shape a test step prints."""
    text = "cat: config.yml: No such file or directory"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == [f"symptom: no such file: {text}"]


def test_a_buildkit_run_failure_without_a_missing_path_is_not_authoring():
    """Negative twin of the buildkit COPY shape. `failed to solve` heads every
    buildkit failure, including a RUN step whose command exited non-zero --
    the commonest PROJECT shape there is. Only the quoted path the build could
    not find makes it a defect of the artifact."""
    text = (
        'ERROR: failed to solve: process "/bin/sh -c uv sync --frozen" '
        "did not complete successfully: exit code: 1"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == ["no rule matched"]


def test_a_shell_command_not_found_is_not_an_entrypoint_defect():
    """Negative twin of the old entrypoint rule: a shell inside a RUN step,
    not the image's CMD/ENTRYPOINT."""
    v = _verdict("bash: foo: command not found")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == ["no rule matched"]


def test_the_docker_exec_shape_is_a_symptom_not_authoring():
    """Demotion (owner, 2026-09-22). Docker's `exec:` line proves a missing
    executable, NOT that its name came from the image's CMD/ENTRYPOINT: the
    identical line is printed when the command is overridden at run time
    (`docker run --entrypoint`, a `container:`/`options:` job, a compose
    `command:`). The snapshot cannot tell those apart, so it observes."""
    text = (
        "docker: Error response from daemon: failed to create task for container: "
        "OCI runtime create failed: unable to start container process: "
        'exec: "serve": executable file not found in $PATH: unknown.'
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: entrypoint executable not found: {text}"]


def test_an_unresolvable_action_is_a_symptom_not_authoring():
    """Demotion (owner, 2026-09-22). `unable to resolve action` is not always
    a YAML defect: a reference that was valid when the workflow was authored
    prints the same line once the upstream tag or repository is gone. The
    snapshot carries nothing that separates a typo from a deletion."""
    text = "Unable to resolve action actions/checkout@v99, unable to find version v99"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: unresolvable action: {text}"]


def test_the_buildkit_copy_shape_must_name_the_missing_path():
    """The kept shape is buildkit's own: `failed to solve`/`failed to compute
    cache key` AND the quoted path it could not find, at the line's end."""
    v = _verdict('ERROR: failed to compute cache key: "/docs/setup.md": not found')
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING


def test_symptoms_are_a_separate_table_that_names_no_cause():
    """Structural, not per-verdict: a symptom cannot be a class because the
    only kind it carries is the one `CLASSIFIED` never admits."""
    assert SYMPTOMS
    assert all(rule.kind is FailureKind.UNKNOWN for rule in SYMPTOMS)
    assert len({rule.name for rule in SYMPTOMS}) == len(SYMPTOMS)
    assert {rule.name for rule in SYMPTOMS}.isdisjoint({rule.name for rule in RULES})


def test_no_rule_of_the_catalogue_fires_on_a_bare_symptom_line():
    """The demotion checked against the catalogue itself, not one verdict."""
    for text in (
        "stat app.py: no such file or directory",
        "cat: config.yml: No such file or directory",
        "standard_init_linux.go:228: exec user process caused: exec format error",
        "E   RuntimeError: executable file not found in the sandbox",
        "bash: foo: command not found",
        'exec: "serve": executable file not found in $PATH: unknown.',
        "Unable to resolve action actions/checkout@v99, unable to find version v99",
    ):
        assert not [rule.name for rule in RULES if rule.pattern.search(text)], text


def test_a_symptom_beside_an_established_cause_is_observed_not_a_conflict():
    """A symptom is not a second kind: it cannot turn a clean verdict into
    `ambiguous:`, and the operator still reads it."""
    text = (
        'ERROR: failed to solve: failed to compute cache key: "/docs/setup.md": '
        "not found\n"
        "cp: cannot stat '/app/main.py': No such file or directory"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.observations[-1].startswith("symptom: no such file: ")


# --- PR #72 pre-final: the evidence contract cites the ACTUAL matches --------
# A verdict that cites a BLOCK and names only its first matching line hides the
# rest of what the run printed. `FailureVerdict.evidence` stays the block --
# that is the provenance-carrying unit forge produced -- while every matched
# LINE gets an observation of its own, in document order, deduplicated.


def test_two_missing_files_in_one_block_are_two_symptom_observations():
    """One block, one symptom rule, two different lines: two observations.
    Reporting only the first leaves the operator to guess there was a second
    file, which is exactly the fact a diagnosis exists to carry."""
    first = "cp: cannot stat '/app/main.py': No such file or directory"
    second = "cp: cannot stat '/app/util.py': No such file or directory"
    v = _verdict(f"Copying sources\n{first}\n{second}\nDone")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == [
        f"symptom: no such file: {first}",
        f"symptom: no such file: {second}",
    ]


def test_two_copy_failures_in_one_block_are_two_observations_one_citation():
    """The same, for a classifying rule: two cited lines, one cited block."""
    first = 'ERROR: failed to compute cache key: "/docs/setup.md": not found'
    second = 'ERROR: failed to compute cache key: "/docs/usage.md": not found'
    block = f"#12 [stage-0 7/9] COPY docs ./docs\n{first}\n{second}"
    v = _verdict(block)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.evidence == [Evidence(None, block)]
    assert v.observations == [
        f"copy/add source not found: {first}",
        f"copy/add source not found: {second}",
    ]


def test_an_observation_quotes_the_line_the_rule_matched_not_the_first():
    """A marker on line 3 of a block is quoted from line 3."""
    marker = "ERROR: unknown instruction: FORM (did you mean FROM?)"
    v = _verdict(f"#1 [internal] load build definition\n#1 transferring\n{marker}")
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.observations == [f"unknown instruction: {marker}"]


def test_one_line_matched_twice_by_a_rule_is_cited_once():
    """Deduplication: `finditer` can land twice inside one line, and the
    operator reads lines, not match offsets."""
    line = "ERROR: unknown instruction: FORM -- no unknown instruction is accepted"
    v = _verdict(line)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.observations == [f"unknown instruction: {line}"]


def test_evidence_unavailable_keeps_the_symptoms_and_exceptions_found():
    """Spec §3: "markers already found are preserved as observations". That
    is the whole point of the outcome -- the operator sees what the partial
    read DID show beside what could not be read -- and it must hold for the
    symptom- and exception-shaped observations too, not only for the rule
    matches. Dropping them made an unreadable run look emptier than it was."""
    symptom = "cp: cannot stat '/app/main.py': No such file or directory"
    exception = "ValueError: bad configuration"
    v = classify_failure(
        job_with(text=f"{symptom}\n{exception}"),
        step=None,
        completeness=LOGS_UNAVAILABLE,
    )
    assert v.outcome == "EVIDENCE_UNAVAILABLE" and v.kind is None
    assert v.evidence == []
    assert v.observations == [
        f"exception: {exception}",
        f"symptom: no such file: {symptom}",
        "logs unavailable",
    ]


def test_evidence_unavailable_keeps_a_warning_shaped_match_too():
    """The third shape of observation on that branch, pinned beside the
    other two."""
    warned = "W: Failed to fetch http://deb.debian.org/x.deb  [retrying]"
    v = classify_failure(
        job_with(text=warned), step=None, completeness=LOGS_UNAVAILABLE
    )
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert [o for o in v.observations if o.startswith("warning-shaped: ")]
    assert v.observations[-1] == "logs unavailable"
