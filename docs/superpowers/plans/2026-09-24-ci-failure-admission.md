# CI-Failure Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A signed authoring record lets `deployer diagnose --reproduce` decide, from existing evidence only, whether a failed run is a *proven defect of a deployer-authored Dockerfile* (`admitted`) or not (`insufficient_grounds`), and gives `ci-fix-authoring` one refusing-by-default entry point.

**Architecture:** Two new packages. `deployer.provenance` is the authoring side: record/snapshot models, `ssh-keygen -Y` signing, an out-of-repo trust store, local-git helpers, and issuing an immutable set under `.deployer/authoring/` behind an atomically replaced pointer. `deployer.admission` is the diagnosis side: a preparation layer (I/O: set verification, CI text to `ci.log`, ignore-file hashes) produces verified facts; a pure `decide()` turns them into the `admission` section (verdict schema 1.3); `accept_for_fix()` is the consumer gate. Neither touches `author_dockerfile` or `reproduce_run`.

**Tech Stack:** Python 3.12, `uv`, pydantic 2, OpenSSH `ssh-keygen -Y sign|verify` (present on the dev machine and GitHub runners), `git` CLI, pytest, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-09-24-ci-failure-admission-design.md` (rev 2.1 @ `d106df4`, cited **A**) and the reproduction spec `docs/superpowers/specs/2026-09-22-ci-failure-reproduction-design.md` (cited **R**). Read A in full before any task.

## Global Constraints

- `uv` only; after every change `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`, `uv run pytest` — all clean. Line length 88. Type hints and docstrings on public APIs.
- Signature namespace **and** principal: `deployer-authoring`. Record/snapshot `format_version`: `"1"`. Verdict schema with an `admission` key: `"1.3"` (distinct from the snapshot schema's own 1.3).
- Set layout (A §2.2): `.deployer/authoring/Dockerfile.current` (one line: `Dockerfile/<record_sha256>`), `.deployer/authoring/Dockerfile/<record_sha256>/{snapshot.json,record.json,record.json.sig}`. Directory name = SHA-256 of `record.json` bytes. Set directories are immutable once written.
- Trust store default `~/.config/deployer/`, override `DEPLOYER_TRUST_DIR`; files `allowed_signers`, `revoked_keys`. Its **real path** must lie outside the checked repository.
- Signing key: `--signing-key` or `DEPLOYER_SIGNING_KEY`. Authoring never reads the trust store.
- No network, no builds at diagnosis for admission; no log phrase is evidence on its own; unknown → `insufficient_grounds`.
- Exit codes unchanged (including R's two exit-2 cases).
- Only the Dockerfile gets a set in this slice. Defect catalogue closed: `missing_copy_source`, `from_argument_count`.
- Every GitHub call through `forge`; every container call through `deployer.runtime.container_run`; `git`/`ssh-keygen` calls each through one small chokepoint function in their module.
- PR shape (owner lesson): each PR ≤ 30 files and ≤ 400 KB of diff, passes all checks on its own; data bundles in their own PR for owner review. Merge with `--merge`; stacked PRs.

## Review Focus

1. A Dockerfile that is hand-edited *after* authoring and committed — ownership must read `not_confirmed` (step 4), never `admitted` (test in Task 7).
2. A repository whose remote is HTTPS vs SSH (`https://github.com/o/r.git`, `git@github.com:o/r.git`, with/without `.git`) — the record's `repo` must be the same `owner/name` (test in Task 4).
3. An authoring run interrupted after the new set directory is written but before the pointer rename — the old set still verifies internally, the current Dockerfile is not confirmed (test in Task 5).
4. `ssh-keygen` missing from `PATH` — authoring issues no set with a named reason, diagnosis reports ownership not confirmed; neither crashes (tests in Tasks 2 and 7).
5. A verdict document produced before this feature (schema 1.2, no `admission`) handed to `accept_for_fix` — refused, never an exception (test in Task 12).

---

## PR plan (stacked, `--merge`)

| PR | Branch | Tasks | Review |
|---|---|---|---|
| A1 | `feat/admission-1-provenance` | 1–4 | ai-prosto |
| A2 | `feat/admission-2-authoring` | 5–6 | ai-prosto |
| A3 | `feat/admission-3-decision` | 7–12 | ai-prosto |
| A4 | `feat/admission-4-bundles` | 13 | **owner** (data) |
| A5 | `feat/admission-5-acceptance` | 14 | ai-prosto |

Retarget each dependent PR to `master` after its base merges (GitHub does it on auto-delete; verify).

## File Structure

| File | Responsibility |
|---|---|
| `src/deployer/provenance/__init__.py` | package docstring |
| `src/deployer/provenance/model.py` | `Snapshot`, `Record`, `TreeRow`, canonical bytes, hashing, layout constants |
| `src/deployer/provenance/sshsig.py` | the `ssh-keygen` chokepoint: sign, verify, public key |
| `src/deployer/provenance/trust.py` | trust dir resolution, outside-repo check, add/replace/revoke |
| `src/deployer/provenance/gitrepo.py` | the `git` chokepoint: checkout, origin slug, dirty paths, head, listing, export |
| `src/deployer/provenance/issue.py` | preflight, exclusion, build/sign/publish/reuse, withdraw |
| `src/deployer/admission/__init__.py` | re-exports |
| `src/deployer/admission/model.py` | `AdmissionSection` and invariants |
| `src/deployer/admission/ownership.py` | A §2.4 steps 0–6 → `OwnershipFacts` |
| `src/deployer/admission/templates.py` | the closed row table (A §4.1) and matchers |
| `src/deployer/admission/decide.py` | `VerifiedFacts` → `AdmissionSection` (pure) |
| `src/deployer/admission/prepare.py` | I/O layer: `prepare()` → `VerifiedFacts` |
| `src/deployer/admission/consumer.py` | `accept_for_fix()` |
| `src/deployer/cli.py` (modify) | `author --signing-key`, `trust …`, diagnose wiring, printing |
| `src/deployer/diagnose.py` (modify) | `render_verdict(…, admission=None)` → 1.3 |
| `tests/provenance/…`, `tests/admission/…` | unit tests per module |
| `tests/fixtures/admission/…` | A4 bundles + test keys + integrity |

---

### Task 1: Provenance model

**Files:** Create `src/deployer/provenance/__init__.py`, `src/deployer/provenance/model.py`; Test `tests/provenance/__init__.py` (empty), `tests/provenance/test_model.py`.

**Interfaces — Produces:**
- `FORMAT_VERSION = "1"`, `NAMESPACE = "deployer-authoring"`, `PRINCIPAL = "deployer-authoring"`
- `SET_ROOT = ".deployer/authoring"`, `POINTER = "Dockerfile.current"`, `SET_PARENT = "Dockerfile"`, file names `SNAPSHOT_FILE = "snapshot.json"`, `RECORD_FILE = "record.json"`, `SIGNATURE_FILE = "record.json.sig"`
- `class TreeRow(BaseModel)`: `path: str, mode: str, type: str, sha: str` (all required, `extra="forbid"`)
- `class Snapshot(BaseModel)`: `format_version: Literal["1"]`, `source_commit: str`, `tree: list[TreeRow]`, `tree_complete: bool`, `facts: ProjectFacts`; `extra="forbid"`
- `class Record(BaseModel)`: `format_version: Literal["1"]`, `repo: str`, `artifact_path: str`, `artifact_sha256: str`, `source_commit: str`, `snapshot_sha256: str`, `deployer_version: str`; `extra="forbid"`
- `canonical_bytes(model: BaseModel) -> bytes` — `json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"`
- `sha256_hex(data: bytes) -> str`
- `set_dir_name(record_sha256: str) -> str` → `f"Dockerfile/{record_sha256}"`

