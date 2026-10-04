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
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import groupby
from typing import IO, Any, Literal, Protocol, TypeGuard, runtime_checkable

from pydantic import TypeAdapter

from deployer.logarchive import ArchiveLimits, ArchiveRefused, read_step_directories
from deployer.stepbinding import JobBinding, StepBindingState, StepSpan, bind_jobs

GH_TIMEOUT_S = 30.0
"""Wall-clock budget for one ``gh api`` invocation."""

ARCHIVE_TIMEOUT_S = 120.0
"""Wall-clock budget for downloading one source archive."""

DEFAULT_MAX_ARCHIVE_MB = 200
"""Default ``--max-archive-mb`` cap (spec §1.4) on a fetched source archive."""

LOG_ARCHIVE_TIMEOUT_S = 120.0
"""Wall-clock budget for downloading one per-attempt log archive."""

SNAPSHOT_SCHEMA_VERSION = "1.5"

_PER_PAGE = 100
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})
_GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")
RUNNER_LOG_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?"
)
"""The ISO-8601 UTC timestamp (and one space) a runner prefixes to each log line."""
# The runner colours some lines (e.g. echoing the step command) with ANSI CSI
# sequences; stripping is mechanical framing removal, not interpretation.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
"""An ANSI CSI escape sequence (colour and cursor control) in a runner log."""
RUNNER_GROUP_PREFIX = "##[group]"
"""The runner's marker that opens a collapsible log group; the title follows it."""
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


ArchiveState = Literal["available", "absent", "unavailable", "refused"]


@dataclass(frozen=True)
class ArchiveStatus:
    """How the per-attempt log archive was read (snapshot 1.5, spec §6).

    ``None`` on a run means no archive was attempted: an older snapshot, or a
    runner without the capped download.
    """

    state: ArchiveState
    reason: str | None = None


@dataclass(frozen=True)
class StepBinding:
    """What step binding decided for one job (snapshot 1.5, spec §6)."""

    state: StepBindingState
    reason: str | None = None


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
    step_binding: StepBinding | None = None


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
    archive: ArchiveStatus | None = None
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class RunSummary:
    """One row of the runs listing for a commit (spec §7.1).

    ``attempts`` is the run's ``run_attempt``: the attempts ``1..attempts``
    exist, whether or not each is completed. ``path`` is the workflow path
    normalised by :func:`normalise_workflow_path` (no ``@<ref>``), the form
    reproduction binds; ``head_branch`` is ``None`` when GitHub gives none.
    """

    run_id: int
    attempts: int
    event: str
    head_sha: str
    path: str
    head_branch: str | None


@dataclass(frozen=True)
class AttemptRead:
    """One attempt of a run of any outcome, read as far as it could be.

    ``status``/``conclusion`` are the attempt's own; ``status`` is
    ``"unknown"`` when its metadata could not be read. ``jobs`` holds every
    job of the attempt (no green filter), each built like a failed-run job;
    it is ``None`` when the attempt is not ``completed`` (nothing was read
    past its metadata) or when ``error`` is set. ``logs_state`` is per job
    id. ``error`` records any ``GhError`` — HTTP status or none — and any
    malformed or unparseable response; it is never raised. ``logs`` maps a
    job id to the exact log text read for it (the text ``build_failed_job`` was
    given); a job whose log was not read (``logs_state`` other than
    ``"present"``) has no key, so a consumer never mistakes an absent log
    for an empty one.

    Annotations are not read: each job's ``completeness.annotations`` is
    ``"absent"`` by construction and says nothing about GitHub.
    """

    run: RunSummary
    attempt: int
    status: str
    conclusion: str | None
    jobs: list[FailedJob] | None
    logs_state: dict[int, LogsState]
    error: str | None
    logs: dict[int, str] = field(default_factory=dict)


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


class GhTimeout(GhError):
    """A ``gh api`` call hit its deadline, as opposed to other status-less failures."""


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


@dataclass(frozen=True)
class OverCap:
    """``api_bytes_capped`` stopped reading: more than ``max_bytes`` arrived."""

    max_bytes: int


@runtime_checkable
class GhCappedBytesRunner(GhRunner, Protocol):
    """A runner whose binary download stops at a byte cap (spec §7.2)."""

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        """Stdout bytes, or :class:`OverCap`; raise :class:`GhError` on failure."""
        ...


