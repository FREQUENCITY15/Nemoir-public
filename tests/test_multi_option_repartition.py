"""Regression: multi-option adaptation is a complete evidence repartition.

Selecting a boundary that differs from the fake provider's original claim used
to drop the former claim's evidence (``DISCOVERED_EVIDENCE_DROPPED`` for the
compassion and consequential-war quotations). These tests drive the STANDARD
synthetic discovery numbering (option 1 = free will / AI compassion /
consequential war; options 2 and 3 = unfamiliar life / time; options 4 and 5 =
AI-as-one-lens / chat catalogue), select boundaries other than option 1, and
assert that every discovered unit stays claimed or tendrilled exactly once.
"""

from __future__ import annotations

import pytest

from nemoir.application.analysis_service import AnalysisOutcome, AnalysisService
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.application.claim_options_service import ClaimOptionsService
from nemoir.domain.models import (
    ClaimCandidate,
    ClaimDiscoveryRequest,
    SourceFragment,
)
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.states import BundleState, CoverageClassification
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.fake import FakeAnalysisProvider, adapt_analysis_to_selected_candidates
from nemoir.providers.synthetic import SyntheticClaimDiscoveryProvider, synthetic_claim_candidates

FREE_WILL = "Free will matters only if choices have consequences."
AI_COMPASSION = (
    "If an AI only repeats compassionate language, has it learned compassion or "
    "merely reproduced its shape?"
)
WAR_STRESS = (
    "A system choosing between a devastating short war and a longer war with "
    "greater suffering would stress-test what consequential compassion means."
)
UNFAMILIAR_LIFE = (
    "Could unfamiliar life sit outside the assumptions built into how we observe "
    "the universe?"
)
TIME = "I keep wondering whether time can be both eternal and an illusion."
AI_LENS = "Maybe AI should be one lens rather than the only perspective."
CATALOGUE = (
    "We should build a tool that catalogues long human and AI chats, separates "
    "unfinished branches, and lets us return to them."
)

ALL_QUOTES = {
    FREE_WILL,
    AI_COMPASSION,
    WAR_STRESS,
    UNFAMILIAR_LIFE,
    TIME,
    AI_LENS,
    CATALOGUE,
}


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
    return capture.seal(
        bundle.id, actor_user_id="person-1", idempotency_key="repartition-seal"
    )


async def _discover_and_select(repository, synthetic_messages, authorization, option_numbers):
    """Seal with the standard synthetic discovery, select, and return state."""
    bundle = _seal(repository, synthetic_messages)
    options = ClaimOptionsService(repository, SyntheticClaimDiscoveryProvider(), authorization)
    await options.discover(
        bundle.id, actor_user_id="person-1", idempotency_key="repartition-disc"
    )
    candidates = repository.list_claim_candidates(bundle.id)
    claim = options.select(
        bundle.id, actor_user_id="person-2", option_numbers=option_numbers
    )
    return bundle, claim, candidates


def _expected_partition(candidates, option_numbers):
    """Split the discovered evidence into the selected claim and the rest."""
    selected_numbers = set(option_numbers)
    claim_quotes = {
        fragment.exact_quote
        for candidate in candidates
        if candidate.display_order in selected_numbers
        for fragment in candidate.evidence
    }
    all_quotes = {
        fragment.exact_quote for candidate in candidates for fragment in candidate.evidence
    }
    return claim_quotes, all_quotes - claim_quotes


def _assert_repartitioned(outcome: AnalysisOutcome, expected_claim, expected_tendrils) -> None:
    assert outcome.state == BundleState.REVIEW_READY
    assert outcome.validation.valid
    review = outcome.review
    assert review is not None

    assert {fragment.exact_quote for fragment in review.claim.matching_fragments} == set(
        expected_claim
    )
    assert {
        fragment.exact_quote for tendril in review.tendrils for fragment in tendril.evidence
    } == set(expected_tendrils)

    # Every source unit is covered exactly once and never demoted to CONTEXT.
    unit_ids = [entry.unit_id for entry in review.coverage]
    assert len(unit_ids) == 7
    assert len(set(unit_ids)) == 7
    assert all(
        entry.classification != CoverageClassification.CONTEXT for entry in review.coverage
    )

    claimed_units = {
        unit_id
        for fragment in review.claim.matching_fragments
        for unit_id in fragment.unit_ids
    }
    for entry in review.coverage:
        if entry.unit_id in claimed_units:
            assert entry.classification == CoverageClassification.CLAIMED
            assert entry.tendril_client_ids == []
        else:
            assert entry.classification == CoverageClassification.TENDRIL
            assert entry.tendril_client_ids


