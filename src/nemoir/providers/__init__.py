"""Analysis provider adapters."""

from .base import (
    AnalysisProvider,
    AutonomousSortProvider,
    ClaimDiscoveryProvider,
    PromptProvider,
)
from .fake import FakeAnalysisProvider, SyntheticFixtureAnalysisProvider
from .synthetic import (
    SyntheticAutonomousSortProvider,
    SyntheticClaimDiscoveryProvider,
    SyntheticPromptProvider,
)

__all__ = [
    "AnalysisProvider",
    "AutonomousSortProvider",
    "ClaimDiscoveryProvider",
    "FakeAnalysisProvider",
    "PromptProvider",
    "SyntheticAutonomousSortProvider",
    "SyntheticClaimDiscoveryProvider",
    "SyntheticFixtureAnalysisProvider",
    "SyntheticPromptProvider",
]
