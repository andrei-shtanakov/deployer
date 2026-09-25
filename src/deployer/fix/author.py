"""``deployer fix`` (design §2-§6, §8.1-§8.2, §8.4): author one fix of an
admitted defect and prove it locally, up to one commit in a fix worktree.

The order is F §5.3's: a new fix directory, checked writable, and an
``in_progress`` ``fix.json`` → the gate (§2) → the bound instruction (§3.1)
→ the cheap preconditions (fix directory and worktree outside the clone, the
worktree, A's ``preflight`` there with the signing key, a preliminary
exclusion check) → the proposal (§4; the model is asked at most once, and
never when the envelope already stopped) → the local proof (§6) → the planned
set and the final exclusion check on its real paths → the corrected bytes
written into the worktree, the set issued without an ignore-file edit, the
full diff checked, one commit → ``locally_confirmed``.

``fix.json`` is saved at every checkpoint: after the preconditions, the
proposal, the local proof and the commit, and on every stop. Each step either
returns its value or a :class:`Stop` carrying one of the §1.1 reasons; an
exception inside a step becomes that step's reason, never a traceback.
:class:`FixAbort` (CLI exit 2) is kept for an invalid invocation or a local
I/O failure — a verdict that cannot be read, a fix directory that cannot be
made or written, a fix directory or worktree inside the clone, a failed
``fix.json`` save — and names what was already created.
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from pydantic import TypeAdapter

from deployer.admission.fsread import read_in_tree
from deployer.admission.model import Defect
from deployer.admission.prepare import _as_r_reads, _head_listing
from deployer.author import deployer_version
from deployer.fix.binding import Bound, bind_instruction, link_problem, splice
from deployer.fix.chooser import SourceChooser, build_prompt, validate_answer
from deployer.fix.document import (
    FIX_SCHEMA_VERSION,
    FixDocument,
    Input,
    LastOperation,
    Proposal,
    Publication,
    StopReason,
    StoredFile,
    check_writable,
    save,
)
from deployer.fix.envelope import apply_source, eligible_sources
from deployer.fix.fromfix import propose_from
from deployer.fix.gate import MAX_VERDICT_BYTES, Admitted, gate
from deployer.fix.localproof import LocalResult, local_proof, write_no_follow
from deployer.fix.workspace import (
    CommitError,
    Committed,
    FixDir,
    FixDirError,
    add_worktree,
    allowed_diff_problem,
    branch_name,
    commit,
    new_fix_dir,
    outside,
)
from deployer.forge import load_snapshot
from deployer.models import ContainerRuntime
from deployer.provenance import gitrepo
from deployer.provenance.issue import (
    PlannedSet,
    Preflight,
    exclusion_proven,
    issue,
    plan_set,
    preflight,
)
from deployer.provenance.model import (
    POINTER,
    RECORD_FILE,
    SET_ROOT,
    SIGNATURE_FILE,
    SNAPSHOT_FILE,
    TreeRow,
    set_dir_name,
    sha256_hex,
)
from deployer.reproduce import dockerfile, ignore
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.model import ReproductionSection
from deployer.reproduce.shape import Refusal, check_workflow

FIX_FILE = "fix.json"
PLACEHOLDER_SET = "0" * 64
"""The set name of the preliminary exclusion check (§5.2): no record hashes to
it, so a rule that happens to cover it proves nothing about the real set."""

T = TypeVar("T")


class FixAbort(Exception):
    """Exit 2 (§8.2): an invalid invocation or a local I/O failure; the message
    names every identifier created so far (§8.4)."""


@dataclass(frozen=True)
class Stop:
    """No fix: one of the §1.1 reasons and the concrete explanation."""

    reason: StopReason
    detail: str


@dataclass(frozen=True)
class _Inputs:
    """What the gate admitted plus R's records the later steps read."""

    admitted: Admitted
    defect: Defect
    section: ReproductionSection
    try_dir: Path
    source_dir: Path
    original: bytes
    build: BuildConfig
    listing: list[TreeRow]
    listing_complete: bool
    input: Input


