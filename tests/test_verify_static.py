from pathlib import Path
from typing import Literal

import pytest

from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    ServiceDependency,
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


# -- Task 3: L1 install-strategy rules for Poetry --


POETRY_FACTS = ProjectFacts(package_manager="poetry", has_build_system=True)

POETRY_GOOD = (
    "FROM python:3.12-slim AS builder\nWORKDIR /app\n"
    "RUN pip install --no-cache-dir poetry==2.4.1\n"
    "RUN poetry install --no-root --only main --no-interaction --no-ansi\n"
    "FROM python:3.12-slim\nWORKDIR /app\n"
    'CMD ["python", "main.py"]\n'
)


def _poetry_report(hello_service: Path, run_line: str):
    dockerfile = POETRY_GOOD.replace(
        "RUN pip install --no-cache-dir poetry==2.4.1", run_line
    )
    return verify_static(dockerfile, hello_service, POETRY_FACTS)


def test_poetry_pinned_bootstrap_passes(hello_service: Path) -> None:
    report = verify_static(POETRY_GOOD, hello_service, POETRY_FACTS)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


@pytest.mark.parametrize(
    "line",
    ["RUN pip install poetry", "RUN pip install 'poetry>=1.8'"],
)
def test_poetry_unpinned_bootstrap_warns(hello_service: Path, line: str) -> None:
    report = _poetry_report(hello_service, line)
    check = _by_id(report, "install_strategy")
    assert check.status is CheckStatus.WARNING
    assert "pin" in check.message or "==" in check.message


@pytest.mark.parametrize(
    "line",
    [
        "RUN pip install -r requirements.txt",
        "RUN pip install .",
        "RUN pip install flask",
        "RUN pip install poetry==2.4.1 flask",
        "RUN pip3 install flask",
        "RUN python -m pip install flask",
        "RUN python3 -m pip install flask",
        "RUN uv sync --frozen",
        "RUN uv pip install flask",
    ],
)
def test_poetry_project_direct_dep_install_fails(
    hello_service: Path, line: str
) -> None:
    report = _poetry_report(hello_service, line)
    check = _by_id(report, "install_strategy")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"


def test_poetry_bootstrap_with_index_url_passes(hello_service: Path) -> None:
    line = (
        "RUN pip install --index-url https://pypi.internal/simple "
        "--no-cache-dir poetry==2.4.1"
    )
    report = _poetry_report(hello_service, line)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


@pytest.mark.parametrize(
    "line",
    [
        "RUN /usr/bin/pip install flask",
        "RUN ./.venv/bin/pip3 install flask",
        "RUN pip install --index-url https://pypi.internal/simple flask",
    ],
)
def test_poetry_project_pip_variants_still_fail(hello_service: Path, line: str) -> None:
    report = _poetry_report(hello_service, line)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


@pytest.mark.parametrize("manager", ["uv", "pip"])
def test_non_poetry_project_poetry_install_fails(
    hello_service: Path, manager: Literal["uv", "pip"]
) -> None:
    facts = ProjectFacts(package_manager=manager, has_build_system=True)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\n"
        "RUN poetry install --no-root\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.FAILED


def test_pip_project_poetry_bootstrap_alone_is_not_flagged(
    hello_service: Path,
) -> None:
    facts = ProjectFacts(package_manager="pip", has_build_system=False)
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\n"
        "RUN pip install poetry==2.4.1\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, facts)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


