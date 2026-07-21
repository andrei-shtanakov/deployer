from pathlib import Path

import pytest

from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    ServiceSpec,
)
from deployer.verify import (
    _classify,
    _isolated_context,
    _run_healthcheck,
    parse_dockerfile,
    verify,
    verify_docker,
    verify_static,
)

GOOD = """\
FROM python:3.12-slim
WORKDIR /app
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
"""

UV_STYLE = (
    "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
    'RUN uv sync --frozen\nCMD ["python", "main.py"]\n'
)
PIP_STYLE = (
    "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
    "RUN pip install --no-cache-dir -r requirements.txt\n"
    'CMD ["python", "main.py"]\n'
)


def _by_id(report, check_id: str):
    return next(r for r in report.results if r.check_id == check_id)


def test_parse_joins_continuations_and_skips_comments() -> None:
    text = "# comment\nFROM python:3.12-slim\nRUN echo a \\\n    && echo b\n"
    instructions = parse_dockerfile(text)
    assert instructions[0] == ("FROM", "python:3.12-slim")
    assert instructions[1][0] == "RUN"
    assert "echo b" in instructions[1][1]


def test_good_dockerfile_passes_static(hello_service: Path) -> None:
    report = verify_static(GOOD, hello_service)
    assert report.passed
    assert _by_id(report, "parses").status is CheckStatus.PASSED
    assert _by_id(report, "copy_sources").status is CheckStatus.PASSED


def test_missing_from_fails_as_authoring(hello_service: Path) -> None:
    report = verify_static("RUN echo hi\n", hello_service)
    check = _by_id(report, "parses")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_copy_of_nonexistent_file_fails(hello_service: Path) -> None:
    bad = GOOD.replace("COPY main.py .", "COPY nope.py .")
    report = verify_static(bad, hello_service)
    check = _by_id(report, "copy_sources")
    assert check.status is CheckStatus.FAILED
    assert "nope.py" in check.message


def test_copy_from_stage_is_ignored(hello_service: Path) -> None:
    multi = (
        "FROM python:3.12-slim AS build\n"
        "COPY main.py .\n"
        "FROM python:3.12-slim\n"
        "COPY --from=build /app/main.py .\n"
    )
    report = verify_static(multi, hello_service)
    assert _by_id(report, "copy_sources").status is CheckStatus.PASSED


def test_unpinned_base_image_warns(hello_service: Path) -> None:
    for base in ("FROM python\n", "FROM python:latest\n"):
        report = verify_static(base + "COPY main.py .\n", hello_service)
        assert _by_id(report, "base_pinned").status is CheckStatus.WARNING


def test_pinned_base_image_passes(hello_service: Path) -> None:
    report = verify_static(GOOD, hello_service)
    assert _by_id(report, "base_pinned").status is CheckStatus.PASSED


def test_base_pinned_skips_platform_flag(hello_service: Path) -> None:
    text = "FROM --platform=linux/amd64 python:3.12-slim\nCOPY main.py .\n"
    report = verify_static(text, hello_service)
    assert _by_id(report, "base_pinned").status is CheckStatus.PASSED

    unpinned = "FROM --platform=linux/amd64 python\nCOPY main.py .\n"
    report = verify_static(unpinned, hello_service)
    assert _by_id(report, "base_pinned").status is CheckStatus.WARNING


def test_hadolint_skipped_marks_non_comparable(
    hello_service: Path, monkeypatch
) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    report = verify_static(GOOD, hello_service)
    assert _by_id(report, "hadolint").status is CheckStatus.SKIPPED
    assert report.hadolint_available is False


def test_hadolint_timeout_degrades_to_skipped(hello_service: Path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/hadolint")

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hadolint", timeout=30)

    monkeypatch.setattr("deployer.verify.subprocess.run", _boom)
    report = verify_static(GOOD, hello_service)
    check = _by_id(report, "hadolint")
    assert check.status is CheckStatus.SKIPPED
    assert report.hadolint_available is False


def test_hadolint_garbage_output_degrades_to_skipped(
    hello_service: Path, monkeypatch
) -> None:
    import subprocess

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/hadolint")

    def _fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Haskell Dockerfile Linter 2.12.0", stderr=""
            )
        return subprocess.CompletedProcess(
            cmd, 1, stdout="hadolint: internal error", stderr=""
        )

    monkeypatch.setattr("deployer.verify.subprocess.run", _fake_run)
    report = verify_static(GOOD, hello_service)
    check = _by_id(report, "hadolint")
    assert check.status is CheckStatus.SKIPPED
    assert report.hadolint_available is False


