from pathlib import Path

from deployer.models import CheckStatus
from deployer.verify import parse_dockerfile, verify_static

GOOD = """\
FROM python:3.12-slim
WORKDIR /app
COPY main.py .
EXPOSE 8000
CMD ["python", "main.py"]
"""


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


def test_hadolint_skipped_marks_non_comparable(
    hello_service: Path, monkeypatch
) -> None:
    monkeypatch.setattr("deployer.verify.shutil.which", lambda _: None)
    report = verify_static(GOOD, hello_service)
    assert _by_id(report, "hadolint").status is CheckStatus.SKIPPED
    assert report.hadolint_available is False
