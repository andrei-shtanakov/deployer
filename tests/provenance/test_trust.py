"""The trust store: allowed_signers/revoked_keys directory outside the repo."""

import os
from pathlib import Path

import pytest

from deployer.provenance import trust
from tests.provenance.conftest import make_key

# Static shapes ssh-keygen never needs to see: rejected by the pure-Python
# layer of `validate_public_line` alone (wrong token count / bad base64).
_STATIC_BAD_LINES = {
    "truncated": "ssh-ed25519",
    "garbage_base64": "ssh-ed25519 not-valid-base64!!! c",
    "header_only": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
}

_BAD_LABELS = [*_STATIC_BAD_LINES, "cut_short", "type_mismatch"]


def _cut_short_line(real_pub: str, length: int = 60) -> str:
    """A real key's base64 truncated mid-key: valid base64, wrong length.

    Passes the pure-Python layer (decodes, wire name still matches — the
    header survives a cut this shallow) but ssh-keygen rejects the result as
    an incomplete key.
    """
    type_token, b64 = real_pub.split()[:2]
    return f"{type_token} {b64[:length]} c"


def _type_mismatch_line(real_pub: str) -> str:
    """A real key's blob relabeled under a type it does not encode."""
    _, b64 = real_pub.split()[:2]
    return f"ssh-rsa {b64} c"


def _bad_line(label: str, real_pub: str) -> str:
    """One of the five malformed shapes; ``real_pub`` only matters for the
    two built from real key material."""
    if label == "cut_short":
        return _cut_short_line(real_pub)
    if label == "type_mismatch":
        return _type_mismatch_line(real_pub)
    return _STATIC_BAD_LINES[label]


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


def test_file_has_key_does_not_match_a_substring_of_a_stored_body(
    tmp_path: Path, keypair: tuple[Path, str]
) -> None:
    """The regression this guards: dedup must compare a whole stored key
    body, not test whether it merely *contains* the candidate as text — a
    strict prefix of a real, already-stored body must not read as present.
    """
    _, pub = keypair
    trust.add(tmp_path, pub)
    body = " ".join(pub.split()[:2])
    allowed = tmp_path / trust.ALLOWED_FILE
    assert trust._file_has_key(allowed, body)
    assert not trust._file_has_key(allowed, body[:30])


@pytest.mark.parametrize("label", _BAD_LABELS)
def test_add_refuses_a_malformed_key(
    tmp_path: Path, keypair: tuple[Path, str], label: str
) -> None:
    _, real_pub = keypair
    with pytest.raises(trust.TrustError):
        trust.add(tmp_path, _bad_line(label, real_pub))
    assert not (tmp_path / trust.ALLOWED_FILE).exists()


@pytest.mark.parametrize("label", _BAD_LABELS)
def test_revoke_refuses_a_malformed_key(
    tmp_path: Path, keypair: tuple[Path, str], label: str
) -> None:
    _, real_pub = keypair
    with pytest.raises(trust.TrustError):
        trust.revoke(tmp_path, _bad_line(label, real_pub))
    assert not (tmp_path / trust.REVOKED_FILE).exists()


@pytest.mark.parametrize("label", _BAD_LABELS)
def test_replace_refuses_a_malformed_new_key_and_leaves_the_old_key_allowed(
    tmp_path: Path, keypair: tuple[Path, str], label: str
) -> None:
    _, old_pub = keypair
    trust.add(tmp_path, old_pub)
    allowed_before = (tmp_path / trust.ALLOWED_FILE).read_bytes()
    with pytest.raises(trust.TrustError):
        trust.replace(tmp_path, old_pub, _bad_line(label, old_pub))
    assert (tmp_path / trust.ALLOWED_FILE).read_bytes() == allowed_before
    assert not (tmp_path / trust.REVOKED_FILE).exists()


@pytest.mark.parametrize("label", _BAD_LABELS)
def test_replace_refuses_a_malformed_old_key_and_touches_nothing(
    tmp_path: Path, keypair: tuple[Path, str], label: str
) -> None:
    _, new_pub = keypair
    with pytest.raises(trust.TrustError):
        trust.replace(tmp_path, _bad_line(label, new_pub), new_pub)
    assert not (tmp_path / trust.ALLOWED_FILE).exists()
    assert not (tmp_path / trust.REVOKED_FILE).exists()
