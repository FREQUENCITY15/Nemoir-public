from __future__ import annotations

import sys

import pytest

from nemoir.__main__ import _synthetic_pilot_provider, main
from nemoir.domain.models import AnalysisRequest
from nemoir.domain.segmentation import segment_messages


def test_discord_pilot_refuses_connection_without_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["nemoir", "discord-pilot"])
    with pytest.raises(SystemExit, match="without --confirm-live"):
        main()


@pytest.mark.asyncio
async def test_synthetic_pilot_provider_remaps_live_message_ids(
    synthetic_bundle_data, synthetic_messages
) -> None:
    live_messages = [
        message.model_copy(update={"external_message_id": f"live-{index}"})
        for index, message in enumerate(synthetic_messages, start=1)
    ]
    provider = _synthetic_pilot_provider()
    response = await provider.analyse(
        AnalysisRequest(
            bundle_id="pilot-bundle",
            messages=live_messages,
            source_units=segment_messages(live_messages),
            raw_claim=synthetic_bundle_data["claim"],
        )
    )
    assert response.receipt.provider == "fake"
    assert response.receipt.model == "synthetic-discord-pilot"
    assert {
        fragment.source_message_id
        for fragment in response.result.claim.matching_fragments
    } == {"live-2"}
    assert {entry.source_message_id for entry in response.result.coverage} == {
        "live-1",
        "live-2",
        "live-3",
    }


@pytest.mark.asyncio
async def test_synthetic_pilot_provider_rejects_nonfixture_content(
    synthetic_bundle_data, synthetic_messages
) -> None:
    changed = list(synthetic_messages)
    changed[0] = changed[0].model_copy(update={"content": "Not the synthetic fixture."})
    provider = _synthetic_pilot_provider()
    with pytest.raises(ValueError, match="exact ordered synthetic fixture"):
        await provider.analyse(
            AnalysisRequest(
                bundle_id="pilot-bundle",
                messages=changed,
                source_units=segment_messages(changed),
                raw_claim=synthetic_bundle_data["claim"],
            )
        )


@pytest.mark.asyncio
async def test_synthetic_pilot_provider_rejects_nonfixture_claim(
    synthetic_messages,
) -> None:
    provider = _synthetic_pilot_provider()
    with pytest.raises(ValueError, match="synthetic fixture claim"):
        await provider.analyse(
            AnalysisRequest(
                bundle_id="pilot-bundle",
                messages=synthetic_messages,
                source_units=segment_messages(synthetic_messages),
                raw_claim="a different claim",
            )
        )
