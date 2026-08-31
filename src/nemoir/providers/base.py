"""Provider-neutral semantic analysis seam."""

from __future__ import annotations

from typing import Protocol

from nemoir.domain.models import (
    AnalysisRequest,
    AnalysisResponse,
    AutonomousSortRequest,
    AutonomousSortResponse,
    ClaimDiscoveryRequest,
    ClaimDiscoveryResponse,
    PromptRequest,
    PromptResponse,
)


class AnalysisProvider(Protocol):
    async def analyse(self, request: AnalysisRequest) -> AnalysisResponse:
        """Return one structured result for a sealed and claimed bundle."""


class ClaimDiscoveryProvider(Protocol):
    async def discover_claim_candidates(
        self, request: ClaimDiscoveryRequest
    ) -> ClaimDiscoveryResponse:
        """Return validated claim-candidate options for a sealed (unclaimed) bundle.

        This is a distinct pre-claim operation; it must not be conflated with
        the post-claim ``analyse`` decomposition.
        """


class PromptProvider(Protocol):
    async def prompt(self, request: PromptRequest) -> PromptResponse:
        """Answer one ordinary, stateless question as unstructured display text.

        This is a single-turn operation: it must not capture source, discover
        claims, run analysis, create tendrils, or perform channel actions.
        """


class AutonomousSortProvider(Protocol):
    """Recipient-free thought sorting: a separate provider boundary.

    Autonomous sorting never flows through the claim/selected-candidate model:
    one sealed, owner-held bundle goes in and a strict full-source topic
    decomposition (every unit assigned to exactly one primary topic) comes
    out. Output is validated by ``validate_autonomous_sort`` before any
    tendril, channel, or post is created.
    """

    async def sort(self, request: AutonomousSortRequest) -> AutonomousSortResponse:
        """Return one strict, source-backed topic decomposition for the bundle."""
