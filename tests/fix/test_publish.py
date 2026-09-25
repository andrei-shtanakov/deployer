"""``deployer fix publish`` (F §1, §8.2-§8.4, Task 14).

The ``locally_confirmed`` document is reached through ``deployer fix`` itself
with the template seam enabled. ``origin`` is a local bare repository whose
path ends in ``example/project.git`` (the slug the fix stored), so the real
guarded Git runs fetch, ls-remote and push without any network; failures
are injected by a wrapper. ``gh`` is always a fake.
"""

import json
import os
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from deployer.fix.author import FIX_FILE
from deployer.fix.document import CiAttempt, FixDocument, load, save
from deployer.fix.publish import (
    PUBLISHED,
    REFUSED,
    GitRemoteError,
    PublishAbort,
    SubprocessGitRemote,
    pr_text,
    publish,
)
from deployer.forge import GhError
from deployer.provenance import trust
from deployer.provenance.model import SET_ROOT
from tests.fix.conftest import enable_for_test, git
from tests.fix.test_author import FROM_STDOUT, Case, _case
from tests.provenance.conftest import make_key

BASE = "main"
REPO = "example/project"


@dataclass
class Remote(SubprocessGitRemote):
    """The real guarded Git against the local bare ``origin``, recording
    each call; a method named in ``fail`` raises instead."""

    calls: list[str] = field(default_factory=list)
    fail: set[str] = field(default_factory=set)

    def _enter(self, name: str) -> None:
        """Record the call; raise if it is to fail."""
        self.calls.append(name)
        if name in self.fail:
            raise GitRemoteError(f"git {name} failed: injected")

    def fetch(self, repo_dir: Path, branch: str) -> str:
        """Recorded :meth:`SubprocessGitRemote.fetch`."""
        self._enter("fetch")
        return super().fetch(repo_dir, branch)

    def merge_base(self, repo_dir: Path, a: str, b: str) -> str:
        """Recorded :meth:`SubprocessGitRemote.merge_base`."""
        self._enter("merge_base")
        return super().merge_base(repo_dir, a, b)

    def diff_names(self, repo_dir: Path, a: str, b: str) -> list[tuple[str, str]]:
        """Recorded :meth:`SubprocessGitRemote.diff_names`."""
        self._enter("diff_names")
        return super().diff_names(repo_dir, a, b)

    def remote_tip(self, repo_dir: Path, branch: str) -> str | None:
        """Recorded :meth:`SubprocessGitRemote.remote_tip`."""
        self._enter("remote_tip")
        return super().remote_tip(repo_dir, branch)

    def push(self, repo_dir: Path, branch: str, commit: str) -> None:
        """Recorded :meth:`SubprocessGitRemote.push`."""
        self._enter("push")
        super().push(repo_dir, branch, commit)


@dataclass
class FakeGh:
    """GitHub's pulls endpoint in memory: open PRs, lookups and creations.

    ``create_times_out`` creates the PR and then raises, as a network
    timeout after the server acted would."""

    prs: list[dict[str, object]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    created: list[dict[str, str]] = field(default_factory=list)
    create_times_out: bool = False
    missing: bool = False
    head_sha: dict[str, str] = field(default_factory=dict)

    def api(self, argv: list[str], *, timeout: float) -> str:
        """Answer a PR listing or a PR creation."""
        self.calls.append(argv)
        if "POST" in argv:
            return self._create(argv)
        if self.missing:
            raise GhError("gh api could not start: [Errno 2] No such file: 'gh'")
        query = parse_qs(urlsplit(argv[0]).query)
        head = query["head"][0].split(":", 1)[1]
        state = query["state"][0]
        return json.dumps(
            [
                pr
                for pr in self.prs
                if _head(pr)["ref"] == head and state in ("all", pr["state"])
            ]
        )

    def _create(self, argv: list[str]) -> str:
        """Create a PR from the ``-f`` fields."""
        fields = dict(
            value.split("=", 1) for flag, value in zip(argv, argv[1:]) if flag == "-f"
        )
        self.created.append(fields)
        pr = self.pr(fields["head"], self.head_sha[fields["head"]], fields["base"])
        self.prs.append(pr)
        if self.create_times_out:
            raise GhError("gh api repos/x/pulls timed out after 30.0s")
        return json.dumps(pr)

    def pr(
        self,
        branch: str,
        sha: str,
        base: str,
        state: str = "open",
        repo: str = REPO,
    ) -> dict[str, object]:
        """A PR listing entry."""
        number = len(self.prs) + 1
        return {
            "number": number,
            "state": state,
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}},
            "base": {"ref": base},
        }


