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