- [ ] **Step 1: Failing tests**

```python
"""Record/snapshot models: strict, canonical, hashable."""

import pytest
from pydantic import ValidationError

from deployer.models import ProjectFacts
from deployer.provenance.model import (
    Record,
    Snapshot,
    TreeRow,
    canonical_bytes,
    set_dir_name,
    sha256_hex,
)


def _snapshot(**kw) -> Snapshot:
    base = dict(
        format_version="1",
        source_commit="a" * 40,
        tree=[TreeRow(path="Dockerfile", mode="100644", type="blob", sha="b" * 40)],
        tree_complete=True,
        facts=ProjectFacts(name="p"),
    )
    return Snapshot(**{**base, **kw})


def test_canonical_bytes_are_stable_and_sorted():
    snap = _snapshot()
    assert canonical_bytes(snap) == canonical_bytes(_snapshot())
    assert canonical_bytes(snap).endswith(b"\n")
    assert b'"format_version":"1"' in canonical_bytes(snap)


def test_unknown_format_version_is_rejected():
    with pytest.raises(ValidationError):
        _snapshot(format_version="2")


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        Record.model_validate(
            {"format_version": "1", "repo": "o/r", "artifact_path": "Dockerfile",
             "artifact_sha256": "0" * 64, "source_commit": "a" * 40,
             "snapshot_sha256": "1" * 64, "deployer_version": "0.1", "extra": 1}
        )


def test_set_dir_is_named_by_the_record_hash():
    assert set_dir_name("c" * 64) == "Dockerfile/" + "c" * 64
    assert sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
```

- [ ] **Step 2:** `uv run pytest tests/provenance/test_model.py -v` → FAIL (import error).
- [ ] **Step 3: Implement** `src/deployer/provenance/__init__.py`:

```python
"""Authoring provenance (spec 2026-09-24 A §2, §5): a signed record that ties a
Dockerfile's bytes to the trusted authoring system and its source state."""
```

`src/deployer/provenance/model.py`:

```python
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
    """Sorted-key, compact JSON plus a newline: the bytes that are hashed and signed."""
    data = model.model_dump(mode="json")
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode() + b"\n"


def sha256_hex(data: bytes) -> str:
    """SHA-256 of ``data`` as lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def set_dir_name(record_sha256: str) -> str:
    """The set directory, relative to ``SET_ROOT``, named by the record's hash."""
    return f"{SET_PARENT}/{record_sha256}"
```

If `ProjectFacts` lacks `extra="forbid"` that is fine: only the new models forbid extras.

- [ ] **Step 4:** tests PASS. **Step 5:** format/lint/type, commit `feat(provenance): record and snapshot models with canonical bytes`.

---

### Task 2: `ssh-keygen` chokepoint

**Files:** Create `src/deployer/provenance/sshsig.py`; Test `tests/provenance/test_sshsig.py`, `tests/provenance/conftest.py`.

**Interfaces — Produces:**
- `class SshSigError(Exception)` (message is the reason)
- `sign(data: bytes, key: Path) -> bytes` — raises `SshSigError`
- `public_key(key: Path) -> str` — the `.pub` line (`ssh-keygen -y -f key`), raises `SshSigError`
- `@dataclass(frozen=True) Verified(ok: bool, fingerprint: str | None, reason: str | None)`
- `verify(data: bytes, signature: bytes, allowed_signers: Path, revoked: Path | None) -> Verified` — never raises
- `verify_with_public_key(data: bytes, signature: bytes, public_line: str) -> Verified` — builds a temporary allowed_signers
- fixture `keypair(tmp_path) -> tuple[Path, str]` in `tests/provenance/conftest.py` (private key path, public line)

- [ ] **Step 1: Tests**

`tests/provenance/conftest.py`:

```python
import subprocess
from pathlib import Path

import pytest


def make_key(directory: Path, name: str = "k") -> tuple[Path, str]:
    key = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
    )
    return key, (directory / f"{name}.pub").read_text().strip()


@pytest.fixture()
def keypair(tmp_path: Path) -> tuple[Path, str]:
    return make_key(tmp_path)
```

`tests/provenance/test_sshsig.py`:

```python
"""The ssh-keygen chokepoint: sign, verify, revoke; never crashes on a bad input."""

from pathlib import Path

from deployer.provenance import sshsig
from deployer.provenance.model import PRINCIPAL, NAMESPACE
from tests.provenance.conftest import make_key


def _allowed(tmp_path: Path, public_line: str) -> Path:
    f = tmp_path / "allowed_signers"
    f.write_text(f'{PRINCIPAL} namespaces="{NAMESPACE}" {public_line}\n')
    return f


def test_sign_then_verify_reports_the_fingerprint(tmp_path, keypair):
    key, pub = keypair
    sig = sshsig.sign(b"record\n", key)
    result = sshsig.verify(b"record\n", sig, _allowed(tmp_path, pub), None)
    assert result.ok and result.fingerprint and result.fingerprint.startswith("SHA256:")


def test_changed_data_unknown_key_and_revoked_key_fail(tmp_path, keypair):
    key, pub = keypair
    sig = sshsig.sign(b"record\n", key)
    allowed = _allowed(tmp_path, pub)
    assert not sshsig.verify(b"other\n", sig, allowed, None).ok
    _, other_pub = make_key(tmp_path, "other")
    assert not sshsig.verify(b"record\n", sig, _allowed(tmp_path / "..", other_pub), None).ok
    revoked = tmp_path / "revoked_keys"
    revoked.write_text(pub + "\n")
    assert not sshsig.verify(b"record\n", sig, allowed, revoked).ok


def test_garbage_signature_is_a_failure_not_an_exception(tmp_path, keypair):
    _, pub = keypair
    result = sshsig.verify(b"x", b"not a signature", _allowed(tmp_path, pub), None)
    assert (result.ok, result.fingerprint) == (False, None)


def test_verify_with_public_key(tmp_path, keypair):
    key, pub = keypair
    sig = sshsig.sign(b"r\n", key)
    assert sshsig.verify_with_public_key(b"r\n", sig, pub).ok
    assert sshsig.public_key(key) == pub.rsplit(" ", 1)[0] or sshsig.public_key(key).startswith("ssh-ed25519 ")


def test_missing_ssh_keygen_is_a_named_failure(monkeypatch, tmp_path, keypair):  # Review Focus 4
    key, pub = keypair
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = sshsig.verify(b"r", b"s", _allowed(tmp_path, pub), None)
    assert not result.ok and "ssh-keygen" in (result.reason or "")
    try:
        sshsig.sign(b"r", key)
    except sshsig.SshSigError as exc:
        assert "ssh-keygen" in str(exc)
    else:
        raise AssertionError("sign must fail without ssh-keygen")
```

