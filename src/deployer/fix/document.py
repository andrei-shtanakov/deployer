"""``fix.json`` (design §8.1): the fix document, schema 1.0.

The single artifact the ``fix``/``fix publish``/``fix confirm`` commands read
and write, dev-side evidence stored in the fix directory (§5.1) and never
committed to the project. ``Input`` is the single producer of what
``fix publish`` (T14) and ``fix confirm`` (T19) need: they never re-derive it
from the clone. This module is pure model shape plus local I/O (save, load,
a writability probe, a re-hash of the stored inputs); it authors nothing and
runs no command.
"""

import contextlib
import os
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    model_validator,
)

from deployer.provenance.model import sha256_hex

FIX_SCHEMA_VERSION = "1.0"

Status = Literal[
    "in_progress",
    "stopped",
    "locally_confirmed",
    "fix_proposed",
    "ci_confirmed",
]
"""The reached state (design §1); ``last_operation`` records the latest
command's outcome separately."""

StopReason = Literal[
    "no admission",
    "fix method not established",
    "no proposal",
    "no local confirmation",
    "commit blocked",
]
"""Why ``deployer fix`` produced no fix (design §1.1)."""

_CONFIRMED_STATUSES = ("locally_confirmed", "fix_proposed", "ci_confirmed")
_PUBLISHED_STATUSES = ("fix_proposed", "ci_confirmed")

Uuid4 = Annotated[
    StrictStr,
    StringConstraints(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
            r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    ),
]
"""A lowercase, canonically-formatted UUID4 string."""


class LastOperation(BaseModel):
    """The outcome of the latest command, stored apart from ``status``
    (design §1): its name, time, result and, on a refusal, why."""

    model_config = ConfigDict(extra="forbid")
    command: Literal["fix", "publish", "confirm"]
    at: str
    result: str
    reason: str | None = None


class StoredFile(BaseModel):
    """An absolute path plus the SHA-256 of its bytes, taken at
    ``deployer fix`` time; ``verify_inputs`` re-hashes it later."""

    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str


class Input(BaseModel):
    """Everything ``fix publish`` and ``fix confirm`` need, captured once by
    ``deployer fix`` so neither re-derives it from the clone (design §8.1)."""

    model_config = ConfigDict(extra="forbid")
    verdict: StoredFile
    root: str
    try_dir: str
    source_dir: str
    evidence: list[StoredFile]
    binding: dict[str, Any]
    reproduction_binding: dict[str, Any]
    build: dict[str, Any]
    workflow_path: str
    workflow_sha256: str
    backend: str
    clone: str
    origin: str
    head: str
    clean: bool
    target: dict[str, Any]


