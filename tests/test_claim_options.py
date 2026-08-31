"""Focused tests for the two-stage claim-option workflow (service layer)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.application.claim_options_service import ClaimOptionsService
from nemoir.domain.errors import AuthorizationError, ConflictError
from nemoir.domain.models import (
    AnalysisResponse,
    AnalysisResult,
    Claim,
    ClaimAnalysis,
    ClaimCandidate,
    ClaimDiscoveryRequest,
    ConversationBundle,
    CoverageEntry,
    ProviderReceipt,
    SourceFragment,
    SourceMessage,
    TendrilCandidate,
    new_id,
)
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.states import (
    Actionability,
    BundleState,
    CoverageClassification,
    TendrilType,
)
from nemoir.domain.validation import validate_analysis, validate_claim_candidates
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.fake import FakeAnalysisProvider
from nemoir.providers.synthetic import SyntheticClaimDiscoveryProvider, synthetic_claim_candidates


def _seal(repository, synthetic_messages):
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    for message in synthetic_messages:
        capture.capture_message(
            bundle.id,
            actor_user_id="person-1",
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id="intake-1",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    return capture.seal(bundle.id, actor_user_id="person-1", idempotency_key="claim-options-seal")


@pytest.mark.asyncio
async def test_candidate_persistence_and_stable_ordering(
    repository, synthetic_messages, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = SyntheticClaimDiscoveryProvider()
    outcome = await ClaimOptionsService(repository, provider, authorization).discover(
        bundle.id, actor_user_id="person-1", idempotency_key="discover-persist"
    )
    assert outcome.state == BundleState.CLAIM_OPTIONS_READY
    assert outcome.candidates is not None
    candidates = outcome.candidates
    assert 2 <= len(candidates) <= 5
    assert [candidate.display_order for candidate in candidates] == list(
        range(1, len(candidates) + 1)
    )
    assert len({candidate.candidate_id for candidate in candidates}) == len(candidates)

    path = repository.database_path
    repository.close()
    reopened = SQLiteRepository(path)
    try:
        reloaded = reopened.list_claim_candidates(bundle.id)
        assert [candidate.model_dump() for candidate in reloaded] == [
            candidate.model_dump() for candidate in candidates
        ]
        # Order is deterministic across regeneration.
        assert provider.call_count == 1
    finally:
        reopened.close()


def test_exact_quote_and_span_validation(repository, synthetic_messages) -> None:
    bundle = repository.get_bundle(_seal(repository, synthetic_messages).id)
    candidate_set = synthetic_claim_candidates(
        ClaimDiscoveryRequest(
            bundle_id=bundle.id,
            messages=bundle.source_messages,
            source_units=bundle.source_units,
        )
    )
    report = validate_claim_candidates(bundle, bundle.source_units, candidate_set)
    assert report.valid
    assert not report.errors

    invented = candidate_set.model_copy(deep=True)
    invented.candidates[0].evidence[0].exact_quote = "This quotation never existed."
    report = validate_claim_candidates(bundle, bundle.source_units, invented)
    assert not report.valid
    assert "INVENTED_QUOTE" in {issue.code for issue in report.errors}

    bad_span = candidate_set.model_copy(deep=True)
    bad_span.candidates[0].evidence[0].end_offset = 5
    bad_span.candidates[0].evidence[0].start_offset = 1
    report = validate_claim_candidates(bundle, bundle.source_units, bad_span)
    assert "INVALID_FRAGMENT_OFFSETS" in {issue.code for issue in report.errors}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant, expected_code",
    [
        ("invented_quote", "INVENTED_QUOTE"),
        ("unknown_message", "UNKNOWN_SOURCE_MESSAGE"),
        ("duplicate_id", "DUPLICATE_CANDIDATE_ID"),
        ("bad_order", "CANDIDATE_ORDER"),
        ("empty_title", "EMPTY_CANDIDATE_TITLE"),
    ],
)
async def test_hallucinated_or_malformed_candidate_rejection(
    repository, synthetic_messages, authorization, variant, expected_code
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = SyntheticClaimDiscoveryProvider(variant=variant)
    outcome = await ClaimOptionsService(repository, provider, authorization).discover(
        bundle.id, actor_user_id="person-1", idempotency_key=f"discover-{variant}"
    )
    assert outcome.state == BundleState.CLAIM_OPTIONS_FAILED
    assert not outcome.validation.valid
    assert expected_code in {issue.code for issue in outcome.validation.errors}
    assert repository.list_claim_candidates(bundle.id) == []
    attempt = repository.get_claim_discovery_attempt(outcome.attempt_id)
    assert attempt["status"] == "FAILED"
    assert attempt["validation_json"] is not None  # failure remains reviewable


@pytest.mark.asyncio
async def test_provider_failure_remains_reviewable(
    repository, synthetic_messages, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = SyntheticClaimDiscoveryProvider(variant="provider_failure")
    outcome = await ClaimOptionsService(repository, provider, authorization).discover(
        bundle.id, actor_user_id="person-1", idempotency_key="discover-failure"
    )
    assert outcome.state == BundleState.CLAIM_OPTIONS_FAILED
    assert repository.get_bundle(bundle.id).status == BundleState.CLAIM_OPTIONS_FAILED


def test_within_message_clustering(synthetic_messages) -> None:
    units = segment_messages(synthetic_messages)
    result = synthetic_claim_candidates(
        ClaimDiscoveryRequest(bundle_id="b", messages=synthetic_messages, source_units=units)
    )
    first = result.candidates[0]
    # The three compassion/free-will paragraphs of one message cohere.
    assert {fragment.source_message_id for fragment in first.evidence} == {"synthetic-102"}
    assert [fragment.exact_quote for fragment in first.evidence] == [
        "Free will matters only if choices have consequences.",
        "If an AI only repeats compassionate language, has it learned compassion or merely reproduced its shape?",
        "A system choosing between a devastating short war and a longer war with greater suffering would stress-test what consequential compassion means.",
    ]
    assert "free will" in first.title.lower()
    assert "compassion" in first.title.lower()


def test_cross_message_clustering() -> None:
    messages = [
        SourceMessage(
            platform="discord",
            external_message_id="m-1",
            author_user_id="a",
            author_display_name="A",
            channel_id="c",
            content=(
                "Consciousness may be an emergent property of brains.\n\n"
                "The coastline erodes each winter."
            ),
            source_url="https://discord.invalid/m-1",
            timestamp=datetime.now(timezone.utc),
            ordinal=0,
        ),
        SourceMessage(
            platform="discord",
            external_message_id="m-2",
            author_user_id="a",
            author_display_name="A",
            channel_id="c",
            content="Emergent behavior appears across many systems.",
            source_url="https://discord.invalid/m-2",
            timestamp=datetime.now(timezone.utc),
            ordinal=1,
        ),
    ]
    result = synthetic_claim_candidates(
        ClaimDiscoveryRequest(bundle_id="b", messages=messages, source_units=segment_messages(messages))
    )
    cross = [candidate for candidate in result.candidates if len(candidate.evidence) > 1]
    assert cross
    assert {fragment.source_message_id for fragment in cross[0].evidence} == {"m-1", "m-2"}


@pytest.mark.asyncio
async def test_selecting_a_valid_option(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="select-valid")
    candidates = repository.list_claim_candidates(bundle.id)

    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])
    assert claim.raw_topic == candidates[0].title
    assert claim.selected_candidate_ids == [candidates[0].candidate_id]
    assert claim.selected_candidates == [candidates[0]]
    assert claim.selected_candidates[0].evidence == candidates[0].evidence

    persisted = repository.get_claim_for_bundle(bundle.id)
    assert persisted.selected_candidate_ids == [candidates[0].candidate_id]
    assert persisted.selected_candidates[0].evidence == candidates[0].evidence

    outcome = await AnalysisService(repository, provider, authorization).analyse(
        bundle.id, actor_user_id="person-2", idempotency_key="select-valid-analysis"
    )
    assert outcome.review is not None
    assert outcome.review.claim is not None


@pytest.mark.asyncio
async def test_invalid_option_numbers(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="invalid-options")

    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[0])
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[99])
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2")
    with pytest.raises(ConflictError):
        options.select(
            bundle.id,
            actor_user_id="person-2",
            option_numbers=[1],
            custom_topic="both supplied",
        )


def test_custom_claim_fallback(repository, synthetic_messages, authorization) -> None:
    bundle = _seal(repository, synthetic_messages)
    options = ClaimOptionsService(
        repository, SyntheticClaimDiscoveryProvider(), authorization
    )
    claim = options.select(
        bundle.id, actor_user_id="person-2", custom_topic="My custom subject"
    )
    assert claim.raw_topic == "My custom subject"
    assert claim.selected_candidates == []
    assert claim.selected_candidate_ids == []


@pytest.mark.asyncio
async def test_discover_and_select_authorization(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)

    # Discovery belongs to the capture owner or an administrator, not the recipient.
    with pytest.raises(AuthorizationError):
        await options.discover(bundle.id, actor_user_id="person-2", idempotency_key="authz-d-recipient")
    await options.discover(bundle.id, actor_user_id="admin-1", idempotency_key="authz-d-admin")

    # Inspection and selection belong to the recipient or an administrator.
    with pytest.raises(AuthorizationError):
        options.get_options(bundle.id, actor_user_id="intruder")
    with pytest.raises(AuthorizationError):
        options.get_options(bundle.id, actor_user_id="person-1")
    with pytest.raises(AuthorizationError):
        options.select(bundle.id, actor_user_id="person-1", option_numbers=[1])
    assert options.get_options(bundle.id, actor_user_id="person-2")
    assert options.select(bundle.id, actor_user_id="admin-1", option_numbers=[1])


def test_ambiguous_pending_bundles(repository, synthetic_messages) -> None:
    first = _seal(repository, synthetic_messages)
    capture = CaptureService(repository)
    second = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    for message in synthetic_messages:
        capture.capture_message(
            second.id,
            actor_user_id="person-1",
            external_message_id=f"{message.external_message_id}-2",
            author_display_name=message.author_display_name,
            channel_id="intake-1",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    capture.seal(second.id, actor_user_id="person-1", idempotency_key="second-seal")

    pending = repository.list_pending_bundles("person-2")
    assert {bundle.id for bundle in pending} == {first.id, second.id}


@pytest.mark.asyncio
async def test_idempotent_retries_and_select_replay(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = SyntheticClaimDiscoveryProvider()
    options = ClaimOptionsService(repository, provider, authorization)

    first = await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="idem-disc")
    second = await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="idem-disc")
    assert first.replayed is False
    assert second.replayed is True
    assert provider.call_count == 1

    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])
    assert claim.selected_candidates
    with pytest.raises(ConflictError):
        options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])


def test_migration_of_existing_database(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_meta(version, applied_at) VALUES (1, '2026-08-27T00:00:00Z');
        CREATE TABLE claims (
            id TEXT PRIMARY KEY,
            bundle_id TEXT NOT NULL UNIQUE,
            raw_topic TEXT NOT NULL,
            claimant_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    repository = SQLiteRepository(db_path)
    try:
        columns = {
            row["name"]
            for row in repository._connection.execute("PRAGMA table_info(claims)").fetchall()
        }
        assert "selected_candidate_id" in columns
        assert "selected_candidate_json" in columns
        assert "selected_candidate_ids_json" in columns
        assert "selected_candidates_json" in columns
        repository._connection.execute("SELECT * FROM claim_candidates")
        repository._connection.execute("SELECT * FROM claim_discovery_attempts")
        version = repository._connection.execute(
            "SELECT version FROM schema_meta"
        ).fetchone()["version"]
        assert version == 4
    finally:
        repository.close()


def _fragment_for_unit(unit) -> SourceFragment:
    return SourceFragment(
        source_message_id=unit.source_message_id,
        exact_quote=unit.exact_text,
        unit_ids=[unit.unit_id],
        start_offset=unit.start_offset,
        end_offset=unit.end_offset,
    )


@pytest.mark.asyncio
async def test_analysis_request_carries_full_selected_candidate(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="carry-discover")
    claim = options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])

    class InspectingProvider(FakeAnalysisProvider):
        async def analyse(self, request):
            assert request.selected_candidates
            assert request.selected_candidates[0].candidate_id == claim.selected_candidate_ids[0]
            assert request.selected_candidates[0].evidence == claim.selected_candidates[0].evidence
            return await super().analyse(request)

    outcome = await AnalysisService(
        repository, InspectingProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="carry-analysis")
    assert outcome.review is not None
    expected = {fragment.exact_quote for fragment in claim.selected_candidates[0].evidence}
    actual = {fragment.exact_quote for fragment in outcome.review.claim.matching_fragments}
    assert actual == expected


@pytest.mark.asyncio
async def test_similar_titles_with_different_evidence_are_not_confused(
    repository, synthetic_messages, authorization
) -> None:
    bundle = repository.get_bundle(_seal(repository, synthetic_messages).id)
    units = bundle.source_units
    # Two candidates share a title but carry different source evidence.
    selected = ClaimCandidate(
        candidate_id="cand-a",
        title="Decision-making",
        summary="About choices and consequences.",
        evidence=[_fragment_for_unit(units[2])],  # synthetic-102:p1 (free will)
        display_order=1,
    )
    other = ClaimCandidate(
        candidate_id="cand-b",
        title="Decision-making",
        summary="About learned compassion.",
        evidence=[_fragment_for_unit(units[3])],  # synthetic-102:p2 (AI compassion)
        display_order=2,
    )
    assert selected.title == other.title
    assert selected.evidence != other.evidence
    claim = Claim(
        id=new_id("claim"),
        bundle_id=bundle.id,
        raw_topic="Decision-making",
        claimant_user_id="person-2",
        selected_candidate_ids=[selected.candidate_id],
        selected_candidates=[selected],
    )
    repository.create_claim(claim)

    class ConfusedProvider:
        """Returns the other candidate's evidence despite the selection."""

        async def analyse(self, request):
            wrong_unit = units[3]  # the other candidate's source unit
            coverage = [
                CoverageEntry(
                    source_message_id=unit.source_message_id,
                    unit_id=unit.unit_id,
                    classification=CoverageClassification.CONTEXT,
                    tendril_client_ids=[],
                    reason="context",
                    confidence=1.0,
                )
                for unit in units
            ]
            result = AnalysisResult(
                claim=ClaimAnalysis(
                    raw_topic=request.raw_claim,
                    normalized_label=request.raw_claim,
                    matching_fragments=[_fragment_for_unit(wrong_unit)],
                    confidence=1.0,
                    rationale="confused",
                ),
                tendrils=[],
                coverage=coverage,
                unresolved=[],
            )
            return AnalysisResponse(
                result=result,
                receipt=ProviderReceipt(
                    provider="fake",
                    model="confused",
                    latency_ms=0,
                    outcome="success",
                ),
            )

    outcome = await AnalysisService(
        repository, ConfusedProvider(), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="confused-analysis")
    assert outcome.state == BundleState.ANALYSIS_FAILED
    codes = {issue.code for issue in outcome.validation.errors}
    assert "SELECTED_EVIDENCE_DROPPED" in codes
    assert "SELECTED_EVIDENCE_REPLACED" in codes


