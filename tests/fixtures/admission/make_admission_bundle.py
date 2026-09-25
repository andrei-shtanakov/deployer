"""Dev tool: build the admission bundles (A §8.2, §8.3 level P) from the
committed reproduction bundles ``run-1`` / ``run-5``.

Regenerate every bundle, then its checksums, from the repository root::

    uv run python tests/fixtures/admission/make_admission_bundle.py \
        [--key PATH] [--unknown-key PATH]

**No private key is committed.** ``--key`` is the test signing key (the one
``trust/allowed_signers`` trusts), ``--unknown-key`` the key no trust store
lists (``unknown-key``'s signature). Either one omitted is replaced by a
fresh ed25519 key made in a temporary directory and discarded afterwards.
The original private keys were kept by their author, not committed:

- with the same two keys the output is byte-identical to the committed data;
- with fresh keys every ``record.json.sig``, both ``*.pub``, the trust
  files, the in-tree trust copy, their listing entries, the checksums and
  the fingerprints recorded in ``PROVENANCE.md`` change (records, snapshots
  and set directory names do not: the record does not name the key). That
  is a data change like any other: it needs the owner's review again
  before it is committed.

The run replaces every case directory, ``trust/``, ``test-key.pub``,
``unknown-test-key.pub``, the key block of the top-level ``PROVENANCE.md``
and ``CHECKSUMS.sha256`` under ``tests/fixtures/admission``. Commit the
result only after ``uv run pytest tests/admission/test_bundle_integrity.py``
passes and the diff has been read.

What a case is:

1. a copy of its base reproduction bundle (``snapshot.json``,
   ``endpoint.json``, ``local.*``, ``tree/``), untouched except as below;
2. for ``dollar-var`` / ``syntax-directive`` only, one edit of
   ``tree/Dockerfile`` *before* the set is issued, so the set covers it;
3. an authoring set published into ``tree/`` by the real
   :func:`deployer.provenance.issue.issue` (which also writes the
   ``.deployer/`` exclusion into ``.dockerignore``), signed with the
   **test-only** signing key (``--key``; its public half is ``test-key.pub``). The preflight is built by hand,
   as ``tests/admission/test_end_to_end.py`` does: the real one needs a
   checkout whose ``HEAD`` is the run's ``head_sha``, which a bundle cannot
   give. ``source_commit`` is the run's ``head_sha``, the snapshot's tree is
   the base ``tree-listing.json``, the facts are ``analyze_project`` of the
   base tree;
4. exactly one mutation (``MUTATIONS``), mirroring
   ``tests/admission/conftest.py``: of the set, of the Dockerfile, of the
   trust store, or an in-tree trust copy;
5. ``tree-listing.json`` rewritten as ``git ls-tree -r -t --full-tree`` of
   the resulting tree (``git write-tree`` in a scratch repository); its
   ``sha`` stays the run's ``head_sha``. The same method is first checked to
   reproduce the base listing byte for byte;
6. ``expected.json`` and ``PROVENANCE.md`` from ``CASES`` below, the latter
   naming every file that differs from the base, computed.

Determinism: given the keys, no clock and no randomness. Ed25519
signatures are deterministic, ``deployer_version`` is the constant
``DEPLOYER_VERSION``, JSON is written sorted and compact (as
``canonical_bytes``), and paths are walked sorted. Running the tool twice
with the same keys gives byte-identical bundles.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deployer.facts import analyze_project
from deployer.provenance import issue, sshsig, trust
from deployer.provenance.model import (
    POINTER,
    RECORD_FILE,
    SET_PARENT,
    SET_ROOT,
    SIGNATURE_FILE,
    SNAPSHOT_FILE,
    TreeRow,
    set_dir_name,
    sha256_hex,
)

HERE = Path(__file__).resolve().parent
REPRODUCTION = HERE.parent / "reproduction"
SIGNING = "test-key"
UNKNOWN = "unknown-test-key"
KEY_COMMENTS = {
    SIGNING: "deployer-admission-TEST-ONLY-never-trust",
    UNKNOWN: "deployer-admission-TEST-ONLY-unknown-signer",
}
KEYS: dict[str, Path] = {}
"""The private keys of this run, by role (``SIGNING``/``UNKNOWN``); set by
:func:`main`, never inside this directory."""
KEYS_BEGIN = "<!-- keys:begin (written by make_admission_bundle.py) -->"
KEYS_END = "<!-- keys:end -->"
SHARED_TRUST = "trust"
IN_TREE_TRUST = "ci-trust"
DEPLOYER_VERSION = "0.0.0+admission-test"
CHECKSUMS = "CHECKSUMS.sha256"
NOT_CHECKSUMMED = {CHECKSUMS, "make_admission_bundle.py"}
BASE_FILES = ("snapshot.json", "endpoint.json", "local.stdout", "local.stderr")
BASE_FILES_OPTIONAL = ("local.exit", "local.timeout")
NOISE = shutil.ignore_patterns("__pycache__", ".DS_Store")
GIT = ("git", "-c", "core.excludesFile=/dev/null", "-c", "core.autocrlf=false")


# --- the cases ---------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One bundle: base, mutation, trust placement, expectation, prose."""

    base: str
    mutation: str
    change: str
    expected: dict[str, Any]
    trust: dict[str, str] = field(
        default_factory=lambda: {"kind": "dir", "path": SHARED_TRUST}
    )
    spec: str = ""


