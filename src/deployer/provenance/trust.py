"""The trust store, outside the checked repository (A §2.3)."""

import base64
import binascii
import os
from collections.abc import Mapping
from pathlib import Path

from deployer.provenance import sshsig
from deployer.provenance.model import NAMESPACE, PRINCIPAL

ALLOWED_FILE = "allowed_signers"
REVOKED_FILE = "revoked_keys"
DEFAULT_TRUST_DIR = Path.home() / ".config" / "deployer"


class TrustError(ValueError):
    """A public-key line is malformed, so no file was touched."""


def trust_dir(env: Mapping[str, str]) -> Path:
    """``DEPLOYER_TRUST_DIR`` or the default; not yet resolved."""
    override = env.get("DEPLOYER_TRUST_DIR")
    return Path(override) if override else DEFAULT_TRUST_DIR


def outside(trust: Path, *repos: Path) -> str | None:
    """``None`` when the trust dir's real path is outside every repo, else why not.

    Paths are compared by spelling and, for every existing ancestor of the
    trust dir, by file identity (``st_dev``, ``st_ino``): on a
    case-insensitive filesystem (APFS) ``/x/WORK`` names ``/x/work`` although
    the strings differ.
    """
    real = trust.resolve(strict=False)
    roots = [repo.resolve(strict=False) for repo in repos]
    for root in roots:
        if real == root or root in real.parents:
            return f"trust directory {real} lies inside {root}"
    identities = {
        ident: root for root in roots if (ident := _identity(root)) is not None
    }
    for ancestor in (real, *real.parents):
        ident = _identity(ancestor)
        root = identities.get(ident) if ident is not None else None
        if root is not None:
            return f"trust directory {real} lies inside {root}"
    return None


def _identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of an existing path, else ``None``."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _key_body(public_line: str) -> str:
    """``type base64`` without the comment, for comparisons."""
    return " ".join(public_line.split()[:2])


def validate_public_line(public_line: str) -> None:
    """Reject anything that is not a well-formed SSH public-key line.

    Two layers. First, pure Python, cheap and specific: at least a ``type``
    and a ``base64`` token; the base64 must decode strictly
    (``base64.b64decode(..., validate=True)``); and the decoded blob's own
    length-prefixed name — the SSH wire format for a public key, a uint32
    big-endian length followed by exactly that many bytes — must equal the
    ``type`` token. ssh-keygen itself does not check that the type token on
    the line matches what the blob actually encodes (it goes by the blob),
    so this stays even though the second layer also parses the blob.

    Second, ssh-keygen is the authority on whether the blob is an actually
    complete, well-formed key of its type: ``sshsig.fingerprint_of`` must
    resolve to a fingerprint. This catches what the first layer cannot — a
    structurally-plausible-looking blob (right header, valid base64) that is
    truncated mid-key or otherwise the wrong length for its declared type,
    such as a bare header with no key material at all.
    """
    tokens = public_line.split()
    if len(tokens) < 2:
        raise TrustError(
            f"not a public key: need a type and base64 token, got {public_line!r}"
        )
    type_token, b64_token = tokens[0], tokens[1]
    try:
        blob = base64.b64decode(b64_token, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustError(f"not a public key: invalid base64: {exc}") from exc
    if len(blob) < 4:
        raise TrustError("not a public key: blob too short for a wire-format name")
    name_length = int.from_bytes(blob[:4], "big")
    name = blob[4 : 4 + name_length]
    if len(name) != name_length or name != type_token.encode():
        raise TrustError(
            f"not a public key: type token {type_token!r} does not match "
            "the key type encoded in the blob"
        )
    if sshsig.fingerprint_of(public_line) is None:
        raise TrustError(
            "not a public key: ssh-keygen does not recognize it as a "
            f"complete {type_token} key"
        )


def _line_key_body(line: str) -> str:
    """The trailing ``type base64`` pair of a stored line.

    Both files store the pair last: `allowed_signers` prefixes it with the
    principal and `namespaces="..."`, `revoked_keys` has nothing before it.
    Taking the last two tokens (rather than a substring test) means a key
    whose base64 happens to be a prefix of another stored key's base64 is
    never mistaken for it.
    """
    tokens = line.split()
    return " ".join(tokens[-2:]) if len(tokens) >= 2 else line.strip()


def _file_has_key(path: Path, body: str) -> bool:
    """Whether some line of ``path`` carries exactly this key body."""
    if not path.is_file():
        return False
    return any(_line_key_body(ln) == body for ln in path.read_text().splitlines())


def add(trust: Path, public_line: str) -> None:
    """Allow a key for ``deployer-authoring``."""
    validate_public_line(public_line)
    trust.mkdir(parents=True, exist_ok=True)
    allowed = trust / ALLOWED_FILE
    body = _key_body(public_line)
    if not _file_has_key(allowed, body):
        with allowed.open("a") as f:
            f.write(f'{PRINCIPAL} namespaces="{NAMESPACE}" {body}\n')


def revoke(trust: Path, public_line: str) -> None:
    """Drop a key from the allowed set and list it as revoked, once."""
    validate_public_line(public_line)
    trust.mkdir(parents=True, exist_ok=True)
    body = _key_body(public_line)
    allowed = trust / ALLOWED_FILE
    if allowed.is_file():
        kept = [
            ln for ln in allowed.read_text().splitlines() if _line_key_body(ln) != body
        ]
        allowed.write_text("".join(f"{ln}\n" for ln in kept))
    revoked = trust / REVOKED_FILE
    if not _file_has_key(revoked, body):
        with revoked.open("a") as f:
            f.write(f"{body}\n")


def replace(trust: Path, old_line: str, new_line: str) -> None:
    """Add the new key, then revoke the old one. No rotation beyond this.

    Both lines are validated before either file is touched: a malformed
    ``new_line`` must leave the store exactly as it was, with the old key
    still allowed, rather than revoking it and writing a broken allow line.
    """
    validate_public_line(old_line)
    validate_public_line(new_line)
    add(trust, new_line)
    revoke(trust, old_line)