def _head(pr: dict[str, object]) -> dict[str, object]:
    """A PR's ``head`` mapping."""
    head = pr["head"]
    assert isinstance(head, dict)
    return head


@dataclass
class Published:
    """A ``locally_confirmed`` fix, its bare ``origin`` and the fakes."""

    case: Case
    doc_path: Path
    bare: Path
    remote: Remote
    gh: FakeGh

    @property
    def doc(self) -> FixDocument:
        """The document as saved now."""
        return load(self.doc_path)

    @property
    def branch(self) -> str:
        """The fix branch."""
        publication = self.doc.publication
        assert publication is not None
        return publication.branch

    @property
    def commit(self) -> str:
        """The stored fix commit."""
        publication = self.doc.publication
        assert publication is not None and publication.fix_commit is not None
        return publication.fix_commit

    @property
    def worktree(self) -> Path:
        """The fix worktree."""
        publication = self.doc.publication
        assert publication is not None
        return Path(publication.worktree)

    def run(self, base: str = BASE) -> FixDocument:
        """``publish`` with this case's fakes."""
        return publish(self.doc_path, base, self.case.s.env, self.remote, self.gh)

    def remote_ref(self, branch: str) -> str | None:
        """What the bare ``origin`` has at ``refs/heads/<branch>``."""
        proc = subprocess.run(
            ["git", "-C", str(self.bare), "rev-parse", "--verify", "--quiet", branch],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout.strip() or None

    def rewrite(self, **fields: object) -> None:
        """Re-save the document with ``fields`` replaced."""
        save(self.doc.model_copy(update=fields), self.doc_path)


@pytest.fixture()
def pub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Published]:
    """run-5 ``locally_confirmed``; ``origin`` a bare repo with ``main`` at
    ``head_sha``."""
    yield _published(tmp_path, monkeypatch)


def _published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fix_key: Path | None = None
) -> Published:
    """:func:`pub`, the fix's set signed with ``fix_key`` when given."""
    case = _case(tmp_path, monkeypatch, "run-5")
    if fix_key is not None:
        case.s.key = fix_key
    case.set_build(FROM_STDOUT)
    with enable_for_test("from-parsed/podman"):
        doc = case.run()
    assert doc.status == "locally_confirmed", (doc.stop_reason, doc.stop_detail)
    bare = tmp_path / "remote" / "example" / "project.git"
    bare.mkdir(parents=True)
    git(bare, "init", "-q", "--bare")
    clone = case.s.clone
    git(clone, "remote", "set-url", "origin", str(bare))
    git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}")
    gh = FakeGh()
    publication = doc.publication
    assert publication is not None and publication.fix_commit is not None
    gh.head_sha[publication.branch] = publication.fix_commit
    return Published(case, case.fix_dir() / FIX_FILE, bare, Remote(), gh)


def _put(pub: Published, commit: str, branch: str) -> None:
    """Set the bare ``origin``'s ``branch`` to ``commit`` (objects included)."""
    ref = f"{commit}:refs/heads/{branch}"
    git(pub.worktree, "push", "-q", "--no-verify", "--force", str(pub.bare), ref)


def _refused(doc: FixDocument, reason: str) -> None:
    """``doc`` is a recorded refusal whose reason contains ``reason``."""
    last = doc.last_operation
    assert last is not None and last.command == "publish"
    assert last.result == REFUSED, last
    assert last.reason is not None and reason in last.reason, last.reason


def _ok(doc: FixDocument) -> None:
    """``doc`` is a recorded publication."""
    last = doc.last_operation
    assert last is not None and last.result == PUBLISHED, last


# --- the happy path -----------------------------------------------------------


