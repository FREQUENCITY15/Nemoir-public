"""Strict autonomous-sort validation: full-source coverage and fail-closed output."""

from __future__ import annotations

from copy import deepcopy

import pytest

from nemoir.application.capture_service import CaptureService
from nemoir.domain.models import AutonomousSortRequest, SourceMessage
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.validation import validate_autonomous_sort
from nemoir.persistence.sqlite_repository import SQLiteRepository
from nemoir.providers.synthetic import (
    SyntheticAutonomousSortProvider,
    synthetic_autonomous_sort,
)


def _sealed_bundle(repository: SQLiteRepository, messages: list[SourceMessage]):
    capture = CaptureService(repository)
    bundle = capture.start_capture(
        guild_id="guild-1",
        intake_channel_id="intake-1",
        submitter_user_id="person-1",
        recipient_user_id="unused",
        autonomous=True,
    )
    for message in messages:
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
    capture.seal(bundle.id, actor_user_id="person-1", idempotency_key="auto-seal")
    return repository.get_bundle(bundle.id)


def _sort_result(messages):
    units = segment_messages(messages)
    request = AutonomousSortRequest(
        bundle_id="validation-bundle",
        messages=messages,
        source_units=units,
    )
    return synthetic_autonomous_sort(request)


async def test_synthetic_sort_is_strictly_valid(synthetic_messages) -> None:
    provider = SyntheticAutonomousSortProvider()
    units = segment_messages(synthetic_messages)
    response = await provider.sort(
        AutonomousSortRequest(
            bundle_id="auto-valid",
            messages=synthetic_messages,
            source_units=units,
        )
    )
    # Deterministic topic IDs and display order.
    assert [topic.provider_client_id for topic in response.result.topics] == [
        f"topic-{n}" for n in range(1, len(response.result.topics) + 1)
    ]
    assert [topic.display_order for topic in response.result.topics] == list(
        range(1, len(response.result.topics) + 1)
    )
    assert response.receipt.provider == "synthetic-autonomous"
    # Full source coverage: every unit appears exactly once as a primary topic.
    covered = [entry.unit_id for entry in response.result.coverage]
    assert sorted(covered) == sorted(unit.unit_id for unit in units)
    assert len(set(covered)) == len(covered)


def test_valid_result_passes_validation(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    result = _sort_result(synthetic_messages).model_copy(
        update={"bundle_id": bundle.id}
    )
    report = validate_autonomous_sort(bundle, bundle.source_units, result)
    assert report.valid
    assert not report.errors


def test_invented_quote_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = _sort_result(synthetic_messages).model_dump(mode="python")
    payload["topics"][0]["evidence"][0]["exact_quote"] = "This quotation never existed."
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert {issue.code for issue in report.errors} & {
        "INVENTED_QUOTE",
        "AUTONOMOUS_UNIT_NOT_SOURCE_BACKED",
    }


def test_dropped_unit_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    payload["coverage"] = payload["coverage"][:-1]
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "MISSING_COVERAGE" in {issue.code for issue in report.errors}


def test_duplicated_evidence_across_topics_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    if len(payload["topics"]) < 2:
        return
    payload["topics"][1]["evidence"] = [
        *payload["topics"][1]["evidence"],
        *deepcopy(payload["topics"][0]["evidence"]),
    ]
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "AUTONOMOUS_DUPLICATE_EVIDENCE" in {issue.code for issue in report.errors}


def test_context_coverage_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    payload["coverage"][0]["classification"] = "CONTEXT"
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "AUTONOMOUS_CONTEXT_COVERAGE" in {issue.code for issue in report.errors}


def test_multi_topic_assignment_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    if len(payload["topics"]) < 2:
        return
    payload["coverage"][0]["tendril_client_ids"] = [
        payload["topics"][0]["provider_client_id"],
        payload["topics"][1]["provider_client_id"],
    ]
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "AUTONOMOUS_PRIMARY_TOPIC_ASSIGNMENT" in {issue.code for issue in report.errors}


def test_missing_offsets_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    payload["topics"][0]["evidence"][0]["start_offset"] = None
    payload["topics"][0]["evidence"][0]["end_offset"] = None
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "AUTONOMOUS_FRAGMENT_WITHOUT_OFFSETS" in {issue.code for issue in report.errors}


def test_duplicate_topic_ids_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    if len(payload["topics"]) < 2:
        return
    payload["topics"][1]["provider_client_id"] = payload["topics"][0][
        "provider_client_id"
    ]
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "DUPLICATE_TOPIC_CLIENT_ID" in {issue.code for issue in report.errors}


def test_bad_display_order_rejected(repository, synthetic_messages) -> None:
    bundle = _sealed_bundle(repository, synthetic_messages)
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    for topic in payload["topics"]:
        topic["display_order"] = 1
    from nemoir.domain.models import AutonomousSortResult

    report = validate_autonomous_sort(
        bundle, bundle.source_units, AutonomousSortResult.model_validate(payload)
    )
    assert not report.valid
    assert "AUTONOMOUS_TOPIC_ORDER" in {issue.code for issue in report.errors}


def test_empty_topic_rejected(repository, synthetic_messages) -> None:
    # The strict contract itself refuses an evidence-less topic before any
    # tendril or channel could exist (fail closed at the provider boundary).
    payload = deepcopy(_sort_result(synthetic_messages).model_dump(mode="python"))
    payload["topics"][0]["evidence"] = []
    from pydantic import ValidationError

    from nemoir.domain.models import AutonomousSortResult

    with pytest.raises(ValidationError):
        AutonomousSortResult.model_validate(payload)
