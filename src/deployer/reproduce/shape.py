"""The supported shape of a failed run (spec §1.2) and the inert list (§1.3 a).

Every §1.2 check refuses by name; only a preceding non-inert step degrades the
restoration to an approximation, and that is recorded, not refused.
"""

import re
from dataclasses import dataclass
from typing import Any

import yaml

from deployer.forge import FailedJob, FailedRun, StepInfo
from deployer.reproduce.buildline import BuildConfig, Unsupported, parse_build_line

INERT_RUNS = frozenset(
    {"ls", "ls -la", "pwd", "docker version", "docker info", "docker buildx version"}
)
_SETUP_BUILDX_RE = re.compile(r"^docker/setup-buildx-action@[0-9a-f]{40}$")
_CHECKOUT_RE = re.compile(r"^actions/checkout@")
_SHA_LINE = "[command]/usr/bin/git log -1 --format=%H"
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_EVENTS = ("push", "workflow_dispatch")
_CHECKOUT_FORBIDDEN = (
    "ref",
    "repository",
    "path",
    "sparse-checkout",
    "lfs",
    "submodules",
    "fetch-depth",
)
_RUNNER_STEPS = ("Set up job", "Complete job")


@dataclass(frozen=True)
class Refusal:
    """A named reason reproduction does not proceed."""

    reason: str


@dataclass(frozen=True)
class Shape:
    """A run in the supported shape, with its binding."""

    job: FailedJob
    workflow_job: str
    build_step: int
    build: BuildConfig
    preceding_unmet: list[str]


def job_text(job: FailedJob) -> str:
    """Every piece of a job's evidence text, steps first, in order."""
    parts = [e.text for s in job.steps for e in s.evidence]
    parts.extend(e.text for e in job.evidence)
    return "\n".join(parts)


def precheck(run: FailedRun) -> FailedJob | Refusal:
    """§1.1 fields, then §1.2 #1 event, #2 checkout SHA per job, #3 one job."""
    missing = [
        name
        for name, value in (
            ("workflow_ref_path", run.workflow_ref_path),
            ("workflow_path", run.workflow_path),
            ("event", run.event),
        )
        if value is None
    ]
    if any(job.all_steps is None for job in run.jobs):
        missing.append("all_steps")
    if missing:
        return Refusal(f"snapshot lacks reproduction fields: {', '.join(missing)}")
    path = run.workflow_path or ""
    if not path.startswith(".github/workflows/") or not path.endswith(
        (".yml", ".yaml")
    ):
        return Refusal(f"workflow path not understood: {run.workflow_ref_path}")
    if run.event not in _EVENTS:
        return Refusal(f"event {run.event} not supported")
    for kept_job in run.jobs:
        shas = checkout_shas(job_text(kept_job))
        if len(shas) != 1:
            return Refusal("checkout SHA not established")
        if shas[0] != run.head_sha:
            return Refusal(f"checkout at {shas[0]}, run at {run.head_sha}")
    if len(run.jobs) != 1:
        return Refusal("several failed jobs")
    return run.jobs[0]


def job_key(workflow_text: str, job_name: str) -> str | Refusal:
    """The workflow job key an API job ``name`` maps to, by R's name rule.

    A job maps to the one key whose ``definition.name or key`` equals the
    name; none or several is a refusal, as is an unreadable workflow.
    """
    jobs = workflow_jobs(workflow_text)
    if isinstance(jobs, Refusal):
        return jobs
    matches = keys_named(jobs, job_name)
    if len(matches) != 1:
        return Refusal(
            f"no workflow job named {job_name}"
            if not matches
            else f"several workflow jobs named {job_name}"
        )
    return matches[0]


def keys_named(jobs: dict[Any, Any], job_name: str) -> list[str]:
    """Every workflow job key whose ``definition.name or key`` is ``job_name``."""
    return [
        k
        for k, v in jobs.items()
        if isinstance(v, dict) and (v.get("name") or k) == job_name
    ]


def workflow_jobs(workflow_text: str) -> dict[Any, Any] | Refusal:
    """The workflow's ``jobs`` mapping, or why the workflow cannot give one."""
    loaded = _load(workflow_text)
    return loaded if isinstance(loaded, Refusal) else loaded[1]


def _load(workflow_text: str) -> tuple[dict[str, Any], dict[Any, Any]] | Refusal:
    try:
        document = yaml.safe_load(workflow_text)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        # safe_load raises bare ValueError on an impossible date (2001-02-30),
        # TypeError on an unhashable mapping key (e.g. a flow sequence used as
        # a key) and RecursionError on deep nesting: all are an unreadable
        # workflow, never an exception out of this total reader.
        return Refusal(f"workflow not readable: {exc.__class__.__name__}")
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return Refusal("workflow has no jobs")
    return document, jobs


def check_workflow(
    run: FailedRun, job: FailedJob, workflow_text: str
) -> Shape | Refusal:
    """§1.2 #4-#8 against the workflow read from the tree at ``head_sha``."""
    prepared = _prepare(workflow_text, job)
    if isinstance(prepared, Refusal):
        return prepared
    document, definition, bound, key = prepared
    return _check_steps(document, definition, bound, job, key)