(`_allowed(tmp_path / "..", …)` writes a second allowed file elsewhere; if pyrefly or the filesystem objects, use `tmp_path / "other"` after `mkdir`.)

- [ ] **Step 2:** FAIL. **Step 3: Implement**

```python
"""The ssh-keygen chokepoint (A §2.2-§2.4): sign and verify authoring records."""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from deployer.provenance.model import NAMESPACE, PRINCIPAL

_TIMEOUT_S = 30
_FINGERPRINT_RE = re.compile(r"key (SHA256:\S+)")


class SshSigError(Exception):
    """ssh-keygen could not sign or read a key; the message names the reason."""


@dataclass(frozen=True)
class Verified:
    """A verification outcome; the fingerprint exists only when ``ok``."""

    ok: bool
    fingerprint: str | None
    reason: str | None


def _run(args: list[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["ssh-keygen", *args], input=stdin, capture_output=True, timeout=_TIMEOUT_S
    )


def sign(data: bytes, key: Path) -> bytes:
    """Sign ``data`` in the ``deployer-authoring`` namespace with ``key``."""
    try:
        proc = _run(["-Y", "sign", "-f", str(key), "-n", NAMESPACE], data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshSigError(f"ssh-keygen sign failed: {exc}") from exc
    if proc.returncode != 0:
        raise SshSigError(f"ssh-keygen sign failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def public_key(key: Path) -> str:
    """The public line of a private key (``type base64``)."""
    try:
        proc = _run(["-y", "-f", str(key)], b"")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshSigError(f"ssh-keygen -y failed: {exc}") from exc
    if proc.returncode != 0:
        raise SshSigError(f"ssh-keygen -y failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode().strip()


def verify(
    data: bytes, signature: bytes, allowed_signers: Path, revoked: Path | None
) -> Verified:
    """Verify against an allowed_signers file; never raises."""
    with tempfile.TemporaryDirectory() as tmp:
        sig_path = Path(tmp) / "sig"
        sig_path.write_bytes(signature)
        args = ["-Y", "verify", "-f", str(allowed_signers), "-I", PRINCIPAL,
                "-n", NAMESPACE, "-s", str(sig_path)]
        if revoked is not None:
            args += ["-r", str(revoked)]
        try:
            proc = _run(args, data)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Verified(False, None, f"ssh-keygen verify could not run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode(errors="replace").strip()
        return Verified(False, None, f"signature not verified: {detail or proc.returncode}")
    match = _FINGERPRINT_RE.search(proc.stdout.decode(errors="replace"))
    if match is None:
        return Verified(False, None, "signature verified but no key fingerprint reported")
    return Verified(True, match.group(1), None)


def verify_with_public_key(data: bytes, signature: bytes, public_line: str) -> Verified:
    """Verify against one public key (authoring's own reuse check, A §5.2)."""
    with tempfile.TemporaryDirectory() as tmp:
        allowed = Path(tmp) / "allowed_signers"
        allowed.write_text(f'{PRINCIPAL} namespaces="{NAMESPACE}" {public_line}\n')
        return verify(data, signature, allowed, None)
```

`OSError` covers `FileNotFoundError` when `ssh-keygen` is not on `PATH`; its message contains `ssh-keygen`. Wrap long lines with ruff.

- [ ] **Step 4:** PASS. **Step 5:** commit `feat(provenance): ssh-keygen chokepoint for signing and verifying records`.

---

### Task 3: Trust store and `deployer trust`

**Files:** Create `src/deployer/provenance/trust.py`; Modify `src/deployer/cli.py` (subcommand `trust`); Test `tests/provenance/test_trust.py`, `tests/test_cli.py`.

**Interfaces — Produces:**
- `ALLOWED_FILE = "allowed_signers"`, `REVOKED_FILE = "revoked_keys"`, `DEFAULT_TRUST_DIR = Path.home() / ".config" / "deployer"`
- `trust_dir(env: Mapping[str, str]) -> Path` — `DEPLOYER_TRUST_DIR` or default (not resolved)
- `outside(trust: Path, *repos: Path) -> str | None` — `None` if `trust.resolve(strict=False)` lies outside every `repo.resolve()`, else a reason naming the repo
- `add(trust: Path, public_line: str) -> None`, `revoke(trust: Path, public_line: str) -> None`, `replace(trust: Path, old_line: str, new_line: str) -> None`
- CLI: `deployer trust add|revoke <pubkey-file>`, `deployer trust replace <old-pubkey-file> <new-pubkey-file>`; exit 0 / 2 on a bad file.

- [ ] **Step 1: Tests** (`tests/provenance/test_trust.py`)

```python
import os

from deployer.provenance import trust


def test_trust_dir_default_and_override(tmp_path):
    assert trust.trust_dir({}) == trust.DEFAULT_TRUST_DIR
    assert trust.trust_dir({"DEPLOYER_TRUST_DIR": str(tmp_path)}) == tmp_path


def test_inside_the_repo_directly_or_via_symlink_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / "t").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    assert trust.outside(outside_dir, repo) is None
    assert trust.outside(repo / "t", repo) is not None
    link = tmp_path / "link"
    os.symlink(repo / "t", link)
    assert trust.outside(link, repo) is not None


def test_add_revoke_replace(tmp_path):
    a, b = "ssh-ed25519 AAAAone a", "ssh-ed25519 AAAAtwo b"
    trust.add(tmp_path, a)
    assert "AAAAone" in (tmp_path / trust.ALLOWED_FILE).read_text()
    trust.replace(tmp_path, a, b)
    allowed = (tmp_path / trust.ALLOWED_FILE).read_text()
    assert "AAAAtwo" in allowed and "AAAAone" not in allowed
    assert "AAAAone" in (tmp_path / trust.REVOKED_FILE).read_text()
    trust.revoke(tmp_path, b)
    assert "AAAAtwo" not in (tmp_path / trust.ALLOWED_FILE).read_text()
```

In `tests/test_cli.py`:

```python
def test_trust_add_writes_the_allowed_signers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEPLOYER_TRUST_DIR", str(tmp_path / "trust"))
    pub = tmp_path / "k.pub"
    pub.write_text("ssh-ed25519 AAAAone c\n")
    assert cli.main(["trust", "add", str(pub)]) == 0
    assert "AAAAone" in (tmp_path / "trust" / "allowed_signers").read_text()
    assert cli.main(["trust", "add", str(tmp_path / "missing.pub")]) == 2
```

- [ ] **Step 2:** FAIL. **Step 3: Implement** `trust.py`:

