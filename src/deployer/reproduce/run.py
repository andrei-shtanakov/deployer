"""Orchestration of one reproduction try (spec §1.5, §6).

Order: prechecks (no I/O) → source (fetch once per attempt, reuse) → workflow
shape → a new try directory → static checks → runtime and endpoint → build →
builder check → comparison → manifest. Refusals after the tree is available
keep the checks already made; a refusal before it creates nothing.
"""

import json
import os
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from deployer.forge import (
    DEFAULT_MAX_ARCHIVE_MB,
    FailedRun,
    GhBytesRunner,
    GhError,
    TreeEntry,
    TreeListing,
    fetch_archive,
    fetch_tree_listing,
)
from deployer.models import ContainerRuntime
from deployer.reproduce import build as build_mod
from deployer.reproduce import (
    buildcheck,
    checks,
    compare,
    dockerfile,
    endpoint,
    ignore,
    restore,
    shape,
)
from deployer.reproduce.model import (
    Binding,
    BuildResult,
    Dimension,
    Environment,
    ReproductionCheck,
    ReproductionSection,
    Restoration,
)
from deployer.runtime import probe_runtime_versions


class TryDirError(Exception):
    """The try directory cannot be created, or the attempt names another SHA."""


def reproduce_run(
    snapshot: FailedRun,
    *,
    gh: GhBytesRunner,
    rt: ContainerRuntime | None,
    runtime_error: str | None,
    env: Mapping[str, str],
    root: Path,
    build_timeout: int,
    max_archive_mb: int = DEFAULT_MAX_ARCHIVE_MB,
) -> ReproductionSection:
    """One try; raises :class:`TryDirError` only for the §6 exit-2 case."""
    job = shape.precheck(snapshot)
    if isinstance(job, shape.Refusal):
        return ReproductionSection(status="refused", refusal=job.reason)
    attempt_dir = (
        root
        / ".deployer-runs"
        / str(snapshot.run_id)
        / "reproduction"
        / f"attempt-{snapshot.attempt}"
    )
    source = _source(snapshot, gh, attempt_dir, max_archive_mb)
    if isinstance(source, str):
        return ReproductionSection(status="unavailable", refusal=source)
    source_dir, listing = source
    workflow = source_dir / (snapshot.workflow_path or "")
    if not workflow.is_file():
        return ReproductionSection(
            status="refused",
            refusal=f"workflow file {snapshot.workflow_path} not in the tree",
        )
    found = shape.check_workflow(snapshot, job, workflow.read_text(errors="replace"))
    if isinstance(found, shape.Refusal):
        return ReproductionSection(status="refused", refusal=found.reason)

    try_dir = _new_try(attempt_dir)
    context = try_dir / "context"
    try:
        shutil.copytree(source_dir, context, symlinks=True)
    except OSError as exc:
        raise TryDirError(f"cannot prepare the try context: {exc}") from exc
    try:
        _make_writable(context)
    except OSError as exc:
        raise TryDirError(f"cannot make context/ writable: {exc}") from exc
    rel_try = try_dir.relative_to(root).as_posix()

    df_path = context / found.build.dockerfile
    parsed = dockerfile.parse(
        df_path.read_text(errors="replace") if df_path.is_file() else ""
    )
    ci_rules = ignore.load_rules(
        context, ignore.ci_ignore_file(context, found.build.dockerfile)
    )
    parser_checks = dockerfile.syntax_checks(parsed, found.build.dockerfile)
    static = (
        parser_checks
        + checks.copy_source_checks(parsed, context, found.build.dockerfile, ci_rules)
        + checks.from_ref_checks(parsed)
    )
    unmet = (
        found.preceding_unmet
        + restore.listing_conditions(source_dir, listing)
        + checks.context_conditions(parsed)
    )
    restoration = Restoration(
        state="approximation" if unmet else "exact", sha=snapshot.head_sha, unmet=unmet
    )
    binding = Binding(
        job_id=job.job_id,
        workflow_job=found.workflow_job,
        build_step=found.build_step,
        dockerfile=found.build.dockerfile,
    )
    common: dict[str, Any] = dict(
        try_dir=rel_try, restoration=restoration, binding=binding
    )

    if rt is None:
        section = ReproductionSection(
            status="refused",
            refusal=f"no container runtime: {runtime_error or 'none found'}",
            checks=static,
            **common,
        )
        return _write(try_dir, section)

    confirmed = endpoint.confirm_local(rt, env)
    if isinstance(confirmed, shape.Refusal):
        section = ReproductionSection(
            status="refused", refusal=confirmed.reason, checks=static, **common
        )
        return _write(try_dir, section)

    section = _build_and_compare(
        snapshot.run_id,
        found,
        rt,
        confirmed,
        context,
        try_dir,
        parsed,
        parser_checks,
        static,
        ci_rules,
        restoration,
        build_timeout,
    )
    return _write(try_dir, section.model_copy(update=common))


