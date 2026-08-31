"""Provider-neutral routing with restart-safe external-side-effect reconciliation.

The external side effect (a Discord post, channel creation) is reserved as a
local PENDING operation BEFORE it happens. The returned external IDs are
retained immediately. An ambiguous failure is never automatically repeated:
the operation is left in PENDING or NEEDS_RECONCILIATION and an operator
reconciles it explicitly.
"""

from __future__ import annotations

from typing import Protocol

from nemoir.application.authorization import AuthorizationPolicy
from nemoir.domain.errors import ConflictError
from nemoir.domain.models import ExternalOperation, Tendril
from nemoir.domain.states import ExternalOperationStatus
from nemoir.persistence.sqlite_repository import SQLiteRepository


class RoutePublisher(Protocol):
    platform: str

    async def publish(self, tendril: Tendril, external_destination_id: str) -> str | None:
        """Publish evidence-backed tendril content and return its external message ID."""


class RoutingService:
    def __init__(
        self,
        repository: SQLiteRepository,
        publisher: RoutePublisher,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.publisher = publisher
        self.authorization = authorization

    def _continue_or_raise(
        self,
        operation: ExternalOperation,
        tendril_id: str,
        external_destination_id: str,
        idempotency_key: str,
    ) -> Tendril | None:
        """Decide how to continue from an existing reservation.

        Returns the tendril when the operation is already COMPLETED, or
        performs the local completion for a confirmed EXTERNAL_SUCCEEDED
        operation. Raises for PENDING / NEEDS_RECONCILIATION (never repeat).
        """
        if operation.idempotency_key != idempotency_key:
            raise ConflictError(
                f"Tendril already has an unresolved external operation "
                f"({operation.id}, {operation.status}); reconcile it before retrying"
            )
        if operation.external_destination_id not in (None, external_destination_id):
            raise ConflictError("Existing external operation targets a different destination")
        if operation.status == ExternalOperationStatus.COMPLETED:
            return self.repository.get_tendril(tendril_id)
        if operation.status == ExternalOperationStatus.EXTERNAL_SUCCEEDED:
            # The external side effect is confirmed (its ID is retained);
            # completing the local record is not repeating it.
            message_id = operation.external_message_id
            if message_id is None:
                raise ConflictError(
                    "External operation succeeded but its message ID was never retained; "
                    "reconcile manually before continuing"
                )
            self.repository.record_route(
                tendril_id,
                operation.platform,
                external_destination_id,
                message_id,
                operation.actor_user_id,
                operation.idempotency_key,
            )
            self.repository.mark_operation_completed(operation.id)
            return self.repository.get_tendril(tendril_id)
        raise ConflictError(
            f"External operation is {operation.status.value}; an ambiguous external "
            "side effect is never repeated automatically. Reconcile it first."
        )

    async def route(
        self,
        tendril_id: str,
        *,
        external_destination_id: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> Tendril:
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        tendril = self.repository.get_tendril(tendril_id)
        if (
            tendril.routed_platform == self.publisher.platform
            and tendril.routed_external_id == external_destination_id
        ):
            return tendril
        existing_route = self.repository.get_route_for_tendril(tendril_id)
        if existing_route is not None:
            raise ConflictError("Tendril is already routed elsewhere; it cannot be routed again")

        operation, created = self.repository.reserve_external_operation(
            operation_type="route",
            idempotency_key=idempotency_key,
            platform=self.publisher.platform,
            tendril_id=tendril_id,
            actor_user_id=actor_user_id,
            external_destination_id=external_destination_id,
        )
        if not created:
            completed = self._continue_or_raise(
                operation, tendril_id, external_destination_id, idempotency_key
            )
            if completed is not None:
                return completed
            raise ConflictError("External operation cannot continue")

        try:
            external_message_id = await self.publisher.publish(tendril, external_destination_id)
        except Exception as exc:
            self.repository.mark_operation_needs_reconciliation(
                operation.id, reason=f"publish raised {type(exc).__name__}: {exc}"
            )
            raise
        if external_message_id is None:
            self.repository.mark_operation_needs_reconciliation(
                operation.id, reason="publisher returned no external message ID"
            )
            raise ConflictError("Publisher did not return an external message ID")
        self.repository.mark_operation_external_succeeded(
            operation.id, external_message_id=external_message_id
        )
        self.repository.record_route(
            tendril_id,
            self.publisher.platform,
            external_destination_id,
            external_message_id,
            actor_user_id,
            idempotency_key,
        )
        self.repository.mark_operation_completed(operation.id)
        return self.repository.get_tendril(tendril_id)

    def reconcile_route(
        self,
        tendril_id: str,
        *,
        external_destination_id: str,
        external_message_id: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> Tendril:
        """Operator-confirmed completion after an ambiguous external result.

        The operator attests that the post exists in the destination and
        supplies its external message ID; the local route is recorded without
        any new external side effect.
        """
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        operation = self.repository.get_external_operation_by_key(idempotency_key)
        if operation.operation_type != "route":
            raise ConflictError("The operation is not a route operation")
        if operation.external_destination_id not in (None, external_destination_id):
            raise ConflictError("Operation targets a different destination")
        self.repository.record_route(
            tendril_id,
            operation.platform,
            external_destination_id,
            external_message_id,
            actor_user_id,
            operation.idempotency_key,
        )
        self.repository.reconcile_external_operation(
            operation.id, external_message_id=external_message_id
        )
        return self.repository.get_tendril(tendril_id)

    def list_unresolved_operations(self) -> list[ExternalOperation]:
        return self.repository.list_external_operations(
            statuses={
                ExternalOperationStatus.PENDING,
                ExternalOperationStatus.EXTERNAL_SUCCEEDED,
                ExternalOperationStatus.NEEDS_RECONCILIATION,
            }
        )
