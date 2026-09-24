"""§1.2: every check refuses by name; §1.3 (a): the inert list."""

from dataclasses import replace

import pytest

from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedRun,
    FailedStep,
    StepInfo,
    StepRef,
)
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.shape import Refusal, Shape, check_workflow, precheck

SHA = "d6e330fd8d85f761962d8a134f0ffdd0e914bf9b"
CHECKOUT = "Run actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd"
BUILD = "Run docker build --file ./Dockerfile ."
WORKFLOW = """\
name: build-image
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd
      - run: docker build --file ./Dockerfile .
"""


def _job(
    text: str = f"[command]/usr/bin/git log -1 --format=%H\n{SHA}", **kw
) -> FailedJob:
    steps = [
        StepInfo(1, "Set up job", "success"),
        StepInfo(2, CHECKOUT, "success"),
        StepInfo(3, BUILD, "failure"),
        StepInfo(5, f"Post {CHECKOUT}", "success"),
        StepInfo(6, "Complete job", "success"),
    ]
    base = FailedJob(
        job_id=7,
        name="build",
        conclusion="failure",
        steps=[FailedStep(StepRef(7, 3), BUILD, "failure", [])],
        evidence=[Evidence(source=None, text=text)],
        completeness=Completeness("present", "absent"),
        all_steps=steps,
    )
    return replace(base, **kw)


def _run(**kw) -> FailedRun:
    base = FailedRun(
        repo="example/project",
        run_id=1,
        attempt=1,
        head_sha=SHA,
        url="u",
        jobs=[_job()],
        completeness=Completeness("present", "absent"),
        workflow_ref_path=".github/workflows/diagnosis-polygon.yml",
        workflow_path=".github/workflows/diagnosis-polygon.yml",
        event="workflow_dispatch",
    )
    return replace(base, **kw)


def test_polygon_shape_passes():
    run = _run()
    job = precheck(run)
    assert isinstance(job, FailedJob)
    shape = check_workflow(run, job, WORKFLOW)
    assert shape == Shape(
        job=job,
        workflow_job="build",
        build_step=3,
        build=BuildConfig("Dockerfile", (), None, None),
        preceding_unmet=[],
    )


@pytest.mark.parametrize(
    ("run_kw", "reason"),
    [
        ({"event": None}, "snapshot lacks reproduction fields: event"),
        (
            {"workflow_path": "ci.yml"},
            "workflow path not understood: .github/workflows/diagnosis-polygon.yml",
        ),
        ({"event": "pull_request"}, "event pull_request not supported"),
        ({"jobs": [_job(text="no checkout here")]}, "checkout SHA not established"),
        (
            {
                "jobs": [
                    _job(text=f"[command]/usr/bin/git log -1 --format=%H\n{'a' * 40}")
                ]
            },
            f"checkout at {'a' * 40}, run at {SHA}",
        ),
        # §1.2 #2 is per kept job: the second job here has no checkout pair
        # of its own, so it refuses there, before #3 is ever reached.
        (
            {"jobs": [_job(), _job(job_id=8, evidence=[])]},
            "checkout SHA not established",
        ),
    ],
)
def test_precheck_refusals(run_kw, reason):
    assert precheck(_run(**run_kw)) == Refusal(reason)


def test_several_failed_jobs_each_with_its_own_valid_checkout_pair():
    run = _run(jobs=[_job(), _job(job_id=8)])
    assert precheck(run) == Refusal("several failed jobs")


def test_all_steps_missing_is_a_field_refusal():
    run = _run(jobs=[_job(all_steps=None)])
    assert precheck(run) == Refusal("snapshot lacks reproduction fields: all_steps")


@pytest.mark.parametrize(
    ("workflow", "reason"),
    [
        (
            WORKFLOW.replace(
                "    runs-on",
                "    strategy:\n      matrix:\n        a: [1]\n    runs-on",
            ),
            "job build: strategy.matrix not supported",
        ),
        (
            WORKFLOW.replace(
                "@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n",
                "@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n        with:\n          ref: main\n",
            ),
            "checkout input ref not supported",
        ),
        (
            WORKFLOW.replace("./Dockerfile .", "./Dockerfile . && echo ok"),
            "step binding failed at step 2",
        ),
        (WORKFLOW.replace("  build:\n", "  other:\n"), "no workflow job named build"),
        (
            WORKFLOW.replace(
                "    runs-on",
                "    defaults:\n      run:\n        working-directory: app\n    runs-on",
            ),
            "working-directory not supported",
        ),
        (
            WORKFLOW.replace("    runs-on", "    container: node:20\n    runs-on"),
            "job build: container not supported",
        ),
    ],
)
def test_workflow_refusals(workflow, reason):
    run = _run()
    job = precheck(run)
    assert isinstance(job, FailedJob)
    assert check_workflow(run, job, workflow) == Refusal(reason)


