"""F §7.1: ``job_key`` and the conclusion-independent build binding."""

from dataclasses import replace
from typing import Any

import pytest

from deployer.forge import (
    Completeness,
    Evidence,
    FailedJob,
    FailedStep,
    StepInfo,
    StepRef,
)
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.shape import Refusal, Shape, bind_build_any, job_key

SHA = "d6e330fd8d85f761962d8a134f0ffdd0e914bf9b"
PIN = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"
CHECKOUT = f"Run actions/checkout@{PIN}"
BUILD = "Run docker build --file ./Dockerfile ."
TEST = "make test"
WORKFLOW = f"""\
name: build-image
on:
  workflow_dispatch:
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@{PIN}
      - run: docker build --file ./Dockerfile .
      - name: make test
        run: make test
"""
LOG = f"[command]/usr/bin/git log -1 --format=%H\n{SHA}"


def _job(conclusions: tuple[str, str, str], **kw: Any) -> FailedJob:
    """A job of WORKFLOW whose checkout/build/test steps ended as given."""
    names = (CHECKOUT, BUILD, TEST)
    user = [StepInfo(n, name, c) for n, name, c in zip((2, 3, 4), names, conclusions)]
    steps = [
        StepInfo(1, "Set up job", "success"),
        *user,
        StepInfo(5, f"Post {CHECKOUT}", "success"),
        StepInfo(6, "Complete job", "success"),
    ]
    failed = [
        FailedStep(StepRef(7, s.number), s.name, str(s.conclusion), [])
        for s in user
        if s.conclusion != "success"
    ]
    base = FailedJob(
        job_id=7,
        name="build",
        conclusion="failure" if failed else "success",
        steps=failed,
        evidence=[Evidence(source=7, text=LOG)],
        completeness=Completeness("present", "absent"),
        all_steps=steps,
    )
    return replace(base, **kw)


def _shape(job: FailedJob) -> Shape:
    return Shape(
        job=job,
        workflow_job="build",
        build_step=3,
        build=BuildConfig("Dockerfile", (), None, None),
        preceding_unmet=[],
    )


def test_green_build_step_binds() -> None:
    job = _job(("success", "success", "success"))
    assert bind_build_any(WORKFLOW, job) == _shape(job)


def test_job_failed_after_the_build_binds_the_green_build() -> None:
    job = _job(("success", "success", "failure"))
    assert bind_build_any(WORKFLOW, job) == _shape(job)


def test_failed_build_binds_too() -> None:
    job = _job(("success", "failure", "skipped"))
    assert bind_build_any(WORKFLOW, job) == _shape(job)


def test_step_not_on_the_inert_list_is_recorded() -> None:
    workflow = WORKFLOW.replace(
        "      - run: docker build", "      - run: make gen\n      - run: docker build"
    )
    job = _job(("success", "success", "success"))
    assert job.all_steps is not None
    steps = list(job.all_steps)
    steps.insert(2, StepInfo(3, "Run make gen", "success"))
    steps[3:6] = [replace(s, number=s.number + 1) for s in steps[3:6]]
    job = replace(job, all_steps=steps)
    shape = bind_build_any(workflow, job)
    assert isinstance(shape, Shape)
    assert shape.build_step == 4
    assert shape.preceding_unmet == ["step 3 (Run make gen) is not on the inert list"]


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            "        run: make test\n",
            "        run: docker build .\n",
            "several build steps",
        ),
        (
            "      - run: docker build --file ./Dockerfile .\n",
            "      - run: |\n          docker build --file ./Dockerfile .\n"
            "          echo done\n",
            "unsupported build configuration: multi-line run",
        ),
        (
            f"@{PIN}\n",
            f"@{PIN}\n        with:\n          fetch-depth: 0\n",
            "checkout input fetch-depth not supported",
        ),
        (
            "    runs-on: ubuntu-24.04\n",
            "    runs-on: ubuntu-24.04\n    defaults:\n      run:\n"
            "        working-directory: app\n",
            "working-directory not supported",
        ),
        (
            "    runs-on: ubuntu-24.04\n",
            "    runs-on: ubuntu-24.04\n    container: alpine\n",
            "job build: container not supported",
        ),
    ],
    ids=["several", "multiline", "fetch-depth", "wd", "container"],
)
def test_r_refusals_hold(old: str, new: str, reason: str) -> None:
    workflow = WORKFLOW.replace(old, new)
    job = _job(("success", "success", "success"))
    assert bind_build_any(workflow, job) == Refusal(reason)


def test_no_build_step_refuses() -> None:
    workflow = WORKFLOW.replace("docker build --file ./Dockerfile .", "make image")
    job = _job(("success", "success", "success"))
    job = replace(
        job,
        all_steps=[
            replace(s, name="Run make image") if s.number == 3 else s
            for s in job.all_steps or []
        ],
    )
    assert bind_build_any(workflow, job) == Refusal("no build step")


def test_checkout_after_the_build_is_not_before_it() -> None:
    workflow = WORKFLOW.replace(f"      - uses: actions/checkout@{PIN}\n", "").replace(
        "      - name: make test\n        run: make test\n",
        f"      - uses: actions/checkout@{PIN}\n",
    )
    job = _job(("success", "success", "success"))
    job = replace(
        job,
        all_steps=[
            StepInfo(1, "Set up job", "success"),
            StepInfo(2, BUILD, "success"),
            StepInfo(3, CHECKOUT, "success"),
            StepInfo(4, "Complete job", "success"),
        ],
    )
    assert bind_build_any(workflow, job) == Refusal(
        "exactly one checkout step before the build is required"
    )


def test_unsupported_build_line_refuses_by_parser_reason() -> None:
    workflow = WORKFLOW.replace(
        "docker build --file ./Dockerfile .", "docker build --no-cache ."
    )
    job = _job(("success", "success", "success"))
    job = replace(
        job,
        all_steps=[
            replace(s, name="Run docker build --no-cache .") if s.number == 3 else s
            for s in job.all_steps or []
        ],
    )
    assert bind_build_any(workflow, job) == Refusal(
        "unsupported build configuration: flag --no-cache"
    )


def test_step_binding_failure_refuses() -> None:
    job = _job(("success", "success", "success"), all_steps=[])
    assert bind_build_any(WORKFLOW, job) == Refusal("step binding failed at step 1")


def test_job_key_by_name_rule() -> None:
    workflow = WORKFLOW.replace("  build:\n", "  build:\n    name: Image\n")
    assert job_key(WORKFLOW, "build") == "build"
    assert job_key(workflow, "Image") == "build"
    assert job_key(workflow, "build") == Refusal("no workflow job named build")


def test_job_key_several_and_unreadable() -> None:
    twin = WORKFLOW + "  other:\n    name: build\n    runs-on: x\n    steps: []\n"
    assert job_key(twin, "build") == Refusal("several workflow jobs named build")
    assert job_key("jobs: [", "build") == Refusal("workflow not readable: ParserError")
    assert job_key("name: x\n", "build") == Refusal("workflow has no jobs")
