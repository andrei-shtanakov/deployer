"""The gate (design §2, §8.3): may fix authoring start, or a fix be published?

:func:`gate` runs for ``deployer fix``: the user's clone is checked as a whole
(a checkout at its toplevel, with an ``origin``, ``HEAD`` at the admitted
``head_sha``, a clean working tree), the target is derived from it, the
verdict must pass :func:`accept_for_fix`, and ownership is re-verified with
the **current** trust directory. :func:`recheck_admission` runs for
``fix publish``: the same two checks over the inputs stored in ``fix.json``
only, never over the clone's current state.

Both functions are total: every failure, an I/O or Git error included, is a
reason string; neither raises.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from deployer.admission.consumer import Accepted, Target, accept_for_fix
from deployer.admission.fsread import read_in_tree
from deployer.admission.model import AdmissionSection
from deployer.admission.ownership import OwnershipFacts, verify_ownership
from deployer.fix.document import FixDocument, verify_inputs
from deployer.provenance import gitrepo
from deployer.provenance.gitrepo import GitError
from deployer.provenance.model import sha256_hex
from deployer.provenance.trust import trust_dir

_DIRTY_SHOWN = 5
_FAILURES = (OSError, GitError, ValueError, RecursionError)
"""What the gate turns into a reason: I/O and Git errors, and ``ValueError``
(covering ``ValidationError``, ``JSONDecodeError``, ``UnicodeDecodeError``
and a path the OS cannot encode); ``RecursionError`` for a pathologically
nested verdict."""


@dataclass(frozen=True)
class Admitted:
    """The gate opened: the accepted ``section``, the ``target`` derived from
    the clone, the confirmed ``ownership`` (its ``snapshot.facts`` feed the
    proposal prompt), the clone's ``HEAD`` and its ``origin`` slug."""

    section: AdmissionSection
    target: Target
    ownership: OwnershipFacts
    clone_head: str
    origin: str


def gate(
    document: Mapping[str, object],
    root: Path,
    clone: Path,
    env: Mapping[str, str],
    extra_roots: Sequence[Path],
) -> Admitted | str:
    """Admit fix authoring for verdict ``document`` over ``clone`` (§2).

    ``root`` is the directory R's paths are relative to; ``extra_roots`` are
    the planned fix directory and worktree, which the trust directory must
    lie outside of too. A string is the no-admission reason. Never raises.
    """
    try:
        return _gate(document, root, clone, env, tuple(extra_roots))
    except _FAILURES as exc:
        return _failure(exc)


def recheck_admission(doc: FixDocument, env: Mapping[str, str]) -> str | None:
    """Re-run §2 steps 4-5 before a publication (§8.3); ``None`` if they hold.

    Uses only what ``doc.input`` stored — the re-hashed verdict and evidence,
    the stored target, try dir and ``source_dir`` — plus the **current**
    trust directory from ``env``. The clone's current ``HEAD`` and
    cleanliness are deliberately not looked at. ``try_dir`` and
    ``source_dir`` are resolved against ``root`` (an absolute stored path is
    used as is). Never raises.
    """
    try:
        return _recheck(doc, env)
    except _FAILURES as exc:
        return _failure(exc)


def _gate(
    document: Mapping[str, object],
    root: Path,
    clone: Path,
    env: Mapping[str, str],
    extra_roots: tuple[Path, ...],
) -> Admitted | str:
    """§2 steps 1 and 3-5 in order; the first failure is the reason."""
    state = _clone_state(clone)
    if isinstance(state, str):
        return state
    slug, head = state
    bound = _bound(document)
    if isinstance(bound, str):
        return bound
    head_sha, artifact_path = bound
    if head != head_sha:
        return f"clone HEAD {head} is not the admitted head_sha {head_sha}"
    try:
        artifact = read_in_tree(clone, artifact_path)
    except OSError as exc:  # Unreadable: missing, behind a symlink, not regular
        return f"the clone's {artifact_path} is not read: {exc}"
    target = Target(slug, head, artifact_path, sha256_hex(artifact))
    try_dir = _try_dir(document, root)
    if isinstance(try_dir, str):
        return try_dir
    source_dir = try_dir.parent.parent / "source"
    roots = (source_dir, clone, *extra_roots)
    admitted = _admit(document, try_dir, target, source_dir, trust_dir(env), roots)
    if isinstance(admitted, str):
        return admitted
    section, ownership = admitted
    return Admitted(section, target, ownership, head, slug)


