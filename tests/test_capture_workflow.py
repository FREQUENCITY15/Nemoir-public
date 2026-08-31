from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nemoir.application.capture_service import CaptureService
from nemoir.domain.errors import AuthorizationError, ConflictError
from nemoir.domain.states import BundleState


def test_capture_seal_and_claim_persist_in_order(repository, synthetic_messages) -> None:
    service = CaptureService(repository)
    bundle = service.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    for message in synthetic_messages:
        service.capture_message(
            bundle.id,
            actor_user_id="person-1",
            external_message_id=message.external_message_id,
            author_display_name="Person 1",
            channel_id="intake-1",
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    sealed = service.seal(
        bundle.id, actor_user_id="person-1", idempotency_key="interaction-seal-1"
    )
    assert sealed.status == BundleState.SEALED
    assert [item.ordinal for item in sealed.source_messages] == [0, 1, 2]
    assert len(sealed.source_units) == 7

    repeated = service.seal(
        bundle.id, actor_user_id="person-1", idempotency_key="interaction-seal-1"
    )
    assert repeated.status == BundleState.SEALED

    claim = service.claim(
        bundle.id,
        actor_user_id="person-2",
        raw_topic=" LLM consciousness and learned compassion ",
    )
    assert claim.raw_topic == "LLM consciousness and learned compassion"
    assert repository.get_bundle(bundle.id).claim_id == claim.id


def test_only_owner_can_capture_and_recipient_can_claim(repository) -> None:
    service = CaptureService(repository)
    bundle = service.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    with pytest.raises(AuthorizationError):
        service.capture_message(
            bundle.id,
            actor_user_id="intruder",
            external_message_id="x",
            author_display_name="Intruder",
            channel_id="intake-1",
            content="Nope",
            source_url="https://discord.invalid/x",
            timestamp=datetime.now(timezone.utc),
        )


def test_empty_bundle_cannot_be_sealed(repository) -> None:
    service = CaptureService(repository)
    bundle = service.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    with pytest.raises(ConflictError):
        service.seal(bundle.id, actor_user_id="person-1", idempotency_key="empty")


def test_one_open_capture_per_owner_and_channel(repository) -> None:
    service = CaptureService(repository)
    service.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    with pytest.raises(ConflictError):
        service.start_capture(
            guild_id="guild-1",
            intake_channel_id="intake-1",
            submitter_user_id="person-1",
            recipient_user_id="person-3",
        )
