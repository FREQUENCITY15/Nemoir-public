"""Manual, non-spamming tendril resurfacing and lifecycle decisions."""

from __future__ import annotations

from datetime import datetime, timezone

from nemoir.application.authorization import AuthorizationPolicy
from nemoir.domain.errors import AuthorizationError, ConflictError, NotFoundError
from nemoir.domain.models import Tendril
from nemoir.domain.states import Actionability, TendrilState, TendrilType
from nemoir.persistence.sqlite_repository import SQLiteRepository


class ResurfacingService:
    def __init__(
        self,
        repository: SQLiteRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def pull(
        self,
        *,
        actor_user_id: str,
        idempotency_key: str,
        tendril_type: TendrilType | None = None,
    ) -> Tendril:
        prior = self.repository.find_lifecycle_event("tendril", idempotency_key)
        if prior is not None:
            if prior["new_state"] != TendrilState.RESURFACED.value:
                raise ConflictError(
                    "Idempotency key already represents another tendril transition"
                )
            item = self.repository.get_tendril(prior["entity_id"])
            if not self.authorization.can_act_on_tendril(item.id, actor_user_id):
                raise AuthorizationError(
                    "Only bundle participants or administrators may perform this operation"
                )
            return item
        now = datetime.now(timezone.utc)
        candidates = self.repository.list_tendrils(
            statuses={TendrilState.OPEN, TendrilState.SNOOZED}
        )
        candidates = [
            item
            for item in candidates
            if (item.status == TendrilState.OPEN)
            or (item.snoozed_until is not None and item.snoozed_until <= now)
        ]
        if tendril_type:
            candidates = [item for item in candidates if item.type == tendril_type]
        candidates = [
            item
            for item in candidates
            if self.authorization.can_act_on_tendril(item.id, actor_user_id)
        ]
        if not candidates:
            raise NotFoundError("No eligible open tendril is available")
        selected = candidates[0]
        return self.repository.transition_tendril(
            selected.id,
            TendrilState.RESURFACED,
            actor_user_id,
            idempotency_key,
        )

    def resolve(self, tendril_id: str, *, actor_user_id: str, idempotency_key: str) -> Tendril:
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        return self.repository.transition_tendril(
            tendril_id, TendrilState.RESOLVED, actor_user_id, idempotency_key
        )

    def release(self, tendril_id: str, *, actor_user_id: str, idempotency_key: str) -> Tendril:
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        return self.repository.transition_tendril(
            tendril_id, TendrilState.RELEASED, actor_user_id, idempotency_key
        )

    def snooze(
        self,
        tendril_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
        until: datetime,
    ) -> Tendril:
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        if until.tzinfo is None or until.utcoffset() is None:
            raise ValueError("Snooze time must include a timezone")
        return self.repository.transition_tendril(
            tendril_id,
            TendrilState.SNOOZED,
            actor_user_id,
            idempotency_key,
            snoozed_until=until.astimezone(timezone.utc),
        )

    def reopen(self, tendril_id: str, *, actor_user_id: str, idempotency_key: str) -> Tendril:
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        return self.repository.transition_tendril(
            tendril_id, TendrilState.OPEN, actor_user_id, idempotency_key
        )

    def promote_actionable(
        self,
        tendril_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
    ) -> Tendril:
        """Explicitly promote an eligible tendril to PROMOTED_ACTIONABLE.

        Eligibility is a lifecycle property: the tendril must still be OPEN or
        RESURFACED (the only states the closed state machine allows to reach
        PROMOTED_ACTIONABLE) and the actor must be a bundle participant or
        administrator. Actionability is an AI inference, not source truth, so
        an authorised human may override NOT_ACTIONABLE; the originally
        inferred value is preserved untouched on the tendril and recorded in
        the promotion lifecycle event as provenance.
        """
        self.authorization.require_tendril_participant(tendril_id, actor_user_id)
        tendril = self.repository.get_tendril(tendril_id)
        if tendril.status == TendrilState.PROMOTED_ACTIONABLE:
            # Already promoted: a repeated idempotency key is an idempotent
            # replay handled by the repository; a new key is rejected by the
            # closed state machine there.
            return self.repository.transition_tendril(
                tendril_id,
                TendrilState.PROMOTED_ACTIONABLE,
                actor_user_id,
                idempotency_key,
            )
        if tendril.status not in {TendrilState.OPEN, TendrilState.RESURFACED}:
            raise ConflictError(
                f"Only OPEN or RESURFACED tendrils can be promoted, not {tendril.status.value}"
            )
        return self.repository.transition_tendril(
            tendril_id,
            TendrilState.PROMOTED_ACTIONABLE,
            actor_user_id,
            idempotency_key,
            metadata={
                "promoted_from": tendril.status.value,
                "inferred_actionability": tendril.actionability.value,
            },
        )

    def merge(
        self,
        source_tendril_id: str,
        target_tendril_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
    ) -> Tendril:
        if source_tendril_id == target_tendril_id:
            raise ValueError("A tendril cannot be merged into itself")
        self.authorization.require_tendril_participant(source_tendril_id, actor_user_id)
        self.repository.get_tendril(target_tendril_id)
        return self.repository.transition_tendril(
            source_tendril_id,
            TendrilState.MERGED,
            actor_user_id,
            idempotency_key,
            metadata={"merged_into": target_tendril_id},
        )
