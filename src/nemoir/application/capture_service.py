"""Capture, seal, claim, and cancellation workflow."""

from __future__ import annotations

from datetime import datetime

from nemoir.domain.errors import AuthorizationError, ConflictError
from nemoir.domain.models import Claim, ConversationBundle, SourceMessage, new_id
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.states import BundleState
from nemoir.persistence.sqlite_repository import SQLiteRepository


class CaptureService:
    def __init__(self, repository: SQLiteRepository, admin_user_ids: set[str] | None = None) -> None:
        self.repository = repository
        self.admin_user_ids = admin_user_ids or set()

    def start_capture(
        self,
        *,
        guild_id: str,
        intake_channel_id: str,
        submitter_user_id: str,
        recipient_user_id: str,
        autonomous: bool = False,
        idempotency_key: str | None = None,
    ) -> ConversationBundle:
        """Open one capture session (or replay it for a repeated ``/tend`` key).

        ``autonomous=True`` is the recipient-free default product mode: the
        invoking user owns the capture (``recipient_user_id`` is stored as the
        submitter for schema compatibility, but the bundle is explicitly marked
        autonomous and no recipient action is ever required).
        """
        bundle = ConversationBundle(
            id=new_id("bundle"),
            guild_id=guild_id,
            intake_channel_id=intake_channel_id,
            submitter_user_id=submitter_user_id,
            recipient_user_id=submitter_user_id if autonomous else recipient_user_id,
            autonomous_mode=autonomous,
        )
        return self.repository.create_bundle(bundle, idempotency_key=idempotency_key)

    def capture_message(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        external_message_id: str,
        author_display_name: str,
        channel_id: str,
        content: str,
        source_url: str,
        timestamp: datetime,
    ) -> SourceMessage:
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.submitter_user_id:
            raise AuthorizationError("Only the capture owner can add source messages")
        if channel_id != bundle.intake_channel_id:
            raise AuthorizationError("Source message is outside the configured intake channel")
        ordinal = len(bundle.source_messages)
        message = SourceMessage(
            external_message_id=external_message_id,
            author_user_id=actor_user_id,
            author_display_name=author_display_name,
            channel_id=channel_id,
            content=content,
            source_url=source_url,
            timestamp=timestamp,
            ordinal=ordinal,
        )
        return self.repository.add_source_message(bundle_id, message)

    def seal(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
    ) -> ConversationBundle:
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.submitter_user_id and actor_user_id not in self.admin_user_ids:
            raise AuthorizationError("Only the capture owner or an administrator can seal this bundle")
        if bundle.status in {
            BundleState.SEALED,
            BundleState.CLAIM_OPTIONS_READY,
            BundleState.CLAIM_OPTIONS_FAILED,
            BundleState.AWAITING_CLAIM,
            # Autonomous bundles: a repeated /seal delivery (or a direct
            # service-level replay) must acknowledge the already-sealed bundle
            # rather than error from the sorting/publishing states.
            BundleState.AUTONOMOUS_SORTING,
            BundleState.AUTONOMOUS_FAILED,
        }:
            return bundle
        if bundle.status != BundleState.CAPTURING:
            raise ConflictError(f"Bundle cannot be sealed from {bundle.status}")
        if not bundle.source_messages or not any(item.content.strip() for item in bundle.source_messages):
            raise ConflictError("Cannot seal an empty bundle")

        units = segment_messages(bundle.source_messages)
        if not units:
            raise ConflictError("Bundle contains no meaningful source units")
        self.repository.store_source_units(bundle_id, units)
        return self.repository.transition_bundle(
            bundle_id,
            BundleState.SEALED,
            actor_user_id,
            f"{idempotency_key}:sealed",
            {"message_count": len(bundle.source_messages), "unit_count": len(units)},
        )

    def claim(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        raw_topic: str,
    ) -> Claim:
        """Record a custom-topic claim (the option-selection escape hatch).

        Option-based selection lives in ``ClaimOptionsService.select``; this
        method remains the direct custom-topic path used by offline fixtures.
        """
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.recipient_user_id and actor_user_id not in self.admin_user_ids:
            raise AuthorizationError("Only the designated recipient or an administrator can claim")
        if bundle.status not in {
            BundleState.SEALED,
            BundleState.CLAIM_OPTIONS_READY,
            BundleState.CLAIM_OPTIONS_FAILED,
            BundleState.AWAITING_CLAIM,
        }:
            raise ConflictError(f"Bundle cannot be claimed from {bundle.status}")
        if bundle.claim_id is not None:
            raise ConflictError("Bundle already has a claim")
        if not raw_topic.strip():
            raise ConflictError("Claim topic cannot be empty")
        claim = Claim(
            id=new_id("claim"),
            bundle_id=bundle_id,
            raw_topic=raw_topic.strip(),
            claimant_user_id=actor_user_id,
        )
        return self.repository.create_claim(claim)

    def cancel(
        self,
        bundle_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
    ) -> ConversationBundle:
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.submitter_user_id and actor_user_id not in self.admin_user_ids:
            raise AuthorizationError("Only the owner or administrator can cancel this bundle")
        if bundle.status == BundleState.CANCELLED:
            return bundle
        return self.repository.transition_bundle(
            bundle_id,
            BundleState.CANCELLED,
            actor_user_id,
            idempotency_key,
        )
