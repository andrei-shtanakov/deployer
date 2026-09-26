"""``deployer fix confirm`` (F §1, §7.4-§7.5, §8.2, Task 19).

The ``fix_proposed`` document is reached through ``deployer fix`` and
``fix publish`` (run-5, a FROM fix) once per module; each test restores its
bytes. GitHub is always :class:`CiGh`, a fake serving synthetic runs of the
fix commit: the BuildKit logs are synthetic (NOT recordings). The CI rows
are enabled on the C-recordings, replayed end to end in
``test_ci_recordings.py``.
"""

import json
import re
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from deployer import cli
from deployer.fix import ci_eval, templates
from deployer.fix import confirm as confirm_mod
from deployer.fix.confirm import REFUSED, ConfirmAbort, confirm
from deployer.fix.document import FixDocument, load, save
from deployer.fix.qualify import Qualified
from deployer.forge import AttemptRead, Evidence, FailedJob, GhError
from tests.fix.conftest import enable_for_test
from tests.fix.test_ci_eval import FROM_BODY, FROM_RECUR_BODY
from tests.fix.test_publish import _published

AT = "2026-09-25T12:00:00+00:00"
ROW = "from-parsed/buildkit"
PIN = "93cb6efe18208431cddfb8368fd83d5badbf9bfd"
CHECKOUT = f"Run actions/checkout@{PIN}"
BUILD = "Run docker build --file ./Dockerfile ."
_RUNS_RE = re.compile(r"/actions/runs\?head_sha=(\w+)&per_page=100&page=(\d+)$")
_ATTEMPT_RE = re.compile(r"/actions/runs/(\d+)/attempts/(\d+)$")
_JOBS_RE = re.compile(r"/actions/runs/(\d+)/attempts/(\d+)/jobs\?")
_LOGS_RE = re.compile(r"/actions/jobs/(\d+)/logs$")


def ci_log(sha: str, build: str = FROM_BODY) -> str:
    """A job log: the checkout printing ``sha``, the build step, ``build``."""
    return "\n".join(
        [
            f"##[group]{CHECKOUT}",
            "[command]/usr/bin/git log -1 --format=%H",
            sha,
            "##[endgroup]",
            f"##[group]{BUILD}",
            "docker build --file ./Dockerfile .",
            "##[endgroup]",
            build,
        ]
    )


@dataclass
class Attempt:
    """One attempt of a synthetic run: its status and its one job's log
    (a :class:`GhError` for a log GitHub refuses)."""

    log: str | GhError
    status: str = "completed"
    conclusion: str | None = "success"
    job_name: str = "build"


@dataclass
class Run:
    """A synthetic run of the fix commit."""

    run_id: int
    attempts: list[Attempt]
    event: str = "push"
    path: str = ".github/workflows/diagnosis-polygon.yml"
    claimed: int | None = None
    """``run_attempt`` the listing claims (default: ``len(attempts)``)."""


