from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.application.resurfacing_service import ResurfacingService
from nemoir.application.routing_service import RoutingService
from nemoir.domain.errors import AuthorizationError
from nemoir.domain.states import TendrilState
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.fake import FakeAnalysisProvider


async def _analyse(repository, captured_bundle, synthetic_analysis, authorization):
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="analysis-for-lifecycle",
    )


@pytest.mark.asyncio
async def test_pull_does_not_immediately_repeat_and_resolution_survives_restart(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analyse(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    first = service.pull(actor_user_id="person-2", idempotency_key="pull-1")
    second = service.pull(actor_user_id="person-2", idempotency_key="pull-2")
    assert first.id != second.id
    assert first.status == TendrilState.RESURFACED
    resolved = service.resolve(
        first.id, actor_user_id="person-2", idempotency_key="resolve-1"
    )
    assert resolved.status == TendrilState.RESOLVED

    path = repository.database_path
    repository.close()
    reopened = SQLiteRepository(path)
    try:
        assert reopened.get_tendril(first.id).status == TendrilState.RESOLVED
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_pull_replays_same_tendril_for_a_repeated_key(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analyse(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    first = service.pull(actor_user_id="person-2", idempotency_key="pull-replay-key")
    repeated = service.pull(actor_user_id="person-2", idempotency_key="pull-replay-key")
    assert repeated.id == first.id
    assert repeated.status == TendrilState.RESURFACED
    count = repository._connection.execute(
        "SELECT COUNT(*) AS count FROM lifecycle_events "
        "WHERE entity_id = ? AND idempotency_key = 'pull-replay-key'",
        (first.id,),
    ).fetchone()["count"]
    assert count == 1
    # A different actor replaying the same key is still refused.
    with pytest.raises(AuthorizationError):
        service.pull(actor_user_id="intruder", idempotency_key="pull-replay-key")


@pytest.mark.asyncio
async def test_snoozed_tendril_is_only_due_at_or_after_time(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analyse(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    future = datetime.now(timezone.utc) + timedelta(days=1)
    snoozed = service.snooze(
        tendril.id,
        actor_user_id="person-2",
        idempotency_key="snooze-1",
        until=future,
    )
    assert snoozed.status == TendrilState.SNOOZED
    pulled = service.pull(actor_user_id="person-2", idempotency_key="pull-other")
    assert pulled.id != tendril.id


@pytest.mark.asyncio
async def test_routing_is_idempotent(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analyse(repository, captured_bundle, synthetic_analysis, authorization)

    class FakePublisher:
        platform = "discord"

        def __init__(self):
            self.calls = 0

        async def publish(self, tendril, external_destination_id):
            self.calls += 1
            return "posted-message-1"

    publisher = FakePublisher()
    service = RoutingService(repository, publisher, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    routed = await service.route(
        tendril.id,
        external_destination_id="channel-42",
        actor_user_id="person-2",
        idempotency_key="route-1",
    )
    repeated = await service.route(
        tendril.id,
        external_destination_id="channel-42",
        actor_user_id="person-2",
        idempotency_key="route-1",
    )
    assert routed.status == TendrilState.ROUTED
    assert repeated.routed_external_id == "channel-42"
    assert publisher.calls == 1


@pytest.mark.asyncio
async def test_merge_preserves_target_and_closes_source(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analyse(repository, captured_bundle, synthetic_analysis, authorization)
    items = repository.list_tendrils(bundle_id=captured_bundle.id)
    merged = ResurfacingService(repository, authorization).merge(
        items[0].id,
        items[1].id,
        actor_user_id="person-2",
        idempotency_key="merge-1",
    )
    assert merged.status == TendrilState.MERGED
    assert repository.get_tendril(items[1].id).status == TendrilState.OPEN
