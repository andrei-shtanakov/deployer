"""Thin argparse CLI over the deployer library."""

import argparse
import sys
from pathlib import Path

from deployer.author import author_dockerfile
from deployer.llm import AnthropicAuthor
from deployer.models import CheckStatus, DeployTarget, VerificationReport
from deployer.verify import detect_container_tool, verify

_STATUS_ICONS = {
    CheckStatus.PASSED: "ok",
    CheckStatus.FAILED: "FAIL",
    CheckStatus.WARNING: "warn",
    CheckStatus.SKIPPED: "skip",
}


def _load_target(path: str | None) -> DeployTarget:
    if path is None:
        return DeployTarget()
    return DeployTarget.model_validate_json(Path(path).read_text())


def _print_report(report: VerificationReport) -> None:
    for result in report.results:
        icon = _STATUS_ICONS[result.status]
        line = f"[{icon:>4}] {result.check_id}"
        if result.message:
            line += f": {result.message.splitlines()[0]}"
        print(line)
    if not report.docker_available:
        print("note: no container runtime found; static-only verification")


def _cmd_verify(args: argparse.Namespace) -> int:
    project = Path(args.path)
    dockerfile_path = project / "Dockerfile"
    if not dockerfile_path.is_file():
        print(f"error: {dockerfile_path} not found", file=sys.stderr)
        return 1
    target = _load_target(args.target)
    report = verify(
        dockerfile_path.read_text(), project, target, detect_container_tool()
    )
    _print_report(report)
    return 0 if report.passed else 1


def _cmd_author(args: argparse.Namespace) -> int:
    project = Path(args.path)
    target = _load_target(args.target)
    if args.max_iterations < 1:
        print("error: --max-iterations must be >= 1", file=sys.stderr)
        return 2
    run = author_dockerfile(
        project,
        target,
        AnthropicAuthor(),
        max_iterations=args.max_iterations,
        run_docker=not args.no_docker,
    )
    if run.iterations:
        (project / "Dockerfile").write_text(run.iterations[-1].dockerfile + "\n")
        _print_report(run.iterations[-1].report)
    report_dir = project / ".deployer"
    report_dir.mkdir(exist_ok=True)
    (report_dir / "authoring-run.json").write_text(run.model_dump_json(indent=2))
    print(
        f"stopped: {run.stopped_reason} after {len(run.iterations)} iteration(s); "
        f"run report: {report_dir / 'authoring-run.json'}"
    )
    accepted = ("success", "static_only") if args.no_docker else ("success",)
    return 0 if run.stopped_reason in accepted else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `deployer` CLI."""
    parser = argparse.ArgumentParser(prog="deployer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="verify an existing Dockerfile")
    p_verify.add_argument("path")
    p_verify.add_argument("--target", default=None, help="DeployTarget JSON file")
    p_verify.set_defaults(func=_cmd_verify)

    p_author = sub.add_parser("author", help="author a Dockerfile with the LLM")
    p_author.add_argument("path")
    p_author.add_argument("--target", default=None, help="DeployTarget JSON file")
    p_author.add_argument("--max-iterations", type=int, default=3)
    p_author.add_argument(
        "--no-docker", action="store_true", help="static-only verification"
    )
    p_author.set_defaults(func=_cmd_author)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
