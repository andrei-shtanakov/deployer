"""CI-failure admission (spec 2026-09-24 A): may a failed CI run enter
``ci-fix-authoring``? Only when a defect of a deployer-authored artifact is
proven."""

from deployer.admission.decide import VerifiedFacts, decide
from deployer.admission.model import AdmissionSection
from deployer.admission.prepare import prepare

__all__ = ["AdmissionSection", "VerifiedFacts", "decide", "prepare"]