@dataclass(frozen=True)
class _Proposed:
    """The proposal, the corrected Dockerfile (produced once, in memory) and
    what the local proof needs to find the corrected record."""

    proposal: Proposal
    corrected: bytes
    position: int | None
    new_source: str | None


@dataclass
class _Session:
    """The fix directory, its document and the identifiers created so far."""

    fix_dir: FixDir
    doc: FixDocument
    clone: Path
    worktree: Path | None = None
    branch: str | None = None
    commit: str | None = None

    @property
    def path(self) -> Path:
        """``fix.json`` in the fix directory."""
        return self.fix_dir.path / FIX_FILE

    def update(self, **fields: Any) -> None:
        """Replace document fields; :meth:`save` re-validates the whole."""
        self.doc = self.doc.model_copy(update=fields)

    def save(self) -> None:
        """A checkpoint (§8.1); a failure is exit 2 naming the identifiers."""
        try:
            save(self.doc, self.path)
        except Exception as exc:  # noqa: BLE001 — every save failure is exit 2
            raise FixAbort(
                f"cannot save {self.path}: {_describe(exc)}; {self.created()}"
            ) from exc

    def created(self) -> str:
        """The identifiers created so far, for an exit-2 message (§8.4)."""
        parts = [f"fix directory {self.fix_dir.path}"]
        if self.worktree is not None:
            parts.append(f"worktree {self.worktree}")
        if self.branch is not None:
            parts.append(f"branch {self.branch}")
        if self.commit is not None:
            parts.append(f"commit {self.commit}")
        return "created: " + ", ".join(parts)


def author_fix(
    verdict: Path,
    clone: Path,
    root: Path,
    env: Mapping[str, str],
    signing_key: Path | None,
    chooser: SourceChooser,
    rt: ContainerRuntime | None,
    build_timeout: int,
    *,
    on_document: Callable[[Path], None] | None = None,
) -> FixDocument:
    """Author a fix of the defect admitted in ``verdict`` over ``clone``.

    ``root`` is the directory R's paths are relative to (``deployer
    diagnose``'s working directory). Returns the saved document:
    ``locally_confirmed`` with a commit in the fix worktree, or ``stopped``
    with its reason. ``on_document`` is told the ``fix.json`` path as soon as
    the fix directory exists. Raises :class:`FixAbort` for exit 2 only.
    """
    root, clone = root.absolute(), clone.absolute()
    data, stored = _read_verdict(verdict)
    session = _open_session(data, stored, root, clone)
    session.save()
    if on_document is not None:
        on_document(session.path)
    outcome = _author(session, data, root, env, signing_key, chooser, rt, build_timeout)
    if isinstance(outcome, Stop):
        return _stopped(session, outcome)
    return session.doc


def _author(
    session: _Session,
    data: dict[str, Any],
    root: Path,
    env: Mapping[str, str],
    signing_key: Path | None,
    chooser: SourceChooser,
    rt: ContainerRuntime | None,
    build_timeout: int,
) -> Stop | None:
    """The steps after the first save, in F §5.3 order; a stop, or ``None``
    once the fix is ``locally_confirmed`` (and saved)."""
    inputs = _guarded("no admission", lambda: _admit(session, data, root, env))
    if isinstance(inputs, Stop):
        return inputs
    session.update(input=inputs.input)
    bound = _guarded("fix method not established", lambda: _bind(inputs))
    if isinstance(bound, Stop):
        return bound
    _require_outside(session)
    pre = _guarded(
        "commit blocked", lambda: _preconditions(session, inputs, signing_key)
    )
    if isinstance(pre, Stop):
        return pre
    session.save()
    proposed = _propose(inputs, bound, chooser)
    if isinstance(proposed, Stop):
        return proposed
    session.update(proposal=proposed.proposal)
    session.save()
    proved = _prove(session, inputs, bound, proposed, rt, env, build_timeout)
    if proved is not None:
        return proved
    committed = _guarded(
        "commit blocked",
        lambda: _commit(session, inputs, bound, pre, proposed, signing_key),
    )
    if isinstance(committed, Stop):
        return committed
    _confirmed(session, committed)
    return None


