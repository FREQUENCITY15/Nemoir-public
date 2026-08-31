"""Synthetic channel-writing test mode: safety gates and runtime identity."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nemoir.__main__ import _synthetic_pilot_provider, main
from nemoir.config import Settings
from nemoir.gui.controller import build_launch_request
from nemoir.runtime import RuntimeStatusStore


def test_channel_test_launch_request_uses_synthetic_and_sets_gate() -> None:
    base_env = {
        "PATH": "C:\\Windows",
        "DEEPSEEK_API_KEY": "sk-secret",
        "NEMOIR_ALLOW_LIVE_DEEPSEEK": "true",
    }
    request = build_launch_request(
        "channel-test",
        python_exe="C:\\venv\\python.exe",
        repo_root=Path("C:\\repo"),
        runtime_dir=Path("C:\\repo\\data\\runtime"),
        base_env=base_env,
        instance_id="gui-1",
    )
    assert request.argv == (
        "C:\\venv\\python.exe",
        "-m",
        "nemoir",
        "discord-channel-test",
        "--confirm-live",
    )
    assert request.env["NEMOIR_ALLOW_CHANNEL_WRITE"] == "true"
    # Channel-test mode never reads DeepSeek configuration.
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in request.env
    assert "DEEPSEEK_API_KEY" not in request.env
    # The parent environment is never mutated.
    assert base_env["DEEPSEEK_API_KEY"] == "sk-secret"


def test_synthetic_launch_never_sets_channel_write_gate() -> None:
    request = build_launch_request(
        "synthetic",
        python_exe="C:\\venv\\python.exe",
        repo_root=Path("C:\\repo"),
        runtime_dir=Path("C:\\repo\\data\\runtime"),
        base_env={"PATH": "C:\\Windows"},
        instance_id="gui-1",
    )
    assert "NEMOIR_ALLOW_CHANNEL_WRITE" not in request.env


def test_channel_test_cli_refuses_without_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["nemoir", "discord-channel-test"])
    with pytest.raises(SystemExit, match="without --confirm-live"):
        main()


def test_channel_test_cli_refuses_without_channel_write_gate(monkeypatch) -> None:
    monkeypatch.delenv("NEMOIR_ALLOW_CHANNEL_WRITE", raising=False)
    monkeypatch.setattr(sys, "argv", ["nemoir", "discord-channel-test", "--confirm-live"])
    with pytest.raises(SystemExit, match="NEMOIR_ALLOW_CHANNEL_WRITE=true"):
        main()


@pytest.mark.asyncio
async def test_channel_test_provider_never_contacts_deepseek(synthetic_bundle_data, synthetic_messages) -> None:
    from nemoir.domain.models import AnalysisRequest
    from nemoir.domain.segmentation import segment_messages

    provider = _synthetic_pilot_provider()
    response = await provider.analyse(
        AnalysisRequest(
            bundle_id="channel-test-bundle",
            messages=synthetic_messages,
            source_units=segment_messages(synthetic_messages),
            raw_claim=synthetic_bundle_data["claim"],
        )
    )
    assert response.receipt.provider == "fake"
    assert response.receipt.model == "synthetic-discord-pilot"
    assert "deepseek" not in response.receipt.provider.lower()
    assert "deepseek" not in response.receipt.model.lower()


def test_run_discord_bot_records_channel_test_mode(monkeypatch, tmp_path) -> None:
    from nemoir.adapters.discord_bot import run_discord_bot

    store = RuntimeStatusStore(tmp_path / "runtime")
    seen: list[str] = []

    class FakeBot:
        def run(self, token, log_handler=None):
            seen.append(store.read().mode)

    monkeypatch.setattr(
        "nemoir.adapters.discord_bot.create_discord_bot",
        lambda *args, **kwargs: FakeBot(),
    )
    settings = Settings(discord_bot_token="not-a-real-token")
    run_discord_bot(
        settings,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        channel_test=True,
        status_store=store,
        instance_id="pid-1",
    )
    assert seen == ["channel-test"]
