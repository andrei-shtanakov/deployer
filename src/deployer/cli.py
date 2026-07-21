"""Thin argparse CLI over the deployer library."""

import argparse
import re
import sys
from pathlib import Path

from pydantic import ValidationError

from deployer.author import author_dockerfile
from deployer.bench import FixtureAuthor, run_bench, verify_corpus
from deployer.facts import analyze_project
from deployer.llm import AnthropicAuthor
from deployer.models import (
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    VerificationReport,
)
from deployer.runtime import (
    RuntimeConfigError,
    probe_runtime_versions,
    resolve_runtime,
)
from deployer.verify import DEFAULT_BUILD_TIMEOUT, DEFAULT_HEALTH_TIMEOUT, verify

_LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")

_STATUS_ICONS = {
    CheckStatus.PASSED: "ok",
    CheckStatus.FAILED: "FAIL",
    CheckStatus.WARNING: "warn",
    CheckStatus.SKIPPED: "skip",
}


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
        help="seconds allowed for the healthcheck; ignored for non-service targets",
    )


def _timeout_error(args: argparse.Namespace) -> str | None:
    if args.build_timeout < 1:
        return "--build-timeout must be >= 1"
    if args.health_timeout < 1:
        return "--health-timeout must be >= 1"
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
    dockerfile_path = project / "Dockerfile"
    if not dockerfile_path.is_file():
        print(f"error: {dockerfile_path} not found", file=sys.stderr)
        return 1
    runtime = _resolve_runtime_or_error(args)
    if isinstance(runtime, str):
        print(f"error: {runtime}", file=sys.stderr)
        return 2
    report = verify(
        dockerfile_path.read_text(),
        project,
        target,
        runtime,
        analyze_project(project),
        build_timeout=args.build_timeout,
        health_timeout=args.health_timeout,
    )
    if runtime is not None:
        report.runtime_versions = probe_runtime_versions(runtime)
    _print_report(report)
    report_path = _write_report(
        project, "verify-report.json", report.model_dump_json(indent=2)
    )
    if report_path is not None:
        print(f"report: {report_path}")
    return 0 if report.passed else 1


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
    runtime = None
    if not args.no_docker:
        runtime = _resolve_runtime_or_error(args)
        if isinstance(runtime, str):
            print(f"error: {runtime}", file=sys.stderr)
            return 2
    run = author_dockerfile(
        project,
        target,
        AnthropicAuthor(),
        max_iterations=args.max_iterations,
        runtime=runtime,
        build_timeout=args.build_timeout,
        health_timeout=args.health_timeout,
    )
    if run.iterations:
        (project / "Dockerfile").write_text(run.iterations[-1].dockerfile + "\n")
        _print_report(run.iterations[-1].report)
    report_path = _write_report(
        project, "authoring-run.json", run.model_dump_json(indent=2)
    )
    line = f"stopped: {run.stopped_reason} after {len(run.iterations)} iteration(s)"
    if report_path is not None:
        line += f"; run report: {report_path}"
    print(line)
    accepted = ("success", "static_only") if args.no_docker else ("success",)
    return 0 if run.stopped_reason in accepted else 1


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
        shared = AnthropicAuthor()
        make_author = lambda case: shared  # noqa: E731
    else:
        make_author = lambda case: (  # noqa: E731
            FixtureAuthor(case.fixture_dockerfile.read_text())
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
        )
    except (FileNotFoundError, ValueError) as exc:
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
        status = "ok" if report.passed else "FAIL"
        print(f"[{status:>4}] {name}")
        if not report.passed:
            failed = True
            _print_report(report)
    return 1 if failed else 0


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
