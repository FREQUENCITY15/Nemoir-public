"""Adapter-level tests for the shared runtime-status boundary.

These exercise the discord.py adapter hooks with a status store, but never
connect to Discord: event handlers are invoked directly and the bot's
``run``/``sync`` methods are faked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nemoir.adapters.discord_bot import create_discord_bot, run_discord_bot
from nemoir.config import Settings
from nemoir.providers.fake import FakeAnalysisProvider
from nemoir.runtime import RuntimeStatusStore, ShutdownRequest


def _settings() -> Settings:
    return Settings(
        discord_bot_token="not-a-real-token",
        guild_id="1",
        intake_channel_id="2",
        anemone_category_id="3",
        nursery_channel_id="4",
    )


def _create_bot(tmp_path: Path, synthetic_analysis, **kwargs):
    pytest.importorskip("discord")
    store = RuntimeStatusStore(tmp_path / "runtime")
    bot = create_discord_bot(
        _settings(),
        None,  # repository is not touched by the event handlers under test
        FakeAnalysisProvider(synthetic_analysis),
        pilot_mode=True,
        status_store=store,
        **kwargs,
    )
    return bot, store


async def _sync_noop(*_args, **_kwargs) -> None:
    return None


def test_bot_construction_does_not_record_online(tmp_path, synthetic_analysis) -> None:
    bot, store = _create_bot(tmp_path, synthetic_analysis)
    # Constructing the bot must never claim Discord is ready.
    assert store.read().state == "OFFLINE"


async def test_on_ready_records_online_only_after_ready(tmp_path, synthetic_analysis) -> None:
    bot, store = _create_bot(tmp_path, synthetic_analysis)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    assert store.read().state == "STARTING"

    bot.tree.sync = _sync_noop
    bot.tree.copy_global_to = lambda **kwargs: None
    await bot.on_ready()

    snapshot = store.read()
    assert snapshot.state == "ONLINE"
    assert snapshot.mode == "synthetic"  # preserved from STARTING
    assert snapshot.last_heartbeat_at is not None


async def test_on_disconnect_records_reconnecting(tmp_path, synthetic_analysis) -> None:
    bot, store = _create_bot(tmp_path, synthetic_analysis)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    await bot.on_disconnect()
    assert store.read().state == "RECONNECTING"


async def test_on_resumed_records_online(tmp_path, synthetic_analysis) -> None:
    bot, store = _create_bot(tmp_path, synthetic_analysis)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    await bot.on_resumed()
    snapshot = store.read()
    assert snapshot.state == "ONLINE"
    assert snapshot.last_heartbeat_at is not None


async def test_on_error_records_safe_class_only(tmp_path, synthetic_analysis) -> None:
    bot, store = _create_bot(tmp_path, synthetic_analysis)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    await bot.on_error(SimpleNamespace(__name__="on_message"), "boom", extra="secret")
    snapshot = store.read()
    assert snapshot.state == "ERROR"
    assert snapshot.error_class == "DISCORD_EVENT:on_message"


async def test_heartbeat_updates_and_honours_shutdown_request(
    tmp_path, synthetic_analysis
) -> None:
    bot, store = _create_bot(
        tmp_path,
        synthetic_analysis,
        shutdown_request=ShutdownRequest(tmp_path / "runtime", "pid-1"),
        heartbeat_interval=0.01,
    )
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)

    bot.tree.sync = _sync_noop
    bot.tree.copy_global_to = lambda **kwargs: None
    closed = asyncio.Event()

    async def fake_close() -> None:
        closed.set()

    bot.close = fake_close  # type: ignore[assignment]

    await bot.on_ready()
    assert store.read().state == "ONLINE"

    async def wait_until(predicate, timeout: float = 2.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    assert await wait_until(lambda: store.read().last_heartbeat_at is not None)

    request = ShutdownRequest(tmp_path / "runtime", "pid-1")
    request.request()
    assert await wait_until(lambda: store.read().state == "STOPPING")
    assert await wait_until(lambda: closed.is_set())
    # The request file is cleared once honoured.
    assert request.is_requested() is False


def test_run_discord_bot_records_starting_before_connecting_and_offline_after(
    monkeypatch, tmp_path
) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    seen_during_run: list[str] = []

    class FakeBot:
        def run(self, token, log_handler=None):
            seen_during_run.append(store.read().state)

    monkeypatch.setattr(
        "nemoir.adapters.discord_bot.create_discord_bot",
        lambda *args, **kwargs: FakeBot(),
    )
    run_discord_bot(
        _settings(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        pilot_mode=True,
        status_store=store,
        instance_id="pid-1",
    )
    assert seen_during_run == ["STARTING"]
    assert store.read().state == "OFFLINE"
    assert store.read().instance_id == "pid-1"


def test_run_discord_bot_records_safe_error_class_on_exception(monkeypatch, tmp_path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")

    class FakeBot:
        def run(self, token, log_handler=None):
            raise RuntimeError("a secret password should not leak")

    monkeypatch.setattr(
        "nemoir.adapters.discord_bot.create_discord_bot",
        lambda *args, **kwargs: FakeBot(),
    )
    with pytest.raises(RuntimeError):
        run_discord_bot(
            _settings(),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            pilot_mode=True,
            status_store=store,
            instance_id="pid-1",
        )
    snapshot = store.read()
    assert snapshot.state == "ERROR"
    assert snapshot.error_class == "RuntimeError"
    assert "password" not in str(snapshot.error_class)
