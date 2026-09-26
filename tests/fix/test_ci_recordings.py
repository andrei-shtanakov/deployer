"""The CI template rows replayed on the C-recordings (F §7.3-§7.4, §9).

Every case under ``tests/fixtures/recordings/ci`` is served byte for byte by
:class:`Replay`, a ``GhRunner`` keyed by the recorded argv (a recorded error
is raised as the recorded ``GhError``). The path is the production one:
forge's own reading (``list_runs_for_sha``, ``read_attempt``), then
``qualify``, ``attempt_evidence`` and ``evaluate``; and every case once more
end to end through ``confirm.confirm`` on a ``fix_proposed`` document built
for it.

``confirm`` reads the workflow and the Dockerfile at the fix commit through
the guarded Git chokepoint, so the worktree must hold the recorded commit
object. It is rebuilt from the recording alone — ``tree/`` (every file mode
``100644``, as the recorder committed it) and the run's ``head_commit``
(message, identity, UTC time) — and the one fact the recording lacks, the
recorder's UTC offset, is found by trying every offset until the commit id
equals the recorded SHA. No production check is bypassed.

The expected table lives here; the committed ``expected.json`` is the
observation made with the earlier hypothesis matchers and is not read.
"""

import hashlib
import json
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from deployer.admission.model import DefectClass
from deployer.fix import ci_eval, templates
from deployer.fix.confirm import confirm
from deployer.fix.document import (
    FixDocument,
    Input,
    LocalProof,
    Proposal,
    Publication,
    load,
    save,
)
from deployer.fix.qualify import qualify
from deployer.forge import GhError, list_runs_for_sha, read_attempt
from deployer.reproduce.buildline import BuildConfig, parse_build_line
from deployer.reproduce.shape import job_text

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / templates.CI_RECORDING
AT = "2026-09-26T12:00:00+00:00"
JOB_KEY = "build"
BUILD_LINE = "docker build --file ./Dockerfile ."
CLASSES: dict[str, DefectClass] = {
    "copy": "missing_copy_source",
    "from": "from_argument_count",
}

Attempt = tuple[
    str, str | None, bool, bool, tuple[int, ...], tuple[int, ...], str | None
]
"""Per attempt: qualification, template verdict, positive, recurred, the
recurrence lines (1-based, in the job text), the template's lines (1-based,
in the job log as read, split on ``\n``; the bound build step's section only)
and detail."""

_UNREAD = (
    "attempt not read: gh api --allow-escape-sequences "
    "repos/andrei-shtanakov/deployer/actions/jobs/108113903561/logs failed: "
    'Get "https://api.github.com/repos/andrei-shtanakov/deployer/actions/jobs/'
    '108113903561/logs": net/http: TLS handshake timeout'
)
_RECUR_C5 = (134, 135, 153, 154, 155, 156, 157, 158, 159, 160, 161)
_PASS = ("qualified", "passed", True, False, ())
_UNQUALIFIED = ("undetermined", None, False, False, (), ())
INSUFFICIENT = "ci_confirmation_insufficient"

