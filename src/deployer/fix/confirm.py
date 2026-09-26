"""``deployer fix confirm`` (design §1, §7.4-§7.5, §8.2): one CI
confirmation attempt of a published fix.

Load ``fix.json``; the status must be ``fix_proposed`` or ``ci_confirmed``,
else the command refuses before any network or Git read (the refusal is
recorded in ``last_operation``, the status unchanged). The fix directory
must be writable. Then every input comes from the document, never from the
clone: the fix commit and worktree (``publication``), the workflow path and
its SHA-256, R's workflow job key and build (``input``), the repository
slug (``input.target["repo"]`` as ``fix publish`` reads it, which must
agree with ``input.origin``), the Dockerfile path
(``input.target["artifact_path"]``, as ``fix publish`` reads it), the class,
corrected text and line span (``proposal``).

The workflow and the corrected Dockerfile are read at the fix commit in the
fix worktree through ``fix.workspace``'s guarded chokepoint (``git cat-file
blob``, hooks, filters and fsmonitor off; nothing is written to the clone or
the worktree); the CI template reads the Dockerfile's bytes (§7.3). The
Dockerfile path must be the bound build's (``input.build["dockerfile"]``) and
the bytes read must hash to ``local_proof.dockerfile_sha256`` — the file CI
built is the one proved locally — else the attempt is ``qualification
undetermined`` with the reason.

The runs of the fix commit are listed; **every** attempt ``1..attempts`` of
every listed run is read, qualified (§7.2) and turned into evidence (§7.3)
with exactly the log text that read returned — nothing is fetched twice and
nothing is dropped between the listing and
:func:`deployer.fix.ci_eval.evaluate`. An incomplete listing, an
unreadable workflow or Dockerfile, or any failure is ``qualification
undetermined``.

The attempt is appended to ``ci_attempts`` (never replacing an earlier
one); the status mirrors it (``ci_confirmed`` or ``fix_proposed``); the
document is saved atomically. The whole operation, load through save,
holds ``fix.json``'s exclusive lock (:func:`deployer.fix.document.exclusive`)
so a concurrent ``confirm`` or ``publish`` cannot drop this attempt. Nothing
raises except :class:`ConfirmAbort` (exit 2): the lock is held elsewhere or
cannot be taken, ``fix.json`` could not be read or saved, or its directory is
not writable.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deployer.admission.model import DefectClass
from deployer.admission.templates import split_lines
from deployer.fix import ci_eval
from deployer.fix.ci_eval import AttemptEvidence, Outcome
from deployer.fix.document import (
    CiAttempt,
    FixDocument,
    LastOperation,
    LockError,
    check_writable,
    exclusive,
    load,
    save,
)
from deployer.fix.localproof import build_config
from deployer.fix.qualify import Qualified, qualify
from deployer.fix.workspace import _guards, _run, outside
from deployer.forge import GhRunner, RunSummary, list_runs_for_sha, read_attempt
from deployer.provenance.model import sha256_hex
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.shape import job_text

CONFIRMABLE = ("fix_proposed", "ci_confirmed")
REFUSED = "refused"
MAX_ATTEMPTS = 100
"""More attempts than this on one run are not read: the run is
``undetermined`` (a bound on the reads one confirmation makes)."""

_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class ConfirmAbort(Exception):
    """``fix.json`` could not be locked, read or saved (exit 2); the message
    names the file."""


@dataclass(frozen=True)
class _Inputs:
    """What one confirmation reads from the document (see the module
    docstring for the fields)."""

    repo: str
    fix_commit: str
    worktree: Path
    workflow_path: str
    workflow_sha256: str
    dockerfile_path: str
    dockerfile_sha256: str
    job_key: str
    build: BuildConfig
    cls: DefectClass
    corrected: str
    lines: tuple[int, int]


@dataclass(frozen=True)
class _Result:
    """The attempt's outcome, reason, per-attempt evidence and, when the
    runs could not be judged at all, why (``detail``)."""

    outcome: Outcome
    reason: str | None
    evidence: list[AttemptEvidence]
    records: list[dict[str, Any]]
    detail: str | None = None


def confirm(doc_path: Path, gh: GhRunner, clock: Callable[[], str]) -> FixDocument:
    """One confirmation attempt of the fix recorded in ``doc_path``.

    Returns the saved document. ``last_operation.result`` is the attempt's
    outcome (``ci_confirmed`` or ``ci_confirmation_insufficient``), or
    :data:`REFUSED` when the status is not published (nothing read, no
    attempt recorded). Raises :class:`ConfirmAbort` only when ``fix.json``'s
    lock is held elsewhere or cannot be taken (nothing read, the document
    untouched), ``fix.json`` cannot be read or saved, or its directory is not
    writable.
    """
    try:
        with exclusive(doc_path):
            return _confirm_locked(doc_path, gh, clock)
    except LockError as exc:
        raise ConfirmAbort(str(exc)) from exc


def _confirm_locked(
    doc_path: Path, gh: GhRunner, clock: Callable[[], str]
) -> FixDocument:
    """:func:`confirm` under the document's lock."""
    try:
        doc = load(doc_path)
    except Exception as exc:  # noqa: BLE001 — every read failure is exit 2
        raise ConfirmAbort(f"cannot read {doc_path}: {_describe(exc)}") from exc
    at = clock()
    if doc.status not in CONFIRMABLE:
        reason = f"status {doc.status} is not published"
        return _save(_operation(doc, at, REFUSED, reason), doc_path)
    reason = check_writable(doc_path.parent)
    if reason is not None:
        raise ConfirmAbort(f"cannot save {doc_path}: {reason}")
    try:
        result = _confirm(doc, doc_path, gh)
    except Exception as exc:  # noqa: BLE001 — every step is total (F §8.4)
        result = _undetermined(f"confirmation failed: {_describe(exc)}")
    return _save(_recorded(doc, at, result), doc_path)


