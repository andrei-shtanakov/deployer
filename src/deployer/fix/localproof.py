"""The local proof (design §6): rebuild the corrected Dockerfile through R's
adapter and read the closed local "passed" templates.

Order: backend (must equal R's recorded one, and be Podman) → endpoint
(R's :func:`~deployer.reproduce.endpoint.confirm_local`) → a fix-dir
``context/`` copied from R's ``source/`` with the corrected bytes written at
the bound Dockerfile path → R's offline checks, fresh, on ``source/``
(original bytes) and on the context (corrected bytes) → the defect check and
the no-regression rule → only then a build, tagged
``localhost/deployer-fix-<fix_id>`` → cleanup exactly as R records it →
``templates.match_local``.

``source/`` is only ever read. The function is total: every failure is a
not-ok :class:`LocalResult` whose ``reason`` starts with
``no local confirmation:``; it never raises.
"""

import hashlib
import os
import posixpath
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deployer.admission.model import DefectClass
from deployer.fix import regress, templates
from deployer.fix.binding import Bound
from deployer.fix.document import LocalProof
from deployer.models import ContainerRuntime
from deployer.reproduce import build as build_mod
from deployer.reproduce import dockerfile, endpoint, ignore
from deployer.reproduce.buildline import BuildConfig
from deployer.reproduce.detail import RecordRun, copy_source_records, syntax_records
from deployer.reproduce.model import ReproductionSection

# TODO: _make_writable, _relative_argv and _write_text should become public in
# reproduce.run; they are imported here, not copied.
from deployer.reproduce.run import _make_writable, _relative_argv, _write_text
from deployer.reproduce.shape import Refusal
from deployer.runtime import probe_runtime_versions

NO_LOCAL = "no local confirmation"
STDOUT_FILE = "build.stdout"
STDERR_FILE = "build.stderr"
CLAIM = "the diagnosed source error is removed locally"
_KINDS: dict[str, templates.Kind] = {
    "missing_copy_source": "copy",
    "from_argument_count": "from",
}


@dataclass(frozen=True)
class LocalResult:
    """Whether the corrected Dockerfile is confirmed locally, why not, and
    the evidence recorded either way."""

    ok: bool
    reason: str | None
    proof: LocalProof


@dataclass
class _Draft:
    """The proof as far as it got; turned into a :class:`LocalProof` at the end."""

    dockerfile_sha256: str
    backend: str
    build: dict[str, Any]
    versions: dict[str, Any] = field(default_factory=dict)
    records_before: list[dict[str, Any]] = field(default_factory=list)
    records_after: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    later_failure: dict[str, Any] | None = None

    def result(self, reason: str | None) -> LocalResult:
        """The final result: ok iff ``reason`` is ``None``."""
        proof = LocalProof(
            dockerfile_sha256=self.dockerfile_sha256,
            build=self.build,
            backend=self.backend,
            versions=self.versions,
            records_before=self.records_before,
            records_after=self.records_after,
            evidence=self.evidence,
            later_failure=self.later_failure,
        )
        full = None if reason is None else f"{NO_LOCAL}: {reason}"
        return LocalResult(ok=reason is None, reason=full, proof=proof)


def fix_tag(fix_id: str) -> str:
    """The fix build's own tag, unique per fix directory; CI's ``-t`` is
    never used and ``<run_id>-<seq>`` would repeat across attempts."""
    return f"localhost/deployer-fix-{fix_id}"


def build_config(stored: Mapping[str, Any]) -> BuildConfig | str:
    """R's bound ``BuildConfig`` from ``FixDocument.input.build``: its
    ``dockerfile``, ``build_args`` (``[name, value]`` pairs, as pydantic
    dumps the dataclass) and ``platform``; the tag is dropped. A context
    other than ``.``, a Dockerfile path that is absolute or leaves the
    context, or any malformed field returns the reason."""
    name = stored.get("dockerfile")
    platform = stored.get("platform")
    raw_args = stored.get("build_args", [])
    if stored.get("context", ".") != ".":
        return f"stored build context {stored.get('context')!r} is not '.'"
    if not isinstance(name, str) or not name:
        return "stored build config has no dockerfile"
    path_reason = _dockerfile_reason(name)
    if path_reason is not None:
        return path_reason
    if platform is not None and not isinstance(platform, str):
        return "stored build config platform is not a string"
    if not isinstance(raw_args, list | tuple):
        return "stored build config build_args is not a list"
    pairs: list[tuple[str, str]] = []
    for pair in raw_args:
        if (
            not isinstance(pair, list | tuple)
            or len(pair) != 2
            or not all(isinstance(part, str) for part in pair)
        ):
            return f"stored build arg {pair!r} is not a [name, value] pair"
        pairs.append((pair[0], pair[1]))
    return BuildConfig(
        dockerfile=name, build_args=tuple(pairs), platform=platform, tag=None
    )


