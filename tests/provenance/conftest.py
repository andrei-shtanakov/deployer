import base64
import subprocess
from pathlib import Path

import pytest


def make_key(directory: Path, name: str = "k") -> tuple[Path, str]:
    key = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
    )
    return key, (directory / f"{name}.pub").read_text().strip()


@pytest.fixture()
def keypair(tmp_path: Path) -> tuple[Path, str]:
    return make_key(tmp_path)


def synthetic_key_line(key_type: str, payload: bytes = b"") -> str:
    """A syntactically valid SSH public-key line, built without ssh-keygen.

    Encodes the SSH wire format directly (uint32 big-endian name length, the
    name, then ``payload``) so tests can control the base64 bytes exactly —
    e.g. to make one key's base64 a literal string prefix of another's, or to
    mismatch the leading type token against the name actually encoded —
    without needing a real key pair.
    """
    name = key_type.encode()
    blob = len(name).to_bytes(4, "big") + name + payload
    return f"{key_type} {base64.b64encode(blob).decode()} synthetic"


def _git(repo: Path, *args: str) -> None:
    """Run a git command against ``repo``, raising on failure."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    """Create a committed repo under ``root``: pyproject.toml, src/m.py, init."""
    r = root / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "pyproject.toml").write_text('[project]\nname = "p"\n')
    (r / "src").mkdir()
    (r / "src" / "m.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def make_repo_with_origin(root: Path, url: str = "git@github.com:o/r.git") -> Path:
    """``make_repo`` plus an ``origin`` remote pointing at ``url``."""
    r = make_repo(root)
    _git(r, "remote", "add", "origin", url)
    return r


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)
