"""F §7.2: every attempt is qualified, excluded (proven) or undetermined."""

from dataclasses import replace
from typing import Any

import pytest

from deployer.fix.qualify import Qualified, qualify
from deployer.forge import (
    AttemptRead,
    Completeness,
    Evidence,
    FailedJob,
    FailedStep,
    LogsState,
    RunSummary,
    StepInfo,
    StepRef,
)
from deployer.provenance.model import sha256_hex
from deployer.reproduce.buildline import BuildConfig, Unsupported, parse_build_line
from deployer.reproduce.shape import Shape

FIX = "f" * 40
OTHER = "a" * 40
PIN = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"
PATH = ".github/workflows/image.yml"
CHECKOUT = f"Run actions/checkout@{PIN}"
BUILD = "Run docker build --file ./Dockerfile --build-arg V=1 ."
WORKFLOW = f"""\
name: image
on:
  push:
jobs:
  lint:
    runs-on: ubuntu-24.04
    steps:
      - run: ruff check
  image:
    name: Build image
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@{PIN}
      - run: docker build --file ./Dockerfile --build-arg V=1 .
      - name: smoke
        run: make smoke
""".encode()
ORIGINAL_SHA = sha256_hex(WORKFLOW)
ORIGINAL_BUILD = BuildConfig("Dockerfile", (("V", "1"),), None, "ignored:tag")


def _log(sha: str) -> str:
    return f"[command]/usr/bin/git log -1 --format=%H\n{sha}"


def _job(job_id: int = 11, name: str = "Build image", log: str = "") -> FailedJob:
    """A green image job whose later smoke step failed; checkout at FIX."""
    steps = [
        StepInfo(1, "Set up job", "success"),
        StepInfo(2, CHECKOUT, "success"),
        StepInfo(3, BUILD, "success"),
        StepInfo(4, "smoke", "failure"),
        StepInfo(5, "Complete job", "success"),
    ]
    return FailedJob(
        job_id=job_id,
        name=name,
        conclusion="failure",
        steps=[FailedStep(StepRef(job_id, 4), "smoke", "failure", [])],
        evidence=[Evidence(source=job_id, text=log or _log(FIX))],
        completeness=Completeness("present", "absent"),
        all_steps=steps,
    )


def _lint() -> FailedJob:
    return FailedJob(
        job_id=10,
        name="lint",
        conclusion="success",
        steps=[],
        evidence=[],
        completeness=Completeness("present", "absent"),
        all_steps=[StepInfo(1, "Run ruff check", "success")],
    )


def _read(
    jobs: list[FailedJob] | None = None,
    logs: dict[int, LogsState] | None = None,
    **kw: Any,
) -> AttemptRead:
    listed = jobs if jobs is not None else [_lint(), _job()]
    run = RunSummary(
        run_id=900,
        attempts=2,
        event=str(kw.pop("event", "push")),
        head_sha=str(kw.pop("head_sha", FIX)),
        path=str(kw.pop("path", PATH)),
        head_branch="fix/x",
    )
    base = AttemptRead(
        run=run,
        attempt=2,
        status="completed",
        conclusion="failure",
        jobs=listed,
        logs_state=logs if logs is not None else {j.job_id: "present" for j in listed},
        error=None,
    )
    return replace(base, **kw)


def _q(
    read: AttemptRead,
    workflow: bytes = WORKFLOW,
    build: BuildConfig = ORIGINAL_BUILD,
    original_sha: str = ORIGINAL_SHA,
) -> Qualified:
    return qualify(read, FIX, PATH, "image", build, workflow, original_sha)


def test_qualified_on_a_job_green_through_the_build() -> None:
    job = _job()
    result = _q(_read(jobs=[_lint(), job]))
    assert result == Qualified(
        run_id=900,
        attempt=2,
        job_key="image",
        status="qualified",
        reason=None,
        job=job,
        shape=Shape(
            job=job,
            workflow_job="image",
            build_step=3,
            build=BuildConfig("Dockerfile", (("V", "1"),), None, None),
            preceding_unmet=[],
        ),
    )


def test_workflow_dispatch_qualifies_and_tag_is_not_compared() -> None:
    build = replace(ORIGINAL_BUILD, tag=None)
    assert _q(_read(event="workflow_dispatch"), build=build).status == "qualified"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"event": "pull_request"}, "event pull_request is not push/workflow_dispatch"),
        ({"head_sha": OTHER}, f"head_sha {OTHER} is not the fix commit {FIX}"),
        (
            {"path": ".github/workflows/other.yml"},
            f"workflow path .github/workflows/other.yml is not {PATH}",
        ),
    ],
    ids=["event", "head-sha", "path"],
)
def test_run_level_exclusions(kw: dict[str, Any], reason: str) -> None:
    result = _q(_read(**kw))
    assert (result.status, result.reason) == ("excluded", reason)


def test_run_level_exclusion_wins_over_an_unread_attempt() -> None:
    read = replace(_read(event="pull_request"), error="HTTP 502", jobs=None)
    assert _q(read).status == "excluded"


def test_workflow_bytes_differ_is_excluded() -> None:
    result = _q(_read(), original_sha="0" * 64)
    assert result.status == "excluded"
    assert result.reason == (
        f"workflow {PATH} at the fix commit differs from the one R bound"
    )