def _build_and_compare(
    run_id: int,
    found: shape.Shape,
    rt: ContainerRuntime,
    confirmed: endpoint.Endpoint,
    context: Path,
    try_dir: Path,
    parsed: dockerfile.ParsedDockerfile,
    parser_checks: list[ReproductionCheck],
    static: list[ReproductionCheck],
    ci_rules: ignore.IgnoreRules,
    restoration: Restoration,
    build_timeout: int,
) -> ReproductionSection:
    """Build the failed step, run the builder check, compare with CI (§4-§7)."""
    seq = try_dir.name
    tag = build_mod.repro_tag(run_id, seq)
    run = build_mod.run_build(rt, context, found.build, tag, build_timeout)
    _write_text(try_dir / "build.stdout", run.stdout)
    _write_text(try_dir / "build.stderr", run.stderr)

    syntax, lint, buildx, check_run = buildcheck.run_builder_check(
        rt, context, found.build.dockerfile, build_timeout
    )
    if check_run is not None:
        _write_text(try_dir / "check.stdout", check_run.stdout)
        _write_text(try_dir / "check.stderr", check_run.stderr)
    merged = buildcheck.merge_syntax(parser_checks, syntax, found.build.dockerfile)
    checks_out = merged + lint + [c for c in static if c not in parser_checks]

    images = checks.external_images(parsed)
    local_digests = build_mod.local_repo_digests(rt, images)
    text = shape.job_text(found.job)
    failed = [c for c in parser_checks if c.status == "failed"]
    local_ref = (
        compare.local_instruction(run.stdout, run.stderr, rt.tool, parsed, failed)
        if run.exit_code not in (None, 0)
        else None
    )
    ci_side = compare.Side(
        compare.ci_instruction(text), compare.ci_signature(text), _last_error(text)
    )
    local_side = compare.Side(
        local_ref,
        compare.local_signature(run.stdout, run.stderr, rt.tool),
        _last_error(run.stdout + "\n" + run.stderr),
    )
    ci_ignore = ci_rules.file
    local_ignore = ignore.local_ignore_file(context, found.build.dockerfile, rt.tool)
    values: dict[str, tuple[str | None, str | None]] = {
        "backend": ("docker", rt.tool),
        "host_arch": (None, platform.machine()),
        "ignore_file": (ci_ignore, local_ignore),
    }
    dimensions: dict[str, Dimension] = {
        "backend": compare.dimension("docker", rt.tool),
        "host_arch": "unknown",
        "base_image_digests": compare.digest_dimension(
            compare.ci_digests(text), local_digests, images
        ),
        "ignore_file": "same" if ci_ignore == local_ignore else "differs",
        "restoration": "same" if restoration.state == "exact" else "unknown",
    }
    comparison = compare.compare(
        exit_code=run.exit_code,
        launch_error=run.launch_error,
        ci=ci_side,
        local=local_side,
        parsed=parsed,
        dimensions=dimensions,
        values=values,
    )
    versions = probe_runtime_versions(rt)
    return ReproductionSection(
        status="attempted",
        environment=Environment(
            backend=rt.tool,
            backend_version=versions.client_version,
            endpoint=confirmed.uri,
            endpoint_source=confirmed.source,
            buildx_version=buildx,
            host_arch=platform.machine(),
            syntax_directive=parsed.syntax_directive,
        ),
        checks=checks_out,
        build=BuildResult(
            argv=_relative_argv(run.argv, try_dir),
            exit_code=run.exit_code,
            launch_error=run.launch_error,
            failed_instruction=local_ref,
            signature=local_side.signature,
            stdout="build.stdout",
            stderr="build.stderr",
            image_cleanup=build_mod.cleanup_image(rt, tag, built=run.exit_code == 0),
            build_containers=build_mod.build_containers_state(
                rt, finished=run.launch_error is None
            ),
        ),
        comparison=comparison,
    )