def local_proof(
    section: ReproductionSection,
    source_dir: Path,
    fix_dir: Path,
    corrected: bytes,
    bound: Bound,
    cls: DefectClass,
    replaced_position: int | None,
    new_source: str | None,
    rt: ContainerRuntime,
    env: Mapping[str, str],
    build: BuildConfig,
    fix_id: str,
    build_timeout: int,
) -> LocalResult:
    """Confirm ``corrected`` locally (design §6); never raises.

    ``replaced_position`` is ``Candidates.position`` for
    ``missing_copy_source`` and ``None`` for ``from_argument_count``;
    ``new_source`` is the chosen replacement source (``None`` for FROM).
    """
    draft = _Draft(
        dockerfile_sha256=hashlib.sha256(corrected).hexdigest(),
        backend=rt.tool,
        build=_config_record(build, fix_tag(fix_id)),
    )
    try:
        reason = _prove(
            draft,
            section,
            source_dir,
            fix_dir,
            corrected,
            bound,
            cls,
            replaced_position,
            new_source,
            rt,
            env,
            build,
            fix_id,
            build_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — totality: never raise
        reason = f"{exc.__class__.__name__}: {exc}"
    try:
        return draft.result(reason)
    except Exception as exc:  # noqa: BLE001 — a draft the model refuses
        fallback = _Draft(draft.dockerfile_sha256, draft.backend, {})
        return fallback.result(f"cannot record the proof: {exc.__class__.__name__}")


def _prove(
    draft: _Draft,
    section: ReproductionSection,
    source_dir: Path,
    fix_dir: Path,
    corrected: bytes,
    bound: Bound,
    cls: DefectClass,
    replaced_position: int | None,
    new_source: str | None,
    rt: ContainerRuntime,
    env: Mapping[str, str],
    build: BuildConfig,
    fix_id: str,
    build_timeout: int,
) -> str | None:
    """The steps of :func:`local_proof`; the not-ok reason or ``None``."""
    kind = _KINDS.get(cls)
    if kind is None:
        return f"unrecognised defect class {cls!r}"
    backend_reason = _backend_reason(section, rt)
    if backend_reason is not None:
        return backend_reason
    confirmed = endpoint.confirm_local(rt, env)
    if isinstance(confirmed, Refusal):
        return confirmed.reason
    draft.build.update(endpoint=confirmed.uri, endpoint_source=confirmed.source)
    path_reason = _dockerfile_reason(build.dockerfile)
    if path_reason is not None:
        return path_reason
    before = _records(source_dir, build.dockerfile)
    draft.records_before = [_run_record(run) for run in before]
    context = fix_dir / "context"
    shutil.copytree(source_dir, context, symlinks=True)
    _make_writable(context)
    write_reason = write_no_follow(context, build.dockerfile, corrected)
    if write_reason is not None:
        return write_reason
    draft.build.update(
        ci_ignore_file=ignore.ci_ignore_file(context, build.dockerfile),
        local_ignore_file=ignore.local_ignore_file(context, build.dockerfile, rt.tool),
    )
    after = _records(context, build.dockerfile)
    draft.records_after = [_run_record(run) for run in after]
    offline = _offline_reason(
        before, after, cls, bound.ordinal, replaced_position, new_source
    )
    if offline is not None:
        return offline
    draft.versions = probe_runtime_versions(rt).model_dump()
    return _build_and_match(
        draft,
        rt,
        context,
        fix_dir,
        build,
        fix_tag(fix_id),
        build_timeout,
        kind,
        _corrected_text(corrected, bound),
    )


def _backend_reason(section: ReproductionSection, rt: ContainerRuntime) -> str | None:
    """The local templates are Podman-only and must match R's backend."""
    if section.environment is None:
        return "R recorded no environment"
    if rt.tool != section.environment.backend or rt.tool != "podman":
        return "local backend differs from R's"
    return None


def _dockerfile_reason(name: str) -> str | None:
    """A Dockerfile path must stay inside the context (as ``buildline`` binds
    it): not absolute, no ``..`` component."""
    if name.startswith("/") or ".." in posixpath.normpath(name).split("/"):
        return f"dockerfile path {name!r} leaves the context"
    return None


def write_no_follow(
    context: Path, name: str, data: bytes, mode: int | None = None
) -> str | None:
    """Write ``data`` at ``context/<name>`` without following a link; the
    reason it could not, or ``None``.

    Every ancestor under ``context`` must be a real directory and the target
    an existing regular file (an lstat walk); the target is unlinked and
    recreated with ``O_CREAT|O_EXCL|O_NOFOLLOW``, so no write ever lands
    outside. The new file gets ``mode`` (permission bits), by default the
    original file's own, set explicitly so the umask cannot change it: an
    executable Dockerfile stays executable."""
    parts = posixpath.normpath(name).split("/")
    current = context
    for part in parts[:-1]:
        current = current / part
        try:
            found = os.lstat(current).st_mode
        except FileNotFoundError:
            return f"dockerfile ancestor {part!r} is absent from the context"
        if stat.S_ISLNK(found) or not stat.S_ISDIR(found):
            return f"dockerfile ancestor {part!r} is not a real directory"
    target = current / parts[-1]
    try:
        found = os.lstat(target).st_mode
    except FileNotFoundError:
        return f"dockerfile {name!r} is absent from the context"
    if not stat.S_ISREG(found):
        return f"dockerfile {name!r} is not a regular file"
    permissions = stat.S_IMODE(found) if mode is None else mode
    target.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), permissions)
        handle.write(data)
    return None


