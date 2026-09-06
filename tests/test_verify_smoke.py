"""Unit tests for the `verify_docker` smoke branch and cleanup bookkeeping.

Daemon-free: every case here monkeypatches `_build`, `_image_size`,
`container_run` and `_check_atp_smoke`, so these run in the default suite
(no `docker` mark, no container runtime required).
"""

import subprocess

import pytest

from deployer.models import (
    CheckResult,
    CheckStatus,
    ContainerRuntime,
    DeployTarget,
    FailureKind,
)
from deployer.verify import verify_docker


def test_smoke_target_does_not_run_the_job_itself(monkeypatch, tmp_path) -> None:
    """ATP is the runtime check for a smoke target.

    `_run_completes` starts the container with no stdin at all, and a correct
    ATP agent reading stdin would get EOF; running it first would fail a
    healthy image.
    """
    called: list[str] = []
    monkeypatch.setattr(
        "deployer.verify._run_completes",
        lambda *a, **k: (
            called.append("run")
            or CheckResult(check_id="run_completes", status=CheckStatus.PASSED)
        ),
    )
    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr(
        "deployer.verify.container_run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: (
            CheckResult(check_id="atp_smoke", status=CheckStatus.PASSED),
            True,
        ),
    )
    target = DeployTarget(run={}, smoke={"suite": "s.yaml"})

    results, _size, image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        target,
        ContainerRuntime(tool="docker"),
        smoke_suite=tmp_path / "s.yaml",
    )

    assert called == []  # the job check never ran
    assert [r.check_id for r in results] == ["build", "atp_smoke"]
    assert available is True
    assert image.tag.startswith("localhost/deployer-verify-")


def test_smoke_target_without_resolved_suite_fails_environment(
    monkeypatch, tmp_path
) -> None:
    """A smoke target must never fall through to `_run_completes`.

    Without a resolved `smoke_suite` there is nothing to run ATP against;
    the old `elif` chain fell through to the job check instead, which starts
    the container with no stdin and fails a healthy ATP agent on EOF.
    """
    called: list[str] = []
    monkeypatch.setattr(
        "deployer.verify._run_completes",
        lambda *a, **k: (
            called.append("run")
            or CheckResult(check_id="run_completes", status=CheckStatus.PASSED)
        ),
    )
    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr(
        "deployer.verify.container_run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: pytest.fail("no suite to run ATP against"),
    )
    target = DeployTarget(run={}, smoke={"suite": "s.yaml"})

    results, _size, _image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        target,
        ContainerRuntime(tool="docker"),
        smoke_suite=None,
    )

    assert called == []  # the job check never ran either
    smoke = [r for r in results if r.check_id == "atp_smoke"][0]
    assert smoke.status is CheckStatus.FAILED
    assert smoke.failure_kind is FailureKind.ENVIRONMENT
    assert available is False


def test_built_image_records_a_failed_cleanup(monkeypatch, tmp_path) -> None:
    """rmi is best-effort, so cleanup_status must report what happened."""
    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)

    def boom(*a, **k):
        raise OSError("no daemon")

    monkeypatch.setattr("deployer.verify.container_run", boom)

    _results, _size, image, _available = verify_docker(
        "FROM x:1\n", tmp_path, DeployTarget(), ContainerRuntime(tool="docker")
    )

    assert image.cleanup_status == "failed"


def test_smoke_on_a_remote_runtime_is_skipped(monkeypatch, tmp_path) -> None:
    """ATP's container adapter has no remote-host support, so it must not
    silently test whatever image happens to be local."""
    monkeypatch.setattr(
        "deployer.verify._build",
        lambda *a, **k: CheckResult(check_id="build", status=CheckStatus.PASSED),
    )
    monkeypatch.setattr("deployer.verify._image_size", lambda *a, **k: 1)
    monkeypatch.setattr(
        "deployer.verify.container_run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )
    monkeypatch.setattr(
        "deployer.verify._check_atp_smoke",
        lambda *a, **k: pytest.fail("ATP must not run against a remote build"),
    )
    remote = ContainerRuntime(tool="docker", host="ssh://box", host_source="cli")

    results, _size, _image, available = verify_docker(
        "FROM x:1\n",
        tmp_path,
        DeployTarget(run={}, smoke={"suite": "s.yaml"}),
        remote,
        smoke_suite=tmp_path / "s.yaml",
    )

    smoke = [r for r in results if r.check_id == "atp_smoke"][0]
    assert smoke.status is CheckStatus.SKIPPED
    assert "remote" in smoke.message
    assert available is False
