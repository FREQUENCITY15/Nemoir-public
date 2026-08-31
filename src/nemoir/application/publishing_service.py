"""Bundle-level tendril publishing with restart-safe external reconciliation.

Publishing a ``REVIEW_READY`` bundle turns each eligible, unrouted tendril
into its own Discord text channel under the configured anemone category. The
external side effects (channel creation and the single tracked post) reuse the
existing ``external_operations`` machinery: a durable per-tendril operation is
reserved BEFORE channel creation, with a stable idempotency key derived from
the bundle and tendril — never from the latest interaction — so a duplicate
delivery, a restart, or a retry can never create a duplicate channel or a
duplicate post.

Claimed material is never published: publishing only visits tendril rows, so
the recipient's claim (which is not a tendril) is inherently excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nemoir.adapters.channel_naming import derive_channel_slug, resolve_channel_slug
from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.routing_service import RoutePublisher
from nemoir.domain.errors import ConflictError
from nemoir.domain.models import ExternalOperation, Tendril
from nemoir.domain.states import BundleState, ExternalOperationStatus, TendrilState
from nemoir.persistence.sqlite_repository import SQLiteRepository


class ChannelFactory(Protocol):
    """Discord-side channel naming/creation seam, implemented by the adapter."""

    platform: str

    async def list_channel_names(self, category_id: str) -> set[str]:
        """Return the current text-channel names under the category."""

    async def create_channel(self, slug: str, category_id: str) -> str:
        """Create one text channel and return its external channel ID."""


_ELIGIBLE_STATES: frozenset[TendrilState] = frozenset(
    {TendrilState.OPEN, TendrilState.RESURFACED}
)

_SKIP_REASONS: dict[TendrilState, str] = {
    TendrilState.SNOOZED: "snoozed",
    TendrilState.PROMOTED_ACTIONABLE: "promoted_actionable",
    TendrilState.RESOLVED: "resolved",
    TendrilState.MERGED: "merged",
    TendrilState.RELEASED: "released",
}

_UNRESOLVED_STATUSES: frozenset[ExternalOperationStatus] = frozenset(
    {
        ExternalOperationStatus.PENDING,
        ExternalOperationStatus.EXTERNAL_SUCCEEDED,
        ExternalOperationStatus.NEEDS_RECONCILIATION,
    }
)


def stable_publish_key(bundle_id: str, tendril_id: str) -> str:
    """Stable idempotency key derived from the bundle and tendril.

    It deliberately ignores the Discord interaction ID, so a duplicate
    delivery or a human retry resolves to the same durable reservation.
    """
    return f"publish:{bundle_id}:{tendril_id}"


@dataclass(frozen=True)
class PublishPreviewItem:
    tendril_id: str
    title: str
    type: str
    evidence_count: int
    status: str  # will_publish | already_published | skipped | reconciliation_required
    channel_slug: str | None = None
    channel_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PublishPreview:
    bundle_id: str
    items: list[PublishPreviewItem]

    def of_status(self, status: str) -> list[PublishPreviewItem]:
        return [item for item in self.items if item.status == status]


@dataclass(frozen=True)
class PublishItemResult:
    tendril_id: str
    title: str
    status: str  # published | already_published | skipped | reconciliation_required | failed
    channel_slug: str | None = None
    channel_id: str | None = None
    message_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PublishOutcome:
    bundle_id: str
    items: list[PublishItemResult]

    def of_status(self, status: str) -> list[PublishItemResult]:
        return [item for item in self.items if item.status == status]


class PublishingService:
    def __init__(
        self,
        repository: SQLiteRepository,
        authorization: AuthorizationPolicy,
        publisher: RoutePublisher,
        channel_factory: ChannelFactory,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.publisher = publisher
        self.channel_factory = channel_factory

    def preview(self, bundle_id: str, *, actor_user_id: str) -> PublishPreview:
        """Read-only preview of the channel-split execution plan.

        Requires ``REVIEW_READY`` (the same gate as execution) and classifies
        every tendril exactly as a confirmed run would, without any database
        write or Discord call. Proposed channel names are base names resolved
        deterministically against duplicates *within the bundle*; a confirmed
        run may still apply a collision suffix once it inspects the real
        category. Terminal and already-routed tendrils are never shown as
        channels that will definitely be created.
        """
        bundle = self.authorization.require_bundle_participant(bundle_id, actor_user_id)
        if bundle.status != BundleState.REVIEW_READY:
            raise ConflictError(
                f"Bundle must be REVIEW_READY to preview publishing, not {bundle.status.value}"
            )
        tendrils = self.repository.list_tendrils(bundle_id=bundle_id)
        items: list[PublishPreviewItem] = []
        used_slugs: set[str] = set()
        for tendril in tendrils:
            route = self.repository.get_route_for_tendril(tendril.id)
            if route is not None or tendril.status == TendrilState.ROUTED:
                items.append(
                    PublishPreviewItem(
                        tendril_id=tendril.id,
                        title=tendril.title,
                        type=tendril.type.value,
                        evidence_count=len(tendril.evidence),
                        status="already_published",
                        channel_id=tendril.routed_external_id or route["external_destination_id"],
                    )
                )
                continue
            if tendril.status not in _ELIGIBLE_STATES:
                items.append(
                    PublishPreviewItem(
                        tendril_id=tendril.id,
                        title=tendril.title,
                        type=tendril.type.value,
                        evidence_count=len(tendril.evidence),
                        status="skipped",
                        reason=_SKIP_REASONS.get(tendril.status, tendril.status.value),
                    )
                )
                continue
            operations = self.repository.list_external_operations(tendril_id=tendril.id)
            unresolved = [
                item
                for item in operations
                if item.operation_type == "habitat_create"
                and item.status in _UNRESOLVED_STATUSES
            ]
            if unresolved:
                item = unresolved[0]
                items.append(
                    PublishPreviewItem(
                        tendril_id=tendril.id,
                        title=tendril.title,
                        type=tendril.type.value,
                        evidence_count=len(tendril.evidence),
                        status="reconciliation_required",
                        channel_id=item.external_destination_id,
                        reason=f"unresolved operation {item.id} is {item.status.value}",
                    )
                )
                continue
            base = derive_channel_slug(tendril)
            slug = resolve_channel_slug(base, used_slugs)
            used_slugs.add(slug)
            items.append(
                PublishPreviewItem(
                    tendril_id=tendril.id,
                    title=tendril.title,
                    type=tendril.type.value,
                    evidence_count=len(tendril.evidence),
                    status="will_publish",
                    channel_slug=slug,
                )
            )
        return PublishPreview(bundle_id=bundle_id, items=items)

    async def publish_bundle(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        guild_id: str,
        category_id: str,
    ) -> PublishOutcome:
        bundle = self.authorization.require_bundle_participant(bundle_id, actor_user_id)
        if bundle.status != BundleState.REVIEW_READY:
            raise ConflictError(
                f"Bundle must be REVIEW_READY to publish, not {bundle.status.value}"
            )
        tendrils = self.repository.list_tendrils(bundle_id=bundle_id)

        # One deterministic read of the destination category. When it fails the
        # category is unavailable and no channel can be created: eligible items
        # are reported as a deterministic "failed" (safe to retry, nothing
        # reserved), while already-published and skipped items still report
        # their real classification.
        category_error: str | None = None
        existing_names: set[str] | None = None
        try:
            existing_names = await self.channel_factory.list_channel_names(category_id)
        except Exception as exc:  # deterministic read failure, no side effect
            category_error = type(exc).__name__

        results: list[PublishItemResult] = []
        for tendril in tendrils:
            result = await self._publish_one(
                tendril,
                actor_user_id,
                guild_id,
                category_id,
                existing_names,
                category_error,
            )
            results.append(result)
            if existing_names is not None and result.channel_slug is not None:
                # Remember channels created during this run so later tendrils in
                # the same bundle resolve collisions deterministically even when
                # the Discord category cache has not refreshed yet.
                existing_names.add(result.channel_slug)
        return PublishOutcome(bundle_id=bundle_id, items=results)

    async def _publish_one(
        self,
        tendril: Tendril,
        actor_user_id: str,
        guild_id: str,
        category_id: str,
        existing_names: set[str] | None,
        category_error: str | None,
    ) -> PublishItemResult:
        route = self.repository.get_route_for_tendril(tendril.id)
        if route is not None or tendril.status == TendrilState.ROUTED:
            channel_id = tendril.routed_external_id or route["external_destination_id"]
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "already_published",
                channel_id=channel_id,
            )

        if tendril.status not in _ELIGIBLE_STATES:
            reason = _SKIP_REASONS.get(tendril.status, tendril.status.value)
            return PublishItemResult(tendril.id, tendril.title, "skipped", reason=reason)

        operations = self.repository.list_external_operations(tendril_id=tendril.id)
        publish_ops = [
            item for item in operations if item.operation_type == "habitat_create"
        ]
        # The unique one_unresolved_operation_per_tendril index guarantees at most
        # one non-COMPLETED operation; that one decides whether we block, resume,
        # or (for a confirmed EXTERNAL_SUCCEEDED with both IDs) complete locally
        # without any new external side effect.
        unresolved = next(
            (item for item in publish_ops if item.status != ExternalOperationStatus.COMPLETED),
            None,
        )
        if unresolved is not None:
            return self._resume_operation(unresolved, tendril, actor_user_id)
        if publish_ops:
            # Every prior operation is COMPLETED; only a missing route could remain.
            return self._complete_route_from(publish_ops[0], tendril, actor_user_id)

        if existing_names is None:
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "failed",
                reason=f"destination category unavailable: {category_error}",
            )

        base = derive_channel_slug(tendril)
        slug = resolve_channel_slug(base, existing_names)

        operation, created = self.repository.reserve_external_operation(
            operation_type="habitat_create",
            idempotency_key=stable_publish_key(tendril.bundle_id, tendril.id),
            platform=self.channel_factory.platform,
            tendril_id=tendril.id,
            actor_user_id=actor_user_id,
            metadata={
                "bundle_id": tendril.bundle_id,
                "publish": "true",
                "requested_slug": slug,
                "guild_id": guild_id,
                "category_id": category_id,
                "phase": "reserved",
            },
        )
        if not created:
            # Narrow re-entrancy: a reservation appeared after the scan above.
            return self._resume_operation(operation, tendril, actor_user_id)

        try:
            channel_id = await self.channel_factory.create_channel(slug, category_id)
        except Exception as exc:
            # Ambiguous: the channel may have been created before the failure.
            self.repository.mark_operation_needs_reconciliation(
                operation.id,
                reason=f"channel creation raised {type(exc).__name__}: {exc}",
            )
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "reconciliation_required",
                channel_slug=slug,
                reason=f"channel creation result unknown: {type(exc).__name__}",
            )

        # Retain the channel ID immediately, then register the habitat row.
        self.repository.mark_operation_external_succeeded(
            operation.id, external_destination_id=channel_id
        )
        self.repository.update_operation_metadata(operation.id, phase="channel_created")
        self.repository.register_habitat(
            guild_id=guild_id,
            platform=self.channel_factory.platform,
            external_id=channel_id,
            canonical_slug=slug,
            description=f"Habitat for tendril {tendril.id}",
        )

        try:
            message_id = await self.publisher.publish(tendril, channel_id)
        except Exception as exc:
            # Ambiguous: the channel exists but the post result is unknown.
            self.repository.mark_operation_needs_reconciliation(
                operation.id,
                reason=f"publish raised {type(exc).__name__}: {exc}",
            )
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "reconciliation_required",
                channel_slug=slug,
                channel_id=channel_id,
                reason=f"post result unknown: {type(exc).__name__}",
            )
        if message_id is None:
            self.repository.mark_operation_needs_reconciliation(
                operation.id, reason="publisher returned no external message ID"
            )
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "reconciliation_required",
                channel_slug=slug,
                channel_id=channel_id,
                reason="post result unknown: no message id",
            )

        self.repository.mark_operation_external_succeeded(
            operation.id, external_message_id=message_id
        )
        self.repository.update_operation_metadata(operation.id, phase="message_published")
        self.repository.record_route(
            tendril.id,
            self.channel_factory.platform,
            channel_id,
            message_id,
            actor_user_id,
            operation.idempotency_key,
        )
        self.repository.mark_operation_completed(operation.id)
        return PublishItemResult(
            tendril.id,
            tendril.title,
            "published",
            channel_slug=slug,
            channel_id=channel_id,
            message_id=message_id,
        )

    def _complete_route_from(
        self,
        operation: ExternalOperation,
        tendril: Tendril,
        actor_user_id: str,
    ) -> PublishItemResult:
        """Complete a channel whose create succeeded but whose route was not recorded."""
        if operation.external_destination_id is None or operation.external_message_id is None:
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "reconciliation_required",
                channel_id=operation.external_destination_id,
                reason="completed operation retained no channel/message ID",
            )
        self.repository.record_route(
            tendril.id,
            operation.platform,
            operation.external_destination_id,
            operation.external_message_id,
            actor_user_id,
            operation.idempotency_key,
        )
        return PublishItemResult(
            tendril.id,
            tendril.title,
            "published",
            channel_id=operation.external_destination_id,
            message_id=operation.external_message_id,
        )

    def _resume_operation(
        self,
        operation: ExternalOperation,
        tendril: Tendril,
        actor_user_id: str,
    ) -> PublishItemResult:
        """Continue from an already-reserved publish operation without repeating it.

        - PENDING / NEEDS_RECONCILIATION never repeat the external side effect:
          the item stays reconciliation-required for an operator to resolve.
        - EXTERNAL_SUCCEEDED with both the channel and message IDs retained is a
          confirmed external success whose only missing work is local: record the
          route and complete the operation, with no new channel or post.
        - EXTERNAL_SUCCEEDED with only the channel ID (message result unknown)
          must be reconciled by the operator; it is never re-posted.
        """
        if operation.status in {
            ExternalOperationStatus.PENDING,
            ExternalOperationStatus.NEEDS_RECONCILIATION,
        }:
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "reconciliation_required",
                channel_id=operation.external_destination_id,
                reason=f"operation {operation.id} is {operation.status.value}",
            )
        if operation.status == ExternalOperationStatus.EXTERNAL_SUCCEEDED:
            if (
                operation.external_destination_id is not None
                and operation.external_message_id is not None
            ):
                self.repository.record_route(
                    tendril.id,
                    operation.platform,
                    operation.external_destination_id,
                    operation.external_message_id,
                    actor_user_id,
                    operation.idempotency_key,
                )
                self.repository.mark_operation_completed(operation.id)
                return PublishItemResult(
                    tendril.id,
                    tendril.title,
                    "published",
                    channel_id=operation.external_destination_id,
                    message_id=operation.external_message_id,
                )
            return PublishItemResult(
                tendril.id,
                tendril.title,
                "reconciliation_required",
                channel_id=operation.external_destination_id,
                reason="channel created but post result unknown",
            )
        if operation.status == ExternalOperationStatus.COMPLETED:
            return self._complete_route_from(operation, tendril, actor_user_id)
        return PublishItemResult(
            tendril.id,
            tendril.title,
            "reconciliation_required",
            channel_id=operation.external_destination_id,
            reason=f"operation {operation.id} is {operation.status.value}",
        )