def _clone_state(clone: Path) -> tuple[str, str] | str:
    """``(origin slug, HEAD)`` for a clean checkout at its toplevel, else why
    the clone is refused (§2 step 1)."""
    if not gitrepo.is_checkout(clone):
        return f"{clone} is not a Git checkout"
    top = gitrepo.toplevel(clone)
    if not top.samefile(clone):
        return f"{clone} is not the repository root ({top})"
    slug = gitrepo.origin_slug(clone)
    if slug is None:
        return f"{clone} has no parseable origin remote"
    dirty = gitrepo.dirty_paths(clone)
    if dirty:
        shown = ", ".join(dirty[:_DIRTY_SHOWN])
        more = len(dirty) - _DIRTY_SHOWN
        suffix = f" (+{more} more)" if more > 0 else ""
        return f"{clone} has uncommitted or untracked changes: {shown}{suffix}"
    return slug, gitrepo.head_commit(clone)


def _bound(document: Mapping[str, object]) -> tuple[str, str] | str:
    """``(head_sha, artifact_path)`` from the admission binding, read only to
    derive the target; :func:`accept_for_fix` validates the section itself."""
    admission = document.get("admission")
    binding = admission.get("binding") if isinstance(admission, Mapping) else None
    if not isinstance(binding, Mapping):
        return "the verdict document has no admission binding"
    head_sha, path = binding.get("head_sha"), binding.get("artifact_path")
    if not isinstance(head_sha, str) or not isinstance(path, str):
        return "the admission binding has no head_sha or artifact_path"
    return head_sha, path


def _try_dir(document: Mapping[str, object], root: Path) -> Path | str:
    """R's try directory, ``root / reproduction.try_dir`` (a relative path)."""
    reproduction = document.get("reproduction")
    rel = reproduction.get("try_dir") if isinstance(reproduction, Mapping) else None
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        return f"the verdict document has no relative reproduction.try_dir: {rel!r}"
    return root / rel


def _recheck(doc: FixDocument, env: Mapping[str, str]) -> str | None:
    """Stored inputs intact, then §2 steps 4-5 against the stored target."""
    reason = verify_inputs(doc)
    if reason is not None:
        return reason
    stored = doc.input
    data = Path(stored.verdict.path).read_bytes()
    if sha256_hex(data) != stored.verdict.sha256:
        return f"{stored.verdict.path} changed while it was re-read"
    verdict = json.loads(data.decode("utf-8"))
    if not isinstance(verdict, Mapping):
        return f"{stored.verdict.path} is not a JSON object"
    target = _stored_target(stored.target)
    if isinstance(target, str):
        return target
    if doc.publication is None:
        return "no fix worktree is recorded; nothing to publish"
    worktree = Path(doc.publication.worktree)
    root = Path(stored.root)
    source_dir = root / stored.source_dir
    roots = (source_dir, Path(stored.clone), worktree, worktree.parent)
    admitted = _admit(
        verdict, root / stored.try_dir, target, source_dir, trust_dir(env), roots
    )
    return admitted if isinstance(admitted, str) else None


def _stored_target(raw: Mapping[str, object]) -> Target | str:
    """The ``Target`` stored in ``fix.json``, every field a string."""
    fields = ("repo", "head_sha", "artifact_path", "artifact_sha256")
    values = [raw.get(name) for name in fields]
    strings = [value for value in values if isinstance(value, str)]
    if len(strings) != len(fields):
        return f"the stored target is malformed: {dict(raw)!r}"[:320]
    return Target(*strings)


def _admit(
    document: Mapping[str, object],
    try_dir: Path,
    target: Target,
    source_dir: Path,
    trust: Path,
    checked_roots: tuple[Path, ...],
) -> tuple[AdmissionSection, OwnershipFacts] | str:
    """§2 steps 4-5: ``accept_for_fix``, then ownership re-verified with the
    current trust directory over R's restored ``source/``."""
    result = accept_for_fix(document, try_dir, target)
    if not isinstance(result, Accepted):
        return result.reason
    ownership = verify_ownership(
        source_dir,
        repo=target.repo,
        artifact_path=target.artifact_path,
        trust=trust,
        checked_roots=checked_roots,
    )
    if ownership.status != "confirmed":
        return f"ownership not confirmed at step {ownership.step}: {ownership.reason}"
    return result.section, ownership


def _failure(exc: Exception) -> str:
    """An exception as a reason: its type and message."""
    return f"{type(exc).__name__}: {exc}"
