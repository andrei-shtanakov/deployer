import pytest
from pydantic import ValidationError

from deployer.models import (
    AuthoringRun,
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    ProjectFacts,
    ServiceSpec,
    SystemDepHint,
    VerificationReport,
)


def _failed(check_id: str, kind: FailureKind, message: str = "boom") -> CheckResult:
    return CheckResult(
        check_id=check_id, status=CheckStatus.FAILED, failure_kind=kind, message=message
    )


def test_deploy_target_defaults() -> None:
    target = DeployTarget()
    assert target.base_image is None
    assert target.service is None
    assert target.memory_limit == "512m"


def test_deploy_target_roundtrip_json() -> None:
    target = DeployTarget(service=ServiceSpec(port=8000))
    restored = DeployTarget.model_validate_json(target.model_dump_json())
    assert restored == target


def test_report_passed_ignores_warnings() -> None:
    report = VerificationReport(
        results=[
            CheckResult(check_id="a", status=CheckStatus.PASSED),
            CheckResult(check_id="b", status=CheckStatus.WARNING, message="meh"),
            CheckResult(check_id="c", status=CheckStatus.SKIPPED),
        ]
    )
    assert report.passed


def test_report_failed_and_taxonomy() -> None:
    report = VerificationReport(
        results=[
            _failed("build", FailureKind.AUTHORING),
            _failed("pull", FailureKind.ENVIRONMENT),
        ]
    )
    assert not report.passed
    assert [r.check_id for r in report.environment_failures] == ["pull"]


def test_error_signature_is_stable_and_first_line_only() -> None:
    r1 = VerificationReport(
        results=[_failed("build", FailureKind.AUTHORING, "line one\nline two")]
    )
    r2 = VerificationReport(
        results=[_failed("build", FailureKind.AUTHORING, "line one\nDIFFERENT")]
    )
    assert r1.error_signature() == r2.error_signature()
    assert "line one" in r1.error_signature()


def test_authoring_run_serializes() -> None:
    run = AuthoringRun(
        project="demo",
        target=DeployTarget(),
        iterations=[],
        environment_retries=0,
        docker_available=False,
        hadolint_available=False,
        stopped_reason="static_only",
        success=False,
    )
    assert '"static_only"' in run.model_dump_json()


def test_failed_check_requires_failure_kind() -> None:
    with pytest.raises(ValidationError):
        CheckResult(check_id="x", status=CheckStatus.FAILED)


def test_error_signature_sorts_multiple_failures() -> None:
    a = _failed("zeta", FailureKind.AUTHORING, "zz")
    b = _failed("alpha", FailureKind.AUTHORING, "aa")
    r1 = VerificationReport(results=[a, b])
    r2 = VerificationReport(results=[b, a])
    assert r1.error_signature() == r2.error_signature()
    assert r1.error_signature() == "alpha:aa|zeta:zz"


def test_system_dep_hint_defaults() -> None:
    hint = SystemDepHint(python_package="psycopg2-binary")
    assert hint.build_packages == []
    assert hint.runtime_packages == []


def test_new_facts_fields_default_safe() -> None:
    facts = ProjectFacts()
    assert facts.package_manager is None
    assert facts.has_build_system is False
    assert facts.requirements_files == {}


def test_deploy_target_system_packages_roundtrip() -> None:
    target = DeployTarget(system_packages=["libpq5", "curl"])
    restored = DeployTarget.model_validate_json(target.model_dump_json())
    assert restored.system_packages == ["libpq5", "curl"]


def test_authoring_run_records_hints() -> None:
    run = AuthoringRun(
        project="demo",
        target=DeployTarget(),
        hints_offered=[
            SystemDepHint(python_package="psycopg2", build_packages=["libpq-dev"])
        ],
        docker_available=False,
        hadolint_available=False,
        stopped_reason="static_only",
        success=False,
    )
    assert '"psycopg2"' in run.model_dump_json()


def test_report_image_size_default_none() -> None:
    assert VerificationReport().image_size_bytes is None