EXPECTED: dict[str, tuple[list[Attempt], str, str | None]] = {
    "c1-copy-done": ([(*_PASS, (191, 192), None)], "ci_confirmed", None),
    "c2-copy-rerun": (
        [(*_PASS, (191, 192), None), (*_UNQUALIFIED, _UNREAD)],
        INSUFFICIENT,
        ci_eval.UNDETERMINED,
    ),
    "c2b-copy-rerun-reread": (
        [(*_PASS, (191, 192), None), (*_PASS, (192, 193), None)],
        "ci_confirmed",
        None,
    ),
    "c3-from-parsed": ([(*_PASS, (147,), None)], "ci_confirmed", None),
    # The padded header ``#14 [stage-0  7/10]`` binds, but the build step
    # concluded ``failure`` (``RUN false``): a failed step's output cannot
    # prove the COPY ran — a failing RUN can print every line that would
    # (#100 review, ruling AD) — so passage is not proven (§7.4).
    "c4-copy-later-failure": (
        [
            (
                "qualified",
                "binding_ambiguous",
                False,
                False,
                (),
                (),
                "build step conclusion is 'failure'; only success proves the COPY ran",
            )
        ],
        INSUFFICIENT,
        "binding ambiguous",
    ),
    "c5-copy-recurred": (
        [
            (
                "qualified",
                "not_confirmed",
                False,
                True,
                _RECUR_C5,
                (150, 151),
                "defect recurred at lines 11-11",
            )
        ],
        INSUFFICIENT,
        ci_eval.RECURRED,
    ),
    # Two build steps in one job: qualification cannot bind one build, so
    # the attempt is undetermined before any template runs (§7.2).
    "c6-copy-cached": (
        [(*_UNQUALIFIED, "build not bound: several build steps")],
        INSUFFICIENT,
        ci_eval.UNDETERMINED,
    ),
    "c7-copy-done-padded": ([(*_PASS, (183, 184), None)], "ci_confirmed", None),
    # The "corrected" FROM asked here is the bad one: the template refuses on
    # the parse error, and the admission matcher binds it at line 1.
    "c8-from-bad-in-skipped-stage": (
        [
            (
                "qualified",
                "not_confirmed",
                False,
                True,
                (104,),
                (120,),
                "defect recurred at lines 1-1",
            )
        ],
        INSUFFICIENT,
        ci_eval.RECURRED,
    ),
    "c9-copy-done-padded-run": ([(*_PASS, (189, 190), None)], "ci_confirmed", None),
}

TEMPLATE: dict[str, tuple[str, tuple[int, ...], str | None]] = {
    "c6-copy-cached": ("binding_ambiguous", (95, 198), "several builds in log"),
    "c8-from-bad-in-skipped-stage": ("not_confirmed", (104,), "dockerfile parse error"),
}
"""``match_ci`` straight on the job text, where the pipeline hides it: ``c6``
never reaches the template (qualification), ``c8``'s recurrence overrides the
detail."""


@dataclass
class Replay:
    """A ``GhRunner`` serving one case's ``gh-calls.json`` byte for byte."""

    calls: list[dict[str, Any]]
    served: list[list[str]] = field(default_factory=list)

    def api(self, argv: list[str], *, timeout: float) -> str:
        """The recorded stdout for ``argv``, or the recorded ``GhError``."""
        self.served.append(list(argv))
        for call in self.calls:
            if call["argv"] == argv:
                if "error" in call:
                    raise GhError(call["error"], call["status"])
                return call["stdout"]
        raise AssertionError(f"unrecorded gh call {argv}")


@dataclass(frozen=True)
class Case:
    """One C-recording: its directory, environment and one check."""

    root: Path
    env: dict[str, Any]
    kind: templates.Kind
    corrected: str
    lines: tuple[int, int]

    @property
    def dockerfile(self) -> bytes:
        """The corrected Dockerfile, as committed at the fix commit."""
        return (self.root / "tree" / "Dockerfile").read_bytes()

    @property
    def workflow(self) -> bytes:
        """The workflow at the fix commit."""
        return (self.root / "tree" / self.env["workflow_path"]).read_bytes()

    def replay(self) -> Replay:
        """A fresh runner over the recorded calls."""
        return Replay(json.loads((self.root / "gh-calls.json").read_text()))


def _case(name: str) -> Case:
    root = CI / name
    (check,) = json.loads((root / "checks.json").read_text())
    return Case(
        root,
        json.loads((root / "environment.json").read_text()),
        check["kind"],
        check["corrected"],
        (check["lines"][0], check["lines"][1]),
    )


def _build() -> BuildConfig:
    build = parse_build_line(BUILD_LINE)
    assert isinstance(build, BuildConfig)
    return build


