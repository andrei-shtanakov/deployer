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

# A recovered network problem in the shape the owner's evidence rule requires
# (2026-09-22): apt's `Err:` detail line names the host and the port it could
# not reach, which is what separates a TOOL's own report from an application
# printing the same words. The bare phrase establishes nothing at any level
# now, so a warning-level test written on it would pass for the wrong reason.
RECOVERED_TIMEOUT = "Could not connect to pypi.org:443, connection timed out"


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
        job_with(text="FAILED tests/test_x.py::test_x - AssertionError: 1 != 2"),
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
        job_with(
            text=(
                "cannot connect to the docker daemon\n"
                'File "tests/test_x.py", line 10, in test_x\n'
                "AssertionError: 1 != 2"
            )
        ),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED"
    assert "ambiguous" in " ".join(v.observations).lower()


def test_conflict_is_the_same_with_the_evidence_reversed():
    forward = job_with(
        evidence=[
            Evidence(None, "cannot connect to the docker daemon"),
            Evidence(
                None,
                'File "tests/test_x.py", line 10, in test_x\n'
                "E   AssertionError: 1 != 2",
            ),
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
        "ERROR: failed to solve: dockerfile parse error on line 3: "
        "FROM requires either one or three arguments"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.evidence == [Evidence(None, text)]


def test_environment_marker_is_classified_with_its_evidence():
    v = classify_failure(
        job_with(text="E: Temporary failure resolving 'deb.debian.org'"),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_several_markers_of_one_kind_cite_each_item_once():
    e1 = Evidence(
        None,
        "failed to solve: write /var/lib/docker/tmp/a: no space left on device\n"
        "failed to solve: write /var/lib/docker/tmp/b: no space left on device",
    )
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
        2, "Dockerfile parse error on line 3: unexpected end of statement"
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


def test_docker_copy_no_such_file_is_a_symptom_not_authoring():
    """Demoted by the owner's evidence rule (2026-09-22). docker names the
    source it could not resolve against the build context -- but the context
    IS the checkout, so a path the COPY never had right and a file the
    project moved after the Dockerfile was authored print the same sentence.
    The line is reported, twice: by the builder's own shape and by the bare
    `no such file` it also carries."""
    text = (
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        f"symptom: copy/add failed in build context: {text}",
        f"symptom: no such file: {text}",
    ]


def test_exception_shaped_environment_line_is_still_environment():
    """The exception-line exclusion is scoped to `no such file` only.

    The marker carries docker's own framing (the evidence rule): a client
    that could not reach the daemon says so in its own words, whoever
    called it."""
    v = classify_failure(
        job_with(
            text=(
                "docker.errors.DockerException: Cannot connect to the Docker "
                "daemon at unix:///var/run/docker.sock"
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
        job_with(
            text='File "tests/test_x.py", line 3, in test_x\nE\nAssertionError: split'
        ),
        step=None,
        completeness=COMPLETE,
    )
    assert v.kind is FailureKind.PROJECT
    assert v.observations == ["assertion error: AssertionError: split"]


# --- buildkit line framing ---------------------------------------------------
# `docker build` frames every RUN-step output line as `#<step> <seconds> `;
# lines below are copied verbatim from real job logs (evidence/job-*.raw.log).


def test_buildkit_framed_assertion_error_is_still_project():
    text = (
        '#16 0.310   File "/app/tests/test_greeting.py", line 10, in test_greeting\n'
        "#16 0.310 AssertionError: 'hello from ci_build' != 'hello from ci-build'"
    )
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
        job_with(
            text='File "tests/test_x.py", line 3, in test_x\n'
            "   E   AssertionError: 1 != 2"
        ),
        step=None,
        completeness=COMPLETE,
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT


def test_buildkit_framed_connection_timeout_is_environment():
    """The prose ENVIRONMENT rules are unanchored; this is a pin. The line is
    apt's `Err:` detail, which names the host and the port it could not
    reach -- the framing the evidence rule requires."""
    text = (
        "#11 15.43   Could not connect to 10.255.255.1:80 (10.255.255.1), "
        "connection timed out"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_real_buildkit_copy_not_found_is_an_observation():
    """Verbatim from evidence/run-1.log-failed.txt -- the line live
    acceptance run 1 really printed. It established AUTHORING until the
    owner's evidence rule; it is an observation now, and run 1 is
    UNCLASSIFIED (`tests/test_fixture_runs.py`)."""
    text = (
        "ERROR: failed to build: failed to solve: failed to compute cache key: "
        'failed to calculate checksum of ref a4efb8b6::t0jk: "/docs/setup.md": '
        "not found"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: copy/add source not found: {text}"]


def test_buildkit_framing_does_not_reopen_the_split_line_gap():
    """`E` and `assert x` on separate buildkit-framed lines still do not match."""
    v = classify_failure(
        job_with(text="#12 0.5 E\n#12 0.5 assert x"), step=None, completeness=COMPLETE
    )
    assert v.kind is not FailureKind.PROJECT


def test_step_verdict_cites_a_job_annotation_with_a_marker():
    """Review #6: int-source (annotation) evidence is in the step pool and citable."""
    target = failed_step(3)
    annotation = Evidence(
        JOB_ID, "E: Temporary failure resolving 'deb.debian.org'", "failure"
    )
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
        text=(
            "ERROR: failed to solve: dockerfile parse error on line 3: "
            "FROM requires either one or three arguments"
        ),
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
    assert any("dockerfile parse error" in e.text for e in established.evidence)
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
        "#8 12.1 W: Could not connect to deb.debian.org:80 (1.2.3.4), "
        "connection timed out [retrying]\n"
        "#8 30.2 apt-get update succeeded on retry\n"
        "ERROR: failed to solve: dockerfile parse error on line 3: "
        "FROM requires either one or three arguments"
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
    text = "W: curl: (6) Could not resolve host: proxy.internal [retrying]"
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"warning-shaped: host unresolvable: {text}"]


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
        "The workflow is not valid. .github/workflows/ci.yml (Line: 31, "
        "Col: 9): Unrecognized named-value: 'secret'",
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
        "#8 0.42 ERROR: failed to solve: dockerfile parse error on line 3: "
        "FROM requires either one or three arguments"
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING


# --- PR #72 round 2, finding 1: warning-level annotations ---------------------


def _step_with_a_warning_annotation(level: str) -> FailedJob:
    """One failed step whose log says only that the step exited non-zero, plus
    a job annotation at `level` naming a recovered network problem."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, f"{RECOVERED_TIMEOUT}; retry succeeded", level)
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
        f"warning-shaped: connection timed out: warning: {RECOVERED_TIMEOUT}; "
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
    annotation = Evidence(
        JOB_ID, "E: Temporary failure resolving 'deb.debian.org'", "error"
    )
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
    annotation = Evidence(JOB_ID, f"{RECOVERED_TIMEOUT}; retry succeeded", "warning")
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
        f"W: {RECOVERED_TIMEOUT}; retrying\n"
        "Process completed with exit code 1."
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        f"warning-shaped: connection timed out: W: {RECOVERED_TIMEOUT}; retrying"
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
    annotation = Evidence(JOB_ID, f"{RECOVERED_TIMEOUT}; retry succeeded", "warning")
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
        f"Recovered network issue\n{RECOVERED_TIMEOUT}; retry succeeded",
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
        f"Recovered network issue\n{RECOVERED_TIMEOUT}; retry succeeded",
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
    annotation = Evidence(JOB_ID, f"\n{RECOVERED_TIMEOUT}; retry succeeded", "warning")
    v = classify_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "warning-shaped: connection timed out: "
        f"warning: {RECOVERED_TIMEOUT}; retry succeeded"
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
    annotation = Evidence(
        JOB_ID, "E: Temporary failure resolving 'deb.debian.org'", "fatal"
    )
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
        "warning: curl: (6) Could not resolve host: proxy.internal\n"
        "Process completed with exit code 1."
    )
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        "warning-shaped: host unresolvable: warning: curl: (6) Could not "
        "resolve host: proxy.internal"
    ]


def test_a_notice_prefixed_log_line_is_warning_shaped():
    """The other noticed-not-fatal shape, on a log line rather than a level."""
    text = f"notice: {RECOVERED_TIMEOUT}; the retry succeeded"
    v = classify_failure(job_with(text=text), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"warning-shaped: connection timed out: {text}"]


def test_a_warning_log_line_is_warning_shaped_under_buildkit_framing():
    """`docker build` frames every RUN-step line as `#<step> <seconds> `, so
    the warning shape is read after `_LINE_PREFIX`, like every other line
    rule -- not at the raw start of the line."""
    text = "#8 0.42 warning: curl: (6) Could not resolve host: proxy.internal"
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


def test_the_buildkit_copy_shape_is_an_observation_not_authoring():
    """Demotion (owner's evidence rule, 2026-09-22). buildkit's shape does
    name the path it could not resolve against the build context -- but the
    context IS the checkout the Dockerfile was authored against, so a path
    the COPY never had right and a file the project moved AFTER the
    Dockerfile was authored print the identical line. The snapshot cannot
    say which side moved, so it does not name the artifact."""
    text = 'ERROR: failed to compute cache key: "/docs/setup.md": not found'
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: copy/add source not found: {text}"]


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
        # Demoted by the evidence rule of 2026-09-22: the COPY/ADD shapes,
        # `unknown instruction` (with the parser's framing beside it, which
        # proves only that both sentences share a line), and every BARE
        # ENVIRONMENT phrase an application under test prints for itself.
        'ERROR: failed to compute cache key: "/docs/setup.md": not found',
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory",
        "dockerfile parse error on line 7: unknown instruction: FROBNICATE",
        "connection timed out",
        "503 Service Unavailable",
        "no space left on device",
        "Temporary failure in name resolution",
        "Could not resolve host: pypi.org",
        "toomanyrequests",
    ):
        assert not [rule.name for rule in RULES if rule.pattern.search(text)], text


def test_a_symptom_beside_an_established_cause_is_observed_not_a_conflict():
    """A symptom is not a second kind: it cannot turn a clean verdict into
    `ambiguous:`, and the operator still reads it."""
    text = (
        "ERROR: failed to solve: dockerfile parse error on line 3: "
        "FROM requires either one or three arguments\n"
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


def test_two_fetch_failures_in_one_block_are_two_observations_one_citation():
    """The same, for a classifying rule: two cited lines, one cited block.
    apt really does print one `E: Failed to fetch` per index it could not
    reach, so reporting only the first hides half the failure."""
    first = "E: Failed to fetch http://deb.debian.org/debian/a.deb  404  Not Found"
    second = "E: Failed to fetch http://deb.debian.org/debian/b.deb  404  Not Found"
    block = f"#9 [stage-0 3/9] RUN apt-get install -y a b\n{first}\n{second}"
    v = _verdict(block)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [Evidence(None, block)]
    assert v.observations == [
        f"fetch failure: {first}",
        f"fetch failure: {second}",
    ]


def test_an_observation_quotes_the_line_the_rule_matched_not_the_first():
    """A marker on line 3 of a block is quoted from line 3.

    The marker is the parser's own sentence about the Dockerfile's text --
    a plain syntax error, with no `unknown instruction` beside it, which is
    the whole of what AUTHORING keeps after the evidence rule.
    """
    marker = "dockerfile parse error on line 3: unexpected end of statement"
    v = _verdict(f"#1 [internal] load build definition\n#1 transferring\n{marker}")
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.observations == [f"dockerfile parse error: {marker}"]


def test_one_line_matched_twice_by_a_rule_is_cited_once():
    """Deduplication: `finditer` can land twice inside one line, and the
    operator reads lines, not match offsets."""
    line = (
        "Could not connect to pypi.org:443, connection timed out; "
        "retried: Could not connect to pypi.org:443, connection timed out"
    )
    v = _verdict(line)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.observations == [f"connection timed out: {line}"]


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
    warned = f"W: {RECOVERED_TIMEOUT} [retrying]"
    v = classify_failure(
        job_with(text=warned), step=None, completeness=LOGS_UNAVAILABLE
    )
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert [o for o in v.observations if o.startswith("warning-shaped: ")]
    assert v.observations[-1] == "logs unavailable"


# --- PR #72 final review round, finding 1: AUTHORING prose anchored to the --
# parser's own framing, not the bare words -----------------------------------
# `unknown instruction` and `unrecognized named-value` are ordinary English:
# an application can say the first about its own vocabulary, and a test can
# quote the second while asserting on it. Anchoring both to the parser's own
# line (buildkit/the legacy daemon; GitHub's workflow validator) is what
# makes "no other cause prints that shape" actually true.


def test_a_bare_unknown_instruction_in_an_application_exception_is_not_authoring():
    """The finding itself: an app's own `ValueError` about ITS OWN unknown
    instruction has never seen a Dockerfile. The exception rule still
    records it; no class is established."""
    text = "ValueError: unknown instruction: frobnicate"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        f"exception: {text}",
        f"symptom: unknown instruction: {text}",
    ]


def test_buildkits_own_unknown_instruction_line_establishes_nothing():
    """Demotion (owner's evidence rule, 2026-09-22). The previous round kept
    this shape because the parser's framing and the bad instruction share a
    line. They still do -- and that is exactly what an application quoting
    the parser also produces, so the framing beside the words proves only
    that two sentences share a line. `unknown instruction` is a symptom now,
    and `dockerfile parse error` refuses a line carrying it."""
    text = "dockerfile parse error on line 7: unknown instruction: FROBNICATE"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"symptom: unknown instruction: {text}"]


def test_the_legacy_daemons_one_line_shape_is_demoted_too():
    """The pre-buildkit daemon's framing, demoted with it: the class is the
    unit, not the instance."""
    text = (
        "Error response from daemon: dockerfile parse error line 7: "
        "unknown instruction: FROBNICATE"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_a_plain_dockerfile_syntax_error_is_still_authoring():
    """The positive twin of the two demotions above, and the whole of what
    AUTHORING keeps for the Dockerfile: the parser read the artifact's bytes
    and could not, in words no other tool prints and no application quotes
    about its own vocabulary."""
    text = (
        "dockerfile parse error on line 3: FROM requires either one or three arguments"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING
    assert v.observations == [f"dockerfile parse error: {text}"]


def test_githubs_own_one_line_shape_still_establishes_unrecognized_named_value():
    """GitHub's real validator line: framing and marker together."""
    text = (
        "The workflow is not valid. .github/workflows/ci.yml (Line: 12, "
        "Col: 9): Unrecognized named-value: 'foo'"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.AUTHORING


def test_a_pytest_assertion_quoting_the_words_is_project_not_authoring():
    """Corrected during review: `E   assert "unrecognized named-value" in
    out` is PROJECT evidence outright (pytest's own bare-assert shape) --
    the anchored AUTHORING rule does not also fire on it, so there is no
    ambiguity left to resolve, only the one honest kind."""
    text = (
        "FAILED tests/test_ci.py::test_named_value - "
        'assert "unrecognized named-value" in out'
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT


def test_a_third_partys_own_message_is_not_authoring():
    """Not GitHub's framing: a tool that happens to share the phrase."""
    text = "my-tool: unrecognized named-value 'x'"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == ["no rule matched"]


# --- PR #72 final review round, finding 2: warning prefixes are -------------
# case-insensitive ------------------------------------------------------------
# `_LINE_WARNING_RE` was case-sensitive, so `WARNING:`/`Warning:` -- shapes
# several CI actions and tools actually write -- defeated it exactly as
# completely as no prefix at all.


def test_upper_case_warning_prefix_does_not_establish_environment():
    """The bug, in the shape the review found it: `WARNING:` (all caps)
    read as fresh failure evidence instead of a noticed, recovered problem."""
    text = f"WARNING: {RECOVERED_TIMEOUT}; retry succeeded"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [f"warning-shaped: connection timed out: {text}"]


def test_title_case_warning_prefix_is_also_warning_shaped():
    text = f"Warning: {RECOVERED_TIMEOUT}; retry succeeded"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == [f"warning-shaped: connection timed out: {text}"]


def test_upper_case_warning_run_does_not_classify_the_exit_code_either():
    """The full two-line shape: a `WARNING:` line the run recovered from,
    followed only by the step's own exit code -- must not read as
    ENVIRONMENT with a class the operator would exit 0 with, undiagnosed."""
    text = (
        f"WARNING: {RECOVERED_TIMEOUT}; retry succeeded\n"
        "Process completed with exit code 1."
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


# --- PR #72 bounded pass: a class needs the shape, not the words ------------
# The owner's evidence rule, 2026-09-22: a class is established only where
# the SHAPE of the evidence ties the message to its cause, and a rule known
# to fire on the wrong cause is a wrong diagnosis, not a limitation. Two
# features carry that here -- a TOOL'S OWN FRAMING for ENVIRONMENT, and
# PROVENANCE for PROJECT -- and each is pinned by a pair: the framed line
# that keeps its class, and the bare words that lose it.


def test_an_app_printing_a_503_body_is_not_an_environment_failure():
    """The twin the old rule got wrong: an HTTP fixture replaying a canned
    upstream response says `503 Service Unavailable` exactly as the registry
    does, and nothing in the sentence says which one printed it."""
    v = _verdict("error parsing HTTP 503 response body: 503 Service Unavailable")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == ["no rule matched"]


def test_buildkits_own_503_is_still_environment():
    """The positive twin: buildkit says IT could not reach the registry."""
    text = (
        "ERROR: failed to solve: failed to do request: "
        "docker.io/library/python:3.12-slim: 503 Service Unavailable"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert v.evidence == [Evidence(None, text)]


def test_a_test_printing_a_timeout_is_not_an_environment_failure():
    """A retry test prints the phrase it exists to exercise."""
    v = _verdict("requests: connection timed out after 5s")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == ["no rule matched"]


def test_apts_own_unreachable_line_is_still_environment():
    """apt names the host AND the port it could not reach -- framing the
    application above does not have. Verbatim from live acceptance run 2."""
    text = (
        "#11 15.43   Could not connect to 10.255.255.1:80 (10.255.255.1), "
        "connection timed out"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_curls_own_timeout_line_is_still_environment():
    v = _verdict("curl: (28) Operation timed out after 5001 milliseconds")
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_gits_own_transport_failure_is_still_environment():
    text = (
        "fatal: unable to access 'https://github.com/o/r/': "
        "Could not resolve host: github.com"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_uvs_own_error_chain_is_still_environment():
    text = (
        "  Caused by: failed to lookup address information: Name or service not known"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_pips_retry_warning_is_a_noticed_problem_not_a_cause():
    """`WARNING: Retrying ...` is pip saying it recovered, and the level rule
    already covers it; the words alone never did name a cause."""
    text = (
        "WARNING: Retrying (Retry(total=4)) after connection broken by "
        "'NewConnectionError': Temporary failure in name resolution"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_pips_own_install_failure_is_environment():
    """The positive twin: pip's `ERROR: ` is the failure, not the retry."""
    text = (
        "ERROR: Could not install packages due to an OSError: "
        "[Errno -3] Temporary failure in name resolution"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_a_bare_disk_full_sentence_is_not_a_full_runner():
    """A test writing to a deliberately tiny tmpfs prints it verbatim."""
    v = _verdict("OSError: [Errno 28] No space left on device")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.observations == ["exception: OSError: [Errno 28] No space left on device"]


def test_the_daemons_own_storage_path_is_still_a_full_runner():
    v = _verdict("write /var/lib/docker/tmp/x: no space left on device")
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_a_bare_rate_limit_word_is_not_a_registry_refusal():
    v = _verdict("assert 'toomanyrequests' in body")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN


def test_the_registrys_own_refusal_is_still_environment():
    text = "toomanyrequests: You have reached your pull rate limit."
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT


def test_jests_failed_to_fetch_no_longer_reads_as_apt():
    """The over-firer recorded in TODO.md: jest's own `TypeError: Failed to
    fetch` carries neither apt's `E: ` nor uv's trailing colon."""
    v = _verdict("E   TypeError: Failed to fetch")
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


# --- PROJECT: the assertion must say whose it was ---------------------------


def test_a_bare_assertion_error_establishes_nothing():
    """The runner's own setup step, `python -c 'assert ...'`, prints exactly
    this. The match is kept as an observation so the operator still reads
    it, and it names no cause."""
    text = "AssertionError: 1 != 2"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []
    assert v.observations == [
        f"assertion without project provenance: assertion error: {text}"
    ]


def test_a_traceback_frame_in_the_checkout_establishes_project():
    """The shape live acceptance run 3 really printed: unittest's frame names
    the image's WORKDIR copy of the project, and the assertion under it."""
    text = (
        '  File "/app/tests/test_greeting.py", line 10, in test_greeting_text\n'
        "AssertionError: 'hello from ci_build' != 'hello from ci-build'"
    )
    v = _verdict(text)
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT
    assert v.evidence == [Evidence(None, text)]


def test_a_pytest_node_id_is_provenance_in_its_own_right():
    v = _verdict("FAILED tests/test_greet.py::test_greet - AssertionError: 1 != 2")
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT


def test_a_frame_in_site_packages_is_not_the_projects_assertion():
    """A vendored dependency's doctest, or a conftest plugin the CI image
    installs, collected by the same run."""
    text = (
        '  File "/usr/lib/python3.12/site-packages/vendorlib/check.py", '
        "line 8, in verify\n"
        "AssertionError: 1 != 2"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_a_node_id_under_site_packages_is_not_provenance_either():
    v = _verdict(
        "FAILED /usr/lib/python3/dist-packages/vendorlib/tests/test_a.py::test_a "
        "- AssertionError: 1 != 2"
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN


def test_a_synthetic_frame_from_python_dash_c_is_not_provenance():
    """`python -c` reports `<string>`: a CI setup script asserting a
    precondition, which is not the project's suite."""
    v = _verdict('  File "<string>", line 1, in <module>\nAssertionError: 1 != 2')
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN


def test_a_frame_in_the_runners_temp_area_is_not_provenance():
    v = _verdict(
        '  File "/home/runner/work/_temp/8f2a/provision.py", line 12, in main\n'
        "AssertionError: 1 != 2"
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN


def test_an_absolute_frame_outside_the_projects_directories_is_not_provenance():
    """`.github/scripts/provision.py` is the CI's own script: absolute, and
    under neither `tests/` nor `src/`."""
    v = _verdict(
        '  File "/home/runner/work/project/project/.github/scripts/provision.py", '
        "line 12, in main\nAssertionError: 1 != 2"
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN


def test_a_relative_frame_is_the_checkouts_by_construction():
    v = _verdict(
        '  File "tests/test_greet.py", line 10, in test_greet\nE   assert 1 == 2'
    )
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.PROJECT


def test_provenance_must_be_in_the_same_piece_of_evidence():
    """Two blocks: one carries the frame, the other the assertion. Nothing
    in the snapshot binds them, so neither establishes the class."""
    job = job_with(
        evidence=[
            Evidence(None, '  File "tests/test_greet.py", line 10, in test_greet'),
            Evidence(None, "AssertionError: 1 != 2"),
        ]
    )
    v = classify_failure(job, step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is FailureKind.UNKNOWN
    assert v.evidence == []


def test_an_unprovenanced_assertion_does_not_make_a_real_cause_ambiguous():
    """The availability side of the gate: a demoted match is not a second
    kind, so an established ENVIRONMENT cause survives beside it."""
    v = _verdict("curl: (6) Could not resolve host: pypi.org\nAssertionError: 1 != 2")
    assert v.outcome == "CLASSIFIED" and v.kind is FailureKind.ENVIRONMENT
    assert any(
        o.startswith("assertion without project provenance: ") for o in v.observations
    )
