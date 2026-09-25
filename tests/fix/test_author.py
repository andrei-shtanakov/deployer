"""``deployer fix`` end to end (F §2-§6, §8; P level): R's replayed run-1 and
run-5, a real signed set and admission, a real Git clone and worktree; the
container runtime and the model are faked, nothing is pushed.

run-1 variants are built at test time over R's restored tree (the committed
bundles are untouched): ``basename-unique`` adds one other file named
``setup.md``; ``basename-ambiguous`` adds two. The happy path is reachable
only through the ``enable_for_test`` seam.
"""

import dataclasses
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from deployer import runtime as runtime_mod
from deployer.fix import author as author_mod
from deployer.fix.author import FIX_FILE, FixAbort, author_fix
from deployer.fix.document import FixDocument, load
from deployer.fix.workspace import Committed, FixDirError, Vetted, new_fix_dir
from deployer.models import ContainerRuntime
from deployer.provenance.issue import Issued
from deployer.provenance.model import POINTER, SET_ROOT
from tests.admission.conftest import Replayed
from tests.fix.conftest import (
    Scenario,
    add_tree_file,
    admitted_scenario,
    commit_all,
    enable_for_test,
    git,
    restored_at,
)
from tests.reproduce.conftest import FakeContainers, proc

PODMAN = ContainerRuntime(tool="podman")
UNIQUE = "docs/guide/setup.md"
F1_LINE = "FROM python:3.12-slim AS extra"
COPY_LINE = f"COPY {UNIQUE} ./setup.md"
FROM_STDOUT = f"STEP 1/10: {F1_LINE}\nSTEP 2/10: WORKDIR /app\n"
COPY_STDOUT = (
    "STEP 1/12: FROM python:3.12-slim\n"
    f"STEP 7/12: {COPY_LINE}\n"
    "STEP 8/12: RUN uv sync --frozen\n"
)


@dataclass
class FakeChooser:
    """Answers the §4.1 prompt with ``answer`` (or raises it); counts calls."""

    answer: str | Exception = ""
    prompts: list[str] = field(default_factory=list)

    def choose(self, prompt: str) -> str:
        """Record the prompt and answer."""
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _answer(source: str) -> str:
    """A well-formed model answer choosing ``source``."""
    return json.dumps(
        {
            "source": source,
            "plausible": [source],
            "rationale": [
                {
                    "facts": [{"kind": "path", "ref": source}],
                    "explanation": "the only file named like the absent source",
                }
            ],
        }
    )


@dataclass
class Case:
    """An admitted scenario plus the fakes ``deployer fix`` runs against."""

    s: Scenario
    fake: FakeContainers
    chooser: FakeChooser
    tmp: Path

    def verdict(self) -> Path:
        """The verdict bound to the clone's ``HEAD``, written to a file."""
        path = self.tmp / "verdict.json"
        path.write_text(json.dumps(self.s.document()))
        return path

    def run(
        self,
        *,
        no_key: bool = False,
        rt: ContainerRuntime | None = PODMAN,
        root: Path | None = None,
    ) -> FixDocument:
        """``author_fix`` with this case's defaults."""
        return author_fix(
            self.verdict(),
            self.s.clone,
            self.s.r.root if root is None else root,
            self.s.env,
            None if no_key else self.s.key,
            self.chooser,
            rt,
            60,
        )

    def set_build(self, stdout: str, code: int = 0) -> None:
        """Script the fake's answer to the local proof's build."""
        self.fake.responses[("build",)] = proc(code, stdout=stdout)

    def builds(self) -> list[list[str]]:
        """Build calls made after the scenario was set up."""
        return [call for call in self.fake.calls if call[:1] == ["build"]]

    def fix_dir(self, seq: int = 1) -> Path:
        """``attempt/fixes/<seq>``."""
        return self.s.r.attempt_dir / "fixes" / f"{seq:03d}"


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    before_issue: Callable[[Replayed], None] | None = None,
    answer: str | Exception = "",
) -> Case:
    """``name`` admitted over a clean clone; the fake's calls cleared."""
    fake = FakeContainers()
    monkeypatch.setattr(runtime_mod, "container_run", fake)
    scenario = admitted_scenario(name, tmp_path, fake, before_issue)
    fake.calls.clear()
    return Case(scenario, fake, FakeChooser(answer), tmp_path)


