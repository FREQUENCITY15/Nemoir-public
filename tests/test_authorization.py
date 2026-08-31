"""Application-owned participant/admin authorization across the service layer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.application.resurfacing_service import ResurfacingService
from nemoir.application.routing_service import RoutingService
from nemoir.domain.errors import AuthorizationError, NotFoundError
from nemoir.domain.states import TendrilState
from nemoir.providers.fake import FakeAnalysisProvider


async def _analysed(repository, captured_bundle, synthetic_analysis, authorization) -> None:
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="authz-analyse",
    )


@pytest.mark.asyncio
async def test_analysis_refuses_non_participant(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = AnalysisService(repository, FakeAnalysisProvider(synthetic_analysis), authorization)
    with pytest.raises(AuthorizationError):
        await service.analyse(
            captured_bundle.id,
            actor_user_id="intruder",
            idempotency_key="authz-analysis",
        )


@pytest.mark.asyncio
async def test_analysis_refuses_submitter_who_is_not_recipient(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = AnalysisService(repository, FakeAnalysisProvider(synthetic_analysis), authorization)
    with pytest.raises(AuthorizationError):
        await service.analyse(
            captured_bundle.id,
            actor_user_id="person-1",
            idempotency_key="authz-submitter-analysis",
        )


@pytest.mark.asyncio
async def test_analysis_allows_admin_override(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    service = AnalysisService(repository, FakeAnalysisProvider(synthetic_analysis), authorization)
    outcome = await service.analyse(
        captured_bundle.id,
        actor_user_id="admin-1",
        idempotency_key="authz-admin-analysis",
    )
    assert outcome.review is not None


@pytest.mark.asyncio
async def test_lifecycle_actions_refuse_non_participant(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    items = repository.list_tendrils(bundle_id=captured_bundle.id)
    with pytest.raises(AuthorizationError):
        service.resolve(items[0].id, actor_user_id="intruder", idempotency_key="authz-resolve")
    with pytest.raises(AuthorizationError):
        service.snooze(
            items[0].id,
            actor_user_id="intruder",
            idempotency_key="authz-snooze",
            until=datetime.now(timezone.utc),
        )
    with pytest.raises(AuthorizationError):
        service.merge(
            items[0].id,
            items[1].id,
            actor_user_id="intruder",
            idempotency_key="authz-merge",
        )


@pytest.mark.asyncio
async def test_pull_only_offers_tendrils_the_actor_may_act_on(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    with pytest.raises(NotFoundError):
        service.pull(actor_user_id="intruder", idempotency_key="authz-pull")
    pulled = service.pull(actor_user_id="person-2", idempotency_key="authz-pull-ok")
    assert pulled.status == TendrilState.RESURFACED


@pytest.mark.asyncio
async def test_admin_can_perform_lifecycle_actions(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    service = ResurfacingService(repository, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    resolved = service.resolve(tendril.id, actor_user_id="admin-1", idempotency_key="authz-admin-resolve")
    assert resolved.status == TendrilState.RESOLVED


@pytest.mark.asyncio
async def test_routing_refuses_non_participant(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)

    class FakePublisher:
        platform = "discord"

        async def publish(self, tendril, external_destination_id):
            return "m-1"

    service = RoutingService(repository, FakePublisher(), authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    with pytest.raises(AuthorizationError):
        await service.route(
            tendril.id,
            external_destination_id="ch-1",
            actor_user_id="intruder",
            idempotency_key="authz-route",
        )
    with pytest.raises(AuthorizationError):
        service.reconcile_route(
            tendril.id,
            external_destination_id="ch-1",
            external_message_id="observed-1",
            actor_user_id="intruder",
            idempotency_key="authz-reconcile",
        )