STDERR_TAIL_BYTES = 64 * 1024
"""How much of a capped call's stderr is kept: its tail, for the error message."""
_CAPPED_CHUNK = 64 * 1024
_TERMINATE_GRACE_S = 2.0
_READER_JOIN_S = 5.0
_POLL_S = 0.05


class _StdoutReader(threading.Thread):
    """Reads stdout in chunks until EOF or until more than ``max_bytes`` arrived.

    The thread owns its pipe and closes it when it ends, so no other thread
    can close the descriptor while a read on it is still in flight.
    """

    def __init__(self, pipe: IO[bytes], max_bytes: int) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._max = max_bytes
        self.chunks: list[bytes] = []
        self.size = 0
        self.over = threading.Event()
        self.error: Exception | None = None

    def run(self) -> None:
        """Append chunks until EOF or the cap; never decide anything."""
        try:
            fd = self._pipe.fileno()
            while chunk := os.read(fd, _CAPPED_CHUNK):
                self.size += len(chunk)
                self.chunks.append(chunk)  # at most the cap plus one chunk
                if self.size > self._max:
                    self.over.set()
                    return
        except Exception as exc:  # surfaced by the caller, never swallowed
            self.error = exc
        finally:
            self._pipe.close()


class _StderrTail(threading.Thread):
    """Drains stderr to EOF, keeping only its last ``STDERR_TAIL_BYTES``.

    Like :class:`_StdoutReader`, the thread owns and closes its pipe.
    """

    def __init__(self, pipe: IO[bytes]) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self.tail = bytearray()
        self.error: Exception | None = None

    def run(self) -> None:
        """Drain to EOF, trimming to the tail after every chunk."""
        try:
            fd = self._pipe.fileno()
            while chunk := os.read(fd, _CAPPED_CHUNK):
                self.tail += chunk
                del self.tail[:-STDERR_TAIL_BYTES]
        except Exception as exc:  # surfaced by the caller, never swallowed
            self.error = exc
        finally:
            self._pipe.close()


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