def _guarded(reason: StopReason, step: Callable[[], T | str]) -> T | Stop:
    """Run ``step``; a returned string or a raised exception is a stop for
    ``reason``. :class:`FixAbort` passes through (exit 2)."""
    try:
        result = step()
    except FixAbort:
        raise
    except Exception as exc:  # noqa: BLE001 — every step is total (F §8.4)
        return Stop(reason, _describe(exc))
    if isinstance(result, str):
        return Stop(reason, _detail(reason, result))
    return result


def _describe(exc: BaseException) -> str:
    """An exception as a reason: its type and message."""
    return f"{type(exc).__name__}: {exc}"


def _detail(reason: str, text: str) -> str:
    """``text`` without a leading copy of ``reason`` (the stop names it)."""
    return text.removeprefix(f"{reason}: ")


def _now() -> str:
    """The current UTC time, ISO 8601, to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- the fix directory and the first save ----------------------------------


def _read_verdict(verdict: Path) -> tuple[dict[str, Any], StoredFile]:
    """The verdict as a JSON object and its stored hash; exit 2 otherwise."""
    try:
        with open(verdict, "rb") as handle:
            raw = handle.read(MAX_VERDICT_BYTES + 1)
    except OSError as exc:
        raise FixAbort(f"cannot read the verdict {verdict}: {exc}") from exc
    if len(raw) > MAX_VERDICT_BYTES:
        raise FixAbort(f"the verdict {verdict} exceeds {MAX_VERDICT_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise FixAbort(f"the verdict {verdict} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FixAbort(f"the verdict {verdict} is not a JSON object")
    return data, StoredFile(path=str(verdict.absolute()), sha256=sha256_hex(raw))


def _open_session(
    data: dict[str, Any], verdict: StoredFile, root: Path, clone: Path
) -> _Session:
    """A new fix directory under R's attempt (§5.1), checked writable, and
    the ``in_progress`` document of what is known before the gate."""
    try_dir = _try_dir(data)
    attempt = PurePosixPath(try_dir).parent.parent
    fixes = root / attempt / "fixes"
    if not outside(fixes, clone):
        raise FixAbort(
            f"the fix location {fixes} is inside the clone {clone}; nothing created"
        )
    try:
        fix_dir = new_fix_dir(root / attempt)
    except FixDirError as exc:
        raise FixAbort(str(exc)) from exc
    reason = check_writable(fix_dir.path)
    if reason is not None:
        raise FixAbort(f"{reason}; created: fix directory {fix_dir.path}")
    doc = FixDocument(
        schema_version=FIX_SCHEMA_VERSION,
        fix_id=fix_dir.fix_id,
        status="in_progress",
        input=_initial_input(data, verdict, root, clone, try_dir, str(attempt)),
        last_operation=LastOperation(command="fix", at=_now(), result="in_progress"),
    )
    return _Session(fix_dir=fix_dir, doc=doc, clone=clone)


def _initial_input(
    data: dict[str, Any],
    verdict: StoredFile,
    root: Path,
    clone: Path,
    try_dir: str,
    attempt: str,
) -> Input:
    """The inputs as the unverified verdict states them; the gate's values
    (target, origin, head, the clean tree, evidence, build) replace the
    placeholders once it admits."""
    return Input(
        verdict=verdict,
        root=str(root),
        try_dir=try_dir,
        source_dir=str(PurePosixPath(attempt) / "source"),
        evidence=[],
        binding=_mapping(_get(data, "admission", "binding")),
        reproduction_binding=_mapping(_get(data, "reproduction", "binding")),
        build={},
        workflow_path=_text(_get(data, "run", "workflow_path")),
        workflow_sha256="",
        backend=_text(_get(data, "reproduction", "environment", "backend")),
        clone=str(clone),
        origin="",
        head="",
        clean=False,
        target={},
    )


def _try_dir(data: dict[str, Any]) -> str:
    """R's ``reproduction.try_dir``: a plain relative path of the shape
    ``<…>/tries/<NNN>`` (so its attempt directory is inside ``root``);
    exit 2 otherwise, before anything is created."""
    try_dir = _plain(_get(data, "reproduction", "try_dir"))
    if try_dir is None:
        raise FixAbort("the verdict has no plain relative reproduction.try_dir")
    parts = try_dir.split("/")
    seq = parts[-1]
    if len(parts) < 3 or parts[-2] != "tries" or not (seq.isascii() and seq.isdigit()):
        raise FixAbort(
            f"reproduction.try_dir {try_dir!r} is not of the shape "
            "<attempt>/tries/<NNN>"
        )
    return try_dir


def _get(data: object, *keys: str) -> object:
    """``data[k1][k2]…``, or ``None`` where a level is missing."""
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _plain(value: object) -> str | None:
    """``value`` if it is a plain relative path (Ruling I) with no control
    character, else ``None``."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    if any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return value


