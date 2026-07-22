"""Unit matrix for the run_completes job check (mocked container runtime)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

import deployer.verify as verify_mod
from deployer.models import (
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    RunSpec,
)
from deployer.verify import _redact_oracle, _run_completes

RUNTIME = ContainerRuntime(tool="docker")
MARKER = "hello from job"
CMD_FEEDBACK = 'ENTRYPOINT null, CMD ["python"]'


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_container_run(
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    inspect_stdout: str = CMD_FEEDBACK,
) -> None:
    """Route the foreground `run` to `outcome`; keep inspect/rm working."""

    def fake(runtime: ContainerRuntime, args: list[str], **kwargs: Any) -> Any:
        if args[0] == "image":
            return _proc(0, stdout=inspect_stdout)
        if args[0] == "rm":
            return _proc(0)
        assert args[0] == "run"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(verify_mod, "container_run", fake)


def _target(marker: str | None = MARKER) -> DeployTarget:
    return DeployTarget(run=RunSpec(expect_stdout=marker))


def test_exit_zero_without_oracle_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout=""))
    result = _run_completes(_target(marker=None), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.PASSED


def test_exit_zero_with_marker_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout=f"start\n{MARKER}\n"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.PASSED


def test_marker_in_stderr_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout="", stderr=MARKER))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING


def test_inert_cmd_exit_zero_missing_marker_is_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(0, stdout=""))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "container command" in result.message
    assert MARKER not in result.message


def test_nonzero_exit_is_authoring_with_output_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(
        monkeypatch, _proc(1, stderr="Traceback ...\nValueError: boom")
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "ValueError: boom" in result.message


def test_app_connection_refused_stays_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-case: app output must not trip broad ENVIRONMENT markers."""
    _patch_container_run(
        monkeypatch,
        _proc(1, stderr="ConnectionRefusedError: connection refused"),
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.AUTHORING


def test_cli_transport_failure_is_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(
        monkeypatch,
        _proc(125, stderr="error during connect: ssh tunnel died"),
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_exit_125_without_transport_marker_stays_authoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, _proc(125, stderr="invalid memory limit"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.AUTHORING


def test_timeout_is_authoring_and_names_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_container_run(monkeypatch, subprocess.TimeoutExpired(cmd="run", timeout=30))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.AUTHORING
    assert "did not exit within" in result.message
    assert "container command" in result.message


def test_oserror_is_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_container_run(monkeypatch, OSError("broken pipe"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_marker_printed_then_crash_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle leak path 1: program prints the marker, then fails."""
    _patch_container_run(monkeypatch, _proc(1, stdout=f"{MARKER}\n", stderr="boom"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert MARKER not in result.message
    assert "<redacted>" in result.message


def test_marker_in_command_feedback_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oracle leak path 2: an echo-CMD carries the marker into feedback."""
    _patch_container_run(
        monkeypatch,
        _proc(0, stdout="wrong output"),
        inspect_stdout=f'ENTRYPOINT null, CMD ["echo", "{MARKER}"]',
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert MARKER not in result.message


def test_redact_oracle_none_marker_is_noop() -> None:
    assert _redact_oracle("msg", None) == "msg"
    assert _redact_oracle("has secret", "secret") == "has <redacted>"


def test_verify_docker_dispatches_run_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a passed build, a run target triggers run_completes (not
    run_healthcheck)."""

    def fake(runtime: ContainerRuntime, args: list[str], **kwargs: Any) -> Any:
        if args[0] == "build":
            return _proc(0)
        if args[0] == "image":
            return _proc(0, stdout="123" if "Size" in args[3] else CMD_FEEDBACK)
        if args[0] == "run":
            return _proc(0, stdout=MARKER)
        return _proc(0)

    monkeypatch.setattr(verify_mod, "container_run", fake)
    results, _ = verify_mod.verify_docker(
        "FROM python:3.12-slim", tmp_path, _target(), RUNTIME
    )
    assert [r.check_id for r in results] == ["build", "run_completes"]
    assert all(r.status is CheckStatus.PASSED for r in results)
