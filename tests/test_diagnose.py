"""diagnose.py: the reading layer over a FailedRun snapshot.

The layer asserts no cause (owner's decision, 2026-09-22). What these tests
prove instead: every matched line is reported and its block cited; the
evidence belongs to THIS failure (a step's own pool, a sibling step's blocks
excluded, a job's own completeness); the two completeness states stay
distinct; and no verdict of any shape carries a kind, a cause, or an
ambiguity -- not merely "the outcome is always UNCLASSIFIED".
"""

import json
from dataclasses import fields

import pytest

from deployer.diagnose import (
    OBSERVATIONS,
    FailureVerdict,
    RunDiagnosis,
    diagnose_run,
    read_failure,
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

JOB_ID = 77
COMPLETE = Completeness(logs="present", annotations="present")
LOGS_UNAVAILABLE = Completeness(logs="unavailable", annotations="present")
LOGS_ERROR = Completeness(logs="error", annotations="present")
ANNOTATIONS_ERROR = Completeness(logs="present", annotations="error")
ANNOTATIONS_ABSENT = Completeness(logs="present", annotations="absent")
# What forge records for a job whose log came back and that had no
# annotations: the default a hand-built job carries.
READ_COMPLETELY = ANNOTATIONS_ABSENT

NO_OBSERVATION = "no observation matched"
JOB_LEVEL_NOTE = "cited evidence is job-level (no step binding)"

# apt's `Err:` detail line: it names the host and the port it could not
# reach, which is the framing the `connection timed out` shape requires, so
# a warning-shaped test written on it exercises the label and not a miss.
RECOVERED_TIMEOUT = "Could not connect to pypi.org:443, connection timed out"
PARSE_ERROR = (
    "ERROR: failed to solve: dockerfile parse error on line 3: "
    "FROM requires either one or three arguments"
)
ASSERTION_SUMMARY = "FAILED tests/test_x.py::test_x - AssertionError: 1 != 2"


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


def _verdict(text: str) -> FailureVerdict:
    """The job-level verdict over one unbound, completely read log block."""
    return read_failure(job_with(text=text), step=None, completeness=COMPLETE)


# --- read_failure: observations and citations --------------------------------


def test_no_evidence_is_unclassified_with_nothing_cited():
    v = read_failure(job_with(evidence=[]), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == []
    assert v.observations == [NO_OBSERVATION]


def test_an_assertion_is_an_observation_with_its_block_cited():
    v = _verdict(ASSERTION_SUMMARY)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, ASSERTION_SUMMARY)]
    assert v.observations == [f"pytest failed with assertion: {ASSERTION_SUMMARY}"]


def test_the_words_alone_are_not_the_assertion_shape():
    """A shape is a line shape, not a vocabulary."""
    v = _verdict("AssertionError in the runner's own setup")
    assert v.observations == [NO_OBSERVATION]
    assert v.evidence == []


def test_incomplete_evidence_is_reported_beside_what_was_read():
    v = read_failure(
        job_with(text="AssertionError: 1 != 2"),
        step=None,
        completeness=LOGS_UNAVAILABLE,
    )
    assert v.outcome == "EVIDENCE_UNAVAILABLE" and v.kind is None
    assert v.observations == [
        "assertion error: AssertionError: 1 != 2",
        "logs unavailable",
    ]


def test_two_shapes_in_one_block_are_two_observations_not_a_conflict():
    """There is no ambiguity to report where nothing competes for a cause."""
    daemon = "cannot connect to the docker daemon"
    assertion = "AssertionError: 1 != 2"
    v = _verdict(f'{daemon}\nFile "tests/test_x.py", line 10, in test_x\n{assertion}')
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.observations == [
        f"docker daemon unreachable: {daemon}",
        f"assertion error: {assertion}",
    ]
    assert not any("ambiguous" in o for o in v.observations)


def test_the_evidence_order_does_not_change_what_is_read():
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
    a = read_failure(forward, step=None, completeness=COMPLETE)
    b = read_failure(backward, step=None, completeness=COMPLETE)
    assert a.outcome == b.outcome == "UNCLASSIFIED"
    assert a.kind is b.kind is None
    assert set(a.evidence) == set(b.evidence) == set(forward.evidence)
    assert sorted(a.observations) == sorted(b.observations)
    assert a.observations == list(reversed(b.observations))


def test_module_not_found_is_an_exception_observation():
    """Wrong dependencies and a project defect share this line (spec §5)."""
    text = "E   ModuleNotFoundError: No module named 'flask'"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, text)]
    assert v.observations == [f"exception: {text}"]


def test_no_shape_at_all_says_so():
    v = _verdict("Process completed with exit code 1.")
    assert v.outcome == "UNCLASSIFIED"
    assert v.observations == [NO_OBSERVATION]
    assert v.evidence == []


def test_a_dockerfile_parse_error_is_observed_with_its_block_cited():
    v = _verdict(PARSE_ERROR)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, PARSE_ERROR)]
    assert v.observations == [f"dockerfile parse error: {PARSE_ERROR}"]


def test_apts_name_resolution_line_is_observed():
    text = "E: Temporary failure resolving 'deb.debian.org'"
    v = _verdict(text)
    assert v.observations == [f"name resolution failure: {text}"]