def _unique(r: Replayed) -> None:
    """basename-unique: one other regular file named ``setup.md``."""
    add_tree_file(r, UNIQUE, b"# Setup\n")


def _ambiguous(r: Replayed) -> None:
    """basename-ambiguous: two other regular files named ``setup.md``."""
    add_tree_file(r, "docs/a/setup.md", b"# A\n")
    add_tree_file(r, "docs/b/setup.md", b"# B\n")


@pytest.fixture()
def run5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Case:
    """run-5 (``FROM python:3.12-slim extra``) admitted."""
    return _case(tmp_path, monkeypatch, "run-5")


@pytest.fixture()
def unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Case:
    """run-1 basename-unique; the fake model picks the one ``setup.md``."""
    return _case(tmp_path, monkeypatch, "run-1", _unique, _answer(UNIQUE))


@pytest.fixture()
def ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Case:
    """run-1 basename-ambiguous; the model must never be asked."""
    return _case(tmp_path, monkeypatch, "run-1", _ambiguous, _answer(UNIQUE))


def _changes(worktree: Path, commit: str) -> dict[str, str]:
    """``path -> status letter`` of ``commit`` against its parent."""
    out = git(worktree, "diff-tree", "-r", "--no-commit-id", "--name-status", commit)
    rows = (line.split("\t") for line in out.splitlines())
    return {path: status for status, path in rows}


def _status(worktree: Path) -> str:
    """The worktree's porcelain status (empty when clean)."""
    return git(worktree, "status", "--porcelain", "--untracked-files=all")


# --- run-5: F1 ---------------------------------------------------------------


def test_run5_locally_confirmed_by_default(run5: Case) -> None:
    """Production rows: the recording-backed local FROM row confirms the F1
    line without the seam."""
    run5.set_build(FROM_STDOUT)
    doc = run5.run()
    assert doc.status == "locally_confirmed", (doc.stop_reason, doc.stop_detail)
    assert doc.stop_reason is None and doc.stop_detail is None
    assert doc.proposal is not None and doc.proposal.transformation == "F1"
    assert doc.proposal.replacement.strip() == F1_LINE
    assert doc.local_proof is not None
    assert doc.local_proof.evidence[0]["evidence"] == "passed"
    assert doc.publication is not None and doc.publication.fix_commit is not None
    assert load(run5.fix_dir() / FIX_FILE) == doc
    assert run5.chooser.prompts == []


def test_run5_with_the_seam_is_locally_confirmed(run5: Case) -> None:
    """The commit holds the F1 line and the new set; the clone is untouched."""
    clone_head = git(run5.s.clone, "rev-parse", "HEAD")
    run5.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"):
        doc = run5.run()
    assert doc.status == "locally_confirmed", (doc.stop_reason, doc.stop_detail)
    assert doc.stop_reason is None
    publication = doc.publication
    assert publication is not None and publication.fix_commit is not None
    assert publication.diff_ok
    worktree, sha = Path(publication.worktree), publication.fix_commit
    assert worktree == run5.fix_dir() / "worktree"
    assert git(worktree, "rev-parse", publication.branch) == sha
    assert publication.branch.startswith("deployer/fix/from-argument-count/")
    dockerfile = git(worktree, "show", f"{sha}:Dockerfile")
    assert dockerfile.splitlines()[0] == F1_LINE
    changes = _changes(worktree, sha)
    pointer = f"{SET_ROOT}/{POINTER}"
    new_set = git(worktree, "show", f"{sha}:{pointer}")
    assert changes.pop("Dockerfile") == "M"
    assert changes.pop(pointer) == "M"
    added = {path for path, status in changes.items() if status == "A"}
    assert {path.rsplit("/", 1)[0] for path in added} == {f"{SET_ROOT}/{new_set}"}
    assert len(added) == 3
    assert set(changes.values()) == {"A", "D"}
    assert git(worktree, "log", "-1", "--format=%an <%ae>", sha) == (
        "deployer <deployer@localhost>"
    )
    assert doc.last_operation is not None
    assert doc.last_operation.result == "locally_confirmed"
    assert doc.last_operation.reason is None  # the worktree index was synced
    assert publication.index_synced is True
    assert _status(worktree) == ""
    assert git(run5.s.clone, "rev-parse", "HEAD") == clone_head
    assert _status(run5.s.clone) == ""
    assert load(run5.fix_dir() / FIX_FILE) == doc


