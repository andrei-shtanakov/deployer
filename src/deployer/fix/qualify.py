"""Which CI attempts of the fix commit qualify (design §7.2).

Every attempt gets exactly one of three results. ``excluded`` needs a reason
**proven** from data that was read; ``undetermined`` is every case where a
condition could not be established — an unread attempt, an unfinished one, a
missing log, an ambiguous mapping, a checkout SHA not in the log, a build
that does not bind. Nothing is dropped silently: an undetermined attempt
blocks the claim that no attempt contradicts the fix (§7.4).

Order: the run-level facts from the listing (event, ``head_sha``, path) and
the workflow bytes are judged first, since they exclude the attempt whatever
else could or could not be read; then the attempt itself, the job mapping,
the mapped job's log, and finally the checkout and the build binding, where
a proven exclusion wins over an undetermined sibling condition.

The workflow bytes are the caller's read at the fix commit; ``qualify``
hashes them against the SHA-256 R's workflow had at the original run's
``head_sha`` (``FixDocument.input.workflow_sha256``) and decodes them as R
reads a workflow. It never raises.
"""

from dataclasses import dataclass
from typing import Literal

from deployer.admission.prepare import decode_as_read_text
from deployer.forge import AttemptRead, FailedJob
from deployer.provenance.model import sha256_hex
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.shape import (
    Refusal,
    Shape,
    bind_build_any,
    checkout_shas,
    job_text,
    keys_named,
    workflow_jobs,
)

Qualification = Literal["qualified", "excluded", "undetermined"]
_EVENTS = ("push", "workflow_dispatch")
_CONTINUE_ON_ERROR = "continue-on-error"
# No context field: buildline.parse_build_line admits only the context ".".
_COMPARED = ("dockerfile", "build_args", "platform")


@dataclass(frozen=True)
class Qualified:
    """One attempt's qualification, and what it was bound to.

    ``job_key`` is the original workflow job key once exactly one job of the
    attempt maps to it; ``job``/``shape`` are set only when qualified.
    """

    run_id: int
    attempt: int
    job_key: str | None
    status: Qualification
    reason: str | None
    job: FailedJob | None
    shape: Shape | None


@dataclass(frozen=True)
class _Verdict:
    """A qualification before the attempt's identity is stamped on it."""

    status: Qualification
    reason: str | None
    key: str | None = None
    job: FailedJob | None = None
    shape: Shape | None = None


def qualify(
    read: AttemptRead,
    fix_commit: str,
    original_path: str,
    original_key: str,
    original_build: BuildConfig,
    workflow: bytes,
    original_workflow_sha256: str,
) -> Qualified:
    """Qualify one attempt against the original run's binding (§7.2).

    ``original_path``/``original_key``/``original_build`` are the fix
    document's ``input.workflow_path``, ``reproduction_binding
    ["workflow_job"]`` and ``build``; ``workflow`` is the bytes of
    ``original_path`` read at ``fix_commit``. Any unexpected exception is
    an ``undetermined`` result, never raised.
    """
    try:
        verdict = _judge(
            read,
            fix_commit,
            original_path,
            original_key,
            original_build,
            workflow,
            original_workflow_sha256,
        )
    except Exception as exc:  # noqa: BLE001 — totality: never raise
        verdict = _Verdict(
            "undetermined", f"qualification failed: {type(exc).__name__}: {exc}"
        )
    return Qualified(
        run_id=read.run.run_id,
        attempt=read.attempt,
        job_key=verdict.key,
        status=verdict.status,
        reason=verdict.reason,
        job=verdict.job,
        shape=verdict.shape,
    )


def _judge(
    read: AttemptRead,
    fix_commit: str,
    original_path: str,
    original_key: str,
    original_build: BuildConfig,
    workflow: bytes,
    original_workflow_sha256: str,
) -> _Verdict:
    """Run-level, attempt-level, mapping, then the mapped job's own checks."""
    excluded = _run_exclusion(read, fix_commit, original_path)
    if excluded is None and sha256_hex(workflow) != original_workflow_sha256:
        excluded = (
            f"workflow {original_path} at the fix commit differs from the one R bound"
        )
    if excluded is not None:
        return _Verdict("excluded", excluded)
    if read.error is not None:
        return _Verdict("undetermined", f"attempt not read: {read.error}")
    if read.status != "completed":
        return _Verdict("undetermined", f"attempt not completed: {read.status}")
    if read.jobs is None:
        return _Verdict("undetermined", "attempt jobs not read")
    text = decode_as_read_text(workflow)
    mapped = _map_job(read.jobs, text, original_key)
    if isinstance(mapped, _Verdict):
        return mapped
    return _judge_job(read, mapped, text, fix_commit, original_key, original_build)