```python
"""The trust store, outside the checked repository (A §2.3)."""

from collections.abc import Mapping
from pathlib import Path

from deployer.provenance.model import NAMESPACE, PRINCIPAL

ALLOWED_FILE = "allowed_signers"
REVOKED_FILE = "revoked_keys"
DEFAULT_TRUST_DIR = Path.home() / ".config" / "deployer"


def trust_dir(env: Mapping[str, str]) -> Path:
    """``DEPLOYER_TRUST_DIR`` or the default; not yet resolved."""
    override = env.get("DEPLOYER_TRUST_DIR")
    return Path(override) if override else DEFAULT_TRUST_DIR


def outside(trust: Path, *repos: Path) -> str | None:
    """``None`` when the trust dir's real path is outside every repo, else why not."""
    real = trust.resolve(strict=False)
    for repo in repos:
        root = repo.resolve(strict=False)
        if real == root or root in real.parents:
            return f"trust directory {real} lies inside {root}"
    return None


def _key_body(public_line: str) -> str:
    """``type base64`` without the comment, for comparisons."""
    return " ".join(public_line.split()[:2])


def add(trust: Path, public_line: str) -> None:
    """Allow a key for ``deployer-authoring``."""
    trust.mkdir(parents=True, exist_ok=True)
    allowed = trust / ALLOWED_FILE
    body = _key_body(public_line)
    existing = allowed.read_text() if allowed.is_file() else ""
    if body not in existing:
        with allowed.open("a") as f:
            f.write(f'{PRINCIPAL} namespaces="{NAMESPACE}" {body}\n')


def revoke(trust: Path, public_line: str) -> None:
    """Drop a key from the allowed set and list it as revoked."""
    trust.mkdir(parents=True, exist_ok=True)
    body = _key_body(public_line)
    allowed = trust / ALLOWED_FILE
    if allowed.is_file():
        kept = [ln for ln in allowed.read_text().splitlines() if body not in ln]
        allowed.write_text("".join(f"{ln}\n" for ln in kept))
    with (trust / REVOKED_FILE).open("a") as f:
        f.write(f"{body}\n")


def replace(trust: Path, old_line: str, new_line: str) -> None:
    """Add the new key, then revoke the old one. No rotation beyond this."""
    add(trust, new_line)
    revoke(trust, old_line)
```

CLI (`cli.py`): a `trust` subparser with `trust_command` in `{add, revoke, replace}`; read each pubkey file (`OSError`/empty → print error, return 2); call `trust.add/revoke/replace(trust.trust_dir(os.environ), …)`; print the directory used; return 0. Follow the existing `bench` subparser pattern.

- [ ] **Step 4:** PASS. **Step 5:** commit `feat(provenance): out-of-repo trust store and deployer trust add|replace|revoke`.

---

### Task 4: `git` chokepoint

**Files:** Create `src/deployer/provenance/gitrepo.py`; Test `tests/provenance/test_gitrepo.py`.

**Interfaces — Produces:**
- `class GitError(Exception)`
- `is_checkout(path: Path) -> bool`
- `origin_slug(path: Path) -> str | None` — `owner/name` from `origin` (SSH `git@host:o/r(.git)`, `ssh://git@host/o/r(.git)`, `https://host/o/r(.git)`); `None` if no origin or unparseable
- `dirty_paths(path: Path) -> list[str]` — `git status --porcelain=v1 --untracked-files=all -z`; staged, unstaged and untracked
- `head_commit(path: Path) -> str`
- `tree_listing(path: Path, commit: str) -> list[TreeRow]` — `git ls-tree -r -t --full-tree -z <commit>`
- `export_commit(path: Path, commit: str, dest: Path) -> None` — `git archive --format=tar <commit>` extracted with `tarfile` `filter="data"`

- [ ] **Step 1: Tests**

```python
import subprocess
from pathlib import Path

import pytest

from deployer.provenance import gitrepo


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "pyproject.toml").write_text('[project]\nname = "p"\n')
    (r / "src").mkdir()
    (r / "src" / "m.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:o/r.git", "o/r"),
        ("git@github.com:o/r", "o/r"),
        ("https://github.com/o/r.git", "o/r"),
        ("https://github.com/o/r", "o/r"),
        ("ssh://git@github.com/o/r.git", "o/r"),
    ],
)
def test_origin_slug_forms(repo, url, slug):  # Review Focus 2
    _git(repo, "remote", "add", "origin", url)
    assert gitrepo.origin_slug(repo) == slug


def test_no_origin_and_not_a_checkout(repo, tmp_path):
    assert gitrepo.origin_slug(repo) is None
    assert gitrepo.is_checkout(repo) and not gitrepo.is_checkout(tmp_path / "nope")


def test_dirty_paths_cover_staged_unstaged_untracked(repo):
    assert gitrepo.dirty_paths(repo) == []
    (repo / "src" / "m.py").write_text("x = 2\n")
    (repo / "new.txt").write_text("n")
    (repo / "staged.txt").write_text("s")
    _git(repo, "add", "staged.txt")
    assert sorted(gitrepo.dirty_paths(repo)) == ["new.txt", "src/m.py", "staged.txt"]


def test_listing_and_export(repo, tmp_path):
    head = gitrepo.head_commit(repo)
    rows = gitrepo.tree_listing(repo, head)
    assert {r.path for r in rows} == {"pyproject.toml", "src", "src/m.py"}
    dest = tmp_path / "export"
    gitrepo.export_commit(repo, head, dest)
    assert (dest / "src" / "m.py").read_text() == "x = 1\n"
```

- [ ] **Step 2:** FAIL. **Step 3: Implement**

```python
"""The local-git chokepoint for authoring provenance (A §5.1)."""

import io
import re
import subprocess
import tarfile
from pathlib import Path

from deployer.provenance.model import TreeRow

_TIMEOUT_S = 60
_SLUG_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


class GitError(Exception):
    """A git command failed; the message names it."""


def _git(path: Path, *args: str) -> bytes:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, timeout=_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} could not run: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def is_checkout(path: Path) -> bool:
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except GitError:
        return False


def origin_slug(path: Path) -> str | None:
    try:
        url = _git(path, "remote", "get-url", "origin").decode().strip()
    except GitError:
        return None
    match = _SLUG_RE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def dirty_paths(path: Path) -> list[str]:
    out = _git(path, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    paths: list[str] = []
    entries = out.decode(errors="replace").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        paths.append(name)
        if "R" in status or "C" in status:
            i += 1  # the rename/copy source follows as its own field
    return paths


def head_commit(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").decode().strip()


def tree_listing(path: Path, commit: str) -> list[TreeRow]:
    out = _git(path, "ls-tree", "-r", "-t", "--full-tree", "-z", commit)
    rows: list[TreeRow] = []
    for record in out.decode(errors="replace").split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        mode, kind, sha = meta.split()
        rows.append(TreeRow(path=name, mode=mode, type=kind, sha=sha))
    return rows


def export_commit(path: Path, commit: str, dest: Path) -> None:
    data = _git(path, "archive", "--format=tar", commit)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
```

- [ ] **Step 4:** PASS. **Step 5:** commit `feat(provenance): git chokepoint — origin slug, dirty paths, listing, export`. **Open PR A1** (base `master`), run the repo's review, merge on approve.

---

### Task 5: Issuing a set

**Files:** Create `src/deployer/provenance/issue.py`; Test `tests/provenance/test_issue.py`.

