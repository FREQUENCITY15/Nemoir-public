"""Restart-safe external-operation reconciliation for routing side effects."""

from __future__ import annotations

import pytest

from nemoir.application.analysis_service import AnalysisService
from nemoir.application.routing_service import RoutingService
from nemoir.domain.errors import ConflictError
from nemoir.domain.states import ExternalOperationStatus, TendrilState
from nemoir.providers.fake import FakeAnalysisProvider


class FakePublisher:
    platform = "discord"

    def __init__(self, *, fail: bool = False, message_id: str = "posted-message-1") -> None:
        self.calls = 0
        self.fail = fail
        self.message_id = message_id

    async def publish(self, tendril, external_destination_id) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("Synthetic Discord outage")
        return self.message_id


async def _analysed(repository, captured_bundle, synthetic_analysis, authorization) -> None:
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="ops-analyse",
    )


@pytest.mark.asyncio
async def test_route_reserves_before_publish_and_completes_idempotently(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    publisher = FakePublisher()
    service = RoutingService(repository, publisher, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    routed = await service.route(
        tendril.id,
        external_destination_id="ch-9",
        actor_user_id="person-2",
        idempotency_key="route-op-1",
    )
    assert routed.status == TendrilState.ROUTED
    assert publisher.calls == 1
    operations = repository.list_external_operations()
    assert len(operations) == 1
    assert operations[0].operation_type == "route"
    assert operations[0].status == ExternalOperationStatus.COMPLETED
    assert operations[0].external_message_id == "posted-message-1"

    repeated = await service.route(
        tendril.id,
        external_destination_id="ch-9",
        actor_user_id="person-2",
        idempotency_key="route-op-1",
    )
    assert repeated.routed_external_id == "ch-9"
    assert publisher.calls == 1


@pytest.mark.asyncio
async def test_ambiguous_publish_failure_is_never_automatically_repeated(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    publisher = FakePublisher(fail=True)
    service = RoutingService(repository, publisher, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    with pytest.raises(RuntimeError):
        await service.route(
            tendril.id,
            external_destination_id="ch-9",
            actor_user_id="person-2",
            idempotency_key="route-op-2",
        )
    operations = repository.list_external_operations()
    assert len(operations) == 1
    assert operations[0].status == ExternalOperationStatus.NEEDS_RECONCILIATION

    # Same-key retry: refused, no second publish.
    with pytest.raises(ConflictError):
        await service.route(
            tendril.id,
            external_destination_id="ch-9",
            actor_user_id="person-2",
            idempotency_key="route-op-2",
        )
    # Human retry with a new key: also refused while the operation is unresolved.
    with pytest.raises(ConflictError):
        await service.route(
            tendril.id,
            external_destination_id="ch-9",
            actor_user_id="person-2",
            idempotency_key="route-op-2b",
        )
    assert publisher.calls == 1

    # Operator confirms the post exists and supplies its ID.
    reconciled = service.reconcile_route(
        tendril.id,
        external_destination_id="ch-9",
        external_message_id="observed-9",
        actor_user_id="person-2",
        idempotency_key="route-op-2",
    )
    assert reconciled.status == TendrilState.ROUTED
    operations = repository.list_external_operations()
    assert operations[0].status == ExternalOperationStatus.COMPLETED
    assert operations[0].external_message_id == "observed-9"
    assert publisher.calls == 1


@pytest.mark.asyncio
async def test_confirmed_external_success_resumes_without_republishing(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    publisher = FakePublisher()
    service = RoutingService(repository, publisher, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    # Simulate a crash after the external post but before the local route record.
    operation, created = repository.reserve_external_operation(
        operation_type="route",
        idempotency_key="route-op-3",
        platform="discord",
        tendril_id=tendril.id,
        actor_user_id="person-2",
        external_destination_id="ch-9",
    )
    assert created
    repository.mark_operation_external_succeeded(
        operation.id, external_message_id="posted-message-1"
    )
    routed = await service.route(
        tendril.id,
        external_destination_id="ch-9",
        actor_user_id="person-2",
        idempotency_key="route-op-3",
    )
    assert routed.status == TendrilState.ROUTED
    assert publisher.calls == 0
    assert (
        repository.get_external_operation_by_key("route-op-3").status
        == ExternalOperationStatus.COMPLETED
    )


@pytest.mark.asyncio
async def test_pending_operation_blocks_retry_until_reconciled(
    repository, captured_bundle, synthetic_analysis, authorization
) -> None:
    await _analysed(repository, captured_bundle, synthetic_analysis, authorization)
    publisher = FakePublisher()
    service = RoutingService(repository, publisher, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    operation, created = repository.reserve_external_operation(
        operation_type="route",
        idempotency_key="route-op-4",
        platform="discord",
        tendril_id=tendril.id,
        actor_user_id="person-2",
        external_destination_id="ch-9",
    )
    assert created
    with pytest.raises(ConflictError):
        await service.route(
            tendril.id,
            external_destination_id="ch-9",
            actor_user_id="person-2",
            idempotency_key="route-op-4",
        )
    with pytest.raises(ConflictError):
        await service.route(
            tendril.id,
            external_destination_id="ch-9",
            actor_user_id="person-2",
            idempotency_key="route-op-4b",
        )
    assert publisher.calls == 0
    reconciled = service.reconcile_route(
        tendril.id,
        external_destination_id="ch-9",
        external_message_id="observed-9",
        actor_user_id="person-2",
        idempotency_key="route-op-4",
    )
    assert reconciled.status == TendrilState.ROUTED
    assert publisher.calls == 0
    assert (
        repository.get_external_operation_by_key("route-op-4").status
        == ExternalOperationStatus.COMPLETED
    )
    assert service.list_unresolved_operations() == []