@dataclass
class CiGh:
    """GitHub's runs, attempts, jobs and logs endpoints over :class:`Run`s.

    ``short`` claims one run more than it lists (an incomplete listing);
    ``listing_error`` fails the listing."""

    sha: str
    runs: list[Run] = field(default_factory=list)
    short: bool = False
    listing_error: GhError | None = None
    paths: list[str] = field(default_factory=list)

    def api(self, argv: list[str], *, timeout: float) -> str:
        """Answer one ``gh api`` call."""
        path = argv[-1]
        self.paths.append(path)
        if m := _RUNS_RE.search(path):
            return self._listing(int(m.group(2)))
        if m := _JOBS_RE.search(path):
            return self._jobs(int(m.group(1)), int(m.group(2)))
        if m := _LOGS_RE.search(path):
            return self._log(int(m.group(1)))
        if m := _ATTEMPT_RE.search(path):
            return self._attempt(int(m.group(1)), int(m.group(2)))
        raise AssertionError(f"unexpected path {path}")

    def _listing(self, page: int) -> str:
        if self.listing_error is not None:
            raise self.listing_error
        rows = [
            {
                "id": r.run_id,
                "run_attempt": len(r.attempts) if r.claimed is None else r.claimed,
                "event": r.event,
                "head_sha": self.sha,
                "path": r.path,
                "head_branch": "fix",
            }
            for r in self.runs
        ]
        total = len(rows) + (1 if self.short else 0)
        return json.dumps(
            {"total_count": total, "workflow_runs": rows if page == 1 else []}
        )

    def _find(self, run_id: int, attempt: int) -> Attempt:
        run = next(r for r in self.runs if r.run_id == run_id)
        return run.attempts[attempt - 1]

    def _attempt(self, run_id: int, attempt: int) -> str:
        a = self._find(run_id, attempt)
        return json.dumps(
            {
                "id": run_id,
                "head_sha": self.sha,
                "run_attempt": attempt,
                "status": a.status,
                "conclusion": a.conclusion,
            }
        )

    def _jobs(self, run_id: int, attempt: int) -> str:
        a = self._find(run_id, attempt)
        steps = [
            ("Set up job", "success"),
            (CHECKOUT, "success"),
            (BUILD, "success"),
            (f"Post {CHECKOUT}", "success"),
            ("Complete job", "success"),
        ]
        record = {
            "id": _job_id(run_id, attempt),
            "name": a.job_name,
            "conclusion": "success",
            "steps": [
                {"number": n, "name": name, "conclusion": c}
                for n, (name, c) in enumerate(steps, start=1)
            ],
        }
        return json.dumps({"total_count": 1, "jobs": [record]})

    def _log(self, job_id: int) -> str:
        run_id, attempt = divmod(job_id, 100)
        log = self._find(run_id, attempt).log
        if isinstance(log, GhError):
            raise log
        return log

    def log_reads(self) -> list[str]:
        """Every log fetch made."""
        return [p for p in self.paths if _LOGS_RE.search(p)]


def _job_id(run_id: int, attempt: int) -> int:
    return run_id * 100 + attempt


@dataclass
class Fix:
    """The published fix, its ``fix.json`` and the original bytes."""

    doc_path: Path
    original: bytes

    @property
    def doc(self) -> FixDocument:
        """The document as saved now."""
        return load(self.doc_path)

    @property
    def commit(self) -> str:
        """The stored fix commit."""
        publication = self.doc.publication
        assert publication is not None and publication.fix_commit is not None
        return publication.fix_commit

    def gh(self, *runs: Run, **kw: Any) -> CiGh:
        """A fake GitHub serving ``runs`` of the fix commit."""
        return CiGh(self.commit, list(runs), **kw)

    def ok(self, run_id: int = 1, **kw: Any) -> Run:
        """A run whose one attempt passed the corrected FROM."""
        return Run(run_id, [Attempt(ci_log(self.commit))], **kw)

    def recurred(self, run_id: int = 2) -> Run:
        """A run whose one attempt hit the same FROM parse error again."""
        return Run(run_id, [Attempt(ci_log(self.commit, FROM_RECUR_BODY.format(n=1)))])

    def run(self, gh: CiGh) -> FixDocument:
        """One confirmation with ``gh`` and the fixed clock."""
        return confirm(self.doc_path, gh, lambda: AT)

    def rewrite(self, **fields: object) -> None:
        """Re-save the document with ``fields`` replaced."""
        save(self.doc.model_copy(update=fields), self.doc_path)


@pytest.fixture(scope="module")
def published(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Fix]:
    """run-5 authored, proved and published once: ``fix_proposed``."""
    with pytest.MonkeyPatch.context() as mp:
        pub = _published(tmp_path_factory.mktemp("confirm"), mp)
        doc = pub.run()
        assert doc.status == "fix_proposed", doc.last_operation
        yield Fix(pub.doc_path, pub.doc_path.read_bytes())


@pytest.fixture()
def fix(published: Fix) -> Iterator[Fix]:
    """The published fix, its ``fix.json`` restored for every test."""
    published.doc_path.write_bytes(published.original)
    yield published
    published.doc_path.parent.chmod(0o755)
    published.doc_path.write_bytes(published.original)


def _insufficient(doc: FixDocument, reason: str) -> None:
    """``doc`` recorded one more insufficient attempt with ``reason``."""
    last = doc.ci_attempts[-1]
    assert (last.outcome, last.reason) == ("ci_confirmation_insufficient", reason)
    assert doc.status == "fix_proposed"
    assert doc.last_operation is not None
    assert doc.last_operation.command == "confirm"
    assert doc.last_operation.result == "ci_confirmation_insufficient"
    assert doc.last_operation.reason is not None
    assert doc.last_operation.reason.startswith(reason)


