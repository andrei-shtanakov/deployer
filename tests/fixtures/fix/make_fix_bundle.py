"""Dev tool: build the derived run-1 fix bundles (F §10 level P, §11 stage
1b) from the committed reproduction bundle ``run-1``.

Regenerate every bundle, then its checksums, from the repository root::

    uv run python tests/fixtures/fix/make_fix_bundle.py [--key PATH]

**No private key is committed.** ``--key`` is the test signing key (the one
``trust/allowed_signers`` trusts). Omitted, a fresh ed25519 key is made in a
temporary directory and discarded afterwards. The key is this tool's own —
not the admission bundles' — so the two data sets are signed independently:

- with the same key the output is byte-identical to the committed data;
- with a fresh key every ``record.json.sig``, ``test-key.pub``,
  ``trust/allowed_signers``, the listing entries of the signature files, the
  checksums and the fingerprint in ``PROVENANCE.md`` change (records,
  snapshots and set directory names do not: the record does not name the
  key). That is a data change like any other: it needs the owner's review
  again before it is committed.

The run replaces every case directory, ``trust/``, ``test-key.pub``, the key
block of the top-level ``PROVENANCE.md`` and ``CHECKSUMS.sha256`` under
``tests/fixtures/fix``. Commit the result only after
``uv run pytest tests/fix/test_fix_bundle_integrity.py`` passes and the diff
has been read.

What a case is:

1. a copy of ``run-1`` (``snapshot.json``, ``endpoint.json``, ``local.*``,
   ``tree/``), byte-identical except as below;
2. the case's added regular files (``Case.added``) written into ``tree/``
   *before* the set is issued, as if committed at ``head_sha``: the set's
   snapshot tree lists them;
3. an authoring set published into ``tree/`` by the real
   :func:`deployer.provenance.issue.issue` (which also writes the
   ``.deployer/`` exclusion into ``.dockerignore``), signed with the
   **test-only** key. The preflight is built by hand, as
   ``make_admission_bundle.py`` does: ``source_commit`` is the run's
   ``head_sha``, the snapshot's tree is ``git ls-tree -r -t --full-tree`` of
   step 2's tree, the facts are ``analyze_project`` of it;
4. ``tree-listing.json`` rewritten as ``git ls-tree -r -t --full-tree`` of
   the resulting tree (``git write-tree`` in a scratch repository); its
   ``sha`` stays the run's ``head_sha``. The same method is first checked to
   reproduce the base listing byte for byte;
5. ``expected.json``, the labelled fake model answer where the case has one,
   and ``PROVENANCE.md`` naming every file that differs from the base,
   computed.

Determinism: given the key, no clock and no randomness (ed25519 signatures
are deterministic, ``deployer_version`` is ``DEPLOYER_VERSION``, JSON is
sorted, paths are walked sorted).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deployer.facts import analyze_project
from deployer.provenance import issue, sshsig, trust
from deployer.provenance.model import TreeRow

HERE = Path(__file__).resolve().parent
BASE = HERE.parent / "reproduction" / "run-1"
SIGNING = "test-key"
KEY_COMMENT = "deployer-fix-TEST-ONLY-never-trust"
KEYS_BEGIN = "<!-- keys:begin (written by make_fix_bundle.py) -->"
KEYS_END = "<!-- keys:end -->"
SHARED_TRUST = "trust"
DEPLOYER_VERSION = "0.0.0+fix-test"
CHECKSUMS = "CHECKSUMS.sha256"
NOT_CHECKSUMMED = {CHECKSUMS, "make_fix_bundle.py"}
BASE_FILES = ("snapshot.json", "endpoint.json", "local.stdout", "local.stderr")
BASE_FILES_OPTIONAL = ("local.exit", "local.timeout")
FAKE_ANSWER = "fake-model-answer.json"
NOISE = shutil.ignore_patterns("__pycache__", ".DS_Store")
GIT = ("git", "-c", "core.excludesFile=/dev/null", "-c", "core.autocrlf=false")
ABSENT = "docs/setup.md"
UNIQUE = "docs/guide/setup.md"
ORIGINAL_COPY = f"COPY {ABSENT} ./setup.md"
# Every regular file of the unique case's head listing that survives the
# envelope's conditions 2-4, in the prompt's order (the Dockerfile itself and
# the ``.deployer/`` set, excluded by ``.dockerignore``, are not eligible;
# ``.dockerignore`` is): the stage-5 acceptance checks the prompt lists
# exactly these.
UNIQUE_ELIGIBLE = [
    ".dockerignore",
    ".github/workflows/diagnosis-polygon.yml",
    UNIQUE,
    "pyproject.toml",
    "src/ci_build/__init__.py",
    "tests/test_greeting.py",
    "uv.lock",
]


def _added(path: str) -> bytes:
    """The bytes of an added file: it says what it is."""
    return (
        f"# {path}\n\nTest file added by tests/fixtures/fix/make_fix_bundle.py; "
        "not part of the real run-1 commit.\n"
    ).encode()


@dataclass(frozen=True)
class Case:
    """One bundle: the added files, the expectation, the fake answer."""

    added: tuple[str, ...]
    change: str
    expected: dict[str, Any]
    answer: dict[str, Any] | None
    proves: str


FAKE_MODEL_ANSWER: dict[str, Any] = {
    "source": UNIQUE,
    "plausible": [UNIQUE],
    "rationale": [
        {
            "facts": [{"kind": "path", "ref": UNIQUE}],
            "explanation": (
                "FAKE model answer (test fixture, not a model output): "
                f"{UNIQUE} is the only eligible file whose basename is the "
                f"absent {ABSENT}'s"
            ),
        }
    ],
}

CASES: dict[str, Case] = {
    "copy-basename-unique": Case(
        (UNIQUE,),
        f"one regular file `tree/{UNIQUE}` added before the set was issued: the "
        "only file whose basename is the absent source's. Every other regular "
        "file of the tree stays eligible; the envelope does not choose",
        {
            "base": "run-1",
            "trust": {"kind": "dir", "path": SHARED_TRUST},
            "admission": "admitted",
            "defect": {"class": "missing_copy_source", "lines": [11, 11]},
            "envelope": "passed",
            "model_called": True,
            "prompt_lists": UNIQUE_ELIGIBLE,
            "model_answer": {"file": FAKE_ANSWER, "fake": True},
            "proposal": {
                "chosen_by": "model",
                "transformation": "copy-source",
                "original": ORIGINAL_COPY,
                "replacement": f"COPY {UNIQUE} ./setup.md",
            },
            "without_seam": {
                "status": "stopped",
                "reason": "no local confirmation",
                "detail": "templates not enabled",
            },
        },
        FAKE_MODEL_ANSWER,
        "the envelope passes, the prompt lists every eligible blob, and the "
        "proposal is the model's choice (here: the fake answer's)",
    ),
    "copy-basename-ambiguous": Case(
        ("docs/a/setup.md", "docs/b/setup.md"),
        "two regular files `tree/docs/a/setup.md` and `tree/docs/b/setup.md` "
        "added before the set was issued: two eligible blobs share the absent "
        "source's basename",
        {
            "base": "run-1",
            "trust": {"kind": "dir", "path": SHARED_TRUST},
            "admission": "admitted",
            "defect": {"class": "missing_copy_source", "lines": [11, 11]},
            "envelope": "stopped",
            "model_called": False,
            "stop": {
                "status": "stopped",
                "reason": "fix method not established",
                "detail_prefix": "5 basename floor: 2 eligible files",
            },
        },
        None,
        "the basename floor stops the envelope before the model is called",
    ),
}


# --- keys and trust -------------------------------------------------------------


def _json_bytes(data: dict[str, Any]) -> bytes:
    """Sorted, indented JSON plus a newline (reviewable, deterministic)."""
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def _private_copy(key: Path, tmp: Path) -> Path:
    """A 0600 copy of ``key`` (ssh-keygen refuses a key readable by others)."""
    private = tmp / "key"
    shutil.copyfile(key, private)
    os.chmod(private, 0o600)
    return private


def _fresh_key(directory: Path) -> Path:
    """A new ed25519 test key in ``directory``."""
    key = directory / SIGNING
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT]
        + ["-f", str(key)],
        check=True,
    )
    return key


def _write_public_key(key: Path) -> str:
    """``test-key.pub`` and ``trust/allowed_signers``; the fingerprint."""
    with tempfile.TemporaryDirectory() as tmp:
        public = sshsig.public_key(_private_copy(key, Path(tmp)))
    (HERE / f"{SIGNING}.pub").write_text(public + "\n")
    shutil.rmtree(HERE / SHARED_TRUST, ignore_errors=True)
    trust.add(HERE / SHARED_TRUST, public)
    fingerprint = sshsig.fingerprint_of(public)
    if fingerprint is None:
        raise SystemExit("the test key has no fingerprint")
    return fingerprint


def _write_key_block(fingerprint: str) -> None:
    """Replace the key block of the top-level ``PROVENANCE.md``."""
    path = HERE / "PROVENANCE.md"
    head, sep, rest = path.read_text().partition(KEYS_BEGIN)
    _, sep2, tail = rest.partition(KEYS_END)
    if not sep or not sep2:
        raise SystemExit("PROVENANCE.md lacks the key block markers")
    line = f"- `{SIGNING}.pub`: `{fingerprint}` ({KEY_COMMENT})\n"
    path.write_text(f"{head}{KEYS_BEGIN}\n{line}{KEYS_END}{tail}")


# --- listing, the set, files -------------------------------------------------------


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


def _issue(tree: Path, repo: str, head_sha: str, key: Path) -> None:
    """Publish the set with the real ``issue`` over ``tree``'s own listing;
    the tree is a Git directory only for the call (``issue`` locks under
    ``.git``)."""
    rows = [TreeRow(**row) for row in git_listing(tree)]
    _git(tree, "init", "-q")
    with tempfile.TemporaryDirectory() as tmp:
        pre = issue.Preflight(
            project=tree,
            repo=repo,
            source_commit=head_sha,
            tree=rows,
            facts=analyze_project(tree),
        )
        dockerfile = (tree / "Dockerfile").read_bytes()
        private = _private_copy(key, Path(tmp))
        out = issue.issue(pre, private, DEPLOYER_VERSION, dockerfile)
    shutil.rmtree(tree / ".git")
    if not out.published:
        raise SystemExit(f"issue refused: {out.reason}")


def _files(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root``, by relative POSIX path."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
    }


def _changes(bundle: Path) -> list[str]:
    """Every input file of ``bundle`` that differs from the base (the case's
    own ``expected.json``, ``PROVENANCE.md`` and fake answer aside)."""
    skip = {"expected.json", "PROVENANCE.md", FAKE_ANSWER}
    old = {k: v for k, v in _files(BASE).items() if k not in skip}
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


def build_case(name: str, case: Case, key: Path) -> None:
    """Build ``HERE / name`` from scratch."""
    bundle = HERE / name
    bundle.mkdir()
    for file in (*BASE_FILES, *BASE_FILES_OPTIONAL):
        if (BASE / file).is_file():
            shutil.copyfile(BASE / file, bundle / file)
    snapshot = json.loads((BASE / "snapshot.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(BASE / "tree", tree, symlinks=True, ignore=NOISE)
        for rel in case.added:
            if (tree / rel).exists():
                raise SystemExit(f"{name}: {rel} already exists in the base")
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes(_added(rel))
        _issue(tree, snapshot["repo"], snapshot["head_sha"], key)
        shutil.copytree(tree, bundle / "tree", symlinks=True)
    listing = {
        "sha": snapshot["head_sha"],
        "truncated": False,
        "tree": git_listing(bundle / "tree"),
    }
    (bundle / "tree-listing.json").write_text(json.dumps(listing, indent=2) + "\n")
    (bundle / "expected.json").write_bytes(_json_bytes(case.expected))
    if case.answer is not None:
        (bundle / FAKE_ANSWER).write_bytes(_json_bytes(case.answer))
    (bundle / "PROVENANCE.md").write_text(_provenance(name, case, bundle))


def _provenance(name: str, case: Case, bundle: Path) -> str:
    """The case's ``PROVENANCE.md``: the real record and the test change,
    kept apart, and every file that differs from the base."""
    changes = "\n".join(_changes(bundle))
    answer = (
        f"\n- `{FAKE_ANSWER}` is a **FAKE model answer**, written by hand into "
        "the generator for the acceptance test's fake chooser. No model "
        "produced it; it shows the shape of an answer, not what a model "
        "would choose."
        if case.answer is not None
        else ""
    )
    return f"""# Provenance: `{name}` (fix case, F §10 level P, §11 stage 1b)

