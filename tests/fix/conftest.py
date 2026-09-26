"""Test-only seam for the closed "passed" template tables (F §10).

Production rows stay disabled until a real recording backs them (F §9). The
only way to enable a row without a recording is :func:`enable_for_test`,
which patches the module-private registry of ``deployer.fix.templates`` for
the duration of a ``with`` block. Nothing in ``src/`` can reach it: no CLI
flag, environment variable or configuration names the registry.
"""

import copy
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from deployer.admission import decide, prepare
from deployer.diagnose import diagnose_run, render_verdict
from deployer.fix import templates
from deployer.fix.gate import Admitted, gate
from deployer.provenance import trust
from tests.admission.conftest import Replayed, replay_case
from tests.admission.test_end_to_end import _issue_into_source
from tests.provenance.conftest import make_key
from tests.reproduce.conftest import FakeContainers


@contextmanager
def enable_for_test(*row_ids: str) -> Iterator[tuple[templates.Row, ...]]:
    """Inject the production rows named by ``row_ids`` (all four when none are
    named) as enabled synthetic rows, then restore the empty registry."""
    wanted = row_ids or tuple(row.id for row in templates.ROWS)
    by_id = {row.id: row for row in templates.ROWS}
    unknown = [row_id for row_id in wanted if row_id not in by_id]
    if unknown:
        raise KeyError(f"unknown template rows: {unknown}")
    rows = [by_id[row_id] for row_id in wanted]
    with patch.object(templates, "_TEST_REGISTRY", rows):
        yield tuple(rows)


# --- an admitted verdict over a real clone (shared by the gate and ``fix``) ---

ORIGIN = "git@github.com:example/project.git"


def git(repo: Path, *args: str) -> str:
    """Run ``git`` in ``repo``, raising on failure; its stdout, stripped."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    """Stage everything, commit, and return the new ``HEAD``."""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def partial_clone(source: Path, dest: Path) -> str:
    """A REAL ``--filter=blob:none`` clone of ``source`` at ``dest`` over
    ``file://`` (offline), with a blob that only a side branch holds; asserts
    the clone is partial (a promisor remote, or ``extensions.partialClone``)
    and that the blob is missing locally, and returns that blob's id."""
    git(source, "config", "uploadpack.allowFilter", "true")
    git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    head = git(source, "rev-parse", "HEAD")
    git(source, "switch", "-q", "-c", "side")
    (source / "side-only.txt").write_text("only on the side branch\n")
    commit_all(source, "side")
    blob = git(source, "rev-parse", "HEAD:side-only.txt")
    git(source, "switch", "-q", "--detach", head)
    subprocess.run(
        ["git", "clone", "-q", "--filter=blob:none", source.resolve().as_uri(), dest],
        check=True,
        capture_output=True,
        timeout=60,
    )
    config = git(dest, "config", "--list")
    assert "remote.origin.promisor=true" in config or (
        "extensions.partialclone=" in config
    ), config
    missing = git(dest, "rev-list", "--missing=print", "--objects", "--all")
    assert f"?{blob}" in missing.splitlines()
    no_fetch = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
    probe = subprocess.run(
        ["git", "-C", str(dest), "cat-file", "-e", blob],
        capture_output=True,
        timeout=60,
        env=no_fetch,
    )
    assert probe.returncode != 0  # really absent, not merely unreferenced
    return blob


def at_head(document: dict[str, Any], head: str) -> dict[str, Any]:
    """``document`` with every ``head_sha`` A compares re-pointed at ``head``."""
    moved = copy.deepcopy(document)
    moved["run"]["head_sha"] = head
    moved["admission"]["binding"]["head_sha"] = head
    moved["reproduction"]["restoration"]["sha"] = head
    return moved


def restored_at(attempt_dir: Path, head: str) -> None:
    """Re-point R's ``source.json`` (the restored head) at ``head``."""
    meta = attempt_dir / "source.json"
    data = json.loads(meta.read_text())
    data["head_sha"] = head
    meta.chmod(0o644)
    meta.write_text(json.dumps(data))


def add_tree_file(r: Replayed, rel: str, data: bytes) -> None:
    """Add a regular file to R's restored ``source/`` and to the ``head_sha``
    listing in ``source.json``, as if it had been committed at the head: a
    test-time variant of a bundle (the committed data stays untouched)."""
    source = r.unlock()
    path = source / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    blob = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=data,
            check=True,
            capture_output=True,
            timeout=60,
        )
        .stdout.decode()
        .strip()
    )
    meta = r.attempt_dir / "source.json"
    doc = json.loads(meta.read_text())
    entries = doc["listing"]["entries"]
    entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob})
    entries.sort(key=lambda row: row["path"])
    meta.chmod(0o644)
    meta.write_text(json.dumps(doc))


@dataclass
class Scenario:
    """An admitted verdict, R's tree, a clean clone at the admitted head, the
    trust dir, the signing key and the planned fix dir."""

    r: Replayed
    original: dict[str, Any]
    clone: Path
    trust: Path
    pub: str
    fix_dir: Path
    key: Path

    @property
    def env(self) -> dict[str, str]:
        """The environment naming the trust dir."""
        return {"DEPLOYER_TRUST_DIR": str(self.trust)}

    @property
    def extra_roots(self) -> tuple[Path, Path]:
        """The planned fix dir and its worktree."""
        return (self.fix_dir, self.fix_dir / "worktree")

    def document(self) -> dict[str, Any]:
        """The verdict bound to the clone's current ``HEAD``."""
        return at_head(self.original, git(self.clone, "rev-parse", "HEAD"))

    def gate(
        self, document: dict[str, Any] | None = None, clone: Path | None = None
    ) -> Admitted | str:
        """Run the gate over this scenario."""
        return gate(
            self.document() if document is None else document,
            self.r.root,
            self.clone if clone is None else clone,
            self.env,
            self.extra_roots,
        )


def admitted_scenario(
    case: str,
    tmp_path: Path,
    fake: FakeContainers,
    before_issue: Callable[[Replayed], None] | None = None,
) -> Scenario:
    """``case`` replayed (R's containers answered by ``fake``), optionally
    changed by ``before_issue``, signed with a fresh key, admitted, and cloned
    into a clean checkout whose ``origin`` is :data:`ORIGIN`."""
    r = replay_case(case, tmp_path, fake)
    if before_issue is not None:
        before_issue(r)
    keys = tmp_path / "keys"
    keys.mkdir()
    key, pub = make_key(keys)
    trust_dir = tmp_path / "trust"
    trust.add(trust_dir, pub)
    env = {"DEPLOYER_TRUST_DIR": str(trust_dir)}
    unsigned = prepare(r.run, r.section, r.root, env)
    _issue_into_source(r, unsigned.head_listing, key)
    section = decide(prepare(r.run, r.section, r.root, env))
    assert section.verdict == "admitted", section.unmet
    original = json.loads(render_verdict(diagnose_run(r.run), r.section, section))
    clone = tmp_path / "clone"
    shutil.copytree(r.source, clone)
    git(clone, "init", "-q")
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    restored_at(r.attempt_dir, commit_all(clone, "admitted tree"))
    git(clone, "remote", "add", "origin", ORIGIN)
    fix_dir = tmp_path / "fixes" / "001"
    return Scenario(r, original, clone, trust_dir, pub, fix_dir, key)