def _mapping(value: object) -> dict[str, Any]:
    """``value`` as a dict, or an empty one."""
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    """``value`` if it is a string, else ``""``."""
    return value if isinstance(value, str) else ""


# --- the gate and R's records ------------------------------------------------


def _admit(
    session: _Session, data: dict[str, Any], root: Path, env: Mapping[str, str]
) -> _Inputs | str:
    """§2 over the clone, then R's records the proposal and proof read."""
    fix_dir = session.fix_dir
    admitted = gate(data, root, session.clone, env, (fix_dir.path, fix_dir.worktree))
    if isinstance(admitted, str):
        return admitted
    return _inputs(data, root, session, admitted)


def _inputs(
    data: dict[str, Any], root: Path, session: _Session, admitted: Admitted
) -> _Inputs | str:
    """The admitted Dockerfile's bytes (R's, equal to the committed blob:
    Ruling L), R's build configuration re-bound from the run and the
    workflow, and the head listing; the stored ``Input`` completed."""
    section = ReproductionSection.model_validate(data["reproduction"])
    defect = admitted.section.defect
    binding = section.binding
    if defect is None or binding is None or section.try_dir is None:
        return "the admitted verdict has no defect or reproduction binding"
    target = admitted.target
    try_dir = root / section.try_dir
    source_dir = try_dir.parent.parent / "source"
    original = read_in_tree(source_dir, target.artifact_path)
    committed = gitrepo.blob_bytes(session.clone, target.head_sha, target.artifact_path)
    if committed != original:
        return (
            f"the committed {target.artifact_path} at {target.head_sha} is not "
            "R's restored bytes"
        )
    rebound = _build(data, section, source_dir)
    if isinstance(rebound, str):
        return rebound
    build, workflow_path, workflow_sha = rebound
    listing, complete = _head_listing(source_dir.parent, target.head_sha)
    stored = _admitted_input(
        session.doc.input, admitted, section, try_dir, build, workflow_sha
    )
    return _Inputs(
        admitted=admitted,
        defect=defect,
        section=section,
        try_dir=try_dir,
        source_dir=source_dir,
        original=original,
        build=build,
        listing=listing,
        listing_complete=complete,
        input=stored.model_copy(update={"workflow_path": workflow_path}),
    )