@pytest.mark.asyncio
async def test_standard_numbering_options_2_3_repartitions_former_claim(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    """The real pilot defect: options 2,3 select a boundary other than option 1."""
    bundle, claim, _candidates = await _discover_and_select(
        repository, synthetic_messages, authorization, [2, 3]
    )
    assert claim.selected_candidate_ids == ["cand-2", "cand-3"]

    outcome = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="std-23-analysis")

    # The composite claim is unfamiliar-life + time; the three former option-1
    # quotations (free will, AI compassion, consequential war) plus AI-as-one-lens
    # and chat-catalogue all become tendrils.
    assert {
        fragment.exact_quote for fragment in outcome.review.claim.matching_fragments
    } == {UNFAMILIAR_LIFE, TIME}
    assert {
        fragment.exact_quote
        for tendril in outcome.review.tendrils
        for fragment in tendril.evidence
    } == {FREE_WILL, AI_COMPASSION, WAR_STRESS, AI_LENS, CATALOGUE}
    _assert_repartitioned(
        outcome,
        {UNFAMILIAR_LIFE, TIME},
        {FREE_WILL, AI_COMPASSION, WAR_STRESS, AI_LENS, CATALOGUE},
    )


def _standard_candidates(synthetic_messages):
    return synthetic_claim_candidates(
        ClaimDiscoveryRequest(
            bundle_id="b",
            messages=synthetic_messages,
            source_units=segment_messages(synthetic_messages),
        )
    ).candidates


def _unit_quotes(analysis):
    mapping = {}
    for fragment in analysis.claim.matching_fragments:
        for unit_id in fragment.unit_ids:
            mapping[unit_id] = fragment.exact_quote
    for tendril in analysis.tendrils:
        for fragment in tendril.evidence:
            for unit_id in fragment.unit_ids:
                mapping[unit_id] = fragment.exact_quote
    return mapping


def test_adapt_repartitions_with_valid_coverage(synthetic_messages, synthetic_analysis) -> None:
    """The adapted result maps every TENDRIL coverage entry to a tendril that
    actually contains that unit's quotation, with no duplicate quotations."""
    candidates = {
        candidate.display_order: candidate
        for candidate in _standard_candidates(synthetic_messages)
    }
    selected = [candidates[2], candidates[3]]

    adapted = adapt_analysis_to_selected_candidates(synthetic_analysis, selected)

    assert {fragment.exact_quote for fragment in adapted.claim.matching_fragments} == {
        UNFAMILIAR_LIFE,
        TIME,
    }

    tendril_quotes = {
        tendril.client_id: {fragment.exact_quote for fragment in tendril.evidence}
        for tendril in adapted.tendrils
    }
    # No duplicate client ids.
    assert len(tendril_quotes) == len(adapted.tendrils)
    # No quotation appears in more than one tendril.
    flattened = [quote for quotes in tendril_quotes.values() for quote in quotes]
    assert len(flattened) == len(set(flattened)) == 5

    quote_by_unit = _unit_quotes(synthetic_analysis)
    claimed_units = {
        unit_id for fragment in adapted.claim.matching_fragments for unit_id in fragment.unit_ids
    }
    for entry in adapted.coverage:
        if entry.unit_id in claimed_units:
            assert entry.classification == CoverageClassification.CLAIMED
            continue
        assert entry.classification == CoverageClassification.TENDRIL
        # The referenced tendril(s) actually contain this unit's quotation.
        for client_id in entry.tendril_client_ids:
            assert quote_by_unit[entry.unit_id] in tendril_quotes[client_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("option_numbers", [[1], [4], [1, 4], [2, 5], [1, 3, 5], [2, 4, 5]])
async def test_arbitrary_combinations_repartition_all_evidence(
    repository, synthetic_messages, synthetic_analysis, authorization, option_numbers
) -> None:
    bundle, _claim, candidates = await _discover_and_select(
        repository, synthetic_messages, authorization, option_numbers
    )
    key = "combo-" + "-".join(map(str, option_numbers))
    outcome = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key=key)
    expected_claim, expected_tendrils = _expected_partition(candidates, option_numbers)
    _assert_repartitioned(outcome, expected_claim, expected_tendrils)