**Interfaces:**
- Consumes: Tasks 1, 2, 4; `deployer.facts.analyze_project`; `deployer.reproduce.ignore.{ci_ignore_file, local_ignore_file, load_rules, excluded_by}`.
- Produces:
  - `@dataclass(frozen=True) Preflight(project: Path, repo: str, source_commit: str, tree: list[TreeRow], facts: ProjectFacts)`
  - `preflight(project: Path, signing_key: Path | None) -> Preflight | str` — run **before** authoring writes anything; `str` is the reason no set will be issued (not a checkout; no origin; dirty tree listing the paths; no signing key; `ssh-keygen` unusable)
  - `@dataclass(frozen=True) Issued(published: bool, reason: str | None, set_dir: str | None)`
  - `issue(pre: Preflight, signing_key: Path, deployer_version: str) -> Issued` — after authoring wrote the Dockerfile
  - `withdraw(project: Path) -> bool` — removes `Dockerfile.current` first, then `Dockerfile/*`; returns whether anything was removed

`issue()` in order (A §5.2):
1. `analyze_project(pre.project)` must equal `pre.facts` (computed in `preflight` from `export_commit(source_commit)` into a temp dir). Differ → `Issued(False, "facts depend on files outside source_commit", None)`.
2. Build `Snapshot(format_version="1", source_commit, tree=pre.tree, tree_complete=True, facts=pre.facts)` → `snap_bytes = canonical_bytes(...)`; `Record(..., artifact_sha256=sha256_hex(Dockerfile bytes), snapshot_sha256=sha256_hex(snap_bytes))` → `rec_bytes`; `rec_sha = sha256_hex(rec_bytes)`.
3. Exclusion: `ensure_excluded(project, paths)` where `paths` = the pointer and the three set files under `SET_ROOT/set_dir_name(rec_sha)`. It appends `.deployer/` to the effective CI ignore file (create root `.dockerignore` if none) and to `.containerignore` if present (only if not already excluding), then checks every path with `excluded_by(load_rules(...), path) is not None` for both `ci_ignore_file(project, "Dockerfile")` and `local_ignore_file(project, "Dockerfile", "podman")`; any rules with `unsupported` → not proven. Not proven → `Issued(False, "<reason>", None)`.
4. `sig = sshsig.sign(rec_bytes, signing_key)` (`SshSigError` → `Issued(False, reason, None)`).
5. Publish: `target = project/SET_ROOT/set_dir_name(rec_sha)`. If it exists: reuse only if `record.json` bytes == `rec_bytes`, `snapshot.json` bytes == `snap_bytes`, and `sshsig.verify_with_public_key(rec_bytes, existing_sig, sshsig.public_key(signing_key)).ok`; else `Issued(False, "existing set <dir> does not match; not written", None)`. If absent: write the three files into `target.parent / f".tmp-{rec_sha}-{os.getpid()}"` then `os.rename` to `target`. Then write the pointer to a temp file in `SET_ROOT` and `os.replace` it onto `Dockerfile.current`. Then remove every other directory under `SET_ROOT/Dockerfile/`. Return `Issued(True, None, set_dir_name(rec_sha))`.

- [ ] **Step 1: Tests** (use the `repo` fixture from Task 4 copied into `tests/provenance/conftest.py`, plus `keypair`; commit a `Dockerfile` so the tree is clean, then simulate authoring by writing a new Dockerfile after `preflight`):

```python
"""Issuing: preflight gates, exclusion, immutable dirs, atomic pointer, reuse, withdraw."""

import os
import subprocess
from pathlib import Path

from deployer.provenance import issue, sshsig
from deployer.provenance.model import POINTER, SET_ROOT, Record, sha256_hex

DOCKERFILE = "FROM python:3.12-slim\nCOPY src ./src\n"


def _author(repo: Path, text: str = DOCKERFILE) -> None:
    (repo / "Dockerfile").write_text(text)


def _pointer(repo: Path) -> str:
    return (repo / SET_ROOT / POINTER).read_text().strip()


def test_issue_publishes_a_verifiable_set(repo_with_origin, keypair):
    key, pub = keypair
    pre = issue.preflight(repo_with_origin, key)
    assert isinstance(pre, issue.Preflight)
    _author(repo_with_origin)
    out = issue.issue(pre, key, "0.1")
    assert out.published and out.set_dir == _pointer(repo_with_origin)
    set_dir = repo_with_origin / SET_ROOT / out.set_dir
    rec = (set_dir / "record.json").read_bytes()
    assert set_dir.name == sha256_hex(rec)
    assert Record.model_validate_json(rec).artifact_sha256 == sha256_hex(
        (repo_with_origin / "Dockerfile").read_bytes()
    )
    assert sshsig.verify_with_public_key(rec, (set_dir / "record.json.sig").read_bytes(), pub).ok
    assert ".deployer/" in (repo_with_origin / ".dockerignore").read_text()


def test_preflight_refusals(repo, repo_with_origin, keypair, tmp_path):
    key, _ = keypair
    assert "origin" in issue.preflight(repo, key)
    assert "not a Git checkout" in issue.preflight(tmp_path, key)
    assert "signing key" in issue.preflight(repo_with_origin, None)
    (repo_with_origin / "Dockerfile").write_text("hand edit\n")  # untracked
    assert "dirty" in issue.preflight(repo_with_origin, key)


def test_a_fact_from_an_untracked_ignored_file_blocks_the_set(repo_with_origin, keypair):
    key, _ = keypair
    (repo_with_origin / ".gitignore").write_text(".python-version\n")
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_with_origin), "commit", "-qm", "ig"], check=True)
    (repo_with_origin / ".python-version").write_text("3.11\n")  # ignored, read by facts
    pre = issue.preflight(repo_with_origin, key)
    _author(repo_with_origin)
    assert not issue.issue(pre, key, "0.1").published


def test_interrupted_before_the_pointer_keeps_the_old_set(repo_with_origin, keypair, monkeypatch):  # Review Focus 3
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1")
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_with_origin), "commit", "-qm", "a"], check=True)
    pre2 = issue.preflight(repo_with_origin, key)
    _author(repo_with_origin, DOCKERFILE + "CMD [\"python\"]\n")
    real_replace = os.replace
    def boom(src, dst):
        if str(dst).endswith(POINTER):
            raise OSError("killed")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", boom)
    try:
        issue.issue(pre2, key, "0.1")
    except OSError:
        pass
    assert _pointer(repo_with_origin) == first.set_dir  # old set still named, intact
    rec = Record.model_validate_json(
        (repo_with_origin / SET_ROOT / first.set_dir / "record.json").read_bytes()
    )
    assert rec.artifact_sha256 != sha256_hex((repo_with_origin / "Dockerfile").read_bytes())


def test_reissuing_the_same_record_reuses_without_writing(repo_with_origin, keypair):
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    _author(repo_with_origin)
    first = issue.issue(pre, key, "0.1")
    set_dir = repo_with_origin / SET_ROOT / first.set_dir
    mtimes = {p.name: p.stat().st_mtime_ns for p in set_dir.iterdir()}
    again = issue.issue(pre, key, "0.1")
    assert again.published and again.set_dir == first.set_dir
    assert {p.name: p.stat().st_mtime_ns for p in set_dir.iterdir()} == mtimes
    (set_dir / "snapshot.json").write_text("{}")  # tampered
    assert not issue.issue(pre, key, "0.1").published


def test_withdraw_removes_pointer_then_dirs(repo_with_origin, keypair):
    key, _ = keypair
    pre = issue.preflight(repo_with_origin, key)
    _author(repo_with_origin)
    issue.issue(pre, key, "0.1")
    assert issue.withdraw(repo_with_origin)
    assert not (repo_with_origin / SET_ROOT / POINTER).exists()
    assert not any((repo_with_origin / SET_ROOT / "Dockerfile").iterdir())


def test_exclusion_not_provable_blocks_the_set(repo_with_origin, keypair):
    key, _ = keypair
    (repo_with_origin / ".dockerignore").write_text("[ab]\n")  # unmodelled pattern
    subprocess.run(["git", "-C", str(repo_with_origin), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_with_origin), "commit", "-qm", "d"], check=True)
    pre = issue.preflight(repo_with_origin, key)
    _author(repo_with_origin)
    assert "exclusion" in (issue.issue(pre, key, "0.1").reason or "")
```