def _build(
    data: dict[str, Any], section: ReproductionSection, source_dir: Path
) -> tuple[BuildConfig, str, str] | str:
    """R's bound build configuration, re-derived with R's own workflow check
    over the job R bound, cross-checked against R's recorded binding; with the
    workflow path and the SHA-256 of its bytes at ``head_sha``."""
    binding = section.binding
    assert binding is not None
    run = load_snapshot(json.dumps(data["run"]))
    jobs = [job for job in run.jobs if job.job_id == binding.job_id]
    if len(jobs) != 1 or run.workflow_path is None:
        return f"R's bound job {binding.job_id} or its workflow is not in the run"
    workflow = read_in_tree(source_dir, run.workflow_path)
    shape = check_workflow(run, jobs[0], _as_r_reads(workflow))
    if isinstance(shape, Refusal):
        return f"R's build configuration is not re-bound: {shape.reason}"
    bound = (shape.workflow_job, shape.build_step, shape.build.dockerfile)
    recorded = (binding.workflow_job, binding.build_step, binding.dockerfile)
    if bound != recorded or binding.context != ".":
        return f"R's recorded binding {recorded} is not the re-bound {bound}"
    return shape.build, run.workflow_path, sha256_hex(workflow)


def _admitted_input(
    current: Input,
    admitted: Admitted,
    section: ReproductionSection,
    try_dir: Path,
    build: BuildConfig,
    workflow_sha: str,
) -> Input:
    """``current`` with what the gate established (Ruling I shapes; Ruling O
    ``build`` as pydantic dumps ``BuildConfig``)."""
    link = admitted.section.link
    assert link is not None and section.binding is not None
    evidence = [
        _stored(try_dir / name)
        for name in (link.ci.evidence_file, link.local.evidence_file)
    ]
    return current.model_copy(
        update={
            "evidence": evidence,
            "binding": admitted.section.binding.model_dump(mode="json"),
            "reproduction_binding": section.binding.model_dump(mode="json"),
            "build": TypeAdapter(BuildConfig).dump_python(build, mode="json"),
            "workflow_sha256": workflow_sha,
            "origin": admitted.origin,
            "head": admitted.clone_head,
            "clean": True,
            "target": asdict(admitted.target),
        }
    )


def _stored(path: Path) -> StoredFile:
    """``path`` with the SHA-256 of its current bytes."""
    return StoredFile(path=str(path), sha256=sha256_hex(path.read_bytes()))


def _bind(inputs: _Inputs) -> Bound | str:
    """§3.1 over R's bytes; the Dockerfile must be the build's own."""
    if inputs.build.dockerfile != inputs.admitted.target.artifact_path:
        return (
            f"the build's Dockerfile {inputs.build.dockerfile!r} is not the "
            f"admitted {inputs.admitted.target.artifact_path!r}"
        )
    return bind_instruction(inputs.original, inputs.defect)


# --- preconditions -------------------------------------------------------------


def _require_outside(session: _Session) -> None:
    """§2 step 2: the fix directory and the worktree lie outside the clone,
    else exit 2."""
    for name, path in (
        ("fix directory", session.fix_dir.path),
        ("worktree", session.fix_dir.worktree),
    ):
        if not outside(path, session.clone):
            raise FixAbort(
                f"the {name} {path} is inside the clone {session.clone}; "
                f"{session.created()}"
            )


def _preconditions(
    session: _Session, inputs: _Inputs, signing_key: Path | None
) -> Preflight | str:
    """§5.3 step 1: the worktree, A's preflight there with the key, and the
    preliminary exclusion check (pointer plus a placeholder set path)."""
    head = inputs.admitted.target.head_sha
    worktree = session.fix_dir.worktree
    branch = branch_name(inputs.defect.cls, head, session.fix_dir.seq)
    reason = add_worktree(session.clone, worktree, branch, head)
    if reason is None or os.path.lexists(worktree):
        session.worktree, session.branch = worktree, branch
    if reason is not None:
        return f"the worktree is not created: {reason}"
    session.update(
        publication=Publication(
            worktree=str(worktree),
            branch=branch,
            fix_commit=None,
            diff_ok=False,
            base=None,
            pr_url=None,
        )
    )
    pre = preflight(worktree, signing_key)
    if isinstance(pre, str):
        return f"preflight in the worktree: {pre}"
    if pre.source_commit != head or pre.repo != inputs.admitted.target.repo:
        return (
            f"the worktree's preflight is {pre.repo}@{pre.source_commit}, not "
            f"{inputs.admitted.target.repo}@{head}"
        )
    placeholder = _set_paths(PLACEHOLDER_SET)
    reason = exclusion_proven(worktree, placeholder)
    if reason is not None:
        return f"preliminary exclusion check: {reason}"
    return pre