def test_publish_pushes_and_creates_the_pr(pub: Published) -> None:
    """``locally_confirmed`` → ``fix_proposed``; the branch is on ``origin``
    at the fix commit; one PR created with the deterministic text."""
    doc = pub.run()
    _ok(doc)
    assert doc.status == "fix_proposed"
    assert doc.publication is not None
    assert doc.publication.base == BASE
    assert doc.publication.pr_url == f"https://github.com/{REPO}/pull/1"
    assert pub.remote_ref(pub.branch) == pub.commit
    assert len(pub.gh.created) == 1
    title, body = pr_text(doc)
    assert pub.gh.created[0] == {
        "head": pub.branch,
        "base": BASE,
        "title": title,
        "body": body,
    }
    assert pub.doc == doc
    assert pub.remote.calls == [
        "fetch",
        "merge_base",
        "diff_names",
        "remote_tip",
        "push",
    ]


def test_a_moved_or_dirty_clone_does_not_matter(pub: Published) -> None:
    """The user's clone ``HEAD`` moved and its tree dirty after ``deployer
    fix``: publish reads the worktree and the stored inputs only."""
    clone = pub.case.s.clone
    (clone / "new.txt").write_text("more\n")
    git(clone, "add", "new.txt")
    git(clone, "commit", "-q", "-m", "moved on")
    (clone / "untracked.txt").write_text("dirty\n")
    (clone / "Dockerfile").write_text("FROM scratch\n")
    doc = pub.run()
    _ok(doc)
    assert doc.status == "fix_proposed"


def test_a_base_that_moved_on_from_head_is_fine(pub: Published) -> None:
    """A base with commits after ``head_sha`` still has ``head_sha`` as the
    merge base, and the PR's diff is exactly the fix."""
    clone = pub.case.s.clone
    (clone / "later.txt").write_text("later\n")
    git(clone, "add", "later.txt")
    git(clone, "commit", "-q", "-m", "later")
    git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}")
    _ok(pub.run())


def test_hooks_never_run(pub: Published) -> None:
    """A pre-push (and every other) hook in the clone does not run."""
    marker = pub.case.tmp / "hook-ran"
    hooks = Path(git(pub.case.s.clone, "rev-parse", "--git-common-dir"))
    if not hooks.is_absolute():
        hooks = pub.case.s.clone / hooks
    for name in ("pre-push", "reference-transaction", "post-checkout"):
        hook = hooks / "hooks" / name
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    _ok(pub.run())
    assert not marker.exists()


# --- repeats and idempotence --------------------------------------------------


def test_a_repeat_finds_the_pr_and_keeps_fix_proposed(pub: Published) -> None:
    """A second publish re-verifies, pushes nothing, creates nothing."""
    first = pub.run()
    pub.remote.calls.clear()
    second = pub.run()
    _ok(second)
    assert second.status == "fix_proposed"
    assert second.publication == first.publication
    assert len(pub.gh.created) == 1
    assert "push" not in pub.remote.calls


def test_a_repeat_on_ci_confirmed_keeps_it(pub: Published) -> None:
    """``ci_confirmed`` is kept on a successful repeat."""
    pub.run()
    attempt = CiAttempt(
        at="2026-09-25T00:00:00+00:00",
        considered=[],
        outcome="ci_confirmed",
        reason=None,
        evidence=[],
    )
    pub.rewrite(status="ci_confirmed", ci_attempts=[attempt])
    doc = pub.run()
    _ok(doc)
    assert doc.status == "ci_confirmed"
    assert doc.ci_attempts == [attempt]
    assert len(pub.gh.created) == 1


def test_a_create_timeout_then_repeat_finds_the_pr(pub: Published) -> None:
    """The server made the PR but the call timed out: refused, status kept;
    the repeat finds the PR and never creates a second one."""
    pub.gh.create_times_out = True
    first = pub.run()
    _refused(first, "PR creation failed")
    assert first.status == "locally_confirmed"
    assert first.publication is not None and first.publication.pr_url is None
    pub.gh.create_times_out = False
    second = pub.run()
    _ok(second)
    assert second.status == "fix_proposed"
    assert second.publication is not None
    assert second.publication.pr_url == f"https://github.com/{REPO}/pull/1"
    assert len(pub.gh.created) == 1