@pytest.mark.asyncio
async def test_selected_candidate_id_stable_across_lifecycle(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="stable-id-disc")

    displayed = repository.list_claim_candidates(bundle.id)
    chosen = displayed[0]
    # Selection resolves the persisted candidate by display order, never by a
    # provider-supplied identifier supplied by the caller.
    claim = options.select(
        bundle.id, actor_user_id="person-2", option_numbers=[chosen.display_order]
    )
    assert claim.selected_candidate_ids == [chosen.candidate_id]

    path = repository.database_path
    repository.close()
    reopened = SQLiteRepository(path)
    try:
        reloaded = reopened.list_claim_candidates(bundle.id)
        assert reloaded[0].candidate_id == chosen.candidate_id
        persisted_claim = reopened.get_claim_for_bundle(bundle.id)
        assert persisted_claim.selected_candidate_ids == [chosen.candidate_id]
        assert persisted_claim.selected_candidates[0].candidate_id == chosen.candidate_id

        reopened_auth = AuthorizationPolicy(reopened, admin_user_ids={"admin-1"})
        outcome = await AnalysisService(
            reopened, FakeAnalysisProvider(synthetic_analysis), reopened_auth
        ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="stable-id-analysis")
        assert outcome.review is not None
        expected = {fragment.exact_quote for fragment in chosen.evidence}
        assert {fragment.exact_quote for fragment in outcome.review.claim.matching_fragments} == expected
    finally:
        reopened.close()