def test_no_job_maps_is_excluded() -> None:
    result = _q(_read(jobs=[_lint()]))
    assert (result.status, result.reason) == (
        "excluded",
        "no job maps to workflow job image",
    )
    assert result.job_key is None


def test_checkout_read_and_different_is_excluded() -> None:
    result = _q(_read(jobs=[_job(log=_log(OTHER))]))
    assert (result.status, result.reason) == (
        "excluded",
        f"checkout at {OTHER}, not the fix commit {FIX}",
    )


def test_build_config_differs_is_excluded() -> None:
    build = replace(ORIGINAL_BUILD, build_args=(("V", "2"),))
    result = _q(_read(), build=build)
    assert result.status == "excluded"
    assert result.reason == "build configuration differs: build_args"
    assert result.job_key == "image"


def test_platform_differs_is_excluded() -> None:
    build = replace(ORIGINAL_BUILD, platform="linux/arm64")
    assert _q(_read(), build=build).reason == "build configuration differs: platform"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"error": "HTTP 502", "jobs": None}, "attempt not read: HTTP 502"),
        (
            {"status": "in_progress", "conclusion": None, "jobs": None},
            "attempt not completed: in_progress",
        ),
        ({"jobs": None}, "attempt jobs not read"),
    ],
    ids=["error", "in-progress", "no-jobs"],
)
def test_attempt_level_undetermined(kw: dict[str, Any], reason: str) -> None:
    result = _q(replace(_read(), **kw))
    assert (result.status, result.reason) == ("undetermined", reason)


@pytest.mark.parametrize("state", ["unavailable", "error"])
def test_mapped_job_log_missing_is_undetermined(state: LogsState) -> None:
    result = _q(_read(logs={10: "present", 11: state}))
    assert (result.status, result.reason) == (
        "undetermined",
        f"log of job 11 is {state}",
    )
    assert result.job_key == "image"


def test_mapped_job_log_state_absent_is_undetermined() -> None:
    result = _q(_read(logs={10: "present"}))
    assert (result.status, result.reason) == (
        "undetermined",
        "log of job 11 is not recorded",
    )


def test_several_jobs_map_is_undetermined() -> None:
    result = _q(_read(jobs=[_job(), _job(job_id=12)]))
    assert (result.status, result.reason) == (
        "undetermined",
        "several jobs map to workflow job image: 11, 12",
    )


def test_job_name_ambiguous_in_workflow_is_undetermined() -> None:
    twin = WORKFLOW + b"  twin:\n    name: Build image\n    runs-on: x\n"
    result = _q(_read(), workflow=twin, original_sha=sha256_hex(twin))
    assert (result.status, result.reason) == (
        "undetermined",
        "job 11 (Build image) maps to several workflow jobs",
    )


@pytest.mark.parametrize(
    "log", ["no checkout here", _log(FIX) + "\n" + _log(OTHER)], ids=["none", "two"]
)
def test_checkout_not_established_is_undetermined(log: str) -> None:
    result = _q(_read(jobs=[_job(log=log)]))
    assert (result.status, result.reason) == (
        "undetermined",
        "checkout SHA not established",
    )


def test_build_binding_refused_is_undetermined() -> None:
    job = replace(_job(), all_steps=[StepInfo(1, "Set up job", "failure")])
    result = _q(_read(jobs=[job]))
    assert (result.status, result.reason) == (
        "undetermined",
        "build not bound: step binding failed at step 1",
    )


def test_unreadable_workflow_is_undetermined() -> None:
    bad = b"jobs: ["
    result = _q(_read(), workflow=bad, original_sha=sha256_hex(bad))
    assert (result.status, result.reason) == (
        "undetermined",
        "workflow at the fix commit: workflow not readable: ParserError",
    )


def test_qualify_never_raises() -> None:
    # A timestamp YAML accepts syntactically but cannot construct.
    bad = b"when: 2001-13-45\njobs: {}\n"
    result = _q(_read(), workflow=bad, original_sha=sha256_hex(bad))
    assert result.status == "undetermined"
    assert result.reason is not None
    assert result.reason.startswith("qualification failed: ValueError")


def test_proven_config_difference_wins_over_missing_checkout() -> None:
    build = replace(ORIGINAL_BUILD, dockerfile="other/Dockerfile")
    result = _q(_read(jobs=[_job(log="no checkout here")]), build=build)
    assert (result.status, result.reason) == (
        "excluded",
        "build configuration differs: dockerfile",
    )


def test_build_args_compare_as_pairs() -> None:
    args: Any = [["V", "1"]]
    build = replace(ORIGINAL_BUILD, build_args=args)  # the stored JSON form
    assert _q(_read(), build=build).status == "qualified"


def test_context_other_than_dot_never_parses() -> None:
    """Why ``_COMPARED`` has no context: the build line admits ``.`` only."""
    assert parse_build_line("docker build app") == Unsupported("context app")
    assert parse_build_line("docker build . ") == BuildConfig(
        "Dockerfile", (), None, None
    )


def test_fully_green_mapped_job_qualifies() -> None:
    job = _job()
    assert job.all_steps is not None
    green = replace(
        job,
        conclusion="success",
        steps=[],
        all_steps=[replace(s, conclusion="success") for s in job.all_steps],
    )
    result = _q(_read(jobs=[_lint(), green]))
    assert (result.status, result.reason, result.job) == ("qualified", None, green)
    assert result.shape is not None
    assert result.shape.build_step == 3