def _await_exit(
    proc: subprocess.Popen[bytes], over: threading.Event, deadline: float
) -> Literal["exited", "over", "deadline"]:
    """Wait for the child, an exceeded cap or the deadline, whichever is first.

    Never reads a pipe: the readers do, so no blocking read holds off the
    deadline.
    """
    while True:
        if over.is_set():
            return "over"
        left = deadline - time.monotonic()
        if left <= 0:
            return "deadline"
        try:
            proc.wait(timeout=min(left, _POLL_S))
            return "exited"
        except subprocess.TimeoutExpired:
            continue


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Terminate, wait a grace period, then kill; always reap.

    ``Popen`` signals only a child it has not reaped yet, so a child that
    exited meanwhile is never confused with a reused pid.
    """
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class SubprocessGh:
    """The real runner: ``gh api`` as an argument vector, never a shell."""

    def __init__(self, command: Sequence[str] = ("gh",)) -> None:
        """``command`` is the program run before ``api`` (``gh`` in production)."""
        self._command = tuple(command)

    def api(self, argv: list[str], *, timeout: float) -> str:
        """Run ``gh api *argv`` under ``timeout`` with prompts disabled."""
        cmd = [*self._command, "api", *argv]
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
        cmd = [*self._command, "api", *argv]
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

    def api_bytes_capped(
        self, argv: list[str], *, timeout: float, max_bytes: int
    ) -> bytes | OverCap:
        """``gh api *argv`` stdout, stopping once more than ``max_bytes`` arrived.

        The whole lifecycle is owned here (spec §7.2): two reader threads, one
        deadline in the calling thread, terminate → grace → kill → reap, and
        both readers finished before any result is chosen, so a child that
        exits before its last bytes are read can neither hide an exceeded cap
        nor return partial output.
        """
        cmd = [*self._command, "api", *argv]
        what = " ".join(argv)
        deadline = time.monotonic() + timeout
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_gh_env(),
            )
        except OSError as exc:
            raise GhError(f"gh api could not start: {exc}") from exc
        assert proc.stdout is not None and proc.stderr is not None
        out = _StdoutReader(proc.stdout, max_bytes)
        err = _StderrTail(proc.stderr)
        try:
            out.start()
            err.start()
            ended = _await_exit(proc, out.over, deadline)
        finally:
            if proc.returncode is None:
                _stop(proc)
            for pipe, reader in ((proc.stdout, out), (proc.stderr, err)):
                if reader.ident is None:  # never started: nobody else closes it
                    pipe.close()
        returncode = proc.wait()  # already reaped: returns at once
        for reader in (out, err):
            reader.join(_READER_JOIN_S)
        return _capped_result(what, timeout, max_bytes, returncode, ended, out, err)


def _capped_result(
    what: str,
    timeout: float,
    max_bytes: int,
    returncode: int,
    ended: Literal["exited", "over", "deadline"],
    out: _StdoutReader,
    err: _StderrTail,
) -> bytes | OverCap:
    """Choose the result of a reaped child once both readers have finished."""
    if out.is_alive() or err.is_alive():
        raise GhError(f"gh api {what}: an output reader did not finish")
    for failure in (out.error, err.error):
        if failure is not None:
            raise GhError(f"gh api {what}: reading output failed: {failure}")
    if out.over.is_set():
        return OverCap(max_bytes)
    if ended == "deadline":
        raise GhTimeout(f"gh api {what} timed out after {timeout}s")
    if returncode != 0:
        stderr = bytes(err.tail).decode("utf-8", errors="replace")
        raise _gh_failure(what, returncode, stderr)
    return b"".join(out.chunks)


_run_adapter: TypeAdapter[FailedRun] = TypeAdapter(FailedRun)


def dump_snapshot(run: FailedRun) -> str:
    """Serialize a snapshot as versioned JSON (``snapshot_schema_version``).

    Schema 1.3 adds ``workflow_ref_path``, ``workflow_path``, ``event`` on
    the run and ``all_steps`` on each job; additive like 1.1 and 1.2, so
    older documents still load with them ``None``. Schema 1.4 changes no
    field, only attribution: job-log group headers are no ground for a step
    binding, so a 1.4 snapshot's log blocks are all ``source=None``. An older
    document keeps the sources it recorded; nothing rebinds them on load.
    Schema 1.5 adds the run's ``archive`` and each job's ``step_binding``
    (spec §6); additive, so a 1.4 or older document loads with both ``None``,
    which means not attempted.
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
    runner = runner if runner is not None else SubprocessGh()
    gh = _Gh(runner, ref.repo)
    run = gh.json(_run_path(ref.run_id, attempt))
    refusal = _refuse(run)
    if refusal is not None:
        return refusal
    resolved = attempt if attempt is not None else int(run["run_attempt"])
    listing = gh.jobs(ref.run_id, resolved)
    kept = [job for job in listing if job.get("conclusion") not in _GREEN_CONCLUSIONS]
    reads: dict[int, tuple[str, LogsState]] = {}
    noted: dict[int, tuple[list[dict[str, Any]], AnnotationsState]] = {}
    for record in kept:
        job_id = int(record["id"])
        reads[job_id] = gh.logs(job_id)
        noted[job_id] = gh.annotations(job_id)
    archive, bindings = _bind_steps(runner, gh, ref, resolved, listing, kept, reads)
    jobs: list[FailedJob] = []
    for record in kept:
        job_id = int(record["id"])
        log_text, logs_state = reads[job_id]
        annotations, annotations_state = noted[job_id]
        binding = bindings.get(job_id)
        jobs.append(
            build_failed_job(
                record,
                job_id,
                log_text,
                annotations,
                Completeness(logs=logs_state, annotations=annotations_state),
                spans=binding.spans if binding is not None else (),
                step_binding=(
                    StepBinding(binding.state, binding.reason)
                    if binding is not None
                    else None
                ),
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
        archive=archive,
    )


def _bind_steps(
    runner: GhRunner,
    gh: "_Gh",
    ref: RunRef,
    attempt: int,
    listing: list[dict[str, Any]],
    kept: list[dict[str, Any]],
    reads: dict[int, tuple[str, LogsState]],
) -> tuple[ArchiveStatus | None, dict[int, JobBinding]]:
    """The archive's state and each kept job's binding (spec §4, §6, §7).

    A runner without the capped download attempts nothing. Only an
    ``available`` archive costs the population's extra log reads (§4.1).
    """
    if not isinstance(runner, GhCappedBytesRunner) or not reads:
        return None, {}
    limits = ArchiveLimits()
    path = f"repos/{ref.repo}/actions/runs/{ref.run_id}/attempts/{attempt}/logs"
    try:
        blob = runner.api_bytes_capped(
            [path], timeout=LOG_ARCHIVE_TIMEOUT_S, max_bytes=limits.download
        )
    except GhTimeout as exc:
        reason = f"the download timed out: {exc}"
        return ArchiveStatus("unavailable", reason), _each(
            reads,
            "no_archive",
            "the log archive is unavailable: the download timed out",
        )
    except GhError as exc:
        if exc.status is None:
            raise
        message = re.sub(r"\s*\(HTTP \d+\)\s*$", "", str(exc))
        unavailable = ArchiveStatus("unavailable", f"HTTP {exc.status}: {message}")
        return unavailable, _each(reads, "no_archive", "the log archive is unavailable")
    if isinstance(blob, OverCap):
        refused = ArchiveStatus(
            "refused", f"the download exceeds {blob.max_bytes} bytes"
        )
        return refused, _each(reads, "no_archive", "the log archive was refused")
    found = read_step_directories(blob, limits)
    if isinstance(found, ArchiveRefused):
        return ArchiveStatus("refused", found.reason), _each(
            reads, "no_archive", "the log archive was refused"
        )
    if not found:
        return ArchiveStatus("absent", "the archive holds no per-step files"), _each(
            reads, "no_archive", "the log archive holds no per-step files"
        )
    population = [r for r in listing if r.get("conclusion") != "skipped"]
    for index, record in enumerate(listing):
        if record.get("conclusion") == "skipped":
            continue
        defect = _job_record_defect(record)
        if defect is None:
            continue
        if any(record is k for k in kept):
            _check_job_record(record)
        reason = (
            f"the record of green job {record.get('id')!r} "
            f"(listing index {index}) is malformed: {defect}"
        )
        return ArchiveStatus("available"), _each(reads, "unverifiable", reason)
    logs: dict[int, str] = {}
    for job_id, (text, state) in reads.items():
        if state != "present":
            return ArchiveStatus("available"), _each(
                reads, "unverifiable", f"the log of job {job_id} is {state}"
            )
        logs[job_id] = text
    for record in population:
        job_id = int(record["id"])
        if job_id in reads:
            continue
        text, state = gh.logs(job_id)
        if state != "present":
            return ArchiveStatus("available"), _each(
                reads, "unverifiable", f"the log of job {job_id} is {state}"
            )
        logs[job_id] = text
    numbers = {
        int(r["id"]): [int(s["number"]) for s in r.get("steps") or []]
        for r in population
    }
    bound = bind_jobs(logs, numbers, found)
    return ArchiveStatus("available"), {job_id: bound[job_id] for job_id in reads}


def _each(
    reads: dict[int, tuple[str, LogsState]], state: StepBindingState, reason: str
) -> dict[int, JobBinding]:
    """The same binding state for every kept job."""
    return {job_id: JobBinding(state, reason) for job_id in reads}


def list_runs_for_sha(repo: str, sha: str, runner: GhRunner) -> list[RunSummary] | str:
    """Every workflow run of commit ``sha``, or why the listing is not whole.

    Paginates ``actions/runs?head_sha=`` under the same ``total_count`` rule
    as the jobs listing: a short, malformed or inconsistent listing — and a
    row that is malformed or names another commit — is returned as a reason
    string, never as a shorter list (an absent run could hide a
    contradiction, spec §7.4). A ``run_id`` listed twice is a reason too
    (a page shifted between reads can repeat one run and hide another), never
    de-duplicated. A ``GhError`` of any kind and an unparseable
    response are reasons too: nothing but a programming error raises.
    """
    gh = _Gh(runner, repo)
    try:
        rows = gh.paged(f"actions/runs?head_sha={sha}", "workflow_runs", "runs")
    except GhError as exc:
        return str(exc)
    except ValueError as exc:
        return f"runs listing unparseable: {exc}"
    runs: list[RunSummary] = []
    for row in rows:
        summary = _run_summary(row)
        if summary is None:
            return f"runs listing malformed: bad row {row!r}"
        if summary.head_sha != sha:
            return (
                f"runs listing inconsistent: run {summary.run_id} "
                f"has head_sha {summary.head_sha}, not {sha}"
            )
        if any(r.run_id == summary.run_id for r in runs):
            return f"runs listing incomplete: run {summary.run_id} listed twice"
        runs.append(summary)
    return runs


def _run_summary(row: object) -> RunSummary | None:
    """A listing row as a :class:`RunSummary`, or ``None`` if it is malformed."""
    if not isinstance(row, dict):
        return None
    run_id, attempts = row.get("id"), row.get("run_attempt")
    event, head_sha, path = row.get("event"), row.get("head_sha"), row.get("path")
    branch = row.get("head_branch")
    if not (_is_int(run_id) and _is_int(attempts)):
        return None
    if not (
        isinstance(event, str) and isinstance(head_sha, str) and isinstance(path, str)
    ):
        return None
    if branch is not None and not isinstance(branch, str):
        return None
    return RunSummary(
        run_id=run_id,
        attempts=attempts,
        event=event,
        head_sha=head_sha,
        path=normalise_workflow_path(path),
        head_branch=branch,
    )


def _is_int(value: object) -> TypeGuard[int]:
    """An int that is not a bool (JSON ``true`` must not pass as an id)."""
    return isinstance(value, int) and not isinstance(value, bool)


def read_attempt(
    repo: str, run: RunSummary, attempt: int, runner: GhRunner
) -> AttemptRead:
    """Read one attempt of ``run`` whatever its outcome (spec §7.1).

    The attempt's metadata fixes ``status``/``conclusion``; a completed
    attempt then has **all** its jobs read (the jobs listing's completeness
    rule applies) and each job's full log, recorded per job in
    ``logs_state`` with :meth:`_Gh.logs`' semantics (410 ``unavailable``,
    another status ``error``). An attempt that is not completed reads no
    jobs. Every failure — a ``GhError`` with or without a status, an
    incomplete listing, a malformed or unparseable response — lands in
    ``error`` with ``jobs=None``; nothing but a programming error raises.
    """
    gh = _Gh(runner, repo)
    try:
        meta = gh.json(_run_path(run.run_id, attempt))
    except GhError as exc:
        return _attempt_error(run, attempt, "unknown", None, str(exc))
    except ValueError as exc:
        return _attempt_error(
            run, attempt, "unknown", None, f"attempt metadata unparseable: {exc}"
        )
    identity = _attempt_identity_error(meta, run, attempt)
    if identity is not None:
        return _attempt_error(run, attempt, "unknown", None, identity)
    status = meta.get("status")
    conclusion = meta.get("conclusion")
    if not isinstance(status, str) or not (
        conclusion is None or isinstance(conclusion, str)
    ):
        return _attempt_error(
            run,
            attempt,
            "unknown",
            None,
            f"attempt metadata malformed: status={status!r} conclusion={conclusion!r}",
        )
    if status != "completed":
        return AttemptRead(run, attempt, status, conclusion, None, {}, None)
    try:
        jobs, logs_state, logs = _read_all_jobs(gh, run.run_id, attempt)
    except GhError as exc:
        return _attempt_error(run, attempt, status, conclusion, str(exc))
    except ValueError as exc:
        return _attempt_error(
            run, attempt, status, conclusion, f"jobs listing unparseable: {exc}"
        )
    return AttemptRead(run, attempt, status, conclusion, jobs, logs_state, None, logs)


def _attempt_identity_error(meta: object, run: RunSummary, attempt: int) -> str | None:
    """Why the metadata is not attempt ``attempt`` of ``run``, or ``None``.

    ``head_sha`` must equal the listed run's, ``run_attempt`` the requested
    attempt, and ``id`` — only when present — the run id. A missing value or
    a wrong type is malformed, a different value a mismatch; either names the
    field, and neither is ever accepted silently.
    """
    if not isinstance(meta, dict):
        return f"attempt metadata malformed: not an object ({type(meta).__name__})"
    head_sha, run_attempt = meta.get("head_sha"), meta.get("run_attempt")
    if not isinstance(head_sha, str):
        return f"attempt metadata malformed: head_sha={head_sha!r}"
    if head_sha != run.head_sha:
        return f"attempt metadata mismatch: head_sha {head_sha}, not {run.head_sha}"
    if not _is_int(run_attempt):
        return f"attempt metadata malformed: run_attempt={run_attempt!r}"
    if run_attempt != attempt:
        return f"attempt metadata mismatch: run_attempt {run_attempt}, not {attempt}"
    if "id" not in meta:
        return None
    run_id = meta["id"]
    if not _is_int(run_id):
        return f"attempt metadata malformed: id={run_id!r}"
    if run_id != run.run_id:
        return f"attempt metadata mismatch: id {run_id}, not {run.run_id}"
    return None


def _attempt_error(
    run: RunSummary, attempt: int, status: str, conclusion: str | None, error: str
) -> AttemptRead:
    return AttemptRead(run, attempt, status, conclusion, None, {}, error)


def _read_all_jobs(
    gh: "_Gh", run_id: int, attempt: int
) -> tuple[list[FailedJob], dict[int, LogsState], dict[int, str]]:
    """Every job of the attempt with its log state and, when read, its log
    text; raises ``GhError`` on failure.

    A malformed job record (no int ``id``, a step without an int ``number``)
    is raised as a status-less ``GhError`` before its log is fetched.
    """
    jobs: list[FailedJob] = []
    states: dict[int, LogsState] = {}
    texts: dict[int, str] = {}
    for record in gh.jobs(run_id, attempt):
        _check_job_record(record)
        job_id = int(record["id"])
        log_text, state = gh.logs(job_id)
        states[job_id] = state
        if state == "present":
            texts[job_id] = log_text
        jobs.append(
            build_failed_job(
                record, job_id, log_text, [], Completeness(state, "absent")
            )
        )
    return jobs, states, texts


def _job_record_defect(record: object) -> str | None:
    """Why ``build_failed_job`` could not read the record without guessing."""
    if not isinstance(record, dict):
        return "not an object"
    if not _is_int(record.get("id")):
        return "`id` is not an int"
    steps = record.get("steps")
    if steps is not None and not isinstance(steps, list):
        return "`steps` is not a list"
    if not all(isinstance(s, dict) and _is_int(s.get("number")) for s in steps or []):
        return "a step without an int `number`"
    return None


def _check_job_record(record: object) -> None:
    """Refuse a job record ``build_failed_job`` could not read without guessing."""
    if _job_record_defect(record) is not None:
        raise GhError(f"job record malformed: {record!r}", None)


def fetch_tree_listing(repo: str, sha: str, runner: GhRunner) -> TreeListing:
    """The recursive Git tree at ``sha`` (spec §1.3 b, c).

    Every field is required and type-checked: a response missing ``sha``,
    ``tree`` or a boolean ``truncated``, or an entry missing one of its four
    string fields, is refused as a status-less :class:`GhError` — an unknown
    completeness must never read as a confirmed ``truncated=False``.
    """
    body = json.loads(
        runner.api([f"repos/{repo}/git/trees/{sha}?recursive=1"], timeout=GH_TIMEOUT_S)
    )
    if not isinstance(body, dict):
        raise GhError("tree listing malformed: not an object", None)
    tree, listed_sha, truncated = (
        body.get("tree"),
        body.get("sha"),
        body.get("truncated"),
    )
    if not isinstance(tree, list) or not isinstance(listed_sha, str):
        raise GhError("tree listing malformed: missing tree or sha", None)
    if not isinstance(truncated, bool):
        raise GhError("tree listing malformed: truncated is not a boolean", None)
    return TreeListing(listed_sha, [_tree_entry(e) for e in tree], truncated)


def _tree_entry(entry: object) -> TreeEntry:
    """One listing entry with all four fields present as strings."""
    fields = ("path", "mode", "type", "sha")
    if not isinstance(entry, dict) or not all(
        isinstance(entry.get(f), str) for f in fields
    ):
        raise GhError(f"tree listing malformed: bad entry {entry!r}", None)
    return TreeEntry(entry["path"], entry["mode"], entry["type"], entry["sha"])


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
        return self.paged(base, "jobs", "jobs")

    def paged(self, base: str, key: str, label: str) -> list[Any]:
        """Every row of a ``total_count`` listing, or a status-less GhError.

        The completeness rule of :meth:`jobs`, shared with the runs listing:
        each page must be an object whose ``key`` is a list and whose
        ``total_count`` is an int (else ``malformed``); the count is fixed by
        the first page (a different one later is ``inconsistent``); meeting
        the count ends the loop, and an empty page before it is met — or
        more rows than the count — is ``incomplete``. ``label`` names the
        listing in the message. ``base`` may carry its own query; the page
        parameters are appended to it.
        """
        sep = "&" if "?" in base else "?"
        collected: list[Any] = []
        total: int | None = None
        page = 1
        while True:
            body = self.json(f"{base}{sep}per_page={_PER_PAGE}&page={page}")
            rows = body.get(key) if isinstance(body, dict) else None
            page_total = body.get("total_count") if isinstance(body, dict) else None
            if (
                not isinstance(rows, list)
                or not isinstance(page_total, int)
                or isinstance(page_total, bool)
            ):
                raise GhError(
                    f"{label} listing malformed: page {page} has "
                    f"{key}={rows!r} total_count={page_total!r}",
                    None,
                )
            if total is None:
                total = page_total
            elif page_total != total:
                raise GhError(
                    f"{label} listing inconsistent: "
                    f"total_count {total} then {page_total}",
                    None,
                )
            collected.extend(rows)
            if len(collected) > total:
                raise GhError(
                    f"{label} listing incomplete: {len(collected)} rows "
                    f"exceed total_count {total}",
                    None,
                )
            if len(collected) == total:
                return collected
            if not rows:
                raise GhError(
                    f"{label} listing incomplete: {len(collected)} of {total}", None
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


def build_failed_job(
    record: dict[str, Any],
    job_id: int,
    log_text: str,
    annotations: list[dict[str, Any]],
    completeness: Completeness,
    *,
    spans: Sequence[StepSpan] = (),
    step_binding: StepBinding | None = None,
) -> FailedJob:
    """A :class:`FailedJob` built from a job ``record`` and its ``log_text``.

    Only non-green steps are kept as ``steps`` (``all_steps`` keeps every
    step). Every log block and every annotation is job-level evidence; no
    step is given evidence from the job log (:func:`_log_evidence`). ``spans``
    (spec §4) bind log lines to steps; without them every log block is
    job-level.
    """
    all_steps = list(record.get("steps") or [])
    step_infos = [
        StepInfo(
            number=int(s["number"]),
            name=str(s.get("name", "")),
            conclusion=None if s.get("conclusion") is None else str(s["conclusion"]),
        )
        for s in all_steps
    ]
    steps = [
        FailedStep(
            ref=StepRef(job_id, int(step["number"])),
            name=str(step.get("name", "")),
            conclusion=str(step.get("conclusion")),
            evidence=[],
        )
        for step in all_steps
        if step.get("conclusion") not in _GREEN_CONCLUSIONS
    ]
    job_evidence = _log_evidence(log_text, job_id, spans)
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
        step_binding=step_binding,
    )


def _log_evidence(
    log_text: str, job_id: int, spans: Sequence[StepSpan] = ()
) -> list[Evidence]:
    """A job log's blocks, in log order, as evidence.

    A ``##[group]<title>`` header is no ground for binding a block to a step
    (snapshot 1.4): a step's own output can print a group whose title is
    byte-identical to the next step's runner header (recording ``steps-1``,
    job ``s5-spoof``), and a named step's header is ``Run <first script
    line>``, not its name (``s2-named``). Only ``spans`` bind: boundaries the
    per-attempt archive proved (spec §4). A block that crosses a boundary is
    cut at it; every piece keeps all its lines, blank ones included, so the
    pieces of a block join back to the block and ``job_text`` never depends on
    binding (spec §5.3). Lines outside every span stay ``source=None``.
    """
    owner = {i: span.number for span in spans for i in range(span.start, span.end)}
    evidence: list[Evidence] = []
    for block in _split_blocks(log_text):
        for number, piece in groupby(block, key=lambda item: owner.get(item[0])):
            source = None if number is None else StepRef(job_id, number)
            text = "\n".join(line for _, line in piece)
            evidence.append(Evidence(source=source, text=text))
    return evidence


def _split_blocks(log_text: str) -> list[list[tuple[int, str]]]:
    """Blocks of ``(splitlines index, normalised line)``, split at
    ``##[group]`` and after ``##[endgroup]``; a block with no non-blank line
    is dropped."""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []

    def flush() -> None:
        if any(line.strip() for _, line in current):
            blocks.append(current)

    for index, raw in enumerate(log_text.splitlines()):
        line = RUNNER_LOG_TIMESTAMP_RE.sub("", raw, count=1)
        line = ANSI_CSI_RE.sub("", line)
        if line.startswith(RUNNER_GROUP_PREFIX):
            flush()
            current = [(index, line)]
        elif line == _ENDGROUP:
            current.append((index, line))
            flush()
            current = []
        else:
            current.append((index, line))
    flush()
    return blocks
