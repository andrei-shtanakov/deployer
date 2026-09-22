"""The single GitHub-API chokepoint: a snapshot of facts about a failed run.

Every ``gh api`` call this package makes goes through :class:`GhRunner`, by the
same argument that makes ``runtime.py`` the only container-subprocess boundary:
one chokepoint is trivially substitutable in tests and impossible to bypass
unnoticed. The module GETS facts and does not interpret them; ``diagnose.py``
consumes the dataclasses below unchanged.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import TypeAdapter

GH_TIMEOUT_S = 30.0
"""Wall-clock budget for one ``gh api`` invocation."""

SNAPSHOT_SCHEMA_VERSION = "1.0"

_PER_PAGE = 100
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})
_GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")
_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
# The runner colours some lines (e.g. echoing the step command) with ANSI CSI
# sequences; stripping is mechanical framing removal, not interpretation.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_GROUP_PREFIX = "##[group]"
_ENDGROUP = "##[endgroup]"

LogsState = Literal["present", "unavailable", "error"]
AnnotationsState = Literal["present", "absent", "error"]
_LOGS_RANK: dict[LogsState, int] = {"present": 0, "unavailable": 1, "error": 2}
_ANNOTATIONS_RANK: dict[AnnotationsState, int] = {"present": 0, "absent": 1, "error": 2}


@dataclass(frozen=True)
class RunRef:
    """A workflow run named by repository (``owner/name``) and run id."""

    repo: str
    run_id: int


@dataclass(frozen=True)
class StepRef:
    """Composite step key; the API gives steps no id of their own."""

    job_id: int
    number: int


@dataclass(frozen=True)
class Evidence:
    """A block of text and where it came from.

    ``source`` is a step, a job id (job-level evidence such as an annotation),
    or ``None`` when the API gave no binding and none was invented.
    """

    source: StepRef | int | None
    text: str


@dataclass(frozen=True)
class FailedStep:
    """A non-green step of a kept job."""

    ref: StepRef
    name: str
    conclusion: str
    evidence: list[Evidence]


@dataclass(frozen=True)
class FailedJob:
    """A non-green job with its kept steps and job-level evidence."""

    job_id: int
    name: str
    conclusion: str
    steps: list[FailedStep]
    evidence: list[Evidence]


@dataclass(frozen=True)
class Completeness:
    """Per-source collection state: three distinct states, not a boolean.

    With zero kept jobs nothing was fetched, so the states read
    ``unavailable``/``absent``; check ``jobs`` before reading them as
    "logs expired".
    """

    logs: LogsState
    annotations: AnnotationsState


@dataclass(frozen=True)
class FailedRun:
    """The snapshot of a finished, failed run at one fixed attempt."""

    repo: str
    run_id: int
    attempt: int
    head_sha: str
    url: str
    jobs: list[FailedJob]
    completeness: Completeness
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class AdapterRefusal:
    """The adapter declined to snapshot: the run is unfinished or not failed."""

    reason: Literal["not_finished", "not_failed"]
    detail: str


class GhError(Exception):
    """A ``gh api`` call failed: nonzero exit, timeout or missing binary."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GhRunner(Protocol):
    """Anything that answers ``gh api <argv>`` with stdout text."""

    def api(self, argv: list[str], *, timeout: float) -> str:
        """Return stdout; raise :class:`GhError` on failure."""
        ...


class SubprocessGh:
    """The real runner: ``gh api`` as an argument vector, never a shell."""

    def api(self, argv: list[str], *, timeout: float) -> str:
        """Run ``gh api *argv`` under ``timeout`` with prompts disabled."""
        cmd = ["gh", "api", *argv]
        what = " ".join(argv)
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh api {what} timed out after {timeout}s") from exc
        except OSError as exc:
            raise GhError(f"gh api could not start: {exc}") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            match = _HTTP_STATUS_RE.search(stderr)
            status = int(match.group(1)) if match else None
            detail = stderr or f"exit code {proc.returncode}"
            raise GhError(f"gh api {what} failed: {detail}", status)
        return proc.stdout


_run_adapter: TypeAdapter[FailedRun] = TypeAdapter(FailedRun)


def dump_snapshot(run: FailedRun) -> str:
    """Serialize a snapshot as versioned JSON (``snapshot_schema_version``)."""
    return _run_adapter.dump_json(run, indent=2).decode()


def load_snapshot(text: str) -> FailedRun:
    """Parse JSON written by :func:`dump_snapshot`."""
    return _run_adapter.validate_json(text)


