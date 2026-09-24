"""The trust store: allowed_signers/revoked_keys directory outside the repo."""

import os
from pathlib import Path

from deployer.provenance import trust


def test_trust_dir_default_and_override(tmp_path: Path) -> None:
    assert trust.trust_dir({}) == trust.DEFAULT_TRUST_DIR
    assert trust.trust_dir({"DEPLOYER_TRUST_DIR": str(tmp_path)}) == tmp_path


def test_inside_the_repo_directly_or_via_symlink_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "t").mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    assert trust.outside(outside_dir, repo) is None
    assert trust.outside(repo / "t", repo) is not None
    link = tmp_path / "link"
    os.symlink(repo / "t", link)
    assert trust.outside(link, repo) is not None


def test_add_revoke_replace(tmp_path: Path) -> None:
    a, b = "ssh-ed25519 AAAAone a", "ssh-ed25519 AAAAtwo b"
    trust.add(tmp_path, a)
    assert "AAAAone" in (tmp_path / trust.ALLOWED_FILE).read_text()
    trust.replace(tmp_path, a, b)
    allowed = (tmp_path / trust.ALLOWED_FILE).read_text()
    assert "AAAAtwo" in allowed and "AAAAone" not in allowed
    assert "AAAAone" in (tmp_path / trust.REVOKED_FILE).read_text()
    trust.revoke(tmp_path, b)
    assert "AAAAtwo" not in (tmp_path / trust.ALLOWED_FILE).read_text()


def test_revoke_is_idempotent(tmp_path: Path) -> None:
    key = "ssh-ed25519 AAAAone a"
    trust.revoke(tmp_path, key)
    trust.revoke(tmp_path, key)
    trust.revoke(tmp_path, key)
    lines = (tmp_path / trust.REVOKED_FILE).read_text().splitlines()
    assert lines.count("ssh-ed25519 AAAAone") == 1


def test_add_does_not_skip_a_key_whose_base64_is_a_prefix_of_a_stored_one(
    tmp_path: Path,
) -> None:
    trust.add(tmp_path, "ssh-ed25519 AAAAlonger x")
    trust.add(tmp_path, "ssh-ed25519 AAAA y")
    lines = (tmp_path / trust.ALLOWED_FILE).read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("ssh-ed25519 AAAAlonger")
    assert lines[1].endswith("ssh-ed25519 AAAA")