def test_pip_install_hyphenated_flag_does_not_crash(hello_service: Path) -> None:
    """`pip install-e .` matches _PIP_INSTALL's `\\b` but has no standalone
    "install" token; _pip_install_payload must not raise ValueError."""
    dockerfile = (
        "FROM python:3.12-slim AS builder\nWORKDIR /app\n"
        "RUN pip install-e .\n"
        "FROM python:3.12-slim\nWORKDIR /app\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, POETRY_FACTS)
    assert _by_id(report, "install_strategy").status is CheckStatus.PASSED


@pytest.mark.parametrize(
    "line,expected_status",
    [
        ("RUN python -m pip install poetry==2.4.1", CheckStatus.PASSED),
        ("RUN python -m pip install poetry", CheckStatus.WARNING),
    ],
)
def test_python_m_pip_poetry_bootstrap_forms(
    hello_service: Path, line: str, expected_status: CheckStatus
) -> None:
    dockerfile = (
        f"FROM python:3.12-slim AS builder\nWORKDIR /app\n{line}\n"
        "RUN poetry install --no-root --only main\n"
        "FROM python:3.12-slim\nWORKDIR /app\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, POETRY_FACTS)
    assert _by_id(report, "install_strategy").status is expected_status


def test_unpinned_bootstrap_warning_is_deduped(hello_service: Path) -> None:
    dockerfile = (
        "FROM python:3.12-slim AS builder\nWORKDIR /app\n"
        "RUN pip install poetry\n"
        "RUN pip install poetry\n"
        "RUN poetry install --no-root --only main\n"
        "FROM python:3.12-slim\nWORKDIR /app\n"
        'CMD ["python", "main.py"]\n'
    )
    report = verify_static(dockerfile, hello_service, POETRY_FACTS)
    check = _by_id(report, "install_strategy")
    assert check.status is CheckStatus.WARNING
    assert check.message.count("poetry bootstrap is not pinned") == 1


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


# -- Fix (review): ENVIRONMENT start failure must never reach image inspect --


def test_environment_start_failure_never_calls_image_inspect(monkeypatch) -> None:
    """run -d failing with a daemon-unreachable error classifies as
    ENVIRONMENT and must short-circuit before the command-feedback path,
    which would otherwise call `image inspect`.
    """

    def fake(runtime, args, **kwargs):
        if args[0] == "run":
            return _fake_proc(1, stderr="cannot connect to the docker daemon")
        if args[:2] == ["image", "inspect"]:
            raise AssertionError("image inspect must not be called")
        if args[0] == "rm":
            return _fake_proc(0)
        raise AssertionError(f"unexpected container command: {args}")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    result = _run_healthcheck(
        DeployTarget(service=ServiceSpec(port=8000)),
        ContainerRuntime(tool="docker"),
        "tag",
        timeout=1,
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT
    assert "container command:" not in result.message


# -- Fix 5: UnicodeDecodeError in image-command feedback --


def test_image_command_unicode_decode_error_swallowed(monkeypatch) -> None:
    """UnicodeDecodeError during image inspect must be swallowed, not escape."""

    def fake(runtime, args, **kwargs):
        if args[0] == "run":
            return _fake_proc(0, stdout="cid")
        if args[0] == "exec":
            return _fake_proc(1, stderr="probe refused")
        if args[:2] == ["image", "inspect"]:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "x")
        if args[0] in ("logs", "rm"):
            return _fake_proc(0)
        raise AssertionError(f"unexpected container command: {args}")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    result = _run_healthcheck(
        DeployTarget(service=ServiceSpec(port=8000)),
        ContainerRuntime(tool="docker"),
        "tag",
        timeout=1,
    )
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "container command:" not in result.message


# -- Task 3: L1 check entrypoint_in_command --


def _entry_target() -> DeployTarget:
    return DeployTarget(entrypoint="app.py")


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "main.py").write_text("y = 2\n")
    return tmp_path


def test_entrypoint_check_absent_without_intent(tmp_path: Path) -> None:
    df = 'FROM python:3.12-slim\nCOPY app.py .\nCMD ["python", "app.py"]\n'
    report = verify_static(df, _project(tmp_path))
    assert all(r.check_id != "entrypoint_in_command" for r in report.results)


def test_entrypoint_in_exec_cmd_passes(tmp_path: Path) -> None:
    df = 'FROM python:3.12-slim\nCOPY app.py .\nCMD ["python", "app.py"]\n'
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_entrypoint_in_shell_cmd_passes(tmp_path: Path) -> None:
    df = "FROM python:3.12-slim\nCOPY app.py .\nCMD python app.py\n"
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_entrypoint_in_entrypoint_with_args_cmd_passes(tmp_path: Path) -> None:
    df = (
        "FROM python:3.12-slim\n"
        "COPY app.py .\n"
        'ENTRYPOINT ["python", "app.py"]\n'
        'CMD ["--port", "8000"]\n'
    )
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_scripts_name_entrypoint_matches(tmp_path: Path) -> None:
    # scripts names are not files: the project dir needs no "serve"
    df = 'FROM python:3.12-slim\nCMD ["serve"]\n'
    report = verify_static(
        df, _project(tmp_path), target=DeployTarget(entrypoint="serve")
    )
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.PASSED


def test_wrong_cmd_fails_with_both_named(tmp_path: Path) -> None:
    df = 'FROM python:3.12-slim\nCOPY main.py .\nCMD ["python", "main.py"]\n'
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    check = _by_id(report, "entrypoint_in_command")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "app.py" in check.message
    assert "main.py" in check.message


def test_no_command_in_final_stage_fails(tmp_path: Path) -> None:
    df = "FROM python:3.12-slim\nCOPY app.py .\n"
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    check = _by_id(report, "entrypoint_in_command")
    assert check.status is CheckStatus.FAILED
    assert "none" in check.message


def test_builder_stage_cmd_does_not_satisfy_entrypoint(tmp_path: Path) -> None:
    """The spec-review blocker case: a builder-stage CMD must not
    false-pass when the final stage sets no command."""
    df = (
        "FROM python:3.12-slim AS build\n"
        'CMD ["python", "app.py"]\n'
        "FROM python:3.12-slim\n"
        "COPY app.py .\n"
    )
    report = verify_static(df, _project(tmp_path), target=_entry_target())
    assert _by_id(report, "entrypoint_in_command").status is CheckStatus.FAILED


COMPOSE_TARGET = DeployTarget(
    service=ServiceSpec(port=8000),
    env={"REDIS_URL": "redis://cache:6379/0"},
    dependencies=[ServiceDependency(name="cache", image="redis:7-alpine")],
)

COMPOSE_GOOD = """\
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      REDIS_URL: redis://cache:6379/0
    depends_on:
      cache:
        condition: service_healthy
  cache:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
"""


def _compose_checks(compose: str | None):
    from deployer.verify import _compose_l1_checks

    return {r.check_id: r for r in _compose_l1_checks(compose, COMPOSE_TARGET)}


def test_compose_good_passes_all_l1() -> None:
    checks = _compose_checks(COMPOSE_GOOD)
    for check_id in (
        "compose_present",
        "compose_parses",
        "compose_services",
        "compose_wiring",
    ):
        assert checks[check_id].status is CheckStatus.PASSED, check_id


def test_compose_missing_artifact_fails_present() -> None:
    checks = _compose_checks(None)
    check = checks["compose_present"]
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "compose_parses" not in checks  # later checks not attempted


def test_compose_unparseable_yaml_fails() -> None:
    checks = _compose_checks("services: [unclosed")
    assert checks["compose_parses"].status is CheckStatus.FAILED


def test_compose_non_mapping_shapes_fail() -> None:
    for text in ("- a\n- b", "services: []", "services:\n  app: []"):
        checks = _compose_checks(text)
        assert checks["compose_parses"].status is CheckStatus.FAILED, text


def test_compose_wrong_service_set_fails() -> None:
    missing_dep = COMPOSE_GOOD.replace("  cache:\n    image: redis:7-alpine\n", "")
    extra = COMPOSE_GOOD + "  rogue:\n    image: nginx:1.27\n"
    for text in (missing_dep, extra):
        checks = _compose_checks(text)
        assert checks["compose_services"].status is CheckStatus.FAILED, text


def test_compose_image_mismatch_fails() -> None:
    checks = _compose_checks(COMPOSE_GOOD.replace("redis:7-alpine", "redis:6"))
    assert checks["compose_services"].status is CheckStatus.FAILED


def test_compose_app_build_shapes() -> None:
    short_form = COMPOSE_GOOD.replace(
        "    build:\n      context: .\n      dockerfile: Dockerfile\n",
        "    build: .\n",
    )
    assert _compose_checks(short_form)["compose_services"].status is (
        CheckStatus.PASSED
    )
    wrong_context = COMPOSE_GOOD.replace("context: .", "context: ./src")
    assert _compose_checks(wrong_context)["compose_services"].status is (
        CheckStatus.FAILED
    )


def test_compose_missing_healthcheck_fails_wiring() -> None:
    text = COMPOSE_GOOD.replace(
        '    healthcheck:\n      test: ["CMD", "redis-cli", "ping"]\n'
        "      interval: 2s\n",
        "",
    )
    assert _compose_checks(text)["compose_wiring"].status is CheckStatus.FAILED


def test_compose_depends_on_needs_condition() -> None:
    list_form = COMPOSE_GOOD.replace(
        "    depends_on:\n      cache:\n        condition: service_healthy\n",
        "    depends_on: [cache]\n",
    )
    assert _compose_checks(list_form)["compose_wiring"].status is CheckStatus.FAILED


def test_compose_missing_env_key_fails_wiring() -> None:
    text = COMPOSE_GOOD.replace(
        "    environment:\n      REDIS_URL: redis://cache:6379/0\n", ""
    )
    assert _compose_checks(text)["compose_wiring"].status is CheckStatus.FAILED


def test_compose_env_list_form_accepted() -> None:
    text = COMPOSE_GOOD.replace(
        "    environment:\n      REDIS_URL: redis://cache:6379/0\n",
        "    environment:\n      - REDIS_URL=redis://cache:6379/0\n",
    )
    assert _compose_checks(text)["compose_wiring"].status is CheckStatus.PASSED


def test_compose_ports_forbidden_everywhere() -> None:
    on_app = COMPOSE_GOOD.replace(
        "    environment:", '    ports:\n      - "8000:8000"\n    environment:'
    )
    on_dep = COMPOSE_GOOD.replace(
        "    image: redis:7-alpine",
        '    image: redis:7-alpine\n    ports:\n      - "6379:6379"',
    )
    for text in (on_app, on_dep):
        checks = _compose_checks(text)
        assert checks["compose_wiring"].status is CheckStatus.FAILED, text


def test_compose_network_escape_hatches_forbidden() -> None:
    host_mode = COMPOSE_GOOD.replace(
        "    environment:", "    network_mode: host\n    environment:"
    )
    external_net = COMPOSE_GOOD.replace(
        "    image: redis:7-alpine",
        "    image: redis:7-alpine\n    networks:\n      - outside",
    )
    for text in (host_mode, external_net):
        checks = _compose_checks(text)
        check = checks["compose_wiring"]
        assert check.status is CheckStatus.FAILED, text
        assert "internal-only" in check.message


def test_verify_appends_compose_checks_for_deps_target(hello_service: Path) -> None:
    report = verify(GOOD, hello_service, COMPOSE_TARGET, None, compose=COMPOSE_GOOD)
    ids = [r.check_id for r in report.results]
    assert "compose_parses" in ids and "compose_wiring" in ids

    plain = verify(GOOD, hello_service, DeployTarget(), None)
    assert "compose_present" not in [r.check_id for r in plain.results]


# -- Task 4: compose L2 — up/exec-probe/down (fake-driven, no marker) --


def _assert_both_compose_files(calls: list[list[str]]) -> None:
    """Every compose invocation must carry both `-f` entries.

    The base-list construction (`compose -p <project> -f compose.yaml -f
    deployer.override.yaml`) is shared by every subcommand, so this is one
    assertion on any recorded call — it also covers the egress-sandbox
    override landing on the command line.
    """
    for call in calls:
        if call[0] != "compose":
            continue
        f_values = [call[i + 1] for i, tok in enumerate(call) if tok == "-f"]
        assert len(f_values) == 2, call
        assert f_values[0].endswith("compose.yaml"), call
        assert f_values[1].endswith("deployer.override.yaml"), call


def test_verify_compose_up_probe_down_sequence(monkeypatch, tmp_path: Path) -> None:
    import subprocess

    from deployer.verify import _verify_compose

    calls: list[list[str]] = []

    def fake(runtime, args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    results = _verify_compose(
        "FROM python:3.12-slim",
        COMPOSE_GOOD,
        tmp_path,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        build_timeout=60,
        health_timeout=5,
    )
    by_id = {r.check_id: r for r in results}
    assert by_id["compose_up"].status is CheckStatus.PASSED
    assert by_id["compose_healthcheck"].status is CheckStatus.PASSED
    assert any("up" in c for c in calls if c[0] == "compose")
    project_flags = {c[c.index("-p") + 1] for c in calls if "-p" in c}
    assert len(project_flags) == 1  # one unique project name throughout
    project = next(iter(project_flags))
    assert project.startswith("deployer-verify-")
    _assert_both_compose_files(calls)
    exec_call = next(c for c in calls if "exec" in c)
    url = "http://127.0.0.1:8000/health"
    assert exec_call[-1] == (
        f"import urllib.request; urllib.request.urlopen({url!r}, timeout=2)"
    )
    down_call, image_rm_call = calls[-2], calls[-1]
    assert down_call[:1] == ["compose"] and "down" in down_call  # teardown
    assert "-v" in down_call
    assert image_rm_call[:3] == ["image", "rm", "-f"]  # image cleanup follows down
    assert f"{project}-app" in image_rm_call
    assert f"{project}_app" in image_rm_call


def test_verify_compose_up_failure_classifies_and_still_tears_down(
    monkeypatch, tmp_path: Path
) -> None:
    import subprocess

    from deployer.verify import _verify_compose

    calls: list[list[str]] = []

    def fake(runtime, args, **kwargs):
        calls.append(args)
        if "up" in args:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="build failed: syntax error"
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    results = _verify_compose(
        "FROM python:3.12-slim",
        COMPOSE_GOOD,
        tmp_path,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        build_timeout=60,
        health_timeout=5,
    )
    by_id = {r.check_id: r for r in results}
    assert by_id["compose_up"].status is CheckStatus.FAILED
    assert by_id["compose_up"].failure_kind == "authoring"
    assert "compose_healthcheck" not in by_id
    _assert_both_compose_files(calls)
    assert "down" in calls[-2]  # teardown ran despite failure
    assert calls[-1][:3] == ["image", "rm", "-f"]  # image cleanup follows down


def test_verify_compose_probe_failure_collects_logs(
    monkeypatch, tmp_path: Path
) -> None:
    import subprocess

    from deployer.verify import _verify_compose

    calls: list[list[str]] = []

    def fake(runtime, args, **kwargs):
        calls.append(args)
        if "exec" in args:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="urlopen error"
            )
        if "logs" in args:
            return subprocess.CompletedProcess(
                args, 0, stdout="app exploded here", stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.container_run", fake)
    results = _verify_compose(
        "FROM python:3.12-slim",
        COMPOSE_GOOD,
        tmp_path,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        build_timeout=60,
        health_timeout=2,
    )
    _assert_both_compose_files(calls)
    by_id = {r.check_id: r for r in results}
    check = by_id["compose_healthcheck"]
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "authoring"
    assert "app exploded here" in check.message


def test_verify_missing_compose_provider_is_environment_failure(
    monkeypatch, hello_service: Path
) -> None:
    monkeypatch.setattr("deployer.verify.compose_available", lambda rt: False)
    report = verify(
        GOOD,
        hello_service,
        COMPOSE_TARGET,
        ContainerRuntime(tool="podman"),
        compose=COMPOSE_GOOD,
    )
    check = next(r for r in report.results if r.check_id == "compose_available")
    assert check.status is CheckStatus.FAILED
    assert check.failure_kind == "environment"


GOOD_SHA = "a" * 40

CI_GOOD = f"""\
name: ci
on:
  push:
  pull_request:
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@{GOOD_SHA}
      - run: docker build --file ./Dockerfile .
"""


def _ci_checks(ci: str | None):
    from deployer.verify import _ci_l1_checks

    results, _ = _ci_l1_checks(ci)
    return {r.check_id: r for r in results}


def test_ci_good_passes_own_checks(monkeypatch) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    checks = _ci_checks(CI_GOOD)
    for check_id in ("ci_present", "ci_parses", "ci_wiring", "ci_pinned"):
        assert checks[check_id].status is CheckStatus.PASSED, check_id
    assert checks["actionlint"].status is CheckStatus.SKIPPED


def test_ci_missing_artifact_fails_present_and_skips_rest() -> None:
    checks = _ci_checks(None)
    assert checks["ci_present"].status is CheckStatus.FAILED
    assert checks["ci_present"].failure_kind == "authoring"
    for dep in ("ci_parses", "ci_wiring", "ci_pinned", "actionlint"):
        assert checks[dep].status is CheckStatus.SKIPPED, dep


def test_ci_unparseable_fails_parses_and_skips_dependents() -> None:
    checks = _ci_checks("on: [unclosed")
    assert checks["ci_parses"].status is CheckStatus.FAILED
    for dep in ("ci_wiring", "ci_pinned", "actionlint"):
        assert checks[dep].status is CheckStatus.SKIPPED, dep


def test_ci_on_true_key_normalized(monkeypatch) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    # unquoted `on:` -> YAML 1.1 boolean True key; must still parse+wire
    checks = _ci_checks(CI_GOOD)  # CI_GOOD's `on:` IS the True key
    assert checks["ci_parses"].status is CheckStatus.PASSED
    assert checks["ci_wiring"].status is CheckStatus.PASSED


def test_ci_both_on_keys_ambiguous_fails() -> None:
    text = CI_GOOD.replace("name: ci", 'name: ci\n"on":\n  push:')
    checks = _ci_checks(text)
    assert checks["ci_parses"].status is CheckStatus.FAILED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("  pull_request:\n", ""),  # missing trigger
        lambda t: t.replace("pull_request", "pull_request_target"),
        lambda t: t.replace("--file ./Dockerfile", "--file /abs/Dockerfile"),
        lambda t: t.replace("--file ./Dockerfile", "--file other.Dockerfile"),
        lambda t: t.replace(
            "      - uses: actions/checkout@" + GOOD_SHA + "\n"
            "      - run: docker build --file ./Dockerfile .",
            "      - run: docker build --file ./Dockerfile .\n"
            "      - uses: actions/checkout@" + GOOD_SHA,
        ),  # checkout after build
        lambda t: t + "      - run: docker push ghcr.io/x/y\n",
        lambda t: t.replace(
            "docker build --file ./Dockerfile .",
            "docker buildx build --push --file ./Dockerfile .",
        ),
        lambda t: t + "      - run: docker login ghcr.io\n",
        lambda t: t + f"      - uses: docker/login-action@{GOOD_SHA}\n",
        lambda t: t + "      - run: echo ${{ secrets.TOKEN }}\n",
        lambda t: (
            t + f"      - uses: docker/build-push-action@{GOOD_SHA}\n"
            "        with:\n"
            "          push: true\n"
        ),
        lambda t: (
            t + f"      - uses: docker/build-push-action@{GOOD_SHA}\n"
            "        with:\n"
            '          push: "true"\n'
        ),
        lambda t: t.replace(
            "docker build --file ./Dockerfile .",
            "docker buildx build --push=true --file ./Dockerfile .",
        ),
        lambda t: (
            t
            + (
                "      - run: true && docker buildx build --push --file "
                "./Dockerfile .\n"
            )
        ),
        lambda t: t + "      - run: docker image push x\n",
        lambda t: t + "      - run: podman push x\n",
        lambda t: t + "      - run: echo ${{ secrets['TOKEN'] }}\n",
        lambda t: (
            t + f"      - uses: docker/build-push-action@{GOOD_SHA}\n"
            "        with:\n"
            "          push: ${{ github.event_name != 'pull_request' }}\n"
        ),
        lambda t: (
            t + f"      - uses: docker/build-push-action@{GOOD_SHA}\n"
            "        with:\n"
            '          push: "yes"\n'
        ),
    ],
)
def test_ci_wiring_negatives(mutate) -> None:
    checks = _ci_checks(mutate(CI_GOOD))
    assert checks["ci_wiring"].status is CheckStatus.FAILED


