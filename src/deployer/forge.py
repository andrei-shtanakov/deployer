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

ARCHIVE_TIMEOUT_S = 120.0
"""Wall-clock budget for downloading one source archive."""

DEFAULT_MAX_ARCHIVE_MB = 200
"""Default ``--max-archive-mb`` cap (spec §1.4) on a fetched source archive."""

SNAPSHOT_SCHEMA_VERSION = "1.3"

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
    """A block of text, where it came from, and the level it was filed under.

    ``source`` is a step, a job id (job-level evidence such as an annotation),
    or ``None`` when the API gave no binding and none was invented.

    ``level`` is a GitHub annotation's raw ``annotation_level``
    (``failure``/``warning``/``notice``/...), and ``None`` for anything read
    out of a log, which has no level of its own.

    Both are DATA beside the text, never rendered into it. ``text`` is the
    message verbatim, exactly as GitHub gave it: this module reports facts
    and ``diagnose.py`` matches its rules against them, so anything forge
    wrote into the text would be one more thing every text rule has to see
    through. It was: the level used to be a ``"<level>: "`` prefix, which an
    empty first line pushed out of reach, and which stood between a rule and
    the exception line that rule must skip. A readable rendering of the level
    belongs where a human reads it, not in the evidence.
    """

    source: StepRef | int | None
    text: str
    level: str | None = None


@dataclass(frozen=True)
class FailedStep:
    """A non-green step of a kept job."""

    ref: StepRef
    name: str
    conclusion: str
    evidence: list[Evidence]


@dataclass(frozen=True)
class StepInfo:
    """One step of a job as the jobs listing gives it, green or not.

    Reproduction binds workflow steps to these one-to-one (spec §1.2 #5); the
    reading layer keeps using ``FailedJob.steps``, which holds only the
    non-green ones.
    """

    number: int
    name: str
    conclusion: str | None


@dataclass(frozen=True)
class Completeness:
    """Per-source collection state: three distinct states, not a boolean.

    Recorded twice over: on each :class:`FailedJob`, for how that one job
    was read, and once on the run as the worst-of aggregate over the kept
    jobs. With zero kept jobs nothing was fetched, so the run's states read
    ``unavailable``/``absent``; check ``jobs`` before reading them as
    "logs expired".
    """

    logs: LogsState
    annotations: AnnotationsState


COMPLETE_BY_CONSTRUCTION = Completeness(logs="present", annotations="absent")
"""What a job built by hand — a test, a fixture — asserts about its own read.

A job assembled in memory has no failed fetch behind it, and a stored
snapshot from schema 1.0 says nothing per job; both read as "the log is
here, there were no annotations", which is what 1.0's whole-run
``completeness`` already implied for every job it kept.
"""


@dataclass(frozen=True)
class FailedJob:
    """A non-green job with its kept steps, job-level evidence and read state.

    ``completeness`` is this job's own: a sibling whose log could not be
    fetched says nothing about this one. The run's worst-of aggregate lives
    on :class:`FailedRun`.
    """

    job_id: int
    name: str
    conclusion: str
    steps: list[FailedStep]
    evidence: list[Evidence]
    completeness: Completeness = COMPLETE_BY_CONSTRUCTION
    all_steps: list[StepInfo] | None = None


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
    workflow_ref_path: str | None = None
    workflow_path: str | None = None
    event: str | None = None
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class AdapterRefusal:
    """The adapter declined to snapshot: the run is unfinished or not failed."""

    reason: Literal["not_finished", "not_failed"]
    detail: str


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a recursive Git tree listing, as GitHub returns it."""

    path: str
    mode: str
    type: str
    sha: str


@dataclass(frozen=True)
class TreeListing:
    """The Git tree at a commit; ``truncated`` is GitHub's own flag."""

    sha: str
    entries: list[TreeEntry]
    truncated: bool


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


class GhBytesRunner(GhRunner, Protocol):
    """A runner that can also return raw bytes (the tarball endpoint)."""

    def api_bytes(self, argv: list[str], *, timeout: float) -> bytes:
        """Return stdout bytes; raise :class:`GhError` on failure."""
        ...


def _gh_failure(what: str, returncode: int, stderr: str) -> GhError:
    """Map a nonzero ``gh api`` exit to a :class:`GhError`, HTTP status if any."""
    stderr = stderr.strip()
    match = _HTTP_STATUS_RE.search(stderr)
    status = int(match.group(1)) if match else None
    return GhError(
        f"gh api {what} failed: {stderr or f'exit code {returncode}'}", status
    )


def _gh_env() -> dict[str, str]:
    """The environment ``gh api`` runs under: prompts and update checks off."""
    return {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}