def _set_paths(record_sha: str) -> list[str]:
    """The pointer and the three set files of the set named ``record_sha``."""
    return [f"{SET_ROOT}/{POINTER}"] + [
        f"{SET_ROOT}/{set_dir_name(record_sha)}/{name}"
        for name in (RECORD_FILE, SNAPSHOT_FILE, SIGNATURE_FILE)
    ]


# --- the proposal ------------------------------------------------------------


def _propose(inputs: _Inputs, bound: Bound, chooser: SourceChooser) -> _Proposed | Stop:
    """§4.1 for ``missing_copy_source`` (the model), §4.2 for
    ``from_argument_count`` (the closed list)."""
    if inputs.defect.cls == "missing_copy_source":
        return _guarded(
            "fix method not established",
            lambda: _propose_copy(inputs, bound, chooser),
        )
    return _guarded("no proposal", lambda: _propose_from(inputs, bound))


def _propose_copy(
    inputs: _Inputs, bound: Bound, chooser: SourceChooser
) -> _Proposed | str:
    """The envelope, then one model call over its eligible blobs, then the
    one-token replacement. The model is not called on an envelope stop."""
    if not inputs.listing_complete:
        return "R's head_sha listing is truncated; eligibility is unprovable"
    absent = inputs.defect.object
    rules = _ignore_rules(inputs)
    if isinstance(rules, str):
        return rules
    candidates = eligible_sources(
        bound,
        absent,
        inputs.listing,
        *rules,
        dockerfile=inputs.original,
        artifact_path=inputs.admitted.target.artifact_path,
    )
    if isinstance(candidates, str):
        return candidates
    snapshot = inputs.admitted.ownership.snapshot
    if snapshot is None:
        return "the confirmed ownership carries no snapshot facts"
    prompt = build_prompt(
        _as_r_reads(inputs.original), bound, absent, snapshot.facts, candidates.eligible
    )
    try:
        raw = chooser.choose(prompt)
    except Exception as exc:  # noqa: BLE001 — one attempt; a failure is a stop
        return f"the model call failed: {_describe(exc)}"
    paths = {row.path for row in inputs.listing}
    choice = validate_answer(raw, candidates.eligible, snapshot.facts, paths)
    if isinstance(choice, str):
        return choice
    replacement = apply_source(bound, absent, choice.source)
    if isinstance(replacement, str):
        return replacement
    return _proposed(
        inputs,
        bound,
        replacement,
        transformation="copy-source",
        rationale=choice.rationale,
        envelope=candidates.conditions,
        position=candidates.position,
        new_source=choice.source,
    )


def _ignore_rules(
    inputs: _Inputs,
) -> tuple[ignore.IgnoreRules, ignore.IgnoreRules] | str:
    """R's effective ignore rules over ``source/``: CI's, and the local
    ones for R's recorded backend."""
    environment = inputs.section.environment
    if environment is None:
        return "R recorded no environment; the local ignore file is unknown"
    source, name = inputs.source_dir, inputs.build.dockerfile
    ci = ignore.load_rules(source, ignore.ci_ignore_file(source, name))
    local_file = ignore.local_ignore_file(source, name, environment.backend)
    return ci, ignore.load_rules(source, local_file)