def test_a_different_base_on_repeat_is_refused_before_the_network(
    pub: Published,
) -> None:
    """The stored base wins; no Git or ``gh`` network call is made."""
    pub.run()
    pub.remote.calls.clear()
    pub.gh.calls.clear()
    doc = pub.run("develop")
    _refused(doc, "published against base 'main', not 'develop'")
    assert doc.status == "fix_proposed"
    assert doc.publication is not None and doc.publication.base == BASE
    assert pub.remote.calls == [] and pub.gh.calls == []


def test_a_push_failure_on_a_repeat_leaves_fix_proposed(pub: Published) -> None:
    """The remote branch is gone and the push fails: ``fix_proposed`` kept."""
    pub.run()
    git(pub.bare, "update-ref", "-d", f"refs/heads/{pub.branch}")
    pub.remote.fail = {"push"}
    doc = pub.run()
    _refused(doc, "push of")
    assert doc.status == "fix_proposed"
    assert doc.publication is not None and doc.publication.pr_url is not None


def test_a_push_failure_on_the_first_publish_keeps_locally_confirmed(
    pub: Published,
) -> None:
    """The PRs are looked up before the push; none is created after it
    failed."""
    pub.remote.fail = {"push"}
    doc = pub.run()
    _refused(doc, "injected")
    assert doc.status == "locally_confirmed"
    assert len(pub.gh.calls) == 1 and pub.gh.created == []


def test_a_remote_branch_at_another_commit_is_refused(pub: Published) -> None:
    """No force: a remote fix branch elsewhere is never overwritten."""
    head = pub.case.s.document()["admission"]["binding"]["head_sha"]
    git(pub.bare, "update-ref", f"refs/heads/{pub.branch}", head)
    doc = pub.run()
    _refused(doc, f"the remote branch {pub.branch} is at {head}")
    assert pub.remote_ref(pub.branch) == head
    assert pub.gh.created == []


def test_a_remote_branch_at_the_fix_commit_is_reused(pub: Published) -> None:
    """An earlier push that landed: nothing is pushed again."""
    _put(pub, pub.commit, pub.branch)
    _ok(pub.run())
    assert "push" not in pub.remote.calls


@pytest.mark.parametrize(("sha", "base"), [("other", BASE), ("fix", "develop")])
def test_an_open_pr_with_another_head_or_base_is_refused(
    pub: Published, sha: str, base: str
) -> None:
    """A PR on the fix branch that is not the fix commit against the base."""
    head = pub.commit if sha == "fix" else "0" * 40
    pub.gh.prs.append(pub.gh.pr(pub.branch, head, base))
    doc = pub.run()
    _refused(doc, "an open PR")
    assert doc.status == "locally_confirmed"
    assert pub.gh.created == []


def test_a_recorded_pr_that_is_no_longer_open_is_not_replaced(
    pub: Published,
) -> None:
    """A published fix whose PR vanished from the listing: refused, no second
    PR."""
    pub.run()
    pub.gh.prs.clear()
    doc = pub.run()
    _refused(doc, "is not open with the fix commit")
    assert len(pub.gh.created) == 1


def test_a_repeat_after_a_squash_merge_is_refused_without_a_push(
    pub: Published,
) -> None:
    """The PR was merged (closed) and its branch deleted: the lookup refuses
    before anything is pushed again."""
    pub.run()
    pub.gh.prs[0]["state"] = "closed"
    git(pub.bare, "update-ref", "-d", f"refs/heads/{pub.branch}")
    pub.remote.calls.clear()
    doc = pub.run()
    _refused(doc, "is not open with the fix commit")
    assert doc.status == "fix_proposed"
    assert "push" not in pub.remote.calls and "remote_tip" not in pub.remote.calls
    assert pub.remote_ref(pub.branch) is None
    assert len(pub.gh.created) == 1


def test_an_edited_pr_url_is_refused_without_a_push(pub: Published) -> None:
    """A recorded ``pr_url`` that is not the open PR: refused before the
    push."""
    pub.run()
    publication = pub.doc.publication
    assert publication is not None
    elsewhere = f"https://github.com/{REPO}/pull/99"
    pub.rewrite(publication=publication.model_copy(update={"pr_url": elsewhere}))
    git(pub.bare, "update-ref", "-d", f"refs/heads/{pub.branch}")
    pub.remote.calls.clear()
    _refused(pub.run(), f"the recorded PR {elsewhere}")
    assert "push" not in pub.remote.calls
    assert pub.remote_ref(pub.branch) is None


