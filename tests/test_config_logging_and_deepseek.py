from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nemoir.config import Settings
from nemoir.check_deepseek import check
from nemoir.domain.errors import LiveOperationDisabled, ProviderOutputValidationError
from nemoir.domain.models import AnalysisRequest, ClaimDiscoveryRequest, ProviderReceipt
from nemoir.domain.segmentation import segment_messages
from nemoir.domain.states import Actionability, CoverageClassification, TendrilType
from nemoir.logging_setup import redact
from nemoir.providers.deepseek import DeepSeekAnalysisProvider


def test_settings_safe_summary_never_contains_secrets(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-value")
    settings = Settings.from_environment()
    encoded = json.dumps(settings.safe_summary())
    assert "discord-secret-value" not in encoded
    assert "sk-secret-value" not in encoded
    assert settings.safe_summary()["deepseek_key_configured"] is True


def test_settings_parse_additional_intake_channels(monkeypatch) -> None:
    monkeypatch.setenv("NEMOIR_INTAKE_CHANNEL_ID", "primary")
    monkeypatch.setenv(
        "NEMOIR_ADDITIONAL_INTAKE_CHANNEL_IDS",
        " second, third, second,  ",
    )
    settings = Settings.from_environment()
    assert settings.intake_channel_id == "primary"
    assert settings.additional_intake_channel_ids == {"second", "third"}
    assert settings.safe_summary()["intake_channel_count"] == 3


def test_discord_pilot_settings_do_not_read_deepseek_environment(monkeypatch) -> None:
    requested: list[str] = []
    original_getenv = os.getenv

    def tracked_getenv(name: str, default=None):
        requested.append(name)
        return original_getenv(name, default)

    monkeypatch.setattr(os, "getenv", tracked_getenv)
    settings = Settings.from_environment(include_deepseek=False)
    assert not any(name.startswith("DEEPSEEK_") for name in requested)
    assert settings.deepseek_api_key is None
    assert settings.allow_live_deepseek is False


def test_log_redaction_handles_keys_and_bearer_tokens() -> None:
    message = "Authorization: Bearer secret-token DEEPSEEK_API_KEY=sk-abcdefghijk"
    result = redact(message)
    assert "secret-token" not in result
    assert "sk-abcdefghijk" not in result
    assert result.count("[REDACTED]") >= 2


@pytest.mark.asyncio
async def test_deepseek_live_gate_is_disabled_by_default(synthetic_messages) -> None:
    request = AnalysisRequest(
        bundle_id="bundle-live-gate",
        messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
        raw_claim="claim",
    )
    provider = DeepSeekAnalysisProvider(api_key="not-used", model="test-model")
    with pytest.raises(LiveOperationDisabled):
        await provider.analyse(request)


@pytest.mark.asyncio
async def test_deepseek_contract_parses_one_mocked_response(
    synthetic_messages, synthetic_analysis
) -> None:
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "mock-request-1",
                "model": "mock-model",
                "choices": [
                    {"message": {"content": synthetic_analysis.model_dump_json()}}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            }

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, headers, json):
            self.calls.append((url, headers, json))
            return FakeResponse()

    client = FakeClient()
    request = AnalysisRequest(
        bundle_id="bundle-mock",
        messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
        raw_claim="LLM consciousness and learned compassion",
    )
    provider = DeepSeekAnalysisProvider(
        api_key="secret-not-logged",
        model="mock-model",
        allow_live=True,
        client=client,
    )
    response = await provider.analyse(request)
    assert len(client.calls) == 1
    assert response.result.schema_version == "1.0"
    assert response.receipt.input_tokens == 100
    assert response.receipt.output_tokens == 200

    emitted_payload = json.loads(client.calls[0][2]["messages"][1]["content"])
    assert emitted_payload["allowed_values"] == {
        "tendril_type": [item.value for item in TendrilType],
        "actionability": [item.value for item in Actionability],
        "coverage_classification": [item.value for item in CoverageClassification],
    }
    coverage_shape = emitted_payload["required_output_shape"]["coverage"][0]
    assert set(coverage_shape) == {
        "source_message_id",
        "unit_id",
        "classification",
        "tendril_client_ids",
        "reason",
        "confidence",
    }


