"""Autonomous modes: CLI gates, runtime status identity, Control Panel launches."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nemoir.__main__ import main
from nemoir.gui.controller import (
    ControlPanelController,
    LaunchRequest,
    _display_status,
    build_launch_request,
)
from nemoir.runtime import RuntimeStatusStore, StatusSnapshot


# -- CLI gates ------------------------------------------------------------


def test_autonomous_test_cli_refuses_without_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["nemoir", "discord-autonomous-test"])
    with pytest.raises(SystemExit, match="without --confirm-live"):
        main()


def test_autonomous_test_alias_refuses_without_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["nemoir", "autonomous-test"])
    with pytest.raises(SystemExit, match="without --confirm-live"):
        main()


def test_autonomous_test_cli_requires_channel_write_gate(monkeypatch) -> None:
    monkeypatch.delenv("NEMOIR_ALLOW_CHANNEL_WRITE", raising=False)
    monkeypatch.delenv("NEMOIR_ALLOW_LIVE_DEEPSEEK", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["nemoir", "discord-autonomous-test", "--confirm-live"]
    )
    with pytest.raises(SystemExit, match="NEMOIR_ALLOW_CHANNEL_WRITE=true"):
        main()


def test_autonomous_live_cli_refuses_without_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["nemoir", "discord-autonomous-live"])
    with pytest.raises(SystemExit, match="without --confirm-live"):
        main()


def test_autonomous_live_cli_requires_live_gate(monkeypatch) -> None:
    monkeypatch.delenv("NEMOIR_ALLOW_LIVE_DEEPSEEK", raising=False)
    monkeypatch.setenv("NEMOIR_ALLOW_CHANNEL_WRITE", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    monkeypatch.setattr(
        sys, "argv", ["nemoir", "discord-autonomous-live", "--confirm-live"]
    )
    with pytest.raises(SystemExit, match="NEMOIR_ALLOW_LIVE_DEEPSEEK=true"):
        main()


def test_autonomous_live_cli_requires_channel_write_gate_too(monkeypatch) -> None:
    monkeypatch.setenv("NEMOIR_ALLOW_LIVE_DEEPSEEK", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    monkeypatch.delenv("NEMOIR_ALLOW_CHANNEL_WRITE", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["nemoir", "discord-autonomous-live", "--confirm-live"]
    )
    with pytest.raises(SystemExit, match="NEMOIR_ALLOW_CHANNEL_WRITE=true"):
        main()


def test_autonomous_live_cli_requires_api_key(monkeypatch) -> None:
    monkeypatch.setenv("NEMOIR_ALLOW_LIVE_DEEPSEEK", "true")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("NEMOIR_ALLOW_CHANNEL_WRITE", "true")
    monkeypatch.setattr(
        sys, "argv", ["nemoir", "discord-autonomous-live", "--confirm-live"]
    )
    with pytest.raises(SystemExit, match="DEEPSEEK_API_KEY is not configured"):
        main()


def test_autonomous_live_cli_requires_both_gates(monkeypatch) -> None:
    monkeypatch.setenv("NEMOIR_ALLOW_LIVE_DEEPSEEK", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    monkeypatch.delenv("NEMOIR_ALLOW_CHANNEL_WRITE", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["nemoir", "autonomous-live", "--confirm-live"]
    )
    with pytest.raises(SystemExit, match="NEMOIR_ALLOW_CHANNEL_WRITE=true"):
        main()


# -- runtime status identity ---------------------------------------------


def test_run_discord_bot_records_autonomous_test_mode(monkeypatch, tmp_path) -> None:
    from nemoir.adapters.discord_bot import run_discord_bot
    from nemoir.config import Settings

    store = RuntimeStatusStore(tmp_path / "runtime")
    seen: list[tuple[str, str | None]] = []

    class FakeBot:
        def run(self, token, log_handler=None):
            snapshot = store.read()
            seen.append((snapshot.mode, snapshot.model))

    monkeypatch.setattr(
        "nemoir.adapters.discord_bot.create_discord_bot",
        lambda *args, **kwargs: FakeBot(),
    )
    settings = Settings(discord_bot_token="not-a-real-token")
    run_discord_bot(
        settings,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        autonomous_mode="test",
        status_store=store,
        instance_id="pid-1",
    )
    assert seen == [("autonomous-test", None)]


def test_run_discord_bot_records_autonomous_live_mode(monkeypatch, tmp_path) -> None:
    from nemoir.adapters.discord_bot import run_discord_bot
    from nemoir.config import Settings

    store = RuntimeStatusStore(tmp_path / "runtime")
    seen: list[tuple[str, str | None]] = []

    class FakeBot:
        def run(self, token, log_handler=None):
            snapshot = store.read()
            seen.append((snapshot.mode, snapshot.model))

    monkeypatch.setattr(
        "nemoir.adapters.discord_bot.create_discord_bot",
        lambda *args, **kwargs: FakeBot(),
    )
    settings = Settings(
        discord_bot_token="not-a-real-token", deepseek_model="deepseek-v4-flash"
    )
    run_discord_bot(
        settings,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        autonomous_mode="live",
        status_store=store,
        instance_id="pid-1",
    )
    assert seen == [("autonomous-live", "deepseek-v4-flash")]


def test_status_store_round_trips_autonomous_modes(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    store.record_starting(
        mode="autonomous-test", instance_id="pid-1", pid=4242
    )
    assert store.read().mode == "autonomous-test"
    store.record_starting(
        mode="autonomous-live", instance_id="pid-1", pid=4242
    )
    assert store.read().mode == "autonomous-live"


def test_display_status_distinguishes_autonomous_modes() -> None:
    assert (
        _display_status(StatusSnapshot(state="ONLINE", mode="autonomous-test"))
        == "ONLINE — AUTONOMOUS TEST"
    )
    assert (
        _display_status(StatusSnapshot(state="ONLINE", mode="autonomous-live"))
        == "ONLINE — AUTONOMOUS LIVE"
    )
    # Ordinary modes keep their existing labels.
    assert _display_status(StatusSnapshot(state="ONLINE", mode="synthetic")) == "ONLINE — SYNTHETIC"
    assert (
        _display_status(StatusSnapshot(state="ONLINE", mode="channel-test"))
        == "ONLINE — CHANNEL TEST"
    )


# -- Control Panel launch environments ------------------------------------


def test_autonomous_test_launch_strips_deepseek_and_sets_channel_gate() -> None:
    base_env = {
        "PATH": "C:\\Windows",
        "DEEPSEEK_API_KEY": "sk-secret",
        "NEMOIR_ALLOW_LIVE_DEEPSEEK": "true",
    }
    request = build_launch_request(
        "autonomous-test",
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
        "discord-autonomous-test",
        "--confirm-live",
    )
    assert request.env["NEMOIR_ALLOW_CHANNEL_WRITE"] == "true"
    # Autonomous Test never reads DeepSeek configuration.
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in request.env
    assert "DEEPSEEK_API_KEY" not in request.env
    # The parent environment is never mutated.
    assert base_env["DEEPSEEK_API_KEY"] == "sk-secret"


def test_autonomous_live_launch_sets_both_gates_only_in_child() -> None:
    base_env = {
        "PATH": "C:\\Windows",
        "DEEPSEEK_API_KEY": "sk-secret",
    }
    request = build_launch_request(
        "autonomous-live",
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
        "discord-autonomous-live",
        "--confirm-live",
    )
    assert request.env["NEMOIR_ALLOW_LIVE_DEEPSEEK"] == "true"
    assert request.env["NEMOIR_ALLOW_CHANNEL_WRITE"] == "true"
    assert request.env["DEEPSEEK_API_KEY"] == "sk-secret"
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in base_env
    assert "NEMOIR_ALLOW_CHANNEL_WRITE" not in base_env


# -- Control Panel controller ---------------------------------------------


class FakeDialog:
    def __init__(self) -> None:
        self.confirm_autonomous_test_result = True
        self.confirm_autonomous_live_result = True
        self.autonomous_test_confirmations = 0
        self.autonomous_live_confirmations = 0
        self.messages: list[str] = []

    def confirm_autonomous_test(self) -> bool:
        self.autonomous_test_confirmations += 1
        return self.confirm_autonomous_test_result

    def confirm_autonomous_live(self) -> bool:
        self.autonomous_live_confirmations += 1
        return self.confirm_autonomous_live_result

    def confirm_live(self) -> bool:
        return True

    def confirm_channel_test(self) -> bool:
        return True

    def confirm_close(self) -> bool:
        return True

    def show_message(self, text: str) -> None:
        self.messages.append(text)


class FakeRunner:
    def __init__(self) -> None:
        self.launches: list[LaunchRequest] = []

    def __call__(self, request: LaunchRequest):
        self.launches.append(request)

        class Child:
            pid = 1000 + len(self.launches)

            def poll(self):
                return None

        return Child()


class FakeLock:
    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


class FakeAssess:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[bool] = []

    def __call__(self, live: bool):
        self.calls.append(live)

        class Readiness:
            pass

        result = Readiness()
        result.ready = self.ready
        result.missing = () if self.ready else ("DeepSeek API key",)
        return result


def _make_controller(tmp_path, *, ready: bool = True) -> tuple[
    ControlPanelController, FakeRunner, FakeDialog, FakeAssess
]:
    from datetime import datetime, timezone

    store = RuntimeStatusStore(tmp_path / "runtime")
    runner = FakeRunner()
    dialog = FakeDialog()
    assess = FakeAssess(ready)

    def build_request(mode: str, instance_id: str) -> LaunchRequest:
        return LaunchRequest(
            mode=mode,
            argv=(f"python-{mode}",),
            env={},
            cwd=".",
            instance_id=instance_id,
            no_window=True,
        )

    controller = ControlPanelController(
        status_store=store,
        runner=runner,
        build_request=build_request,
        lock_factory=FakeLock,
        dialog=dialog,
        assess=assess,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        pid_alive=lambda pid: True,
        notify_message=dialog.show_message,
    )
    return controller, runner, dialog, assess


def test_autonomous_test_confirmation_cancellation_launches_nothing(
    tmp_path: Path,
) -> None:
    controller, runner, dialog, _assess = _make_controller(tmp_path)
    dialog.confirm_autonomous_test_result = False
    controller.start_autonomous_test()
    assert dialog.autonomous_test_confirmations == 1
    assert runner.launches == []


def test_autonomous_live_confirmation_cancellation_launches_nothing(
    tmp_path: Path,
) -> None:
    controller, runner, dialog, _assess = _make_controller(tmp_path)
    dialog.confirm_autonomous_live_result = False
    controller.start_autonomous_live()
    assert dialog.autonomous_live_confirmations == 1
    assert runner.launches == []


def test_autonomous_test_confirmation_launches_autonomous_test(tmp_path: Path) -> None:
    controller, runner, _dialog, assess = _make_controller(tmp_path)
    controller.start_autonomous_test()
    assert [launch.mode for launch in runner.launches] == ["autonomous-test"]
    assert assess.calls == [False]  # never assesses DeepSeek readiness


def test_autonomous_live_confirmation_launches_autonomous_live(tmp_path: Path) -> None:
    controller, runner, _dialog, assess = _make_controller(tmp_path)
    controller.start_autonomous_live()
    assert [launch.mode for launch in runner.launches] == ["autonomous-live"]
    assert assess.calls == [True]  # DeepSeek readiness IS assessed


def test_autonomous_live_requires_ready_configuration(tmp_path: Path) -> None:
    controller, runner, dialog, _assess = _make_controller(tmp_path, ready=False)
    controller.start_autonomous_live()
    assert runner.launches == []
    assert dialog.messages
    assert "DeepSeek API key" in dialog.messages[0]