Fixtures to add to `tests/provenance/conftest.py`: `repo` (as in Task 4) and `repo_with_origin` (the same plus `git remote add origin git@github.com:o/r.git`). Note that `.python-version` must be a file `analyze_project` reads (it does: `facts.py` reads `.python-version`).

- [ ] **Step 2:** FAIL. **Step 3: Implement** `issue.py` following the order above. Keep each step a small function (`_facts_from_commit`, `_build`, `ensure_excluded`, `_publish`). `preflight` catches `GitError` and returns its message; it also calls `sshsig.public_key(signing_key)` to prove the key and `ssh-keygen` are usable. Use `tempfile.TemporaryDirectory()` for the export.

- [ ] **Step 4:** PASS. **Step 5:** commit `feat(provenance): issue an immutable, signed, excluded set behind an atomic pointer`.

---

### Task 6: `deployer author --signing-key`

**Files:** Modify `src/deployer/cli.py` (`_cmd_author`, author parser); Test `tests/test_cli.py`.

**Behaviour:** add `--signing-key` (default `os.environ.get("DEPLOYER_SIGNING_KEY")`). In `_cmd_author`, before `author_dockerfile`: `pre = issue.preflight(project, key)`; if `str`, print `warning: ownership will not be confirmable: <reason>` to stderr. After the Dockerfile is written (existing block): if `pre` is a `Preflight`, `out = issue.issue(pre, Path(key), deployer_version())`; if not published, print the warning with `out.reason` and call `issue.withdraw(project)` (print `warning: previous authoring set removed` when it returns `True`). If no Dockerfile was written, `withdraw` too. Exit codes unchanged. `deployer_version()` from `importlib.metadata.version("deployer")`.

- [ ] **Step 1: Tests** — with `monkeypatch.setattr(cli, "author_dockerfile", fake)` returning an `AuthoringRun` with one iteration whose Dockerfile is `DOCKERFILE` and a passing report (follow the existing author CLI tests in `tests/test_cli.py`), and `repo_with_origin` + `keypair`:
  - with `--signing-key`: exit as before, `.deployer/authoring/Dockerfile.current` exists;
  - without a key: stderr has `ownership will not be confirmable`, no pointer;
  - dirty tree (untracked file) with a key: warning, no pointer, and a pre-existing set from a previous clean run is removed.
- [ ] **Step 2–4:** red → implement → green. **Step 5:** commit `feat(cli): author issues an authoring set with --signing-key`. **Open PR A2** (stacked on A1).

---

### Task 7: Ownership verification (A §2.4)

**Files:** Create `src/deployer/admission/__init__.py` (docstring only for now), `src/deployer/admission/ownership.py`; Test `tests/admission/__init__.py`, `tests/admission/test_ownership.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) OwnershipFacts(status: Literal["confirmed","not_confirmed"], step: int | None, reason: str | None, key_fingerprint: str | None, record_sha256: str | None, snapshot_sha256: str | None, record: Record | None, snapshot: Snapshot | None)`
- `verify_ownership(source_dir: Path, *, repo: str, artifact_path: str, trust: Path, checked_roots: tuple[Path, ...]) -> OwnershipFacts` — steps 0–6 in order; the first failure returns `not_confirmed` with `step` and `reason`; hashes filled only when obtained; fingerprint only after step 3 passes.

Steps exactly as A §2.4. Step 0: `trust.outside(trust, *checked_roots)`; also require `trust/allowed_signers` to exist. Step 1: pointer parses as `Dockerfile/<64 hex>`, the three files exist, both JSONs validate (`Record`/`Snapshot`; `ValidationError` → reason names the file). Step 2: `sha256_hex(record bytes) == dir name`. Step 3: `sshsig.verify(rec_bytes, sig, trust/allowed_signers, trust/revoked_keys if exists)`. Step 4: artifact hash vs `source_dir/artifact_path` bytes (missing file → reason), `repo` and `artifact_path` equality. Step 5: snapshot hash. Step 6: `tree_complete`, no duplicate paths, `source_commit` equal.

- [ ] **Step 1: Tests** — build sets with `provenance.issue` in a temp git repo (reuse `tests/provenance/conftest.py` fixtures by importing them), copy the repo tree to a `source_dir`, then mutate one thing per test, re-signing via `sshsig.sign` where the step needs it:

```python
@pytest.mark.parametrize("step,mutate", [
    (1, "remove_pointer"), (1, "pointer_to_missing_dir"), (1, "record_unknown_format"),
    (1, "snapshot_unknown_format"), (2, "rename_dir"), (3, "bad_signature"),
    (3, "unknown_key"), (3, "revoked_key"), (4, "hand_edit_dockerfile"),  # Review Focus 1
    (4, "foreign_repo_resigned"), (4, "foreign_path_resigned"),
    (5, "snapshot_edited"), (6, "source_commit_differs_resigned"),
    (6, "tree_incomplete_resigned"), (6, "row_missing_field_resigned"),
    (6, "duplicate_path_resigned"),
])
def test_each_step_refuses(step, mutate, admission_set):
    facts = admission_set.apply(mutate)
    assert facts.status == "not_confirmed" and facts.step == step
    assert (facts.key_fingerprint is None) == (step <= 3)
```

plus `test_trust_inside_the_checked_tree_is_step_0` (direct and via symlink), `test_happy_path_confirms_with_fingerprint`, and `test_missing_ssh_keygen_is_not_confirmed` (empty `PATH`; Review Focus 4). Implement the `admission_set` fixture in `tests/admission/conftest.py` as a small helper class whose `apply(name)` performs the named mutation (for `*_resigned`: rewrite the JSON, recompute `snapshot_sha256` where the snapshot changed, rewrite the record, re-sign with the test key, move the directory to the new record hash, rewrite the pointer).

- [ ] **Step 2–4.** **Step 5:** commit `feat(admission): ownership verification, A §2.4 steps 0-6`.

---

### Task 8: The template table (A §4.1)

