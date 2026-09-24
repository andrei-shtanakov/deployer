"""Thin argparse CLI over the deployer library."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from deployer.author import author_dockerfile
from deployer.bench import (
    CloneError,
    FixtureAuthor,
    PromoteRefusedError,
    compare_runs,
    load_baseline,
    promote_run,
    run_bench,
    verify_corpus,
)
from deployer.diagnose import RunDiagnosis, diagnose_run, render_verdict
from deployer.facts import TargetConfigError, analyze_project
from deployer.forge import (
    DEFAULT_MAX_ARCHIVE_MB,
    AdapterRefusal,
    GhError,
    RunRef,
    StepRef,
    SubprocessGh,
    fetch_failed_run,
)
from deployer.llm import AnthropicAuthor
from deployer.models import (
    BenchReport,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    VerificationReport,
    satisfies_declared_smoke,
)
from deployer.provenance import trust
from deployer.reproduce import ReproductionSection, TryDirError, reproduce_run
from deployer.runtime import (
    RuntimeConfigError,
    probe_runtime_versions,
    resolve_runtime,
)
from deployer.verify import DEFAULT_BUILD_TIMEOUT, DEFAULT_HEALTH_TIMEOUT, verify

_LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")

_DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_RUN_URL_RE = re.compile(
    r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)(?:/attempts/(\d+))?"
)

#: A GitHub repository slug. The value is interpolated straight into
#: `repos/{repo}/...` (`forge.py`), so a run URL pasted from an issue or
#: handed over by an agent could otherwise steer `gh api` at a path other
#: than the one the URL appears to name. Neither segment may be made of dots
#: only (`.`, `..`, `...`, ...) — no GitHub owner or repo name is, and
#: `owner/..` or `../..` reads as path traversal against `repos/{repo}/...`.
_REPO_SLUG_RE = re.compile(r"^(?!\.+/)[A-Za-z0-9._-]+/(?!\.+$)[A-Za-z0-9._-]+$")

# `diagnose.py` asserts no cause and never produces `CLASSIFIED`; the key is
# kept so the map covers the `Outcome` literal, and exit 0 is not reachable
# through this command.
_EXIT_BY_OUTCOME: dict[str, int] = {
    "CLASSIFIED": 0,
    "UNCLASSIFIED": 3,
    "EVIDENCE_UNAVAILABLE": 4,
}

_STATUS_ICONS = {
    CheckStatus.PASSED: "ok",
    CheckStatus.FAILED: "FAIL",
    CheckStatus.WARNING: "warn",
    CheckStatus.SKIPPED: "skip",
}


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Narrow KEY=VALUE loader for Anthropic author auth; env always wins.

    Intentionally not a general dotenv: no `export`, no interpolation,
    no escapes, no multiline, and inline `#` comments become part of the
    value. Quotes are stripped only when the whole value is wrapped in
    matching quotes. Runtime env defaults (DEPLOYER_CONTAINER_*) are
    resolved before the author is constructed and never come from this
    file.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not _DOTENV_KEY_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_target(path: str | None) -> DeployTarget | str:
    """Load a DeployTarget JSON file; return an error message on failure."""
    if path is None:
        return DeployTarget()
    try:
        return DeployTarget.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return f"cannot read --target file: {exc}"
    except ValidationError as exc:
        return f"--target is not a valid DeployTarget: {exc}"


def _resolve_smoke_suite(target: DeployTarget, target_path: str | None) -> Path | None:
    """Absolute path of the ATP suite, resolved against the target document.

    Resolving against the cwd would make a target non-portable, and against
    the project directory would put a test suite inside the build context.
    """
    if target.smoke is None:
        return None
    if target_path is None:
        raise ValueError(
            "a smoke intent requires --target: the suite path is resolved "
            "relative to the target file"
        )
    return (Path(target_path).parent / target.smoke.suite).resolve()


def _add_timeout_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--build-timeout",
        type=int,
        default=DEFAULT_BUILD_TIMEOUT,
        help="seconds allowed for the container build",
    )
    parser.add_argument(
        "--health-timeout",
        type=int,
        default=DEFAULT_HEALTH_TIMEOUT,
        help=(
            "seconds allowed for runtime checks (service healthcheck or "
            "run intent); ignored for build-only targets"
        ),
    )


def _timeout_error(args: argparse.Namespace) -> str | None:
    if args.build_timeout < 1:
        return "--build-timeout must be >= 1"
    if args.health_timeout < 1:
        return "--health-timeout must be >= 1"
    return None


def _diagnose_flag_error(args: argparse.Namespace) -> str | None:
    """``diagnose`` has no ``--health-timeout``; its own flags checked here."""
    if args.build_timeout < 1:
        return "--build-timeout must be >= 1"
    if args.max_archive_mb < 1:
        return "--max-archive-mb must be >= 1"
    return None


def _add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--container-tool",
        choices=("docker", "podman"),
        default=None,
        help="container CLI to use (default: DEPLOYER_CONTAINER_TOOL or detection)",
    )
    parser.add_argument(
        "--container-host",
        default=None,
        metavar="ssh://user@host",
        help="remote engine over SSH (default: DEPLOYER_CONTAINER_HOST or local)",
    )


def _resolve_runtime_or_error(
    args: argparse.Namespace,
) -> ContainerRuntime | None | str:
    """Resolve the runtime from CLI flags; return an error string on failure."""
    try:
        return resolve_runtime(args.container_tool, args.container_host)
    except RuntimeConfigError as exc:
        return f"{exc}"


def _write_report(project: Path, name: str, payload: str) -> Path | None:
    """Persist a report under <project>/.deployer; warn instead of crashing."""
    report_dir = project / ".deployer"
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / name
        path.write_text(payload)
    except OSError as exc:
        print(f"warning: could not write {name}: {exc}", file=sys.stderr)
        return None
    return path


def _print_report(report: VerificationReport) -> None:
    for result in report.results:
        icon = _STATUS_ICONS[result.status]
        line = f"[{icon:>4}] {result.check_id}"
        if result.message:
            first, *rest = result.message.splitlines()
            line += f": {first}"
            if result.status is CheckStatus.FAILED:
                line += "".join(f"\n       {tail}" if tail else "\n" for tail in rest)
        print(line)
    if not report.docker_available:
        print("note: no container runtime found; static-only verification")


def _cmd_verify(args: argparse.Namespace) -> int:
    project = Path(args.path)
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    target = _load_target(args.target)
    if isinstance(target, str):
        print(f"error: {target}", file=sys.stderr)
        return 2
    try:
        smoke_suite = _resolve_smoke_suite(target, args.target)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dockerfile_path = project / "Dockerfile"
    if not dockerfile_path.is_file():
        print(f"error: {dockerfile_path} not found", file=sys.stderr)
        return 1
    runtime = _resolve_runtime_or_error(args)
    if isinstance(runtime, str):
        print(f"error: {runtime}", file=sys.stderr)
        return 2
    # artifacts are consulted only when the target requests them: a plain
    # target must not depend on the presence/readability of these files
    compose: str | None = None
    if target.dependencies:
        compose_path = project / "compose.yaml"
        compose = compose_path.read_text() if compose_path.is_file() else None
    ci: str | None = None
    if target.ci is not None:
        ci_path = project / ".github" / "workflows" / "ci.yml"
        ci = ci_path.read_text() if ci_path.is_file() else None
    try:
        report = verify(
            dockerfile_path.read_text(),
            project,
            target,
            runtime,
            analyze_project(project),
            compose=compose,
            ci=ci,
            build_timeout=args.build_timeout,
            health_timeout=args.health_timeout,
            smoke_suite=smoke_suite,
        )
    except TargetConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if runtime is not None:
        report.runtime_versions = probe_runtime_versions(runtime)
    _print_report(report)
    report_path = _write_report(
        project, "verify-report.json", report.model_dump_json(indent=2)
    )
    if report_path is not None:
        print(f"report: {report_path}")
    return 0 if report.passed else 1


def _is_parse_failure(report: VerificationReport) -> bool:
    """Whether a report is an artifact-parse failure, not a verify failure.

    A parse failure means the LLM response never became structured
    artifacts (`IterationRecord.dockerfile` still holds the raw,
    possibly sentinel-laden response text) — writing it out would
    violate the authoring spec's transactional-write guarantee.
    """
    return any(
        r.check_id == "artifact_format" and r.status is CheckStatus.FAILED
        for r in report.results
    )


def _cmd_author(args: argparse.Namespace) -> int:
    project = Path(args.path)
    if not project.is_dir():
        print(f"error: {project} is not a directory", file=sys.stderr)
        return 2
    if args.max_iterations < 1:
        print("error: --max-iterations must be >= 1", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    target = _load_target(args.target)
    if isinstance(target, str):
        print(f"error: {target}", file=sys.stderr)
        return 2
    try:
        smoke_suite = _resolve_smoke_suite(target, args.target)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    runtime = None
    if not args.no_docker:
        runtime = _resolve_runtime_or_error(args)
        if isinstance(runtime, str):
            print(f"error: {runtime}", file=sys.stderr)
            return 2
    _load_dotenv()
    try:
        run = author_dockerfile(
            project,
            target,
            AnthropicAuthor(),
            max_iterations=args.max_iterations,
            runtime=runtime,
            build_timeout=args.build_timeout,
            health_timeout=args.health_timeout,
            smoke_suite=smoke_suite,
        )
    except TargetConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if run.iterations:
        last = run.iterations[-1]
        if not _is_parse_failure(last.report):
            (project / "Dockerfile").write_text(last.dockerfile + "\n")
            if last.compose is not None:
                (project / "compose.yaml").write_text(last.compose + "\n")
            if last.ci is not None:
                wf_dir = project / ".github" / "workflows"
                wf_dir.mkdir(parents=True, exist_ok=True)
                (wf_dir / "ci.yml").write_text(last.ci + "\n")
        _print_report(last.report)
    report_path = _write_report(
        project, "authoring-run.json", run.model_dump_json(indent=2)
    )
    line = f"stopped: {run.stopped_reason} after {len(run.iterations)} iteration(s)"
    if report_path is not None:
        line += f"; run report: {report_path}"
    print(line)
    accepted = ("success", "static_only") if args.no_docker else ("success",)
    return 0 if run.stopped_reason in accepted else 1


def _parse_positive_int(value: str, label: str) -> int | str:
    """Parse a CLI integer argument; return an error message on failure.

    Manual, not ``argparse type=``, so a bad value makes ``_cmd_diagnose``
    return 2 instead of ``main`` raising ``SystemExit``.
    """
    try:
        parsed = int(value)
    except ValueError:
        return f"{label} must be a positive integer"
    if parsed < 1:
        return f"{label} must be a positive integer"
    return parsed


def _resolve_run_ref(args: argparse.Namespace) -> tuple[RunRef, int | None] | str:
    """Build a run reference and resolved attempt from `diagnose` args.

    Exactly one of ``run_url`` or (``--repo`` and ``--run-id``) is required.
    An attempt on the URL and an explicit ``--attempt`` must agree when both
    are given. The run id and the attempt are range-checked whichever door
    they came through: `/runs/0` would otherwise reach ``gh``. Returns an
    error message instead of raising.
    """
    has_url = args.run_url is not None
    has_repo = args.repo is not None
    has_run_id = args.run_id is not None
    if has_url and (has_repo or has_run_id):
        return "run_url and --repo/--run-id are mutually exclusive"
    if not has_url and has_repo != has_run_id:
        return "--repo and --run-id must be given together"
    if not has_url and not has_repo:
        return "either run_url or --repo and --run-id is required"

    flag_attempt: int | None = None
    if args.attempt is not None:
        parsed = _parse_positive_int(args.attempt, "--attempt")
        if isinstance(parsed, str):
            return parsed
        flag_attempt = parsed

    if has_url:
        match = _RUN_URL_RE.search(args.run_url)
        if match is None:
            return f"not a recognized GitHub Actions run URL: {args.run_url}"
        repo, run_id_text, url_attempt_text = match.groups()
        if not _REPO_SLUG_RE.match(repo):
            return f"not a repository owner/name: {repo}"
        run_id_parsed = _parse_positive_int(run_id_text, "run id in URL")
        if isinstance(run_id_parsed, str):
            return run_id_parsed
        run_id = run_id_parsed
        url_attempt: int | None = None
        if url_attempt_text is not None:
            parsed_url_attempt = _parse_positive_int(
                url_attempt_text, "the run URL's attempt"
            )
            if isinstance(parsed_url_attempt, str):
                return parsed_url_attempt
            url_attempt = parsed_url_attempt
        if (
            url_attempt is not None
            and flag_attempt is not None
            and url_attempt != flag_attempt
        ):
            return (
                f"--attempt {flag_attempt} conflicts with the run URL's "
                f"attempt {url_attempt}"
            )
        resolved_attempt = flag_attempt if flag_attempt is not None else url_attempt
        return RunRef(repo, run_id), resolved_attempt

    if not _REPO_SLUG_RE.match(args.repo):
        return f"--repo must be a repository owner/name: {args.repo}"
    run_id_result = _parse_positive_int(args.run_id, "--run-id")
    if isinstance(run_id_result, str):
        return run_id_result
    return RunRef(args.repo, run_id_result), flag_attempt


def _format_where(where: StepRef | int) -> str:
    if isinstance(where, int):
        return f"job {where}"
    return f"job {where.job_id} step {where.number}"


def _print_diagnosis(diagnosis: RunDiagnosis) -> None:
    """Human summary on stdout; instrument diagnostics on stderr (spec §7).

    Run-level observations — what was missing, or that there was nothing to
    diagnose — belong to the summary and are printed once, on stdout. stderr
    carries only what the operator needs to judge the read itself.
    """
    print(f"outcome: {diagnosis.outcome}")
    print(f"causes: {', '.join(diagnosis.causes) or 'none asserted'}")
    for verdict in diagnosis.failures:
        observations = verdict.observations or ["-"]
        where = _format_where(verdict.where)
        print(f"[{where}] {verdict.outcome}: {observations[0]}")
        for observation in observations[1:]:
            print(f"    {observation}")
    for observation in diagnosis.observations:
        print(observation)
    completeness = diagnosis.run.completeness
    print(
        f"completeness: logs={completeness.logs} "
        f"annotations={completeness.annotations}",
        file=sys.stderr,
    )


def _print_reproduction(section: ReproductionSection) -> None:
    """Headline findings, one per line (§6)."""
    print(f"reproduction: {section.status}")
    if section.refusal:
        label = "refused" if section.status == "refused" else "unavailable"
        print(f"  {label}: {section.refusal}")
    if section.restoration is not None:
        unmet = "; ".join(section.restoration.unmet)
        line = f"  restoration: {section.restoration.state}"
        print(f"{line} ({unmet})" if unmet else line)
    for check in section.checks:
        if check.status in ("failed", "inconclusive"):
            print(f"  {check.status}: {check.finding or check.reason}")
    syntax_checks = [c for c in section.checks if c.check_id.startswith("syntax_")]
    if syntax_checks and all(c.status == "passed" for c in syntax_checks):
        print("  syntax: no finding among checks 1–4")
    if section.comparison is not None:
        extra = f" ({section.comparison.reason})" if section.comparison.reason else ""
        print(f"  comparison: {section.comparison.state}{extra}")
    if section.try_dir:
        print(f"  try: {section.try_dir}")


def _cmd_diagnose(args: argparse.Namespace) -> int:
    error = _diagnose_flag_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    resolved = _resolve_run_ref(args)
    if isinstance(resolved, str):
        print(f"error: {resolved}", file=sys.stderr)
        return 2
    if args.reproduce and args.container_host:
        print(
            "error: --reproduce builds locally only; drop --container-host",
            file=sys.stderr,
        )
        return 2
    ref, attempt = resolved
    try:
        result = fetch_failed_run(ref, attempt=attempt)
    except GhError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(result, AdapterRefusal):
        print(f"refused: {result.reason}: {result.detail}", file=sys.stderr)
        return 5
    diagnosis = diagnose_run(result)
    _print_diagnosis(diagnosis)
    section: ReproductionSection | None = None
    if args.reproduce:
        try:
            rt = resolve_runtime(args.container_tool, None)
            runtime_error = None if rt is not None else "no container tool found"
        except RuntimeConfigError as exc:
            rt, runtime_error = None, str(exc)
        try:
            section = reproduce_run(
                result,
                gh=SubprocessGh(),
                rt=rt,
                runtime_error=runtime_error,
                env=os.environ,
                root=Path.cwd(),
                build_timeout=args.build_timeout,
                max_archive_mb=args.max_archive_mb,
            )
        except TryDirError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _print_reproduction(section)
    if args.output_file is not None:
        try:
            Path(args.output_file).write_text(render_verdict(diagnosis, section))
        except OSError as exc:
            print(f"error: cannot write {args.output_file}: {exc}", file=sys.stderr)
            return 2
    return _EXIT_BY_OUTCOME[diagnosis.outcome]


def _cmd_bench_run(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"error: {corpus} is not a directory", file=sys.stderr)
        return 2
    if not _LABEL_RE.fullmatch(args.label):
        print("error: --label must match [A-Za-z0-9._-]+", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    runtime = _resolve_runtime_or_error(args)
    if isinstance(runtime, str):
        print(f"error: {runtime}", file=sys.stderr)
        return 2
    if args.author == "anthropic":
        _load_dotenv()
        shared = AnthropicAuthor()
        make_author = lambda case: shared  # noqa: E731
    else:
        make_author = lambda case: (  # noqa: E731
            FixtureAuthor(
                case.fixture_dockerfile.read_text(),
                compose=(
                    case.fixture_compose.read_text() if case.fixture_compose else None
                ),
                ci=case.fixture_ci.read_text() if case.fixture_ci else None,
            )
            if case.fixture_dockerfile is not None
            else None
        )
    try:
        report, run_dir = run_bench(
            corpus,
            make_author,
            runtime,
            label=args.label,
            author_backend=args.author,
            pattern=args.filter_pattern,
            build_timeout=args.build_timeout,
            health_timeout=args.health_timeout,
            include_external=args.include_external,
        )
    except (
        FileNotFoundError,
        ValueError,
        CloneError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for case in report.cases:
        line = f"[{case.outcome:>10}] {case.case}"
        if case.skip_reason:
            line += f": {case.skip_reason}"
        print(line)
    rate = report.success_rate
    print(f"success rate: {rate if rate is not None else 'n/a'}")
    print(f"bench-report: {run_dir / 'bench-report.json'}")
    print(f"markdown: {run_dir / 'bench-report.md'}")
    if args.require_atp:
        # Scope and intent both come from `report.cases` — never a second
        # `load_corpus` read, and never `skip_reason` prose. A re-read would
        # also miss external targets (cloned into the run, never in the
        # synthetic corpus dir); `smoke_declared` and `atp_smoke_status` are
        # recorded on every case, external or synthetic, skipped or not.
        smoke_cases = [c for c in report.cases if c.smoke_declared]
        if not smoke_cases:
            # An empty scope closes the gate even less than an unsatisfied
            # smoke does: nothing at all was exercised, so `report.all_matched`
            # alone must not be allowed to satisfy `--require-atp`.
            print(
                "error: --require-atp: no case in scope declares a smoke "
                f"intent (filter {args.filter_pattern!r} matched none); the "
                "gate has nothing to enforce",
                file=sys.stderr,
            )
            return 1
        # The spec's acceptance contract is `atp_smoke: PASSED` specifically,
        # not merely "the case matched its expectation" or "didn't skip": a
        # corpus case may declare `expected_success: false`, so a FAILED
        # atp_smoke can still end up `outcome="matched"`, and a case can be
        # skipped either by `atp_smoke` itself (version mismatch, binary
        # missing) or earlier still (no container runtime resolved) before
        # ATP ever runs. `satisfies_declared_smoke` treats all of these —
        # SKIPPED, FAILED, absent — as unsatisfied alike.
        unsatisfied = [c for c in smoke_cases if not satisfies_declared_smoke(c)]
        if unsatisfied:
            details = ", ".join(
                f"{c.case} ("
                f"{c.atp_smoke_status.value if c.atp_smoke_status is not None else 'no atp_smoke check recorded'}"
                ")"
                for c in unsatisfied
            )
            print(
                f"error: --require-atp: atp_smoke not satisfied for: {details}",
                file=sys.stderr,
            )
            return 1
    return 0 if report.all_matched else 1


def _cmd_bench_verify(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"error: {corpus} is not a directory", file=sys.stderr)
        return 2
    error = _timeout_error(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    runtime = _resolve_runtime_or_error(args)
    if isinstance(runtime, str):
        print(f"error: {runtime}", file=sys.stderr)
        return 2
    try:
        results = verify_corpus(
            corpus,
            runtime,
            pattern=args.filter_pattern,
            build_timeout=args.build_timeout,
            health_timeout=args.health_timeout,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failed = False
    for name, report in results:
        # `report.passed` alone is not enough: it treats SKIPPED as success
        # (correct for optional linters), so a declared smoke intent whose
        # `atp_smoke` was SKIPPED, FAILED, or never ran would print `ok`.
        # `smoke_declared`/`atp_smoke_status` are read straight off the
        # report's own fields — `verify_static` stamps `smoke_declared` at
        # construction so it is available even when L2, and therefore
        # `atp_smoke`, never ran at all.
        ok = report.passed and satisfies_declared_smoke(report)
        status = "ok" if ok else "FAIL"
        print(f"[{status:>4}] {name}")
        if not ok:
            failed = True
            if not report.passed:
                _print_report(report)
            elif not satisfies_declared_smoke(report):
                atp_status = report.atp_smoke_status
                detail = atp_status.value if atp_status is not None else "not run"
                print(f"  atp_smoke declared but not satisfied: {detail}")
    if runtime is None:
        print("note: no container runtime found; static-only verification")
    return 1 if failed else 0


def _cmd_bench_promote(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a directory", file=sys.stderr)
        return 2
    try:
        golden_dir = promote_run(run_dir, Path(args.corpus), force=args.force)
    except PromoteRefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"golden: {golden_dir}")
    return 0


def _cmd_bench_compare(args: argparse.Namespace) -> int:
    if args.candidate == "golden":
        print(
            "error: candidate must be a raw run dir "
            "(the golden can only be a baseline)",
            file=sys.stderr,
        )
        return 2
    candidate_dir = Path(args.candidate)
    try:
        candidate = load_baseline(candidate_dir, Path(args.corpus))
        baseline = load_baseline(
            args.baseline if args.baseline == "golden" else Path(args.baseline),
            Path(args.corpus),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(candidate, BenchReport):
        print("error: candidate must be a raw run dir", file=sys.stderr)
        return 2
    findings = compare_runs(
        candidate,
        baseline,
        image_threshold_pct=args.image_threshold,
        wall_threshold_pct=args.wall_threshold,
        iteration_threshold=args.iteration_threshold,
    )
    if not findings:
        print("no regressions")
        return 0
    for finding in findings:
        print(
            f"[{finding.level:>9}] {finding.case}: {finding.metric} — {finding.detail}"
        )
    blocking = any(f.level in ("hard", "important") for f in findings)
    return 1 if blocking else 0


def _read_pubkey_line(path: str) -> str | None:
    """Read a public-key file's first non-blank line; ``None`` on a bad file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    line = text.strip()
    return line or None