def _reason(doc: FixDocument) -> str:
    """``last_operation.reason``, ``""`` when absent."""
    last = doc.last_operation
    return (last.reason or "") if last is not None else ""


def _considered(doc: FixDocument) -> list[tuple[Any, ...]]:
    """The last attempt's considered list as tuples."""
    return [
        (c["run_id"], c["attempt"], c["job_key"], c["qualification"])
        for c in doc.ci_attempts[-1].considered
    ]


# --- the status gate ---------------------------------------------------------


@pytest.mark.parametrize("status", ["locally_confirmed", "stopped"])
def test_unpublished_is_refused_unread(fix: Fix, status: str) -> None:
    """Not published → refused before any read: no gh call, no attempt."""
    extra = {"stop_reason": "no proposal"} if status == "stopped" else {}
    fix.rewrite(status=status, **extra)
    gh = fix.gh(fix.ok())
    git_calls: list[object] = []

    def spy(*args: Any, **kw: Any) -> Any:
        git_calls.append(args)
        raise AssertionError("git must not run")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(confirm_mod, "_run", spy)
        mp.setattr(confirm_mod, "_guards", spy)
        doc = fix.run(gh)
    assert gh.paths == [] and git_calls == []
    assert doc.status == status and doc.ci_attempts == []
    assert doc.last_operation is not None
    assert (doc.last_operation.result, doc.last_operation.at) == (REFUSED, AT)
    assert fix.doc == doc


# --- outcomes ------------------------------------------------------------------


def test_rows_disabled_is_not_enabled(fix: Fix) -> None:
    """No recording backs the CI row → ``templates not enabled``."""
    rows = tuple(
        replace(row, recording=None) if row.side == "ci" else row
        for row in templates.ROWS
    )
    with patch.object(templates, "ROWS", rows):
        doc = fix.run(fix.gh(fix.ok()))
    _insufficient(doc, ci_eval.NOT_ENABLED)
    assert _considered(doc) == [(1, 1, "build", "qualified")]


def test_seam_confirms(fix: Fix) -> None:
    """With the seam: ``ci_confirmed``, the attempt and its lines recorded,
    the log read once and handed over exactly as read."""
    seen: list[str] = []
    dockerfiles: list[bytes] = []
    real = ci_eval.attempt_evidence

    def spy(*args: Any, **kw: Any) -> ci_eval.AttemptEvidence:
        seen.append(args[-1])
        dockerfiles.append(kw["dockerfile"])
        return real(*args, **kw)

    gh = fix.gh(fix.ok())
    with pytest.MonkeyPatch.context() as mp, enable_for_test(ROW):
        mp.setattr(ci_eval, "attempt_evidence", spy)
        doc = fix.run(gh)
    assert seen == [ci_log(fix.commit)]
    assert dockerfiles == [_committed_dockerfile(fix)]
    assert len(gh.log_reads()) == 1
    assert doc.status == "ci_confirmed"
    attempt = doc.ci_attempts[-1]
    assert (attempt.at, attempt.outcome, attempt.reason) == (AT, "ci_confirmed", None)
    assert attempt.considered == [
        {
            "run_id": 1,
            "attempt": 1,
            "job_key": "build",
            "qualification": "qualified",
            "reason": None,
        }
    ]
    (record,) = attempt.evidence
    assert (record["run_id"], record["job_id"], record["positive"]) == (1, 101, True)
    assert record["lines"] == []
    texts = [line["text"] for line in record["log_lines"]]
    assert any("FROM docker.io/library/python:3.12-slim" in t for t in texts)
    assert doc.last_operation is not None
    assert doc.last_operation.result == "ci_confirmed"
    assert fix.doc == doc


def test_evidence_text_split_on_newline_only() -> None:
    """A ``\\r`` inside an earlier line (job evidence keeps it verbatim) does
    not shift the recorded text off the evidence line number (review M1)."""
    job = FailedJob(101, "build", "success", [], [Evidence(101, "a\rb\nc\nd")])
    q = Qualified(1, 1, "build", "qualified", None, job, None)
    e = ci_eval.AttemptEvidence(
        (1, 1, "build"), "qualified", True, False, None, (2,), log_lines=(3,)
    )
    record = confirm_mod._record(e, q, {101: "x\ry\nz\nw"})
    assert record["lines"] == [{"line": 2, "text": "c"}]
    assert record["log_lines"] == [{"line": 3, "text": "w"}]


