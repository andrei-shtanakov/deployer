"""The ssh-keygen chokepoint (A §2.2-§2.4): sign and verify authoring records."""

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from deployer.provenance.model import NAMESPACE, PRINCIPAL

_TIMEOUT_S = 30
_FINGERPRINT_RE = re.compile(r"key (SHA256:\S+)")
_KEYGEN_L_FINGERPRINT_RE = re.compile(r"(SHA256:\S+)")


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
        ["ssh-keygen", *args],
        input=stdin,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def sign(data: bytes, key: Path) -> bytes:
    """Sign ``data`` in the ``deployer-authoring`` namespace with ``key``."""
    try:
        proc = _run(["-Y", "sign", "-f", str(key), "-n", NAMESPACE], data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshSigError(f"ssh-keygen sign failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise SshSigError(f"ssh-keygen sign failed: {detail}")
    return proc.stdout


def public_key(key: Path) -> str:
    """The public line of a private key (``type base64``)."""
    try:
        proc = _run(["-y", "-f", str(key)], b"")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SshSigError(f"ssh-keygen -y failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise SshSigError(f"ssh-keygen -y failed: {detail}")
    return proc.stdout.decode().strip()


def fingerprint_of(public_line: str) -> str | None:
    """The key's fingerprint via ``ssh-keygen -l``, or ``None`` if it is not
    a real key ssh-keygen recognizes; never raises.

    ssh-keygen is the authority on what is actually a well-formed public
    key of its declared type (correct key-material length, valid point
    encoding where it checks one, and so on) — a per-algorithm parser in
    pure Python would only ever re-implement a subset of that and drift
    from it over time. A structurally plausible but truncated or
    otherwise-invalid line (right token count, valid base64, matching wire
    name, wrong key-material length) makes ``ssh-keygen -l`` exit nonzero
    with no fingerprint, exactly as it does for garbage.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "key.pub"
            key_file.write_text(public_line.strip() + "\n")
            proc = _run(["-l", "-f", str(key_file)], b"")
    except (OSError, subprocess.TimeoutExpired, SshSigError):
        return None
    if proc.returncode != 0:
        return None
    match = _KEYGEN_L_FINGERPRINT_RE.search(proc.stdout.decode(errors="replace"))
    return match.group(1) if match is not None else None


def verify(
    data: bytes,
    signature: bytes,
    allowed_signers: Path,
    revoked: Path | None,
) -> Verified:
    """Verify against an allowed_signers file; never raises."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sig_path = Path(tmp) / "sig"
            sig_path.write_bytes(signature)
            args = [
                "-Y",
                "verify",
                "-f",
                str(allowed_signers),
                "-I",
                PRINCIPAL,
                "-n",
                NAMESPACE,
                "-s",
                str(sig_path),
            ]
            if revoked is not None:
                args += ["-r", str(revoked)]
            proc = _run(args, data)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Verified(False, None, f"ssh-keygen verify could not run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode(errors="replace").strip()
        return Verified(
            False, None, f"signature not verified: {detail or proc.returncode}"
        )
    match = _FINGERPRINT_RE.search(proc.stdout.decode(errors="replace"))
    if match is None:
        return Verified(
            False, None, "signature verified but no key fingerprint reported"
        )
    return Verified(True, match.group(1), None)


def verify_with_public_key(data: bytes, signature: bytes, public_line: str) -> Verified:
    """Verify against one public key (authoring's own reuse check); never raises."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            allowed = Path(tmp) / "allowed_signers"
            allowed.write_text(f'{PRINCIPAL} namespaces="{NAMESPACE}" {public_line}\n')
            return verify(data, signature, allowed, None)
    except OSError as exc:
        return Verified(False, None, f"could not prepare verification: {exc}")
