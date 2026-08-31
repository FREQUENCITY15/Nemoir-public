"""Autonomous sort providers: synthetic determinism and gated DeepSeek contract."""

from __future__ import annotations

import json

import pytest

from nemoir.domain.errors import LiveOperationDisabled, ProviderOutputValidationError
from nemoir.domain.models import AutonomousSortRequest
from nemoir.domain.segmentation import segment_messages
from nemoir.providers.deepseek import DeepSeekAnalysisProvider, _autonomous_sort_request_payload
from nemoir.providers.synthetic import (
    SyntheticAutonomousSortProvider,
    synthetic_autonomous_sort,
)


def _request(synthetic_messages, bundle_id="provider-bundle") -> AutonomousSortRequest:
    return AutonomousSortRequest(
        bundle_id=bundle_id,
        messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
    )


@pytest.mark.asyncio
async def test_synthetic_sort_is_deterministic(synthetic_messages) -> None:
    first = await SyntheticAutonomousSortProvider().sort(_request(synthetic_messages))
    second = await SyntheticAutonomousSortProvider().sort(_request(synthetic_messages))
    assert first.result.model_dump(mode="json") == second.result.model_dump(mode="json")
    # Topic IDs and order are stable across calls (never wall-clock derived).
    assert [topic.provider_client_id for topic in first.result.topics] == [
        topic.provider_client_id for topic in second.result.topics
    ]
    assert [topic.display_order for topic in first.result.topics] == [
        topic.display_order for topic in second.result.topics
    ]


@pytest.mark.asyncio
async def test_synthetic_sort_never_contacts_deepseek(synthetic_messages) -> None:
    # The autouse socket-denial fixture in conftest guarantees this raises if
    # any network is attempted; the provider is purely deterministic.
    response = await SyntheticAutonomousSortProvider().sort(_request(synthetic_messages))
    assert response.receipt.provider != "deepseek"
    assert "deepseek" not in response.receipt.model


@pytest.mark.asyncio
async def test_synthetic_invalid_schema_raises_contract_error(synthetic_messages) -> None:
    provider = SyntheticAutonomousSortProvider(variant="invalid_schema")
    with pytest.raises(ProviderOutputValidationError) as captured:
        await provider.sort(_request(synthetic_messages))
    assert captured.value.raw_response == '{"not": "an autonomous sort"}'
    assert captured.value.receipt.outcome == "invalid_response"


@pytest.mark.asyncio
async def test_synthetic_provider_failure_propagates(synthetic_messages) -> None:
    provider = SyntheticAutonomousSortProvider(variant="provider_failure")
    with pytest.raises(RuntimeError, match="autonomous-sort failure"):
        await provider.sort(_request(synthetic_messages))


@pytest.mark.asyncio
async def test_deepseek_autonomous_sort_live_gate_disabled_by_default(
    synthetic_messages,
) -> None:
    provider = DeepSeekAnalysisProvider(api_key="not-used", model="test-model")
    with pytest.raises(LiveOperationDisabled):
        await provider.sort(_request(synthetic_messages))


@pytest.mark.asyncio
async def test_deepseek_autonomous_sort_parses_one_mocked_response(
    synthetic_messages,
) -> None:
    result = synthetic_autonomous_sort(_request(synthetic_messages, "mock-bundle"))
    raw_response = result.model_dump_json()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "mock-autonomous-1",
                "model": "mock-model",
                "choices": [{"message": {"content": raw_response}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 60},
            }

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, headers, json):
            self.calls.append((url, headers, json))
            return FakeResponse()

    client = FakeClient()
    provider = DeepSeekAnalysisProvider(
        api_key="secret-not-logged",
        model="mock-model",
        allow_live=True,
        client=client,
    )
    response = await provider.sort(_request(synthetic_messages, "mock-bundle"))
    assert len(client.calls) == 1
    assert response.result.bundle_id == "mock-bundle"
    assert response.receipt.input_tokens == 40
    assert response.receipt.output_tokens == 60
    assert response.receipt.provider == "deepseek"

    # The emitted payload carries the strict JSON contract.
    emitted = json.loads(client.calls[0][2]["messages"][1]["content"])
    assert emitted["schema_version"] == "1.0"
    assert emitted["allowed_values"]["coverage_classification"] == [
        "CLAIMED",
        "TENDRIL",
        "CONTEXT",
        "DUPLICATE",
    ]
    shape = emitted["required_output_shape"]["coverage"][0]
    assert set(shape) == {
        "source_message_id",
        "unit_id",
        "classification",
        "tendril_client_ids",
        "reason",
        "confidence",
    }
    topic_shape = emitted["required_output_shape"]["topics"][0]
    assert set(topic_shape) == {
        "provider_client_id",
        "display_order",
        "title",
        "summary",
        "tendril_type",
        "actionability",
        "evidence",
        "why_open",
        "suggested_habitat_slug",
        "confidence",
    }


@pytest.mark.asyncio
async def test_deepseek_autonomous_sort_invalid_output_preserves_paid_receipt(
    synthetic_messages,
) -> None:
    raw_response = json.dumps({"topics": "not a list"})

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "mock-invalid-autonomous",
                "model": "mock-model-returned",
                "choices": [{"message": {"content": raw_response}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8},
            }

    class FakeClient:
        async def post(self, url, headers, json):
            return FakeResponse()

    provider = DeepSeekAnalysisProvider(
        api_key="secret-not-logged",
        model="mock-model-requested",
        allow_live=True,
        client=FakeClient(),
    )
    with pytest.raises(ProviderOutputValidationError) as captured:
        await provider.sort(_request(synthetic_messages))

    failure = captured.value
    assert failure.receipt.model == "mock-model-returned"
    assert failure.receipt.outcome == "invalid_response"
    assert failure.receipt.input_tokens == 7
    assert failure.receipt.output_tokens == 8
    assert failure.raw_response == raw_response


def test_autonomous_payload_never_contains_credentials(synthetic_messages) -> None:
    request = _request(synthetic_messages)
    payload = _autonomous_sort_request_payload(request)
    assert "sk-" not in payload
    assert "api_key" not in payload