def test_run5_input_is_stored_in_the_shape_publish_reads(run5: Case) -> None:
    """Ruling I paths and Ruling O ``build``; the gate's values filled in."""
    run5.set_build(FROM_STDOUT)
    stored = run5.run().input
    assert Path(stored.root).is_absolute() and Path(stored.clone).is_absolute()
    assert not Path(stored.try_dir).is_absolute()
    assert stored.source_dir == str(Path(stored.try_dir).parent.parent / "source")
    assert stored.build == {
        "dockerfile": "Dockerfile",
        "build_args": [],
        "platform": None,
        "tag": None,
    }
    assert stored.clean and stored.origin == "example/project"
    assert stored.head == git(run5.s.clone, "rev-parse", "HEAD")
    assert stored.target["head_sha"] == stored.head
    assert len(stored.evidence) == 2
    assert stored.workflow_path == ".github/workflows/diagnosis-polygon.yml"


def test_a_second_fix_gets_a_new_directory_and_branch(run5: Case) -> None:
    """A fix directory is never reused; the earlier one stays as evidence."""
    first = run5.run()
    second = run5.run()
    assert first.fix_id != second.fix_id
    assert (run5.fix_dir(1) / FIX_FILE).is_file()
    assert (run5.fix_dir(2) / FIX_FILE).is_file()
    assert first.publication is not None and second.publication is not None
    assert first.publication.branch.endswith("-001")
    assert second.publication.branch.endswith("-002")


# --- run-1: the envelope and the model -----------------------------------------


def test_run1_ambiguous_stops_before_the_model(ambiguous: Case) -> None:
    """Two eligible ``setup.md`` files: the basename floor, no model call."""
    doc = ambiguous.run()
    assert doc.status == "stopped"
    assert doc.stop_reason == "fix method not established"
    assert doc.stop_detail is not None
    assert doc.stop_detail.startswith("5 basename floor: 2 eligible files")
    assert ambiguous.chooser.prompts == []
    assert doc.proposal is None and doc.local_proof is None
    assert ambiguous.builds() == []


def test_run1_unique_with_the_seam_is_locally_confirmed(unique: Case) -> None:
    """The envelope passes, the prompt lists every eligible blob, the fake
    model picks the one ``setup.md``, and the commit holds that COPY."""
    unique.set_build(COPY_STDOUT)
    with enable_for_test("copy-passed/podman"):
        doc = unique.run()
    assert doc.status == "locally_confirmed", (doc.stop_reason, doc.stop_detail)
    assert len(unique.chooser.prompts) == 1
    prompt = unique.chooser.prompts[0]
    eligible = [
        UNIQUE,
        ".github/workflows/diagnosis-polygon.yml",
        "pyproject.toml",
        "src/ci_build/__init__.py",
        "tests/test_greeting.py",
        "uv.lock",
    ]
    assert all(f"- {path}\n" in prompt for path in eligible)
    assert doc.proposal is not None
    assert doc.proposal.transformation == "copy-source"
    assert doc.proposal.rationale == json.loads(_answer(UNIQUE))["rationale"]
    assert doc.proposal.replacement.strip() == COPY_LINE
    assert doc.publication is not None and doc.publication.fix_commit is not None
    worktree = Path(doc.publication.worktree)
    committed = git(worktree, "show", f"{doc.publication.fix_commit}:Dockerfile")
    assert COPY_LINE in committed.splitlines()
    assert "COPY docs/setup.md ./setup.md" not in committed


def test_run1_unique_locally_confirmed_by_default(unique: Case) -> None:
    """Production rows: the recording-backed local COPY row confirms the
    model's proposal without the seam."""
    unique.set_build(COPY_STDOUT)
    doc = unique.run()
    assert doc.status == "locally_confirmed", (doc.stop_reason, doc.stop_detail)
    assert (doc.stop_reason, doc.stop_detail) == (None, None)
    assert len(unique.chooser.prompts) == 1


