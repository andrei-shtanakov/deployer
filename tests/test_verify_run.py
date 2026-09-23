"""Unit matrix for the run_completes job check (mocked container runtime)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

import deployer.verify as verify_mod
from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
    RunSpec,
)
from deployer.verify import (
    _classify_exit,
    _redact_oracle,
    _run_completes,
    verify,
)

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


def test_nonzero_exit_without_markers_is_unknown_with_output_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare traceback carries no marker at all, so exit 1 alone does not
    establish a cause — the honest class is UNKNOWN, with the raw output
    still surfaced in the message."""
    _patch_container_run(
        monkeypatch, _proc(1, stderr="Traceback ...\nValueError: boom")
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert result.failure_kind is FailureKind.UNKNOWN
    assert "ValueError: boom" in result.message


def test_app_connection_refused_is_unknown_not_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-case: app output must not trip broad ENVIRONMENT markers.

    "connection refused" here is the app's own traceback text (its port
    refusing a connection), not container-runtime transport loss — and
    exit 1 is outside the 125/126 range `_classify_exit` trusts for a
    transport marker. Nothing else on this path establishes a class
    either, so UNKNOWN is the honest answer."""
    _patch_container_run(
        monkeypatch,
        _proc(1, stderr="ConnectionRefusedError: connection refused"),
    )
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.UNKNOWN


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


def test_exit_125_without_transport_marker_or_evidence_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "invalid memory limit" is neither a transport marker nor other
    positive evidence of an authoring cause, so the honest class is
    UNKNOWN."""
    _patch_container_run(monkeypatch, _proc(125, stderr="invalid memory limit"))
    result = _run_completes(_target(), RUNTIME, "tag", 30)
    assert result.failure_kind is FailureKind.UNKNOWN


