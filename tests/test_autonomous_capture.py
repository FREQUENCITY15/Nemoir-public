"""Recipient-free autonomous capture: ownership, concurrency, idempotency, migration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from nemoir.application.capture_service import CaptureService
from nemoir.domain.errors import AuthorizationError, ConflictError
from nemoir.domain.models import SourceMessage
from nemoir.domain.states import BundleState
from nemoir.persistence.sqlite_repository import SQLiteRepository


def _message(external_id: str, author: str, content: str, ordinal: int = 0) -> SourceMessage:
    return SourceMessage(
        external_message_id=external_id,
        author_user_id=author,
        author_display_name=author,
        channel_id="intake-1",
        content=content,
        source_url=f"https://discord.invalid/{external_id}",
        timestamp=datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc),
        ordinal=ordinal,
    )


def test_recipient_free_tend_marks_bundle_autonomous(repository) -> None:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
    )
    persisted = repository.get_bundle(bundle.id)
    assert persisted.autonomous_mode is True
    # Stored recipient is the submitter for schema compatibility, but the
    # bundle is explicitly autonomous: no recipient action is required.
    assert persisted.recipient_user_id == "person-1"
    assert persisted.submitter_user_id == "person-1"


def test_manual_tend_keeps_recipient_behavior(repository) -> None:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="person-2",
    )
    persisted = repository.get_bundle(bundle.id)
    assert persisted.autonomous_mode is False
    assert persisted.recipient_user_id == "person-2"


def test_two_users_capture_independently_in_same_channel(repository) -> None:
    capture = CaptureService(repository)
    first = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
    )
    second = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-2",
        recipient_user_id="unused",
        autonomous=True,
    )
    assert first.id != second.id
    assert repository.find_open_capture("person-1", "intake-1").id == first.id
    assert repository.find_open_capture("person-2", "intake-1").id == second.id

    # Each user's messages enter only their own capture.
    for external_id, author, content, ordinal in [
        ("m-1", "person-1", "thoughts from person one", 0),
        ("m-2", "person-2", "thoughts from person two", 0),
    ]:
        message = _message(external_id, author, content, ordinal)
        capture.capture_message(
            first.id if author == "person-1" else second.id,
            actor_user_id=author,
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id=message.channel_id,
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    first_messages = repository.get_bundle(first.id).source_messages
    second_messages = repository.get_bundle(second.id).source_messages
    assert [item.external_message_id for item in first_messages] == ["m-1"]
    assert [item.external_message_id for item in second_messages] == ["m-2"]


def test_one_users_active_capture_never_absorbs_another_users_message(repository) -> None:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
    )
    with pytest.raises(AuthorizationError):
        capture.capture_message(
            bundle.id,
            actor_user_id="person-2",
            external_message_id="m-other",
            author_display_name="person-2",
            channel_id="intake-1",
            content="someone else's thought",
            source_url="https://discord.invalid/m-other",
            timestamp=datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        )


def test_same_user_cannot_open_two_captures_in_one_channel(repository) -> None:
    capture = CaptureService(repository)
    capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
    )
    with pytest.raises(ConflictError):
        capture.start_capture(
            guild_id="guild-1",
            intake_channel_id="intake-1",
            submitter_user_id="person-1",
            recipient_user_id="unused",
            autonomous=True,
        )


def test_duplicate_tend_delivery_replays_same_bundle(repository) -> None:
    capture = CaptureService(repository)
    first = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
        idempotency_key="discord-tend:interaction-1",
    )
    replayed = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
        idempotency_key="discord-tend:interaction-1",
    )
    assert replayed.id == first.id
    message = _message("m-1", "person-1", "a captured thought", 0)
    capture.capture_message(
        first.id,
        actor_user_id="person-1",
        external_message_id=message.external_message_id,
        author_display_name=message.author_display_name,
        channel_id=message.channel_id,
        content=message.content,
        source_url=message.source_url,
        timestamp=message.timestamp,
    )
    # A genuinely new interaction opens a new capture once the first is sealed.
    capture.seal(first.id, actor_user_id="person-1", idempotency_key="seal-1")
    second = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
        idempotency_key="discord-tend:interaction-2",
    )
    assert second.id != first.id


def test_duplicate_seal_delivery_is_idempotent(repository, synthetic_messages) -> None:
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
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
    sealed = capture.seal(bundle.id, actor_user_id="person-1", idempotency_key="seal-1")
    assert sealed.status == BundleState.SEALED
    replayed = capture.seal(bundle.id, actor_user_id="person-1", idempotency_key="seal-2")
    assert replayed.status == BundleState.SEALED
    assert len(repository.get_bundle(bundle.id).source_units) == len(
        sealed.source_units
    )
    # Only one sealed lifecycle event was recorded for the first key.
    events = repository._connection.execute(
        "SELECT idempotency_key FROM lifecycle_events WHERE entity_id = ? AND entity_type = 'bundle'",
        (bundle.id,),
    ).fetchall()
    assert [row["idempotency_key"] for row in events] == ["seal-1:sealed"]


def test_migration_adds_autonomous_column_and_preserves_legacy_bundles(
    tmp_path,
) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE bundles (
            id TEXT PRIMARY KEY,
            guild_id TEXT NOT NULL,
            intake_channel_id TEXT NOT NULL,
            submitter_user_id TEXT NOT NULL,
            recipient_user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sealed_at TEXT,
            claim_id TEXT
        );
        INSERT INTO bundles(id, guild_id, intake_channel_id, submitter_user_id,
            recipient_user_id, status, created_at)
        VALUES ('legacy-bundle', 'guild-1', 'intake-1', 'person-1', 'person-2',
            'CAPTURING', '2026-08-27T08:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    repository = SQLiteRepository(db_path)
    try:
        columns = {
            row["name"]
            for row in repository._connection.execute("PRAGMA table_info(bundles)").fetchall()
        }
        assert "autonomous_mode" in columns
        legacy = repository.get_bundle("legacy-bundle")
        assert legacy.autonomous_mode is False
        version = repository._connection.execute(
            "SELECT version FROM schema_meta"
        ).fetchone()["version"]
        assert version == 4
    finally:
        repository.close()
