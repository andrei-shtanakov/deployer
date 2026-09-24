"""The ssh-keygen chokepoint: sign, verify, revoke; never crashes on a bad input."""

from pathlib import Path

import pytest

from deployer.provenance import sshsig
from deployer.provenance.model import NAMESPACE, PRINCIPAL
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
    # Deviation from brief: brief used `_allowed(tmp_path / "..", other_pub)`
    # for a second allowed file elsewhere; that name collides with the
    # "other" private key file `make_key` just wrote under tmp_path, and a
    # bare `tmp_path / ".."` can collide across runs, so use a dedicated
    # subdirectory with a name distinct from any key file instead.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert not sshsig.verify(b"record\n", sig, _allowed(elsewhere, other_pub), None).ok
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
    assert sshsig.public_key(key) == pub.rsplit(" ", 1)[0] or sshsig.public_key(
        key
    ).startswith("ssh-ed25519 ")


def test_temp_dir_failure_is_a_named_failure_not_an_exception(
    monkeypatch, tmp_path, keypair
):
    key, pub = keypair
    sig = sshsig.sign(b"r", key)

    def boom(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(sshsig.tempfile, "TemporaryDirectory", boom)
    for result in (
        sshsig.verify(b"r", sig, _allowed(tmp_path, pub), None),
        sshsig.verify_with_public_key(b"r", sig, pub),
    ):
        assert not result.ok and "no space" in (result.reason or "")


def test_missing_ssh_keygen_is_a_named_failure(monkeypatch, tmp_path, keypair):
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


def test_fingerprint_of_a_real_key(keypair: tuple[Path, str]) -> None:
    _, pub = keypair
    fingerprint = sshsig.fingerprint_of(pub)
    assert fingerprint is not None and fingerprint.startswith("SHA256:")


def test_fingerprint_of_returns_none_for_a_header_only_line() -> None:
    assert sshsig.fingerprint_of("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5") is None


def test_fingerprint_of_returns_none_for_a_key_cut_short_mid_key(
    keypair: tuple[Path, str],
) -> None:
    _, pub = keypair
    type_token, b64 = pub.split()[:2]
    assert sshsig.fingerprint_of(f"{type_token} {b64[:60]}") is None


def test_fingerprint_of_never_raises_when_ssh_keygen_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keypair: tuple[Path, str]
) -> None:
    _, pub = keypair
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert sshsig.fingerprint_of(pub) is None