def bind_build_any(workflow_text: str, job: FailedJob) -> Shape | Refusal:
    """R's binding of ``job`` to the workflow, whatever the job's outcome.

    The job maps by R's name rule and binds step by step as in
    :func:`check_workflow`; the build step is the unique bound step whose
    single-line ``run:`` parses as a build line — its conclusion is never
    looked at. Every one of R's refusals holds: job-level constructs, a
    multi-line build run, several build steps, exactly one checkout before
    the build with no forbidden input, an unsupported build line, a
    ``working-directory``; non-inert steps between the checkout and the
    build are recorded as unmet, not refused.
    """
    prepared = _prepare(workflow_text, job)
    if isinstance(prepared, Refusal):
        return prepared
    document, definition, bound, key = prepared
    located = _locate_build(bound)
    if isinstance(located, Refusal):
        return located
    build_index, parsed = located
    return _finish_shape(document, definition, bound, job, key, build_index, parsed)


def _prepare(
    workflow_text: str, job: FailedJob
) -> (
    tuple[dict[str, Any], dict[str, Any], list[tuple[dict[str, Any], StepInfo]], str]
    | Refusal
):
    """The job's workflow key, definition and step binding (§1.2 #4, #5)."""
    key = job_key(workflow_text, job.name)
    if isinstance(key, Refusal):
        return key
    loaded = _load(workflow_text)
    assert not isinstance(loaded, Refusal)  # job_key has just loaded it
    document, jobs = loaded
    definition: dict[str, Any] = jobs[key]
    for construct, label in (
        ("uses", "job-level uses"),
        ("container", "container"),
        ("services", "services"),
    ):
        if construct in definition:
            return Refusal(f"job {key}: {label} not supported")
    strategy = definition.get("strategy")
    if isinstance(strategy, dict) and "matrix" in strategy:
        return Refusal(f"job {key}: strategy.matrix not supported")
    steps = definition.get("steps")
    if not isinstance(steps, list):
        return Refusal(f"job {key}: no steps")
    runner_steps = [s for s in (job.all_steps or []) if _is_user_step(s)]
    bound = _bind(steps, runner_steps)
    if isinstance(bound, Refusal):
        return bound
    return document, definition, bound, key


def _locate_build(
    bound: list[tuple[dict[str, Any], StepInfo]],
) -> tuple[int, BuildConfig | Unsupported] | Refusal:
    """The unique build step by its parsed ``run:`` line, conclusion ignored."""
    if _multiline_builds(bound):
        return Refusal("unsupported build configuration: multi-line run")
    builds = _single_line_builds(bound)
    if len(builds) > 1:
        return Refusal("several build steps")
    if not builds:
        return Refusal("no build step")
    return builds[0]


def _single_line_builds(
    bound: list[tuple[dict[str, Any], StepInfo]],
) -> list[tuple[int, BuildConfig | Unsupported]]:
    """Every bound step whose single-line ``run:`` parses as a build line."""
    parsed = [
        (i, parse_build_line(_single_line(s)))
        for i, (s, _) in enumerate(bound)
        if "run" in s
    ]
    return [(i, p) for i, p in parsed if p is not None]


def _multiline_builds(bound: list[tuple[dict[str, Any], StepInfo]]) -> list[int]:
    """Every bound multi-line ``run:`` whose first line parses as a build."""
    return [
        i
        for i, (s, _) in enumerate(bound)
        if "run" in s
        and _is_multiline(s)
        and parse_build_line(str(s["run"]).strip().splitlines()[0]) is not None
    ]


def _finish_shape(
    document: dict[str, Any],
    definition: dict[str, Any],
    bound: list[tuple[dict[str, Any], StepInfo]],
    job: FailedJob,
    key: str,
    build_index: int,
    parsed: BuildConfig | Unsupported,
) -> Shape | Refusal:
    """#6 checkout, #7 build line, #8 working-directory, then the inert list."""
    checkouts = [
        i
        for i, (s, _) in enumerate(bound[:build_index])
        if _CHECKOUT_RE.match(str(s.get("uses", "")))
    ]
    if len(checkouts) != 1:
        return Refusal("exactly one checkout step before the build is required")
    with_block = bound[checkouts[0]][0].get("with") or {}
    for name in _CHECKOUT_FORBIDDEN:
        if name in with_block:
            return Refusal(f"checkout input {name} not supported")
    if isinstance(parsed, Unsupported):
        return Refusal(f"unsupported build configuration: {parsed.what}")
    build_step = bound[build_index][0]
    if (
        "working-directory" in build_step
        or _default_wd(definition)
        or _default_wd(document)
    ):
        return Refusal("working-directory not supported")
    unmet = [
        f"step {info.number} ({info.name}) is not on the inert list"
        for step, info in bound[checkouts[0] + 1 : build_index]
        if not _is_inert(step)
    ]
    return Shape(
        job=job,
        workflow_job=key,
        build_step=bound[build_index][1].number,
        build=parsed,
        preceding_unmet=unmet,
    )


