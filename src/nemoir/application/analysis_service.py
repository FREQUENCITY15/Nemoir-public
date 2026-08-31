"""One-attempt analysis orchestration with strict deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass

from nemoir.application.authorization import AuthorizationPolicy
from nemoir.domain.errors import ConflictError, ProviderOutputValidationError
from nemoir.domain.models import AnalysisRequest, ReviewResult
from nemoir.domain.states import BundleState, ClaimStatus
from nemoir.domain.validation import ValidationIssue, ValidationReport, validate_analysis
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.base import AnalysisProvider


@dataclass(frozen=True)
class AnalysisOutcome:
    attempt_id: str
    state: BundleState
    validation: ValidationReport
    review: ReviewResult | None = None
    replayed: bool = False


class AnalysisService:
    def __init__(
        self,
        repository: SQLiteRepository,
        provider: AnalysisProvider,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.authorization = authorization

    async def analyse(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
        habitat_descriptions: list[str] | None = None,
    ) -> AnalysisOutcome:
        self.authorization.require_bundle_recipient(bundle_id, actor_user_id)
        bundle = self.repository.get_bundle(bundle_id)
        claim = self.repository.get_claim_for_bundle(bundle_id)
        if bundle.status not in {
            BundleState.SEALED,
            BundleState.CLAIM_OPTIONS_READY,
            BundleState.CLAIM_OPTIONS_FAILED,
            BundleState.AWAITING_CLAIM,
            BundleState.ANALYSING,
            BundleState.ANALYSIS_FAILED,
            BundleState.NEEDS_REVIEW,
            BundleState.REVIEW_READY,
        }:
            raise ConflictError(f"Bundle cannot be analysed from {bundle.status}")
        attempt_id, created = self.repository.begin_analysis_attempt(
            bundle_id, idempotency_key, "nemoir-analysis-v1"
        )
        if not created:
            attempt = self.repository.get_analysis_attempt(attempt_id)
            if attempt["status"] == "SUCCEEDED":
                review = self.repository.get_review(bundle_id)
                return AnalysisOutcome(
                    attempt_id=attempt_id,
                    state=review.bundle_status,
                    validation=ValidationReport(),
                    review=review,
                    replayed=True,
                )
            if attempt["status"] == "RUNNING":
                # A duplicate interaction or retry with the same key while the
                # original attempt is still running: the original caller owns
                # the provider call and the result posting. Return a replay
                # outcome instead of a spurious failure.
                return AnalysisOutcome(
                    attempt_id=attempt_id,
                    state=BundleState.ANALYSING,
                    validation=ValidationReport(),
                    replayed=True,
                )
            raise ConflictError(
                f"Analysis idempotency key already exists with status {attempt['status']}"
            )

        self.repository.transition_bundle(
            bundle_id,
            BundleState.ANALYSING,
            actor_user_id,
            f"{idempotency_key}:analysing",
            {"attempt_id": attempt_id},
        )
        bundle = self.repository.get_bundle(bundle_id)
        discovered_candidates = self.repository.list_claim_candidates(bundle_id)
        request = AnalysisRequest(
            bundle_id=bundle_id,
            messages=bundle.source_messages,
            source_units=bundle.source_units,
            raw_claim=claim.raw_topic,
            selected_candidates=claim.selected_candidates,
            discovered_candidates=discovered_candidates,
            habitat_descriptions=habitat_descriptions or [],
        )

        try:
            response = await self.provider.analyse(request)
        except ProviderOutputValidationError as exc:
            report = ValidationReport()
            report.errors.append(
                ValidationIssue(
                    code="PROVIDER_OUTPUT_INVALID",
                    message=(
                        "Provider output failed the AnalysisResult contract "
                        f"with {exc.validation_error_count} validation error(s)"
                    ),
                )
            )
            self.repository.persist_analysis_failure(
                attempt_id,
                "FAILED",
                report,
                exc.receipt,
                exc.raw_response,
            )
            self.repository.transition_bundle(
                bundle_id,
                BundleState.ANALYSIS_FAILED,
                actor_user_id,
                f"{idempotency_key}:provider-output-invalid",
                {
                    "attempt_id": attempt_id,
                    "validation_error_count": exc.validation_error_count,
                },
            )
            return AnalysisOutcome(attempt_id, BundleState.ANALYSIS_FAILED, report)
        except Exception as exc:
            report = ValidationReport()
            report.errors.append(
                ValidationIssue(
                    code="PROVIDER_FAILURE",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            self.repository.persist_analysis_failure(attempt_id, "FAILED", report)
            self.repository.transition_bundle(
                bundle_id,
                BundleState.ANALYSIS_FAILED,
                actor_user_id,
                f"{idempotency_key}:provider-failed",
                {"attempt_id": attempt_id, "error_type": type(exc).__name__},
            )
            return AnalysisOutcome(attempt_id, BundleState.ANALYSIS_FAILED, report)

        report = validate_analysis(
            bundle,
            bundle.source_units,
            response.result,
            selected_candidates=claim.selected_candidates,
            discovered_candidates=discovered_candidates,
        )
        if not report.valid:
            self.repository.persist_analysis_failure(
                attempt_id,
                "FAILED",
                report,
                response.receipt,
                response.raw_response,
            )
            self.repository.transition_bundle(
                bundle_id,
                BundleState.ANALYSIS_FAILED,
                actor_user_id,
                f"{idempotency_key}:validation-failed",
                {"attempt_id": attempt_id, "error_count": len(report.errors)},
            )
            return AnalysisOutcome(attempt_id, BundleState.ANALYSIS_FAILED, report)

        if report.needs_review:
            self.repository.persist_analysis_failure(
                attempt_id,
                "NEEDS_REVIEW",
                report,
                response.receipt,
                response.raw_response,
            )
            self.repository.update_claim_status(bundle_id, ClaimStatus.NEEDS_REVIEW)
            self.repository.transition_bundle(
                bundle_id,
                BundleState.NEEDS_REVIEW,
                actor_user_id,
                f"{idempotency_key}:needs-review",
                {"attempt_id": attempt_id, "warning_count": len(report.warnings)},
            )
            return AnalysisOutcome(attempt_id, BundleState.NEEDS_REVIEW, report)

        self.repository.persist_analysis_success(
            attempt_id, bundle_id, response.result, response.receipt, report
        )
        self.repository.update_claim_status(bundle_id, ClaimStatus.MATCHED)
        self.repository.transition_bundle(
            bundle_id,
            BundleState.REVIEW_READY,
            actor_user_id,
            f"{idempotency_key}:review-ready",
            {"attempt_id": attempt_id, "tendril_count": len(response.result.tendrils)},
        )
        review = self.repository.get_review(bundle_id)
        return AnalysisOutcome(attempt_id, BundleState.REVIEW_READY, report, review)