def _owner(step: int, *reason: str) -> list[dict[str, Any]]:
    """A (1) unmet entry naming the step and reason substrings."""
    return [
        {
            "condition": 1,
            "reason_contains": [f"ownership not confirmed: step {step}: ", *reason],
        }
    ]


def _refused(step: int, *reason: str) -> dict[str, Any]:
    """The expectation of a case refused at ownership ``step`` and nowhere
    else: without a verified snapshot the absence proof against it is not
    run, so condition (2) adds no reason of its own (``decide``)."""
    return {
        "verdict": "insufficient_grounds",
        "ownership": {"status": "not_confirmed", "step": step},
        "unmet": _owner(step, *reason),
        "accepted": False,
    }


def _admitted(cls: str, lines: list[int]) -> dict[str, Any]:
    """The expectation of a positive case."""
    return {
        "verdict": "admitted",
        "ownership": {"status": "confirmed", "step": None},
        "unmet": [],
        "defect": {"class": cls, "file": "Dockerfile", "lines": lines},
        "accepted": True,
    }


CASES: dict[str, Case] = {
    "admit-run-1": Case(
        "run-1",
        "none",
        "none beyond the issued set",
        _admitted("missing_copy_source", [11, 11]),
        spec="§8.2 positive, `missing_copy_source`",
    ),
    "admit-run-5": Case(
        "run-5",
        "none",
        "none beyond the issued set",
        _admitted("from_argument_count", [1, 1]),
        spec="§8.2 positive, `from_argument_count`",
    ),
    "trust-inside": Case(
        "run-1",
        "in_tree_trust",
        f"`tree/{IN_TREE_TRUST}/allowed_signers` added, a byte copy of the "
        "shared `trust/allowed_signers`; the trust dir is pointed at it inside "
        "the restored tree",
        _refused(0, "lies inside"),
        trust={"kind": "inside_tree", "path": IN_TREE_TRUST},
        spec="§8.3 (1) step 0, trust dir inside the repo",
    ),
    "trust-symlink": Case(
        "run-1",
        "in_tree_trust",
        f"`tree/{IN_TREE_TRUST}/allowed_signers` added as in `trust-inside`; "
        "the trust dir is a symlink outside the tree that resolves to it",
        _refused(0, "lies inside"),
        trust={"kind": "symlink_into_tree", "path": IN_TREE_TRUST},
        spec="§8.3 (1) step 0, via a symlink",
    ),
    "no-set": Case(
        "run-1",
        "remove_pointer",
        f"`{SET_ROOT}/{POINTER}` removed (the set directory stays: without the "
        "pointer no set is published)",
        _refused(1, f"{POINTER} is missing"),
        spec="§8.3 (1) step 1, no set",
    ),
    "pointer-missing-dir": Case(
        "run-1",
        "pointer_to_missing_dir",
        f"`{POINTER}` names `{SET_PARENT}/{'0' * 64}`, which does not exist",
        _refused(1, f"{SET_PARENT}/{'0' * 64} is missing"),
        spec="§8.3 (1) step 1, pointer names a missing directory",
    ),
    "record-format": Case(
        "run-1",
        "record_unknown_format",
        '`record.json` rewritten in place with `format_version` `"2"` (not '
        "re-signed, directory not renamed)",
        _refused(1, "record.json format_version '2' is not supported"),
        spec="§8.3 (1) step 1, unknown `format_version` in the record",
    ),
    "snapshot-format": Case(
        "run-1",
        "snapshot_unknown_format",
        '`snapshot.json` rewritten in place with `format_version` `"2"` '
        "(record unchanged)",
        _refused(1, "snapshot.json format_version '2' is not supported"),
        spec="§8.3 (1) step 1, unknown `format_version` in the snapshot",
    ),
    "dir-not-hash": Case(
        "run-1",
        "rename_dir",
        f"the set directory renamed to `{SET_PARENT}/{'f' * 64}` and the "
        "pointer repointed to it (half-replaced)",
        _refused(2, "is not the record's hash"),
        spec="§8.3 (1) step 2",
    ),
    "bad-signature": Case(
        "run-1",
        "bad_signature",
        "`record.json.sig` replaced by a well-formed signature of the test key "
        "over other bytes (`other bytes\\n`)",
        _refused(3, "signature not verified"),
        spec="§8.3 (1) step 3, well-formed but invalid signature",
    ),
    "unknown-key": Case(
        "run-1",
        "unknown_key",
        "`record.json.sig` re-made with `unknown-test-key`, which no trust "
        "store lists (record and directory unchanged)",
        _refused(3, "signature not verified"),
        spec="§8.3 (1) step 3, unknown key",
    ),
    "revoked-key": Case(
        "run-1",
        "none",
        "the set is unchanged; the case's own `trust/` keeps the test key in "
        "`allowed_signers` and lists it in `revoked_keys`, so only `-r` refuses "
        "it",
        _refused(3, "signature not verified"),
        trust={"kind": "dir", "path": "revoked-key/trust"},
        spec="§8.3 (1) step 3, revoked key",
    ),
    "hand-edit": Case(
        "run-1",
        "hand_edit",
        "`tree/Dockerfile` gains a last line `# edited by hand after authoring` "
        "after the set was issued (set unchanged)",
        _refused(4, "artifact_sha256", "differs from Dockerfile at head"),
        spec="§8.3 (1) step 4, Dockerfile hand-edited after authoring",
    ),
    "foreign-repo": Case(
        "run-1",
        "foreign_repo",
        "record re-signed with `repo` `other/project` (new set directory)",
        _refused(4, "record repo 'other/project' is not the run's"),
        spec="§8.3 (1) step 4, foreign `repo`",
    ),
    "foreign-path": Case(
        "run-1",
        "foreign_path",
        "record re-signed with `artifact_path` `docker/Dockerfile` (new set directory)",
        _refused(4, "record artifact_path 'docker/Dockerfile' is not the run's"),
        spec="§8.3 (1) step 4, foreign `artifact_path` (re-signed)",
    ),
    "snapshot-edited": Case(
        "run-1",
        "snapshot_edited",
        "`snapshot.json` re-serialised with two-space indentation, still "
        "parseable; record and signature unchanged",
        _refused(5, "snapshot_sha256", "differs from snapshot.json"),
        spec="§8.3 (1) step 5",
    ),
    "source-commit-differs": Case(
        "run-1",
        "source_commit_differs",
        f"snapshot `source_commit` set to `{'0' * 40}`, its hash recomputed "
        "into the record, re-signed (new set directory)",
        _refused(6, "source_commit differs"),
        spec="§8.3 (1) step 6, `source_commit` differs",
    ),
    "tree-incomplete": Case(
        "run-1",
        "tree_incomplete",
        "snapshot `tree_complete` set to `false`, re-signed (new set directory)",
        _refused(6, "tree listing is not complete"),
        spec="§8.3 (1) step 6, `tree_complete: false`",
    ),
    "row-missing-field": Case(
        "run-1",
        "row_missing_field",
        "the snapshot's first tree row loses `mode`, re-signed (new set directory)",
        _refused(6, "is not a well-formed snapshot", "tree.0.mode"),
        spec="§8.3 (1) step 6, an entry missing a field",
    ),
    "row-non-string": Case(
        "run-1",
        "row_non_string",
        "the snapshot's first tree row gets `mode` `100644` as a number, "
        "re-signed (new set directory)",
        _refused(6, "is not a well-formed snapshot", "tree.0.mode"),
        spec="§8.3 (1) step 6, a non-string field",
    ),
    "duplicate-path": Case(
        "run-1",
        "duplicate_path",
        "the snapshot's first tree row appended again at the end, re-signed "
        "(new set directory)",
        _refused(6, "duplicate paths"),
        spec="§8.3 (1) step 6, a duplicate path",
    ),
    "dollar-var": Case(
        "run-1",
        "dollar_var",
        "`tree/Dockerfile` line 11 `COPY docs/setup.md ./setup.md` → "
        "`COPY $VAR ./setup.md`, made *before* the set was issued (the set "
        "covers the edited bytes). The recorded CI and local logs are the "
        "base's and still print `COPY docs/setup.md ./setup.md`: this case "
        "documents R's early refusal (restoration approximation), not a "
        "recorded `$VAR` build",
        {
            "verdict": "insufficient_grounds",
            "ownership": {"status": "confirmed", "step": None},
            "unmet": [
                {"condition": 2, "reason_contains": ["no admissible defect"]},
                {
                    "condition": 3,
                    "reason_contains": ["restoration approximation, not exact"],
                },
            ],
            "accepted": False,
        },
        spec="§8.3 (2) form, `COPY $VAR` (P: `(3) restoration not exact`)",
    ),
    "syntax-directive": Case(
        "run-1",
        "syntax_directive",
        "`tree/Dockerfile` starts with `# syntax=docker/dockerfile:1` in place "
        "of the blank line after `FROM` (lines 3 on keep their numbers), made "
        "*before* the set was issued. The recorded logs are the base's: no "
        "build with the directive was run; the refusal is the directive "
        "itself (A §4.3)",
        {
            "verdict": "insufficient_grounds",
            "ownership": {"status": "confirmed", "step": None},
            "unmet": [
                {
                    "condition": 3,
                    "reason_contains": [
                        "unknown dialect: # syntax=docker/dockerfile:1 (local)",
                        "unknown dialect: # syntax=docker/dockerfile:1 (Dockerfile)",
                    ],
                },
            ],
            "accepted": False,
        },
        spec="§8.3 (3) differences, `# syntax=` on the `run-1` COPY case",
    ),
}