@pytest.mark.parametrize(
    "with_push_line",
    [
        "          push: false\n",
        '          push: "false"\n',
    ],
)
def test_ci_wiring_push_explicitly_false_passes(with_push_line: str) -> None:
    text = (
        CI_GOOD + f"      - uses: docker/build-push-action@{GOOD_SHA}\n"
        "        with:\n" + with_push_line
    )
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.PASSED


@pytest.mark.parametrize(
    "build_cmd",
    [
        "docker build .",
        "docker build -f Dockerfile .",
        "docker build -f ./Dockerfile .",
        "docker build --file=./Dockerfile .",
    ],
)
def test_ci_wiring_accepts_build_forms(build_cmd: str) -> None:
    text = CI_GOOD.replace("docker build --file ./Dockerfile .", build_cmd)
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.PASSED


def test_ci_push_trigger_does_not_trip_push_rule() -> None:
    assert _ci_checks(CI_GOOD)["ci_wiring"].status is CheckStatus.PASSED


def test_ci_multiline_run_scalar_parsed() -> None:
    text = CI_GOOD.replace(
        "      - run: docker build --file ./Dockerfile .",
        "      - run: |\n"
        "          # build the image\n"
        "          docker build --file ./Dockerfile .",
    )
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.PASSED


def test_ci_checkout_and_build_must_share_a_job() -> None:
    text = CI_GOOD.replace(
        "      - run: docker build --file ./Dockerfile .",
        "",
    ) + (
        "  build2:\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n"
        "      - run: docker build --file ./Dockerfile .\n"
    )
    assert _ci_checks(text)["ci_wiring"].status is CheckStatus.FAILED