def test_several_matches_cite_each_block_once():
    first = "failed to solve: write /var/lib/docker/tmp/a: no space left on device"
    second = "failed to solve: write /var/lib/docker/tmp/b: no space left on device"
    e1 = Evidence(None, f"{first}\n{second}")
    e2 = Evidence(None, "toomanyrequests: rate limit exceeded")
    v = read_failure(job_with(evidence=[e1, e2]), step=None, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED"
    assert v.evidence == [e1, e2]
    assert v.observations == [
        f"disk full: {first}",
        f"disk full: {second}",
        f"registry rate limit: {e2.text}",
    ]


def test_step_verdict_excludes_evidence_bound_to_another_step():
    """A block bound to a (green) sibling step is not this step's evidence."""
    target = failed_step(3)
    other = Evidence(StepRef(JOB_ID, 1), "cannot connect to the docker daemon")
    job = job_with(evidence=[other], steps=[target])
    v = read_failure(job, step=target, completeness=COMPLETE)
    assert v.where == target.ref
    assert v.outcome == "UNCLASSIFIED" and v.evidence == []
    assert v.observations == [NO_OBSERVATION]


def test_step_verdict_uses_its_own_and_unbound_job_evidence():
    target = failed_step(3, "##[group]Run pytest")
    unbound = Evidence(None, ASSERTION_SUMMARY)
    annotation = Evidence(JOB_ID, "Process completed with exit code 1.", "failure")
    job = job_with(evidence=[unbound, annotation], steps=[target])
    v = read_failure(job, step=target, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED"
    assert v.evidence == [unbound]
    assert v.observations == [
        f"pytest failed with assertion: {ASSERTION_SUMMARY}",
        JOB_LEVEL_NOTE,
    ]


def test_step_verdict_cites_step_bound_evidence_without_the_job_level_note():
    target = failed_step(
        2, "Dockerfile parse error on line 3: unexpected end of statement"
    )
    v = read_failure(job_with(steps=[target]), step=target, completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED"
    assert v.evidence == target.evidence
    assert JOB_LEVEL_NOTE not in v.observations


def test_a_python_file_not_found_is_one_exception_observation():
    """The `no such file` shape skips exception-shaped lines, so a Python
    `FileNotFoundError` is reported once, under `exception`."""
    text = (
        "E   FileNotFoundError: [Errno 2] No such file or directory: "
        "'tests/data/fixture.json'"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.observations == [f"exception: {text}"]


def test_dockers_copy_line_is_reported_under_both_shapes_it_carries():
    """docker's own shape for a source missing from the build context, and
    the bare `no such file` it also carries: two observations, one block."""
    text = (
        "COPY failed: file not found in build context or excluded by "
        ".dockerignore: stat app.py: no such file or directory"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, text)]
    assert v.observations == [
        f"copy/add failed in build context: {text}",
        f"no such file: {text}",
    ]


def test_an_exception_line_with_dockers_own_framing_is_observed_under_both():
    """The exception-line exclusion is scoped to `no such file` only."""
    text = (
        "docker.errors.DockerException: Cannot connect to the Docker "
        "daemon at unix:///var/run/docker.sock"
    )
    v = _verdict(text)
    assert v.observations == [
        f"docker daemon unreachable: {text}",
        f"exception: {text}",
    ]


def test_dotted_exception_names_are_observed():
    text = "requests.exceptions.ConnectionError: boom"
    assert _verdict(text).observations == [f"exception: {text}"]


def test_pytest_bare_assert_summary_is_observed():
    text = "FAILED tests/test_x.py::test_x - assert 1 == 2"
    assert _verdict(text).observations == [f"pytest bare assert: {text}"]


def test_exact_shapes_do_not_match_across_a_newline():
    """`E` on one line and `assert x` on the next is not a pytest assert line."""
    v = _verdict("E\nassert x")
    assert v.observations == [NO_OBSERVATION]


def test_the_cited_line_is_the_matched_line_not_a_bare_prefix():
    v = _verdict('File "tests/test_x.py", line 3, in test_x\nE\nAssertionError: split')
    assert v.observations == ["assertion error: AssertionError: split"]


# --- buildkit line framing ---------------------------------------------------
# `docker build` frames every RUN-step output line as `#<step> <seconds> `;
# lines below are copied verbatim from real job logs (evidence/job-*.raw.log).


def test_buildkit_framed_assertion_error_is_observed_with_its_frame():
    frame = '#16 0.310   File "/app/tests/test_greeting.py", line 10, in test_greeting'
    line = "#16 0.310 AssertionError: 'hello from ci_build' != 'hello from ci-build'"
    v = _verdict(f"{frame}\n{line}")
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, f"{frame}\n{line}")]
    assert v.observations == [f"assertion error: {line}"]


def test_buildkit_framed_file_not_found_is_still_one_exception_observation():
    """The negative lookahead must see through the buildkit frame too."""
    text = "#12 0.512 E   FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    v = _verdict(text)
    assert v.observations == [f"exception: {text}"]


def test_leading_whitespace_before_pytest_assert_is_still_observed():
    line = "   E   AssertionError: 1 != 2"
    v = _verdict(f'File "tests/test_x.py", line 3, in test_x\n{line}')
    assert v.observations == [f"assertion error: {line}"]


def test_buildkit_framed_connection_timeout_is_observed():
    """apt's `Err:` detail names the host and the port it could not reach."""
    text = (
        "#11 15.43   Could not connect to 10.255.255.1:80 (10.255.255.1), "
        "connection timed out"
    )
    assert _verdict(text).observations == [f"connection timed out: {text}"]


def test_real_buildkit_copy_not_found_is_observed():
    """Verbatim from evidence/run-1.log-failed.txt -- the line live
    acceptance run 1 really printed (`tests/test_fixture_runs.py`)."""
    text = (
        "ERROR: failed to build: failed to solve: failed to compute cache key: "
        'failed to calculate checksum of ref a4efb8b6::t0jk: "/docs/setup.md": '
        "not found"
    )
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.observations == [f"copy/add source not found: {text}"]


def test_buildkit_framing_does_not_reopen_the_split_line_gap():
    """`E` and `assert x` on separate buildkit-framed lines still do not match."""
    assert _verdict("#12 0.5 E\n#12 0.5 assert x").observations == [NO_OBSERVATION]


def test_step_verdict_cites_a_job_annotation_with_its_level_rendered():
    """int-source (annotation) evidence is in the step pool and citable; its
    level is data on the evidence and is rendered back for the operator."""
    target = failed_step(3)
    line = "E: Temporary failure resolving 'deb.debian.org'"
    annotation = Evidence(JOB_ID, line, "failure")
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.evidence == [annotation]
    assert v.observations == [
        f"name resolution failure: failure: {line}",
        JOB_LEVEL_NOTE,
    ]


def test_job_level_verdict_does_not_carry_the_job_level_note():
    """The note is only informative for a step verdict."""
    v = _verdict("cannot connect to the docker daemon")
    assert JOB_LEVEL_NOTE not in v.observations


def test_job_level_verdict_is_addressed_by_job_id():
    assert _verdict("x").where == JOB_ID


def test_annotations_error_is_incomplete_even_with_logs_present():
    text = "cannot connect to the docker daemon"
    v = read_failure(job_with(text=text), step=None, completeness=ANNOTATIONS_ERROR)
    assert v.outcome == "EVIDENCE_UNAVAILABLE" and v.kind is None
    assert v.evidence == [Evidence(None, text)]
    assert v.observations == [
        f"docker daemon unreachable: {text}",
        "annotations fetch error",
    ]


def test_absent_annotations_are_optional_not_incompleteness():
    v = read_failure(
        job_with(text="cannot connect to the docker daemon"),
        step=None,
        completeness=ANNOTATIONS_ABSENT,
    )
    assert v.outcome == "UNCLASSIFIED"


def test_logs_error_names_what_is_missing():
    v = read_failure(job_with(evidence=[]), step=None, completeness=LOGS_ERROR)
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert v.observations == ["logs fetch error"]
    assert v.evidence == []


def test_observations_are_data_with_distinct_names_and_no_kind():
    """Structural: a shape carries a name and a pattern, and nothing that
    could name a cause."""
    assert OBSERVATIONS
    assert len({shape.name for shape in OBSERVATIONS}) == len(OBSERVATIONS)
    assert {field.name for field in fields(OBSERVATIONS[0])} == {"name", "pattern"}
    assert not any(": " in shape.name for shape in OBSERVATIONS), (
        "a name with ': ' in it would be unreadable in '<name>: <line>'"
    )


def test_verdict_shape():
    v = read_failure(job_with(evidence=[]), step=None, completeness=COMPLETE)
    assert isinstance(v, FailureVerdict)
    assert (v.where, v.outcome, v.kind) == (JOB_ID, "UNCLASSIFIED", None)


# --- diagnose_run -------------------------------------------------------------


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


def job_with_assertion(job_id: int = JOB_ID) -> FailedJob:
    return job_with(text=ASSERTION_SUMMARY, job_id=job_id)


def job_with_fetch_failure(job_id: int = JOB_ID + 1) -> FailedJob:
    return job_with(
        text="E: Failed to fetch http://deb.debian.org/debian/x.deb", job_id=job_id
    )


def job_with_exit_code_only(job_id: int = JOB_ID + 2) -> FailedJob:
    return job_with(text="Process completed with exit code 1.", job_id=job_id)


def job_failed_no_steps() -> FailedJob:
    return job_with(text="cannot connect to the docker daemon")


def job_failed_with_steps() -> FailedJob:
    return job_with(
        text="cannot connect to the docker daemon",
        steps=[failed_step(2), failed_step(4)],
    )


def test_two_readable_jobs_read_unclassified_with_no_causes():
    d = diagnose_run(run_with(job_with_assertion(), job_with_fetch_failure()))
    assert d.outcome == "UNCLASSIFIED"
    assert d.causes == []
    assert [v.outcome for v in d.failures] == ["UNCLASSIFIED", "UNCLASSIFIED"]
    assert [v.observations for v in d.failures] == [
        [f"pytest failed with assertion: {ASSERTION_SUMMARY}"],
        ["fetch failure: E: Failed to fetch http://deb.debian.org/debian/x.deb"],
    ]


def test_job_order_does_not_change_the_result():
    a = diagnose_run(run_with(job_with_assertion(), job_with_exit_code_only()))
    b = diagnose_run(run_with(job_with_exit_code_only(), job_with_assertion()))
    assert a.outcome == b.outcome == "UNCLASSIFIED"
    assert a.causes == b.causes == []
    assert {v.where: v.observations for v in a.failures} == {
        v.where: v.observations for v in b.failures
    }


def test_precedence_evidence_unavailable_beats_unclassified():
    d = diagnose_run(run_with(job_with_exit_code_only(), completeness=LOGS_UNAVAILABLE))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"


def test_empty_diagnosable_set_is_unclassified_with_the_note():
    """A failed run with no diagnosable failed job/step is read, and says so."""
    d = diagnose_run(run_with())
    assert d.outcome == "UNCLASSIFIED"
    assert d.failures == []
    assert d.observations == ["failed run exposes no failed job or step"]
    assert d.causes == []


def test_empty_set_with_lost_data_is_evidence_unavailable():
    d = diagnose_run(run_with(completeness=LOGS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.failures == [] and d.causes == []
    assert d.observations == ["logs fetch error"]


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
    d = diagnose_run(run_with(job_with_assertion(), completeness=LOGS_UNAVAILABLE))
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


def test_an_incomplete_run_keeps_each_failures_own_read():
    """Precedence changes the summary, not what each failure found: the job
    was read completely, so its own verdict says so even though the run's
    worst-of did not."""
    d = diagnose_run(run_with(job_with_assertion(), completeness=ANNOTATIONS_ERROR))
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert [v.outcome for v in d.failures] == ["UNCLASSIFIED"]
    assert d.causes == []
    assert d.failures[0].observations == [
        f"pytest failed with assertion: {ASSERTION_SUMMARY}"
    ]


def test_a_job_read_incompletely_is_evidence_unavailable_on_its_own():
    """Per-failure incompleteness is judged on the job's own completeness."""
    unreadable = job_with(
        text=ASSERTION_SUMMARY, completeness=ANNOTATIONS_ERROR, job_id=5
    )
    d = diagnose_run(run_with(unreadable))
    assert [v.outcome for v in d.failures] == ["EVIDENCE_UNAVAILABLE"]
    assert d.causes == []
    assert d.observations == ["job 5: annotations fetch error"]


def test_run_diagnosis_shape():
    run = run_with(job_with_assertion())
    d = diagnose_run(run)
    assert isinstance(d, RunDiagnosis)
    assert d.run is run
    assert (d.outcome, d.causes, d.observations) == ("UNCLASSIFIED", [], [])


# --- the absence of causal claims -------------------------------------------
# A broad set of inputs: one sample per observation shape, the warning shapes,
# exceptions, empty evidence, and both incomplete states.

SAMPLES: dict[str, str] = {
    "dockerfile parse error": PARSE_ERROR,
    "unknown instruction": "dockerfile parse error on line 7: unknown instruction: X",
    "unrecognized named-value": (
        "The workflow is not valid. .github/workflows/ci.yml (Line: 12, "
        "Col: 9): Unrecognized named-value: 'foo'"
    ),
    "unresolvable action": "Unable to resolve action actions/checkout@v99",
    "copy/add source not found": (
        'ERROR: failed to compute cache key: "/docs/setup.md": not found'
    ),
    "copy/add failed in build context": (
        "COPY failed: file not found in build context or excluded by .dockerignore"
    ),
    "entrypoint executable not found": (
        'exec: "serve": executable file not found in $PATH: unknown.'
    ),
    "executable not found": (
        "E   RuntimeError: executable file not found in the sandbox"
    ),
    "exec format error": "exec user process caused: exec format error",
    "docker daemon unreachable": "cannot connect to the docker daemon",
    "host unresolvable": "curl: (6) Could not resolve host: pypi.org",
    "name resolution failure": "E: Temporary failure resolving 'deb.debian.org'",
    "fetch failure": "E: Failed to fetch http://deb.debian.org/debian/x.deb",
    "connection timed out": RECOVERED_TIMEOUT,
    "registry rate limit": "toomanyrequests: You have reached your pull rate limit.",
    "service unavailable": "failed to solve: python:3.12-slim: 503 Service Unavailable",
    "disk full": "write /var/lib/docker/tmp/x: no space left on device",
    "runner shutdown": "The runner has received a shutdown signal.",
    "assertion error": "AssertionError: 1 != 2",
    "pytest failed with assertion": ASSERTION_SUMMARY,
    "pytest bare assert": "FAILED tests/test_x.py::test_x - assert 1 == 2",
    "pytest assert": "E       assert 1 == 2",
    "exception": "ValueError: bad configuration",
    "no such file": "cp: cannot stat '/app/main.py': No such file or directory",
}

_CAUSE_WORDS = ("authoring", "environment", "project", "unknown", "ambiguous")


def _broad_inputs() -> list[FailedJob]:
    """Every sample, alone and beside another; warning shapes; nothing."""
    texts = list(SAMPLES.values())
    jobs = [job_with(text=text) for text in texts]
    jobs += [job_with(text=f"{a}\n{b}") for a, b in zip(texts, texts[1:] + texts[:1])]
    jobs += [job_with(text=f"W: {text}") for text in texts]
    jobs += [job_with(evidence=[Evidence(JOB_ID, text, "warning")]) for text in texts]
    jobs += [job_with(evidence=[])]
    return jobs


def test_every_observation_shape_fires_on_its_sample():
    """The mutation guard for the table: drop a shape and its sample goes
    unobserved here."""
    assert set(SAMPLES) == {shape.name for shape in OBSERVATIONS}
    for name, text in SAMPLES.items():
        assert f"{name}: {text}" in _verdict(text).observations, name


def test_no_verdict_ever_carries_a_kind():
    """Over the broad set and every completeness: no kind, only the two
    outcomes, and no observation that names a cause or an ambiguity."""
    for job in _broad_inputs():
        for completeness in (COMPLETE, LOGS_UNAVAILABLE, ANNOTATIONS_ERROR):
            v = read_failure(job, step=None, completeness=completeness)
            assert v.kind is None
            assert v.outcome in ("UNCLASSIFIED", "EVIDENCE_UNAVAILABLE")
            for observation in v.observations:
                head = observation.split(": ", 1)[0].lower()
                assert head not in _CAUSE_WORDS, observation
                assert not observation.startswith("ambiguous"), observation


def test_causes_are_always_empty():
    jobs = _broad_inputs()
    runs = [run_with(job) for job in jobs]
    runs += [run_with(*jobs[:5]), run_with(*jobs[5:10], completeness=LOGS_ERROR)]
    runs += [run_with(), run_with(completeness=LOGS_ERROR)]
    for run in runs:
        d = diagnose_run(run)
        assert d.causes == []
        assert d.outcome in ("UNCLASSIFIED", "EVIDENCE_UNAVAILABLE")
        assert all(v.kind is None for v in d.failures)


# --- render_verdict ------------------------------------------------------------


def test_render_verdict_carries_its_own_schema_version_first():
    # job_with_assertion() has no itemised steps (job-level `where`); the
    # steps of job_failed_with_steps() give step-level `where` — both at once.
    d = diagnose_run(run_with(job_with_assertion(), job_failed_with_steps()))
    document = json.loads(render_verdict(d))

    assert next(iter(document)) == "verdict_schema_version"
    assert document["verdict_schema_version"] == "1.1"
    assert document["outcome"] == "UNCLASSIFIED"
    assert document["causes"] == []
    assert isinstance(document["failures"], list) and document["failures"]
    assert all(failure["kind"] is None for failure in document["failures"])
    wheres = [failure["where"] for failure in document["failures"]]
    assert any(isinstance(where, int) for where in wheres)
    assert any(isinstance(where, dict) for where in wheres)
    for where in wheres:
        if isinstance(where, dict):
            assert set(where) == {"job_id", "number"}
    # The nested run keeps its own, distinct schema version.
    assert document["run"]["snapshot_schema_version"] == "1.3"


# --- per-job completeness: a fail-fast matrix ------------------------------


def _fail_fast_matrix() -> tuple[FailedJob, FailedJob]:
    """Job 1 fails readably; job 2 is cancelled and its log endpoint errors."""
    readable = job_with(text=PARSE_ERROR, job_id=1)
    unreadable = job_with(job_id=2, completeness=LOGS_ERROR)
    return readable, unreadable


def test_sibling_job_log_error_does_not_erase_a_readable_jobs_observations():
    """Spec §4: what one job's read found is never lost to a sibling.

    `Completeness` used to be one worst-of value for the whole run, handed to
    every verdict, so job 1's complete evidence was dropped with job 2's
    error. Each verdict depends only on how ITS job was read; the run outcome
    still reports that something could not be looked at.
    """
    readable, unreadable = _fail_fast_matrix()
    d = diagnose_run(run_with(readable, unreadable, completeness=LOGS_ERROR))

    assert [v.outcome for v in d.failures] == ["UNCLASSIFIED", "EVIDENCE_UNAVAILABLE"]
    assert d.failures[0].evidence == [Evidence(None, PARSE_ERROR)]
    assert d.failures[0].observations == [f"dockerfile parse error: {PARSE_ERROR}"]
    assert d.outcome == "EVIDENCE_UNAVAILABLE"
    assert d.causes == []


def test_the_unreadable_sibling_is_named_in_the_run_observations():
    """The operator has to know WHICH job was not read, not only that one was."""
    d = diagnose_run(run_with(*_fail_fast_matrix(), completeness=LOGS_ERROR))
    assert d.observations == ["job 2: logs fetch error"]


def test_a_readable_sibling_pair_is_unaffected():
    readable, _ = _fail_fast_matrix()
    d = diagnose_run(run_with(readable, job_with_assertion(job_id=2)))
    assert d.outcome == "UNCLASSIFIED"
    assert [v.outcome for v in d.failures] == ["UNCLASSIFIED", "UNCLASSIFIED"]


def test_the_unreadable_sibling_may_come_first():
    """Whose log failed must not depend on the order jobs were listed in."""
    readable, unreadable = _fail_fast_matrix()
    forward = diagnose_run(run_with(readable, unreadable, completeness=LOGS_ERROR))
    backward = diagnose_run(run_with(unreadable, readable, completeness=LOGS_ERROR))
    assert forward.outcome == backward.outcome == "EVIDENCE_UNAVAILABLE"
    assert forward.observations == backward.observations
    assert {v.where: v.outcome for v in forward.failures} == {
        v.where: v.outcome for v in backward.failures
    }


# --- warning-shaped lines: a label, never an outcome -------------------------
# A log line says it for itself: apt prefixes a recovered problem with `W: `
# and a real one with `E: `; compilers, pip and shell tooling write
# `warning: `/`notice: ` at the head of the line, in whatever case. A GitHub
# annotation carries a level of its own as data (`Evidence.level`), which
# governs its whole message. Either way the line is still reported, with the
# label, so the operator can tell a noticed problem from a failing one.


def test_a_recovered_warning_and_a_failing_line_are_labelled_apart():
    warned = (
        "#8 12.1 W: Could not connect to deb.debian.org:80 (1.2.3.4), "
        "connection timed out [retrying]"
    )
    v = _verdict(f"{warned}\n#8 30.2 apt-get update succeeded on retry\n{PARSE_ERROR}")
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.observations == [
        f"dockerfile parse error: {PARSE_ERROR}",
        f"warning-shaped: connection timed out: {warned}",
    ]


def test_apts_e_line_is_observed_without_the_label():
    """Verbatim from live acceptance run 2: apt's `E: ` prefix is a failure."""
    text = (
        "#11 15.89 E: Failed to fetch http://10.255.255.1/debian/x.deb  "
        "Connection timed out [IP: 10.255.255.1 80]"
    )
    v = _verdict(text)
    assert v.observations == [
        f"fetch failure: {text}",
        f"connection timed out: {text}",
    ]


def test_a_warning_line_and_a_failing_line_of_one_shape_are_both_reported():
    """The label is per matched line, not per shape: every match is cited,
    and the recovered line is not hidden behind the failing one."""
    warned = "W: Could not connect to deb.debian.org:80 (1.2.3.4), connection timed out"
    failed = "E: Failed to fetch http://deb.debian.org/x.deb  Connection timed out"
    v = _verdict(f"{warned}\n{failed}\n")
    assert v.observations == [
        f"fetch failure: {failed}",
        f"warning-shaped: connection timed out: {warned}",
        f"connection timed out: {failed}",
    ]


def test_a_warning_shaped_line_is_labelled_whatever_it_mentions():
    text = "W: curl: (6) Could not resolve host: proxy.internal [retrying]"
    v = _verdict(text)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, text)]
    assert v.observations == [f"warning-shaped: host unresolvable: {text}"]


def test_a_warning_annotation_naming_a_missing_file_is_labelled_by_its_level():
    """The live shape: a step that failed for its own reason, plus a
    `warning` annotation about an optional file the build carried on
    without. The level is data, rendered back onto the line."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        "no such file optional-cache.json; continuing without cache",
        "warning",
    )
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [annotation]
    assert v.observations == [
        f"warning-shaped: no such file: warning: {annotation.text}",
        JOB_LEVEL_NOTE,
    ]


def test_a_failure_annotation_is_observed_without_the_label():
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        "The workflow is not valid. .github/workflows/ci.yml (Line: 31, "
        "Col: 9): Unrecognized named-value: 'secret'",
        "failure",
    )
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.evidence == [annotation]
    assert v.observations[0] == f"unrecognized named-value: failure: {annotation.text}"


def _step_with_an_annotation(level: str) -> FailedJob:
    """One failed step whose log says only that the step exited non-zero, plus
    a job annotation at `level` naming a recovered network problem."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, f"{RECOVERED_TIMEOUT}; retry succeeded", level)
    return job_with(evidence=[annotation], steps=[target])


@pytest.mark.parametrize("level", ["warning", "notice"])
def test_a_noticed_level_annotation_is_labelled(level: str):
    """`warning` and `notice` are GitHub saying it noticed something."""
    job = _step_with_an_annotation(level)
    v = read_failure(job, step=job.steps[0], completeness=COMPLETE)
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.observations == [
        f"warning-shaped: connection timed out: {level}: {RECOVERED_TIMEOUT}; "
        "retry succeeded",
        JOB_LEVEL_NOTE,
    ]


@pytest.mark.parametrize("level", ["failure", "error", "fatal"])
def test_any_other_level_is_not_warning_shaped(level: str):
    """`failure`, `error`, and any level this catalogue has not seen: an
    unknown level never silently softens a line."""
    job = _step_with_an_annotation(level)
    v = read_failure(job, step=job.steps[0], completeness=COMPLETE)
    assert v.observations[0] == (
        f"connection timed out: {level}: {RECOVERED_TIMEOUT}; retry succeeded"
    )


def test_the_run_of_a_warning_annotation_is_unclassified_with_no_cause():
    """The exit-code-3 door: `deployer diagnose` exits 3, not 0, on a run
    whose only network line was a recovered warning -- as on any other."""
    d = diagnose_run(run_with(_step_with_an_annotation("warning")))
    assert d.outcome == "UNCLASSIFIED"
    assert d.causes == []


def test_a_warning_annotation_and_a_plain_log_line_are_labelled_apart():
    """Per piece of evidence: the step's own log line carries no level and
    is reported plainly beside the labelled annotation."""
    curl = "curl: (6) Could not resolve host: pypi.org"
    target = failed_step(1, curl)
    annotation = Evidence(JOB_ID, f"{RECOVERED_TIMEOUT}; retry succeeded", "warning")
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.evidence == [target.evidence[0], annotation]
    assert v.observations == [
        f"host unresolvable: {curl}",
        f"warning-shaped: connection timed out: warning: {RECOVERED_TIMEOUT}; "
        "retry succeeded",
        JOB_LEVEL_NOTE,
    ]


def test_a_warning_line_mid_block_is_labelled_on_that_line():
    """A log block has no level, so it is judged per matched line, never on
    where the block happens to start."""
    warned = f"W: {RECOVERED_TIMEOUT}; retrying"
    v = _verdict(f"Starting checks\n{warned}\nProcess completed with exit code 1.")
    assert v.observations == [f"warning-shaped: connection timed out: {warned}"]


def test_a_warning_shaped_first_line_does_not_label_a_later_line():
    failed = (
        "E: Failed to fetch http://deb.debian.org/debian/x.deb  "
        "Connection timed out [IP: 1.2.3.4 80]"
    )
    v = _verdict(f"W: something noticed; continuing\n{failed}")
    assert v.observations == [
        f"fetch failure: {failed}",
        f"connection timed out: {failed}",
    ]


def test_a_multiline_warning_annotation_is_warning_shaped_throughout():
    """An annotation's level is a property of the whole message, not of
    whichever line a shape happens to match."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        f"Recovered network issue\n{RECOVERED_TIMEOUT}; retry succeeded",
        "warning",
    )
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.observations[0] == (
        f"warning-shaped: connection timed out: warning: {RECOVERED_TIMEOUT}; "
        "retry succeeded"
    )


def test_a_multiline_failure_annotation_is_not_warning_shaped():
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(
        JOB_ID,
        f"Recovered network issue\n{RECOVERED_TIMEOUT}; retry succeeded",
        "failure",
    )
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.observations[0] == (
        f"connection timed out: failure: {RECOVERED_TIMEOUT}; retry succeeded"
    )


def test_an_annotation_opening_with_an_empty_line_is_still_warning_shaped():
    """A level carried as data cannot be hidden by any text at all."""
    target = failed_step(1, "Process completed with exit code 1.")
    annotation = Evidence(JOB_ID, f"\n{RECOVERED_TIMEOUT}; retry succeeded", "warning")
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.observations[0] == (
        f"warning-shaped: connection timed out: warning: {RECOVERED_TIMEOUT}; "
        "retry succeeded"
    )


def test_a_python_exception_in_an_annotation_reads_as_in_a_log():
    """The level is rendered back only for the operator; the shape reads the
    raw message, so the identical sentence is one `exception` observation as
    an annotation and as a log line."""
    target = failed_step(1, "Process completed with exit code 1.")
    message = "FileNotFoundError: [Errno 2] No such file or directory: 'x'"
    annotation = Evidence(JOB_ID, message, "failure")
    v = read_failure(
        job_with(evidence=[annotation], steps=[target]),
        step=target,
        completeness=COMPLETE,
    )
    assert v.observations == [f"exception: failure: {message}", JOB_LEVEL_NOTE]
    assert _verdict(message).observations == [f"exception: {message}"]


@pytest.mark.parametrize("prefix", ["warning: ", "notice: ", "WARNING: ", "Warning: "])
def test_a_log_lines_own_warning_prefix_is_warning_shaped_in_any_case(prefix: str):
    """Compilers, pip and shell tooling emit `warning: ` lines of their own,
    in whatever case the tool writes them."""
    text = f"{prefix}curl: (6) Could not resolve host: proxy.internal"
    v = _verdict(f"Starting\n{text}\nProcess completed with exit code 1.")
    assert v.observations == [f"warning-shaped: host unresolvable: {text}"]


def test_a_warning_log_line_is_warning_shaped_under_buildkit_framing():
    """The warning shape is read after `_LINE_PREFIX`, like every other line
    shape -- not at the raw start of the line."""
    text = "#8 0.42 warning: curl: (6) Could not resolve host: proxy.internal"
    assert _verdict(text).observations == [f"warning-shaped: host unresolvable: {text}"]


# --- the shapes: what each one reports, and what it does not -----------------


def test_a_bare_no_such_file_line_is_observed():
    text = "#8 0.42 cp: cannot stat '/app/main.py': No such file or directory"
    assert _verdict(text).observations == [f"no such file: {text}"]


def test_exec_format_error_is_observed():
    text = "standard_init_linux.go:228: exec user process caused: exec format error"
    assert _verdict(text).observations == [f"exec format error: {text}"]


def test_executable_not_found_outside_the_exec_shape_is_observed_as_bare():
    text = "E   RuntimeError: executable file not found in the sandbox"
    assert _verdict(text).observations == [
        f"executable not found: {text}",
        f"exception: {text}",
    ]


def test_a_plain_shell_stat_line_is_the_bare_shape_only():
    """The same words without docker's own framing are not a COPY failure."""
    text = "stat app.py: no such file or directory"
    assert _verdict(text).observations == [f"no such file: {text}"]


def test_cat_cannot_open_a_config_is_the_bare_shape_only():
    text = "cat: config.yml: No such file or directory"
    assert _verdict(text).observations == [f"no such file: {text}"]


def test_a_buildkit_run_failure_without_a_missing_path_is_no_shape():
    """`failed to solve` heads every buildkit failure, including a RUN step
    whose command exited non-zero; only the quoted path the build could not
    find makes it the COPY/ADD shape."""
    text = (
        'ERROR: failed to solve: process "/bin/sh -c uv sync --frozen" '
        "did not complete successfully: exit code: 1"
    )
    assert _verdict(text).observations == [NO_OBSERVATION]


def test_a_shell_command_not_found_is_no_shape():
    assert _verdict("bash: foo: command not found").observations == [NO_OBSERVATION]


def test_the_docker_exec_shape_is_reported_once_under_its_own_name():
    """The bare `executable not found` shape excludes docker's `exec:` line,
    so the container runtime's own shape is not reported twice."""
    text = (
        "docker: Error response from daemon: failed to create task for container: "
        "OCI runtime create failed: unable to start container process: "
        'exec: "serve": executable file not found in $PATH: unknown.'
    )
    assert _verdict(text).observations == [f"entrypoint executable not found: {text}"]


def test_an_unresolvable_action_is_observed():
    text = "Unable to resolve action actions/checkout@v99, unable to find version v99"
    assert _verdict(text).observations == [f"unresolvable action: {text}"]


def test_bare_network_phrases_without_a_tools_framing_are_not_observed():
    """The network shapes require the tool's own framing (apt's `E: `, curl's
    `curl: (N)`, git's `fatal: unable to access`, uv's error chain,
    buildkit's `failed to solve:`, the daemon's reply): an application under
    test prints the bare phrases for itself."""
    for text in (
        "connection timed out",
        "503 Service Unavailable",
        "no space left on device",
        "Temporary failure in name resolution",
        "Could not resolve host: pypi.org",
        "toomanyrequests",
    ):
        assert _verdict(text).observations == [NO_OBSERVATION], text


def test_two_missing_files_in_one_block_are_two_observations():
    """One block, one shape, two different lines: two observations, one
    citation. Reporting only the first would leave the operator to guess
    there was a second file."""
    first = "cp: cannot stat '/app/main.py': No such file or directory"
    second = "cp: cannot stat '/app/util.py': No such file or directory"
    block = f"Copying sources\n{first}\n{second}\nDone"
    v = _verdict(block)
    assert v.evidence == [Evidence(None, block)]
    assert v.observations == [f"no such file: {first}", f"no such file: {second}"]


def test_two_fetch_failures_in_one_block_are_two_observations_one_citation():
    """apt prints one `E: Failed to fetch` per index it could not reach."""
    first = "E: Failed to fetch http://deb.debian.org/debian/a.deb  404  Not Found"
    second = "E: Failed to fetch http://deb.debian.org/debian/b.deb  404  Not Found"
    block = f"#9 [stage-0 3/9] RUN apt-get install -y a b\n{first}\n{second}"
    v = _verdict(block)
    assert v.evidence == [Evidence(None, block)]
    assert v.observations == [f"fetch failure: {first}", f"fetch failure: {second}"]


def test_an_observation_quotes_the_line_the_shape_matched_not_the_first():
    marker = "dockerfile parse error on line 3: unexpected end of statement"
    v = _verdict(f"#1 [internal] load build definition\n#1 transferring\n{marker}")
    assert v.observations == [f"dockerfile parse error: {marker}"]


def test_one_line_matched_twice_by_a_shape_is_reported_once():
    """`finditer` can land twice inside one line; an operator reads lines."""
    line = (
        "Could not connect to pypi.org:443, connection timed out; "
        "retried: Could not connect to pypi.org:443, connection timed out"
    )
    assert _verdict(line).observations == [f"connection timed out: {line}"]


def test_evidence_unavailable_keeps_every_observation_and_its_citation():
    """Spec §3: "markers already found are preserved as observations". An
    unreadable run must not be made to look emptier than the part of it that
    WAS read -- so the citations survive too."""
    missing = "cp: cannot stat '/app/main.py': No such file or directory"
    exception = "ValueError: bad configuration"
    block = f"{missing}\n{exception}"
    v = read_failure(job_with(text=block), step=None, completeness=LOGS_UNAVAILABLE)
    assert v.outcome == "EVIDENCE_UNAVAILABLE" and v.kind is None
    assert v.evidence == [Evidence(None, block)]
    assert v.observations == [
        f"exception: {exception}",
        f"no such file: {missing}",
        "logs unavailable",
    ]


def test_evidence_unavailable_keeps_a_warning_shaped_match_too():
    warned = f"W: {RECOVERED_TIMEOUT} [retrying]"
    v = read_failure(job_with(text=warned), step=None, completeness=LOGS_UNAVAILABLE)
    assert v.outcome == "EVIDENCE_UNAVAILABLE"
    assert v.observations == [
        f"warning-shaped: connection timed out: {warned}",
        "logs unavailable",
    ]


# --- the parser's framing and the words apart ---------------------------------


def test_an_applications_own_unknown_instruction_is_reported_as_the_words():
    """An app's `ValueError` about ITS OWN unknown instruction has never seen
    a Dockerfile; the words are reported as the words, beside the exception."""
    text = "ValueError: unknown instruction: frobnicate"
    assert _verdict(text).observations == [
        f"unknown instruction: {text}",
        f"exception: {text}",
    ]


def test_a_parse_error_line_carrying_unknown_instruction_is_reported_once():
    """`dockerfile parse error` yields to `unknown instruction` on a line that
    carries both, so the operator reads the line once, under the words that
    name what the parser objected to."""
    text = "dockerfile parse error on line 7: unknown instruction: FROBNICATE"
    assert _verdict(text).observations == [f"unknown instruction: {text}"]


def test_the_legacy_daemons_one_line_shape_reads_the_same():
    text = (
        "Error response from daemon: dockerfile parse error line 7: "
        "unknown instruction: FROBNICATE"
    )
    assert _verdict(text).observations == [f"unknown instruction: {text}"]


def test_a_plain_dockerfile_syntax_error_is_the_parsers_shape():
    text = (
        "dockerfile parse error on line 3: FROM requires either one or three arguments"
    )
    assert _verdict(text).observations == [f"dockerfile parse error: {text}"]


def test_githubs_own_validator_line_is_observed():
    text = (
        "The workflow is not valid. .github/workflows/ci.yml (Line: 12, "
        "Col: 9): Unrecognized named-value: 'foo'"
    )
    assert _verdict(text).observations == [f"unrecognized named-value: {text}"]


def test_a_pytest_assertion_quoting_the_words_is_the_assertion_shape_only():
    """The validator shape demands GitHub's own framing on the line."""
    text = (
        "FAILED tests/test_ci.py::test_named_value - "
        'assert "unrecognized named-value" in out'
    )
    assert _verdict(text).observations == [f"pytest bare assert: {text}"]


def test_a_third_partys_own_message_is_no_shape():
    assert _verdict("my-tool: unrecognized named-value 'x'").observations == [
        NO_OBSERVATION
    ]


# --- a tool's own framing, and the same words without it ---------------------
# Each network shape is pinned by a pair: the framed line the tool printed,
# which is observed, and the bare words an application prints for itself,
# which are not. Neither says anything about a cause.


def test_an_app_printing_a_503_body_is_no_shape():
    text = "error parsing HTTP 503 response body: 503 Service Unavailable"
    assert _verdict(text).observations == [NO_OBSERVATION]


def test_buildkits_own_503_is_observed():
    text = (
        "ERROR: failed to solve: failed to do request: "
        "docker.io/library/python:3.12-slim: 503 Service Unavailable"
    )
    v = _verdict(text)
    assert v.observations == [f"service unavailable: {text}"]
    assert v.evidence == [Evidence(None, text)]


def test_a_test_printing_a_timeout_is_no_shape():
    text = "requests: connection timed out after 5s"
    assert _verdict(text).observations == [NO_OBSERVATION]


def test_curls_own_timeout_line_is_observed():
    text = "curl: (28) Operation timed out after 5001 milliseconds"
    assert _verdict(text).observations == [f"connection timed out: {text}"]


def test_gits_own_transport_failure_is_observed():
    text = (
        "fatal: unable to access 'https://github.com/o/r/': "
        "Could not resolve host: github.com"
    )
    assert _verdict(text).observations == [f"host unresolvable: {text}"]


def test_uvs_own_error_chain_is_observed():
    text = (
        "  Caused by: failed to lookup address information: Name or service not known"
    )
    assert _verdict(text).observations == [f"name resolution failure: {text}"]


def test_pips_retry_warning_is_no_shape():
    """`WARNING: Retrying ...` carries neither pip's `ERROR: ` nor apt's `E: `."""
    text = (
        "WARNING: Retrying (Retry(total=4)) after connection broken by "
        "'NewConnectionError': Temporary failure in name resolution"
    )
    assert _verdict(text).observations == [NO_OBSERVATION]


def test_pips_own_install_failure_is_observed():
    text = (
        "ERROR: Could not install packages due to an OSError: "
        "[Errno -3] Temporary failure in name resolution"
    )
    assert _verdict(text).observations == [f"name resolution failure: {text}"]


def test_a_bare_disk_full_sentence_is_the_exception_only():
    text = "OSError: [Errno 28] No space left on device"
    assert _verdict(text).observations == [f"exception: {text}"]


def test_the_daemons_own_storage_path_is_observed():
    text = "write /var/lib/docker/tmp/x: no space left on device"
    assert _verdict(text).observations == [f"disk full: {text}"]


def test_a_bare_rate_limit_word_is_no_shape():
    assert _verdict("assert 'toomanyrequests' in body").observations == [NO_OBSERVATION]


def test_the_registrys_own_refusal_is_observed():
    text = "toomanyrequests: You have reached your pull rate limit."
    assert _verdict(text).observations == [f"registry rate limit: {text}"]


def test_jests_failed_to_fetch_is_the_exception_only():
    """jest's own `TypeError: Failed to fetch` carries neither apt's `E: `
    nor uv's trailing colon."""
    text = "E   TypeError: Failed to fetch"
    assert _verdict(text).observations == [f"exception: {text}"]


# --- an assertion is an assertion, wherever it ran ---------------------------
# The frame beside an assertion used to decide whether the assertion was "the
# project's". It decides nothing now: the assertion line is reported as the
# assertion shape, the frame is not a shape, and where the code ran is not a
# cause.

FRAMES = (
    "",
    '  File "/app/tests/test_greeting.py", line 10, in test_greeting_text\n',
    '  File "tests/test_greet.py", line 10, in test_greet\n',
    '  File "/usr/lib/python3.12/site-packages/vendorlib/check.py", line 8\n',
    '  File "<string>", line 1, in <module>\n',
    '  File "/home/runner/work/_temp/8f2a/provision.py", line 12, in main\n',
    '  File "/home/runner/work/p/p/.github/scripts/provision.py", line 12\n',
)


@pytest.mark.parametrize("frame", FRAMES, ids=[f or "no frame" for f in FRAMES])
def test_the_frame_beside_an_assertion_changes_nothing(frame: str):
    assertion = "AssertionError: 'hello from ci_build' != 'hello from ci-build'"
    v = _verdict(f"{frame}{assertion}")
    assert v.outcome == "UNCLASSIFIED" and v.kind is None
    assert v.evidence == [Evidence(None, f"{frame}{assertion}")]
    assert v.observations == [f"assertion error: {assertion}"]


@pytest.mark.parametrize(
    "node",
    ["tests/test_greet.py", "/usr/lib/python3/dist-packages/vendorlib/tests/test_a.py"],
)
def test_the_node_id_beside_an_assertion_changes_nothing(node: str):
    text = f"FAILED {node}::test_a - AssertionError: 1 != 2"
    assert _verdict(text).observations == [f"pytest failed with assertion: {text}"]


def test_only_the_block_with_a_match_is_cited():
    """Two blocks: one carries a frame, the other the assertion. The frame
    is not a shape, so only the assertion's block is cited."""
    frame = Evidence(None, '  File "tests/test_greet.py", line 10, in test_greet')
    assertion = Evidence(None, "AssertionError: 1 != 2")
    v = read_failure(
        job_with(evidence=[frame, assertion]), step=None, completeness=COMPLETE
    )
    assert v.evidence == [assertion]
    assert v.observations == ["assertion error: AssertionError: 1 != 2"]