# --- signing and the set -------------------------------------------------------


def _json_bytes(data: dict[str, Any]) -> bytes:
    """Sorted, compact JSON plus a newline: ``canonical_bytes``'s shape."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode() + b"\n"


def _private_copy(role: str, tmp: Path) -> Path:
    """A 0600 copy of the ``role`` key (ssh-keygen refuses a private key
    readable by others)."""
    private = tmp / "key"
    shutil.copyfile(KEYS[role], private)
    os.chmod(private, 0o600)
    return private


def _sign(data: bytes, role: str) -> bytes:
    """Sign ``data`` with the ``role`` key."""
    with tempfile.TemporaryDirectory() as tmp:
        return sshsig.sign(data, _private_copy(role, Path(tmp)))


@dataclass
class TreeSet:
    """The issued set inside a scratch tree, for the mutations."""

    tree: Path

    @property
    def auth(self) -> Path:
        """``.deployer/authoring`` in the tree."""
        return self.tree / SET_ROOT

    @property
    def set_dir(self) -> Path:
        """The directory the pointer names."""
        return self.auth / (self.auth / POINTER).read_text().strip()

    def load(self, name: str) -> dict[str, Any]:
        """A set file of the current set, as JSON."""
        return json.loads((self.set_dir / name).read_bytes())

    def rewrite(self, name: str, data: dict[str, Any]) -> None:
        """Overwrite a set file in place: no re-signing, no rename."""
        (self.set_dir / name).write_bytes(_json_bytes(data))

    def resign(
        self,
        record: dict[str, Any],
        snapshot: dict[str, Any] | None = None,
        key: str = SIGNING,
    ) -> None:
        """Publish ``record`` (with ``snapshot`` rehashed into it) as a new
        signed set under its own hash and repoint to it."""
        old = self.set_dir
        snap = (
            (old / SNAPSHOT_FILE).read_bytes()
            if snapshot is None
            else _json_bytes(snapshot)
        )
        rec = _json_bytes({**record, "snapshot_sha256": sha256_hex(snap)})
        rec_sha = sha256_hex(rec)
        new = self.auth / set_dir_name(rec_sha)
        new.mkdir(exist_ok=True)
        (new / RECORD_FILE).write_bytes(rec)
        (new / SNAPSHOT_FILE).write_bytes(snap)
        (new / SIGNATURE_FILE).write_bytes(_sign(rec, key))
        if old != new:
            shutil.rmtree(old)
        (self.auth / POINTER).write_text(set_dir_name(rec_sha) + "\n")

    def resign_snapshot(self, change: Callable[[dict[str, Any]], None]) -> None:
        """Change the snapshot, rehash it into the record, re-sign."""
        snapshot = self.load(SNAPSHOT_FILE)
        change(snapshot)
        self.resign(self.load(RECORD_FILE), snapshot)


def _issue(tree: Path, repo: str, head_sha: str, listing: list[TreeRow]) -> None:
    """Publish the set with the real ``issue``; the tree is a Git directory
    only for the call (``issue`` takes its lock under ``.git``)."""
    _git(tree, "init", "-q")
    with tempfile.TemporaryDirectory() as tmp:
        private = _private_copy(SIGNING, Path(tmp))
        pre = issue.Preflight(
            project=tree,
            repo=repo,
            source_commit=head_sha,
            tree=listing,
            facts=analyze_project(tree),
        )
        dockerfile = (tree / "Dockerfile").read_bytes()
        out = issue.issue(pre, private, DEPLOYER_VERSION, dockerfile)
    shutil.rmtree(tree / ".git")
    if not out.published:
        raise SystemExit(f"issue refused: {out.reason}")


# --- mutations -------------------------------------------------------------------


def _edit_dockerfile(tree: Path, old: str, new: str) -> None:
    """Replace ``old`` (which must occur once) in ``tree/Dockerfile``."""
    path = tree / "Dockerfile"
    text = path.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"Dockerfile edit: {old!r} not found exactly once")
    path.write_text(text.replace(old, new))


PRE_ISSUE: dict[str, Callable[[Path], None]] = {
    "dollar_var": lambda tree: _edit_dockerfile(
        tree, "COPY docs/setup.md ./setup.md\n", "COPY $VAR ./setup.md\n"
    ),
    "syntax_directive": lambda tree: _edit_dockerfile(
        tree,
        "FROM python:3.12-slim\n\n",
        "# syntax=docker/dockerfile:1\nFROM python:3.12-slim\n",
    ),
}


def _in_tree_trust(s: TreeSet) -> None:
    target = s.tree / IN_TREE_TRUST
    target.mkdir()
    shutil.copyfile(
        HERE / SHARED_TRUST / trust.ALLOWED_FILE, target / "allowed_signers"
    )


def _pointer_to_missing_dir(s: TreeSet) -> None:
    (s.auth / POINTER).write_text(f"{SET_PARENT}/{'0' * 64}\n")


def _rename_dir(s: TreeSet) -> None:
    other = set_dir_name("f" * 64)
    s.set_dir.rename(s.auth / other)
    (s.auth / POINTER).write_text(other + "\n")


def _snapshot_edited(s: TreeSet) -> None:
    snapshot = s.load(SNAPSHOT_FILE)
    (s.set_dir / SNAPSHOT_FILE).write_text(json.dumps(snapshot, indent=2) + "\n")


MUTATIONS: dict[str, Callable[[TreeSet], None]] = {
    "none": lambda s: None,
    "dollar_var": lambda s: None,
    "syntax_directive": lambda s: None,
    "in_tree_trust": _in_tree_trust,
    "remove_pointer": lambda s: (s.auth / POINTER).unlink(),
    "pointer_to_missing_dir": _pointer_to_missing_dir,
    "record_unknown_format": lambda s: s.rewrite(
        RECORD_FILE, {**s.load(RECORD_FILE), "format_version": "2"}
    ),
    "snapshot_unknown_format": lambda s: s.rewrite(
        SNAPSHOT_FILE, {**s.load(SNAPSHOT_FILE), "format_version": "2"}
    ),
    "rename_dir": _rename_dir,
    "bad_signature": lambda s: (s.set_dir / SIGNATURE_FILE).write_bytes(
        _sign(b"other bytes\n", SIGNING)
    ),
    "unknown_key": lambda s: s.resign(s.load(RECORD_FILE), key=UNKNOWN),
    "hand_edit": lambda s: (s.tree / "Dockerfile").write_text(
        (s.tree / "Dockerfile").read_text() + "# edited by hand after authoring\n"
    ),
    "foreign_repo": lambda s: s.resign(
        {**s.load(RECORD_FILE), "repo": "other/project"}
    ),
    "foreign_path": lambda s: s.resign(
        {**s.load(RECORD_FILE), "artifact_path": "docker/Dockerfile"}
    ),
    "snapshot_edited": _snapshot_edited,
    "source_commit_differs": lambda s: s.resign_snapshot(
        lambda snap: snap.update(source_commit="0" * 40)
    ),
    "tree_incomplete": lambda s: s.resign_snapshot(
        lambda snap: snap.update(tree_complete=False)
    ),
    "row_missing_field": lambda s: s.resign_snapshot(
        lambda snap: snap["tree"][0].pop("mode")
    ),
    "row_non_string": lambda s: s.resign_snapshot(
        lambda snap: snap["tree"][0].update(mode=100644)
    ),
    "duplicate_path": lambda s: s.resign_snapshot(
        lambda snap: snap["tree"].append(dict(snap["tree"][0]))
    ),
}


# --- listing, trust, files ---------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd``; its stdout."""
    proc = subprocess.run(
        [*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return proc.stdout


def git_listing(tree: Path) -> list[dict[str, str]]:
    """``git ls-tree -r -t --full-tree`` of ``tree``'s content, via
    ``git write-tree`` in a scratch repository (``tree`` is not modified)."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        shutil.copytree(tree, work, symlinks=True)
        _git(work, "init", "-q")
        _git(work, "add", "-A", "-f", ".")
        tree_sha = _git(work, "write-tree").strip()
        rows = []
        for line in _git(
            work, "ls-tree", "-r", "-t", "--full-tree", tree_sha
        ).splitlines():
            meta, path = line.split("\t", 1)
            mode, kind, sha = meta.split()
            rows.append({"path": path, "mode": mode, "type": kind, "sha": sha})
        return rows


def _write_listing(bundle: Path, head_sha: str) -> None:
    listing = {
        "sha": head_sha,
        "truncated": False,
        "tree": git_listing(bundle / "tree"),
    }
    (bundle / "tree-listing.json").write_text(json.dumps(listing, indent=2) + "\n")


def _write_public_keys() -> dict[str, str]:
    """``<role>.pub`` from the keys in use; each key's fingerprint."""
    fingerprints = {}
    for role in (SIGNING, UNKNOWN):
        with tempfile.TemporaryDirectory() as tmp:
            public = sshsig.public_key(_private_copy(role, Path(tmp)))
        (HERE / f"{role}.pub").write_text(public + "\n")
        fingerprint = sshsig.fingerprint_of(public)
        if fingerprint is None:
            raise SystemExit(f"{role}: no fingerprint")
        fingerprints[role] = fingerprint
    return fingerprints


def _write_key_block(fingerprints: dict[str, str]) -> None:
    """Replace the key block of the top-level ``PROVENANCE.md``."""
    path = HERE / "PROVENANCE.md"
    text = path.read_text()
    head, sep, rest = text.partition(KEYS_BEGIN)
    _, sep2, tail = rest.partition(KEYS_END)
    if not sep or not sep2:
        raise SystemExit("PROVENANCE.md lacks the key block markers")
    lines = "".join(
        f"- `{role}.pub`: `{fp}` ({KEY_COMMENTS[role]})\n"
        for role, fp in fingerprints.items()
    )
    path.write_text(f"{head}{KEYS_BEGIN}\n{lines}{KEYS_END}{tail}")


def _fresh_key(role: str, directory: Path) -> Path:
    """A new ed25519 test key for ``role`` in ``directory``."""
    key = directory / role
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENTS[role]]
        + ["-f", str(key)],
        check=True,
    )
    return key