def _pipeline(case: Case, gh: Replay) -> list[ci_eval.AttemptEvidence]:
    """forge → ``qualify`` → ``attempt_evidence`` for every attempt."""
    runs = list_runs_for_sha(case.env["repo"], case.env["sha"], gh)
    assert not isinstance(runs, str), runs
    evidence: list[ci_eval.AttemptEvidence] = []
    for run in runs:
        for attempt in range(1, run.attempts + 1):
            read = read_attempt(case.env["repo"], run, attempt, gh)
            q = qualify(
                read,
                case.env["sha"],
                case.env["workflow_path"],
                JOB_KEY,
                _build(),
                case.workflow,
                hashlib.sha256(case.workflow).hexdigest(),
            )
            log = read.logs.get(q.job.job_id) if q.job is not None else None
            if q.status != "qualified" or log is None:
                evidence.append(ci_eval.from_qualification(q))
                continue
            evidence.append(
                ci_eval.attempt_evidence(
                    q,
                    CLASSES[case.kind],
                    case.corrected,
                    case.lines,
                    log,
                    dockerfile=case.dockerfile,
                )
            )
    return evidence


def _names() -> list[str]:
    return sorted(p.name for p in CI.iterdir() if p.is_dir())


def test_the_table_covers_every_case() -> None:
    """No recorded case is left out of the table, and none is invented."""
    assert _names() == sorted(EXPECTED)


@pytest.mark.parametrize("name", _names(), ids=[n.split("-")[0] for n in _names()])
def test_ci_recording_replays(name: str) -> None:
    """Every attempt's evidence and the evaluation equal the table; every
    recorded call is served exactly once."""
    case = _case(name)
    gh = case.replay()
    evidence = _pipeline(case, gh)
    got = [
        (
            e.qualification,
            e.template,
            e.positive,
            e.recurred,
            e.lines,
            e.log_lines,
            e.detail,
        )
        for e in evidence
    ]
    attempts, outcome, reason = EXPECTED[name]
    assert got == attempts
    assert ci_eval.evaluate(evidence, listing_complete=True) == (outcome, reason)
    assert gh.served == [call["argv"] for call in gh.calls]


@pytest.mark.parametrize("name", sorted(TEMPLATE), ids=["c6", "c8"])
def test_template_on_the_job_text(name: str) -> None:
    """``match_ci`` read straight on the recorded job's text."""
    case = _case(name)
    gh = case.replay()
    runs = list_runs_for_sha(case.env["repo"], case.env["sha"], gh)
    assert not isinstance(runs, str)
    read = read_attempt(case.env["repo"], runs[0], 1, gh)
    assert read.jobs is not None
    (job,) = read.jobs
    outcome = templates.match_ci(
        case.kind, case.corrected, job_text(job), dockerfile=case.dockerfile
    )
    assert (outcome.evidence, outcome.lines, outcome.detail) == TEMPLATE[name]


def test_padding_is_what_c4_and_c9_print() -> None:
    """``c4``/``c9`` print the corrected COPY with ``k`` right-aligned to
    ``n``'s width; ``c1`` (nine steps) prints it unpadded."""
    corrected = "COPY docs/guide/setup.md ./setup.md"
    for name, header in (
        ("c4-copy-later-failure", f"[stage-0  7/10] {corrected}"),
        ("c9-copy-done-padded-run", f"[stage-0  7/10] {corrected}"),
        ("c1-copy-done", f"[stage-0 7/9] {corrected}"),
    ):
        calls = json.loads((CI / name / "gh-calls.json").read_text())
        assert f"#14 {header}\n" in calls[-1]["stdout"], name


_PRINTED_RE = re.compile(r"#[0-9]+ \[(?P<bracket>[^\]]+)\] (?P<text>.*)")
_STAMP_RE = re.compile(r"\ufeff?[0-9T:.-]+Z ")