def _run_completes_result(returncode: int, output: str) -> CheckResult:
    """Build the CheckResult the 125/126 branch returns for a given
    returncode/output, without wiring monkeypatch into each call site."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_container_run(mp, _proc(returncode, stderr=output))
        return _run_completes(_target(), RUNTIME, "tag", 30)


def test_125_without_transport_marker_and_without_evidence_is_unknown() -> None:
    """Negative case: the exit code alone does not establish a class."""
    result = _run_completes_result(returncode=125, output="something went wrong")
    assert result.failure_kind is FailureKind.UNKNOWN


def test_125_with_transport_marker_is_environment() -> None:
    """Positive twin A: a known transport cause keeps ENVIRONMENT."""
    result = _run_completes_result(
        returncode=125, output="cannot connect to the docker daemon"
    )
    assert result.failure_kind is FailureKind.ENVIRONMENT


def test_125_with_a_container_runtime_shape_is_unknown() -> None:
    """Owner's rule (2026-09-22): a container-runtime shape alone does not
    establish AUTHORING. `exec: … no such file` proves a missing executable,
    not that its name came from the authored CMD/ENTRYPOINT — `container_run`
    can be handed an overriding command. Nothing on this path binds the
    message to the artifact, so the honest class is UNKNOWN."""
    result = _run_completes_result(
        returncode=125, output='exec: "/app/start": stat /app/start: no such file'
    )
    assert result.failure_kind is FailureKind.UNKNOWN


def test_exit_1_without_markers_is_unknown() -> None:
    """Headline case: the second AUTHORING fallthrough, outside 125/126.
    An ordinary exit 1 with no citable evidence is UNKNOWN, not AUTHORING."""
    result = _run_completes_result(returncode=1, output="something went wrong")
    assert result.failure_kind is FailureKind.UNKNOWN


def test_exit_1_with_a_container_runtime_shape_is_unknown() -> None:
    """The same demotion outside 125/126: no general symptom classifies."""
    result = _run_completes_result(
        returncode=1, output='exec: "/app/start": stat /app/start: no such file'
    )
    assert result.failure_kind is FailureKind.UNKNOWN


def test_exit_137_without_markers_is_unknown() -> None:
    """The rule is not special-cased to exit 1: any nonzero exit without
    positive evidence is UNKNOWN."""
    result = _run_completes_result(returncode=137, output="something went wrong")
    assert result.failure_kind is FailureKind.UNKNOWN


def test_exit_1_with_transport_marker_is_not_environment() -> None:
    """Decision: the transport-marker check stays gated to 125/126 — those
    are the container-runtime CLI's own reserved codes for "failed to start
    the job". Outside that range the exit code came from the process under
    test, so a transport-shaped string is more likely the app's own output
    (as in test_app_connection_refused_is_unknown_not_environment) than
    real transport loss. A transport marker on exit 1 is therefore UNKNOWN,
    not ENVIRONMENT — and no other branch of `_classify_exit` claims it."""
    result = _run_completes_result(
        returncode=1, output="cannot connect to the docker daemon"
    )
    assert result.failure_kind is FailureKind.UNKNOWN


# --- `_classify_exit` directly: only the transport branch establishes a class -
# The owner's rule of 2026-09-22, at the function it constrains. A general
# symptom in the run output is not bound to the authored artifact on this path:
# `_run_completes` runs the image's own default command, but nothing in the
# OUTPUT says so — the identical line is printed when the command is overridden
# — so the shape cannot be read back as a defect of the CMD/ENTRYPOINT.
# `_classify_build` keeps its AUTHORING branch because it has that binding:
# it weighs the Dockerfile it built alongside the message.


def test_classify_exit_entrypoint_shape_is_unknown() -> None:
    """The shape that used to read as AUTHORING on its own."""
    assert (
        _classify_exit(1, 'exec: "app": executable file not found in $PATH')
        is FailureKind.UNKNOWN
    )


def test_classify_exit_no_such_file_is_unknown() -> None:
    """A missing file names no owner: artifact, project, environment and
    invocation all print it."""
    assert _classify_exit(127, "no such file") is FailureKind.UNKNOWN


def test_classify_exit_exec_format_error_is_unknown() -> None:
    """An amd64 image on an arm64 runner is an environment mismatch as
    readily as a wrong `--platform` in the Dockerfile."""
    assert _classify_exit(126, "exec format error") is FailureKind.UNKNOWN


def test_classify_exit_transport_marker_on_125_is_environment() -> None:
    """The one branch that survives: the runtime CLI's own reserved codes
    plus its own transport marker."""
    assert _classify_exit(125, "error during connect") is FailureKind.ENVIRONMENT


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
    results, _size, _image, _available = verify_mod.verify_docker(
        "FROM python:3.12-slim", tmp_path, _target(), RUNTIME
    )
    assert [r.check_id for r in results] == ["build", "run_completes"]
    assert all(r.status is CheckStatus.PASSED for r in results)


def test_report_wide_redaction_covers_build_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding 1: the marker can leak through a *different* check's message.

    The model can author `RUN python main.py` in the Dockerfile itself; if
    that RUN step fails during build, the marker lands in the build-log
    tail. `verify()` must redact every check's message, not just
    `run_completes`'s, before returning the report.
    """
    (tmp_path / "main.py").write_text("print('hi')\n")
    dockerfile = 'FROM python:3.12-slim\nCOPY main.py .\nCMD ["python", "main.py"]\n'

    def fake(runtime: ContainerRuntime, args: list[str], **kwargs: Any) -> Any:
        if args[0] == "build":
            return _proc(1, stderr=f"Step 3/3 : RUN python main.py\n{MARKER}\nfail")
        return _proc(0)

    monkeypatch.setattr(verify_mod, "container_run", fake)
    report = verify(dockerfile, tmp_path, _target(), RUNTIME)

    build = next(r for r in report.results if r.check_id == "build")
    assert build.status is CheckStatus.FAILED
    assert MARKER not in build.message
    assert "<redacted>" in build.message


def test_multiline_marker_redacted_before_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 3: redaction must happen before `_tail` truncates output.

    `_tail` is line-based; if a multi-line marker gets split by
    truncation, replacing the whole marker string *after* tailing can
    leave an unmatched fragment (e.g. just "line-two") behind. Redacting
    the raw output first avoids that regardless of where truncation lands.
    """
    marker = "line-one\nline-two"
    filler = "\n".join(f"noise {i}" for i in range(30))
    stdout = f"{marker}\n{filler}\ncrash\n"
    _patch_container_run(monkeypatch, _proc(1, stdout=stdout))
    result = _run_completes(_target(marker=marker), RUNTIME, "tag", 30)
    assert result.status is CheckStatus.FAILED
    assert "line-one" not in result.message
    assert "line-two" not in result.message


def test_multiline_marker_fragment_redacted_in_other_checks() -> None:
    """A tailed message from another check (e.g. build) can carry only a
    fragment of a multi-line marker; per-line redaction must still strip
    it even though the full marker string is absent."""
    marker = "line-one\nline-two"
    message = "Step 2/3 : RUN python main.py\nline-two\nerror: boom"
    redacted = _redact_oracle(message, marker)
    assert "line-two" not in redacted
    assert "<redacted>" in redacted
    assert "error: boom" in redacted


def test_verify_entrypoint_without_facts_is_config_error(
    tmp_path: Path,
) -> None:
    from deployer.facts import TargetConfigError

    with pytest.raises(TargetConfigError, match="no project facts"):
        verify_mod.verify(
            "FROM python:3.12-slim",
            tmp_path,
            DeployTarget(entrypoint="app.py"),
            None,
            None,
        )