def _check_steps(
    document: dict[str, Any],
    definition: dict[str, Any],
    bound: list[tuple[dict[str, Any], StepInfo]],
    job: FailedJob,
    key: str,
) -> Shape | Refusal:
    failed_numbers = {
        s.ref.number for s in job.steps if s.conclusion in ("failure", "timed_out")
    }
    checkouts = [
        i
        for i, (s, _) in enumerate(bound)
        if _CHECKOUT_RE.match(str(s.get("uses", "")))
    ]

    # §1.2 #6 runs before #7: the checkout position is pinned to the failed
    # step (whether or not it turns out to be a supported build), so a
    # forbidden checkout input refuses before any build-line diagnosis.
    build_position = _find_build_position(bound, failed_numbers)
    before = [i for i in checkouts if i < build_position]
    if len(before) != 1:
        return Refusal("exactly one checkout step before the build is required")
    with_block = bound[before[0]][0].get("with") or {}
    for name in _CHECKOUT_FORBIDDEN:
        if name in with_block:
            return Refusal(f"checkout input {name} not supported")

    builds = _single_line_builds(bound)
    if _multiline_builds(bound):
        return Refusal("unsupported build configuration: multi-line run")
    if len(builds) > 1:
        return Refusal("several build steps")
    if not builds or bound[builds[0][0]][1].number not in failed_numbers:
        return Refusal("failed step is not a supported build")
    build_index, parsed = builds[0]
    if isinstance(parsed, Unsupported):
        return Refusal(f"unsupported build configuration: {parsed.what}")
    assert parsed is not None

    build_step = bound[build_index][0]
    if (
        "working-directory" in build_step
        or _default_wd(definition)
        or _default_wd(document)
    ):
        return Refusal("working-directory not supported")
    unmet = [
        f"step {info.number} ({info.name}) is not on the inert list"
        for step, info in bound[before[0] + 1 : build_index]
        if not _is_inert(step)
    ]
    return Shape(
        job=job,
        workflow_job=key,
        build_step=bound[build_index][1].number,
        build=parsed,
        preceding_unmet=unmet,
    )


def _candidate_line(step: dict[str, Any]) -> str:
    """The line a build-line parse is attempted against: the first line."""
    value = str(step.get("run", "")).strip()
    return value.splitlines()[0].strip() if value else ""


def _find_build_position(
    bound: list[tuple[dict[str, Any], StepInfo]], failed_numbers: set[int]
) -> int:
    """Where the build is, for #6's purposes: the failed step, located before

    #7 has judged whether it is actually a supported build. Prefers the
    failed step whose ``run:`` parses as a build line (single- or
    multi-line); falls back to the first failed step, or past the end of
    ``bound`` when no bound step is failed.
    """
    failed_indices = [
        i for i, (_, info) in enumerate(bound) if info.number in failed_numbers
    ]
    for i in failed_indices:
        step, _ = bound[i]
        if "run" in step and parse_build_line(_candidate_line(step)) is not None:
            return i
    return failed_indices[0] if failed_indices else len(bound)


def checkout_shas(text: str) -> list[str]:
    """Every 40-hex SHA the checkout's ``git log -1`` printed in ``text``."""
    lines = text.splitlines()
    return [
        lines[i + 1].strip()
        for i, line in enumerate(lines[:-1])
        if line.strip() == _SHA_LINE and _HEX40_RE.match(lines[i + 1].strip())
    ]


def _is_user_step(step: StepInfo) -> bool:
    return step.name not in _RUNNER_STEPS and not step.name.startswith("Post ")


def _display_name(step: dict[str, Any]) -> str:
    if step.get("name"):
        return str(step["name"])
    if "run" in step:
        return f"Run {str(step['run']).strip().splitlines()[0].strip()}"
    return f"Run {step.get('uses', '')}"


def _bind(
    steps: list[dict[str, Any]], runner_steps: list[StepInfo]
) -> list[tuple[dict[str, Any], StepInfo]] | Refusal:
    for n, (step, info) in enumerate(zip(steps, runner_steps, strict=False), start=1):
        if not isinstance(step, dict) or _display_name(step) != info.name:
            return Refusal(f"step binding failed at step {n}")
    if len(steps) != len(runner_steps):
        return Refusal(
            f"step binding failed at step {min(len(steps), len(runner_steps)) + 1}"
        )
    return list(zip(steps, runner_steps, strict=True))


def _is_multiline(step: dict[str, Any]) -> bool:
    return len(str(step["run"]).strip().splitlines()) > 1


def _single_line(step: dict[str, Any]) -> str:
    value = str(step.get("run", "")).strip()
    return "" if "\n" in value else value


def _default_wd(scope: dict[str, Any]) -> bool:
    defaults = scope.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return isinstance(run, dict) and "working-directory" in run


def _is_inert(step: dict[str, Any]) -> bool:
    if "run" in step:
        return (
            str(step["run"]).strip() in INERT_RUNS
            and "\n" not in str(step["run"]).strip()
        )
    uses = str(step.get("uses", ""))
    return bool(_SETUP_BUILDX_RE.match(uses)) and "with" not in step
