"""The trust store: allowed_signers/revoked_keys directory outside the repo."""

import os
from pathlib import Path

import pytest

from deployer.provenance import trust
from tests.provenance.conftest import make_key, synthetic_key_line

_TYPE_MISMATCH = "ssh-ed25519 " + synthetic_key_line("ssh-rsa").split()[1] + " c"

_BAD_LINES = {
    "truncated": "ssh-ed25519",
    "garbage_base64": "ssh-ed25519 not-valid-base64!!! c",
    "type_mismatch": _TYPE_MISMATCH,
}


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


def test_add_revoke_replace(tmp_path: Path, keypair: tuple[Path, str]) -> None:
    _, a = keypair
    _, b = make_key(tmp_path, "second")
    trust.add(tmp_path, a)
    assert a.split()[1] in (tmp_path / trust.ALLOWED_FILE).read_text()
    trust.replace(tmp_path, a, b)
    allowed = (tmp_path / trust.ALLOWED_FILE).read_text()
    assert b.split()[1] in allowed and a.split()[1] not in allowed
    assert a.split()[1] in (tmp_path / trust.REVOKED_FILE).read_text()
    trust.revoke(tmp_path, b)
    assert b.split()[1] not in (tmp_path / trust.ALLOWED_FILE).read_text()


def test_revoke_is_idempotent(tmp_path: Path, keypair: tuple[Path, str]) -> None:
    _, key = keypair
    trust.revoke(tmp_path, key)
    trust.revoke(tmp_path, key)
    trust.revoke(tmp_path, key)
    lines = (tmp_path / trust.REVOKED_FILE).read_text().splitlines()
    body = " ".join(key.split()[:2])
    assert lines.count(body) == 1


def test_add_does_not_skip_a_key_whose_base64_is_a_prefix_of_a_stored_one(
    tmp_path: Path,
) -> None:
    short = synthetic_key_line("ssh-ed25519")
    long = synthetic_key_line("ssh-ed25519", payload=b"x" * 32)
    # sanity: this is the exact bug shape — one key's base64 is a literal
    # string prefix of the other's.
    assert long.split()[1].startswith(short.split()[1])
    trust.add(tmp_path, long)
    trust.add(tmp_path, short)
    lines = (tmp_path / trust.ALLOWED_FILE).read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(long.split()[1])
    assert lines[1].endswith(short.split()[1])


@pytest.mark.parametrize("label", sorted(_BAD_LINES))
def test_add_refuses_a_malformed_key(tmp_path: Path, label: str) -> None:
    with pytest.raises(trust.TrustError):
        trust.add(tmp_path, _BAD_LINES[label])
    assert not (tmp_path / trust.ALLOWED_FILE).exists()


@pytest.mark.parametrize("label", sorted(_BAD_LINES))
def test_revoke_refuses_a_malformed_key(tmp_path: Path, label: str) -> None:
    with pytest.raises(trust.TrustError):
        trust.revoke(tmp_path, _BAD_LINES[label])
    assert not (tmp_path / trust.REVOKED_FILE).exists()


@pytest.mark.parametrize("label", sorted(_BAD_LINES))
def test_replace_refuses_a_malformed_new_key_and_leaves_the_old_key_allowed(
    tmp_path: Path, keypair: tuple[Path, str], label: str
) -> None:
    _, old_pub = keypair
    trust.add(tmp_path, old_pub)
    allowed_before = (tmp_path / trust.ALLOWED_FILE).read_bytes()
    with pytest.raises(trust.TrustError):
        trust.replace(tmp_path, old_pub, _BAD_LINES[label])
    assert (tmp_path / trust.ALLOWED_FILE).read_bytes() == allowed_before
    assert not (tmp_path / trust.REVOKED_FILE).exists()


@pytest.mark.parametrize("label", sorted(_BAD_LINES))
def test_replace_refuses_a_malformed_old_key_and_touches_nothing(
    tmp_path: Path, label: str
) -> None:
    with pytest.raises(trust.TrustError):
        trust.replace(tmp_path, _BAD_LINES[label], synthetic_key_line("ssh-ed25519"))
    assert not (tmp_path / trust.ALLOWED_FILE).exists()
    assert not (tmp_path / trust.REVOKED_FILE).exists()