class SubprocessGh:
    """The real runner: ``gh api`` as an argument vector, never a shell."""

    def api(self, argv: list[str], *, timeout: float) -> str:
        """Run ``gh api *argv`` under ``timeout`` with prompts disabled."""
        cmd = ["gh", "api", *argv]
        what = " ".join(argv)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
                env=_gh_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh api {what} timed out after {timeout}s") from exc
        except OSError as exc:
            raise GhError(f"gh api could not start: {exc}") from exc
        if proc.returncode != 0:
            raise _gh_failure(what, proc.returncode, proc.stderr or "")
        return proc.stdout

    def api_bytes(self, argv: list[str], *, timeout: float) -> bytes:
        """``gh api *argv`` returning raw stdout bytes (archives, not text).

        ``gh``'s HTTP client follows the tarball endpoint's redirect. Same
        timeout and status mapping as :meth:`api`.
        """
        cmd = ["gh", "api", *argv]
        what = " ".join(argv)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
                env=_gh_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh api {what} timed out after {timeout}s") from exc
        except OSError as exc:
            raise GhError(f"gh api could not start: {exc}") from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise _gh_failure(what, proc.returncode, stderr)
        return proc.stdout


_run_adapter: TypeAdapter[FailedRun] = TypeAdapter(FailedRun)


def dump_snapshot(run: FailedRun) -> str:
    """Serialize a snapshot as versioned JSON (``snapshot_schema_version``).

    Schema 1.3 adds ``workflow_ref_path``, ``workflow_path``, ``event`` on
    the run and ``all_steps`` on each job; additive like 1.1 and 1.2, so
    older documents still load with them ``None``.
    """
    return _run_adapter.dump_json(run, indent=2).decode()


def load_snapshot(text: str) -> FailedRun:
    """Parse JSON written by :func:`dump_snapshot`.

    Evidence stored before 1.2 has no ``level`` and reads back as ``None``:
    level-less, which is what a log block is.
    """
    return _run_adapter.validate_json(text)


