"""The preparation layer (A §6.1): I/O over R's try, no network, no builds.

Gathers the :class:`VerifiedFacts` the pure :func:`decide` reads, from what
R left for the same attempt and nothing else:

- the tree restored at ``head_sha`` (``<attempt>/source``) — ownership
  (A §2.4), the artifact's bytes and the ignore files' content hashes (A
  §4.3), all read through the no-follow reader;
- R's ``source.json`` — the ``head_sha`` listing, passed through as R stored
  it, with its completeness (a truncated listing is not replaced; the
  decision refuses absence over it);
- R's reproduction section — the job it compared, the effective ignore paths
  it compared (``values["ignore_file"]``), the local backend's ``# syntax=``
  directive and the build output files;
- and it writes the CI job text R compared to ``ci.log`` in the try
  directory, the file the CI side's evidence references (R §6 path bases).

A failure to read R's own records or to write ``ci.log`` is a try-directory
failure and raises :class:`TryDirError`, like R's (exit 2, R §6). A tree file
that cannot be read is a value not obtained (A §1): absent, never invented.
"""

import hashlib
import io
import json
from collections.abc import Mapping
from pathlib import Path

from deployer.admission.decide import Hashes, VerifiedFacts
from deployer.admission.fsread import read_in_tree
from deployer.admission.model import Binding
from deployer.admission.ownership import verify_ownership
from deployer.forge import FailedJob, FailedRun
from deployer.provenance.model import TreeRow
from deployer.provenance.trust import trust_dir
from deployer.reproduce import dockerfile, shape
from deployer.reproduce.model import ReproductionSection
from deployer.reproduce.run import TryDirError, _write_text

FRONTEND_ARG = "BUILDKIT_SYNTAX"
"""The build arg that switches BuildKit to another frontend (a dialect)."""
BUILD_ARG_FLAG = "--build-arg"
CI_LOG = "ci.log"
"""The CI job text's file in the try directory, next to ``build.stdout``."""


def prepare(
    snapshot: FailedRun,
    section: ReproductionSection,
    root: Path,
    env: Mapping[str, str],
) -> VerifiedFacts:
    """The verified facts of an attempted reproduction ``section`` of
    ``snapshot``, whose paths are relative to ``root`` (R's working root).

    Raises :class:`TryDirError` when R's records cannot be read or ``ci.log``
    cannot be written; ``ValueError`` when ``section`` carries no try
    directory or binding (it was not attempted: no admission is prepared).
    """
    if section.try_dir is None or section.binding is None:
        raise ValueError("no admission without an attempted reproduction")
    try_dir = root / section.try_dir
    attempt_dir = try_dir.parent.parent
    source_dir = attempt_dir / "source"
    artifact_path = section.binding.dockerfile
    job = _job(snapshot, section.binding.job_id)
    ci_text = shape.job_text(job)
    _write_text(try_dir / CI_LOG, ci_text)
    artifact = _tree_bytes(source_dir, artifact_path)
    stdout, stderr = _build_output(try_dir, section)
    listing, complete = _head_listing(attempt_dir, snapshot.head_sha)
    environment = section.environment
    return VerifiedFacts(
        binding=Binding(
            repo=snapshot.repo,
            head_sha=snapshot.head_sha,
            artifact_path=artifact_path,
            artifact_sha256=_sha256(artifact),
        ),
        ownership=verify_ownership(
            source_dir,
            repo=snapshot.repo,
            artifact_path=artifact_path,
            trust=trust_dir(env),
            checked_roots=(source_dir, root),
        ),
        reproduction=section,
        parsed=dockerfile.parse(_as_r_reads(artifact or b"")),
        head_listing=listing,
        head_listing_complete=complete,
        ci_text=ci_text,
        ci_evidence_file=CI_LOG,
        local_stdout=stdout,
        local_stderr=stderr,
        ignore_hashes=_ignore_hashes(source_dir, section),
        syntax_directive_ci=_ci_frontend(section),
        syntax_directive_local=(
            environment.syntax_directive if environment is not None else None
        ),
    )