def _confirm(doc: FixDocument, doc_path: Path, gh: GhRunner) -> _Result:
    """The inputs, the workflow at the fix commit, then the runs."""
    inputs = _inputs(doc, doc_path)
    if isinstance(inputs, str):
        return _undetermined(inputs)
    workflow = _blob(inputs, inputs.workflow_path, "workflow")
    if isinstance(workflow, str):
        return _undetermined(workflow)
    dockerfile = _blob(inputs, inputs.dockerfile_path, "Dockerfile")
    if isinstance(dockerfile, str):
        return _undetermined(dockerfile)
    if sha256_hex(dockerfile) != inputs.dockerfile_sha256:
        return _undetermined(
            f"the Dockerfile at the fix commit (sha256 {sha256_hex(dockerfile)}) "
            f"is not the locally proved one ({inputs.dockerfile_sha256})"
        )
    listed = list_runs_for_sha(inputs.repo, inputs.fix_commit, gh)
    if isinstance(listed, str):
        outcome, reason = ci_eval.evaluate([], listing_complete=False)
        return _Result(outcome, reason, [], [], f"incomplete run listing: {listed}")
    evidence: list[AttemptEvidence] = []
    records: list[dict[str, Any]] = []
    for run in listed:
        _judge_run(inputs, run, (workflow, dockerfile), gh, evidence, records)
    outcome, reason = ci_eval.evaluate(evidence, listing_complete=True)
    return _Result(outcome, reason, evidence, records)


def _judge_run(
    inputs: _Inputs,
    run: RunSummary,
    blobs: tuple[bytes, bytes],
    gh: GhRunner,
    evidence: list[AttemptEvidence],
    records: list[dict[str, Any]],
) -> None:
    """Every attempt ``1..run.attempts`` read, qualified and judged; a run
    whose attempts cannot be read is one ``undetermined`` entry. ``blobs``
    are the workflow's and the Dockerfile's bytes at the fix commit."""
    workflow, dockerfile = blobs
    if not 1 <= run.attempts <= MAX_ATTEMPTS:
        reason = f"run lists {run.attempts} attempts (read 1..{MAX_ATTEMPTS})"
        evidence.append(
            AttemptEvidence(
                (run.run_id, 0, ""), "undetermined", False, False, reason, ()
            )
        )
        return
    for attempt in range(1, run.attempts + 1):
        read = read_attempt(inputs.repo, run, attempt, gh)
        q = qualify(
            read,
            inputs.fix_commit,
            inputs.workflow_path,
            inputs.job_key,
            inputs.build,
            workflow,
            inputs.workflow_sha256,
        )
        judged = _evidence(inputs, q, read.logs, dockerfile)
        evidence.append(judged)
        if judged.qualification == "qualified":
            records.append(_record(judged, q, read.logs))


