"""Corpus loading, offline fixture author, and bench orchestration."""

import hashlib

from deployer.models import (
    AuthorInfo,
    DeployTarget,
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