def test_a_malformed_model_answer_stops_after_one_call(unique: Case) -> None:
    """One attempt, no retry; the local proof never runs."""
    unique.chooser.answer = "I think docs/guide/setup.md"
    doc = unique.run()
    assert doc.stop_reason == "fix method not established"
    assert doc.stop_detail is not None
    assert doc.stop_detail.startswith("malformed answer:")
    assert len(unique.chooser.prompts) == 1
    assert unique.builds() == []


def test_a_failing_model_call_is_a_reason(unique: Case) -> None:
    """The backend raising is a stop, not a traceback, and not retried."""
    unique.chooser.answer = RuntimeError("rate limited")
    doc = unique.run()
    assert doc.stop_reason == "fix method not established"
    assert doc.stop_detail == "the model call failed: RuntimeError: rate limited"
    assert len(unique.chooser.prompts) == 1


# --- commit preconditions ------------------------------------------------------


def test_no_signing_key_blocks_the_commit(run5: Case) -> None:
    """No key: ``commit blocked`` at the preconditions, before the proposal."""
    doc = run5.run(no_key=True)
    assert doc.stop_reason == "commit blocked"
    assert doc.stop_detail == "preflight in the worktree: no signing key given"
    assert doc.proposal is None
    assert run5.builds() == []


def _narrow_ignore(case: Case, text: str) -> None:
    """Commit a ``.dockerignore`` of ``text`` to the clone (the admitted head
    moves with it) — an exclusion that would need an ignore-file edit."""
    (case.s.clone / ".dockerignore").write_text(text)
    restored_at(case.s.r.attempt_dir, commit_all(case.s.clone, "narrow ignore"))


def test_preliminary_exclusion_needing_an_edit_blocks(run5: Case) -> None:
    """``.deployer`` not excluded: stop before the proposal, nothing written."""
    _narrow_ignore(run5, "*.log\n")
    doc = run5.run()
    assert doc.stop_reason == "commit blocked"
    assert doc.stop_detail is not None
    assert doc.stop_detail.startswith("preliminary exclusion check: ")
    assert doc.proposal is None
    assert doc.publication is not None
    worktree = Path(doc.publication.worktree)
    assert _status(worktree) == ""
    assert (worktree / ".dockerignore").read_text() == "*.log\n"


def test_final_exclusion_needing_an_edit_blocks(run5: Case) -> None:
    """A rule covering the pointer and the placeholder set but not the real
    one passes the preliminary check and stops at the final one, before the
    corrected Dockerfile or any set file is written."""
    placeholder = f"{SET_ROOT}/Dockerfile/{author_mod.PLACEHOLDER_SET}"
    rules = f"{SET_ROOT}/{POINTER}\n{placeholder}\n"
    _narrow_ignore(run5, rules)
    run5.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"):
        doc = run5.run()
    assert doc.stop_reason == "commit blocked"
    assert doc.stop_detail is not None
    assert doc.stop_detail.startswith("final exclusion check: ")
    assert doc.local_proof is not None
    assert doc.publication is not None
    worktree = Path(doc.publication.worktree)
    assert _status(worktree) == ""
    assert (worktree / ".dockerignore").read_text() == rules


# --- errors and exit 2 ---------------------------------------------------------