def _write_trusts() -> None:
    """The shared trust store, and ``revoked-key``'s own."""
    public = (HERE / f"{SIGNING}.pub").read_text().strip()
    shared = HERE / SHARED_TRUST
    shutil.rmtree(shared, ignore_errors=True)
    trust.add(shared, public)
    revoked = HERE / "revoked-key" / "trust"
    trust.add(revoked, public)
    body = " ".join(public.split()[:2])
    (revoked / trust.REVOKED_FILE).write_text(f"{body}\n")


def _files(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root``, by relative POSIX path."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _changes(case: str, base: Path, bundle: Path) -> list[str]:
    """Every input file of ``bundle`` that differs from ``base`` (the
    case's own ``expected.json``/``PROVENANCE.md`` aside)."""
    skip = {"expected.json", "PROVENANCE.md"}
    old = {k: v for k, v in _files(base).items() if k not in skip}
    new = {k: v for k, v in _files(bundle).items() if k not in skip}
    lines = []
    for path in sorted(set(old) | set(new)):
        if path not in new:
            lines.append(f"- `{path}`: removed")
        elif path not in old:
            lines.append(f"- `{path}`: added")
        elif old[path] != new[path]:
            lines.append(f"- `{path}`: changed")
    return lines


# --- building ------------------------------------------------------------------------


def build_case(name: str, case: Case) -> None:
    """Build ``HERE / name`` from scratch."""
    base = REPRODUCTION / case.base
    bundle = HERE / name
    for child in (bundle / "tree", bundle / "tree-listing.json"):
        if child.is_dir():
            shutil.rmtree(child)
        elif child.exists():
            child.unlink()
    bundle.mkdir(exist_ok=True)
    for file in (*BASE_FILES, *BASE_FILES_OPTIONAL):
        if (base / file).is_file():
            shutil.copyfile(base / file, bundle / file)
    snapshot = json.loads((base / "snapshot.json").read_text())
    base_listing = json.loads((base / "tree-listing.json").read_text())
    if git_listing(base / "tree") != base_listing["tree"]:
        raise SystemExit(f"{case.base}: git listing does not reproduce the base")
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(base / "tree", tree, symlinks=True, ignore=NOISE)
        if case.mutation in PRE_ISSUE:
            PRE_ISSUE[case.mutation](tree)
        rows = [TreeRow(**row) for row in base_listing["tree"]]
        _issue(tree, snapshot["repo"], snapshot["head_sha"], rows)
        MUTATIONS[case.mutation](TreeSet(tree))
        shutil.copytree(tree, bundle / "tree", symlinks=True)
    _write_listing(bundle, snapshot["head_sha"])
    expected = {"base": case.base, "trust": case.trust, **case.expected}
    (bundle / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    (bundle / "PROVENANCE.md").write_text(_provenance(name, case, base, bundle))


def _provenance(name: str, case: Case, base: Path, bundle: Path) -> str:
    changes = "\n".join(_changes(name, base, bundle))
    return f"""# Provenance: `{name}` (admission case, A {case.spec})

Base: reproduction bundle `{case.base}` — see
`../../reproduction/{case.base}/PROVENANCE.md` for how every unchanged file was
obtained. Built by `../make_admission_bundle.py` (deterministic; see its
docstring).

## Two sources, kept apart

- **Real logs confirm the format.** `snapshot.json` (the CI job text),
  `local.stdout`, `local.stderr`, `local.exit` and `endpoint.json` are the
  base bundle's, byte for byte: a real CI run and a real Podman 5.7.0 build.
  They confirm what the builders print, nothing about authorship.
- **The signed set is test-made; it claims no historical authorship.** The
  `.deployer/authoring/` set (and the `.dockerignore` exclusion it needs)
  did not exist in the real run. It was issued for this test by
  `deployer.provenance.issue.issue` with the test-only key whose public
  half is `../test-key.pub` (private key not committed),
  `deployer_version` `{DEPLOYER_VERSION}`, `source_commit` = the run's
  `head_sha`, the snapshot tree = the base `tree-listing.json`. No real
  deployer run authored this Dockerfile.

## The mutation

{case.change}.

Trust store for the replay: `{json.dumps(case.trust)}` (see `expected.json`).

## Every file that differs from the base

{changes}

`tree-listing.json` is `git ls-tree -r -t --full-tree` of this `tree/`; its
`sha` stays the run's `head_sha`. `expected.json` and this file are new.
"""


def write_checksums() -> None:
    """``CHECKSUMS.sha256`` over every file but itself and this tool."""
    lines = [
        f"{hashlib.sha256(data).hexdigest()}  {path}"
        for path, data in sorted(_files(HERE).items())
        if path not in NOT_CHECKSUMMED and "__pycache__" not in path
    ]
    (HERE / CHECKSUMS).write_text("\n".join(lines) + "\n")


def main() -> None:
    """Rebuild every case, the public keys, the trust stores and the
    checksums, with the given keys or fresh ones."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", type=Path, help="the trusted test signing key")
    parser.add_argument("--unknown-key", type=Path, help="the untrusted test key")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        for role, given in ((SIGNING, args.key), (UNKNOWN, args.unknown_key)):
            KEYS[role] = given if given is not None else _fresh_key(role, Path(tmp))
        build_all()


def build_all() -> None:
    """Rebuild everything with the keys in ``KEYS``."""
    _write_key_block(_write_public_keys())
    for stale in HERE.iterdir():
        if stale.is_dir() and stale.name not in CASES and stale.name != SHARED_TRUST:
            if (stale / "expected.json").is_file():
                shutil.rmtree(stale)
    for name in CASES:
        shutil.rmtree(HERE / name, ignore_errors=True)
    _write_trusts()
    for name, case in CASES.items():
        build_case(name, case)
    write_checksums()


if __name__ == "__main__":
    main()