def fetch_failed_run(
    ref: RunRef, *, attempt: int | None, runner: GhRunner | None = None
) -> FailedRun | AdapterRefusal:
    """Snapshot a failed run, or refuse if it is unfinished or not failed.

    One run-metadata call decides both the refusal and the attempt; the
    attempt is fixed once, before any jobs or logs are read, so a re-run
    never mixes evidence from different attempts. A GhError on the run or
    jobs calls propagates — including :meth:`_Gh.jobs` refusing a listing
    that ended short of ``total_count``. Per-job log and annotation failures follow the
    same rule as :meth:`_Gh.logs`/:meth:`_Gh.annotations`: an HTTP-status
    ``GhError`` is data about the run and is recorded in ``Completeness``;
    a status-less ``GhError`` (timeout, ``gh`` could not start, an
    unparseable failure) means ``gh`` itself never reached GitHub and
    propagates instead.
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
    for record in kept:
        job_id = int(record["id"])
        log_text, logs_state = gh.logs(job_id)
        annotations, annotations_state = gh.annotations(job_id)
        jobs.append(
            _build_job(
                record,
                job_id,
                log_text,
                annotations,
                Completeness(logs=logs_state, annotations=annotations_state),
            )
        )
    raw_path = run.get("path")
    ref_path = str(raw_path) if raw_path is not None else None
    raw_event = run.get("event")
    return FailedRun(
        repo=ref.repo,
        run_id=ref.run_id,
        attempt=resolved,
        head_sha=str(run.get("head_sha", "")),
        url=str(run.get("html_url", "")),
        jobs=jobs,
        completeness=Completeness(
            logs=_worst([j.completeness.logs for j in jobs], _LOGS_RANK, "unavailable"),
            annotations=_worst(
                [j.completeness.annotations for j in jobs], _ANNOTATIONS_RANK, "absent"
            ),
        ),
        workflow_ref_path=ref_path,
        workflow_path=normalise_workflow_path(ref_path) if ref_path else None,
        event=str(raw_event) if raw_event is not None else None,
    )


def fetch_tree_listing(repo: str, sha: str, runner: GhRunner) -> TreeListing:
    """The recursive Git tree at ``sha`` (spec §1.3 b, c)."""
    body = json.loads(
        runner.api([f"repos/{repo}/git/trees/{sha}?recursive=1"], timeout=GH_TIMEOUT_S)
    )
    entries = [
        TreeEntry(str(e["path"]), str(e["mode"]), str(e["type"]), str(e["sha"]))
        for e in body.get("tree", [])
    ]
    return TreeListing(str(body.get("sha", sha)), entries, bool(body.get("truncated")))


def fetch_archive(
    repo: str, sha: str, runner: GhBytesRunner, *, max_bytes: int
) -> bytes:
    """The source tarball of ``sha``; bytes pass through unaltered (spec §1.4).

    Over ``max_bytes`` is refused as a status-less :class:`GhError`: the
    download happened, but this layer will not unpack it. The cap is
    enforced only after ``api_bytes`` returns — the whole archive is
    downloaded and buffered in memory first, whatever its size, and only
    then measured against ``max_bytes``; this does not stream or abort the
    download early.
    """
    blob = runner.api_bytes([f"repos/{repo}/tarball/{sha}"], timeout=ARCHIVE_TIMEOUT_S)
    if len(blob) > max_bytes:
        raise GhError(f"archive exceeds {max_bytes} bytes ({len(blob)})", None)
    return blob


def _run_path(run_id: int, attempt: int | None) -> str:
    base = f"actions/runs/{run_id}"
    return base if attempt is None else f"{base}/attempts/{attempt}"


def normalise_workflow_path(raw: str) -> str:
    """The run's ``path`` without a trailing ``@<ref>`` (split on the last ``@``).

    GitHub documents values such as ``.github/workflows/build.yml@main``; the
    file in the tree is the part before the ref. Validation of the result is
    the reproduction layer's job (spec §1.1), not the snapshot's.
    """
    head, sep, _ = raw.rpartition("@")
    return head if sep else raw


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
        """All jobs of one attempt, paginated until ``total_count`` is met.

        A listing that runs out of pages before the count is met is an
        adapter error, not a shorter run: spec §2.1 forbids presenting a
        partially collected snapshot as complete, and a snapshot missing a
        job can be diagnosed all the way to ``CLASSIFIED`` (exit 0) over a
        run whose other job was never read. The raised :class:`GhError` has
        no HTTP status, so it propagates like any broken instrument.

        That reading also covers a ``total_count`` that merely lies high:
        the adapter cannot tell an inflated count from a page that went
        missing, and only one of the two is safe to assume, so it refuses
        either way. Meeting the count — not exhausting the pages — is what
        ends the loop, and the empty page still ends it rather than looping.

        Each page's shape is validated before it is trusted: ``jobs`` must
        be a list and ``total_count`` an int, else the page is malformed —
        without this, a page missing both (``{}``) reads as ``total =
        len(collected)``, i.e. "done", turning a broken response into a
        falsely complete listing. The count is fixed from the first page;
        a later page claiming a different ``total_count`` is inconsistent,
        not a correction, since the adapter has no way to tell which of the
        two counts (if either) is the true one.
        """
        base = f"actions/runs/{run_id}/attempts/{attempt}/jobs"
        collected: list[dict[str, Any]] = []
        total: int | None = None
        page = 1
        while True:
            body = self.json(f"{base}?per_page={_PER_PAGE}&page={page}")
            page_jobs = body.get("jobs")
            page_total = body.get("total_count")
            if not isinstance(page_jobs, list) or not isinstance(page_total, int):
                raise GhError(
                    f"jobs listing malformed: page {page} has "
                    f"jobs={page_jobs!r} total_count={page_total!r}",
                    None,
                )
            if total is None:
                total = page_total
            elif page_total != total:
                raise GhError(
                    f"jobs listing inconsistent: total_count {total} then {page_total}",
                    None,
                )
            collected.extend(page_jobs)
            if len(collected) >= total:
                return collected
            if not page_jobs:
                raise GhError(
                    f"jobs listing incomplete: {len(collected)} of {total}", None
                )
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


def _level_of(annotation: dict[str, Any]) -> str | None:
    """An annotation's raw ``annotation_level``, or ``None`` if it carries none."""
    level = annotation.get("annotation_level")
    return None if level is None else str(level)


def _build_job(
    record: dict[str, Any],
    job_id: int,
    log_text: str,
    annotations: list[dict[str, Any]],
    completeness: Completeness,
) -> FailedJob:
    all_steps = list(record.get("steps") or [])
    step_infos = [
        StepInfo(
            number=int(s["number"]),
            name=str(s.get("name", "")),
            conclusion=None if s.get("conclusion") is None else str(s["conclusion"]),
        )
        for s in all_steps
    ]
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
        Evidence(source=job_id, text=str(a.get("message")), level=_level_of(a))
        for a in annotations
    )
    return FailedJob(
        job_id=job_id,
        name=str(record.get("name", "")),
        conclusion=str(record.get("conclusion")),
        steps=steps,
        evidence=job_evidence,
        completeness=completeness,
        all_steps=step_infos,
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