# --- Continuity invariant helpers and tests ---------------------------------


def _analysis_bundle(synthetic_messages) -> ConversationBundle:
    return ConversationBundle(
        id="bundle-continuity",
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
        source_messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
    )


def _cov(unit, classification, client_ids=(), reason="reason"):
    return CoverageEntry(
        source_message_id=unit.source_message_id,
        unit_id=unit.unit_id,
        classification=classification,
        tendril_client_ids=list(client_ids),
        reason=reason,
        confidence=1.0,
    )


def _tendril(client_id: str, units) -> TendrilCandidate:
    return TendrilCandidate(
        client_id=client_id,
        title=f"Tendril {client_id}",
        description="description",
        type=TendrilType.INTERESTING,
        actionability=Actionability.NOT_ACTIONABLE,
        evidence=[_fragment_for_unit(unit) for unit in units],
        why_open="still open",
        confidence=0.9,
    )


def _empty_claim(label: str) -> ClaimAnalysis:
    return ClaimAnalysis(
        raw_topic=label,
        normalized_label=label,
        matching_fragments=[],
        confidence=1.0,
        rationale="no inference",
    )


@pytest.mark.asyncio
async def test_custom_claim_preserves_all_discovered_evidence(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="continuity-disc")
    claim = options.select(
        bundle.id,
        actor_user_id="person-2",
        custom_topic="Free will and compassionate AI decision-making",
    )
    assert claim.selected_candidates == []

    outcome = await AnalysisService(repository, provider, authorization).analyse(
        bundle.id, actor_user_id="person-2", idempotency_key="continuity-analysis"
    )
    assert outcome.state == BundleState.REVIEW_READY
    assert outcome.validation.valid
    review = outcome.review
    assert {fragment.exact_quote for fragment in review.claim.matching_fragments} == {
        "Free will matters only if choices have consequences.",
        "If an AI only repeats compassionate language, has it learned compassion or merely reproduced its shape?",
        "A system choosing between a devastating short war and a longer war with greater suffering would stress-test what consequential compassion means.",
    }
    assert {fragment.exact_quote for t in review.tendrils for fragment in t.evidence} == {
        "Could unfamiliar life sit outside the assumptions built into how we observe the universe?",
        "I keep wondering whether time can be both eternal and an illusion.",
        "Maybe AI should be one lens rather than the only perspective.",
        "We should build a tool that catalogues long human and AI chats, separates unfinished branches, and lets us return to them.",
    }
    assert all(
        entry.classification != CoverageClassification.CONTEXT for entry in review.coverage
    )


