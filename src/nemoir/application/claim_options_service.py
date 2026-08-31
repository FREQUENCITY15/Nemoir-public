"""Two-stage claim: deterministic candidate discovery before any claim.

``/seal`` segments the source and then asks the provider for 2-5 claim
candidates. The candidates are validated against source truth and persisted
before the recipient ever selects one; only then does the existing
post-claim decomposition run. The discovery attempt (and any raw provider
response) is retained so malformed or hallucinated output fails safely and
stays reviewable.
"""

from __future__ import annotations

from dataclasses import dataclass

from nemoir.application.authorization import AuthorizationPolicy
from nemoir.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ProviderOutputValidationError,
)
from nemoir.domain.models import (
    Claim,
    ClaimCandidate,
    ClaimDiscoveryRequest,
    join_selected_titles,
    new_id,
)
from nemoir.domain.states import BundleState
from nemoir.domain.validation import (
    ValidationIssue,
    ValidationReport,
    validate_claim_candidates,
)
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.base import ClaimDiscoveryProvider

# States from which a custom topic may claim directly (no options required).
CUSTOM_CLAIMABLE_STATES = {
    BundleState.SEALED,
    BundleState.CLAIM_OPTIONS_READY,
    BundleState.CLAIM_OPTIONS_FAILED,
    BundleState.AWAITING_CLAIM,
}


@dataclass(frozen=True)
class ClaimOptionsOutcome:
    attempt_id: str | None
    state: BundleState
    validation: ValidationReport
    candidates: list[ClaimCandidate] | None = None
    replayed: bool = False


def parse_option_numbers(value: str) -> list[int]:
    """Parse a comma-separated option list into integers, safely.

    Trims whitespace and rejects empty input, empty elements, and any token
    that is not an integer. Zero, negative, duplicate, and unavailable numbers
    are validated later by ``ClaimOptionsService.select`` against persisted
    state, so this helper stays a pure string→int parser.
    """
    if not value or not value.strip():
        raise ValueError("Claim options must not be empty")
    numbers: list[int] = []
    for part in value.split(","):
        token = part.strip()
        if not token:
            raise ValueError("Claim options must not contain empty elements")
        try:
            numbers.append(int(token))
        except ValueError as exc:
            raise ValueError("Claim options must be comma-separated integers") from exc
    return numbers