def _printed(name: str) -> list[tuple[str, str]]:
    """Every ``(bracket, text)`` stage header in the case's recorded job
    logs (``[internal]``/``[auth]`` vertices are no stage steps)."""
    calls = json.loads((CI / name / "gh-calls.json").read_text())
    logs = [c.get("stdout") or "" for c in calls if c["argv"][-1].endswith("/logs")]
    return [
        (m.group("bracket"), m.group("text"))
        for log in logs
        for line in log.split("\n")
        if (m := _PRINTED_RE.fullmatch(_STAMP_RE.sub("", line, count=1)))
        and m.group("bracket").split()[0] not in ("internal", "auth")
    ]


@pytest.mark.parametrize("name", _names(), ids=[n.split("-")[0] for n in _names()])
def test_buildkit_position_model(name: str) -> None:
    """Ruling Z: every stage header BuildKit printed in a C-recording is the
    position :func:`templates.buildkit_steps` derives from the recorded
    Dockerfile (a FROM header prints the resolved image, so it binds to the
    stage's FROM); the checked COPY/FROM is printed at its derived position.
    ``c8`` (two stages, a parse error) prints no stage header and is outside
    the model."""
    case = _case(name)
    steps = templates.buildkit_steps(case.dockerfile)
    printed = _printed(name)
    if isinstance(steps, str):
        assert name == "c8-from-bad-in-skipped-stage", steps
        assert printed == []
        return
    derived = {("FROM" if i.keyword == "FROM" else i.text): p.bracket for i, p in steps}
    assert printed
    for bracket, text in printed:
        key = "FROM" if text.startswith("FROM ") else text
        assert derived[key] == bracket, (text, bracket)
    if case.kind == "copy":
        assert (derived[case.corrected], case.corrected) in printed
    else:
        assert any(b == derived["FROM"] and t.startswith("FROM ") for b, t in printed)


# --- end to end through ``confirm`` ------------------------------------------


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.stdout.strip()


def _head_commit(case: Case) -> dict[str, Any]:
    """The run's ``head_commit``, as the recorded attempt metadata gives it."""
    calls = json.loads((case.root / "gh-calls.json").read_text())
    meta = next(c for c in calls if c["argv"][-1].endswith("/attempts/1"))
    return json.loads(meta["stdout"])["head_commit"]


def _commit_body(head: dict[str, Any], offset: str) -> bytes:
    """A commit object's body from ``head_commit`` at the UTC ``offset``."""
    when = int(datetime.fromisoformat(head["timestamp"]).timestamp())
    author = f"{head['author']['name']} <{head['author']['email']}>"
    committer = f"{head['committer']['name']} <{head['committer']['email']}>"
    return (
        f"tree {head['tree_id']}\n"
        f"author {author} {when} {offset}\n"
        f"committer {committer} {when} {offset}\n\n"
        f"{head['message']}\n"
    ).encode()


def _offsets() -> Iterator[str]:
    for minutes in range(-12 * 60, 14 * 60 + 1, 15):
        sign = "-" if minutes < 0 else "+"
        hours, rest = divmod(abs(minutes), 60)
        yield f"{sign}{hours:02d}{rest:02d}"


def _recorded_commit(case: Case, repo: Path) -> None:
    """Write ``tree/`` and the recorded commit object into ``repo``; the
    commit id must equal the recorded SHA."""
    head = _head_commit(case)
    tree = case.root / "tree"
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        blob = _git(repo, "hash-object", "-w", str(path))
        rel = path.relative_to(tree).as_posix()
        _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}")
    assert _git(repo, "write-tree") == head["tree_id"]
    for offset in _offsets():
        body = _commit_body(head, offset)
        digest = hashlib.sha1(b"commit %d\0" % len(body) + body).hexdigest()
        if digest == case.env["sha"]:
            written = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "hash-object",
                    "-t",
                    "commit",
                    "-w",
                    "--stdin",
                ],
                input=body,
                check=True,
                capture_output=True,
                timeout=60,
            ).stdout.decode()
            assert written.strip() == case.env["sha"]
            return
    raise AssertionError(f"{case.root.name}: no offset rebuilds the recorded SHA")