@pytest.mark.asyncio
async def test_numbered_selection_preserves_all_discovered_evidence(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="num-continuity-disc")
    options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])

    outcome = await AnalysisService(repository, provider, authorization).analyse(
        bundle.id, actor_user_id="person-2", idempotency_key="num-continuity-analysis"
    )
    assert outcome.state == BundleState.REVIEW_READY
    assert outcome.validation.valid
    assert all(
        entry.classification != CoverageClassification.CONTEXT for entry in outcome.review.coverage
    )


def test_merging_candidates_into_one_tendril_is_valid(synthetic_messages) -> None:
    bundle = _analysis_bundle(synthetic_messages)
    units = bundle.source_units
    candidates = [
        ClaimCandidate(candidate_id="c1", title="A", summary="s", evidence=[_fragment_for_unit(units[0])], display_order=1),
        ClaimCandidate(candidate_id="c2", title="B", summary="s", evidence=[_fragment_for_unit(units[1])], display_order=2),
    ]
    merged = _tendril("t-merged", [units[0], units[1]])
    coverage = (
        [_cov(units[0], CoverageClassification.TENDRIL, ["t-merged"]),
         _cov(units[1], CoverageClassification.TENDRIL, ["t-merged"])]
        + [_cov(unit, CoverageClassification.CONTEXT) for unit in units[2:]]
    )
    result = AnalysisResult(
        claim=_empty_claim("custom"),
        tendrils=[merged],
        coverage=coverage,
        unresolved=[],
    )
    report = validate_analysis(bundle, units, result, discovered_candidates=candidates)
    assert report.valid