Proves: {case.proves}.

Base: reproduction bundle `run-1` — see `../../reproduction/run-1/PROVENANCE.md`.
Built by `../make_fix_bundle.py` (deterministic given the key; see its
docstring).

## The original failure record (real, unchanged)

`snapshot.json` (the CI job text of run 35680991093), `local.stdout`,
`local.stderr`, `local.exit` and `endpoint.json` are `run-1`'s bytes, byte for
byte: a real CI run and a real Podman 5.7.0 build. They record the build
failing on `{ORIGINAL_COPY}` (line 11). Neither the real run nor the
real build saw the files added below.

## The test modification (made for this test, claims no history)

- {case.change}.
- A `.deployer/authoring/` set, and the `.dockerignore` line `.deployer/` it
  requires, issued by `deployer.provenance.issue.issue` with the test-only key
  whose public half is `../test-key.pub` (private key not committed),
  `deployer_version` `{DEPLOYER_VERSION}`, `source_commit` = the run's
  `head_sha`, the snapshot tree = the modified tree's listing. No real
  deployer run authored this Dockerfile.{answer}
- `expected.json` records what `deployer fix` must do with this bundle.

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
        if path not in NOT_CHECKSUMMED
    ]
    (HERE / CHECKSUMS).write_text("\n".join(lines) + "\n")


def build_all(key: Path) -> None:
    """Rebuild everything signed with ``key``."""
    base_listing = json.loads((BASE / "tree-listing.json").read_text())
    if git_listing(BASE / "tree") != base_listing["tree"]:
        raise SystemExit("run-1: git listing does not reproduce the base")
    _write_key_block(_write_public_key(key))
    for stale in HERE.iterdir():
        if stale.is_dir() and (stale / "expected.json").is_file():
            shutil.rmtree(stale)
    for name, case in CASES.items():
        build_case(name, case, key)
    write_checksums()


def main() -> None:
    """Rebuild every case, the public key, the trust store and the
    checksums, with the given key or a fresh one."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--key", type=Path, help="the trusted test signing key")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        key = args.key if args.key is not None else _fresh_key(Path(tmp))
        build_all(key)


if __name__ == "__main__":
    main()