def _cmd_trust(args: argparse.Namespace) -> int:
    trust_directory = trust.trust_dir(os.environ)
    if args.trust_command == "replace":
        old_line = _read_pubkey_line(args.old_pubkey)
        if old_line is None:
            print(f"error: cannot read {args.old_pubkey}", file=sys.stderr)
            return 2
        new_line = _read_pubkey_line(args.new_pubkey)
        if new_line is None:
            print(f"error: cannot read {args.new_pubkey}", file=sys.stderr)
            return 2
        trust.replace(trust_directory, old_line, new_line)
    else:
        line = _read_pubkey_line(args.pubkey)
        if line is None:
            print(f"error: cannot read {args.pubkey}", file=sys.stderr)
            return 2
        if args.trust_command == "add":
            trust.add(trust_directory, line)
        else:
            trust.revoke(trust_directory, line)
    print(f"trust directory: {trust_directory}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `deployer` CLI."""
    parser = argparse.ArgumentParser(prog="deployer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="verify an existing Dockerfile")
    p_verify.add_argument("path")
    p_verify.add_argument("--target", default=None, help="DeployTarget JSON file")
    _add_timeout_flags(p_verify)
    _add_runtime_flags(p_verify)
    p_verify.set_defaults(func=_cmd_verify)

    p_author = sub.add_parser("author", help="author a Dockerfile with the LLM")
    p_author.add_argument("path")
    p_author.add_argument("--target", default=None, help="DeployTarget JSON file")
    p_author.add_argument("--max-iterations", type=int, default=3)
    p_author.add_argument(
        "--no-docker", action="store_true", help="static-only verification"
    )
    _add_timeout_flags(p_author)
    _add_runtime_flags(p_author)
    p_author.set_defaults(func=_cmd_author)

    p_diagnose = sub.add_parser(
        "diagnose",
        help="read a failed CI run: facts, evidence, observations; no cause asserted",
    )
    p_diagnose.add_argument(
        "run_url", nargs="?", default=None, help="GitHub Actions run URL"
    )
    p_diagnose.add_argument("--repo", default=None, help="owner/name")
    p_diagnose.add_argument(
        "--run-id", default=None, help="the run's numeric id (with --repo)"
    )
    p_diagnose.add_argument(
        "--attempt", default=None, help="re-run attempt number (default: the latest)"
    )
    p_diagnose.add_argument(
        "--output-file", default=None, help="write the verdict document here"
    )
    p_diagnose.add_argument(
        "--reproduce",
        action="store_true",
        help="restore the tree at head_sha and rebuild the failed step locally",
    )
    p_diagnose.add_argument(
        "--build-timeout",
        type=int,
        default=DEFAULT_BUILD_TIMEOUT,
        help="seconds allowed for the reproduction build",
    )
    p_diagnose.add_argument(
        "--max-archive-mb",
        type=int,
        default=DEFAULT_MAX_ARCHIVE_MB,
        help="cap on the fetched source archive; only meaningful with --reproduce",
    )
    _add_runtime_flags(p_diagnose)
    p_diagnose.set_defaults(func=_cmd_diagnose)

    p_bench = sub.add_parser("bench", help="corpus bench operations")
    bench_sub = p_bench.add_subparsers(dest="bench_command", required=True)

    p_bench_run = bench_sub.add_parser(
        "run", help="author every corpus case and aggregate metrics"
    )
    p_bench_run.add_argument("--corpus", default="corpus")
    p_bench_run.add_argument(
        "--filter", default="*", dest="filter_pattern", metavar="GLOB"
    )
    p_bench_run.add_argument("--label", default="run")
    p_bench_run.add_argument(
        "--author",
        choices=("fixture", "anthropic"),
        default="fixture",
        help="fixture (offline, default) or anthropic (real LLM, costs money)",
    )
    p_bench_run.add_argument(
        "--include-external",
        action="store_true",
        help="also clone and run corpus/external.toml targets",
    )
    p_bench_run.add_argument(
        "--require-atp",
        action="store_true",
        help=(
            "fail if a case declaring a smoke intent was skipped for a missing "
            "or mismatched atp; acceptance of the ATP seam requires it"
        ),
    )
    _add_runtime_flags(p_bench_run)
    _add_timeout_flags(p_bench_run)
    p_bench_run.set_defaults(func=_cmd_bench_run)

    p_bench_verify = bench_sub.add_parser(
        "verify", help="verify each case's committed fixture.Dockerfile"
    )
    p_bench_verify.add_argument("--corpus", default="corpus")
    p_bench_verify.add_argument(
        "--filter", default="*", dest="filter_pattern", metavar="GLOB"
    )
    _add_runtime_flags(p_bench_verify)
    _add_timeout_flags(p_bench_verify)
    p_bench_verify.set_defaults(func=_cmd_bench_verify)

    p_bench_promote = bench_sub.add_parser(
        "promote", help="promote a raw run to corpus/golden"
    )
    p_bench_promote.add_argument("run_dir")
    p_bench_promote.add_argument("--corpus", default="corpus")
    p_bench_promote.add_argument("--force", action="store_true")
    p_bench_promote.set_defaults(func=_cmd_bench_promote)

    p_bench_compare = bench_sub.add_parser(
        "compare", help="compare a raw run against another run or the golden"
    )
    p_bench_compare.add_argument("candidate")
    p_bench_compare.add_argument("baseline", help="raw run dir or 'golden'")
    p_bench_compare.add_argument("--corpus", default="corpus")
    p_bench_compare.add_argument(
        "--image-threshold", type=float, default=10.0, metavar="PCT"
    )
    p_bench_compare.add_argument(
        "--wall-threshold", type=float, default=25.0, metavar="PCT"
    )
    p_bench_compare.add_argument(
        "--iteration-threshold", type=int, default=0, metavar="N"
    )
    p_bench_compare.set_defaults(func=_cmd_bench_compare)

    p_trust = sub.add_parser("trust", help="manage the out-of-repo trust store")
    trust_sub = p_trust.add_subparsers(dest="trust_command", required=True)

    p_trust_add = trust_sub.add_parser("add", help="allow a signing key")
    p_trust_add.add_argument("pubkey", help="path to a public-key file")
    p_trust_add.set_defaults(func=_cmd_trust)

    p_trust_revoke = trust_sub.add_parser("revoke", help="revoke a signing key")
    p_trust_revoke.add_argument("pubkey", help="path to a public-key file")
    p_trust_revoke.set_defaults(func=_cmd_trust)

    p_trust_replace = trust_sub.add_parser(
        "replace", help="allow a new key and revoke the old one"
    )
    p_trust_replace.add_argument("old_pubkey", help="path to the old public-key file")
    p_trust_replace.add_argument("new_pubkey", help="path to the new public-key file")
    p_trust_replace.set_defaults(func=_cmd_trust)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