def _run_exclusion(read: AttemptRead, fix_commit: str, path: str) -> str | None:
    """A reason the listed run cannot qualify, proven from the listing."""
    run = read.run
    if run.event not in _EVENTS:
        return f"event {run.event} is not push/workflow_dispatch"
    if run.head_sha != fix_commit:
        return f"head_sha {run.head_sha} is not the fix commit {fix_commit}"
    if run.path != path:
        return f"workflow path {run.path} is not {path}"
    return None


def _map_job(jobs: list[FailedJob], text: str, key: str) -> FailedJob | _Verdict:
    """The one job mapping to ``key`` by R's name rule, or the verdict."""
    loaded = workflow_jobs(text)
    if isinstance(loaded, Refusal):
        return _Verdict("undetermined", f"workflow at the fix commit: {loaded.reason}")
    candidates = [(job, keys_named(loaded, job.name)) for job in jobs]
    for job, keys in candidates:
        if key in keys and len(keys) > 1:
            return _Verdict(
                "undetermined",
                f"job {job.job_id} ({job.name}) maps to several workflow jobs",
            )
    mapped = [job for job, keys in candidates if keys == [key]]
    if not mapped:
        return _Verdict("excluded", f"no job maps to workflow job {key}")
    if len(mapped) > 1:
        ids = ", ".join(str(job.job_id) for job in mapped)
        return _Verdict(
            "undetermined", f"several jobs map to workflow job {key}: {ids}"
        )
    return mapped[0]


def _judge_job(
    read: AttemptRead,
    job: FailedJob,
    text: str,
    fix_commit: str,
    key: str,
    original_build: BuildConfig,
) -> _Verdict:
    """The mapped job's log, checkout SHA and build binding."""
    state = read.logs_state.get(job.job_id)
    if state != "present":
        shown = state or "not recorded"
        return _Verdict("undetermined", f"log of job {job.job_id} is {shown}", key)
    shas = checkout_shas(job_text(job))
    if len(shas) == 1 and shas[0] != fix_commit:
        return _Verdict(
            "excluded", f"checkout at {shas[0]}, not the fix commit {fix_commit}", key
        )
    shape = bind_build_any(text, job)
    if isinstance(shape, Shape):
        differs = _differing(shape.build, original_build)
        if differs:
            reason = f"build configuration differs: {', '.join(differs)}"
            return _Verdict("excluded", reason, key)
    if len(shas) != 1:
        return _Verdict("undetermined", "checkout SHA not established", key)
    if isinstance(shape, Refusal):
        return _Verdict("undetermined", f"build not bound: {shape.reason}", key)
    if _CONTINUE_ON_ERROR in text:
        # ``continue-on-error`` can make the API report a failed step as
        # ``success``, and the CI COPY row trusts that conclusion (ruling
        # AB/AD); the workflow text is the project's own, so any occurrence
        # refuses (#100 review).
        reason = f"workflow uses {_CONTINUE_ON_ERROR}; step conclusions unproven"
        return _Verdict("undetermined", reason, key)
    return _Verdict("qualified", None, key, job, shape)


def _differing(bound: BuildConfig, original: BuildConfig) -> list[str]:
    """The compared build fields (never the tag) that differ.

    The context needs no field: R's build line admits ``.`` only. Build args
    compare as pairs whatever sequence type the caller built them in.
    """
    left, right = _comparable(bound), _comparable(original)
    return [f for f in _COMPARED if left[f] != right[f]]


def _comparable(build: BuildConfig) -> dict[str, object]:
    """The compared fields, build args normalised to a tuple of pairs."""
    return {
        "dockerfile": build.dockerfile,
        "build_args": tuple(tuple(p) for p in build.build_args),
        "platform": build.platform,
    }