def _ci_frontend(section: ReproductionSection) -> str | None:
    """CI's frontend switch: ``BUILDKIT_SYNTAX`` among the build args of CI's
    build line, which R replays into its recorded ``build.argv`` (both
    ``--build-arg K=V`` and ``--build-arg=K=V`` normalise to the first form).
    BuildKit honours it, Podman does not: a dialect R's local side cannot
    see. The Dockerfile's own ``# syntax=`` is ``parsed``'s; the last such
    argument wins, as for any build arg."""
    if section.build is None:
        return None
    argv = section.build.argv
    found: list[str] = []
    for index, token in enumerate(argv):
        if token == BUILD_ARG_FLAG and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith(f"{BUILD_ARG_FLAG}="):
            value = token.removeprefix(f"{BUILD_ARG_FLAG}=")
        else:
            continue
        key, sep, frontend = value.partition("=")
        if key == FRONTEND_ARG and sep:
            found.append(frontend)
    return found[-1] if found else None


def _job(snapshot: FailedRun, job_id: int) -> FailedJob:
    """The job R bound and compared (its ``binding.job_id``)."""
    for job in snapshot.jobs:
        if job.job_id == job_id:
            return job
    raise ValueError(f"job {job_id} of the reproduction is not in the run")


def _tree_bytes(source_dir: Path, rel: str) -> bytes | None:
    """``rel``'s bytes in the restored tree, or ``None`` when the no-follow
    reader refuses it (missing, behind a symlink, not a regular file)."""
    try:
        return read_in_tree(source_dir, rel)
    except (OSError, ValueError):  # ValueError: not encodable for the OS
        return None


def _sha256(data: bytes | None) -> str | None:
    """The hex SHA-256 of ``data``; ``None`` for bytes not obtained."""
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _as_r_reads(data: bytes) -> str:
    """``data`` decoded as R's ``Path.read_text(errors="replace")`` decodes
    it: locale encoding, universal newlines, so line numbers agree with R's."""
    return io.TextIOWrapper(io.BytesIO(data), errors="replace").read()


def _build_output(try_dir: Path, section: ReproductionSection) -> tuple[str, str]:
    """The local build's stdout and stderr exactly as R wrote them."""
    build = section.build
    if build is None:
        return "", ""
    return _read_record(try_dir / build.stdout), _read_record(try_dir / build.stderr)


def _read_record(path: Path) -> str:
    """A text file R wrote (UTF-8, newlines untranslated); unreadable →
    TryDirError."""
    try:
        with path.open(encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise TryDirError(f"cannot read {path}: {exc}") from exc


def _head_listing(attempt_dir: Path, head_sha: str) -> tuple[list[TreeRow], bool]:
    """The ``head_sha`` listing R stored in ``source.json`` for this attempt,
    as stored, and whether it is complete (R's ``truncated`` flag negated)."""
    meta = attempt_dir / "source.json"
    try:
        data = json.loads(meta.read_text())
        if not isinstance(data, dict):
            raise TryDirError(f"cannot read {meta}: not a JSON object")
        if data.get("head_sha") != head_sha:
            raise TryDirError(
                f"{meta} names {data.get('head_sha')}, the run is at {head_sha}"
            )
        listing = data["listing"]
        truncated = listing["truncated"]
        if not isinstance(truncated, bool):
            raise TryDirError(f"cannot read {meta}: truncated is not a boolean")
        return [TreeRow(**entry) for entry in listing["entries"]], not truncated
    except TryDirError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise TryDirError(f"cannot read {meta}: {exc}") from exc


def _ignore_hashes(source_dir: Path, section: ReproductionSection) -> Hashes:
    """``((ci_path, ci_sha256), (local_path, local_sha256))`` over the
    effective ignore paths R compared (A §4.3). A path absent on a side has
    no hash; a present one that cannot be read has none either (A §1)."""
    ci_path, local_path = _ignore_paths(section)
    return (
        (ci_path, _ignore_sha(source_dir, ci_path)),
        (local_path, _ignore_sha(source_dir, local_path)),
    )


def _ignore_paths(section: ReproductionSection) -> tuple[str | None, str | None]:
    """R's ``values["ignore_file"]``: the paths its own rule resolved per
    side (``ignore.ci_ignore_file`` / ``ignore.local_ignore_file``) and
    compared. Without a comparison nothing was compared, and :func:`decide`
    refuses before it reads the hashes."""
    comparison = section.comparison
    if comparison is None:
        return None, None
    return comparison.values.get("ignore_file", (None, None))


def _ignore_sha(source_dir: Path, path: str | None) -> str | None:
    """The content hash of one side's effective ignore file."""
    return _sha256(_tree_bytes(source_dir, path)) if path is not None else None
