from pathlib import Path

from deployer.models import CheckResult, CheckStatus, DeployTarget, ProjectFacts
from deployer.verify import parse_dockerfile, verify, verify_static

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

    def spy(dockerfile, project_path, target, tool, *, build_timeout, health_timeout):
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
        "podman",
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
    verify(GOOD, hello_service, DeployTarget(), "podman")
    assert captured == {
        "build_timeout": DEFAULT_BUILD_TIMEOUT,
        "health_timeout": DEFAULT_HEALTH_TIMEOUT,
    }
    assert DEFAULT_BUILD_TIMEOUT == 600
    assert DEFAULT_HEALTH_TIMEOUT == 30
