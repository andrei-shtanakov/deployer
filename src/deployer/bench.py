"""Corpus loading, offline fixture author, and bench orchestration."""

import fnmatch
import hashlib
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from deployer.author import (
    DockerfileAuthor,
    _deployer_git_sha,
    _deployer_version,
    author_dockerfile,
)
from deployer.facts import analyze_project
from deployer.models import (
    AuthorInfo,
    BenchCaseResult,
    BenchReport,
    ContainerRuntime,
    DeployTarget,
    ExpectedOutcome,
    ExternalTarget,
    ProjectFacts,
    VerificationReport,
)
from deployer.runtime import probe_runtime_versions
from deployer.verify import (
    CONTEXT_IGNORE,
    DEFAULT_BUILD_TIMEOUT,
    DEFAULT_HEALTH_TIMEOUT,
    verify,
)


class FixtureAuthor:
    """Offline DockerfileAuthor replaying a case's known-good Dockerfile.

    generate() and repair() both return the fixture verbatim: the bench's
    offline mode measures the verification pipeline, not authoring skill,
    so there is nothing to "repair" — a failing fixture is corpus rot.
    """

    def __init__(self, dockerfile: str) -> None:
        self._dockerfile = dockerfile

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        return self._dockerfile

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        return self._dockerfile

    def info(self) -> AuthorInfo:
        """Comparability metadata: fixture hash stands in for a prompt hash."""
        return AuthorInfo(
            backend="fixture",
            prompt_sha256=hashlib.sha256(self._dockerfile.encode()).hexdigest(),
        )


class BenchCase(BaseModel):
    """One corpus case: a target project plus intent and expectations."""

    name: str
    project_dir: Path
    target: DeployTarget = Field(default_factory=DeployTarget)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    fixture_dockerfile: Path | None = None
    external_url: str | None = None
    external_commit: str | None = None


def load_corpus(corpus_root: Path, pattern: str = "*") -> list[BenchCase]:
    """Load synthetic corpus cases whose directory name matches `pattern`."""
    synthetic = corpus_root / "synthetic"
    if not synthetic.is_dir():
        raise FileNotFoundError(f"no synthetic corpus at {synthetic}")
    cases: list[BenchCase] = []
    for case_dir in sorted(p for p in synthetic.iterdir() if p.is_dir()):
        if not fnmatch.fnmatch(case_dir.name, pattern):
            continue
        project_dir = case_dir / "project"
        if not project_dir.is_dir():
            raise ValueError(f"corpus case {case_dir.name} has no project/ dir")
        target_file = case_dir / "target.json"
        target = (
            DeployTarget.model_validate_json(target_file.read_text())
            if target_file.is_file()
            else DeployTarget()
        )
        expected_file = case_dir / "expected.json"
        expected = (
            ExpectedOutcome.model_validate_json(expected_file.read_text())
            if expected_file.is_file()
            else ExpectedOutcome()
        )
        fixture = case_dir / "fixture.Dockerfile"
        cases.append(
            BenchCase(
                name=case_dir.name,
                project_dir=project_dir,
                target=target,
                expected=expected,
                fixture_dockerfile=fixture if fixture.is_file() else None,
            )
        )
    return cases


def load_external(corpus_root: Path) -> list[ExternalTarget]:
    """Parse corpus/external.toml; a missing file means no external targets."""
    manifest = corpus_root / "external.toml"
    if not manifest.is_file():
        return []
    data = tomllib.loads(manifest.read_text())
    return [ExternalTarget.model_validate(t) for t in data.get("targets", [])]