def test_a_save_failure_after_the_commit_names_the_identifiers(
    run5: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 after the commit: fix dir, worktree, branch and commit named."""
    real_save = author_mod.save

    def failing(doc: FixDocument, path: Path) -> None:
        if doc.status == "locally_confirmed":
            raise OSError("disk full")
        real_save(doc, path)

    monkeypatch.setattr(author_mod, "save", failing)
    run5.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"), pytest.raises(FixAbort) as caught:
        run5.run()
    message = str(caught.value)
    worktree = run5.fix_dir() / "worktree"
    branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    commit = git(worktree, "rev-parse", "HEAD")
    assert "OSError: disk full" in message
    for identifier in (str(run5.fix_dir()), str(worktree), branch, commit):
        assert identifier in message
    assert load(run5.fix_dir() / FIX_FILE).status == "in_progress"


def _root_inside_clone(case: Case, ignored: bool) -> Path:
    """R's root copied under the clone (``.work``), optionally ignored via
    ``info/exclude`` so the clone stays clean."""
    inside = case.s.clone / ".work"
    shutil.copytree(case.s.r.root, inside, symlinks=True)
    if ignored:
        exclude = case.s.clone / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + ".work/\n")
    return inside


@pytest.mark.parametrize("ignored", [True, False])
def test_a_fix_location_inside_the_clone_exits_2_creating_nothing(
    run5: Case, ignored: bool
) -> None:
    """Ruling Q: the fix location is checked before anything is written —
    ignored or not (then the gate would say ``no admission``), exit 2 and no
    fix directory, no ``fix.json``."""
    inside = _root_inside_clone(run5, ignored)
    attempt = inside / run5.s.r.attempt_dir.relative_to(run5.s.r.root)
    with pytest.raises(FixAbort, match="is inside the clone"):
        run5.run(root=inside)
    assert not (attempt / "fixes").exists()
    assert not run5.fix_dir().exists()


def test_an_unreadable_verdict_exits_2(run5: Case, tmp_path: Path) -> None:
    """The verdict cannot be read: exit 2, no fix directory created."""
    with pytest.raises(FixAbort, match="cannot read the verdict"):
        author_fix(
            tmp_path / "absent.json",
            run5.s.clone,
            run5.s.r.root,
            run5.s.env,
            run5.s.key,
            run5.chooser,
            PODMAN,
            60,
        )
    assert not (run5.s.r.attempt_dir / "fixes").exists()


def test_an_injected_exception_becomes_a_reason(
    run5: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception inside a step is its stop reason, never a traceback."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(author_mod, "propose_from", boom)
    doc = run5.run()
    assert (doc.stop_reason, doc.stop_detail) == ("no proposal", "RuntimeError: boom")
    assert load(run5.fix_dir() / FIX_FILE) == doc


def test_a_revoked_key_is_no_admission(run5: Case) -> None:
    """The gate refuses: stopped before any worktree exists."""
    from deployer.provenance import trust

    trust.revoke(run5.s.trust, run5.s.pub)
    doc = run5.run()
    assert doc.stop_reason == "no admission"
    assert doc.publication is None
    assert not (run5.fix_dir() / "worktree").exists()
    assert not doc.input.clean and doc.input.target == {}


def test_no_container_runtime_is_no_local_confirmation(run5: Case) -> None:
    """No runtime resolved: the proof cannot run."""
    doc = run5.run(rt=None)
    assert (doc.stop_reason, doc.stop_detail) == (
        "no local confirmation",
        "no container runtime found",
    )


def test_a_docker_backend_is_no_local_confirmation(run5: Case) -> None:
    """``--container-tool docker`` differs from R's Podman backend."""
    doc = run5.run(rt=ContainerRuntime(tool="docker"))
    assert (doc.stop_reason, doc.stop_detail) == (
        "no local confirmation",
        "local backend differs from R's",
    )
    assert run5.builds() == []


# --- fix round 1 regressions -------------------------------------------------


def _verdict_with(case: Case, **reproduction: object) -> Path:
    """The case's verdict with ``reproduction`` fields replaced."""
    document = case.s.document()
    document["reproduction"].update(reproduction)
    path = case.tmp / "changed.json"
    path.write_text(json.dumps(document))
    return path


def _run_on(case: Case, verdict: Path) -> FixDocument:
    """``author_fix`` over ``verdict`` with the case's defaults."""
    return author_fix(
        verdict,
        case.s.clone,
        case.s.r.root,
        case.s.env,
        case.s.key,
        case.chooser,
        PODMAN,
        60,
    )


def test_a_deeply_nested_verdict_exits_2(run5: Case) -> None:
    """``json.loads`` recursing too deep is exit 2, not a traceback."""
    deep = run5.tmp / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(FixAbort, match="is not JSON"):
        _run_on(run5, deep)
    assert not (run5.s.r.attempt_dir / "fixes").exists()


@pytest.mark.parametrize(
    "try_dir",
    [
        "a\u0000b/tries/001",
        "a\nb/tries/001",
        "x",
        "attempt-1/001",
        "a/tries/x01",
    ],
)
def test_a_malformed_try_dir_exits_2_creating_nothing(run5: Case, try_dir: str) -> None:
    """Control characters (a NUL made ``mkdir`` raise ``ValueError``) and a
    shape other than ``<attempt>/tries/<NNN>`` are exit 2 before any write."""
    before = sorted(run5.s.r.root.rglob("fixes"))
    with pytest.raises(FixAbort):
        _run_on(run5, _verdict_with(run5, try_dir=try_dir))
    assert sorted(run5.s.r.root.rglob("fixes")) == before


def test_new_fix_dir_turns_a_value_error_into_fix_dir_error(tmp_path: Path) -> None:
    """A path the OS cannot take (an embedded NUL) is ``FixDirError``."""
    with pytest.raises(FixDirError):
        new_fix_dir(tmp_path / "a\u0000b")


def test_an_unsynced_index_is_recorded(
    run5: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling P: ``Committed.index_synced`` lands in ``publication``."""
    real = author_mod.commit

    def unsynced(worktree: Path, message: str, vetted: Vetted) -> Committed:
        done = real(worktree, message, vetted)
        return dataclasses.replace(done, index_synced=False, detail="index.lock")

    monkeypatch.setattr(author_mod, "commit", unsynced)
    run5.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"):
        doc = run5.run()
    assert doc.status == "locally_confirmed"
    assert doc.publication is not None and doc.publication.index_synced is False
    assert doc.last_operation is not None
    assert doc.last_operation.reason == (
        "the worktree index was not synced: index.lock"
    )
    assert load(run5.fix_dir() / FIX_FILE) == doc


def _executable(r: Replayed) -> None:
    """The admitted Dockerfile is committed as ``100755``."""
    (r.unlock() / "Dockerfile").chmod(0o755)


def test_an_executable_dockerfile_keeps_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R: the corrected Dockerfile is written with the original mode,
    so a ``100755`` Dockerfile reaches ``locally_confirmed`` and stays so."""
    case = _case(tmp_path, monkeypatch, "run-5", _executable)
    head = git(case.s.clone, "rev-parse", "HEAD")
    assert git(case.s.clone, "ls-tree", head, "Dockerfile").startswith("100755")
    case.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"):
        doc = case.run()
    assert doc.status == "locally_confirmed", (doc.stop_reason, doc.stop_detail)
    assert doc.publication is not None and doc.publication.fix_commit is not None
    worktree = Path(doc.publication.worktree)
    listed = git(worktree, "ls-tree", doc.publication.fix_commit, "Dockerfile")
    assert listed.startswith("100755")
    assert os.stat(worktree / "Dockerfile").st_mode & 0o777 == 0o755


def test_f1_receives_every_build_arg_pair(
    run5: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling S: the orchestrator passes R's pairs, not a last-wins dict."""
    seen: list[object] = []
    real = author_mod.propose_from

    def spy(parsed: Any, bound: Any, build_args: Any, dockerfile: bytes) -> Any:
        seen.append(build_args)
        return real(parsed, bound, build_args, dockerfile)

    monkeypatch.setattr(author_mod, "propose_from", spy)
    run5.run()
    assert seen == [()]  # run-5's build line has no build args: R's tuple


def test_a_stop_after_the_write_says_the_file_was_written(
    run5: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An issuing failure after the corrected bytes reached the worktree
    names that in the stop detail."""
    monkeypatch.setattr(
        author_mod, "issue", lambda *args, **kwargs: Issued(False, "boom", None)
    )
    run5.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"):
        doc = run5.run()
    worktree = run5.fix_dir() / "worktree"
    assert doc.stop_reason == "commit blocked"
    assert doc.stop_detail == (
        "issuing failed: boom; the corrected Dockerfile was already written "
        f"into the worktree {worktree}"
    )
    assert _status(worktree) == "M Dockerfile"


def test_the_fix_path_is_announced_only_after_the_first_save(
    run5: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed first save is exit 2 and ``on_document`` is never told."""

    def failing(doc: FixDocument, path: Path) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(author_mod, "save", failing)
    announced: list[Path] = []
    with pytest.raises(FixAbort, match="read-only"):
        author_fix(
            run5.verdict(),
            run5.s.clone,
            run5.s.r.root,
            run5.s.env,
            run5.s.key,
            run5.chooser,
            PODMAN,
            60,
            on_document=announced.append,
        )
    assert announced == []