def test_a_missing_gh_pushes_nothing(pub: Published) -> None:
    """``gh`` cannot run: the lookup fails before the push."""
    pub.gh.missing = True
    doc = pub.run()
    _refused(doc, "PR lookup failed")
    assert doc.status == "locally_confirmed"
    assert "push" not in pub.remote.calls
    assert pub.remote_ref(pub.branch) is None


def test_a_closed_pr_with_the_fix_commit_is_not_duplicated(pub: Published) -> None:
    """Nothing recorded, but a closed PR already carried the fix commit."""
    pub.gh.prs.append(pub.gh.pr(pub.branch, pub.commit, BASE, state="closed"))
    doc = pub.run()
    _refused(doc, "a closed PR")
    assert pub.gh.created == [] and "push" not in pub.remote.calls


def test_a_closed_pr_with_another_commit_is_ignored(pub: Published) -> None:
    """An old closed PR on the branch name for another commit is no reason
    to refuse."""
    pub.gh.prs.append(pub.gh.pr(pub.branch, "0" * 40, BASE, state="closed"))
    _ok(pub.run())
    assert len(pub.gh.created) == 1


def test_an_open_pr_from_another_repository_is_refused(pub: Published) -> None:
    """A PR whose head is a fork's branch of the same name is not reused."""
    pub.gh.prs.append(pub.gh.pr(pub.branch, pub.commit, BASE, repo="evil/fork"))
    _refused(pub.run(), "an open PR")
    assert "push" not in pub.remote.calls


# --- refusals before the network ---------------------------------------------


def test_a_renamed_branch_is_refused_before_the_network(pub: Published) -> None:
    """``publication.branch`` edited to another branch at the fix commit."""
    git(pub.worktree, "branch", "evil", pub.commit)
    publication = pub.doc.publication
    assert publication is not None
    pub.rewrite(publication=publication.model_copy(update={"branch": "evil"}))
    doc = pub.run()
    _refused(doc, "the stored branch 'evil' is not the fix's")
    assert pub.remote.calls == [] and pub.gh.calls == []
    assert pub.remote_ref("evil") is None


@pytest.mark.parametrize("what", ["verdict", "evidence"])
def test_tampered_stored_inputs_are_refused_before_the_network(
    pub: Published, what: str
) -> None:
    """The verdict or an evidence file changed since ``deployer fix``."""
    stored = pub.doc.input
    path = Path(stored.verdict.path if what == "verdict" else stored.evidence[0].path)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b" ")
    doc = pub.run()
    _refused(doc, "has changed")
    assert pub.remote.calls == [] and pub.gh.calls == []


def test_the_fetch_does_not_update_the_remote_tracking_base(pub: Published) -> None:
    """``--refmap=``: ``refs/remotes/origin/main`` is not created by the
    fetch (the push's own tracking ref for the fix branch is documented)."""
    clone = pub.case.s.clone
    git(clone, "update-ref", "-d", f"refs/remotes/origin/{BASE}")
    _ok(pub.run())
    tracking = git(clone, "for-each-ref", "--format=%(refname)", "refs/remotes/")
    assert f"refs/remotes/origin/{BASE}" not in tracking.splitlines()


def test_a_revoked_key_is_refused(pub: Published) -> None:
    """The current trust set decides: no network call after a revocation."""
    trust.revoke(pub.case.s.trust, pub.case.s.pub)
    doc = pub.run()
    _refused(doc, "admission re-check")
    assert doc.status == "locally_confirmed"
    assert pub.remote.calls == [] and pub.gh.calls == []


def test_the_base_is_recorded_before_any_network_action(pub: Published) -> None:
    """A refusal after the base was recorded keeps it."""
    trust.revoke(pub.case.s.trust, pub.case.s.pub)
    doc = pub.run()
    assert doc.publication is not None and doc.publication.base == BASE
    assert pub.doc.publication is not None and pub.doc.publication.base == BASE