@pytest.mark.asyncio
async def test_deepseek_invalid_output_preserves_paid_call_receipt(
    synthetic_messages, synthetic_analysis
) -> None:
    invalid_payload = synthetic_analysis.model_dump(mode="json")
    invalid_payload["tendrils"][0]["type"] = "TENDRIL"
    raw_response = json.dumps(invalid_payload)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "mock-request-invalid",
                "model": "mock-model-returned",
                "choices": [{"message": {"content": raw_response}}],
                "usage": {"prompt_tokens": 321, "completion_tokens": 654},
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
    request = AnalysisRequest(
        bundle_id="bundle-invalid",
        messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
        raw_claim="LLM consciousness and learned compassion",
    )

    with pytest.raises(ProviderOutputValidationError) as captured:
        await provider.analyse(request)

    failure = captured.value
    assert failure.receipt.model == "mock-model-returned"
    assert failure.receipt.request_id == "mock-request-invalid"
    assert failure.receipt.input_tokens == 321
    assert failure.receipt.output_tokens == 654
    assert failure.receipt.outcome == "invalid_response"
    assert failure.raw_response == raw_response
    assert failure.validation_error_count == 1


@pytest.mark.asyncio
async def test_deepseek_discovery_live_gate_is_disabled_by_default(synthetic_messages) -> None:
    request = ClaimDiscoveryRequest(
        bundle_id="bundle-discovery-gate",
        messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
    )
    provider = DeepSeekAnalysisProvider(api_key="not-used", model="test-model")
    with pytest.raises(LiveOperationDisabled):
        await provider.discover_claim_candidates(request)


@pytest.mark.asyncio
async def test_deepseek_discovery_parses_one_mocked_response(synthetic_messages) -> None:
    candidate_set = {
        "schema_version": "1.0",
        "bundle_id": "bundle-discovery",
        "candidates": [
            {
                "candidate_id": "c1",
                "title": "Free will",
                "summary": "A summary.",
                "display_order": 1,
                "evidence": [
                    {
                        "source_message_id": "synthetic-102",
                        "exact_quote": "Free will matters only if choices have consequences.",
                        "unit_ids": ["synthetic-102:p1"],
                    }
                ],
            },
            {
                "candidate_id": "c2",
                "title": "Time",
                "summary": "Another summary.",
                "display_order": 2,
                "evidence": [
                    {
                        "source_message_id": "synthetic-101",
                        "exact_quote": "I keep wondering whether time can be both eternal and an illusion.",
                        "unit_ids": ["synthetic-101:p2"],
                    }
                ],
            },
        ],
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "mock-discovery-1",
                "model": "mock-model",
                "choices": [{"message": {"content": json.dumps(candidate_set)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
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
    request = ClaimDiscoveryRequest(
        bundle_id="bundle-discovery",
        messages=synthetic_messages,
        source_units=segment_messages(synthetic_messages),
    )
    response = await provider.discover_claim_candidates(request)
    assert len(client.calls) == 1
    assert response.result.schema_version == "1.0"
    assert len(response.result.candidates) == 2
    assert response.receipt.input_tokens == 10
    emitted = json.loads(client.calls[0][2]["messages"][1]["content"])
    assert emitted["required_output_shape"]["candidates"][0]["title"] == "concise inferred title"


@pytest.mark.asyncio
async def test_live_checker_prints_receipt_for_invalid_output(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured-for-mocked-check")
    monkeypatch.setenv("NEMOIR_ALLOW_LIVE_DEEPSEEK", "true")

    async def fail_with_receipt(self, request):
        raise ProviderOutputValidationError(
            receipt=self_receipt,
            raw_response='{"invalid":true}',
            validation_error_count=4,
        )

    self_receipt = ProviderReceipt(
        provider="deepseek",
        model="mock-check-model",
        latency_ms=77,
        input_tokens=111,
        output_tokens=222,
        outcome="invalid_response",
        request_id="mock-check-request",
    )
    monkeypatch.setattr(DeepSeekAnalysisProvider, "analyse", fail_with_receipt)
    fixture = Path(__file__).parent / "fixtures" / "synthetic_bundle.json"

    result = await check(fixture, confirm_live=True)

    assert result == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "provider": "deepseek",
        "model": "mock-check-model",
        "request_id": "mock-check-request",
        "latency_ms": 77,
        "input_tokens": 111,
        "output_tokens": 222,
        "outcome": "invalid_response",
        "output_contract_valid": False,
        "validation_error_count": 4,
    }
