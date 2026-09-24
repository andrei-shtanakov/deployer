"""Record and snapshot of an authoring set (A §2.2). Strict and canonical."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from deployer.models import ProjectFacts

FORMAT_VERSION = "1"
NAMESPACE = "deployer-authoring"
PRINCIPAL = "deployer-authoring"
SET_ROOT = ".deployer/authoring"
POINTER = "Dockerfile.current"
SET_PARENT = "Dockerfile"
SNAPSHOT_FILE = "snapshot.json"
RECORD_FILE = "record.json"
SIGNATURE_FILE = "record.json.sig"


class TreeRow(BaseModel):
    """One entry of a recursive Git tree listing."""

    model_config = ConfigDict(extra="forbid")
    path: str
    mode: str
    type: str
    sha: str


class Snapshot(BaseModel):
    """The source state authoring started from, taken before any write."""

    model_config = ConfigDict(extra="forbid")
    format_version: Literal["1"]
    source_commit: str
    tree: list[TreeRow]
    tree_complete: bool
    facts: ProjectFacts


class Record(BaseModel):
    """What the signature covers: the artifact bytes and their source state."""

    model_config = ConfigDict(extra="forbid")
    format_version: Literal["1"]
    repo: str
    artifact_path: str
    artifact_sha256: str
    source_commit: str
    snapshot_sha256: str
    deployer_version: str


def canonical_bytes(model: BaseModel) -> bytes:
    """Sorted-key, compact JSON plus a newline: the bytes that are hashed and
    signed."""
    data = model.model_dump(mode="json")
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode() + b"\n"


def sha256_hex(data: bytes) -> str:
    """SHA-256 of ``data`` as lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def set_dir_name(record_sha256: str) -> str:
    """The set directory, relative to ``SET_ROOT``, named by the record's hash."""
    return f"{SET_PARENT}/{record_sha256}"