def _records(root: Path, name: str) -> list[RecordRun]:
    """R's offline checks as detailed records over ``root``, read as R reads
    the Dockerfile, with the CI ignore rules (as ``run.py`` does)."""
    path = root / name
    parsed = dockerfile.parse(
        path.read_text(errors="replace") if path.is_file() else ""
    )
    rules = ignore.load_rules(root, ignore.ci_ignore_file(root, name))
    return [
        copy_source_records(parsed, root, name, rules),
        *syntax_records(parsed, name),
    ]


def _offline_reason(
    before: list[RecordRun],
    after: list[RecordRun],
    cls: DefectClass,
    ordinal: int,
    replaced_position: int | None,
    new_source: str | None,
) -> str | None:
    """The defect check, then the no-regression rule (§6.2)."""
    defect = regress.defect_check_passes(
        after, cls, ordinal, replaced_position, new_source
    )
    if defect is not None:
        return f"defect check: {defect}"
    lines = regress.regressions(before, after, ordinal, replaced_position, new_source)
    if lines:
        return f"regression: {'; '.join(lines)}"
    return None


def _build_and_match(
    draft: _Draft,
    rt: ContainerRuntime,
    context: Path,
    fix_dir: Path,
    build: BuildConfig,
    tag: str,
    timeout: int,
    kind: templates.Kind,
    corrected_text: str,
) -> str | None:
    """Build, record the build and its cleanup as R does, read the template."""
    run = build_mod.run_build(rt, context, build, tag, timeout)
    draft.build.update(
        argv=_relative_argv(run.argv, fix_dir),
        exit_code=run.exit_code,
        launch_error=run.launch_error,
        image_cleanup=build_mod.cleanup_image(rt, tag, built=run.exit_code == 0),
        build_containers=build_mod.build_containers_state(
            rt, finished=run.launch_error is None
        ),
    )
    _write_text(fix_dir / STDOUT_FILE, run.stdout)
    _write_text(fix_dir / STDERR_FILE, run.stderr)
    draft.build.update(stdout=STDOUT_FILE, stderr=STDERR_FILE)
    if run.launch_error == "timeout":
        return "the build timed out"
    if run.launch_error is not None:
        return f"the build did not run: {run.launch_error}"
    outcome = templates.match_local(kind, corrected_text, run.stdout, run.stderr)
    draft.evidence.append(_evidence(kind, corrected_text, outcome))
    if outcome.evidence != "passed":
        return outcome.detail or outcome.evidence
    if run.exit_code != 0:
        draft.later_failure = _later_failure(kind, run.exit_code, outcome)
    return None


def _evidence(
    kind: templates.Kind, corrected_text: str, outcome: templates.Outcome
) -> dict[str, Any]:
    """The template outcome, its evidence file and 1-based line numbers."""
    stream = (
        STDERR_FILE
        if outcome.detail is not None and outcome.detail.startswith("stderr")
        else STDOUT_FILE
    )
    return {
        "template": f"local/podman/{kind}",
        "corrected_text": corrected_text,
        "evidence": outcome.evidence,
        "detail": outcome.detail,
        "file": stream,
        "lines": list(outcome.lines),
        "image_pull": outcome.image_pull,
    }


def _later_failure(
    kind: templates.Kind, exit_code: int | None, outcome: templates.Outcome
) -> dict[str, Any]:
    """A failure after the proven boundary (§6.4): whether it is the same
    FROM's image pull, another instruction, or (FROM with no bound pull
    line) not attributable either way."""
    if outcome.image_pull is not None:
        what = "image_pull_of_the_same_from"
    elif kind == "copy":
        what = "another_instruction"
    else:
        what = "unattributed"
    return {
        "exit_code": exit_code,
        "what": what,
        "image_pull": outcome.image_pull,
        "claim": CLAIM,
    }


def _corrected_text(corrected: bytes, bound: Bound) -> str:
    """The corrected instruction's source text at the bound lines, stripped
    (a one-token or F1/F2 edit keeps the line count)."""
    first, last = bound.lines
    lines = corrected.splitlines(keepends=True)[first - 1 : last]
    return b"".join(lines).decode("utf-8", errors="replace").strip()


def _config_record(build: BuildConfig, tag: str) -> dict[str, Any]:
    """The build configuration fixed in the evidence (§6.1, §6.5)."""
    return {
        "dockerfile": build.dockerfile,
        "context": ".",
        "build_args": [list(pair) for pair in build.build_args],
        "platform": build.platform,
        "tag": tag,
    }


def _run_record(run: RecordRun) -> dict[str, Any]:
    """One detailed check run as JSON-ready data."""
    return {
        "check_id": run.check_id,
        "file_status": run.file_status,
        "file_reason": run.file_reason,
        "records": [
            {
                "ordinal": record.ordinal,
                "lines": None if record.lines is None else list(record.lines),
                "subject": record.subject,
                "status": record.status,
                "reason": record.reason,
            }
            for record in run.records
        ],
    }
