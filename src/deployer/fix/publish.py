"""``deployer fix publish`` (design §1, §8.2-§8.4): push the fix branch and
create or find its PR, safely and repeatably.

The order is F §8.3's. Load ``fix.json``; the status must be
``locally_confirmed``, ``fix_proposed`` or ``ci_confirmed``; the chosen base
is recorded (an atomic save) **before any network action**, and a different
base stored earlier refuses. Then, locally: the gate's admission and trust
re-check over the stored inputs (:func:`recheck_admission`, never the
clone's ``HEAD`` or cleanliness); the fix branch's tip is the stored fix
commit, whose single parent is ``head_sha``; the fix **commit object's**
full diff against ``head_sha`` is §3's allowed change, bound to the stored
proposal, local proof and target; the branch is the one ``deployer fix``
names; the fix commit's own new set passes A's ownership check with the
current trust directory; the worktree's ``origin`` is the stored
repository. Then the network: the base is fetched and the future PR's diff
checked (``merge-base(base_tip, fix_commit) == head_sha`` and
``merge-base..fix_commit`` equal to the committed change); the branch's PRs
are looked up and checked against a recorded one **before** the push; the
branch is pushed without force (an existing remote branch at the fix commit
is reused, at any other commit refused); a PR is created only after that.

Success moves ``locally_confirmed`` to ``fix_proposed`` and keeps
``fix_proposed``/``ci_confirmed``. A refusal leaves the status unchanged
and is recorded in ``last_operation``. The whole operation, load through
the last save, holds ``fix.json``'s exclusive lock
(:func:`deployer.fix.document.exclusive`). Nothing raises except
:class:`PublishAbort` (exit 2): the lock is held elsewhere or cannot be
taken, or ``fix.json`` could not be read or saved.

Every Git command runs through ``fix.workspace``'s guarded chokepoint:
hooks, filters and fsmonitor are disabled, so publishing never runs a
user-configured program, and the push passes ``--no-verify`` as well.
"""

import hashlib
import json
import tempfile
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from deployer.admission.ownership import verify_ownership
from deployer.admission.prepare import decode_as_read_text
from deployer.fix.binding import Bound, link_problem
from deployer.fix.chooser import _fence
from deployer.fix.document import (
    FixDocument,
    LastOperation,
    LockError,
    Proposal,
    Publication,
    exclusive,
    load,
    save,
)
from deployer.fix.gate import recheck_admission
from deployer.fix.localproof import CLAIM
from deployer.fix.workspace import (
    _POINTER_PATH,
    _REGULAR_MODE,
    _SET_FILES,
    _SET_PREFIX,
    _decode,
    _guards,
    _is_set_file,
    _run,
    _set_parts,
    branch_name,
)
from deployer.forge import GH_TIMEOUT_S, GhRunner
from deployer.provenance import gitrepo
from deployer.provenance.model import (
    RECORD_FILE,
    SET_ROOT,
    SIGNATURE_FILE,
    SNAPSHOT_FILE,
    set_dir_name,
)
from deployer.provenance.trust import trust_dir
from deployer.reproduce.dockerfile import parse

PUBLISHABLE = ("locally_confirmed", "fix_proposed", "ci_confirmed")
PUBLISHED = "published"
REFUSED = "refused"
_REMOTE = "origin"
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}


class PublishAbort(Exception):
    """Exit 2 (§8.2): ``fix.json`` could not be locked, read or saved; the message
    names what was already done on the remote (§8.4)."""


class GitRemoteError(Exception):
    """A Git command of :class:`GitRemote` failed; the message says which."""


class GitRemote(Protocol):
    """The Git operations ``fix publish`` needs against ``origin`` and the
    fetched objects. Every method raises :class:`GitRemoteError`."""

    def fetch(self, repo_dir: Path, branch: str) -> str:
        """Fetch ``branch`` from ``origin``; its tip commit sha."""
        ...

    def merge_base(self, repo_dir: Path, a: str, b: str) -> str:
        """The merge base of commits ``a`` and ``b``."""
        ...

    def diff_names(self, repo_dir: Path, a: str, b: str) -> list[tuple[str, str]]:
        """``(status, path)`` per changed path from ``a`` to ``b``, no renames."""
        ...

    def remote_tip(self, repo_dir: Path, branch: str) -> str | None:
        """The commit ``origin``'s ``branch`` points at, ``None`` if absent."""
        ...

    def push(self, repo_dir: Path, branch: str, commit: str) -> None:
        """Push ``commit`` to ``origin``'s ``refs/heads/<branch>``, no force."""
        ...