def _propose_from(inputs: _Inputs, bound: Bound) -> _Proposed | str:
    """F1/F2 (§4.2) with R's bound build args; a deterministic rationale."""
    parsed = dockerfile.parse(_as_r_reads(inputs.original))
    fix = propose_from(parsed, bound, inputs.build.build_args, inputs.original)
    if isinstance(fix, str):
        return fix
    rationale = {
        "kind": "deterministic",
        "transformation": fix.transformation,
        "original": _decoded(bound.original).strip(),
        "replacement": _decoded(fix.replacement).strip(),
        "conditions": fix.conditions,
        "note": (
            "a syntactic correction; not evidence of the author's intended stage name"
        ),
    }
    return _proposed(
        inputs,
        bound,
        fix.replacement,
        transformation=fix.transformation,
        rationale=[rationale],
        envelope=[{"condition": item, "ok": True} for item in fix.conditions],
        position=None,
        new_source=None,
    )


def _proposed(
    inputs: _Inputs,
    bound: Bound,
    replacement: bytes,
    *,
    transformation: str,
    rationale: list[dict[str, Any]],
    envelope: list[dict[str, Any]],
    position: int | None,
    new_source: str | None,
) -> _Proposed | str:
    """The corrected Dockerfile, produced once and checked against §3.1."""
    corrected = splice(inputs.original, bound, replacement)
    problem = link_problem(inputs.original, corrected, bound)
    if problem is not None:
        return f"the corrected Dockerfile breaks the link: {problem}"
    proposal = Proposal.model_validate(
        {
            "class": inputs.defect.cls,
            "file": inputs.defect.file,
            "lines": bound.lines,
            "transformation": transformation,
            "original": _decoded(bound.original),
            "replacement": _decoded(replacement),
            "ordinal": bound.ordinal,
            "rationale": rationale,
            "envelope": envelope,
        }
    )
    return _Proposed(proposal, corrected, position, new_source)


def _decoded(data: bytes) -> str:
    """Instruction bytes as text for ``fix.json`` (the commit holds the bytes)."""
    return data.decode("utf-8", errors="replace")


# --- the local proof -----------------------------------------------------------


def _prove(
    session: _Session,
    inputs: _Inputs,
    bound: Bound,
    proposed: _Proposed,
    rt: ContainerRuntime | None,
    env: Mapping[str, str],
    build_timeout: int,
) -> Stop | None:
    """§6 in the fix directory's own context; its evidence is saved either
    way (a checkpoint). ``None`` once locally confirmed."""
    reason = "no local confirmation"
    if rt is None:
        return Stop(reason, "no container runtime found")
    result = _guarded(
        reason,
        lambda: local_proof(
            inputs.section,
            inputs.source_dir,
            session.fix_dir.path,
            proposed.corrected,
            bound,
            inputs.defect.cls,
            proposed.position,
            proposed.new_source,
            rt,
            env,
            inputs.build,
            session.fix_dir.fix_id,
            build_timeout,
        ),
    )
    if isinstance(result, Stop):
        return result
    session.update(local_proof=result.proof)
    session.save()
    return _proof_stop(result)


def _proof_stop(result: LocalResult) -> Stop | None:
    """A not-ok proof as a stop."""
    if result.ok:
        return None
    reason = "no local confirmation"
    return Stop(reason, _detail(reason, result.reason or "not confirmed"))


# --- the set, the full diff and the commit -------------------------------------