def _source(
    snapshot: FailedRun, gh: GhBytesRunner, attempt_dir: Path, max_archive_mb: int
) -> tuple[Path, TreeListing] | str:
    """The attempt's restored tree: reused from disk, or freshly fetched."""
    source_dir = attempt_dir / "source"
    meta = attempt_dir / "source.json"
    if meta.is_file():
        try:
            data = json.loads(meta.read_text())
            if data.get("head_sha") != snapshot.head_sha:
                raise TryDirError(
                    f"{meta} names {data.get('head_sha')}, the run is at "
                    f"{snapshot.head_sha}"
                )
            stored = data["listing"]
            return source_dir, TreeListing(
                sha=stored["sha"],
                entries=[TreeEntry(**e) for e in stored["entries"]],
                truncated=stored["truncated"],
            )
        except TryDirError:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as exc:
            raise TryDirError(f"cannot read {meta}: {exc}") from exc
    try:
        listing = fetch_tree_listing(snapshot.repo, snapshot.head_sha, gh)
        archive = fetch_archive(
            snapshot.repo, snapshot.head_sha, gh, max_bytes=max_archive_mb * 1024 * 1024
        )
    except GhError as exc:
        return f"archive fetch failed: {exc}"
    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TryDirError(f"cannot create {attempt_dir}: {exc}") from exc
    reason = restore.extract(archive, source_dir)
    if reason is not None:
        shutil.rmtree(source_dir, ignore_errors=True)
        return reason
    payload = json.dumps(
        {
            "repo": snapshot.repo,
            "run_id": snapshot.run_id,
            "attempt": snapshot.attempt,
            "head_sha": snapshot.head_sha,
            "archive_bytes": len(archive),
            "listing": {
                "sha": listing.sha,
                "truncated": listing.truncated,
                "entries": [e.__dict__ for e in listing.entries],
            },
            "tree": [e.path for e in listing.entries if e.type == "blob"],
        },
        indent=2,
    )
    try:
        meta.write_text(payload)
    except OSError as exc:
        raise TryDirError(f"cannot write {meta}: {exc}") from exc
    try:
        _make_read_only(source_dir)
    except OSError as exc:
        raise TryDirError(f"cannot make source/ read-only: {exc}") from exc
    return source_dir, listing


def _new_try(attempt_dir: Path) -> Path:
    """A fresh, numbered try directory; never reuses or overwrites one."""
    tries = attempt_dir / "tries"
    try:
        tries.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name) for p in tries.iterdir() if p.name.isdigit()]
        path = tries / f"{max(existing, default=0) + 1:03d}"
        path.mkdir()
    except OSError as exc:
        raise TryDirError(f"cannot create a try under {tries}: {exc}") from exc
    return path


def _write(try_dir: Path, section: ReproductionSection) -> ReproductionSection:
    _write_text(try_dir / "manifest.json", section.model_dump_json(indent=2))
    return section


def _write_text(path: Path, text: str) -> None:
    """``path.write_text(text)``, an ``OSError`` raised as a :class:`TryDirError`.

    Every write here happens after the try directory already exists (the
    manifest, ``build.std{out,err}``, ``check.std{out,err}``); a disk-full or
    permission failure at that point must exit 2 like the other §6
    try-directory failures, not surface as an uncaught traceback.
    """
    try:
        path.write_text(text)
    except OSError as exc:
        raise TryDirError(f"cannot write {path}: {exc}") from exc


def _make_read_only(root: Path) -> None:
    """Strip write permission from every file and directory under ``root``.

    Applied once to a freshly extracted ``source/`` (spec §1.5: "restored
    tree, read-only after restoration, shared by tries"). Files first, then
    directories bottom-up — a directory loses its own write bit only after
    every entry inside it has already been chmod'd, and ``root`` itself is
    covered as the last directory ``os.walk`` visits in that bottom-up pass.
    Symlinks are skipped: ``os.chmod`` would follow a symlink to a file (or
    fail outright on some platforms) rather than change the link itself.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if not path.is_symlink():
                os.chmod(path, path.stat().st_mode & ~0o222)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if not path.is_symlink():
            os.chmod(path, path.stat().st_mode & ~0o222)


def _make_writable(root: Path) -> None:
    """Restore owner write on every file and directory under ``root``.

    ``shutil.copytree`` copies ``source/``'s (now read-only) permission bits
    onto ``context/`` along with everything else; the build receives
    ``source/`` "as is" (spec §1.5) but still needs a writable copy, so write
    permission is added back right after the copy. Symlinks are skipped, as
    in :func:`_make_read_only`.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if not path.is_symlink():
                os.chmod(path, path.stat().st_mode | 0o200)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if not path.is_symlink():
            os.chmod(path, path.stat().st_mode | 0o200)


def _relative_argv(argv: list[str], try_dir: Path) -> list[str]:
    """The recorded argv with any path under ``try_dir`` made relative to it.

    The build still runs with the absolute paths in ``argv``; only the copy
    stored in the manifest changes (spec §6: artifact-naming fields such as
    ``build.argv``'s paths are try-dir-relative, e.g. ``context/Dockerfile``,
    never the host's absolute layout).
    """
    prefix = str(try_dir) + os.sep
    return [
        token[len(prefix) :] if token.startswith(prefix) else token for token in argv
    ]


def _last_error(text: str) -> str | None:
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip().startswith(("ERROR: ", "Error: "))
    ]
    return lines[-1] if lines else None