def test_shell_chain_with_consistent_step_name_refuses_with_the_parser_reason():
    wf = WORKFLOW.replace("./Dockerfile .", "./Dockerfile . && echo ok")
    name = "Run docker build --file ./Dockerfile . && echo ok"
    job = _job(
        steps=[FailedStep(StepRef(7, 3), name, "failure", [])],
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, CHECKOUT, "success"),
            StepInfo(3, name, "failure"),
            StepInfo(6, "Complete job", "success"),
        ],
    )
    run = _run(jobs=[job])
    assert check_workflow(run, job, wf) == Refusal(
        "unsupported build configuration: shell chain"
    )


def test_checkout_refusal_precedes_build_refusal():
    """§1.2 #6 (checkout) runs before #7 (build): both broken, #6 names first."""
    wf = WORKFLOW.replace(
        "@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n",
        "@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n        with:\n          ref: main\n",
    ).replace("./Dockerfile .", "./Dockerfile . --secret id=x,src=foo")
    name = "Run docker build --file ./Dockerfile . --secret id=x,src=foo"
    job = _job(
        steps=[FailedStep(StepRef(7, 3), name, "failure", [])],
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, CHECKOUT, "success"),
            StepInfo(3, name, "failure"),
            StepInfo(6, "Complete job", "success"),
        ],
    )
    run = _run(jobs=[job])
    assert check_workflow(run, job, wf) == Refusal("checkout input ref not supported")


def test_no_checkout_before_the_build_refuses_named():
    wf = WORKFLOW.replace(
        "      - uses: actions/checkout@93cb6efe18208431cddfb8368fd83d5badbf9bfd\n",
        "",
    )
    job = _job(
        steps=[FailedStep(StepRef(7, 2), BUILD, "failure", [])],
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, BUILD, "failure"),
            StepInfo(6, "Complete job", "success"),
        ],
    )
    run = _run(jobs=[job])
    assert check_workflow(run, job, wf) == Refusal(
        "exactly one checkout step before the build is required"
    )


def test_two_build_steps_refuses_several_build_steps():
    wf = WORKFLOW.replace(
        "      - run: docker build --file ./Dockerfile .\n",
        "      - run: docker build --file ./Dockerfile .\n"
        "      - run: docker build --file ./other.Dockerfile .\n",
    )
    name2 = "Run docker build --file ./other.Dockerfile ."
    job = _job(
        steps=[FailedStep(StepRef(7, 4), name2, "failure", [])],
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, CHECKOUT, "success"),
            StepInfo(3, BUILD, "success"),
            StepInfo(4, name2, "failure"),
            StepInfo(6, "Complete job", "success"),
        ],
    )
    run = _run(jobs=[job])
    assert check_workflow(run, job, wf) == Refusal("several build steps")


def test_block_scalar_single_line_is_still_one_line():  # Review Focus 2
    wf = WORKFLOW.replace(
        "      - run: docker build --file ./Dockerfile .\n",
        "      - run: |\n          docker build --file ./Dockerfile .\n",
    )
    run = _run()
    job = precheck(run)
    assert isinstance(job, FailedJob)
    assert isinstance(check_workflow(run, job, wf), Shape)


def test_non_inert_step_between_checkout_and_build_is_an_unmet_condition():
    wf = WORKFLOW.replace(
        "      - run: docker build",
        "      - run: make gen\n      - run: docker build",
    )
    job = _job(
        steps=[FailedStep(StepRef(7, 4), BUILD, "failure", [])],
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, CHECKOUT, "success"),
            StepInfo(3, "Run make gen", "success"),
            StepInfo(4, BUILD, "failure"),
            StepInfo(6, "Complete job", "success"),
        ],
    )
    run = _run(jobs=[job])
    shape = check_workflow(run, job, wf)
    assert isinstance(shape, Shape)
    assert shape.build_step == 4
    assert shape.preceding_unmet == ["step 3 (Run make gen) is not on the inert list"]


def test_inert_steps_are_exact_strings():
    wf = WORKFLOW.replace(
        "      - run: docker build",
        '      - run: ls -la\n      - run: echo "$(touch x)"\n      - run: docker build',
    )
    names = ["Run ls -la", 'Run echo "$(touch x)"']
    job = _job(
        steps=[FailedStep(StepRef(7, 5), BUILD, "failure", [])],
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, CHECKOUT, "success"),
            StepInfo(3, names[0], "success"),
            StepInfo(4, names[1], "success"),
            StepInfo(5, BUILD, "failure"),
            StepInfo(6, "Complete job", "success"),
        ],
    )
    shape = check_workflow(_run(jobs=[job]), job, wf)
    assert isinstance(shape, Shape)
    assert shape.preceding_unmet == [f"step 4 ({names[1]}) is not on the inert list"]