@pytest.mark.asyncio
async def test_selecting_all_candidates_claims_every_unit(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, claim, candidates = await _discover_and_select(
        repository, synthetic_messages, authorization, [1, 2, 3, 4, 5]
    )
    assert [candidate.candidate_id for candidate in claim.selected_candidates] == [
        candidate.candidate_id for candidate in candidates
    ]
    outcome = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="all-analysis")
    assert outcome.state == BundleState.REVIEW_READY
    assert outcome.validation.valid
    review = outcome.review
    assert {fragment.exact_quote for fragment in review.claim.matching_fragments} == ALL_QUOTES
    assert review.tendrils == []
    assert len(review.coverage) == 7
    assert all(
        entry.classification == CoverageClassification.CLAIMED for entry in review.coverage
    )


def _fragment(source_message_id: str, exact_quote: str, unit_ids: list[str]) -> SourceFragment:
    return SourceFragment(
        source_message_id=source_message_id, exact_quote=exact_quote, unit_ids=unit_ids
    )


def test_overlapping_selected_candidates_dedupe_claim_and_coverage(
    synthetic_analysis,
) -> None:
    """Two selected candidates sharing a quotation deduplicate in the claim."""
    shared = _fragment("synthetic-101", UNFAMILIAR_LIFE, ["synthetic-101:p1"])
    time = _fragment("synthetic-101", TIME, ["synthetic-101:p2"])
    first = ClaimCandidate(
        candidate_id="cand-a", title="A", summary="s", evidence=[shared], display_order=1
    )
    second = ClaimCandidate(
        candidate_id="cand-b",
        title="B",
        summary="s",
        evidence=[shared, time],
        display_order=2,
    )
    adapted = adapt_analysis_to_selected_candidates(synthetic_analysis, [first, second])

    assert {fragment.exact_quote for fragment in adapted.claim.matching_fragments} == {
        UNFAMILIAR_LIFE,
        TIME,
    }
    shared_fragments = [
        fragment
        for fragment in adapted.claim.matching_fragments
        if fragment.exact_quote == UNFAMILIAR_LIFE
    ]
    assert len(shared_fragments) == 1
    assert shared_fragments[0].unit_ids == ["synthetic-101:p1"]

    unit_ids = [entry.unit_id for entry in adapted.coverage]
    assert len(unit_ids) == len(set(unit_ids)) == 7
    assert all(
        entry.classification != CoverageClassification.CONTEXT for entry in adapted.coverage
    )


@pytest.mark.asyncio
async def test_restart_and_idempotent_replay_repartition(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    bundle, _claim, _candidates = await _discover_and_select(
        repository, synthetic_messages, authorization, [2, 3]
    )
    first = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="replay-analysis")
    _assert_repartitioned(
        first,
        {UNFAMILIAR_LIFE, TIME},
        {FREE_WILL, AI_COMPASSION, WAR_STRESS, AI_LENS, CATALOGUE},
    )

    path = repository.database_path
    repository.close()
    reopened = SQLiteRepository(path)
    try:
        reopened_auth = AuthorizationPolicy(reopened, admin_user_ids={"admin-1"})
        # Same key replays the persisted result without a second provider call.
        replayed = await AnalysisService(
            reopened, FakeAnalysisProvider(synthetic_analysis), reopened_auth
        ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="replay-analysis")
        assert replayed.replayed is True
        _assert_repartitioned(
            replayed,
            {UNFAMILIAR_LIFE, TIME},
            {FREE_WILL, AI_COMPASSION, WAR_STRESS, AI_LENS, CATALOGUE},
        )
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_legacy_single_option_selection_unchanged(
    repository, synthetic_messages, synthetic_analysis, authorization
) -> None:
    """Selecting option 1 keeps the pre-existing boundary and tendrils."""
    bundle, claim, _candidates = await _discover_and_select(
        repository, synthetic_messages, authorization, [1]
    )
    assert claim.selected_candidate_ids == ["cand-1"]
    outcome = await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(bundle.id, actor_user_id="person-2", idempotency_key="single-analysis")
    _assert_repartitioned(
        outcome,
        {FREE_WILL, AI_COMPASSION, WAR_STRESS},
        {UNFAMILIAR_LIFE, TIME, AI_LENS, CATALOGUE},
    )