def test_recheck_contradicts(fix: Fix) -> None:
    """A later recurrence → ``contradictory runs``, status back to
    ``fix_proposed``; the earlier positive attempt is kept."""
    with enable_for_test(ROW):
        first = fix.run(fix.gh(fix.ok()))
        assert first.status == "ci_confirmed"
        doc = fix.run(fix.gh(fix.ok(), fix.recurred()))
    _insufficient(doc, ci_eval.CONTRADICTORY)
    assert [a.outcome for a in doc.ci_attempts] == [
        "ci_confirmed",
        "ci_confirmation_insufficient",
    ]
    assert doc.ci_attempts[0] == first.ci_attempts[0]


def test_recurrence_alone(fix: Fix) -> None:
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(fix.recurred()))
    _insufficient(doc, ci_eval.RECURRED)


def test_rerun_hides_recurrence(fix: Fix) -> None:
    """Every attempt counts: attempt 1 recurred, the re-run passed."""
    bad = fix.recurred(3).attempts[0]
    run = Run(3, [bad, Attempt(ci_log(fix.commit))])
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(run))
    _insufficient(doc, ci_eval.CONTRADICTORY)
    assert _considered(doc) == [
        (3, 1, "build", "qualified"),
        (3, 2, "build", "qualified"),
    ]


def test_no_runs(fix: Fix) -> None:
    doc = fix.run(fix.gh())
    _insufficient(doc, ci_eval.NO_QUALIFYING)
    assert doc.ci_attempts[-1].considered == []


# --- undetermined and excluded -------------------------------------------------


def test_unavailable_log(fix: Fix) -> None:
    """An expired log → ``qualification undetermined`` even beside a pass."""
    gone = Run(4, [Attempt(GhError("gone (HTTP 410)", 410))])
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(fix.ok(), gone))
    _insufficient(doc, ci_eval.UNDETERMINED)
    entry = doc.ci_attempts[-1].considered[1]
    assert entry["qualification"] == "undetermined"
    assert "unavailable" in entry["reason"]


def test_unfinished_attempt(fix: Fix) -> None:
    """An in-progress re-run is read and undetermined, never skipped."""
    run = Run(5, [Attempt(ci_log(fix.commit)), Attempt("", "in_progress", None)])
    gh = fix.gh(run)
    with enable_for_test(ROW):
        doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert _considered(doc) == [
        (5, 1, "build", "qualified"),
        (5, 2, None, "undetermined"),
    ]
    assert any(p.endswith("/runs/5/attempts/2") for p in gh.paths)


def test_incomplete_listing(fix: Fix) -> None:
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(fix.ok(), short=True))
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert doc.last_operation is not None
    assert "incomplete" in _reason(doc)


def test_duplicate_run_listing(fix: Fix) -> None:
    """A run listed twice (final review M2) → incomplete, undetermined; even
    a passing run is not confirmed from it."""
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(fix.ok(), fix.ok()))
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "run 1 listed twice" in _reason(doc)


def test_listing_gh_error(fix: Fix) -> None:
    """A status-less gh failure is a recorded reason, not a traceback."""
    error = GhError("gh api could not start: no gh")
    doc = fix.run(fix.gh(listing_error=error))
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "no gh" in _reason(doc)


def test_pull_request_excluded(fix: Fix) -> None:
    """A ``pull_request`` run is listed as excluded and takes no part."""
    pr = fix.ok(6, event="pull_request")
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(fix.ok(), pr))
    assert doc.status == "ci_confirmed"
    excluded = doc.ci_attempts[-1].considered[1]
    assert (excluded["run_id"], excluded["qualification"]) == (6, "excluded")
    assert "pull_request" in excluded["reason"]


def test_checkout_elsewhere(fix: Fix) -> None:
    """A checkout at another commit (a merge) is excluded, not a pass."""
    run = Run(7, [Attempt(ci_log("a" * 40))])
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(run))
    _insufficient(doc, ci_eval.NO_QUALIFYING)
    assert _considered(doc) == [(7, 1, "build", "excluded")]