@pytest.mark.parametrize(
    "uses",
    [
        "actions/checkout@v5",
        "actions/checkout@main",
        "actions/checkout",
        "./local-action",
        "docker://alpine:3.20",
    ],
)
def test_ci_pinned_rejects_non_sha_refs(uses: str) -> None:
    text = CI_GOOD.replace(f"actions/checkout@{GOOD_SHA}", uses)
    assert _ci_checks(text)["ci_pinned"].status is CheckStatus.FAILED


def test_ci_pinned_accepts_remote_sha() -> None:
    assert _ci_checks(CI_GOOD)["ci_pinned"].status is CheckStatus.PASSED


def test_actionlint_version_mismatch_skips_without_running(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/actionlint")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="9.9.9", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    checks = _ci_checks(CI_GOOD)
    assert checks["actionlint"].status is CheckStatus.SKIPPED
    assert len(calls) == 1  # only --version; the linter itself never ran


def test_actionlint_runs_against_real_workflow_path(monkeypatch) -> None:
    import subprocess

    from deployer.verify import ACTIONLINT_VERSION, _ci_l1_checks

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/actionlint")
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        if "--version" in cmd or "-version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=ACTIONLINT_VERSION, stderr=""
            )
        seen.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    results, available = _ci_l1_checks(CI_GOOD)
    assert available is True
    assert seen and seen[0].endswith(".github/workflows/ci.yml")