def test_splitting_one_candidate_across_tendrils_is_valid(synthetic_messages) -> None:
    bundle = _analysis_bundle(synthetic_messages)
    units = bundle.source_units
    candidate = ClaimCandidate(
        candidate_id="c1",
        title="A",
        summary="s",
        evidence=[_fragment_for_unit(units[0]), _fragment_for_unit(units[1])],
        display_order=1,
    )
    coverage = (
        [_cov(units[0], CoverageClassification.TENDRIL, ["t1"]),
         _cov(units[1], CoverageClassification.TENDRIL, ["t2"])]
        + [_cov(unit, CoverageClassification.CONTEXT) for unit in units[2:]]
    )
    result = AnalysisResult(
        claim=_empty_claim("custom"),
        tendrils=[_tendril("t1", [units[0]]), _tendril("t2", [units[1]])],
        coverage=coverage,
        unresolved=[],
    )
    report = validate_analysis(bundle, units, result, discovered_candidates=[candidate])
    assert report.valid


def test_overlapping_candidate_evidence_is_deduped(synthetic_messages) -> None:
    bundle = _analysis_bundle(synthetic_messages)
    units = bundle.source_units
    shared = _fragment_for_unit(units[0])
    candidates = [
        ClaimCandidate(candidate_id="c1", title="A", summary="s", evidence=[shared], display_order=1),
        ClaimCandidate(candidate_id="c2", title="B", summary="s", evidence=[shared], display_order=2),
    ]
    coverage = (
        [_cov(units[0], CoverageClassification.TENDRIL, ["t1"])]
        + [_cov(unit, CoverageClassification.CONTEXT) for unit in units[1:]]
    )
    result = AnalysisResult(
        claim=_empty_claim("custom"),
        tendrils=[_tendril("t1", [units[0]])],
        coverage=coverage,
        unresolved=[],
    )
    report = validate_analysis(bundle, units, result, discovered_candidates=candidates)
    assert report.valid