def test_an_amended_commit_in_the_worktree_is_refused(pub: Published) -> None:
    """A new commit on the fix branch: the tip is no longer the fix commit."""
    worktree = pub.worktree
    (worktree / "Dockerfile").write_text("FROM python:3.12-slim AS other\n")
    git(worktree, "add", "Dockerfile")
    git(worktree, "commit", "-q", "--no-verify", "-m", "amend")
    doc = pub.run()
    _refused(doc, "not the fix commit")
    assert doc.status == "locally_confirmed"
    assert pub.remote.calls == [] and pub.remote_ref(pub.branch) is None


def test_a_moved_branch_tip_is_refused(pub: Published) -> None:
    """The branch reset to ``head_sha``: tip changed."""
    head = pub.doc.input.head
    git(pub.worktree, "update-ref", f"refs/heads/{pub.branch}", head)
    _refused(pub.run(), f"is at {head}, not the fix commit")


def test_a_tampered_stored_replacement_is_refused(pub: Published) -> None:
    """The stored proposal no longer describes the committed instruction."""
    proposal = pub.doc.proposal
    assert proposal is not None
    changed = proposal.model_copy(update={"replacement": "FROM scratch AS extra\n"})
    pub.rewrite(proposal=changed)
    _refused(pub.run(), "not the stored replacement")


def test_a_tampered_local_proof_hash_is_refused(pub: Published) -> None:
    """The committed Dockerfile must be the locally proved one."""
    proof = pub.doc.local_proof
    assert proof is not None
    pub.rewrite(local_proof=proof.model_copy(update={"dockerfile_sha256": "0" * 64}))
    _refused(pub.run(), "not the locally proved one")


def _recommit(
    pub: Published,
    extra: str | None,
    keep_old_set: bool = False,
    replace: dict[str, bytes] | None = None,
) -> str:
    """A commit on ``head_sha`` with the fix commit's tree plus ``extra``
    (with ``keep_old_set``, ``head_sha``'s set files back; ``replace``
    overwriting paths); the branch and the stored fix commit re-pointed at
    it."""
    worktree, head = pub.worktree, pub.doc.input.head
    index = pub.case.tmp / "extra-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}

    def plumb(*args: str, data: bytes = b"") -> str:
        """``git`` in the worktree on the temporary index."""
        return (
            subprocess.run(
                ["git", "-C", str(worktree), *args],
                check=True,
                capture_output=True,
                input=data,
                env=env,
                timeout=60,
            )
            .stdout.decode()
            .strip()
        )

    blob = plumb("hash-object", "-w", "--stdin", data=b"extra\n")
    plumb("read-tree", pub.commit)
    entries = [] if extra is None else [f"100644,{blob},{extra}"]
    if keep_old_set:
        listed = plumb("ls-tree", "-r", head, "--", f"{SET_ROOT}/Dockerfile/")
        for row in listed.splitlines():
            meta, path = row.split("\t")
            mode, _, sha = meta.split()
            entries.append(f"{mode},{sha},{path}")
    for path, data in (replace or {}).items():
        sha = plumb("hash-object", "-w", "--stdin", data=data)
        entries.append(f"100644,{sha},{path}")
    assert entries
    cacheinfo = [arg for entry in entries for arg in ("--cacheinfo", entry)]
    plumb("update-index", "--add", *cacheinfo)
    tree = plumb("write-tree")
    new = plumb("commit-tree", tree, "-p", head, "-m", "tampered")
    git(worktree, "update-ref", f"refs/heads/{pub.branch}", new)
    publication = pub.doc.publication
    assert publication is not None
    pub.rewrite(publication=publication.model_copy(update={"fix_commit": new}))
    return new


def test_a_fix_commit_with_another_path_is_refused(pub: Published) -> None:
    """The fix commit object itself is checked against §3, not the stored
    ``diff_ok``."""
    _recommit(pub, "README.extra")
    _refused(pub.run(), "README.extra: A is not an allowed change")
    assert pub.remote.calls == []


def _set_file(pub: Published, name: str) -> str:
    """The path of ``name`` in the fix commit's new set."""
    pointer = git(pub.worktree, "show", f"{pub.commit}:{SET_ROOT}/Dockerfile.current")
    return f"{SET_ROOT}/{pointer}/{name}"


