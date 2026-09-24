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