def test_zero_attempts(fix: Fix) -> None:
    doc = fix.run(fix.gh(Run(8, [])))
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert _considered(doc) == [(8, 0, None, "undetermined")]


def test_too_many_attempts(fix: Fix) -> None:
    """A run claiming more than ``MAX_ATTEMPTS`` attempts: one undetermined
    entry, no attempt read, insufficient."""
    run = Run(9, [], claimed=confirm_mod.MAX_ATTEMPTS + 1)
    gh = fix.gh(fix.ok(), run)
    with enable_for_test(ROW):
        doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert _considered(doc) == [
        (1, 1, "build", "qualified"),
        (9, 0, None, "undetermined"),
    ]
    assert not any("/runs/9/" in p for p in gh.paths)


def test_log_key_missing(fix: Fix) -> None:
    """A qualified attempt whose read carries no log text → undetermined."""
    real = confirm_mod.read_attempt

    def dropped(*args: Any) -> AttemptRead:
        return replace(real(*args), logs={})

    with pytest.MonkeyPatch.context() as mp, enable_for_test(ROW):
        mp.setattr(confirm_mod, "read_attempt", dropped)
        doc = fix.run(fix.gh(fix.ok()))
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "was not read" in doc.ci_attempts[-1].considered[0]["reason"]


# --- document inputs and local failures ----------------------------------------


def test_workflow_unreadable(fix: Fix) -> None:
    """A fix commit the worktree lacks: nothing listed, undetermined."""
    publication = fix.doc.publication
    assert publication is not None
    fix.rewrite(publication=publication.model_copy(update={"fix_commit": "b" * 40}))
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "workflow not read" in _reason(doc)
    assert gh.paths == []