def fetch_failed_run(
    ref: RunRef, *, attempt: int | None, runner: GhRunner | None = None
) -> FailedRun | AdapterRefusal:
    """Snapshot a failed run, or refuse if it is unfinished or not failed.

    One run-metadata call decides both the refusal and the attempt; the
    attempt is fixed once, before any jobs or logs are read, so a re-run
    never mixes evidence from different attempts. A GhError on the run or
    jobs calls propagates; per-job log and annotation failures are recorded
    in ``Completeness`` instead.
    """
    gh = _Gh(runner if runner is not None else SubprocessGh(), ref.repo)
    run = gh.json(_run_path(ref.run_id, attempt))
    refusal = _refuse(run)
    if refusal is not None:
        return refusal
    resolved = attempt if attempt is not None else int(run["run_attempt"])
    kept = [
        job
        for job in gh.jobs(ref.run_id, resolved)
        if job.get("conclusion") not in _GREEN_CONCLUSIONS
    ]
    jobs: list[FailedJob] = []
    logs_states: list[LogsState] = []
    annotations_states: list[AnnotationsState] = []
    for record in kept:
        job_id = int(record["id"])
        log_text, logs_state = gh.logs(job_id)
        annotations, annotations_state = gh.annotations(job_id)
        logs_states.append(logs_state)
        annotations_states.append(annotations_state)
        jobs.append(_build_job(record, job_id, log_text, annotations))
    return FailedRun(
        repo=ref.repo,
        run_id=ref.run_id,
        attempt=resolved,
        head_sha=str(run.get("head_sha", "")),
        url=str(run.get("html_url", "")),
        jobs=jobs,
        completeness=Completeness(
            logs=_worst(logs_states, _LOGS_RANK, "unavailable"),
            annotations=_worst(annotations_states, _ANNOTATIONS_RANK, "absent"),
        ),
    )


def _run_path(run_id: int, attempt: int | None) -> str:
    base = f"actions/runs/{run_id}"
    return base if attempt is None else f"{base}/attempts/{attempt}"


def _refuse(run: dict[str, Any]) -> AdapterRefusal | None:
    status = run.get("status")
    if status != "completed":
        return AdapterRefusal("not_finished", f"run status is {status!r}")
    conclusion = run.get("conclusion")
    if conclusion not in _FAILED_CONCLUSIONS:
        return AdapterRefusal("not_failed", f"run conclusion is {conclusion!r}")
    return None


def _worst[S: str](states: list[S], rank: dict[S, int], empty: S) -> S:
    if not states:
        return empty
    return max(states, key=lambda state: rank[state])


class _Gh:
    """Endpoint helpers over a runner; every path is under ``repos/{repo}``."""

    def __init__(self, runner: GhRunner, repo: str) -> None:
        self._runner = runner
        self._prefix = f"repos/{repo}"

    def text(self, path: str, *, extra: tuple[str, ...] = ()) -> str:
        """Fetch ``path``; ``extra`` are ``gh api`` flags placed before it."""
        return self._runner.api(
            [*extra, f"{self._prefix}/{path}"], timeout=GH_TIMEOUT_S
        )

    def json(self, path: str) -> Any:
        return json.loads(self.text(path))

    def jobs(self, run_id: int, attempt: int) -> list[dict[str, Any]]:
        """All jobs of one attempt, paginated until ``total_count`` is met."""
        base = f"actions/runs/{run_id}/attempts/{attempt}/jobs"
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            body = self.json(f"{base}?per_page={_PER_PAGE}&page={page}")
            items = list(body.get("jobs") or [])
            collected.extend(items)
            total = int(body.get("total_count", len(collected)))
            if not items or len(collected) >= total:
                return collected
            page += 1

    def logs(self, job_id: int) -> tuple[str, LogsState]:
        """A job's log text and what its absence means.

        Only an HTTP status is data about the run: 410 is GitHub's expired-log
        answer (``unavailable``), any other status is a fetch that GitHub
        answered badly (``error``). A ``GhError`` with no status means ``gh``
        itself never reached GitHub — unknown flag, timeout, missing binary —
        which is a broken instrument, not missing data, so it propagates
        rather than being recorded as an unreadable log.
        """
        try:
            # Real build logs routinely carry the runner's ANSI colouring
            # (e.g. the echoed step command); gh refuses to print those
            # without this flag. `_split_blocks` strips the sequences.
            text = self.text(
                f"actions/jobs/{job_id}/logs", extra=("--allow-escape-sequences",)
            )
        except GhError as exc:
            if exc.status is None:
                raise
            return "", "unavailable" if exc.status == 410 else "error"
        return (text, "present") if text.strip() else ("", "unavailable")

    def annotations(self, job_id: int) -> tuple[list[dict[str, Any]], AnnotationsState]:
        """A job's annotations and what a short read means.

        Same rule as :meth:`logs`: an HTTP status means GitHub answered, so a
        failed fetch is data about the run (``error``, with whatever pages did
        arrive kept). A ``GhError`` with no status means ``gh`` itself never
        reached GitHub, which is a broken instrument and propagates.
        """
        base = f"check-runs/{job_id}/annotations"
        collected: list[dict[str, Any]] = []
        page = 1
        try:
            while True:
                items = list(self.json(f"{base}?per_page={_PER_PAGE}&page={page}"))
                collected.extend(items)
                if len(items) < _PER_PAGE:
                    break
                page += 1
        except GhError as exc:
            if exc.status is None:
                raise
            return collected, "error"
        return collected, "present" if collected else "absent"