def test_install_strategy_skipped_without_facts(hello_service: Path) -> None:
    report = verify_static(GOOD, hello_service)
    assert _by_id(report, "install_strategy").status is CheckStatus.SKIPPED


def test_pip_project_using_uv_fails(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="pip", has_build_system=False)
    report = verify_static(UV_STYLE, hello_service, facts)
    check = _by_id(report, "install_strategy")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_uv_project_using_pip_fails(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="uv", has_build_system=True)
    report = verify_static(PIP_STYLE, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


def test_no_build_system_project_install_fails(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="uv", has_build_system=False)
    report = verify_static(UV_STYLE, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


def test_no_install_project_flag_passes_per_line(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="uv", has_build_system=False)
    ok = UV_STYLE.replace(
        "RUN uv sync --frozen", "RUN uv sync --frozen --no-install-project"
    )
    report = verify_static(ok, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED

    mixed = ok + "RUN uv sync --frozen\n"  # second line installs the project
    report = verify_static(mixed, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


def test_matching_strategy_passes(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="pip", has_build_system=False)
    report = verify_static(PIP_STYLE, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


def test_echoed_uv_sync_string_does_not_trigger(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="pip", has_build_system=False)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
        'RUN echo "do not run uv sync here" && '
        "pip install --no-cache-dir -r requirements.txt\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


def test_python_m_pip_detected_in_uv_project(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="uv", has_build_system=True)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
        "RUN python -m pip install -r requirements.txt\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


def test_env_prefix_does_not_bypass_rules(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="uv", has_build_system=False)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
        "RUN UV_LINK_MODE=copy uv sync --frozen\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED

    facts_uv = ProjectFacts(package_manager="uv", has_build_system=True)
    dockerfile2 = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
        "RUN PIP_NO_CACHE_DIR=1 pip install -r requirements.txt\n"
        'CMD ["python", "main.py"]\n'
    )
    report2 = verify_static(dockerfile2, hello_service, facts_uv)
    assert _by_id(report2, "install_strategy").status is CheckStatus.FAILED


def test_echoed_python_m_pip_does_not_trigger(hello_service: Path) -> None:
    facts = ProjectFacts(package_manager="uv", has_build_system=True)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY main.py .\n"
        'RUN echo "python -m pip install nothing"\n'
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


def _spy_docker(captured: dict):
    """verify_docker replacement that records the timeout kwargs it got."""

    def spy(
        dockerfile, project_path, target, runtime, *, build_timeout, health_timeout
    ):
        captured["build_timeout"] = build_timeout
        captured["health_timeout"] = health_timeout
        return [CheckResult(check_id="build", status=CheckStatus.PASSED)], None

    return spy


def _skip_hadolint(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify._check_hadolint",
        lambda _: (
            CheckResult(check_id="hadolint", status=CheckStatus.SKIPPED),
            False,
        ),
    )


def test_verify_forwards_timeouts_to_verify_docker(
    hello_service: Path, monkeypatch
) -> None:
    _skip_hadolint(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("deployer.verify.verify_docker", _spy_docker(captured))
    report = verify(
        GOOD,
        hello_service,
        DeployTarget(),
        ContainerRuntime(tool="podman"),
        build_timeout=1200,
        health_timeout=45,
    )
    assert captured == {"build_timeout": 1200, "health_timeout": 45}
    assert report.docker_available


def test_verify_defaults_match_module_constants(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.verify import DEFAULT_BUILD_TIMEOUT, DEFAULT_HEALTH_TIMEOUT

    _skip_hadolint(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("deployer.verify.verify_docker", _spy_docker(captured))
    verify(GOOD, hello_service, DeployTarget(), ContainerRuntime(tool="podman"))
    assert captured == {
        "build_timeout": DEFAULT_BUILD_TIMEOUT,
        "health_timeout": DEFAULT_HEALTH_TIMEOUT,
    }
    assert DEFAULT_BUILD_TIMEOUT == 600
    assert DEFAULT_HEALTH_TIMEOUT == 30


@pytest.mark.parametrize(
    "line",
    [
        "u@host: Permission denied (publickey).",
        "Host key verification failed.",
        "ssh: Could not resolve hostname bench: nodename nor servname known",
        "ssh: connect to host 10.0.0.5 port 22: Operation timed out",
        "ssh: connect to host bench port 22: Connection refused",
        "Cannot connect to the Docker daemon at ssh://u@host. Is it running?",
        'error during connect: Get "http://docker.example": EOF',
        "Error: context deadline exceeded",
        "connection timed out",
    ],
)
def test_ssh_and_daemon_errors_are_environment(line: str) -> None:
    assert _classify(line) is FailureKind.ENVIRONMENT


def test_classify_sees_stdout_side_of_combined_output() -> None:
    combined = "error during connect: dial tcp: timeout\n" + ""
    assert _classify(combined) is FailureKind.ENVIRONMENT


def test_ordinary_build_error_stays_authoring() -> None:
    assert _classify("E: Unable to locate package libfoo") is FailureKind.AUTHORING


def test_isolated_context_excludes_secrets_and_junk(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "mod.py").write_text("x = 1\n")
    for junk in (".env", ".env.local"):
        (tmp_path / junk).write_text("SECRET=1\n")
    for junk_dir in (".git", ".venv", ".deployer", "__pycache__"):
        (tmp_path / junk_dir).mkdir()
        (tmp_path / junk_dir / "f").write_text("x")
    with _isolated_context(tmp_path) as ctx:
        assert ctx != tmp_path
        assert (ctx / "app.py").read_text() == "print('hi')\n"
        assert (ctx / "nested" / "mod.py").exists()
        assert not (ctx / ".env").exists()
        assert not (ctx / ".env.local").exists()
        for junk_dir in (".git", ".venv", ".deployer", "__pycache__"):
            assert not (ctx / junk_dir).exists()
    assert not ctx.exists()  # cleaned up on exit


def test_isolated_context_copies_dangling_symlink_as_link(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "dangling").symlink_to(tmp_path / "nope")
    with _isolated_context(tmp_path) as ctx:
        assert (ctx / "app.py").exists()
        assert (ctx / "dangling").is_symlink()
        assert not (ctx / "dangling").exists()  # target never resolves


# -- Fix 1: cleanup calls in `finally` must never clobber the return value --


def _fake_container_run(responses: dict):
    """container_run replacement dispatching on the CLI subcommand (args[0])."""

    def _run(runtime, args, **kwargs):
        head = args[0]
        if head not in responses:
            raise AssertionError(f"unexpected container_run call: {args}")
        behavior = responses[head]
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior

    return _run


def test_run_healthcheck_cleanup_timeout_does_not_clobber_result(
    monkeypatch,
) -> None:
    import subprocess

    responses = {
        "run": subprocess.CompletedProcess(["run"], 0, stdout="", stderr=""),
        "exec": subprocess.CompletedProcess(["exec"], 0, stdout="", stderr=""),
        "rm": subprocess.TimeoutExpired("rm", 1),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    target = DeployTarget(service=ServiceSpec(port=8000))
    result = _run_healthcheck(target, ContainerRuntime(tool="podman"), "tag", 5)
    assert result.status is CheckStatus.PASSED


def test_verify_docker_cleanup_timeout_does_not_clobber_result(
    hello_service: Path, monkeypatch
) -> None:
    import subprocess

    responses = {
        "build": subprocess.CompletedProcess(["build"], 0, stdout="", stderr=""),
        "image": subprocess.CompletedProcess(["image"], 0, stdout="1234", stderr=""),
        "rmi": subprocess.TimeoutExpired("rmi", 1),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    results, image_size = verify_docker(
        GOOD, hello_service, DeployTarget(), ContainerRuntime(tool="podman")
    )
    assert results[0].check_id == "build"
    assert results[0].status is CheckStatus.PASSED
    assert image_size == 1234


# -- Fix 2: mid-run transport loss during the healthcheck poll --


def _one_shot_clock(monkeypatch) -> None:
    """Make _run_healthcheck's poll loop run exactly one iteration, instantly."""
    import itertools

    values = itertools.chain([0.0, 0.0, 0.0], itertools.repeat(1000.0))
    monkeypatch.setattr("deployer.verify.time.monotonic", lambda: next(values))
    monkeypatch.setattr("deployer.verify.time.sleep", lambda _: None)


def test_transport_loss_during_poll_classifies_as_environment(monkeypatch) -> None:
    import subprocess

    _one_shot_clock(monkeypatch)
    responses = {
        "run": subprocess.CompletedProcess(["run"], 0, stdout="", stderr=""),
        "exec": subprocess.CompletedProcess(
            ["exec"], 1, stdout="", stderr="error during connect: EOF"
        ),
        "logs": subprocess.CompletedProcess(["logs"], 0, stdout="", stderr=""),
        "rm": subprocess.CompletedProcess(["rm"], 0, stdout="", stderr=""),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    target = DeployTarget(service=ServiceSpec(port=8000))
    result = _run_healthcheck(target, ContainerRuntime(tool="podman"), "tag", 5)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_in_container_traceback_stays_authoring(monkeypatch) -> None:
    import subprocess

    _one_shot_clock(monkeypatch)
    traceback_stderr = (
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "ConnectionRefusedError: [Errno 111] Connection refused"
    )
    responses = {
        "run": subprocess.CompletedProcess(["run"], 0, stdout="", stderr=""),
        "exec": subprocess.CompletedProcess(
            ["exec"], 1, stdout="", stderr=traceback_stderr
        ),
        "logs": subprocess.CompletedProcess(["logs"], 0, stdout="", stderr=""),
        "image": subprocess.CompletedProcess(["image"], 1, stdout="", stderr=""),
        "rm": subprocess.CompletedProcess(["rm"], 0, stdout="", stderr=""),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    target = DeployTarget(service=ServiceSpec(port=8000))
    result = _run_healthcheck(target, ContainerRuntime(tool="podman"), "tag", 5)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING


def test_transport_loss_via_stdout_during_poll_classifies_as_environment(
    monkeypatch,
) -> None:
    """Container CLIs may write transport errors to stdout, not just stderr."""
    import subprocess

    _one_shot_clock(monkeypatch)
    responses = {
        "run": subprocess.CompletedProcess(["run"], 0, stdout="", stderr=""),
        "exec": subprocess.CompletedProcess(
            ["exec"], 1, stdout="error during connect: EOF", stderr=""
        ),
        "logs": subprocess.CompletedProcess(["logs"], 0, stdout="", stderr=""),
        "rm": subprocess.CompletedProcess(["rm"], 0, stdout="", stderr=""),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    target = DeployTarget(service=ServiceSpec(port=8000))
    result = _run_healthcheck(target, ContainerRuntime(tool="podman"), "tag", 5)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


# -- Fix 3: OSError from the container CLI must classify as ENVIRONMENT --


def test_run_healthcheck_oserror_classifies_as_environment(monkeypatch) -> None:
    import subprocess

    responses = {
        "run": OSError("docker: command not found"),
        "rm": subprocess.CompletedProcess(["rm"], 0, stdout="", stderr=""),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    target = DeployTarget(service=ServiceSpec(port=8000))
    result = _run_healthcheck(target, ContainerRuntime(tool="podman"), "tag", 5)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT
    assert "docker: command not found" in result.message


def test_build_oserror_classifies_as_environment(
    hello_service: Path, monkeypatch
) -> None:
    import subprocess

    responses = {
        "build": OSError("no such file or directory: 'docker'"),
        "rmi": subprocess.CompletedProcess(["rmi"], 0, stdout="", stderr=""),
    }
    monkeypatch.setattr("deployer.verify.container_run", _fake_container_run(responses))
    results, image_size = verify_docker(
        GOOD, hello_service, DeployTarget(), ContainerRuntime(tool="podman")
    )
    assert results[0].check_id == "build"
    assert results[0].status is CheckStatus.FAILED
    assert results[0].failure_kind is FailureKind.ENVIRONMENT
    assert "no such file or directory" in results[0].message
    assert image_size is None


# -- Fix 4: healthcheck failure names the built image's ENTRYPOINT/CMD --


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    class P:
        returncode: int
        stdout: str
        stderr: str

    p = P()
    p.returncode, p.stdout, p.stderr = returncode, stdout, stderr
    return p


def _dispatch_with_inspect(inspect_out: str | None):
    """container_run fake: run -d ok, exec fails, image inspect configurable."""

    def fake(runtime, args, **kwargs):
        if args[0] == "run":
            return _fake_proc(0, stdout="cid")
        if args[0] == "exec":
            return _fake_proc(1, stderr="probe refused")
        if args[:2] == ["image", "inspect"]:
            if inspect_out is None:
                raise OSError("inspect exploded")
            return _fake_proc(0, stdout=inspect_out + "\n")
        if args[0] in ("logs", "rm"):
            return _fake_proc(0)
        raise AssertionError(f"unexpected container command: {args}")

    return fake


def test_healthcheck_failure_names_container_command(monkeypatch) -> None:
    monkeypatch.setattr(
        "deployer.verify.container_run",
        _dispatch_with_inspect('ENTRYPOINT null, CMD ["python3"]'),
    )
    result = _run_healthcheck(
        DeployTarget(service=ServiceSpec(port=8000)),
        ContainerRuntime(tool="docker"),
        "tag",
        timeout=1,
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert 'container command: ENTRYPOINT null, CMD ["python3"]' in result.message


def test_healthcheck_command_feedback_is_best_effort(monkeypatch) -> None:
    monkeypatch.setattr("deployer.verify.container_run", _dispatch_with_inspect(None))
    result = _run_healthcheck(
        DeployTarget(service=ServiceSpec(port=8000)),
        ContainerRuntime(tool="docker"),
        "tag",
        timeout=1,
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING  # not flipped
    assert "container command:" not in result.message
