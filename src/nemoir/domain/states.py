"""Closed vocabularies and lifecycle transition rules."""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTransitionError


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class BundleState(StringEnum):
    CAPTURING = "CAPTURING"
    SEALED = "SEALED"
    # Claim-option discovery has produced a persisted, validated candidate set
    # that the recipient can select from (or bypass with a custom topic).
    CLAIM_OPTIONS_READY = "CLAIM_OPTIONS_READY"
    # Claim-option discovery failed safely; the raw response and validation
    # report are retained for review and a custom topic remains available.
    CLAIM_OPTIONS_FAILED = "CLAIM_OPTIONS_FAILED"
    # Retained for databases created before the two-stage claim workflow.
    # Legacy bundles may still be claimed with a custom topic and analysed.
    AWAITING_CLAIM = "AWAITING_CLAIM"
    ANALYSING = "ANALYSING"
    REVIEW_READY = "REVIEW_READY"
    COMPLETED = "COMPLETED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCELLED = "CANCELLED"
    # Autonomous recipient-free capture: the sealed bundle is owned by the
    # submitter and a durable background job is sorting it into topics.
    AUTONOMOUS_SORTING = "AUTONOMOUS_SORTING"
    # Autonomous sorting failed safely (provider or validation failure); no
    # trusted tendrils exist and the owner/admin may retry deliberately.
    AUTONOMOUS_FAILED = "AUTONOMOUS_FAILED"


class TendrilState(StringEnum):
    OPEN = "OPEN"
    SNOOZED = "SNOOZED"
    ROUTED = "ROUTED"
    PROMOTED_ACTIONABLE = "PROMOTED_ACTIONABLE"
    RESURFACED = "RESURFACED"
    RESOLVED = "RESOLVED"
    MERGED = "MERGED"
    RELEASED = "RELEASED"


class ClaimStatus(StringEnum):
    DECLARED = "DECLARED"
    MATCHED = "MATCHED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFIRMED = "CONFIRMED"


class CoverageClassification(StringEnum):
    CLAIMED = "CLAIMED"
    TENDRIL = "TENDRIL"
    CONTEXT = "CONTEXT"
    DUPLICATE = "DUPLICATE"


class TendrilType(StringEnum):
    CONVERSATIONAL = "CONVERSATIONAL"
    OPEN_QUESTION = "OPEN_QUESTION"
    RESEARCH_CANDIDATE = "RESEARCH_CANDIDATE"
    DECISION_CANDIDATE = "DECISION_CANDIDATE"
    ACTIONABLE_CANDIDATE = "ACTIONABLE_CANDIDATE"
    PROJECT_SEED = "PROJECT_SEED"
    INTERESTING = "INTERESTING"


class Actionability(StringEnum):
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    CANDIDATE = "CANDIDATE"


class ExternalOperationStatus(StringEnum):
    PENDING = "PENDING"
    EXTERNAL_SUCCEEDED = "EXTERNAL_SUCCEEDED"
    COMPLETED = "COMPLETED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class PromptAttemptStatus(StringEnum):
    """Lifecycle of a single-turn ordinary prompt attempt."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PromptFailureClass(StringEnum):
    """Safe, non-leaking classification for a failed prompt attempt."""

    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    INVALID_RESPONSE = "INVALID_RESPONSE"


class AutonomousJobPhase(StringEnum):
    """Durable phases of one autonomous bundle job (sort then publish).

    The phase ordering encodes the paid-request crash boundary: QUEUED means a
    model request was definitely never sent, while REQUEST_STARTED means it may
    have been sent, so a resumed REQUEST_STARTED job becomes
    REQUEST_AMBIGUOUS and is never retried automatically.
    """

    QUEUED = "QUEUED"
    REQUEST_STARTED = "REQUEST_STARTED"
    RESULT_PERSISTED = "RESULT_PERSISTED"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REQUEST_AMBIGUOUS = "REQUEST_AMBIGUOUS"
    PUBLISH_RECONCILIATION_REQUIRED = "PUBLISH_RECONCILIATION_REQUIRED"


class AutonomousAttemptStatus(StringEnum):
    """Lifecycle of one bounded autonomous-sort provider attempt."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # The process died after the model request may have been sent and before
    # any result was persisted. Never retried automatically.
    AMBIGUOUS = "AMBIGUOUS"


