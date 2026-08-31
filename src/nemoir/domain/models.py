"""Strict platform-neutral domain and provider-contract models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .states import (
    Actionability,
    AutonomousAttemptStatus,
    AutonomousJobPhase,
    BundleState,
    ClaimStatus,
    CoverageClassification,
    ExternalOperationStatus,
    PromptAttemptStatus,
    PromptFailureClass,
    TendrilState,
    TendrilType,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    stamp = utc_now().strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}_{stamp}_{uuid4().hex[:10]}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceMessage(StrictModel):
    platform: str = "discord"
    external_message_id: str = Field(min_length=1)
    author_user_id: str = Field(min_length=1)
    author_display_name: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    content: str
    source_url: str = Field(min_length=1)
    timestamp: datetime
    ordinal: int = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(timezone.utc)


class SourceUnit(StrictModel):
    unit_id: str = Field(min_length=1)
    source_message_id: str = Field(min_length=1)
    paragraph_ordinal: int = Field(ge=1)
    exact_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @model_validator(mode="after")
    def offsets_are_ordered(self) -> "SourceUnit":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class ConversationBundle(StrictModel):
    id: str = Field(min_length=1)
    guild_id: str = Field(min_length=1)
    intake_channel_id: str = Field(min_length=1)
    submitter_user_id: str = Field(min_length=1)
    recipient_user_id: str = Field(min_length=1)
    status: BundleState = BundleState.CAPTURING
    # ``True`` identifies a recipient-free autonomous bundle: the submitter owns
    # the capture and no recipient action (claim selection) is ever required.
    # Legacy databases (and manual captures) read as ``False``.
    autonomous_mode: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    sealed_at: datetime | None = None
    source_messages: list[SourceMessage] = Field(default_factory=list)
    source_units: list[SourceUnit] = Field(default_factory=list)
    claim_id: str | None = None


class Claim(StrictModel):
    id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    raw_topic: str = Field(min_length=1)
    claimant_user_id: str = Field(min_length=1)
    status: ClaimStatus = ClaimStatus.DECLARED
    created_at: datetime = Field(default_factory=utc_now)
    # When the recipient selects one or more discovered options, the full
    # candidates are retained on the claim (not reduced to bare titles) so the
    # selection remains auditable. Both lists stay empty for a custom-topic
    # claim; ``selected_candidates`` is always in canonical display order.
    selected_candidate_ids: list[str] = Field(default_factory=list)
    selected_candidates: list["ClaimCandidate"] = Field(default_factory=list)


class SourceFragment(StrictModel):
    source_message_id: str = Field(min_length=1)
    exact_quote: str = Field(min_length=1)
    unit_ids: list[str] = Field(default_factory=list)
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def optional_offsets_match(self) -> "SourceFragment":
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("start_offset and end_offset must be supplied together")
        if self.start_offset is not None and self.end_offset <= self.start_offset:
            raise ValueError("fragment offsets are reversed")
        return self


class ClaimAnalysis(StrictModel):
    raw_topic: str = Field(min_length=1)
    normalized_label: str = Field(min_length=1)
    matching_fragments: list[SourceFragment] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)


class ClaimCandidate(StrictModel):
    """One selectable, source-backed claim option with exact evidence."""

    candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1)
    evidence: list[SourceFragment] = Field(min_length=1)
    display_order: int = Field(ge=1)


class ClaimCandidateSet(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(min_length=1)
    candidates: list[ClaimCandidate] = Field(min_length=2, max_length=5)


class ClaimDiscoveryRequest(StrictModel):
    bundle_id: str
    messages: list[SourceMessage] = Field(min_length=1)
    source_units: list[SourceUnit] = Field(min_length=1)
    prompt_version: str = "nemoir-claim-discovery-v1"


class ClaimDiscoveryResponse(StrictModel):
    result: ClaimCandidateSet
    receipt: ProviderReceipt
    raw_response: str | None = None


class TendrilCandidate(StrictModel):
    client_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1)
    type: TendrilType
    actionability: Actionability
    evidence: list[SourceFragment] = Field(min_length=1)
    why_open: str = Field(min_length=1)
    suggested_habitat_slug: str | None = None
    habitat_reasoning: str | None = None
    confidence: float = Field(ge=0, le=1)
    overlap_explanation: str | None = None


class CoverageEntry(StrictModel):
    source_message_id: str = Field(min_length=1)
    unit_id: str = Field(min_length=1)
    classification: CoverageClassification
    tendril_client_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class AnalysisResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    claim: ClaimAnalysis
    tendrils: list[TendrilCandidate] = Field(default_factory=list)
    coverage: list[CoverageEntry] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class ProviderReceipt(StrictModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    outcome: str = Field(min_length=1)
    request_id: str | None = None


class PromptRequest(StrictModel):
    """One stateless ordinary question for the single-turn prompt provider."""

    question: str = Field(min_length=1)
    max_output_tokens: int = Field(default=1024, ge=1)
    prompt_version: str = "nemoir-prompt-v1"


class PromptResponse(StrictModel):
    """One unstructured textual answer plus its provider receipt.

    ``answer`` is deliberately not length-constrained so the service can
    detect and classify an empty completion instead of failing to construct
    the response at all.
    """

    answer: str
    receipt: ProviderReceipt
    raw_response: str | None = None


class PromptAttempt(StrictModel):
    """Restart-safe, additive record of one prompt delivery attempt."""

    id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    guild_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    status: PromptAttemptStatus = PromptAttemptStatus.RUNNING
    receipt: ProviderReceipt | None = None
    response_text: str | None = None
    failure_classification: PromptFailureClass | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class AnalysisRequest(StrictModel):
    bundle_id: str
    messages: list[SourceMessage] = Field(min_length=1)
    source_units: list[SourceUnit] = Field(min_length=1)
    raw_claim: str = Field(min_length=1)
    # When the recipient selected one or more discovered options, the full
    # candidates (id, title, summary, and exact evidence) travel with the
    # request so the second-stage decomposition treats their combined evidence
    # as the authoritative claim boundary. Always canonical display order.
    selected_candidates: list[ClaimCandidate] = Field(default_factory=list)
    # All persisted discovery candidates, so the second stage cannot silently
    # demote any source-backed candidate evidence to CONTEXT.
    discovered_candidates: list[ClaimCandidate] = Field(default_factory=list)
    habitat_descriptions: list[str] = Field(default_factory=list)
    prompt_version: str = "nemoir-analysis-v1"


class AnalysisResponse(StrictModel):
    result: AnalysisResult
    receipt: ProviderReceipt
    raw_response: str | None = None


class Tendril(StrictModel):
    id: str
    bundle_id: str
    title: str
    description: str
    type: TendrilType
    actionability: Actionability
    evidence: list[SourceFragment]
    why_open: str
    suggested_habitat_slug: str | None = None
    habitat_reasoning: str | None = None
    confidence: float = Field(ge=0, le=1)
    status: TendrilState = TendrilState.OPEN
    routed_platform: str | None = None
    routed_external_id: str | None = None
    snoozed_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LifecycleEvent(StrictModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    entity_type: Literal["bundle", "tendril"]
    entity_id: str
    prior_state: str | None = None
    new_state: str
    actor_user_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    idempotency_key: str
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ExternalOperation(StrictModel):
    id: str = Field(min_length=1)
    operation_type: Literal["route", "habitat_create"]
    idempotency_key: str = Field(min_length=1)
    status: ExternalOperationStatus
    platform: str = Field(min_length=1)
    tendril_id: str = Field(min_length=1)
    external_destination_id: str | None = None
    external_message_id: str | None = None
    actor_user_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ReviewResult(StrictModel):
    bundle_id: str
    bundle_status: BundleState
    claim: ClaimAnalysis
    tendrils: list[Tendril]
    coverage: list[CoverageEntry]
    unresolved: list[str]
    receipt: ProviderReceipt


# A readable, deterministic separator used to join selected candidate titles
# into the pre-analysis ``raw_topic``. The second-stage normalized label may
# still produce a more natural final title; this is only the intermediate join.
SELECTED_TOPIC_SEPARATOR = " + "


def join_selected_titles(candidates: list[ClaimCandidate]) -> str:
    """Join selected candidate titles in canonical display order.

    Used to build the pre-analysis ``raw_topic`` for a combined claim without
    adding a third model call merely to name it.
    """
    ordered = sorted(candidates, key=lambda candidate: candidate.display_order)
    return SELECTED_TOPIC_SEPARATOR.join(candidate.title for candidate in ordered)


def authoritative_claim_boundary(candidates: list[ClaimCandidate]) -> list[SourceFragment]:
    """Deterministic union of the exact evidence across selected candidates.

    The combined evidence of every selected candidate is the authoritative
    claim boundary. Identical quotations (same source message and exact text)
    are deduplicated while their ``unit_ids`` are merged so no source
    attribution is lost; fragments are returned in canonical display order.
    """
    by_key: dict[tuple[str, str], SourceFragment] = {}
    for candidate in sorted(candidates, key=lambda item: item.display_order):
        for fragment in candidate.evidence:
            key = (fragment.source_message_id, fragment.exact_quote)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = fragment.model_copy(deep=True)
                continue
            merged_unit_ids = list(dict.fromkeys([*existing.unit_ids, *fragment.unit_ids]))
            start_offset = existing.start_offset
            end_offset = existing.end_offset
            if existing.start_offset is None and fragment.start_offset is not None:
                start_offset = fragment.start_offset
                end_offset = fragment.end_offset
            by_key[key] = existing.model_copy(
                update={
                    "unit_ids": merged_unit_ids,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                }
            )
    return list(by_key.values())


class AutonomousTopic(StrictModel):
    """One validated autonomous-sort output topic; persisted as a tendril.

    Autonomous sorting has no recipient claim and no claim/selected-candidate
    stage: every sealed source unit is assigned to exactly one primary topic,
    and each topic is fully source-backed. The fields mirror the tendril
    persistence shape so the existing lifecycle, routing, publishing, exports,
    and evidence views keep working unchanged.
    """

    provider_client_id: str = Field(min_length=1)
    display_order: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1)
    tendril_type: TendrilType
    actionability: Actionability
    evidence: list[SourceFragment] = Field(min_length=1)
    why_open: str = Field(min_length=1)
    suggested_habitat_slug: str | None = None
    confidence: float = Field(ge=0, le=1)


class AutonomousSortResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(min_length=1)
    topics: list[AutonomousTopic] = Field(min_length=1)
    # Exactly one coverage entry per source unit; every entry is a TENDRIL
    # classification targeting exactly one topic (autonomous sorts never
    # produce CLAIMED or CONTEXT classifications).
    coverage: list[CoverageEntry] = Field(min_length=1)
    unresolved: list[str] = Field(default_factory=list)


class AutonomousSortRequest(StrictModel):
    bundle_id: str
    messages: list[SourceMessage] = Field(min_length=1)
    source_units: list[SourceUnit] = Field(min_length=1)
    prompt_version: str = "nemoir-autonomous-sort-v1"


class AutonomousSortResponse(StrictModel):
    result: AutonomousSortResult
    receipt: ProviderReceipt
    raw_response: str | None = None


class AutonomousJob(StrictModel):
    """One durable autonomous job per bundle (sort then publish)."""

    id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    phase: AutonomousJobPhase = AutonomousJobPhase.QUEUED
    # The next attempt key the worker must use. Set when the job is queued (or
    # retried); consumed atomically when the attempt is created and the phase
    # moves QUEUED -> REQUEST_STARTED, so a crash after that boundary can never
    # silently re-send the same paid request.
    pending_attempt_key: str | None = None
    provider: str | None = None
    model: str | None = None
    last_notification: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AutonomousAttempt(StrictModel):
    """One bounded autonomous-sort provider attempt inside a job."""

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: AutonomousAttemptStatus = AutonomousAttemptStatus.RUNNING
    prompt_version: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    raw_response: str | None = None
    result_json: str | None = None
    validation_json: str | None = None
    receipt: ProviderReceipt | None = None
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
