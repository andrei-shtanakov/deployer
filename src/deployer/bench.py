"""Corpus loading, offline fixture author, and bench orchestration."""

import fnmatch
import hashlib
from pathlib import Path

from pydantic import BaseModel, Field

from deployer.models import (
    AuthorInfo,
    DeployTarget,
    ExpectedOutcome,
    ProjectFacts,
    VerificationReport,
)


class FixtureAuthor:
    """Offline DockerfileAuthor replaying a case's known-good Dockerfile.

    generate() and repair() both return the fixture verbatim: the bench's
    offline mode measures the verification pipeline, not authoring skill,
    so there is nothing to "repair" — a failing fixture is corpus rot.
    """

    def __init__(self, dockerfile: str) -> None:
        self._dockerfile = dockerfile

    def generate(self, facts: ProjectFacts, target: DeployTarget) -> str:
        return self._dockerfile

    def repair(
        self,
        facts: ProjectFacts,
        target: DeployTarget,
        dockerfile: str,
        report: VerificationReport,
    ) -> str:
        return self._dockerfile

    def info(self) -> AuthorInfo:
        """Comparability metadata: fixture hash stands in for a prompt hash."""
        return AuthorInfo(
            backend="fixture",
            prompt_sha256=hashlib.sha256(self._dockerfile.encode()).hexdigest(),
        )


class BenchCase(BaseModel):
    """One corpus case: a target project plus intent and expectations."""

    name: str
    project_dir: Path
    target: DeployTarget = Field(default_factory=DeployTarget)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)
    fixture_dockerfile: Path | None = None


def load_corpus(corpus_root: Path, pattern: str = "*") -> list[BenchCase]:
    """Load synthetic corpus cases whose directory name matches `pattern`."""
    synthetic = corpus_root / "synthetic"
    if not synthetic.is_dir():
        raise FileNotFoundError(f"no synthetic corpus at {synthetic}")
    cases: list[BenchCase] = []
    for case_dir in sorted(p for p in synthetic.iterdir() if p.is_dir()):
        if not fnmatch.fnmatch(case_dir.name, pattern):
            continue
        project_dir = case_dir / "project"
        if not project_dir.is_dir():
            raise ValueError(f"corpus case {case_dir.name} has no project/ dir")
        target_file = case_dir / "target.json"
        target = (
            DeployTarget.model_validate_json(target_file.read_text())
            if target_file.is_file()
            else DeployTarget()
        )
        expected_file = case_dir / "expected.json"
        expected = (
            ExpectedOutcome.model_validate_json(expected_file.read_text())
            if expected_file.is_file()
            else ExpectedOutcome()
        )
        fixture = case_dir / "fixture.Dockerfile"
        cases.append(
            BenchCase(
                name=case_dir.name,
                project_dir=project_dir,
                target=target,
                expected=expected,
                fixture_dockerfile=fixture if fixture.is_file() else None,
            )
        )
    return cases