def test_dropped_discovered_evidence_is_rejected(synthetic_messages) -> None:
    bundle = _analysis_bundle(synthetic_messages)
    units = bundle.source_units
    candidate = ClaimCandidate(
        candidate_id="c1", title="A", summary="s", evidence=[_fragment_for_unit(units[0])], display_order=1
    )
    coverage = (
        [_cov(units[0], CoverageClassification.CLAIMED)]
        + [_cov(unit, CoverageClassification.CONTEXT) for unit in units[1:]]
    )
    result = AnalysisResult(
        claim=_empty_claim("custom"),
        tendrils=[],
        coverage=coverage,
        unresolved=[],
    )
    report = validate_analysis(bundle, units, result, discovered_candidates=[candidate])
    assert "DISCOVERED_EVIDENCE_DROPPED" in {issue.code for issue in report.errors}


def test_discovered_evidence_as_context_is_rejected(synthetic_messages) -> None:
    bundle = _analysis_bundle(synthetic_messages)
    units = bundle.source_units
    candidate = ClaimCandidate(
        candidate_id="c1", title="A", summary="s", evidence=[_fragment_for_unit(units[0])], display_order=1
    )
    coverage = [_cov(unit, CoverageClassification.CONTEXT) for unit in units]
    result = AnalysisResult(
        claim=_empty_claim("custom"),
        tendrils=[],
        coverage=coverage,
        unresolved=[],
    )
    report = validate_analysis(bundle, units, result, discovered_candidates=[candidate])
    assert "DISCOVERED_EVIDENCE_MISCLASSIFIED" in {issue.code for issue in report.errors}