def _build_job(
    record: dict[str, Any],
    job_id: int,
    log_text: str,
    annotations: list[dict[str, Any]],
) -> FailedJob:
    all_steps = list(record.get("steps") or [])
    step_evidence, job_evidence = _bind_log(log_text, job_id, all_steps)
    steps = [
        FailedStep(
            ref=StepRef(job_id, int(step["number"])),
            name=str(step.get("name", "")),
            conclusion=str(step.get("conclusion")),
            evidence=step_evidence.pop(StepRef(job_id, int(step["number"])), []),
        )
        for step in all_steps
        if step.get("conclusion") not in _GREEN_CONCLUSIONS
    ]
    # Blocks bound to a green step: the step is not kept, the fact is.
    job_evidence.extend(e for bound in step_evidence.values() for e in bound)
    job_evidence.extend(
        Evidence(
            source=job_id,
            text=f"{a.get('annotation_level')}: {a.get('message')}",
        )
        for a in annotations
    )
    return FailedJob(
        job_id=job_id,
        name=str(record.get("name", "")),
        conclusion=str(record.get("conclusion")),
        steps=steps,
        evidence=job_evidence,
    )


def _bind_log(
    log_text: str, job_id: int, steps: list[dict[str, Any]]
) -> tuple[dict[StepRef, list[Evidence]], list[Evidence]]:
    """Split a job log into blocks; bind a block to a step only by exact title.

    A ``##[group]<title>`` block binds to the one step whose name is ``title``
    or whose name is ``title`` minus the runner's ``Run `` prefix. Anything
    else — other groups, lines outside a group, ambiguous titles — is
    job-level evidence with ``source=None``.
    """
    by_step: dict[StepRef, list[Evidence]] = {}
    unbound: list[Evidence] = []
    for title, lines in _split_blocks(log_text):
        text = "\n".join(lines)
        ref = _step_for_title(title, job_id, steps) if title is not None else None
        if ref is None:
            unbound.append(Evidence(source=None, text=text))
        else:
            by_step.setdefault(ref, []).append(Evidence(source=ref, text=text))
    return by_step, unbound


def _step_for_title(
    title: str, job_id: int, steps: list[dict[str, Any]]
) -> StepRef | None:
    names = {title}
    if title.startswith("Run "):
        names.add(title.removeprefix("Run "))
    matches = [s for s in steps if s.get("name") in names]
    if len(matches) != 1:
        return None
    return StepRef(job_id, int(matches[0]["number"]))


def _split_blocks(log_text: str) -> list[tuple[str | None, list[str]]]:
    """Blocks of normalised lines: ``(group title | None, lines)``."""
    blocks: list[tuple[str | None, list[str]]] = []
    title: str | None = None
    current: list[str] = []

    def flush() -> None:
        if any(line.strip() for line in current):
            blocks.append((title, current))

    for raw in log_text.splitlines():
        line = _LOG_TIMESTAMP_RE.sub("", raw, count=1)
        line = _ANSI_RE.sub("", line)
        if line.startswith(_GROUP_PREFIX):
            flush()
            title = line.removeprefix(_GROUP_PREFIX)
            current = [line]
        elif line == _ENDGROUP:
            current.append(line)
            flush()
            title, current = None, []
        else:
            current.append(line)
    flush()
    return blocks
