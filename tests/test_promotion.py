"""Application tests for the explicit actionable-promotion surface."""

from __future__ import annotations

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.application.resurfacing_service import ResurfacingService
from nemoir.domain.errors import AuthorizationError, ConflictError, InvalidTransitionError
from nemoir.domain.states import Actionability, TendrilState
from nemoir.providers.fake import FakeAnalysisProvider


async def _analysed(repository, captured_bundle, synthetic_analysis, authorization):
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="promotion-analyse",
    )


def _candidate(repository, captured_bundle):
    return next(
        item
        for item in repository.list_tendrils(bundle_id=captured_bundle.id)
        if item.actionability == Actionability.CANDIDATE
    )


@pytest.mark.asyncio
async def test_promotion_marks_candidate_and_preserves_evidence_and_history(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = _candidate(repository, captured_bundle)
    promoted = service.promote_actionable(
        tendril.id,
        actor_user_id="person-2",
        idempotency_key="promote-1",
    )
    assert promoted.status == TendrilState.PROMOTED_ACTIONABLE
    # Evidence, provenance, and inference labels are untouched by promotion.
    assert promoted.evidence == tendril.evidence
    assert promoted.bundle_id == tendril.bundle_id
    assert promoted.why_open == tendril.why_open
    assert promoted.actionability == Actionability.CANDIDATE
    # The append-only lifecycle history records the explicit promotion.
    events = repository._connection.execute(
        "SELECT * FROM lifecycle_events WHERE entity_type = 'tendril' AND entity_id = ? "
        "ORDER BY timestamp",
        (tendril.id,),
    ).fetchall()
    assert events[-1]["new_state"] == TendrilState.PROMOTED_ACTIONABLE.value
    assert events[-1]["actor_user_id"] == "person-2"
    assert events[-1]["idempotency_key"] == "promote-1"
    assert '"promoted_from":"OPEN"' in events[-1]["metadata_json"]
    assert '"inferred_actionability":"CANDIDATE"' in events[-1]["metadata_json"]


@pytest.mark.asyncio
async def test_promotion_allows_human_override_of_not_actionable_inference(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    not_actionable = next(
        item
        for item in repository.list_tendrils(bundle_id=captured_bundle.id)
        if item.actionability != Actionability.CANDIDATE
    )
    promoted = service.promote_actionable(
        not_actionable.id,
        actor_user_id="person-2",
        idempotency_key="promote-override",
    )
    assert promoted.status == TendrilState.PROMOTED_ACTIONABLE
    # The AI inference is not source truth: promotion overrides it without
    # rewriting the recorded inference (provenance is preserved).
    assert promoted.actionability == not_actionable.actionability
    assert promoted.actionability == Actionability.NOT_ACTIONABLE
    event = repository._connection.execute(
        "SELECT * FROM lifecycle_events WHERE entity_id = ? "
        "AND new_state = 'PROMOTED_ACTIONABLE' ORDER BY timestamp DESC LIMIT 1",
        (not_actionable.id,),
    ).fetchone()
    assert '"inferred_actionability":"NOT_ACTIONABLE"' in event["metadata_json"]


@pytest.mark.asyncio
async def test_promotion_refuses_tendrils_in_an_ineligible_lifecycle_state(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = _candidate(repository, captured_bundle)
    service.resolve(tendril.id, actor_user_id="person-2", idempotency_key="promote-resolve")
    with pytest.raises(ConflictError):
        service.promote_actionable(
            tendril.id,
            actor_user_id="person-2",
            idempotency_key="promote-resolved",
        )


@pytest.mark.asyncio
async def test_promotion_refuses_non_participant_and_allows_admin(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = _candidate(repository, captured_bundle)
    with pytest.raises(AuthorizationError):
        service.promote_actionable(
            tendril.id,
            actor_user_id="intruder",
            idempotency_key="promote-intruder",
        )
    promoted = service.promote_actionable(
        tendril.id,
        actor_user_id="admin-1",
        idempotency_key="promote-admin",
    )
    assert promoted.status == TendrilState.PROMOTED_ACTIONABLE


@pytest.mark.asyncio
async def test_promotion_is_idempotent_for_a_repeated_key(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = _candidate(repository, captured_bundle)
    first = service.promote_actionable(
        tendril.id,
        actor_user_id="person-2",
        idempotency_key="promote-repeat",
    )
    repeated = service.promote_actionable(
        tendril.id,
        actor_user_id="person-2",
        idempotency_key="promote-repeat",
    )
    assert repeated.id == first.id
    assert repeated.status == TendrilState.PROMOTED_ACTIONABLE
    count = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM lifecycle_events "
        "WHERE entity_id = ? AND idempotency_key = 'promote-repeat'",
        (tendril.id,),
    ).fetchone()["count"]
    assert count == 1


@pytest.mark.asyncio
async def test_promotion_with_a_new_key_after_promotion_is_rejected_by_the_state_machine(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = _candidate(repository, captured_bundle)
    service.promote_actionable(
        tendril.id,
        actor_user_id="person-2",
        idempotency_key="promote-once",
    )
    with pytest.raises(InvalidTransitionError):
        service.promote_actionable(
            tendril.id,
            actor_user_id="person-2",
            idempotency_key="promote-again-new-key",
        )


@pytest.mark.asyncio
async def test_promotion_records_resurfaced_origin_in_lifecycle_history(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    service.pull(actor_user_id="person-2", idempotency_key="promote-pull")
    # pull resurfaced one tendril; every eligible tendril may then be
    # promoted, and each promotion records where it came from.
    items = repository.list_tendrils(bundle_id=captured_bundle.id)
    promoted = [
        service.promote_actionable(
            item.id,
            actor_user_id="person-2",
            idempotency_key=f"promote-{item.id}",
        )
        for item in items
    ]
    assert all(item.status == TendrilState.PROMOTED_ACTIONABLE for item in promoted)
    assert any(
        '"promoted_from":"RESURFACED"' in event["metadata_json"]
        for item in items
        for event in repository._connection.execute(
            "SELECT * FROM lifecycle_events WHERE entity_id = ? "
            "AND new_state = 'PROMOTED_ACTIONABLE'",
            (item.id,),
        ).fetchall()
    )