class SubprocessGitRemote:
    """The real :class:`GitRemote`: ``git`` through the guarded chokepoint,
    with terminal prompts disabled; hooks never run."""

    def fetch(self, repo_dir: Path, branch: str) -> str:
        """``git fetch --no-tags --refmap= origin refs/heads/<branch>``, then
        ``FETCH_HEAD``'s commit. The empty ``--refmap`` keeps the fetch from
        updating ``refs/remotes/origin/<branch>`` opportunistically."""
        ref = f"refs/heads/{branch}"
        _git(
            repo_dir,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--refmap=",
            _REMOTE,
            ref,
        )
        tip = _git(repo_dir, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        return tip.decode().strip()

    def merge_base(self, repo_dir: Path, a: str, b: str) -> str:
        """``git merge-base a b``."""
        return _git(repo_dir, "merge-base", a, b).decode().strip()

    def diff_names(self, repo_dir: Path, a: str, b: str) -> list[tuple[str, str]]:
        """``git diff --name-status -z --no-renames a b``."""
        raw = _git(repo_dir, *_DIFF, "--name-status", a, b)
        fields = [field for field in raw.split(b"\0") if field]
        return [
            (status.decode(), _decode(path))
            for status, path in zip(fields[::2], fields[1::2], strict=True)
        ]

    def remote_tip(self, repo_dir: Path, branch: str) -> str | None:
        """``git ls-remote origin refs/heads/<branch>``, matched exactly."""
        ref = f"refs/heads/{branch}"
        out = _git(repo_dir, "ls-remote", _REMOTE, ref).decode()
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name == ref:
                return sha
        return None

    def push(self, repo_dir: Path, branch: str, commit: str) -> None:
        """``git push --no-verify origin <commit>:refs/heads/<branch>``.

        Git has no flag to suppress it: when ``origin``'s fetch refspec maps
        the branch, the push also creates ``refs/remotes/origin/<branch>``
        in the clone (a remote-tracking ref, not a branch)."""
        _git(repo_dir, "push", "--no-verify", _REMOTE, f"{commit}:refs/heads/{branch}")


_DIFF = ("diff", "-z", "--no-renames", "--no-ext-diff", "--no-textconv")


def _git(repo_dir: Path, *args: str) -> bytes:
    """One guarded ``git`` command in ``repo_dir``; its stdout, or
    :class:`GitRemoteError`."""
    guards = _guards(repo_dir)
    if isinstance(guards, str):
        raise GitRemoteError(guards)
    result = _run(repo_dir, *args, g=guards, env=_NO_PROMPT)
    if result.code != 0:
        raise GitRemoteError(f"git {args[0]} failed: {result.error}")
    return result.stdout


@dataclass(frozen=True)
class _Fix:
    """What the checks read from the document: the repository, the head,
    the Dockerfile path, the fix commit, its branch and worktree."""

    repo: str
    head: str
    dockerfile: str
    commit: str
    branch: str
    worktree: Path


@dataclass(frozen=True)
class _Refusal:
    """``fix publish`` refused; ``reason`` says why."""

    reason: str


def publish(
    doc_path: Path, base: str, env: Mapping[str, str], git: GitRemote, gh: GhRunner
) -> FixDocument:
    """Publish the fix recorded in ``doc_path`` as a PR against ``base``.

    Returns the saved document: ``last_operation.result`` is
    :data:`PUBLISHED` (the branch is pushed and a PR created or found, its
    URL in ``publication.pr_url``) or :data:`REFUSED` with the reason; a
    refusal never changes ``status``. Raises :class:`PublishAbort` only when
    ``fix.json``'s lock is held elsewhere or cannot be taken (nothing read,
    the document untouched) or ``fix.json`` cannot be read or saved.
    """
    try:
        with exclusive(doc_path):
            return _publish_locked(doc_path, base, env, git, gh)
    except LockError as exc:
        raise PublishAbort(str(exc)) from exc


def _publish_locked(
    doc_path: Path, base: str, env: Mapping[str, str], git: GitRemote, gh: GhRunner
) -> FixDocument:
    """:func:`publish` under the document's lock."""
    try:
        doc = load(doc_path)
    except Exception as exc:  # noqa: BLE001 — every read failure is exit 2
        raise PublishAbort(f"cannot read {doc_path}: {_describe(exc)}") from exc
    state = _State(doc, doc_path)
    try:
        outcome = _publish(state, base, env, git, gh)
    except PublishAbort:
        raise
    except Exception as exc:  # noqa: BLE001 — every step is total (F §8.4)
        outcome = _Refusal(_describe(exc))
    if isinstance(outcome, _Refusal):
        state.doc = _operation(state.doc, REFUSED, outcome.reason)
        state.save()
    return state.doc


@dataclass
class _State:
    """The document as last saved (the base recorded, once it is) and what
    was already done on the remote, for an exit-2 message (§8.4)."""

    doc: FixDocument
    path: Path
    pushed: str | None = None

    def save(self) -> None:
        """An atomic save; a failure is exit 2 naming the pushed branch."""
        try:
            save(self.doc, self.path)
        except Exception as exc:  # noqa: BLE001 — every save failure is exit 2
            done = f"; pushed: {self.pushed}" if self.pushed else ""
            raise PublishAbort(
                f"cannot save {self.path}: {_describe(exc)}{done}"
            ) from exc


def _publish(
    state: _State,
    base: str,
    env: Mapping[str, str],
    git: GitRemote,
    gh: GhRunner,
) -> None | _Refusal:
    """F §8.3 in order; ``None`` once published (and saved), or the first
    refusal."""
    doc = state.doc
    if doc.status not in PUBLISHABLE:
        return _Refusal(f"status {doc.status} is not publishable")
    fix = _fix(doc)
    if isinstance(fix, _Refusal):
        return fix
    problem = _base_problem(doc, fix, base, state.path.parent)
    if problem is not None:
        return _Refusal(problem)
    state.doc = doc = _with_base(doc, base)
    state.save()
    local = _local_checks(doc, fix, env, state.path)
    if isinstance(local, _Refusal):
        return local
    problem = _future_diff_problem(git, fix, base, local)
    if problem is not None:
        return _Refusal(problem)
    found = _lookup_pr(gh, doc, fix, base)
    if isinstance(found, _Refusal):
        return found
    problem = _push(git, fix)
    if problem is not None:
        return _Refusal(problem)
    state.pushed = f"branch {fix.branch} at {fix.commit}"
    pr_url = found if found is not None else _create_pr(gh, doc, fix, base)
    if isinstance(pr_url, _Refusal):
        return pr_url
    state.pushed = f"branch {fix.branch} at {fix.commit}, PR {pr_url}"
    state.doc = _published(doc, pr_url)
    state.save()
    return None


def _describe(exc: BaseException) -> str:
    """An exception as a reason: its type and message."""
    return f"{type(exc).__name__}: {exc}"


def _now() -> str:
    """The current UTC time, ISO 8601, to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _operation(doc: FixDocument, result: str, reason: str | None) -> FixDocument:
    """``doc`` with ``last_operation`` set for ``fix publish``."""
    last = LastOperation(command="publish", at=_now(), result=result, reason=reason)
    return doc.model_copy(update={"last_operation": last})


def _fix(doc: FixDocument) -> _Fix | _Refusal:
    """The fix's identifiers from ``doc``; a publishable status guarantees
    the proposal, local proof and fix commit (the model's invariants)."""
    publication = doc.publication
    target = doc.input.target
    repo, head = target.get("repo"), target.get("head_sha")
    dockerfile = target.get("artifact_path")
    if publication is None or publication.fix_commit is None:
        return _Refusal("no fix commit is recorded")
    if not all(isinstance(value, str) for value in (repo, head, dockerfile)):
        return _Refusal(f"the stored target is malformed: {target!r}"[:320])
    return _Fix(
        repo=str(repo),
        head=str(head),
        dockerfile=str(dockerfile),
        commit=publication.fix_commit,
        branch=publication.branch,
        worktree=Path(publication.worktree),
    )


def _base_problem(doc: FixDocument, fix: _Fix, base: str, cwd: Path) -> str | None:
    """A stored base must equal ``base``; ``base`` must be a valid branch
    name (``git check-ref-format``, run in ``cwd``, needs no repository)."""
    assert doc.publication is not None
    stored = doc.publication.base
    if stored is not None and stored != base:
        return f"the fix was published against base {stored!r}, not {base!r}"
    checked = _run(cwd, "check-ref-format", f"refs/heads/{base}", g=())
    if checked.code != 0 or base.startswith("-"):
        return f"{base!r} is not a valid branch name"
    if base == fix.branch:
        return f"the base {base!r} is the fix branch itself"
    return None


def _with_base(doc: FixDocument, base: str) -> FixDocument:
    """``doc`` with ``base`` recorded in its publication."""
    assert doc.publication is not None
    publication = doc.publication.model_copy(update={"base": base})
    return doc.model_copy(update={"publication": publication})


# --- the local checks -------------------------------------------------------


def _local_checks(
    doc: FixDocument, fix: _Fix, env: Mapping[str, str], doc_path: Path
) -> list[tuple[str, str]] | _Refusal:
    """Admission and trust re-checked, the branch name, the tip, the fix
    commit's content and its new set's ownership, the worktree's
    ``origin``; the committed ``(status, path)`` change."""
    reason = recheck_admission(doc, env)
    if reason is not None:
        return _Refusal(f"admission re-check: {reason}")
    reason = _branch_problem(doc, fix, doc_path)
    if reason is not None:
        return _Refusal(reason)
    guards = _guards(fix.worktree)
    if isinstance(guards, str):
        return _Refusal(guards)
    reason = _tip_problem(fix, guards)
    if reason is None:
        reason = _origin_problem(fix, guards)
    if reason is not None:
        return _Refusal(reason)
    change = _committed_change(doc, fix, guards)
    if isinstance(change, str):
        return _Refusal(f"the fix commit {fix.commit}: {change}")
    reason = _ownership_problem(doc, fix, guards, env)
    if reason is not None:
        return _Refusal(f"the fix commit {fix.commit}: {reason}")
    return change


def _branch_problem(doc: FixDocument, fix: _Fix, doc_path: Path) -> str | None:
    """The fix branch is the one ``deployer fix`` names for this fix: the
    class, ``head_sha`` and the fix directory's sequence number, where the
    fix directory holds both ``fix.json`` and the worktree."""
    fix_dir = fix.worktree.parent
    if fix.worktree.name != "worktree" or not _same_dir(fix_dir, doc_path.parent):
        return f"the worktree {fix.worktree} is not this fix directory's"
    seq = fix_dir.name
    if not (seq.isascii() and seq.isdigit()):
        return f"the fix directory {fix_dir} has no sequence number"
    assert doc.proposal is not None
    expected = branch_name(doc.proposal.cls, fix.head, int(seq))
    if fix.branch != expected:
        return f"the stored branch {fix.branch!r} is not the fix's {expected!r}"
    return None


def _same_dir(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` resolve to the same path."""
    return a.resolve() == b.resolve()


def _same_repo(a: object, b: str) -> bool:
    """Whether ``a`` is the ``owner/name`` slug ``b``, ignoring case (GitHub
    owner and repository names are case-insensitive)."""
    return isinstance(a, str) and a.casefold() == b.casefold()


def _ownership_problem(
    doc: FixDocument, fix: _Fix, guards: Sequence[str], env: Mapping[str, str]
) -> str | None:
    """A's ownership check (§2.4) over the fix commit's own set, with the
    **current** trust directory: the pointer, the three set files and the
    Dockerfile are written as raw blobs of the fix commit into a temporary
    tree in the fix directory; the record must cover the locally proved
    Dockerfile."""
    assert doc.local_proof is not None
    fix_dir = fix.worktree.parent
    with tempfile.TemporaryDirectory(dir=fix_dir, prefix=".publish-") as tmp:
        tree = Path(tmp)
        reason = _materialize(fix, guards, tree)
        if reason is not None:
            return reason
        roots = (tree, Path(doc.input.clone), fix.worktree, fix_dir)
        facts = verify_ownership(
            tree,
            repo=fix.repo,
            artifact_path=fix.dockerfile,
            trust=trust_dir(env),
            checked_roots=roots,
        )
    if facts.status != "confirmed" or facts.record is None:
        return f"its set is not confirmed at step {facts.step}: {facts.reason}"
    if facts.record.artifact_sha256 != doc.local_proof.dockerfile_sha256:
        return "its signed record does not cover the locally proved Dockerfile"
    return None


def _materialize(fix: _Fix, guards: Sequence[str], tree: Path) -> str | None:
    """Write the fix commit's pointer, set files and Dockerfile under
    ``tree`` (raw blobs, no attributes applied); a reason on failure."""
    pointer = _read(fix, guards, "cat-file", "blob", f"{fix.commit}:{_POINTER_PATH}")
    name = pointer.decode("utf-8", errors="replace").strip()
    set_dir = f"{SET_ROOT}/{name}"
    files = [
        _POINTER_PATH,
        fix.dockerfile,
        *(f"{set_dir}/{file}" for file in (RECORD_FILE, SNAPSHOT_FILE, SIGNATURE_FILE)),
    ]
    for rel in files:
        parts = rel.split("/")
        if any(part in ("", ".", "..") for part in parts) or rel.startswith("/"):
            return f"{rel!r} is not a plain relative path"
        data = _read(fix, guards, "cat-file", "blob", f"{fix.commit}:{rel}")
        path = tree.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return None


def _read(fix: _Fix, guards: Sequence[str], *args: str) -> bytes:
    """A guarded read-only ``git`` command in the worktree; its stdout, or
    :class:`GitRemoteError`."""
    result = _run(fix.worktree, *args, g=guards)
    if result.code != 0:
        raise GitRemoteError(f"git {args[0]} failed: {result.error}")
    return result.stdout


def _tip_problem(fix: _Fix, guards: Sequence[str]) -> str | None:
    """The fix branch's tip is the stored fix commit, whose only parent is
    ``head_sha``."""
    ref = f"refs/heads/{fix.branch}^{{commit}}"
    tip = _run(fix.worktree, "rev-parse", "--verify", "--quiet", ref, g=guards)
    if tip.code != 0:
        return f"the fix branch {fix.branch} is not found: {tip.error}"
    if tip.stdout.decode().strip() != fix.commit:
        now = tip.stdout.decode().strip()
        return (
            f"the fix branch {fix.branch} is at {now}, not the fix commit {fix.commit}"
        )
    parents = _read(fix, guards, "rev-list", "--parents", "-n", "1", fix.commit)
    if parents.decode().split()[1:] != [fix.head]:
        return f"the fix commit {fix.commit} is not a single commit on {fix.head}"
    return None


def _origin_problem(fix: _Fix, guards: Sequence[str]) -> str | None:
    """The worktree's ``origin`` is still the stored repository."""
    url = _run(fix.worktree, "remote", "get-url", _REMOTE, g=guards)
    slug = gitrepo.slug_from_url(url.stdout.decode(errors="replace").strip())
    if url.code != 0 or not _same_repo(slug, fix.repo):
        return f"the worktree's origin is {slug}, not the stored {fix.repo}"
    return None


def _committed_change(
    doc: FixDocument, fix: _Fix, guards: Sequence[str]
) -> list[tuple[str, str]] | str:
    """§3 over the fix commit object: its raw diff against ``head_sha``,
    the new set bound to the committed Dockerfile, and the Dockerfile bound
    to the stored proposal and local proof. The ``(status, path)`` list."""
    raw = _read(fix, guards, *_DIFF, "--raw", "--no-abbrev", fix.head, fix.commit)
    rows = _raw_rows(raw)
    new_set = _new_set(rows, fix)
    if isinstance(new_set, _Refusal):
        return new_set.reason
    old = _old_set_files(fix, guards, new_set)
    deleted = {path for _, _, status, path in rows if status == "D"}
    if deleted != old:
        return f"the deleted set files {sorted(deleted ^ old)} differ from §3's"
    head_df = _read(fix, guards, "cat-file", "blob", f"{fix.head}:{fix.dockerfile}")
    fix_df = _read(fix, guards, "cat-file", "blob", f"{fix.commit}:{fix.dockerfile}")
    problem = _set_binding_problem(fix, guards, new_set, fix_df)
    if problem is None:
        problem = _dockerfile_problem(doc, head_df, fix_df)
    if problem is not None:
        return problem
    return sorted((status, path) for _, _, status, path in rows)


def _raw_rows(raw: bytes) -> list[tuple[str, str, str, str]]:
    """``(old mode, new mode, status, path)`` per ``diff --raw -z`` entry."""
    fields = [field for field in raw.split(b"\0") if field]
    rows: list[tuple[str, str, str, str]] = []
    for meta, path in zip(fields[::2], fields[1::2], strict=True):
        old_mode, new_mode, _, _, status = meta.decode().lstrip(":").split()
        rows.append((old_mode, new_mode, status, _decode(path)))
    return rows


def _new_set(rows: list[tuple[str, str, str, str]], fix: _Fix) -> str | _Refusal:
    """The new set's name if every row is an allowed change of §3 (the
    Dockerfile and the pointer modified in place, set files added as
    regular files or deleted) and exactly one complete set is added."""
    added: dict[str, set[str]] = {}
    modified: set[str] = set()
    for old_mode, new_mode, status, path in rows:
        if path in (fix.dockerfile, _POINTER_PATH):
            if status != "M" or old_mode != new_mode:
                return _Refusal(f"{path}: {status} {old_mode}->{new_mode}")
            modified.add(path)
        elif not _is_set_file(path) or status not in ("A", "D"):
            return _Refusal(f"{path}: {status} is not an allowed change")
        elif status == "A":
            if new_mode != _REGULAR_MODE:
                return _Refusal(f"{path}: added with mode {new_mode}")
            name, file = _set_parts(path)
            added.setdefault(str(name), set()).add(file)
    if modified != {fix.dockerfile, _POINTER_PATH}:
        return _Refusal(f"expected {fix.dockerfile} and {_POINTER_PATH} modified")
    if len(added) != 1 or next(iter(added.values())) != _SET_FILES:
        return _Refusal(f"expected exactly one complete new set; found {sorted(added)}")
    return next(iter(added))


def _old_set_files(fix: _Fix, guards: Sequence[str], new_set: str) -> set[str]:
    """Every set file in ``head_sha`` outside the new set: §3 deletes them."""
    listed = _read(
        fix,
        guards,
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        fix.head,
        "--",
        _SET_PREFIX,
    )
    paths = {_decode(path) for path in listed.split(b"\0") if path}
    return {
        path for path in paths if _is_set_file(path) and _set_parts(path)[0] != new_set
    }


def _set_binding_problem(
    fix: _Fix, guards: Sequence[str], new_set: str, fix_df: bytes
) -> str | None:
    """The new set is named by its record's hash, the pointer names it, and
    the record binds the committed Dockerfile to ``head_sha`` and the
    repository."""
    set_dir = set_dir_name(new_set)
    record_path = f"{SET_ROOT}/{set_dir}/{RECORD_FILE}"
    record = _read(fix, guards, "cat-file", "blob", f"{fix.commit}:{record_path}")
    pointer = _read(fix, guards, "cat-file", "blob", f"{fix.commit}:{_POINTER_PATH}")
    if pointer != f"{set_dir}\n".encode():
        return f"{_POINTER_PATH} does not name the new set {new_set}"
    if _sha256(record) != new_set:
        return f"the new set {new_set} is not named by its record's hash"
    fields = json.loads(record.decode("utf-8"))
    if not isinstance(fields, dict):
        return "the new set's record is not a JSON object"
    expected = {
        "repo": fix.repo,
        "artifact_path": fix.dockerfile,
        "artifact_sha256": _sha256(fix_df),
        "source_commit": fix.head,
    }
    actual = {key: fields.get(key) for key in expected}
    if actual != expected:
        return f"the new set's record {actual} is not the fix's {expected}"
    return None


def _dockerfile_problem(doc: FixDocument, head_df: bytes, fix_df: bytes) -> str | None:
    """The committed Dockerfile changes only the stored proposal's
    instruction into its replacement, over the target's bytes, and is the
    locally proved one."""
    proposal, proof = doc.proposal, doc.local_proof
    assert proposal is not None and proof is not None
    target_sha = doc.input.target.get("artifact_sha256")
    if _sha256(head_df) != target_sha:
        return f"the Dockerfile at {doc.input.target.get('head_sha')} is not the target"
    if _sha256(fix_df) != proof.dockerfile_sha256:
        return "the committed Dockerfile is not the locally proved one"
    bound = _bound(head_df, proposal)
    if isinstance(bound, str):
        return bound
    problem = link_problem(head_df, fix_df, bound)
    if problem is not None:
        return f"the Dockerfile: {problem}"
    first, last = proposal.lines
    replaced = b"".join(fix_df.splitlines(keepends=True)[first - 1 : last])
    if replaced.decode("utf-8", errors="replace") != proposal.replacement:
        return "the committed instruction is not the stored replacement"
    return None


def _bound(head_df: bytes, proposal: Proposal) -> Bound | str:
    """The stored proposal's instruction, re-bound over ``head_df``."""
    instructions = parse(decode_as_read_text(head_df)).instructions
    if not 0 <= proposal.ordinal < len(instructions):
        return f"the proposal's ordinal {proposal.ordinal} is out of range"
    instruction = instructions[proposal.ordinal]
    first, last = proposal.lines
    if (instruction.first_line, instruction.last_line) != (first, last):
        return f"instruction {proposal.ordinal} does not span lines {first}-{last}"
    original = b"".join(head_df.splitlines(keepends=True)[first - 1 : last])
    if original.decode("utf-8", errors="replace") != proposal.original:
        return "the original instruction is not the stored one"
    return Bound(proposal.ordinal, (first, last), original, instruction)


def _sha256(data: bytes) -> str:
    """The SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


# --- the network --------------------------------------------------------------


def _future_diff_problem(
    git: GitRemote, fix: _Fix, base: str, committed: list[tuple[str, str]]
) -> str | None:
    """The fetched base's merge base with the fix commit is ``head_sha``,
    and the PR's diff from it is exactly the committed change."""
    try:
        tip = git.fetch(fix.worktree, base)
        merge_base = git.merge_base(fix.worktree, tip, fix.commit)
    except Exception as exc:  # noqa: BLE001 — a failure is a refusal
        return f"the base {base} is not usable: {_describe(exc)}"
    if merge_base == fix.commit:
        return f"the base {base} already contains the fix commit; the PR would be empty"
    if merge_base != fix.head:
        return (
            f"merge-base({base}, fix commit) is {merge_base}, not head_sha "
            f"{fix.head}; the PR would not carry exactly the fix"
        )
    diff = sorted(git.diff_names(fix.worktree, merge_base, fix.commit))
    if diff != committed:
        return f"the PR's diff {diff} is not the committed change {committed}"
    return None


def _push(git: GitRemote, fix: _Fix) -> str | None:
    """Push the fix commit to the branch unless ``origin`` already has it
    there; a remote branch at another commit is refused."""
    try:
        tip = git.remote_tip(fix.worktree, fix.branch)
        if tip == fix.commit:
            return None
        if tip is not None:
            return (
                f"the remote branch {fix.branch} is at {tip}, not the fix commit "
                f"{fix.commit}"
            )
        git.push(fix.worktree, fix.branch, fix.commit)
    except Exception as exc:  # noqa: BLE001 — a failure is a refusal
        return f"push of {fix.branch} failed: {_describe(exc)}"
    return None


def _lookup_pr(
    gh: GhRunner, doc: FixDocument, fix: _Fix, base: str
) -> str | None | _Refusal:
    """Before any push: the URL of the open PR to reuse, or ``None`` when
    one may be created.

    Every PR ever opened from the fix branch is listed (``state=all``). An
    open one must be the fix commit, from the fix repository, against
    ``base``; any other open one refuses. A recorded ``pr_url`` must be that
    open PR — a recorded PR that was closed or merged is never replaced. With
    nothing recorded, a closed PR that already carried the fix commit
    refuses too: the fix was proposed before, and a second PR is not made.
    """
    assert doc.publication is not None
    recorded = doc.publication.pr_url
    try:
        listed = _list_prs(gh, fix)
    except Exception as exc:  # noqa: BLE001 — a failure is a refusal
        return _Refusal(f"PR lookup failed: {_describe(exc)}")
    if isinstance(listed, _Refusal):
        return listed
    matching: str | None = None
    for pr in listed:
        if pr.ref != fix.branch:
            continue
        ours = (
            pr.sha == fix.commit and pr.base == base and _same_repo(pr.repo, fix.repo)
        )
        if pr.state == "open":
            if not ours or pr.url is None:
                return _Refusal(f"an open PR on {fix.branch} is not the fix: {pr}")
            matching = pr.url
        elif pr.sha == fix.commit and recorded is None:
            return _Refusal(f"a closed PR {pr.url} already carried the fix commit")
    if recorded is not None and matching != recorded:
        return _Refusal(
            f"the recorded PR {recorded} is not open with the fix commit against "
            f"{base} (open: {matching})"
        )
    return matching


@dataclass(frozen=True)
class _PR:
    """The fields of one PR the checks read; ``None`` where absent."""

    url: str | None
    state: object
    ref: object
    sha: object
    repo: object
    base: object


def _list_prs(gh: GhRunner, fix: _Fix) -> list[_PR] | _Refusal:
    """Every PR (open or closed) whose head is ``owner:<fix branch>``."""
    owner = fix.repo.split("/", 1)[0]
    query = urllib.parse.urlencode(
        {"state": "all", "head": f"{owner}:{fix.branch}", "per_page": "100"}
    )
    listed = json.loads(
        gh.api([f"repos/{fix.repo}/pulls?{query}"], timeout=GH_TIMEOUT_S)
    )
    if not isinstance(listed, list):
        return _Refusal("the PR listing is not a JSON array")
    return [_pr(entry) for entry in listed]


def _pr(entry: object) -> _PR:
    """One PR listing (or creation) entry's fields."""
    raw = entry if isinstance(entry, Mapping) else {}
    head = _mapping(raw.get("head"))
    url = raw.get("html_url")
    return _PR(
        url=url if isinstance(url, str) else None,
        state=raw.get("state"),
        ref=head.get("ref"),
        sha=head.get("sha"),
        repo=_mapping(head.get("repo")).get("full_name"),
        base=_mapping(raw.get("base")).get("ref"),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    """``value`` if it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def _create_pr(gh: GhRunner, doc: FixDocument, fix: _Fix, base: str) -> str | _Refusal:
    """Create the PR (only after the lookup and the push); the created PR
    must be the fix commit, from the fix repository, against ``base``."""
    title, body = pr_text(doc)
    argv = [
        f"repos/{fix.repo}/pulls",
        "-X",
        "POST",
        "-f",
        f"head={fix.branch}",
        "-f",
        f"base={base}",
        "-f",
        f"title={title}",
        "-f",
        f"body={body}",
    ]
    try:
        created = _pr(json.loads(gh.api(argv, timeout=GH_TIMEOUT_S)))
    except Exception as exc:  # noqa: BLE001 — a failure is a refusal
        return _Refusal(f"PR creation failed: {_describe(exc)}")
    ours = (
        created.sha == fix.commit
        and created.base == base
        and _same_repo(created.repo, fix.repo)
    )
    if not ours or created.url is None:
        return _Refusal(f"the created PR is not the fix: {created}")
    return created.url


def _published(doc: FixDocument, pr_url: str) -> FixDocument:
    """Record the PR and move ``locally_confirmed`` to ``fix_proposed``; a
    repeat keeps its status."""
    publication: Publication | None = doc.publication
    assert publication is not None
    status = "fix_proposed" if doc.status == "locally_confirmed" else doc.status
    doc = doc.model_copy(
        update={
            "status": status,
            "publication": publication.model_copy(update={"pr_url": pr_url}),
        }
    )
    return _operation(doc, PUBLISHED, None)


# --- the PR text --------------------------------------------------------------


def pr_text(doc: FixDocument) -> tuple[str, str]:
    """The PR's title and body, built only from ``doc`` (deterministic).

    Every piece of project or model text is inside a fence longer than any
    backtick run in it, so it cannot format the page or mention anyone.
    """
    proposal, proof = doc.proposal, doc.local_proof
    assert proposal is not None and proof is not None
    first, last = proposal.lines
    title = f"deployer fix: {proposal.cls} in {proposal.file} (lines {first}-{last})"
    sections = [
        _summary(proposal),
        _block("Original instruction", proposal.original),
        _block("Replacement instruction", proposal.replacement),
        _block("Rationale", "\n".join(_rationale_lines(proposal))),
        _block("Local proof", "\n".join(_proof_lines(doc))),
        _claim(proposal, proof.later_failure is not None),
        f"Fix-Id: `{doc.fix_id}`",
    ]
    return title, "\n\n".join(sections) + "\n"


def _summary(proposal: Proposal) -> str:
    """The heading: the class, the file and lines, the transformation."""
    first, last = proposal.lines
    return (
        "## deployer fix\n\n"
        f"- class: `{proposal.cls}`\n"
        f"- file: `{proposal.file}`, lines {first}-{last}\n"
        f"- transformation: `{proposal.transformation}`"
    )


def _block(heading: str, text: str) -> str:
    """A ``###`` heading and ``text`` in a fence it cannot close."""
    fence = _fence(text)
    return f"### {heading}\n\n{fence}text\n{text.rstrip()}\n{fence}"


def _rationale_lines(proposal: Proposal) -> list[str]:
    """The rationale: the model's statements with their facts, or the
    deterministic F1/F2 conditions."""
    lines: list[str] = []
    for entry in proposal.rationale:
        if entry.get("kind") == "deterministic":
            lines.append(f"deterministic {entry.get('transformation')}:")
            lines += [f"- {condition}" for condition in entry.get("conditions", [])]
            if entry.get("note"):
                lines.append(f"note: {entry['note']}")
            continue
        lines.append(f"model: {entry.get('explanation', '')}")
        for fact in entry.get("facts", []):
            if isinstance(fact, Mapping):
                lines.append(f"- fact {fact.get('kind')}: {fact.get('ref')}")
    return lines


def _proof_lines(doc: FixDocument) -> list[str]:
    """The local proof's evidence summary."""
    proof = doc.local_proof
    assert proof is not None
    lines = [
        f"corrected Dockerfile sha256: {proof.dockerfile_sha256}",
        f"backend: {proof.backend}",
    ]
    for item in proof.evidence:
        lines.append(
            f"{item.get('template')}: {item.get('evidence')} "
            f"({item.get('file')} lines {item.get('lines')})"
        )
    later = proof.later_failure
    if later is not None:
        lines.append(
            f"later failure: {later.get('what')} (exit {later.get('exit_code')})"
        )
    return lines


def _claim(proposal: Proposal, later_failure: bool) -> str:
    """The narrow claim (§6.4) and the caveats."""
    lines = ["### Claim", "", f"{CLAIM[:1].upper()}{CLAIM[1:]}."]
    if later_failure:
        lines.append(
            "The local build passed the corrected instruction and failed later: "
            "this PR does not claim that the substitution builds."
        )
    lines += [
        "",
        "- CI confirmation is pending (`deployer fix confirm`).",
    ]
    if proposal.transformation == "copy-source":
        lines.append(
            "- The COPY source substitution is a proposal for review, not "
            "evidence of the author's intent."
        )
    return "\n".join(lines)