class Proposal(BaseModel):
    """The bound instruction and its proposed replacement (design §3, §4).

    ``class`` is a Python keyword, so the attribute is ``cls``; the wire
    field is ``class`` (the same alias arrangement as
    ``deployer.admission.model.Defect``): ``validate_by_name`` accepts the
    attribute name too, ``serialize_by_alias`` means a plain
    ``model_dump()``/``model_dump_json()`` always emits ``class``.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )
    cls: str = Field(alias="class")
    file: str
    lines: tuple[int, int]
    transformation: Literal["copy-source", "F1", "F2"]
    original: str
    replacement: str
    ordinal: int
    rationale: list[dict[str, Any]]
    envelope: list[dict[str, Any]]


class LocalProof(BaseModel):
    """Local confirmation's evidence (design §6): configuration, the
    before/after per-instruction check records, and evidence lines."""

    model_config = ConfigDict(extra="forbid")
    dockerfile_sha256: str
    build: dict[str, Any]
    backend: str
    versions: dict[str, Any]
    records_before: list[dict[str, Any]]
    records_after: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    later_failure: dict[str, Any] | None


class Publication(BaseModel):
    """The fix worktree, branch, commit and, once published, the PR
    (design §5, §8.3)."""

    model_config = ConfigDict(extra="forbid")
    worktree: str
    branch: str
    fix_commit: str | None
    diff_ok: bool
    base: str | None
    pr_url: str | None


class CiAttempt(BaseModel):
    """One confirmation attempt (design §7.4-§7.5), append-only: earlier
    positive evidence is never erased by a later conclusion."""

    model_config = ConfigDict(extra="forbid")
    at: str
    considered: list[dict[str, Any]]
    outcome: Literal["ci_confirmed", "ci_confirmation_insufficient"]
    reason: str | None
    evidence: list[dict[str, Any]]


class FixDocument(BaseModel):
    """``fix.json``, schema 1.0 (design §8.1): the complete state of one fix
    attempt, saved atomically at each checkpoint."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    fix_id: Uuid4
    status: Status
    stop_reason: StopReason | None = None
    stop_detail: str | None = None
    input: Input
    proposal: Proposal | None = None
    local_proof: LocalProof | None = None
    publication: Publication | None = None
    ci_attempts: list[CiAttempt] = Field(default_factory=list)
    last_operation: LastOperation | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.status == "stopped" and self.stop_reason is None:
            raise ValueError("stopped requires a stop_reason")
        if self.status != "stopped" and self.stop_reason is not None:
            raise ValueError("only stopped carries a stop_reason")
        if self.status in _CONFIRMED_STATUSES:
            if self.proposal is None:
                raise ValueError(f"{self.status} requires a proposal")
            if self.local_proof is None:
                raise ValueError(f"{self.status} requires a local_proof")
            if self.publication is None or self.publication.fix_commit is None:
                raise ValueError(f"{self.status} requires a publication fix_commit")
        if self.status in _PUBLISHED_STATUSES:
            if self.publication is None or self.publication.pr_url is None:
                raise ValueError(f"{self.status} requires a publication pr_url")
        if self.status == "ci_confirmed":
            if not self.ci_attempts or self.ci_attempts[-1].outcome != "ci_confirmed":
                raise ValueError(
                    "ci_confirmed requires the last ci_attempts outcome ci_confirmed"
                )
        return self


def save(doc: FixDocument, path: Path) -> None:
    """Write ``doc`` to ``path`` atomically.

    A temp file named ``<path.name>.tmp-<pid>`` is written in ``path``'s own
    directory, flushed and ``fsync``-ed, then moved onto ``path`` with
    ``os.replace``. If anything fails, the temp file is removed on a
    best-effort basis and the exception is re-raised; ``path`` itself is
    left untouched.
    """
    tmp_path = path.parent / f"{path.name}.tmp-{os.getpid()}"
    data = doc.model_dump_json(indent=2).encode() + b"\n"
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def load(path: Path) -> FixDocument:
    """Load a fix document from ``path``.

    An ``OSError`` reading the file, or a ``pydantic.ValidationError``
    parsing it, propagates to the caller (CLI exit 2).
    """
    return FixDocument.model_validate_json(path.read_bytes())


def check_writable(directory: Path) -> str | None:
    """Whether a fix document could be saved under ``directory``: create and
    remove a uniquely named probe file. A reason if it cannot, else
    ``None``."""
    probe = directory / f".deployer-fix-writable-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        probe.touch(exist_ok=False)
    except OSError as exc:
        return f"{directory} is not writable: {exc}"
    with contextlib.suppress(OSError):
        probe.unlink()
    return None


def verify_inputs(doc: FixDocument) -> str | None:
    """Re-hash the verdict and every evidence file of ``doc.input``.

    A reason naming the path is returned for the first file that is
    missing, unreadable, or whose bytes no longer match the stored hash;
    ``None`` once every stored file is confirmed intact.
    """
    for stored in (doc.input.verdict, *doc.input.evidence):
        reason = _verify_stored_file(stored)
        if reason is not None:
            return reason
    return None


def _verify_stored_file(stored: StoredFile) -> str | None:
    """``None`` if ``stored`` still matches the file on disk, else why not,
    naming ``stored.path``."""
    try:
        data = Path(stored.path).read_bytes()
    except OSError as exc:
        return f"{stored.path} could not be read: {exc}"
    digest = sha256_hex(data)
    if digest != stored.sha256:
        return f"{stored.path} has changed: recorded {stored.sha256}, now {digest}"
    return None