@pytest.mark.parametrize(
    ("name", "step"), [("record.json.sig", "step 3"), ("snapshot.json", "step 1")]
)
def test_a_forged_new_set_is_refused(pub: Published, name: str, step: str) -> None:
    """An amended fix commit whose signature or snapshot is garbage, with
    ``fix_commit`` edited to it: A's ownership check over the fix commit's
    own set refuses before any network call."""
    _recommit(pub, None, replace={_set_file(pub, name): b"garbage\n"})
    doc = pub.run()
    _refused(doc, f"its set is not confirmed at {step}")
    assert pub.remote.calls == [] and pub.gh.calls == []


def test_a_new_set_signed_by_a_key_revoked_since_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admission key stays trusted (the re-check passes); the fix's own
    set was signed by a second key, revoked after ``deployer fix``."""
    keys = tmp_path / "fix-keys"
    keys.mkdir()
    fix_key, fix_pub = make_key(keys, "fixkey")
    trust.add(tmp_path / "trust", fix_pub)
    pub = _published(tmp_path, monkeypatch, fix_key)
    trust.revoke(pub.case.s.trust, fix_pub)
    doc = pub.run()
    _refused(doc, "its set is not confirmed at step 3")
    assert pub.remote.calls == [] and pub.gh.calls == []


def test_a_fix_commit_keeping_an_old_set_is_refused(pub: Published) -> None:
    """§3 deletes every other set directory; one kept back is refused."""
    _recommit(pub, None, keep_old_set=True)
    _refused(pub.run(), "the deleted set files")


def test_a_fix_commit_that_is_not_one_commit_on_head_is_refused(
    pub: Published,
) -> None:
    """An extra commit between ``head_sha`` and the fix commit."""
    worktree = pub.worktree
    middle = git(
        worktree,
        "commit-tree",
        f"{pub.doc.input.head}^{{tree}}",
        "-p",
        pub.doc.input.head,
        "-m",
        "middle",
    )
    new = git(
        worktree, "commit-tree", f"{pub.commit}^{{tree}}", "-p", middle, "-m", "fix"
    )
    git(worktree, "update-ref", f"refs/heads/{pub.branch}", new)
    publication = pub.doc.publication
    assert publication is not None
    pub.rewrite(publication=publication.model_copy(update={"fix_commit": new}))
    _refused(pub.run(), "is not a single commit on")


def test_a_changed_origin_is_refused(pub: Published) -> None:
    """The worktree's ``origin`` must still be the stored repository."""
    other = pub.case.tmp / "remote" / "someone" / "else.git"
    git(pub.case.s.clone, "remote", "set-url", "origin", str(other))
    _refused(pub.run(), "not the stored example/project")


# --- the future PR's diff -----------------------------------------------------


def test_a_base_already_containing_the_fix_commit_is_refused(pub: Published) -> None:
    """The merge base would be the fix commit: an empty PR."""
    _put(pub, pub.commit, BASE)
    doc = pub.run()
    _refused(doc, "already contains the fix commit")
    assert pub.remote_ref(pub.branch) is None and pub.gh.calls == []


def test_a_base_missing_head_sha_is_refused(pub: Published) -> None:
    """An unrelated base has no merge base with the fix commit."""
    worktree = pub.worktree
    tree = git(worktree, "rev-parse", f"{pub.commit}^{{tree}}")
    orphan = git(worktree, "commit-tree", tree, "-m", "unrelated")
    _put(pub, orphan, BASE)
    doc = pub.run()
    _refused(doc, f"the base {BASE} is not usable")
    assert pub.remote_ref(pub.branch) is None


def test_a_pr_diff_differing_from_the_commit_is_refused(pub: Published) -> None:
    """``merge-base..fix_commit`` must be exactly the committed change."""

    def diff_names(repo_dir: Path, a: str, b: str) -> list[tuple[str, str]]:
        return [("M", "Dockerfile")]

    pub.remote.diff_names = diff_names  # type: ignore[method-assign]
    _refused(pub.run(), "is not the committed change")


def test_a_fetch_failure_is_refused(pub: Published) -> None:
    """A failed fetch: nothing pushed."""
    pub.remote.fail = {"fetch"}
    doc = pub.run()
    _refused(doc, "is not usable")
    assert pub.remote_ref(pub.branch) is None