class ClaimOptionsService:
    def __init__(
        self,
        repository: SQLiteRepository,
        provider: ClaimDiscoveryProvider,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.authorization = authorization

    async def discover(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
    ) -> ClaimOptionsOutcome:
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.submitter_user_id and not self.authorization.is_admin(
            actor_user_id
        ):
            raise AuthorizationError(
                "Only the capture owner or an administrator may generate claim options"
            )

        if bundle.status == BundleState.CLAIM_OPTIONS_READY:
            return ClaimOptionsOutcome(
                attempt_id=None,
                state=BundleState.CLAIM_OPTIONS_READY,
                validation=ValidationReport(),
                candidates=self.repository.list_claim_candidates(bundle_id),
                replayed=True,
            )
        if bundle.status not in {BundleState.SEALED, BundleState.CLAIM_OPTIONS_FAILED}:
            raise ConflictError(f"Claim options cannot be generated from {bundle.status}")

        attempt_id, created = self.repository.begin_claim_discovery_attempt(
            bundle_id, idempotency_key, "nemoir-claim-discovery-v1"
        )
        if not created:
            attempt = self.repository.get_claim_discovery_attempt(attempt_id)
            if attempt["status"] == "SUCCEEDED":
                return ClaimOptionsOutcome(
                    attempt_id=attempt_id,
                    state=BundleState.CLAIM_OPTIONS_READY,
                    validation=ValidationReport(),
                    candidates=self.repository.list_claim_candidates(bundle_id),
                    replayed=True,
                )
            if attempt["status"] == "RUNNING":
                return ClaimOptionsOutcome(
                    attempt_id=attempt_id,
                    state=bundle.status,
                    validation=ValidationReport(),
                    replayed=True,
                )
            # A FAILED attempt replayed with the same key is not retried; the
            # original failure is the recorded answer and remains reviewable.
            return ClaimOptionsOutcome(
                attempt_id=attempt_id,
                state=BundleState.CLAIM_OPTIONS_FAILED,
                validation=ValidationReport(),
                replayed=True,
            )

        request = ClaimDiscoveryRequest(
            bundle_id=bundle_id,
            messages=bundle.source_messages,
            source_units=bundle.source_units,
        )

        try:
            response = await self.provider.discover_claim_candidates(request)
        except ProviderOutputValidationError as exc:
            report = ValidationReport(
                errors=[
                    ValidationIssue(
                        code="PROVIDER_OUTPUT_INVALID",
                        message=(
                            "Provider claim-candidate output failed the contract with "
                            f"{exc.validation_error_count} validation error(s)"
                        ),
                    )
                ]
            )
            self.repository.persist_claim_discovery_failure(
                attempt_id, "FAILED", report, exc.receipt, exc.raw_response
            )
            self.repository.transition_bundle(
                bundle_id,
                BundleState.CLAIM_OPTIONS_FAILED,
                actor_user_id,
                f"{idempotency_key}:provider-output-invalid",
                {"attempt_id": attempt_id, "validation_error_count": exc.validation_error_count},
            )
            return ClaimOptionsOutcome(attempt_id, BundleState.CLAIM_OPTIONS_FAILED, report)
        except Exception as exc:
            report = ValidationReport(
                errors=[
                    ValidationIssue(
                        code="PROVIDER_FAILURE",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                ]
            )
            self.repository.persist_claim_discovery_failure(attempt_id, "FAILED", report)
            self.repository.transition_bundle(
                bundle_id,
                BundleState.CLAIM_OPTIONS_FAILED,
                actor_user_id,
                f"{idempotency_key}:provider-failed",
                {"attempt_id": attempt_id, "error_type": type(exc).__name__},
            )
            return ClaimOptionsOutcome(attempt_id, BundleState.CLAIM_OPTIONS_FAILED, report)

        report = validate_claim_candidates(bundle, bundle.source_units, response.result)
        if not report.valid:
            self.repository.persist_claim_discovery_failure(
                attempt_id,
                "FAILED",
                report,
                response.receipt,
                response.raw_response,
            )
            self.repository.transition_bundle(
                bundle_id,
                BundleState.CLAIM_OPTIONS_FAILED,
                actor_user_id,
                f"{idempotency_key}:validation-failed",
                {"attempt_id": attempt_id, "error_count": len(report.errors)},
            )
            return ClaimOptionsOutcome(attempt_id, BundleState.CLAIM_OPTIONS_FAILED, report)

        candidates = self.repository.persist_claim_discovery_success(
            attempt_id, bundle_id, response.result, response.receipt, report
        )
        self.repository.transition_bundle(
            bundle_id,
            BundleState.CLAIM_OPTIONS_READY,
            actor_user_id,
            f"{idempotency_key}:options-ready",
            {"attempt_id": attempt_id, "candidate_count": len(candidates)},
        )
        return ClaimOptionsOutcome(
            attempt_id, BundleState.CLAIM_OPTIONS_READY, report, candidates
        )

    def get_options(self, bundle_id: str, *, actor_user_id: str) -> list[ClaimCandidate]:
        bundle = self.authorization.require_bundle_recipient(bundle_id, actor_user_id)
        candidates = self.repository.list_claim_candidates(bundle.id)
        if not candidates:
            raise ConflictError("No claim options are available for this bundle")
        return candidates

    def select(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        option_numbers: list[int] | None = None,
        custom_topic: str | None = None,
    ) -> Claim:
        bundle = self.authorization.require_bundle_recipient(bundle_id, actor_user_id)
        has_options = bool(option_numbers)
        has_topic = custom_topic is not None and custom_topic.strip() != ""
        if has_options == has_topic:
            raise ConflictError("Supply exactly one of options or custom topic")
        if bundle.claim_id is not None:
            raise ConflictError("Bundle already has a claim")

        if option_numbers:
            if len(set(option_numbers)) != len(option_numbers):
                raise ConflictError("Duplicate claim options are not allowed")
            if any(number < 1 for number in option_numbers):
                raise ConflictError("Claim option numbers must be positive")
            if bundle.status != BundleState.CLAIM_OPTIONS_READY:
                raise ConflictError("Claim options are not ready for this bundle")
            try:
                selected = [
                    self.repository.get_candidate_by_option(bundle_id, number)
                    for number in option_numbers
                ]
            except NotFoundError as exc:
                raise ConflictError("One or more selected claim options are unavailable") from exc
            # Canonical order is persisted display order, never caller input order.
            selected.sort(key=lambda candidate: candidate.display_order)
            claim = Claim(
                id=new_id("claim"),
                bundle_id=bundle_id,
                raw_topic=join_selected_titles(selected),
                claimant_user_id=actor_user_id,
                selected_candidate_ids=[candidate.candidate_id for candidate in selected],
                selected_candidates=selected,
            )
        else:
            if bundle.status not in CUSTOM_CLAIMABLE_STATES:
                raise ConflictError(f"Bundle cannot be claimed from {bundle.status}")
            claim = Claim(
                id=new_id("claim"),
                bundle_id=bundle_id,
                raw_topic=custom_topic.strip(),
                claimant_user_id=actor_user_id,
                selected_candidate_ids=[],
                selected_candidates=[],
            )
        return self.repository.create_claim(claim)
