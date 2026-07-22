"""Two-level deterministic verification of Dockerfile candidates.

L1 (this file's static half): parse, COPY-source existence, base-image pinning,
hadolint at a pinned version. L2 (docker half, Task 5): sandboxed build + run.
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from deployer.facts import (
    TargetConfigError,
    _normalize_requirement_name,
    validate_target_against_facts,
)
from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    VerificationReport,
)
from deployer.runtime import container_run

HADOLINT_VERSION = "2.12.0"
DEFAULT_BUILD_TIMEOUT = 600
DEFAULT_HEALTH_TIMEOUT = 30
_ENV_ASSIGNMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_PYTHON_M_PIP = re.compile(r"^\S*python[\d.]*\s+-m\s+pip\s+install\b")
_PIP_INSTALL = re.compile(r"^(?:\S*python[\d.]*\s+-m\s+pip|(?:\S*/)?pip3?)\s+install\b")
# Flags whose separate-token value is never an install target (index/
# transport options). Deliberately EXCLUDES -r/-c/-e: their values are
# install targets and must stay in the payload for the FAIL rules.
_PIP_VALUE_FLAGS = frozenset(
    {"-i", "--index-url", "--extra-index-url", "--trusted-host", "--proxy", "--cert"}
)

CONTEXT_IGNORE = (
    ".git",
    ".venv",
    ".deployer",
    ".env",
    ".env.*",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
)


@contextmanager
def _isolated_context(project_path: Path) -> Iterator[Path]:
    """Deterministic temp build context: the project minus CONTEXT_IGNORE.

    Restores the MVP invariant "no secrets in the build context, ever" —
    with a remote daemon the context leaves the machine entirely.
    """
    with tempfile.TemporaryDirectory(prefix="deployer-context-") as tmp:
        context = Path(tmp) / "context"
        shutil.copytree(
            project_path,
            context,
            ignore=shutil.ignore_patterns(*CONTEXT_IGNORE),
            symlinks=True,
        )
        yield context


def parse_dockerfile(text: str) -> list[tuple[str, str]]:
    """Split a Dockerfile into (INSTRUCTION, args) pairs.

    Joins backslash line-continuations and drops comments/blank lines.
    """
    logical_lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical_lines.append(buffer + line)
        buffer = ""
    if buffer:
        logical_lines.append(buffer.strip())

    instructions: list[tuple[str, str]] = []
    for line in logical_lines:
        head, _, rest = line.partition(" ")
        instructions.append((head.upper(), rest.strip()))
    return instructions


def _check_parses(instructions: list[tuple[str, str]]) -> CheckResult:
    if not instructions:
        return CheckResult(
            check_id="parses",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="empty Dockerfile",
        )
    if instructions[0][0] != "FROM" or not instructions[0][1]:
        return CheckResult(
            check_id="parses",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="Dockerfile must start with a FROM instruction",
        )
    return CheckResult(check_id="parses", status=CheckStatus.PASSED)


def _check_copy_sources(
    instructions: list[tuple[str, str]], project_path: Path
) -> CheckResult:
    missing: list[str] = []
    for name, args in instructions:
        if name not in ("COPY", "ADD"):
            continue
        tokens = args.split()
        if any(t.startswith("--from=") for t in tokens):
            continue  # copies from a build stage, not the context
        sources = [t for t in tokens if not t.startswith("--")][:-1]
        for src in sources:
            if src.startswith(("http://", "https://")):
                continue
            if any(ch in src for ch in "*?["):
                if not list(project_path.glob(src)):
                    missing.append(src)
            elif not (project_path / src).exists():
                missing.append(src)
    if missing:
        return CheckResult(
            check_id="copy_sources",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=f"COPY/ADD sources not found in project: {', '.join(missing)}",
        )
    return CheckResult(check_id="copy_sources", status=CheckStatus.PASSED)


def _run_commands(run_lines: list[str]) -> list[str]:
    """Split RUN lines into individual shell commands (segments)."""
    commands: list[str] = []
    for line in run_lines:
        for segment in re.split(r"&&|\|\||;|\|", line):
            stripped = segment.strip()
            if not stripped:
                continue
            stripped = _ENV_ASSIGNMENT.sub("", stripped)
            if stripped:
                commands.append(stripped)
    return commands


def _check_base_pinned(instructions: list[tuple[str, str]]) -> CheckResult:
    for name, args in instructions:
        if name != "FROM":
            continue
        tokens = [t for t in args.split() if not t.startswith("--")]
        if not tokens:
            continue
        image = tokens[0]
        if "@sha256:" in image:
            continue
        _, _, tag = image.partition(":")
        if not tag or tag == "latest":
            return CheckResult(
                check_id="base_pinned",
                status=CheckStatus.WARNING,
                message=f"base image '{image}' has no pinned tag; "
                "reproducible builds need a tag (ideally a digest)",
            )
    return CheckResult(check_id="base_pinned", status=CheckStatus.PASSED)


def _pip_install_payload(cmd: str) -> list[str] | None:
    """Positional install args of a pip invocation; None if not one.

    Covers pip / pip3 / python -m pip / python3 -m pip, with an optional
    path prefix on the pip executable (/usr/bin/pip, .venv/bin/pip3).
    Flags are dropped; for the known value-taking flags in
    _PIP_VALUE_FLAGS the value token is dropped too, so an index URL
    never masquerades as an install target. Unknown flags' values may
    still survive as positionals — that errs toward FAIL, never a false
    pass. The regex's `\\b` also matches inside a hyphenated token (e.g.
    `pip install-e .`), so a missing standalone "install" token is
    guarded explicitly rather than letting `.index()` raise.
    """
    if not _PIP_INSTALL.match(cmd):
        return None
    tokens = cmd.split()
    if "install" not in tokens:
        return None
    payload: list[str] = []
    skip_next = False
    for token in tokens[tokens.index("install") + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if token in _PIP_VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        payload.append(token.strip("'\""))
    return payload


def _check_install_strategy(
    instructions: list[tuple[str, str]], facts: ProjectFacts
) -> CheckResult:
    """Deterministic install-strategy rules, promoted from prompt to check."""
    run_lines = [args for name, args in instructions if name == "RUN"]
    commands = _run_commands(run_lines)
    problems: list[str] = []
    warnings: list[str] = []

    # uv-in-pip-project rule
    if facts.package_manager == "pip":
        for cmd in commands:
            if cmd.startswith(("uv sync", "uv pip")):
                problems.append(
                    "project uses pip (requirements.txt) but Dockerfile invokes uv"
                )
                break

    # pip-in-uv-project rule
    if facts.package_manager == "uv":
        for cmd in commands:
            if cmd.startswith(("pip install", "pip3 install")) or _PYTHON_M_PIP.match(
                cmd
            ):
                problems.append("project uses uv (uv.lock) but Dockerfile invokes pip")
                break

    # no-build-system rule
    if not facts.has_build_system:
        for cmd in commands:
            installs_project = False

            # Check for "pip install ." (bare . token)
            if cmd.startswith(("pip install", "pip3 install")):
                tokens = cmd.split()
                if "." in tokens:
                    installs_project = True

            # Check for "uv sync" without "--no-install-project"
            if cmd.startswith("uv sync") and "--no-install-project" not in cmd:
                installs_project = True

            if installs_project:
                problems.append(
                    "project has no [build-system]: do not install it as a "
                    "package (run sources directly / use --no-install-project)"
                )
                break

    # poetry rules: poetry.lock is the only dependency source
    if facts.package_manager == "poetry":
        for cmd in commands:
            if cmd.startswith(("uv sync", "uv pip")):
                problems.append(
                    "project uses poetry (poetry.lock) but Dockerfile invokes uv"
                )
                break
        for cmd in commands:
            payload = _pip_install_payload(cmd)
            if not payload:
                continue
            names = {_normalize_requirement_name(t) for t in payload}
            if names == {"poetry"}:
                # the builder bootstrap — allowed, but must be pinned
                if not all("==" in t for t in payload):
                    msg = "poetry bootstrap is not pinned; use poetry==<version>"
                    if msg not in warnings:
                        warnings.append(msg)
                continue
            problems.append(
                "project uses poetry (poetry.lock) but Dockerfile installs "
                "dependencies with pip; poetry.lock is the only dependency "
                "source"
            )
            break

    # poetry-in-non-poetry rule
    if facts.package_manager in ("uv", "pip"):
        for cmd in commands:
            if cmd.startswith("poetry install"):
                problems.append(
                    f"project uses {facts.package_manager} but Dockerfile "
                    "invokes poetry install"
                )
                break

    if problems:
        return CheckResult(
            check_id="install_strategy",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message="; ".join(problems),
        )
    if warnings:
        return CheckResult(
            check_id="install_strategy",
            status=CheckStatus.WARNING,
            message="; ".join(warnings),
        )
    return CheckResult(check_id="install_strategy", status=CheckStatus.PASSED)


def _final_stage_commands(
    instructions: list[tuple[str, str]],
) -> tuple[str | None, str | None]:
    """Last ENTRYPOINT and CMD args after the last FROM (the final stage).

    Deliberately ignores commands a final stage may inherit (a builder
    alias via `FROM build`, or a base image's own ENTRYPOINT): that errs
    toward a false FAIL — one repair iteration nudging the model to an
    explicit final-stage CMD — never a false pass. Do not "fix" this by
    scanning earlier stages.
    """
    last_from = -1
    for i, (name, _) in enumerate(instructions):
        if name == "FROM":
            last_from = i
    entrypoint: str | None = None
    cmd: str | None = None
    for name, args in instructions[last_from + 1 :]:
        if name == "ENTRYPOINT":
            entrypoint = args
        elif name == "CMD":
            cmd = args
    return entrypoint, cmd


def _check_entrypoint_in_command(
    instructions: list[tuple[str, str]], target: DeployTarget
) -> CheckResult:
    """The image's effective command must reference the operator entrypoint.

    Only the final stage counts: a builder-stage CMD is not the image's
    command. Substring match covers exec form, shell form, and
    [project.scripts] names alike; deliberately conservative (e.g.
    `python -m main` does not satisfy `main.py`).
    """
    assert target.entrypoint is not None
    entry_args, cmd_args = _final_stage_commands(instructions)
    haystack = " ".join(a for a in (entry_args, cmd_args) if a is not None)
    if target.entrypoint in haystack:
        return CheckResult(check_id="entrypoint_in_command", status=CheckStatus.PASSED)
    return CheckResult(
        check_id="entrypoint_in_command",
        status=CheckStatus.FAILED,
        failure_kind=FailureKind.AUTHORING,
        message=(
            f"entrypoint intent {target.entrypoint!r} not found in image "
            f"command: ENTRYPOINT {entry_args if entry_args is not None else 'none'}, "
            f"CMD {cmd_args if cmd_args is not None else 'none'}"
        ),
    )


def _failed(check_id: str, message: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=CheckStatus.FAILED,
        failure_kind=FailureKind.AUTHORING,
        message=message,
    )


def _passed(check_id: str) -> CheckResult:
    return CheckResult(check_id=check_id, status=CheckStatus.PASSED)


def _env_keys(service: dict) -> set[str]:
    """Keys of a compose `environment` block, mapping or KEY=VALUE list."""
    raw = service.get("environment")
    if isinstance(raw, dict):
        return {k for k in raw if isinstance(k, str)}
    if isinstance(raw, list):
        return {e.split("=", 1)[0] for e in raw if isinstance(e, str) and "=" in e}
    return set()


def _compose_l1_checks(compose: str | None, target: DeployTarget) -> list[CheckResult]:
    """Static checks for the compose artifact of a dependencies target.

    Validates only the schema slice deployer relies on (services
    mapping, service mappings) — never the full Compose spec.
    """
    if compose is None:
        return [
            _failed(
                "compose_present",
                "deploy target declares dependencies but no compose.yaml "
                "artifact was provided",
            )
        ]
    results = [_passed("compose_present")]

    try:
        doc = yaml.safe_load(compose)
    except yaml.YAMLError as exc:
        results.append(_failed("compose_parses", f"compose.yaml: {exc}"))
        return results
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not all(
        isinstance(v, dict) for v in services.values()
    ):
        results.append(
            _failed(
                "compose_parses",
                "compose.yaml must be a mapping with a `services` mapping "
                "whose values are mappings",
            )
        )
        return results
    results.append(_passed("compose_parses"))

    problems: list[str] = []
    expected = {"app"} | {d.name for d in target.dependencies}
    actual = set(services)
    if actual != expected:
        problems.append(
            f"services must be exactly {sorted(expected)}, got {sorted(actual)}"
        )
    app = services.get("app", {})
    build = app.get("build")
    build_ok = build == "." or (
        isinstance(build, dict)
        and build.get("context") == "."
        and build.get("dockerfile", "Dockerfile") == "Dockerfile"
    )
    if "app" in services and not build_ok:
        problems.append(
            "app must build from the project Dockerfile "
            '(build: "." or {context: ".", dockerfile: "Dockerfile"})'
        )
    for dep in target.dependencies:
        svc = services.get(dep.name)
        if svc is not None and svc.get("image") != dep.image:
            problems.append(
                f"service {dep.name} must use image {dep.image!r} verbatim, "
                f"got {svc.get('image')!r}"
            )
    results.append(
        _failed("compose_services", "; ".join(problems))
        if problems
        else _passed("compose_services")
    )

    wiring: list[str] = []
    depends = app.get("depends_on")
    for dep in target.dependencies:
        svc = services.get(dep.name)
        if svc is not None and not isinstance(svc.get("healthcheck"), dict):
            wiring.append(f"service {dep.name} must define a healthcheck")
        condition = (
            depends.get(dep.name, {}).get("condition")
            if isinstance(depends, dict) and isinstance(depends.get(dep.name), dict)
            else None
        )
        if condition != "service_healthy":
            wiring.append(
                f"app must depend on {dep.name} with condition: service_healthy"
            )
    missing_env = set(target.env) - _env_keys(app)
    if missing_env:
        wiring.append(
            "app environment must carry the deploy intent env keys: "
            f"missing {sorted(missing_env)}"
        )
    for name, svc in services.items():
        if isinstance(svc, dict) and "ports" in svc:
            wiring.append(
                f"service {name} declares ports; compose networking is "
                "internal-only for the verifier"
            )
    results.append(
        _failed("compose_wiring", "; ".join(wiring))
        if wiring
        else _passed("compose_wiring")
    )
    return results


def _check_hadolint(dockerfile: str) -> tuple[CheckResult, bool]:
    """Run hadolint at the pinned version; (result, available_and_comparable)."""
    binary = shutil.which("hadolint")
    if binary is None:
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.SKIPPED,
                message=f"hadolint {HADOLINT_VERSION} not installed; "
                "run is non-comparable",
            ),
            False,
        )
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        if HADOLINT_VERSION not in version:
            return (
                CheckResult(
                    check_id="hadolint",
                    status=CheckStatus.SKIPPED,
                    message=f"hadolint version mismatch (want {HADOLINT_VERSION}, "
                    f"got: {version.strip()}); run is non-comparable",
                ),
                False,
            )
        proc = subprocess.run(
            [binary, "--no-color", "-f", "json", "-"],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=30,
        )
        findings = json.loads(proc.stdout) if proc.stdout.strip() else []
        errors = [f for f in findings if f.get("level") == "error"]
        if errors:
            lines = "; ".join(f"{f['code']}: {f['message']}" for f in errors)
            return (
                CheckResult(
                    check_id="hadolint",
                    status=CheckStatus.FAILED,
                    failure_kind=FailureKind.AUTHORING,
                    message=lines,
                ),
                True,
            )
        if findings:
            lines = "; ".join(f"{f['code']}: {f['message']}" for f in findings)
            return (
                CheckResult(
                    check_id="hadolint", status=CheckStatus.WARNING, message=lines
                ),
                True,
            )
        return (CheckResult(check_id="hadolint", status=CheckStatus.PASSED), True)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return (
            CheckResult(
                check_id="hadolint",
                status=CheckStatus.SKIPPED,
                message=f"hadolint execution failed ({exc.__class__.__name__}); "
                "run is non-comparable",
            ),
            False,
        )


def verify_static(
    dockerfile: str,
    project_path: Path,
    facts: ProjectFacts | None = None,
    target: DeployTarget | None = None,
) -> VerificationReport:
    """Run all L1 static checks against a Dockerfile candidate."""
    instructions = parse_dockerfile(dockerfile)
    results = [_check_parses(instructions)]
    if results[0].status is CheckStatus.PASSED:
        results.append(_check_copy_sources(instructions, project_path))
        results.append(_check_base_pinned(instructions))
        if facts is not None:
            results.append(_check_install_strategy(instructions, facts))
        else:
            results.append(
                CheckResult(
                    check_id="install_strategy",
                    status=CheckStatus.SKIPPED,
                    message="no project facts provided",
                )
            )
        if target is not None and target.entrypoint is not None:
            results.append(_check_entrypoint_in_command(instructions, target))
    hadolint_result, hadolint_available = _check_hadolint(dockerfile)
    results.append(hadolint_result)
    return VerificationReport(results=results, hadolint_available=hadolint_available)


ENVIRONMENT_MARKERS = (
    "tls handshake",
    "connection refused",
    "connection reset",
    "temporary failure",
    "i/o timeout",
    "toomanyrequests",
    "network is unreachable",
    "no route to host",
    "service unavailable",
    "permission denied (publickey)",
    "host key verification failed",
    "could not resolve hostname",
    "ssh: connect to host",
    "connection timed out",
    "cannot connect to the docker daemon",
    "error during connect",
    "context deadline exceeded",
)


def _classify(output: str) -> FailureKind:
    lowered = output.lower()
    if any(marker in lowered for marker in ENVIRONMENT_MARKERS):
        return FailureKind.ENVIRONMENT
    return FailureKind.AUTHORING


_TRANSPORT_MARKERS = (
    "error during connect",
    "cannot connect to the docker daemon",
    "ssh: ",
    "context deadline exceeded",
)


def _is_transport_failure(output: str) -> bool:
    """Narrow marker check for daemon/SSH-transport loss during a probe loop.

    Deliberately narrower than `_classify`: a legitimately failing
    in-container probe can print a Python traceback containing phrases
    like "connection refused" (the app refusing its own port), which
    would otherwise flip a real authoring failure to ENVIRONMENT.
    """
    lowered = output.lower()
    return any(marker in lowered for marker in _TRANSPORT_MARKERS)


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _build(
    dockerfile: str,
    context_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    tag: str,
    timeout: int,
) -> CheckResult:
    try:
        proc = container_run(
            runtime,
            [
                "build",
                "--memory",
                target.memory_limit,
                "-t",
                tag,
                "-f",
                "-",
                str(context_path),
            ],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        message = (
            f"build timed out after {timeout}s"
            if isinstance(exc, subprocess.TimeoutExpired)
            else f"container runtime command failed: {exc}"
        )
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message=message,
        )
    if proc.returncode != 0:
        return CheckResult(
            check_id="build",
            status=CheckStatus.FAILED,
            failure_kind=_classify(proc.stdout + "\n" + proc.stderr),
            message=_tail(proc.stderr or proc.stdout),
        )
    return CheckResult(check_id="build", status=CheckStatus.PASSED)


def _image_size(runtime: ContainerRuntime, tag: str) -> int | None:
    """Size of a built image in bytes; None when inspection fails."""
    try:
        proc = container_run(
            runtime,
            ["image", "inspect", "--format", "{{.Size}}", tag],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


_IMAGE_COMMAND_FORMAT = (
    "ENTRYPOINT {{json .Config.Entrypoint}}, CMD {{json .Config.Cmd}}"
)


def _image_command(runtime: ContainerRuntime, tag: str) -> str | None:
    """Best-effort ENTRYPOINT/CMD of the built image, for repair feedback.

    Reads the image (never the container, so cleanup cannot race it) and
    swallows every failure: feedback must not change a verdict.
    """
    try:
        proc = container_run(
            runtime,
            ["image", "inspect", "--format", _IMAGE_COMMAND_FORMAT, tag],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _with_command_feedback(message: str, runtime: ContainerRuntime, tag: str) -> str:
    """Append the image's ENTRYPOINT/CMD to an AUTHORING failure, if known."""
    command = _image_command(runtime, tag)
    if command is None:
        return message
    return f"{message}\ncontainer command: {command}"


def _run_healthcheck(
    target: DeployTarget, runtime: ContainerRuntime, tag: str, timeout: int
) -> CheckResult:
    assert target.service is not None
    container = f"deployer-check-{uuid.uuid4().hex[:8]}"
    url = f"http://127.0.0.1:{target.service.port}{target.service.healthcheck_path}"
    probe = f"import urllib.request; urllib.request.urlopen('{url}', timeout=2)"
    try:
        started = container_run(
            runtime,
            [
                "run",
                "-d",
                "--name",
                container,
                "--network=none",
                "--memory",
                target.memory_limit,
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if started.returncode != 0:
            failure_kind = _classify(started.stdout + "\n" + started.stderr)
            message = _tail(started.stderr or started.stdout)
            if failure_kind is FailureKind.AUTHORING:
                message = _with_command_feedback(message, runtime, tag)
            return CheckResult(
                check_id="run_healthcheck",
                status=CheckStatus.FAILED,
                failure_kind=failure_kind,
                message=message,
            )
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                probe_proc = container_run(
                    runtime,
                    ["exec", container, "python", "-c", probe],
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, remaining),
                )
                if probe_proc.returncode == 0:
                    return CheckResult(
                        check_id="run_healthcheck", status=CheckStatus.PASSED
                    )
                last_error = probe_proc.stdout + "\n" + probe_proc.stderr
            except subprocess.TimeoutExpired:
                # Probe timed out; treat as loop exhaustion
                break
            time.sleep(1)
        logs = container_run(
            runtime, ["logs", container], capture_output=True, text=True, timeout=10
        )
        log_text = (logs.stdout + "\n" + logs.stderr).strip()
        if _is_transport_failure(last_error):
            return CheckResult(
                check_id="run_healthcheck",
                status=CheckStatus.FAILED,
                failure_kind=FailureKind.ENVIRONMENT,
                message=(
                    f"healthcheck {url}: container daemon became unreachable "
                    f"mid-poll: {_tail(last_error, 3)}"
                ),
            )
        message = _with_command_feedback(
            f"healthcheck {url} failed within {timeout}s: "
            f"{_tail(last_error, 3)}\ncontainer logs:\n{_tail(log_text)}",
            runtime,
            tag,
        )
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.AUTHORING,
            message=message,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        message = (
            "container runtime command timed out"
            if isinstance(exc, subprocess.TimeoutExpired)
            else f"container runtime command failed: {exc}"
        )
        return CheckResult(
            check_id="run_healthcheck",
            status=CheckStatus.FAILED,
            failure_kind=FailureKind.ENVIRONMENT,
            message=message,
        )
    finally:
        try:
            container_run(
                runtime, ["rm", "-f", container], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort cleanup; must never clobber the return value


def _redact_oracle(message: str, marker: str | None) -> str:
    """Strip the run-intent stdout oracle from verifier text.

    Prompt-side redaction alone cannot stop a program that prints the
    marker and then crashes, or an echo-CMD that carries it into command
    feedback — so every FAILED run_completes message passes through here.

    A multi-line marker is also redacted line by line: other checks
    (e.g. build) tail their output before the report-wide pass runs, and
    truncation can leave a fragment the full-string replace would miss.
    """
    if not marker:
        return message
    message = message.replace(marker, "<redacted>")
    for line in marker.splitlines():
        if line.strip():
            message = message.replace(line, "<redacted>")
    return message


def _run_completes(
    target: DeployTarget, runtime: ContainerRuntime, tag: str, timeout: int
) -> CheckResult:
    """Job intent: the image's default command must exit 0 within timeout.

    With an `expect_stdout` oracle, stdout must also contain the marker.
    ENVIRONMENT is deliberately narrow: a foreground run interleaves app
    and CLI output, so only an explicit CLI failure (OSError, or exit
    125/126 plus transport markers) counts — an app that prints
    "connection refused" and exits non-zero stays AUTHORING.
    """
    assert target.run is not None
    container = f"deployer-check-{uuid.uuid4().hex[:8]}"
    marker = target.run.expect_stdout

    def _failed(kind: FailureKind, message: str) -> CheckResult:
        if kind is FailureKind.AUTHORING:
            message = _with_command_feedback(message, runtime, tag)
        return CheckResult(
            check_id="run_completes",
            status=CheckStatus.FAILED,
            failure_kind=kind,
            message=_redact_oracle(message, marker),
        )

    try:
        proc = container_run(
            runtime,
            [
                "run",
                "--name",
                container,
                "--network=none",
                "--memory",
                target.memory_limit,
                tag,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _failed(
            FailureKind.AUTHORING,
            f"container did not exit within {timeout}s (a run intent means "
            "a job: the default command must run to completion)",
        )
    except OSError as exc:
        return _failed(
            FailureKind.ENVIRONMENT,
            f"container runtime command failed: {exc}",
        )
    finally:
        try:
            container_run(
                runtime, ["rm", "-f", container], capture_output=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort cleanup; must never clobber the return value

    if proc.returncode == 0:
        if marker is not None and marker not in proc.stdout:
            redacted_stdout = _redact_oracle(proc.stdout, marker)
            return _failed(
                FailureKind.AUTHORING,
                "container exited 0 but stdout did not contain the expected "
                f"output\nstdout tail:\n{_tail(redacted_stdout)}",
            )
        return CheckResult(check_id="run_completes", status=CheckStatus.PASSED)

    output = _redact_oracle(proc.stdout + "\n" + proc.stderr, marker)
    if proc.returncode in (125, 126) and _is_transport_failure(output):
        return _failed(
            FailureKind.ENVIRONMENT,
            f"container runtime failed to start the job: {_tail(output, 3)}",
        )
    return _failed(
        FailureKind.AUTHORING,
        f"container exited {proc.returncode}\noutput tail:\n{_tail(output)}",
    )


def verify_docker(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
) -> tuple[list[CheckResult], int | None]:
    """L2: real sandboxed build; then service healthcheck or job run-completes.

    The healthcheck probes over the container's loopback via `exec python -c`,
    so `--network=none` still works. This assumes a Python base image — true
    for every artifact this MVP authors.
    """
    tag = f"deployer-verify-{uuid.uuid4().hex[:8]}"
    results: list[CheckResult] = []
    image_size: int | None = None
    try:
        with _isolated_context(project_path) as context:
            build_result = _build(
                dockerfile, context, target, runtime, tag, build_timeout
            )
        results.append(build_result)
        if build_result.status is CheckStatus.PASSED:
            image_size = _image_size(runtime, tag)
            if target.service is not None:
                results.append(_run_healthcheck(target, runtime, tag, health_timeout))
            elif target.run is not None:
                results.append(_run_completes(target, runtime, tag, health_timeout))
    finally:
        try:
            container_run(runtime, ["rmi", "-f", tag], capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort cleanup; must never clobber the return value
    return results, image_size


def verify(
    dockerfile: str,
    project_path: Path,
    target: DeployTarget,
    runtime: ContainerRuntime | None,
    facts: ProjectFacts | None = None,
    *,
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
    health_timeout: int = DEFAULT_HEALTH_TIMEOUT,
    compose: str | None = None,
) -> VerificationReport:
    """Full verification: L1 static always; L2 docker when available and L1 passed.

    The timeouts bound the L2 build and healthcheck subprocesses (seconds).
    """
    if facts is not None:
        validate_target_against_facts(target, facts)
    elif target.extras or target.entrypoint is not None:
        raise TargetConfigError(
            "deploy target requires facts-based validation (extras or "
            "entrypoint) but no project facts were provided"
        )
    report = verify_static(dockerfile, project_path, facts, target=target)
    if target.dependencies:
        report.results.extend(_compose_l1_checks(compose, target))
    report.runtime = runtime
    if runtime is not None:
        report.docker_available = True
        if report.passed:
            docker_results, image_size = verify_docker(
                dockerfile,
                project_path,
                target,
                runtime,
                build_timeout=build_timeout,
                health_timeout=health_timeout,
            )
            report.results.extend(docker_results)
            report.image_size_bytes = image_size
    if target.run is not None and target.run.expect_stdout:
        marker = target.run.expect_stdout
        report.results = [
            r.model_copy(update={"message": _redact_oracle(r.message, marker)})
            for r in report.results
        ]
    return report