**Files:** Create `src/deployer/admission/templates.py`; Test `tests/admission/test_templates.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) Row(id: str, cls: Literal["missing_copy_source","from_argument_count"], side: Literal["ci","local"], backend: Literal["buildkit","podman"], recording: str)` — `recording` = the fixture path of the verified real output
- `ROWS: tuple[Row, ...]` — exactly the four rows of A §4.1 with recordings `tests/fixtures/reproduction/run-1/snapshot.json`, `…/run-1/local.stderr`, `…/run-5/snapshot.json`, `…/run-5/local.stderr`
- `@dataclass(frozen=True) CopyMatch(row: str, lines: tuple[int, int] | None, step_text: str | None, path: str, evidence_lines: tuple[int, ...])`
- `@dataclass(frozen=True) FromMatch(row: str, line: int | None, evidence_lines: tuple[int, ...])`
- `match_copy_ci(text: str) -> CopyMatch | None | Literal["ambiguous"]`, `match_copy_local(stderr: str) -> …`, `match_from_ci(text: str) -> FromMatch | None | Literal["ambiguous"]`, `match_from_local(stdout: str, stderr: str) -> …`

Regexes (line-anchored, after R's ANSI/timestamp stripping):
- `copy-missing/buildkit`: reuse R's `compare._error_blocks` for the single block span; the checksum line `failed to calculate checksum of ref [^:]+::[^:]+: "/(?P<p>[^"]+)": not found`; exactly one such line; the block's `>>>` line text must start with `COPY` or `ADD`.
- `copy-missing/podman`: `^Error: building at STEP "(?P<step>(?:COPY|ADD) [^"]*)": checking on sources under "[^"]*": copier: stat: "/(?P<p>[^"]+)": no such file or directory$`
- `from-args/buildkit`: `dockerfile parse error on line (?P<n>\d+): FROM requires either one or three arguments`; exactly one distinct `n`.
- `from-args/podman`: stderr line `^Error: FROM requires either one argument, or three: ` and no `STEP ` line in stdout.

- [ ] **Step 1: Tests** — `test_every_row_matches_its_real_recording` (parametrize over `ROWS`, load the recording file — for `snapshot.json` use `load_snapshot` + `shape.job_text` — assert a non-ambiguous match; for run-1 the path is `docs/setup.md`, lines `(11, 11)`); negatives with synthetic text: no match, two checksum lines → `"ambiguous"`, podman line with a different message → `None`, a parse error of another kind on line 1 → `None`, FROM local with a `STEP` line present → `None`.
- [ ] **Step 2–4.** **Step 5:** commit `feat(admission): the closed template table with recording-backed rows`.

---

### Task 9: The `admission` section model

**Files:** Create `src/deployer/admission/model.py`; Test `tests/admission/test_model.py`.

**Interfaces — Produces** (pydantic, `extra="forbid"`):
- `Binding(repo, head_sha, artifact_path, artifact_sha256)`
- `Ownership(status: Literal["confirmed","not_confirmed"], reason: str | None = None, key_fingerprint: str | None = None, record_sha256: str | None = None, snapshot_sha256: str | None = None)` — validator: `confirmed` ⇒ `reason is None` and fingerprint, both hashes present; `not_confirmed` ⇒ `reason` set and `key_fingerprint is None`
- `Defect(cls: Literal[...], file: str, lines: tuple[int, int], object: str)`
- `SideLink(row: str, object: str | None, evidence_file: str, evidence_lines: list[int])`, `DifferenceDecision(name: str, value: str, allowed: bool)`
- `Link(ci: SideLink, local: SideLink, differences: list[DifferenceDecision])`
- `Unmet(condition: Literal[1,2,3], reason: str)`
- `AdmissionSection(verdict: Literal["admitted","insufficient_grounds"], binding: Binding, ownership: Ownership, defect: Defect | None = None, link: Link | None = None, unmet: list[Unmet] = [])` — validator (A §1, §6.2): `admitted` ⇒ ownership confirmed, `defect` and `link` present, `unmet == []`; `insufficient_grounds` ⇒ `unmet` non-empty.
- `ADMISSION_VERDICT_SCHEMA_VERSION = "1.3"`

- [ ] Tests: each invariant violation raises; a valid admitted and a valid refused section construct. Commit `feat(admission): the admission section with its invariants`.

---

### Task 10: The pure decision

**Files:** Create `src/deployer/admission/decide.py`; Test `tests/admission/test_decide.py`.

**Interfaces:**
- Consumes: Tasks 7–9; R's `ReproductionSection`, `ParsedDockerfile`, `TreeListing`/`TreeRow`.
- Produces:
  - `@dataclass(frozen=True) VerifiedFacts(binding: Binding, ownership: OwnershipFacts, reproduction: ReproductionSection, parsed: ParsedDockerfile, head_listing: list[TreeRow], ci_text: str, ci_evidence_file: str, local_stdout: str, local_stderr: str, ignore_hashes: tuple[tuple[str | None, str | None], tuple[str | None, str | None]], syntax_directive_ci: str | None, syntax_directive_local: str | None)` — `ignore_hashes = ((ci_path, ci_sha256), (local_path, local_sha256))`
  - `decide(facts: VerifiedFacts) -> AdmissionSection` — no I/O

Logic, in order; collect every unmet reason reachable without inventing evidence:
1. Ownership: `not_confirmed` → unmet `(1, reason)`.
2. Defect candidates (only if the reproduction is `attempted`): `missing_copy_source` from R's `copy_sources` failed findings of the form `source <p> absent from the context`; `from_argument_count` from R's failed `syntax_from_args`. Any other failed syntax check → unmet `(2, "check <id> not admissible")`. No candidate → unmet `(2, "no admissible defect")`.
3. For `missing_copy_source`: form (A §3.1, using R's `checks._is_modelled_source`, no `--from`, `unread_reason(parsed) is None`, not remote/heredoc/JSON-escape); absence in `ownership.snapshot.tree` and in `head_listing` (equal, child `p/…`, ancestor `120000`/`160000`). For `from_argument_count`: exactly one FROM with a wrong count.
4. Link: R comparison state ∈ {`reproduced`, `reproduced_with_differences`}; `restoration.state == "exact"`; per-class rows match on both sides (Task 8) with objects bound per A §4.2; `# syntax=` on either side → unmet; differences per A §4.3 (`backend` allowed only when both rows of the class are verified — always true for the table's pairs; `ignore_file` same by path **and** hash or absent on both; `host_arch`/`base_image_digests` `unknown` allowed; anything else not allowed).
5. `admitted` iff no unmet; else `insufficient_grounds` with `defect`/`link` filled only when fully established.

- [ ] **Step 1: Tests (level D)** — a `_facts(**overrides)` builder producing an admitted `run-1` shape from the committed `run-1` bundle files (parse its `tree/Dockerfile`, read its `snapshot.json` CI text, `local.*`), with a hand-built confirmed `OwnershipFacts` whose snapshot tree lacks `docs/setup.md`. Then one test per D-row of A §8.3, each changing exactly the named input and asserting the verdict is `insufficient_grounds` with the expected `unmet` condition and a reason substring. Include the run-5 admitted shape too. Required cases (names are the test ids): `glob_source`, `from_flag`, `remote_add`, `heredoc`, `json_escape`, `unread_escape_directive`, `present_in_snapshot`, `child_in_snapshot`, `present_at_head`, `ancestor_symlink`, `ancestor_submodule`, `other_syntax_check`, `ci_no_row`, `local_no_row`, `object_not_source`, `two_object_candidates`, `two_identical_copy`, `two_bad_froms`, `other_parse_error_same_line`, `ignore_path_differs`, `ignore_content_differs`, `approximation`, `backend_pair_unverified` (patch `ROWS` to drop the podman row), `unknown_required_dimension`.
- [ ] **Step 2–4.** **Step 5:** commit `feat(admission): the pure decision over verified facts`.

---

### Task 11: Preparation and diagnose wiring

**Files:** Create `src/deployer/admission/prepare.py`; Modify `src/deployer/admission/__init__.py`, `src/deployer/diagnose.py`, `src/deployer/cli.py`; Test `tests/admission/test_prepare.py`, `tests/test_cli.py`, `tests/reproduce/test_verdict.py`.

**Interfaces:**
- `prepare(snapshot: FailedRun, section: ReproductionSection, root: Path, env: Mapping[str, str]) -> VerifiedFacts` — paths: `try_dir = root / section.try_dir`, `source_dir = try_dir.parent.parent / "source"`; reads `source.json` listing; writes `try_dir / "ci.log"` from `shape.job_text(job)` (via the same write helper as R, `TryDirError` on failure); computes effective ignore files for both backends and their SHA-256 (absent → `None`); calls `verify_ownership(..., checked_roots=(source_dir, root))`.
- `render_verdict(diagnosis, reproduction=None, admission=None)` — with `admission`, version `"1.3"` and key `"admission"`.
- CLI: after `reproduce_run`, when `section.status == "attempted"`: `admission = decide(prepare(result, section, Path.cwd(), os.environ))`; print `admission: <verdict>` and each `unmet`; pass to `render_verdict`. Exit code unchanged.

- [ ] Tests: `prepare` writes `ci.log` equal to the job text and returns ignore hashes; `render_verdict` 1.3 only with admission; CLI with stubbed `reproduce_run`/`prepare`/`decide` keeps exit 3 and prints the verdict; **end-to-end** (A §8.2): `provenance.issue` in a temp repo → copy the tree as a fake restored `source/` → `verify_ownership` → `confirmed`.
- Commit `feat(admission): preparation layer, ci.log evidence, verdict 1.3 in diagnose`.

---

### Task 12: The consumer gate

**Files:** Create `src/deployer/admission/consumer.py`; Test `tests/admission/test_consumer.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) Target(repo: str, head_sha: str, artifact_path: str, artifact_sha256: str)`
- `@dataclass(frozen=True) Accepted(section: AdmissionSection)`, `@dataclass(frozen=True) Refused(reason: str)`
- `accept_for_fix(document: Mapping[str, object], try_dir: Path, target: Target) -> Accepted | Refused` — never raises; refusals per A §7.

- [ ] Tests (A §8.5): no `admission` key (a 1.2 document — Review Focus 5); `verdict_schema_version` ≠ `"1.3"`; unknown verdict string; each invariant violation (built as raw dicts, since the model would reject them); `insufficient_grounds`; binding ≠ target (each field); a missing evidence file; an unreadable one (a directory at that path); evidence lines out of range; acceptance of a valid admitted document against its own target.
- Commit `feat(admission): accept_for_fix — refuse by default`. **Open PR A3** (stacked on A2).

---

### Task 13: Admission bundles (data, owner review)

**Files:** Create `tests/fixtures/admission/<case>/…`, `tests/fixtures/admission/make_admission_bundle.py`, `tests/fixtures/admission/test-key` + `test-key.pub` (test only, named so), `tests/fixtures/admission/trust/allowed_signers`, `tests/fixtures/admission/CHECKSUMS.sha256`, `tests/admission/test_bundle_integrity.py`.

- A case = a copy of its base reproduction bundle (`run-1` / `run-5`) whose `tree/` gains a `.deployer/authoring/` set made by `provenance.issue` semantics with the **test key**, `tree-listing.json` updated with the new blob SHAs, plus `expected.json` (`verdict`, `unmet` conditions and reason substrings) and `PROVENANCE.md` separating "real logs confirm the format" from "the signed set is test-made, no historical authorship".
- Cases (level P of A §8.3 and §8.2): `admit-run-1`, `admit-run-5`, `trust-inside`, `trust-symlink`, `no-set`, `pointer-missing-dir`, `record-format`, `snapshot-format`, `dir-not-hash`, `bad-signature`, `unknown-key`, `revoked-key`, `hand-edit`, `foreign-repo`, `foreign-path`, `snapshot-edited`, `source-commit-differs`, `tree-incomplete`, `row-missing-field`, `row-non-string`, `duplicate-path`, `dollar-var` (expects `(3) restoration not exact`), `syntax-directive`.
- `test_bundle_integrity.py`: the case set equals that list exactly; every file matches `CHECKSUMS.sha256`; every `tree/` equals its listing by blob SHA; every case has `PROVENANCE.md`.
- `pyproject.toml`/`tests/conftest.py`: exclude `tests/fixtures/admission/*/tree` from ruff and pytest collection.
- Commit, **open PR A4** (owner review; the description maps each case's `expected.json` to A §8.2/§8.3 like #81 did).

---

### Task 14: Replay, docs, ledger

**Files:** Create `tests/admission/test_acceptance.py`; Modify `README.md`, `CLAUDE.md`, `TODO.md`.

- Replay every A4 bundle through `reproduce_run` (reuse `tests/reproduce/test_acceptance.py`'s `BundleGh` and fakes), then `prepare` + `decide` with `DEPLOYER_TRUST_DIR` pointed at the bundle's trust dir (or at a directory inside the restored tree for `trust-inside`, a symlink to it for `trust-symlink`); assert `expected.json`. Assert `accept_for_fix` accepts only `admit-run-1`/`admit-run-5` against their own targets.
- README: `deployer author --signing-key`, `deployer trust …`, the `admission` section, that exit codes are unchanged, that admission never claims a cause beyond the two proven defect classes.
- CLAUDE.md: packages `provenance`, `admission`.
- TODO.md: `ci-failure-diagnosis` → Shipped with the admission summary — the checkbox is ticked and the line's tags, `@blocked_by` included, are left exactly as they are (owner decision); open follow-ups: more template rows need recordings + spec change; `ci-fix-authoring` is unblocked in principle but keeps its own spec.
- Commit, **open PR A5** (stacked on A4).

---

## Self-Review

- **Spec coverage:** A §1 → T9/T10; §2.2 → T1/T5; §2.3 → T3; §2.4 → T7; §3 → T10; §4.1 → T8; §4.2–4.3 → T10; §5 → T5/T6; §6.1 → T11; §6.2 → T9/T11; §7 → T12; §8.1 → T8; §8.2 → T11 (end-to-end), T13/T14; §8.3 D → T10, P → T13/T14; §8.4 → T5/T6; §8.5 → T12.
- **Ordering note:** A §5.2 lists exclusion (3) before build/sign (4); the plan builds the record first so the exclusion check uses the set's real paths, then signs and publishes — the order of *effects* (nothing published before exclusion is proven) is the spec's.
- **Placeholders:** Tasks 6, 10, 11, 13, 14 describe some tests in prose with exact names and assertions rather than full code where the code mirrors an existing test pattern named in the step; implementers write them out.
- **Types:** `TreeRow` (T1) is used by T4, T5, T7, T10; `OwnershipFacts` (T7) by T10; `ROWS`/`CopyMatch`/`FromMatch` (T8) by T10; `AdmissionSection` (T9) by T10–T12.
