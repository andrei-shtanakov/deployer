"""The §6 result model enforces its own invariants."""

from typing import Any

import pytest
from pydantic import ValidationError

from deployer.reproduce.model import (
    BuildResult,
    Comparison,
    ReproductionCheck,
    ReproductionSection,
    ReproEvidence,
)


def test_failed_check_needs_finding_and_evidence():
    with pytest.raises(ValidationError, match="finding"):
        ReproductionCheck(check_id="x", status="failed")
    with pytest.raises(ValidationError, match="evidence"):
        ReproductionCheck(check_id="x", status="failed", finding="f")
    ReproductionCheck(
        check_id="x",
        status="failed",
        finding="f",
        evidence=[ReproEvidence(kind="log_excerpt", text="t")],
    )


@pytest.mark.parametrize("status", ["skipped", "inconclusive"])
def test_skipped_and_inconclusive_need_a_reason(status):
    with pytest.raises(ValidationError, match="reason"):
        ReproductionCheck(check_id="x", status=status)
    ReproductionCheck(check_id="x", status=status, reason="why")


def _build(**overrides: Any) -> BuildResult:
    defaults: dict[str, Any] = dict(
        argv=["podman", "build"],
        exit_code=1,
        launch_error=None,
        failed_instruction=None,
        signature=None,
        stdout="build.stdout",
        stderr="build.stderr",
        image_cleanup="not_attempted",
        build_containers="removed_by_builder",
    )
    defaults.update(overrides)
    return BuildResult(**defaults)


def test_exit_code_is_null_iff_launch_error():
    with pytest.raises(ValidationError):
        _build(exit_code=None)
    with pytest.raises(ValidationError):
        _build(exit_code=1, launch_error="timeout")
    _build(exit_code=None, launch_error="timeout")


def test_comparison_reason_set_iff_not_attempted_or_inconclusive():
    with pytest.raises(ValidationError):
        Comparison(state="inconclusive", reason=None)
    with pytest.raises(ValidationError):
        Comparison(state="not_reproduced", reason="x")
    Comparison(state="inconclusive", reason="build did not finish")


def test_refused_section_has_refusal_and_no_build_or_comparison():
    with pytest.raises(ValidationError):
        ReproductionSection(status="refused")
    with pytest.raises(ValidationError):
        ReproductionSection(status="refused", refusal="r", build=_build())
    ReproductionSection(status="refused", refusal="r")


def test_attempted_section_has_build_and_comparison_and_no_refusal():
    with pytest.raises(ValidationError):
        ReproductionSection(status="attempted")
    ReproductionSection(
        status="attempted",
        build=_build(),
        comparison=Comparison(state="not_reproduced"),
    )


def test_no_field_anywhere_is_named_failure_kind():
    for model in (ReproductionCheck, BuildResult, Comparison, ReproductionSection):
        assert "failure_kind" not in model.model_fields