# --- statuses, invocation and local I/O ---------------------------------------


def test_a_stopped_document_is_refused(pub: Published) -> None:
    """Only a confirmed document is publishable; the base is not recorded."""
    pub.rewrite(status="stopped", stop_reason="no proposal", stop_detail="x")
    doc = pub.run()
    _refused(doc, "status stopped is not publishable")
    assert doc.status == "stopped"
    assert doc.publication is not None and doc.publication.base is None


@pytest.mark.parametrize("base", ["", "-x", "a..b", "bad name"])
def test_an_invalid_base_is_refused_unrecorded(pub: Published, base: str) -> None:
    """Not a valid branch name: refused, nothing recorded or called."""
    doc = pub.run(base)
    _refused(doc, "is not a valid branch name")
    assert doc.publication is not None and doc.publication.base is None
    assert pub.remote.calls == []


def test_an_unreadable_document_aborts(tmp_path: Path) -> None:
    """``fix.json`` that cannot be read: exit 2."""
    with pytest.raises(PublishAbort, match="cannot read"):
        publish(tmp_path / "absent.json", BASE, {}, Remote(), FakeGh())


def test_a_save_failure_after_the_pr_names_it(
    pub: Published, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save failure after the push and the PR: exit 2 naming both."""
    from deployer.fix import publish as publish_mod

    real_save = publish_mod.save

    def failing(doc: FixDocument, path: Path) -> None:
        if doc.publication is not None and doc.publication.pr_url is not None:
            raise OSError("disk full")
        real_save(doc, path)

    monkeypatch.setattr(publish_mod, "save", failing)
    with pytest.raises(PublishAbort) as caught:
        pub.run()
    message = str(caught.value)
    assert "disk full" in message and pub.branch in message and "pull/1" in message


def test_an_exception_becomes_a_refusal(
    pub: Published, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing raises: an unexpected exception is a recorded refusal."""
    from deployer.fix import publish as publish_mod

    def boom(*args: object) -> None:
        raise RuntimeError("surprise")

    monkeypatch.setattr(publish_mod, "recheck_admission", boom)
    _refused(pub.run(), "RuntimeError: surprise")


# --- the PR text --------------------------------------------------------------


def test_the_pr_text_for_f1(pub: Published) -> None:
    """Class, transformation, both instructions, the deterministic rationale,
    the proof summary, the narrow claim and the CI caveat; no COPY caveat."""
    doc = pub.doc
    title, body = pr_text(doc)
    assert title == "deployer fix: from_argument_count in Dockerfile (lines 1-1)"
    assert "- class: `from_argument_count`" in body
    assert "- transformation: `F1`" in body
    assert "FROM python:3.12-slim extra" in body
    assert "FROM python:3.12-slim AS extra" in body
    assert "deterministic F1:" in body
    assert "local/podman/from: passed" in body
    assert "The diagnosed source error is removed locally." in body
    assert "CI confirmation is pending" in body
    assert "proposal for review" not in body
    assert f"Fix-Id: `{doc.fix_id}`" in body
    assert pr_text(doc) == (title, body)


def test_the_pr_text_for_a_copy_substitution(pub: Published) -> None:
    """A COPY substitution is marked a proposal for review; model text sits
    in a fence it cannot close; a later failure narrows the claim."""
    doc = pub.doc
    proposal, proof = doc.proposal, doc.local_proof
    assert proposal is not None and proof is not None
    rationale = [
        {
            "facts": [{"kind": "path", "ref": "docs/setup.md"}],
            "explanation": "@someone ```\n# heading",
        }
    ]
    doc = doc.model_copy(
        update={
            "proposal": proposal.model_copy(
                update={"transformation": "copy-source", "rationale": rationale}
            ),
            "local_proof": proof.model_copy(
                update={"later_failure": {"what": "another_instruction"}}
            ),
        }
    )
    _, body = pr_text(doc)
    assert "proposal for review" in body
    assert "model: @someone ```" in body
    assert "````text\nmodel: @someone" in body
    assert "- fact path: docs/setup.md" in body
    assert "does not claim that the substitution builds" in body