def clone_external(ext: ExternalTarget, dest_root: Path) -> BenchCase:
    """Clone an external target at its pinned commit into dest_root/<name>."""
    dest = dest_root / ext.name
    dest.mkdir(parents=True, exist_ok=True)
    commands = [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", ext.url],
        ["git", "fetch", "-q", "--depth", "1", "origin", ext.commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ]
    for command in commands:
        proc = subprocess.run(
            command, cwd=dest, capture_output=True, text=True, timeout=300
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"cloning external target {ext.name} failed at "
                f"{' '.join(command)}: {proc.stderr.strip()}"
            )
    return BenchCase(
        name=ext.name,
        project_dir=dest,
        target=ext.target,
        expected=ext.expected,
        fixture_dockerfile=None,
        external_url=ext.url,
        external_commit=ext.commit,
    )


def run_case(
    case: BenchCase,
    author: DockerfileAuthor | None,
    runtime: ContainerRuntime | None,
    case_out_dir: Path,
    *,
    build_timeout: int,
    health_timeout: int,
) -> BenchCaseResult:
    """Author one corpus case in a scratch copy; never mutates the corpus."""
    if case.expected.requires_l2 and runtime is None:
        return BenchCaseResult(
            case=case.name,
            outcome="skipped",
            skip_reason="case requires L2 but no container runtime resolved",
        )
    if author is None:
        return BenchCaseResult(
            case=case.name,
            outcome="skipped",
            skip_reason="no fixture.Dockerfile for the offline fixture author",
        )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"deployer-bench-{case.name}-") as tmp:
        scratch = Path(tmp) / "project"
        shutil.copytree(
            case.project_dir,
            scratch,
            symlinks=True,
            ignore=shutil.ignore_patterns(*CONTEXT_IGNORE),
        )
        run = author_dockerfile(
            scratch,
            case.target,
            author,
            max_iterations=case.expected.max_iterations,
            runtime=runtime,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
    wall = time.monotonic() - started
    case_out_dir.mkdir(parents=True, exist_ok=True)
    (case_out_dir / "authoring-run.json").write_text(run.model_dump_json(indent=2))
    last = run.iterations[-1] if run.iterations else None
    if last is not None:
        (case_out_dir / "Dockerfile").write_text(last.dockerfile + "\n")
    failure_kinds = sorted(
        {
            r.failure_kind
            for it in run.iterations
            for r in it.report.results
            if r.failure_kind is not None
        }
    )
    achieved_level = run.success or (
        not case.expected.requires_l2
        and run.stopped_reason == "static_only"
        and bool(run.iterations)
        and run.iterations[-1].report.passed
    )
    matched = achieved_level == case.expected.expected_success
    if (
        matched
        and not case.expected.expected_success
        and case.expected.expected_failure_kind is not None
    ):
        matched = case.expected.expected_failure_kind in failure_kinds
    return BenchCaseResult(
        case=case.name,
        outcome="matched" if matched else "mismatched",
        success=achieved_level,
        stopped_reason=run.stopped_reason,
        iterations=len(run.iterations),
        image_size_bytes=last.report.image_size_bytes if last else None,
        wall_time_s=round(wall, 3),
        failure_kinds=failure_kinds,
        external_url=case.external_url,
        external_commit=case.external_commit,
    )


def _corpus_commit() -> str | None:
    """Deployer repo sha, '-dirty'-suffixed when the working tree has changes."""
    sha = _deployer_git_sha()
    if sha is None:
        return None
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(Path(__file__).resolve().parent),
                "status",
                "--porcelain",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return sha
    if proc.returncode == 0 and proc.stdout.strip():
        return f"{sha}-dirty"
    return sha


def _run_bench_cases(
    cases: list[BenchCase],
    make_author: Callable[[BenchCase], DockerfileAuthor | None],
    runtime: ContainerRuntime | None,
    *,
    label: str,
    author_backend: str,
    run_dir: Path,
    build_timeout: int,
    health_timeout: int,
) -> BenchReport:
    """Run the authoring loop over `cases` and write the aggregated report."""
    results = [
        run_case(
            case,
            make_author(case),
            runtime,
            run_dir / "cases" / case.name,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        for case in cases
    ]
    report = BenchReport(
        label=label,
        author_backend=author_backend,
        corpus_commit=_corpus_commit(),
        deployer_version=_deployer_version(),
        runtime=runtime,
        runtime_versions=(
            probe_runtime_versions(runtime) if runtime is not None else None
        ),
        build_timeout_s=build_timeout,
        health_timeout_s=health_timeout,
        cases=results,
    )
    (run_dir / "bench-report.json").write_text(report.model_dump_json(indent=2))
    (run_dir / "bench-report.md").write_text(render_markdown(report))
    return report


def _create_run_dir(runs_root: Path, stamp: str, label: str) -> Path:
    """Create `<runs_root>/<stamp>-<label>`, retrying on same-second collisions.

    `mkdir(exist_ok=False)` raises `FileExistsError` when two runs land in
    the same wall-clock second (the stamp has no microseconds, kept that
    way for readability). Retry with a `-2`, `-3`, ... suffix instead of
    failing the whole bench run.
    """
    base = runs_root / f"{stamp}-{label}"
    for suffix in range(1, 101):
        candidate = base if suffix == 1 else runs_root / f"{stamp}-{label}-{suffix}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(
        f"could not create a unique run dir under {runs_root} for "
        f"{stamp}-{label} after 100 attempts"
    )


def run_bench(
    corpus_root: Path,
    make_author: Callable[[BenchCase], DockerfileAuthor | None],
    runtime: ContainerRuntime | None,
    *,
    label: str,
    author_backend: str,
    pattern: str = "*",
    runs_root: Path = Path(".deployer-runs"),
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
    include_external: bool = False,
) -> tuple[BenchReport, Path]:
    """Run the authoring loop over every matching corpus case and aggregate."""
    cases = load_corpus(corpus_root, pattern)
    if not cases:
        raise ValueError(f"no corpus cases match pattern {pattern!r}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = _create_run_dir(runs_root, stamp, label)
    if not include_external:
        report = _run_bench_cases(
            cases,
            make_author,
            runtime,
            label=label,
            author_backend=author_backend,
            run_dir=run_dir,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        return report, run_dir
    with tempfile.TemporaryDirectory(prefix="deployer-external-") as ext_tmp:
        cases = cases + [
            clone_external(ext, Path(ext_tmp)) for ext in load_external(corpus_root)
        ]
        report = _run_bench_cases(
            cases,
            make_author,
            runtime,
            label=label,
            author_backend=author_backend,
            run_dir=run_dir,
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        return report, run_dir


def verify_corpus(
    corpus_root: Path,
    runtime: ContainerRuntime | None,
    *,
    pattern: str = "*",
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> list[tuple[str, VerificationReport]]:
    """Verify each case's committed fixture.Dockerfile. No LLM, no authoring."""
    cases = load_corpus(corpus_root, pattern)
    if not cases:
        raise ValueError(f"no corpus cases match pattern {pattern!r}")
    results: list[tuple[str, VerificationReport]] = []
    for case in cases:
        if case.fixture_dockerfile is None:
            raise ValueError(f"corpus case {case.name} has no fixture.Dockerfile")
        report = verify(
            case.fixture_dockerfile.read_text(),
            case.project_dir,
            case.target,
            runtime,
            analyze_project(case.project_dir),
            build_timeout=build_timeout,
            health_timeout=health_timeout,
        )
        results.append((case.name, report))
    return results


def render_markdown(report: BenchReport) -> str:
    """Human-readable summary table for one bench run."""
    if report.runtime is None:
        runtime_line = "static-only"
    elif report.runtime.host:
        runtime_line = f"{report.runtime.tool} @ {report.runtime.host}"
    else:
        runtime_line = f"{report.runtime.tool} (local)"
    rate = report.success_rate
    lines = [
        f"# Bench run: {report.label}",
        "",
        f"- author: {report.author_backend}",
        f"- corpus commit: {report.corpus_commit or 'unknown'}",
        f"- deployer: {report.deployer_version or 'unknown'}",
        f"- runtime: {runtime_line}",
        f"- timeouts: build {report.build_timeout_s}s / health {report.health_timeout_s}s",
        f"- success rate: {rate if rate is not None else 'n/a'}",
        "",
        "| case | outcome | stop reason | iters | image MB | wall s | failure kinds |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for c in report.cases:
        size = f"{c.image_size_bytes / 1e6:.1f}" if c.image_size_bytes else "-"
        kinds = ", ".join(k.value for k in c.failure_kinds) or "-"
        lines.append(
            f"| {c.case} | {c.outcome} | {c.stopped_reason or '-'} "
            f"| {c.iterations} | {size} | {c.wall_time_s:.1f} | {kinds} |"
        )
    return "\n".join(lines) + "\n"
