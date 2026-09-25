"""Dev tool: record real local Podman builds for the local template rows
(F §6.3, §9, §11 stage 3). Owner-permitted real runs; the output is data for
the owner's review.

Run from the repository root::

    uv run python tests/fixtures/recordings/record_local.py --all
    uv run python tests/fixtures/recordings/record_local.py --case l3-from-run5

Every case builds with R's own :func:`deployer.reproduce.build.run_build`, so
the argv is the one production uses (``podman build --file … --tag …
--force-rm <context>``). Outputs are written **verbatim**: nothing is trimmed,
edited or redacted. A case directory that already exists is refused —
re-recording means deleting it and going through the owner's review again.

The tool removes only the tags it made (``localhost/deployer-lrec-<case>``)
and never prunes. ``l2-copy-warm`` builds ``l1-copy-cold``'s tree again before
either tag is removed, so its COPY step reads the layer cache.

The trees are derived, not historical: the project files of ``polygon/run-5``
(commit ``937d465``: ``pyproject.toml``, ``uv.lock``, ``src/``, ``tests/``),
plus the case's own ``Dockerfile`` and added files. ``expected.json`` is
written separately, by replaying the matchers (see ``PROVENANCE.md``).
"""

import argparse
import json
import platform
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deployer.reproduce.build import cleanup_image, run_build
from deployer.reproduce.buildline import BuildConfig
from deployer.runtime import ContainerRuntime

HERE = Path(__file__).resolve().parent
LOCAL = HERE / "local"
PROJECT_COMMIT = "937d465"
PROJECT_FILES = ("pyproject.toml", "uv.lock", "src", "tests")
TIMEOUT = 900

_HEAD = (
    "FROM python:3.12-slim\n\n"
    "COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/\n\n"
    "WORKDIR /app\n\n"
    "COPY pyproject.toml uv.lock ./\n"
    "RUN uv sync --frozen --no-install-project\n\n"
    "COPY src/ci_build ./src/ci_build\n"
)
_TAIL = (
    "\nRUN uv sync --frozen\n\n"
    "RUN useradd --create-home appuser\n"
    "USER appuser\n\n"
    'ENV PATH="/app/.venv/bin:$PATH"\n\n'
    'CMD ["python"]\n'
)
COPY_FIXED = "COPY docs/guide/setup.md ./setup.md"
FROM_FIXED = "FROM python:3.12-slim AS extra"
FROM_BAD = "FROM python:3.12-slim extra"
SETUP_MD = "# Setup\n\nDerived recording input (not historical).\n"


@dataclass(frozen=True)
class Check:
    """One matcher question asked of a recording."""

    kind: str
    corrected: str


@dataclass(frozen=True)
class Case:
    """A recording: its Dockerfile, added files and the checks replayed on it."""

    name: str
    dockerfile: str
    checks: tuple[Check, ...]
    added: dict[str, str] = field(default_factory=dict)
    same_tree_as: str | None = None