def _evidence(
    inputs: _Inputs, q: Qualified, logs: dict[int, str], dockerfile: bytes
) -> AttemptEvidence:
    """The attempt's evidence, from exactly the log its read returned."""
    if q.status != "qualified" or q.job is None:
        return ci_eval.from_qualification(q)
    log = logs.get(q.job.job_id)
    if log is None:
        reason = f"log of job {q.job.job_id} was not read"
        key = (q.run_id, q.attempt, q.job_key or "")
        return AttemptEvidence(key, "undetermined", False, False, reason, ())
    return ci_eval.attempt_evidence(
        q, inputs.cls, inputs.corrected, inputs.lines, log, dockerfile=dockerfile
    )


def _record(e: AttemptEvidence, q: Qualified, logs: dict[int, str]) -> dict[str, Any]:
    """One qualified attempt's evidence: run, attempt, job, the template's
    verdict, the recurrence lines (numbers and text, in the job text, split
    by :func:`split_lines`, the rule that numbered them) and the template's
    lines (numbers and raw text, in the job log as read, split on ``\\n``)."""
    assert q.job is not None
    text = split_lines(job_text(q.job))
    log = logs.get(q.job.job_id, "").split("\n")
    return {
        "run_id": e.key[0],
        "attempt": e.key[1],
        "job_key": e.key[2],
        "job_id": q.job.job_id,
        "positive": e.positive,
        "recurred": e.recurred,
        "template": e.template,
        "detail": e.detail,
        "lines": [
            {"line": n, "text": text[n - 1] if 0 < n <= len(text) else None}
            for n in e.lines
        ],
        "log_lines": [
            {"line": n, "text": log[n - 1] if 0 < n <= len(log) else None}
            for n in e.log_lines
        ],
    }


def _inputs(doc: FixDocument, doc_path: Path) -> _Inputs | str:
    """The document fields a confirmation reads, or why they are unusable."""
    publication, proposal = doc.publication, doc.proposal
    proof = doc.local_proof
    if (
        publication is None
        or publication.fix_commit is None
        or proposal is None
        or proof is None
    ):
        return "the document records no fix commit, proposal or local proof"
    fix_commit = publication.fix_commit
    if not _HEX40_RE.fullmatch(fix_commit):
        return f"the stored fix commit {fix_commit!r} is not a commit id"
    repo = _repo(doc)
    if not _is_slug(repo):  # a reason (it has spaces), never a slug
        return repo
    worktree = Path(publication.worktree)
    problem = _worktree_problem(worktree, doc_path, Path(doc.input.clone))
    if problem is not None:
        return problem
    path = doc.input.workflow_path
    if not _is_plain(path):
        return f"the stored workflow path {path!r} is not a plain relative path"
    dockerfile = doc.input.target.get("artifact_path")
    if not isinstance(dockerfile, str) or not _is_plain(dockerfile):
        return f"the stored Dockerfile path {dockerfile!r} is not a plain relative path"
    job_key = doc.input.reproduction_binding.get("workflow_job")
    if not isinstance(job_key, str) or not job_key:
        return "the stored reproduction binding has no workflow job"
    build = build_config(doc.input.build)
    if isinstance(build, str):
        return build
    if build.dockerfile != dockerfile:
        return (
            f"the stored Dockerfile path {dockerfile!r} is not the bound build's "
            f"{build.dockerfile!r}"
        )
    return _Inputs(
        repo=repo,
        fix_commit=fix_commit,
        worktree=worktree,
        workflow_path=path,
        workflow_sha256=doc.input.workflow_sha256,
        dockerfile_path=dockerfile,
        dockerfile_sha256=proof.dockerfile_sha256,
        job_key=job_key,
        build=build,
        cls=proposal.cls,
        corrected=proposal.replacement.strip(),
        lines=(proposal.lines[0], proposal.lines[1]),
    )