BUNDLE_TRANSITIONS: dict[BundleState, frozenset[BundleState]] = {
    BundleState.CAPTURING: frozenset({BundleState.SEALED, BundleState.CANCELLED}),
    BundleState.SEALED: frozenset(
        {
            BundleState.AWAITING_CLAIM,  # legacy; kept for old database replay
            BundleState.CLAIM_OPTIONS_READY,
            BundleState.CLAIM_OPTIONS_FAILED,
            BundleState.ANALYSING,  # a custom topic may claim directly from SEALED
            BundleState.AUTONOMOUS_SORTING,  # recipient-free capture queues a job
            BundleState.CANCELLED,
        }
    ),
    BundleState.CLAIM_OPTIONS_READY: frozenset(
        {BundleState.ANALYSING, BundleState.CANCELLED}
    ),
    BundleState.CLAIM_OPTIONS_FAILED: frozenset(
        {
            BundleState.SEALED,  # legacy retry path
            BundleState.CLAIM_OPTIONS_READY,  # a successful discovery retry
            BundleState.ANALYSING,  # custom-topic escape hatch
            BundleState.CANCELLED,
        }
    ),
    BundleState.AWAITING_CLAIM: frozenset(
        {BundleState.CLAIM_OPTIONS_READY, BundleState.ANALYSING, BundleState.CANCELLED}
    ),
    BundleState.ANALYSING: frozenset(
        {
            BundleState.REVIEW_READY,
            BundleState.NEEDS_REVIEW,
            BundleState.ANALYSIS_FAILED,
        }
    ),
    BundleState.REVIEW_READY: frozenset({BundleState.COMPLETED, BundleState.ANALYSING}),
    BundleState.ANALYSIS_FAILED: frozenset({BundleState.ANALYSING, BundleState.CANCELLED}),
    BundleState.NEEDS_REVIEW: frozenset({BundleState.ANALYSING, BundleState.COMPLETED}),
    BundleState.AUTONOMOUS_SORTING: frozenset(
        {
            BundleState.REVIEW_READY,  # validated topics persisted; publishing may resume
            BundleState.AUTONOMOUS_FAILED,
            BundleState.CANCELLED,
        }
    ),
    BundleState.AUTONOMOUS_FAILED: frozenset(
        {
            BundleState.AUTONOMOUS_SORTING,  # deliberate owner/admin retry
            BundleState.CANCELLED,
        }
    ),
    BundleState.COMPLETED: frozenset(),
    BundleState.CANCELLED: frozenset(),
}


AUTONOMOUS_JOB_TRANSITIONS: dict[AutonomousJobPhase, frozenset[AutonomousJobPhase]] = {
    AutonomousJobPhase.QUEUED: frozenset(
        {AutonomousJobPhase.REQUEST_STARTED, AutonomousJobPhase.FAILED}
    ),
    AutonomousJobPhase.REQUEST_STARTED: frozenset(
        {
            AutonomousJobPhase.RESULT_PERSISTED,
            AutonomousJobPhase.FAILED,
            AutonomousJobPhase.REQUEST_AMBIGUOUS,
        }
    ),
    AutonomousJobPhase.RESULT_PERSISTED: frozenset(
        {AutonomousJobPhase.PUBLISHING, AutonomousJobPhase.COMPLETED}
    ),
    AutonomousJobPhase.PUBLISHING: frozenset(
        {
            AutonomousJobPhase.COMPLETED,
            AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED,
        }
    ),
    # After an operator reconciles the unresolved publish operation(s), the
    # job may safely resume publishing (no ambiguous side effect remains).
    AutonomousJobPhase.PUBLISH_RECONCILIATION_REQUIRED: frozenset(
        {AutonomousJobPhase.PUBLISHING, AutonomousJobPhase.COMPLETED}
    ),
    AutonomousJobPhase.FAILED: frozenset({AutonomousJobPhase.QUEUED}),
    AutonomousJobPhase.REQUEST_AMBIGUOUS: frozenset({AutonomousJobPhase.QUEUED}),
    AutonomousJobPhase.COMPLETED: frozenset(),
}


TENDRIL_TRANSITIONS: dict[TendrilState, frozenset[TendrilState]] = {
    TendrilState.OPEN: frozenset(
        {
            TendrilState.SNOOZED,
            TendrilState.ROUTED,
            TendrilState.PROMOTED_ACTIONABLE,
            TendrilState.RESURFACED,
            TendrilState.RESOLVED,
            TendrilState.MERGED,
            TendrilState.RELEASED,
        }
    ),
    TendrilState.SNOOZED: frozenset(
        {TendrilState.OPEN, TendrilState.RESURFACED, TendrilState.RESOLVED, TendrilState.RELEASED}
    ),
    TendrilState.RESURFACED: frozenset(
        {
            TendrilState.OPEN,
            TendrilState.SNOOZED,
            TendrilState.ROUTED,
            TendrilState.PROMOTED_ACTIONABLE,
            TendrilState.RESOLVED,
            TendrilState.MERGED,
            TendrilState.RELEASED,
        }
    ),
    TendrilState.ROUTED: frozenset(
        {TendrilState.RESOLVED, TendrilState.MERGED, TendrilState.RELEASED}
    ),
    TendrilState.PROMOTED_ACTIONABLE: frozenset(
        {TendrilState.RESOLVED, TendrilState.RELEASED}
    ),
    TendrilState.RESOLVED: frozenset(),
    TendrilState.MERGED: frozenset(),
    TendrilState.RELEASED: frozenset(),
}


def require_bundle_transition(prior: BundleState, new: BundleState) -> None:
    if new not in BUNDLE_TRANSITIONS[prior]:
        raise InvalidTransitionError(f"Invalid bundle transition: {prior} -> {new}")


def require_tendril_transition(prior: TendrilState, new: TendrilState) -> None:
    if new not in TENDRIL_TRANSITIONS[prior]:
        raise InvalidTransitionError(f"Invalid tendril transition: {prior} -> {new}")


def require_autonomous_job_transition(
    prior: AutonomousJobPhase, new: AutonomousJobPhase
) -> None:
    if new not in AUTONOMOUS_JOB_TRANSITIONS[prior]:
        raise InvalidTransitionError(
            f"Invalid autonomous job transition: {prior} -> {new}"
        )
