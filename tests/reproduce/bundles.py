"""The committed reproduction bundles as R §8.A replays them: the forge
side (:class:`BundleGh`) and the fake container runtime (:func:`_containers`),
shared by R's acceptance test and admission's pipeline tests."""

import json
import shutil
import subprocess
from pathlib import Path

from deployer.models import ContainerRuntime
from deployer.reproduce.restore import make_tarball
from tests.reproduce.conftest import FakeContainers, proc

BUNDLES = Path(__file__).parent.parent / "fixtures" / "reproduction"


# Untracked noise a working copy can grow inside ``tree/`` (the root
# .gitignore keeps it out of commits); GitHub's archive never carries it.
LOCAL_NOISE = shutil.ignore_patterns("__pycache__", ".DS_Store")


class BundleGh:
    """The forge side of a bundle: its tree listing and a tarball of ``tree/``."""

    def __init__(self, bundle: Path, scratch: Path) -> None:
        """Serve ``bundle``; stage the tarball's tree under ``scratch``."""
        self.bundle = bundle
        self.scratch = scratch

    def api(self, argv: list[str], *, timeout: float) -> str:
        """The bundle's Git tree listing."""
        return (self.bundle / "tree-listing.json").read_text()

    def api_bytes(self, argv: list[str], *, timeout: float) -> bytes:
        """A GitHub-shaped tarball of the bundle's ``tree/``."""
        tree = self.scratch / "tree"
        shutil.copytree(self.bundle / "tree", tree, symlinks=True, ignore=LOCAL_NOISE)
        return make_tarball(tree, "example-project-bundle")


def _containers(
    bundle: Path, fake: FakeContainers
) -> tuple[ContainerRuntime, dict[str, str]]:
    """Script ``fake`` with the bundle's endpoint and local build; return the
    runtime and the environment reproduction sees."""
    endpoint = json.loads((bundle / "endpoint.json").read_text())
    fake.responses[("system", "connection", "list")] = proc(
        stdout=json.dumps(endpoint["connections"])
    )
    fake.responses[("version",)] = proc(
        stdout=json.dumps({"Client": {"Version": "5.7.0"}})
    )
    fake.responses[("image", "inspect")] = proc(1)
    fake.responses[("rmi",)] = proc(0)
    if (bundle / "local.timeout").is_file():
        fake.responses[("build",)] = subprocess.TimeoutExpired(["podman"], 1)
    else:
        fake.responses[("build",)] = proc(
            int((bundle / "local.exit").read_text()),
            stdout=(bundle / "local.stdout").read_text(),
            stderr=(bundle / "local.stderr").read_text(),
        )
    return ContainerRuntime(tool=endpoint["tool"]), endpoint.get("env", {})