def _committed_dockerfile(fix: Fix) -> bytes:
    """The Dockerfile's bytes at the fix commit, read with plain Git."""
    doc = fix.doc
    assert doc.publication is not None
    path = doc.input.target["artifact_path"]
    return subprocess.run(
        [
            "git",
            "-C",
            doc.publication.worktree,
            "cat-file",
            "blob",
            f"{fix.commit}:{path}",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout


def test_dockerfile_unreadable(fix: Fix) -> None:
    """Ruling P: a Dockerfile the fix commit lacks is undetermined with the
    reason, before anything is listed; nothing raises."""
    target = {**fix.doc.input.target, "artifact_path": "no/such/Dockerfile"}
    build = {**fix.doc.input.build, "dockerfile": "no/such/Dockerfile"}
    _set_input(fix, target=target, build=build)
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "Dockerfile not read at the fix commit" in _reason(doc)
    assert gh.paths == []


def test_dockerfile_path_not_the_bound_build(fix: Fix) -> None:
    """Ruling V: ``target.artifact_path`` must be the bound build's
    Dockerfile, else undetermined before anything is read."""
    build = {**fix.doc.input.build, "dockerfile": "other/Dockerfile"}
    _set_input(fix, build=build)
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "is not the bound build's 'other/Dockerfile'" in _reason(doc)
    assert gh.paths == []


def test_dockerfile_not_the_proved_one(fix: Fix) -> None:
    """Ruling V: the Dockerfile at the fix commit must hash to the locally
    proved bytes, else undetermined before anything is listed."""
    proof = fix.doc.local_proof
    assert proof is not None
    fix.rewrite(local_proof=proof.model_copy(update={"dockerfile_sha256": "0" * 64}))
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "is not the locally proved one" in _reason(doc)
    assert gh.paths == []


@pytest.mark.parametrize(
    "path", ["/Dockerfile", "../Dockerfile", "./Dockerfile", None], ids=str
)
def test_dockerfile_path_not_plain(fix: Fix, path: str | None) -> None:
    """A stored Dockerfile path that is not plain and relative is refused."""
    target = {**fix.doc.input.target, "artifact_path": path}
    _set_input(fix, target=target)
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "is not a plain relative path" in _reason(doc)
    assert gh.paths == []


def _set_input(fix: Fix, **fields: Any) -> None:
    """Re-save the document with ``input`` fields replaced."""
    fix.rewrite(input=fix.doc.input.model_copy(update=fields))


def test_repo_disagrees(fix: Fix) -> None:
    """``target.repo`` (publish's reader) and ``origin`` differ: neither is
    chosen, nothing is listed."""
    _set_input(fix, origin="example/other")
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "disagrees with the origin example/other" in _reason(doc)
    assert gh.paths == []


def test_repo_case_variant(fix: Fix) -> None:
    """A case variant of the same slug agrees (GitHub ignores case)."""
    _set_input(fix, origin="Example/Project")
    with enable_for_test(ROW):
        doc = fix.run(fix.gh(fix.ok()))
    assert doc.status == "ci_confirmed"


@pytest.mark.parametrize(
    "slug", ["../project", "example/..", "./project"], ids=["up", "down", "dot"]
)
def test_repo_dot_segments(fix: Fix, slug: str) -> None:
    """``.``/``..`` segments never reach a ``repos/{slug}`` API path."""
    target = {**fix.doc.input.target, "repo": slug}
    _set_input(fix, target=target, origin=slug)
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "is not an owner/name slug" in _reason(doc)
    assert gh.paths == []


def test_foreign_worktree(fix: Fix, tmp_path: Path) -> None:
    publication = fix.doc.publication
    assert publication is not None
    moved = publication.model_copy(update={"worktree": str(tmp_path / "worktree")})
    fix.rewrite(publication=moved)
    gh = fix.gh(fix.ok())
    doc = fix.run(gh)
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert gh.paths == []


def test_crash_is_recorded(fix: Fix) -> None:
    def boom(*args: Any) -> Any:
        raise RuntimeError("surprise")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(confirm_mod, "list_runs_for_sha", boom)
        doc = fix.run(fix.gh())
    _insufficient(doc, ci_eval.UNDETERMINED)
    assert "RuntimeError: surprise" in _reason(doc)


def test_save_failure_names_file(fix: Fix) -> None:
    def fail(doc: FixDocument, path: Path) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(confirm_mod, "save", fail)
        with pytest.raises(ConfirmAbort, match=re.escape(str(fix.doc_path))):
            fix.run(fix.gh())
    assert fix.doc_path.read_bytes() == fix.original


def test_unreadable_doc(tmp_path: Path) -> None:
    with pytest.raises(ConfirmAbort, match="cannot read"):
        confirm(tmp_path / "fix.json", CiGh("0" * 40), lambda: AT)


# --- the CLI (§8.2) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "code"),
    [("ok", 0), ("short", 1), ("unpub", 1), ("ro", 2), ("absent", 2)],
)
def test_cli_exit(
    fix: Fix,
    case: str,
    code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """0 confirmed, 1 insufficient or not published, 2 local I/O."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: AT)
    gh = fix.gh(fix.ok(), short=case == "short")
    monkeypatch.setattr(cli, "SubprocessGh", lambda: gh)
    path = fix.doc_path
    if case == "unpub":
        fix.rewrite(status="locally_confirmed")
    if case == "ro":
        path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if case == "absent":
        path = tmp_path / "absent.json"
    with enable_for_test(ROW):
        assert cli.main(["fix", "confirm", str(path)]) == code
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    if code == 2:
        assert str(path) in captured.err
    if case == "ro":
        assert gh.paths == []


@pytest.mark.parametrize(
    "argv",
    [
        ["fix", "confirm"],
        ["fix", "confirm", "a.json", "b.json"],
        ["fix", "confirm", "a.json", "--base", "main"],
        ["fix", "confirm", "a.json", "--clone", "c"],
    ],
    ids=["none", "two", "base", "clone"],
)
def test_cli_invalid(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: Any) -> Any:
        raise AssertionError("confirm must not run")

    monkeypatch.setattr(cli, "confirm", never)
    assert cli.main(argv) == 2


def test_cli_loads_dotenv(
    fix: Fix, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``fix confirm`` loads ``.env`` as ``fix publish`` does (gh's token)."""
    monkeypatch.chdir(tmp_path)
    fake_env: dict[str, str] = {}
    monkeypatch.setattr(cli.os, "environ", fake_env)
    (tmp_path / ".env").write_text("GH_TOKEN=t0k\n")
    monkeypatch.setattr(cli, "SubprocessGh", lambda: fix.gh())
    assert cli.main(["fix", "confirm", str(fix.doc_path)]) == 1
    assert fake_env["GH_TOKEN"] == "t0k"