def _cases(nonce: str) -> tuple[Case, ...]:
    setup = {"docs/guide/setup.md": SETUP_MD}
    cold = {"docs/guide/setup.md": f"{SETUP_MD}\nrecording nonce: {nonce}\n"}
    copy_df = _HEAD + f"{COPY_FIXED}\n" + _TAIL
    return (
        Case("l1-copy-cold", copy_df, (Check("copy", COPY_FIXED),), cold),
        Case(
            "l2-copy-warm",
            copy_df,
            (Check("copy", COPY_FIXED),),
            same_tree_as="l1-copy-cold",
        ),
        Case(
            "l3-from-run5",
            _HEAD.replace("FROM python:3.12-slim\n", f"{FROM_FIXED}\n", 1) + _TAIL,
            (Check("from", FROM_FIXED),),
        ),
        Case(
            "l4-from-bad-later",
            f"FROM python:3.12-slim AS a\nRUN true\n{FROM_BAD}\nRUN true\n",
            (Check("from", FROM_BAD),),
        ),
        Case(
            "l5-stages-same-image",
            (
                "FROM python:3.12-slim AS a\nWORKDIR /app\n"
                f"{COPY_FIXED}\n\n"
                "FROM python:3.12-slim AS b\nWORKDIR /app\n"
                f"{COPY_FIXED}\n"
            ),
            (Check("copy", COPY_FIXED), Check("from", "FROM python:3.12-slim AS b")),
            setup,
        ),
        Case(
            "l6-copy-later-failure",
            _HEAD + f"{COPY_FIXED}\nRUN false\n" + _TAIL,
            (Check("copy", COPY_FIXED),),
            setup,
        ),
        # l5 showed Podman skipping a stage nothing depends on; here stage b
        # copies from a, so both stages build the identical COPY.
        Case(
            "l7-stages-both-built",
            (
                "FROM python:3.12-slim AS a\nWORKDIR /app\n"
                f"{COPY_FIXED}\n\n"
                "FROM python:3.12-slim AS b\nWORKDIR /app\n"
                f"{COPY_FIXED}\n"
                "COPY --from=a /app/setup.md ./from-a.md\n"
            ),
            (Check("copy", COPY_FIXED), Check("from", "FROM python:3.12-slim AS b")),
            setup,
        ),
        # Buildah checks FROM's argument count per stage, when the stage starts
        # (docs/fix-buildah-from-parse.md). Here the bad FROM's stage needs a:
        # a builds first, then the bad stage fails.
        Case(
            "l8-from-bad-after-built-stage",
            (
                "FROM python:3.12-slim AS a\nRUN true\n"
                f"{FROM_BAD}\n"
                "COPY --from=a /etc/hostname /hostname-a\n"
            ),
            (Check("from", FROM_BAD),),
        ),
        # A bad FROM in a stage nothing depends on is skipped, never checked:
        # the build succeeds with the defect still in the file.
        Case(
            "l9-from-bad-in-skipped-stage",
            (f"{FROM_BAD}\nRUN true\n\nFROM python:3.12-slim AS final\nRUN true\n"),
            (Check("from", FROM_BAD),),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Record the requested cases; refuse any that already exist."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--case", action="append")
    args = parser.parse_args(argv)
    cases = _cases(uuid.uuid4().hex)
    wanted = [c for c in cases if args.all or c.name in (args.case or [])]
    existing = [c.name for c in wanted if (LOCAL / c.name).exists()]
    if existing:
        print(f"refusing: already recorded: {existing}", file=sys.stderr)
        return 2
    rt = ContainerRuntime(tool="podman")
    env = _environment()
    built: list[tuple[str, bool]] = []
    try:
        for case in wanted:
            built.append((_tag(case.name), _record(case, rt, env)))
    finally:
        for tag, ok in built:
            print(tag, cleanup_image(rt, tag, ok))
    return 0


def _record(case: Case, rt: ContainerRuntime, env: dict[str, str]) -> bool:
    """Build one case and write its files verbatim; return whether it built."""
    root = LOCAL / case.name
    tree = root / "tree"
    if case.same_tree_as is not None:
        _copy_tree(LOCAL / case.same_tree_as / "tree", tree)
    else:
        _project(tree)
        for rel, text in case.added.items():
            _write(tree / rel, text)
        _write(tree / "Dockerfile", case.dockerfile)
    config = BuildConfig("Dockerfile", (), None, None)
    run = run_build(rt, tree, config, _tag(case.name), TIMEOUT)
    _write(root / "build.stdout", run.stdout)
    _write(root / "build.stderr", run.stderr)
    _write(root / "build.exit", f"{run.exit_code}\n")
    argv = [str(a).replace(str(tree), "<context>") for a in run.argv]
    _json(root / "argv.json", {"argv": argv, "launch_error": run.launch_error})
    _json(root / "environment.json", {**env, "recorded_at": _now()})
    _json(
        root / "checks.json",
        [{"kind": c.kind, "corrected": c.corrected} for c in case.checks],
    )
    print(case.name, run.exit_code, run.launch_error)
    return run.exit_code == 0


def _project(tree: Path) -> None:
    """Materialise the project files of ``polygon/run-5`` into ``tree``."""
    tree.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", PROJECT_COMMIT, *PROJECT_FILES],
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)


def _copy_tree(src: Path, dst: Path) -> None:
    for path in sorted(src.rglob("*")):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def _environment() -> dict[str, str]:
    def podman(fmt: str) -> str:
        cmd = ["podman", "info", "--format", fmt]
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout

    return {
        "podman": podman("{{.Version.Version}}").strip(),
        "buildah": podman("{{.Host.BuildahVersion}}").strip(),
        "vm_os": podman("{{.Host.OS}}/{{.Host.Arch}}").strip(),
        "vm_kernel": podman("{{.Host.Kernel}}").strip(),
        "client": f"{platform.system()}/{platform.machine()}",
    }


def _tag(name: str) -> str:
    return f"localhost/deployer-lrec-{name}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def _json(path: Path, data: object) -> None:
    _write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