def _commit(
    session: _Session,
    inputs: _Inputs,
    bound: Bound,
    pre: Preflight,
    proposed: _Proposed,
    signing_key: Path | None,
) -> Committed | str:
    """§5.3 steps 4-7: plan the set, prove its real paths excluded, write the
    corrected bytes, issue without an ignore-file edit, check the full diff,
    commit."""
    version = deployer_version()
    if version is None:
        return "the installed deployer version is unknown; no set can be issued"
    if signing_key is None:
        return "no signing key given"
    worktree = session.fix_dir.worktree
    artifact = inputs.admitted.target.artifact_path
    corrected = proposed.corrected
    plan = plan_set(pre, corrected, version)
    reason = exclusion_proven(worktree, plan.paths)
    if reason is not None:
        return f"final exclusion check: {reason}"
    reason = write_no_follow(worktree, artifact, corrected)
    if reason is not None:
        return f"the corrected Dockerfile is not written: {reason}"
    try:
        result = _issue_and_commit(
            session,
            artifact,
            bound,
            pre,
            proposed,
            _Signing(plan, version, signing_key),
        )
    except Exception as exc:  # noqa: BLE001 — named with the written file below
        result = _describe(exc)
    if isinstance(result, str):
        return (
            f"{result}; the corrected {artifact} was already written into the "
            f"worktree {worktree}"
        )
    return result


@dataclass(frozen=True)
class _Signing:
    """The planned set, the deployer version and the key that signs it."""

    plan: PlannedSet
    version: str
    key: Path


def _issue_and_commit(
    session: _Session,
    artifact: str,
    bound: Bound,
    pre: Preflight,
    proposed: _Proposed,
    signing: _Signing,
) -> Committed | str:
    """§5.3 steps 5-7 once the corrected bytes are in the worktree: issue
    the planned set without an ignore-file edit, check the full diff,
    commit."""
    worktree = session.fix_dir.worktree
    plan = signing.plan
    issued = issue(
        pre, signing.key, signing.version, proposed.corrected, edit_ignore=False
    )
    if not issued.published:
        return f"issuing failed: {issued.reason}"
    if issued.set_dir != set_dir_name(plan.record_sha256):
        return f"the issued set {issued.set_dir} is not the planned one"
    vetted = allowed_diff_problem(worktree, artifact, bound, plan.record_sha256)
    if isinstance(vetted, str):
        return f"full-diff check: {vetted}"
    try:
        return commit(worktree, _message(session, proposed.proposal), vetted)
    except CommitError as exc:
        return str(exc)


def _message(session: _Session, proposal: Proposal) -> str:
    """The fix commit's message: what changed, and the narrow claim."""
    first, last = proposal.lines
    return (
        f"deployer fix: {proposal.cls} in {proposal.file} "
        f"(lines {first}-{last})\n\n"
        f"{proposal.transformation}: {proposal.original.strip()}\n"
        f"{' ' * len(proposal.transformation)}  -> "
        f"{proposal.replacement.strip()}\n\n"
        "Locally confirmed: the diagnosed defect's check passes and the local\n"
        "build passed the corrected instruction. A proposal for review, not\n"
        "evidence of the author's intent.\n\n"
        f"Fix-Id: {session.fix_dir.fix_id}\n"
    )


def _confirmed(session: _Session, committed: Committed) -> None:
    """``locally_confirmed``: record the commit (and whether the worktree's
    index was synced to it) and save."""
    session.commit = committed.sha
    publication = session.doc.publication
    assert publication is not None
    synced = (
        None
        if committed.index_synced
        else f"the worktree index was not synced: {committed.detail}"
    )
    session.update(
        status="locally_confirmed",
        publication=publication.model_copy(
            update={
                "fix_commit": committed.sha,
                "diff_ok": True,
                "index_synced": committed.index_synced,
            }
        ),
        last_operation=LastOperation(
            command="fix", at=_now(), result="locally_confirmed", reason=synced
        ),
    )
    session.save()


def _stopped(session: _Session, stop: Stop) -> FixDocument:
    """Record ``stop`` and save; the stored document."""
    session.update(
        status="stopped",
        stop_reason=stop.reason,
        stop_detail=stop.detail,
        last_operation=LastOperation(
            command="fix",
            at=_now(),
            result="stopped",
            reason=f"{stop.reason}: {stop.detail}",
        ),
    )
    session.save()
    return session.doc