def _document(case: Case, fix_dir: Path, clone: Path) -> FixDocument:
    """A ``fix_proposed`` document whose fix commit is the recorded SHA."""
    repo, sha = case.env["repo"], case.env["sha"]
    build = _build()
    first, last = case.lines
    return FixDocument(
        schema_version="1.0",
        fix_id="0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f",
        status="fix_proposed",
        input=Input(
            verdict={"path": str(fix_dir / "verdict.json"), "sha256": "0" * 64},
            root=str(fix_dir),
            try_dir=str(fix_dir),
            source_dir=str(fix_dir),
            evidence=[],
            binding={"repo": repo, "head_sha": sha},
            reproduction_binding={"workflow_job": JOB_KEY},
            build={
                "dockerfile": build.dockerfile,
                "context": ".",
                "build_args": [],
                "platform": build.platform,
            },
            workflow_path=case.env["workflow_path"],
            workflow_sha256=hashlib.sha256(case.workflow).hexdigest(),
            backend="podman",
            clone=str(clone),
            origin=repo,
            head=sha,
            clean=True,
            target={"repo": repo, "head_sha": sha, "artifact_path": "Dockerfile"},
        ),
        proposal=Proposal(
            cls=CLASSES[case.kind],
            file="Dockerfile",
            lines=(first, last),
            transformation="copy-source" if case.kind == "copy" else "F1",
            original=case.corrected,
            replacement=case.corrected,
            ordinal=0,
            rationale=[],
            envelope=[],
        ),
        local_proof=LocalProof(
            dockerfile_sha256=hashlib.sha256(case.dockerfile).hexdigest(),
            build={},
            backend="podman",
            versions={},
            records_before=[],
            records_after=[],
            evidence=[],
            later_failure=None,
        ),
        publication=Publication(
            worktree=str(fix_dir / "worktree"),
            branch=case.env["branch"],
            fix_commit=sha,
            diff_ok=True,
            base="master",
            pr_url="https://example.com/pr/1",
        ),
    )


def _proposed(case: Case, tmp_path: Path) -> Path:
    """The fix directory (its worktree holding the recorded commit) and the
    saved ``fix.json``; returns the document's path."""
    fix_dir = tmp_path / "fixes" / "001"
    worktree = fix_dir / "worktree"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _recorded_commit(case, worktree)
    clone = tmp_path / "clone"
    clone.mkdir()
    doc_path = fix_dir / "fix.json"
    save(_document(case, fix_dir, clone), doc_path)
    return doc_path


@pytest.mark.parametrize("name", _names(), ids=[n.split("-")[0] for n in _names()])
def test_confirm_end_to_end(name: str, tmp_path: Path) -> None:
    """``confirm`` on the recorded runs: the table's outcome and reason,
    every attempt considered, the status mirroring the outcome."""
    case = _case(name)
    doc_path = _proposed(case, tmp_path)
    gh = case.replay()
    doc = confirm(doc_path, gh, lambda: AT)
    attempts, outcome, reason = EXPECTED[name]
    last = doc.ci_attempts[-1]
    assert (last.outcome, last.reason) == (outcome, reason), doc.last_operation
    considered = [(c["attempt"], c["qualification"]) for c in last.considered]
    assert considered == [(n, a[0]) for n, a in enumerate(attempts, 1)]
    positive = [
        (record["attempt"], [line["line"] for line in record["log_lines"]])
        for record in last.evidence
        if record["positive"]
    ]
    assert positive == [(n, list(a[5])) for n, a in enumerate(attempts, 1) if a[2]]
    assert doc.status == (
        "ci_confirmed" if outcome == "ci_confirmed" else "fix_proposed"
    )
    assert load(doc_path) == doc
    assert gh.served == [call["argv"] for call in gh.calls]
