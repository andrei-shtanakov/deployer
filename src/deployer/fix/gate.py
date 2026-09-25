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
from deployer.admission.prepare import _head_listing
from deployer.fix.document import FixDocument, Input, StoredFile, verify_inputs
from deployer.provenance import gitrepo
from deployer.provenance.model import sha256_hex
from deployer.provenance.trust import trust_dir
from deployer.reproduce.run import TryDirError

_DIRTY_SHOWN = 5
MAX_VERDICT_BYTES = 16 * 1024 * 1024
"""A stored verdict longer than this is not read (and the re-check refuses)."""


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
    if not isinstance(document, Mapping):
        return f"the verdict document is not a JSON object: {type(document).__name__}"
    try:
        return _gate(document, root, clone, env, tuple(extra_roots))
    except Exception as exc:  # noqa: BLE001 — the gate never raises (F §8.4)
        return _failure(exc)


def recheck_admission(doc: FixDocument, env: Mapping[str, str]) -> str | None:
    """Re-run §2 steps 4-5 before a publication (§8.3); ``None`` if they hold.

    Uses only what ``doc.input`` stored — the re-hashed verdict and evidence,
    the stored target, try dir and ``source_dir`` — plus the **current**
    trust directory from ``env``. The clone's current ``HEAD`` and
    cleanliness are deliberately not looked at. The stored shape is enforced:
    ``root``, ``clone`` and the worktree are absolute; ``try_dir`` and
    ``source_dir`` are plain paths relative to ``root``. Never raises.
    """
    try:
        return _recheck(doc, env)
    except Exception as exc:  # noqa: BLE001 — the re-check never raises (F §8.4)
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
    problem = _plain_relative_problem(rel)
    if problem is not None:
        return f"reproduction.try_dir {problem}"
    return root / str(rel)


def _plain_relative_problem(rel: object) -> str | None:
    """Why ``rel`` is not a plain relative path (a non-empty string, not
    absolute, no empty, ``.`` or ``..`` component), else ``None``."""
    if not isinstance(rel, str) or not rel:
        return f"{rel!r} is not a non-empty string"
    if Path(rel).is_absolute():
        return f"{rel!r} is absolute"
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return f"{rel!r} has an empty, '.' or '..' component"
    return None


def _recheck(doc: FixDocument, env: Mapping[str, str]) -> str | None:
    """Stored inputs intact, then §2 steps 4-5 against the stored target."""
    reason = verify_inputs(doc)
    if reason is not None:
        return reason
    stored = doc.input
    if doc.publication is None:
        return "no fix worktree is recorded; nothing to publish"
    problem = _stored_shape_problem(stored, doc.publication.worktree)
    if problem is not None:
        return problem
    verdict = _stored_verdict(stored.verdict)
    if isinstance(verdict, str):
        return verdict
    target = _stored_target(stored.target)
    if isinstance(target, str):
        return target
    worktree = Path(doc.publication.worktree)
    root = Path(stored.root)
    try_dir, source_dir = root / stored.try_dir, root / stored.source_dir
    roots = (source_dir, Path(stored.clone), worktree, worktree.parent)
    admitted = _admit(verdict, try_dir, target, source_dir, trust_dir(env), roots)
    if isinstance(admitted, str):
        return admitted
    return _evidence_coverage_problem(doc, admitted[0], try_dir)


def _stored_shape_problem(stored: Input, worktree: str) -> str | None:
    """The stored path shape T13 writes: absolute ``root``, ``clone`` and
    ``worktree``; ``try_dir`` and ``source_dir`` plain, relative to ``root``."""
    absolute = (
        ("root", stored.root),
        ("clone", stored.clone),
        ("worktree", worktree),
    )
    for name, value in absolute:
        if not Path(value).is_absolute():
            return f"the stored {name} {value!r} is not absolute"
    for name, rel in (("try_dir", stored.try_dir), ("source_dir", stored.source_dir)):
        problem = _plain_relative_problem(rel)
        if problem is not None:
            return f"the stored {name} {problem}"
    return None


def _stored_verdict(stored: StoredFile) -> Mapping[str, object] | str:
    """The stored verdict, read once (at most :data:`MAX_VERDICT_BYTES`),
    its bytes re-hashed, parsed as a JSON object; else why not."""
    with open(stored.path, "rb") as handle:
        data = handle.read(MAX_VERDICT_BYTES + 1)
    if len(data) > MAX_VERDICT_BYTES:
        return f"{stored.path} exceeds {MAX_VERDICT_BYTES} bytes; not read"
    if sha256_hex(data) != stored.sha256:
        return f"{stored.path} changed while it was re-read"
    verdict = json.loads(data.decode("utf-8"))
    if not isinstance(verdict, Mapping):
        return f"{stored.path} is not a JSON object"
    return verdict


def _evidence_coverage_problem(
    doc: FixDocument, section: AdmissionSection, try_dir: Path
) -> str | None:
    """The evidence ``accept_for_fix`` read is among the files
    ``verify_inputs`` re-hashed: both sides' files under ``try_dir``."""
    if section.link is None:
        return "the admitted section has no link"
    hashed = {Path(stored.path).resolve() for stored in doc.input.evidence}
    for name in (section.link.ci.evidence_file, section.link.local.evidence_file):
        if (try_dir / name).resolve() not in hashed:
            return f"evidence {try_dir / name} is not among the stored hashed inputs"
    return None


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
    """§2 steps 4-5: ``accept_for_fix``; R's own state bound to the target;
    ownership re-verified with the current trust directory over R's restored
    ``source/``; the signed record bound to the target.

    The verdict document is not authenticated, so its binding alone proves
    nothing: the target's head and bytes must also be R's (``source.json``,
    ``source/<artifact_path>``) and the signed record's.
    """
    result = accept_for_fix(document, try_dir, target)
    if not isinstance(result, Accepted):
        return result.reason
    problem = _r_state_problem(source_dir, target)
    if problem is not None:
        return problem
    ownership = verify_ownership(
        source_dir,
        repo=target.repo,
        artifact_path=target.artifact_path,
        trust=trust,
        checked_roots=checked_roots,
    )
    if ownership.status != "confirmed":
        return f"ownership not confirmed at step {ownership.step}: {ownership.reason}"
    record = ownership.record
    if record is None or record.artifact_sha256 != target.artifact_sha256:
        signed = None if record is None else record.artifact_sha256
        return (
            f"the signed record's artifact_sha256 {signed} is not the target's "
            f"{target.artifact_sha256}"
        )
    return result.section, ownership


def _r_state_problem(source_dir: Path, target: Target) -> str | None:
    """R restored ``target.head_sha`` (its ``source.json`` says so, read as
    admission's ``prepare`` reads it) and the restored artifact's bytes, read
    no-follow, hash to ``target.artifact_sha256``."""
    try:
        _head_listing(source_dir.parent, target.head_sha)
    except TryDirError as exc:
        return f"R's restoration is not the target's: {exc}"
    try:
        restored = read_in_tree(source_dir, target.artifact_path)
    except OSError as exc:
        return f"R's restored {target.artifact_path} is not read: {exc}"
    digest = sha256_hex(restored)
    if digest != target.artifact_sha256:
        return (
            f"R's restored {target.artifact_path} ({digest}) is not the target's "
            f"{target.artifact_sha256}"
        )
    return None


def _failure(exc: Exception) -> str:
    """An exception as a reason: its type and message."""
    return f"{type(exc).__name__}: {exc}"