def _repo(doc: FixDocument) -> str:
    """The repository slug as ``fix publish`` reads it (``input.target
    ["repo"]``); when ``input.origin`` is recorded too, the two must agree
    (ignoring case, as GitHub does). The slug, or why there is none."""
    repo = doc.input.target.get("repo")
    if not isinstance(repo, str) or not _is_slug(repo):
        return f"the stored target repo {repo!r} is not an owner/name slug"
    origin = doc.input.origin
    if origin and origin.casefold() != repo.casefold():
        return f"the stored target repo {repo} disagrees with the origin {origin}"
    return repo


def _is_slug(value: str) -> bool:
    """``owner/name`` of safe characters, neither part ``.`` or ``..``."""
    if not _SLUG_RE.fullmatch(value):
        return False
    return all(part not in (".", "..") for part in value.split("/"))


def _worktree_problem(worktree: Path, doc_path: Path, clone: Path) -> str | None:
    """The worktree is this fix directory's and lies outside the clone."""
    if worktree.name != "worktree" or (
        worktree.parent.resolve() != doc_path.parent.resolve()
    ):
        return f"the worktree {worktree} is not this fix directory's"
    if not outside(worktree, clone):
        return f"the worktree {worktree} is inside the clone {clone}"
    return None


def _is_plain(path: str) -> bool:
    """A plain relative path: not absolute, no empty, ``.`` or ``..`` part."""
    return not path.startswith("/") and all(
        part not in ("", ".", "..") for part in path.split("/")
    )


def _blob(inputs: _Inputs, path: str, what: str) -> bytes | str:
    """The raw bytes of ``path`` at the fix commit, read in the worktree by
    the guarded chokepoint; or why they could not be read (``what`` names
    the file in the reason)."""
    guards = _guards(inputs.worktree)
    if isinstance(guards, str):
        return f"{what} not read: {guards}"
    spec = f"{inputs.fix_commit}:{path}"
    result = _run(inputs.worktree, "cat-file", "blob", spec, g=guards)
    if result.code != 0:
        return f"{what} not read at the fix commit: {result.error}"
    return result.stdout


def _undetermined(detail: str) -> _Result:
    """No run could be judged: ``qualification undetermined`` with why."""
    return _Result("ci_confirmation_insufficient", ci_eval.UNDETERMINED, [], [], detail)


def _considered(e: AttemptEvidence) -> dict[str, Any]:
    """One considered attempt: identity, qualification and its reason."""
    return {
        "run_id": e.key[0],
        "attempt": e.key[1],
        "job_key": e.key[2] or None,
        "qualification": e.qualification,
        "reason": None if e.qualification == "qualified" else e.detail,
    }


def _recorded(doc: FixDocument, at: str, result: _Result) -> FixDocument:
    """``doc`` with the attempt appended, the status mirroring it and
    ``last_operation`` set."""
    entry = CiAttempt(
        at=at,
        considered=[_considered(e) for e in result.evidence],
        outcome=result.outcome,
        reason=result.reason,
        evidence=result.records,
    )
    status = "ci_confirmed" if result.outcome == "ci_confirmed" else "fix_proposed"
    reason = result.reason
    if result.detail is not None:
        reason = f"{reason}: {result.detail}"
    doc = doc.model_copy(
        update={"ci_attempts": [*doc.ci_attempts, entry], "status": status}
    )
    return _operation(doc, at, result.outcome, reason)


def _operation(
    doc: FixDocument, at: str, result: str, reason: str | None
) -> FixDocument:
    """``doc`` with ``last_operation`` set for ``fix confirm``."""
    last = LastOperation(command="confirm", at=at, result=result, reason=reason)
    return doc.model_copy(update={"last_operation": last})


def _save(doc: FixDocument, path: Path) -> FixDocument:
    """An atomic save; a failure is exit 2 naming the file."""
    try:
        save(doc, path)
    except Exception as exc:  # noqa: BLE001 — every save failure is exit 2
        raise ConfirmAbort(f"cannot save {path}: {_describe(exc)}") from exc
    return doc


def _describe(exc: BaseException) -> str:
    """An exception as a reason: its type and message."""
    return f"{type(exc).__name__}: {exc}"
