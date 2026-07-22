import pytest
from pydantic import ValidationError

from deployer.models import (
    AuthoringRun,
    CheckResult,
    CheckStatus,
    DeployTarget,
    FailureKind,
    IterationRecord,
    ProjectFacts,
    RunSpec,
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


def test_run_spec_defaults_and_roundtrip() -> None:
    target = DeployTarget(run=RunSpec(expect_stdout="ok"))
    parsed = DeployTarget.model_validate_json(target.model_dump_json())
    assert parsed.run is not None
    assert parsed.run.expect_stdout == "ok"


def test_bare_run_spec_has_no_oracle() -> None:
    target = DeployTarget.model_validate_json('{"run": {}}')
    assert target.run is not None
    assert target.run.expect_stdout is None


def test_service_and_run_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        DeployTarget(service=ServiceSpec(port=8000), run=RunSpec(expect_stdout="x"))


def test_build_only_target_still_valid() -> None:
    target = DeployTarget()
    assert target.service is None
    assert target.run is None


def test_run_spec_rejects_empty_oracle() -> None:
    """Finding 2: an empty marker would silently disarm the check
    (`"" not in stdout` is always False), so it must be rejected up front."""
    with pytest.raises(ValidationError):
        RunSpec(expect_stdout="")


def test_run_spec_none_and_nonempty_oracle_remain_valid() -> None:
    assert RunSpec().expect_stdout is None
    assert RunSpec(expect_stdout="x").expect_stdout == "x"


def test_project_facts_layout_fields_default_empty() -> None:
    facts = ProjectFacts()
    assert facts.optional_dependencies == {}
    assert facts.root_modules == []
    assert facts.package_dirs == []


def test_extras_default_empty_and_roundtrip() -> None:
    target = DeployTarget(extras=["gui"])
    parsed = DeployTarget.model_validate_json(target.model_dump_json())
    assert parsed.extras == ["gui"]
    assert DeployTarget().extras == []


def test_extras_canonicalized_and_deduped() -> None:
    target = DeployTarget(extras=["GUI", "my_extra", "my-extra"])
    assert target.extras == ["gui", "my-extra"]


def test_extras_full_pep503_separator_collapse() -> None:
    target = DeployTarget(extras=["my.extra", "my__extra", "my-_.extra"])
    assert target.extras == ["my-extra"]


def test_extras_reject_empty_entries() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        DeployTarget(extras=["gui", "  "])


def test_entrypoint_default_none_and_roundtrip() -> None:
    assert DeployTarget().entrypoint is None
    target = DeployTarget(entrypoint="app.py")
    parsed = DeployTarget.model_validate_json(target.model_dump_json())
    assert parsed.entrypoint == "app.py"


def test_entrypoint_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        DeployTarget(entrypoint="")


def test_service_dependency_requires_pinned_image() -> None:
    from deployer.models import ServiceDependency

    ServiceDependency(name="cache", image="redis:7-alpine")
    ServiceDependency(name="db", image="postgres:16-alpine")
    ServiceDependency(name="cache", image="redis@sha256:" + "a" * 64)
    ServiceDependency(name="cache", image="localhost:5000/redis:7-alpine")
    for image in ("redis", "redis:latest", "localhost:5000/redis"):
        with pytest.raises(ValidationError):
            ServiceDependency(name="cache", image=image)


def test_service_dependency_name_rules() -> None:
    from deployer.models import ServiceDependency

    with pytest.raises(ValidationError):
        ServiceDependency(name="app", image="redis:7-alpine")
    with pytest.raises(ValidationError):
        ServiceDependency(name="Cache!", image="redis:7-alpine")


def test_dependencies_require_service() -> None:
    from deployer.models import ServiceDependency

    dep = ServiceDependency(name="cache", image="redis:7-alpine")
    DeployTarget(service=ServiceSpec(port=8000), dependencies=[dep])
    with pytest.raises(ValidationError):
        DeployTarget(dependencies=[dep])  # no service
    with pytest.raises(ValidationError):
        DeployTarget(run=RunSpec(), dependencies=[dep])  # job with deps


def test_duplicate_dependency_names_rejected() -> None:
    from deployer.models import ServiceDependency

    deps = [
        ServiceDependency(name="cache", image="redis:7-alpine"),
        ServiceDependency(name="cache", image="redis:8-alpine"),
    ]
    with pytest.raises(ValidationError):
        DeployTarget(service=ServiceSpec(port=8000), dependencies=deps)


def test_iteration_record_compose_defaults_none() -> None:
    rec = IterationRecord(
        index=0, dockerfile="FROM x:1", report=VerificationReport(), duration_s=0.1
    )
    assert rec.compose is None
