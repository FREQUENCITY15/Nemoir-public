from __future__ import annotations

import json
import os
from pathlib import Path
import socket

import pytest

from nemoir.application.authorization import AuthorizationPolicy
from nemoir.application.capture_service import CaptureService
from nemoir.domain.models import AnalysisResult, SourceMessage
from nemoir.persistence.sqlite_repository import SQLiteRepository


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def authorization(repository: SQLiteRepository) -> AuthorizationPolicy:
    return AuthorizationPolicy(repository, admin_user_ids={"admin-1"})


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Automated tests must not open network connections")

    monkeypatch.setattr(socket, "create_connection", denied)
    if os.name != "nt":
        monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def synthetic_bundle_data() -> dict:
    return json.loads((FIXTURES / "synthetic_bundle.json").read_text(encoding="utf-8"))


@pytest.fixture
def synthetic_messages(synthetic_bundle_data: dict) -> list[SourceMessage]:
    return [SourceMessage.model_validate(item) for item in synthetic_bundle_data["messages"]]


@pytest.fixture
def synthetic_analysis() -> AnalysisResult:
    return AnalysisResult.model_validate_json(
        (FIXTURES / "synthetic_analysis.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteRepository:
    repo = SQLiteRepository(tmp_path / "nemoir.sqlite3")
    yield repo
    repo.close()


@pytest.fixture
def captured_bundle(repository: SQLiteRepository, synthetic_messages: list[SourceMessage]):
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
            actor_user_id=message.author_user_id,
            external_message_id=message.external_message_id,
            author_display_name=message.author_display_name,
            channel_id=message.channel_id,
            content=message.content,
            source_url=message.source_url,
            timestamp=message.timestamp,
        )
    service.seal(bundle.id, actor_user_id="person-1", idempotency_key="seal-1")
    service.claim(
        bundle.id,
        actor_user_id="person-2",
        raw_topic="LLM consciousness and learned compassion",
    )
    return repository.get_bundle(bundle.id)