def test_tenril_coverage_without_matching_evidence_is_rejected(synthetic_messages) -> None:
    bundle = _analysis_bundle(synthetic_messages)
    units = bundle.source_units
    candidate = ClaimCandidate(
        candidate_id="c1", title="A", summary="s", evidence=[_fragment_for_unit(units[0])], display_order=1
    )
    coverage = (
        [_cov(units[0], CoverageClassification.TENDRIL, ["t-other"]),
         _cov(units[1], CoverageClassification.TENDRIL, ["t-other"])]
        + [_cov(unit, CoverageClassification.CONTEXT) for unit in units[2:]]
    )
    result = AnalysisResult(
        claim=_empty_claim("custom"),
        tendrils=[_tendril("t-other", [units[1]])],
        coverage=coverage,
        unresolved=[],
    )
    report = validate_analysis(bundle, units, result, discovered_candidates=[candidate])
    assert "DISCOVERED_EVIDENCE_MISCLASSIFIED" in {issue.code for issue in report.errors}


@pytest.mark.asyncio
async def test_no_discovery_candidates_keeps_legacy_behavior(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    outcome = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(captured_bundle.id, actor_user_id="person-2", idempotency_key="legacy-continuity")
    assert outcome.review is not None
    assert outcome.state == BundleState.REVIEW_READY
    codes = {issue.code for issue in outcome.validation.errors}
    assert "DISCOVERED_EVIDENCE_DROPPED" not in codes
    assert "DISCOVERED_EVIDENCE_MISCLASSIFIED" not in codes


@pytest.mark.asyncio
async def test_restart_replay_passes_persisted_candidates_to_analysis(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle = _seal(repository, synthetic_messages)
    provider = FakeAnalysisProvider(synthetic_analysis)
    options = ClaimOptionsService(repository, provider, authorization)
    await options.discover(bundle.id, actor_user_id="person-1", idempotency_key="restart-disc")
    options.select(bundle.id, actor_user_id="person-2", option_numbers=[1])

    path = repository.database_path
    repository.close()
    reopened = SQLiteRepository(path)
    try:
        seen: dict[str, object] = {}

        class Inspect(FakeAnalysisProvider):
            async def analyse(self, request):
                seen["discovered"] = request.discovered_candidates
                return await super().analyse(request)

        reopened_auth = AuthorizationPolicy(reopened, admin_user_ids={"admin-1"})
        outcome = await AnalysisService(
            reopened, Inspect(synthetic_analysis), reopened_auth
        ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="restart-analysis")
        assert outcome.review is not None
        assert outcome.validation.valid
        assert len(seen["discovered"]) == len(reopened.list_claim_candidates(bundle.id))
    finally:
        reopened.close()