def test_verify_appends_ci_checks_only_for_ci_target(hello_service: Path) -> None:
    from deployer.models import CISpec

    target = DeployTarget(ci=CISpec())
    report = verify(GOOD, hello_service, target, None, ci=CI_GOOD)
    ids = [r.check_id for r in report.results]
    assert "ci_wiring" in ids and "ci_pinned" in ids

    plain = verify(GOOD, hello_service, DeployTarget(), None)
    assert "ci_present" not in [r.check_id for r in plain.results]


def test_verify_reports_actionlint_unavailable_via_public_api(
    hello_service: Path, monkeypatch
) -> None:
    from deployer.models import CISpec

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    target = DeployTarget(ci=CISpec())
    report = verify(GOOD, hello_service, target, None, ci=CI_GOOD)
    assert report.actionlint_available is False
    assert _by_id(report, "actionlint").status is CheckStatus.SKIPPED


def test_verify_reports_actionlint_available_via_public_api(
    hello_service: Path, monkeypatch
) -> None:
    import subprocess

    from deployer.models import CISpec
    from deployer.verify import ACTIONLINT_VERSION

    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: "/usr/bin/actionlint")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd or "-version" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=ACTIONLINT_VERSION, stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("deployer.verify.subprocess.run", fake_run)
    target = DeployTarget(ci=CISpec())
    report = verify(GOOD, hello_service, target, None, ci=CI_GOOD)
    assert report.actionlint_available is True
