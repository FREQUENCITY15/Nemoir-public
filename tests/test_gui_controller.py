"""Display-free tests for the Nemoir Control Panel controller.

Everything (subprocess, lock, dialogs, configuration, time, PID liveness) is
faked, so no Tk display, Discord connection, or network is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nemoir.gui.controller import (
    ConfigReadiness,
    ControlPanelController,
    LaunchRequest,
    build_launch_request,
)
from nemoir.runtime import DEFAULT_STALE_AFTER, RuntimeStatusStore, ShutdownRequest


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeChild:
    def __init__(self, pid: int = 4242, *, die_on_terminate: bool = True) -> None:
        self.pid = pid
        self._code: int | None = None
        self.terminated = False
        self.killed = False
        self._die_on_terminate = die_on_terminate

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        if self._die_on_terminate:
            self._code = -15

    def kill(self) -> None:
        self.killed = True
        self._code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._code if self._code is not None else -1


class FakeRunner:
    def __init__(self) -> None:
        self.launches: list[LaunchRequest] = []
        self.children: list[FakeChild] = []
        self.next_child: FakeChild | None = None

    def __call__(self, request: LaunchRequest) -> FakeChild:
        self.launches.append(request)
        child = self.next_child or FakeChild(pid=1000 + len(self.children))
        self.children.append(child)
        self.next_child = None
        return child


class FakeLock:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.acquired = False
        self.released = False

    def acquire(self) -> bool:
        if self.result:
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        self.released = True


class FakeDialog:
    def __init__(self) -> None:
        self.confirm_live_result = True
        self.confirm_channel_test_result = True
        self.confirm_close_result = True
        self.live_confirmations = 0
        self.channel_test_confirmations = 0
        self.close_confirmations = 0
        self.messages: list[str] = []

    def confirm_live(self) -> bool:
        self.live_confirmations += 1
        return self.confirm_live_result

    def confirm_channel_test(self) -> bool:
        self.channel_test_confirmations += 1
        return self.confirm_channel_test_result

    def confirm_close(self) -> bool:
        self.close_confirmations += 1
        return self.confirm_close_result

    def show_message(self, text: str) -> None:
        self.messages.append(text)


@dataclass
class Harness:
    clock: Clock
    store: RuntimeStatusStore
    dialog: FakeDialog
    runner: FakeRunner
    locks: list[FakeLock]
    controller: ControlPanelController
    runtime_dir: Path


def make_harness(
    tmp_path: Path,
    *,
    lock_result: bool = True,
    pid_alive=lambda pid: True,
    assess=None,
    base_env: dict[str, str] | None = None,
    graceful_timeout: timedelta = timedelta(seconds=15.0),
    terminate_timeout: timedelta = timedelta(seconds=5.0),
) -> Harness:
    clock = Clock()
    runtime_dir = tmp_path / "runtime"
    store = RuntimeStatusStore(runtime_dir, now=clock)
    dialog = FakeDialog()
    runner = FakeRunner()
    locks: list[FakeLock] = []

    def lock_factory() -> FakeLock:
        lock = FakeLock(lock_result)
        locks.append(lock)
        return lock

    env = dict(base_env or {})
    controller = ControlPanelController(
        status_store=store,
        runner=runner,
        build_request=lambda mode, iid: build_launch_request(
            mode,
            python_exe="C:\\venv\\python.exe",
            repo_root=tmp_path,
            runtime_dir=runtime_dir,
            base_env=env,
            instance_id=iid,
        ),
        lock_factory=lock_factory,
        dialog=dialog,
        assess=assess or (lambda live: ConfigReadiness(True, ())),
        now=clock,
        pid_alive=pid_alive,
        new_instance_id=lambda: "gui-instance-1",
        stale_after=DEFAULT_STALE_AFTER,
        graceful_timeout=graceful_timeout,
        terminate_timeout=terminate_timeout,
        notify_message=dialog.show_message,
    )
    return Harness(clock, store, dialog, runner, locks, controller, runtime_dir)


# -- status display -----------------------------------------------------

def test_missing_status_file_shows_offline(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    state = h.controller.display_state()
    assert state.status == "OFFLINE"
    assert state.can_start is True
    assert state.can_stop is False


def test_malformed_status_file_shows_safe_error(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.store.path.parent.mkdir(parents=True, exist_ok=True)
    h.store.path.write_text("{bad json", encoding="utf-8")
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == "ERROR"
    assert "status file" in state.error


def test_fresh_online_heartbeat_shows_online_synthetic(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    h.store.record_online()
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == "ONLINE — SYNTHETIC"
    assert state.mode == "synthetic"
    assert state.stale is False


def test_fresh_online_heartbeat_shows_online_live_with_model(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.store.record_starting(
        mode="live", model="deepseek-v4-flash", instance_id="pid-1", pid=1
    )
    h.store.record_online()
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == "ONLINE — LIVE"
    assert state.mode == "live"
    assert state.model == "deepseek-v4-flash"


def test_stale_heartbeat_shows_offline(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    h.store.record_online()
    h.clock.advance(DEFAULT_STALE_AFTER.total_seconds() + 1)
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == "OFFLINE"
    assert state.stale is True


def test_dead_pid_shows_offline(tmp_path: Path) -> None:
    h = make_harness(tmp_path, pid_alive=lambda pid: False)
    h.store.record_starting(mode="synthetic", instance_id="pid-1", pid=999)
    h.store.record_online()
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == "OFFLINE"


@pytest.mark.parametrize(
    ("record", "expected_status"),
    [
        ("starting", "STARTING"),
        ("reconnecting", "RECONNECTING"),
        ("stopping", "STOPPING"),
        ("offline", "OFFLINE"),
        ("error", "ERROR"),
    ],
)
def test_transitional_states_render(tmp_path: Path, record: str, expected_status: str) -> None:
    h = make_harness(tmp_path)
    if record == "starting":
        h.store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    elif record == "reconnecting":
        h.store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
        h.store.record_reconnecting()
    elif record == "stopping":
        h.store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
        h.store.record_stopping()
    elif record == "offline":
        h.store.record_offline()
    else:
        h.store.record_error("LoginFailure")
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == expected_status


# -- launch commands and environment ------------------------------------

def test_synthetic_launch_command_and_environment() -> None:
    base_env = {"PATH": "C:\\Windows", "DEEPSEEK_API_KEY": "sk-secret", "NEMOIR_ALLOW_LIVE_DEEPSEEK": "true"}
    request = build_launch_request(
        "synthetic",
        python_exe="C:\\venv\\python.exe",
        repo_root=Path("C:\\repo"),
        runtime_dir=Path("C:\\repo\\data\\runtime"),
        base_env=base_env,
        instance_id="gui-1",
    )
    assert request.argv == ("C:\\venv\\python.exe", "-m", "nemoir", "discord-pilot", "--confirm-live")
    assert request.mode == "synthetic"
    assert request.instance_id == "gui-1"
    assert request.env["NEMOIR_INSTANCE_ID"] == "gui-1"
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in request.env
    assert "DEEPSEEK_API_KEY" not in request.env
    # base environment was not mutated
    assert base_env["DEEPSEEK_API_KEY"] == "sk-secret"
    assert base_env["NEMOIR_ALLOW_LIVE_DEEPSEEK"] == "true"


def test_live_launch_command_and_child_only_gate() -> None:
    base_env = {"PATH": "C:\\Windows"}
    request = build_launch_request(
        "live",
        python_exe="C:\\venv\\python.exe",
        repo_root=Path("C:\\repo"),
        runtime_dir=Path("C:\\repo\\data\\runtime"),
        base_env=base_env,
        instance_id="gui-1",
    )
    assert request.argv == ("C:\\venv\\python.exe", "-m", "nemoir", "discord", "--confirm-live")
    assert request.env["NEMOIR_ALLOW_LIVE_DEEPSEEK"] == "true"
    # the gate is set only in the child env, never the caller's environment
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in base_env


def test_start_synthetic_launches_child(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_synthetic()
    assert len(h.runner.launches) == 1
    request = h.runner.launches[0]
    assert request.mode == "synthetic"
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in request.env
    assert h.controller.display_state().can_stop is True


def test_start_live_confirmation_and_launch(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_live()
    assert h.dialog.live_confirmations == 1
    assert len(h.runner.launches) == 1
    request = h.runner.launches[0]
    assert request.mode == "live"
    assert request.env["NEMOIR_ALLOW_LIVE_DEEPSEEK"] == "true"


def test_live_confirmation_cancellation_launches_nothing(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.dialog.confirm_live_result = False
    h.controller.start_live()
    assert h.dialog.live_confirmations == 1
    assert h.runner.launches == []
    assert h.controller.display_state().can_stop is False


def test_configuration_not_ready_blocks_live(tmp_path: Path) -> None:
    h = make_harness(
        tmp_path,
        assess=lambda live: ConfigReadiness(False, ("DeepSeek API key",)),
    )
    h.controller.start_live()
    assert h.runner.launches == []
    assert any("DeepSeek API key" in message for message in h.dialog.messages)


def test_start_channel_test_launches_child_with_channel_write_gate(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_channel_test()
    assert h.dialog.channel_test_confirmations == 1
    assert len(h.runner.launches) == 1
    request = h.runner.launches[0]
    assert request.mode == "channel-test"
    assert request.env["NEMOIR_ALLOW_CHANNEL_WRITE"] == "true"
    assert "NEMOIR_ALLOW_LIVE_DEEPSEEK" not in request.env
    assert "DEEPSEEK_API_KEY" not in request.env


def test_channel_test_confirmation_cancellation_launches_nothing(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.dialog.confirm_channel_test_result = False
    h.controller.start_channel_test()
    assert h.dialog.channel_test_confirmations == 1
    assert h.runner.launches == []
    assert h.controller.display_state().can_stop is False


def test_channel_test_status_identifies_channel_test_mode(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.store.record_starting(mode="channel-test", instance_id="pid-1", pid=1)
    h.store.record_online()
    h.controller.tick()
    state = h.controller.display_state()
    assert state.status == "ONLINE — CHANNEL TEST"
    assert state.mode == "channel-test"


def test_configuration_not_ready_blocks_synthetic(tmp_path: Path) -> None:
    h = make_harness(
        tmp_path,
        assess=lambda live: ConfigReadiness(False, ("Discord bot token",)),
    )
    h.controller.start_synthetic()
    assert h.runner.launches == []
    assert any("Discord bot token" in message for message in h.dialog.messages)


# -- duplicate-instance prevention --------------------------------------

def test_start_refuses_when_lock_is_held_by_another_process(tmp_path: Path) -> None:
    h = make_harness(tmp_path, lock_result=False)
    h.controller.start_synthetic()
    assert h.runner.launches == []
    assert any("already running" in message for message in h.dialog.messages)


def test_gui_refuses_duplicate_cli_started_bot(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    # A CLI bot wrote a fresh ONLINE heartbeat with a live PID.
    h.store.record_starting(mode="synthetic", instance_id="pid-cli", pid=777)
    h.store.record_online()
    h.controller.start_synthetic()
    assert h.runner.launches == []
    assert any("already running" in message for message in h.dialog.messages)


def test_start_refuses_when_own_child_already_running(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_synthetic()
    h.controller.start_synthetic()
    assert len(h.runner.launches) == 1
    assert any("already running" in message for message in h.dialog.messages)


# -- stopping ------------------------------------------------------------

def test_stop_does_nothing_without_owned_child(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.stop_bot()
    assert h.runner.children == []
    assert h.controller.display_state().can_stop is False


def test_stop_requests_graceful_shutdown_of_owned_child_only(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_synthetic()
    h.controller.stop_bot()
    # Graceful path: an instance-scoped request is written; no terminate yet.
    assert ShutdownRequest(h.runtime_dir, "gui-instance-1").is_requested() is True
    assert ShutdownRequest(h.runtime_dir, "pid-cli").is_requested() is False
    assert h.runner.children[0].terminated is False


def test_stop_waits_for_offline_status(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_synthetic()
    h.controller.stop_bot()
    # The child cooperates: it writes STOPPING then OFFLINE and exits.
    h.store.record_stopping()
    h.store.record_offline()
    h.controller.tick()
    assert h.runner.children[0].terminated is False
    assert h.controller.display_state().can_stop is False


def test_stop_uses_bounded_fallback_termination(tmp_path: Path) -> None:
    h = make_harness(tmp_path, graceful_timeout=timedelta(seconds=10), terminate_timeout=timedelta(seconds=4))
    h.runner.next_child = FakeChild(pid=55, die_on_terminate=False)
    h.controller.start_synthetic()
    # The child wrote STARTING but then stops cooperating (no OFFLINE, no exit).
    h.store.record_starting(mode="synthetic", instance_id="gui-instance-1", pid=55)
    h.controller.stop_bot()

    # Still within the graceful window: no terminate.
    h.clock.advance(9)
    h.controller.tick()
    assert h.runner.children[0].terminated is False

    # Past the graceful deadline: bounded terminate fires.
    h.clock.advance(2)
    h.controller.tick()
    assert h.runner.children[0].terminated is True
    assert h.runner.children[0].killed is False

    # The child still has not exited; past the terminate deadline: kill.
    h.clock.advance(5)
    h.controller.tick()
    assert h.runner.children[0].killed is True


# -- closing -------------------------------------------------------------

def test_close_without_child_is_immediate(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.begin_close()
    assert h.controller.ready_to_close is True


def test_close_with_running_child_asks_and_defers(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.controller.start_synthetic()
    h.controller.begin_close()
    assert h.dialog.close_confirmations == 1
    assert h.controller.ready_to_close is False  # child still running

    # The child cooperates and stops; the panel becomes ready to close.
    h.store.record_stopping()
    h.store.record_offline()
    h.controller.tick()
    assert h.controller.ready_to_close is True


def test_close_with_running_child_can_be_cancelled(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.dialog.confirm_close_result = False
    h.controller.start_synthetic()
    h.controller.begin_close()
    assert h.controller.ready_to_close is False
    assert h.runner.children[0].terminated is False


# -- secrets -------------------------------------------------------------

def test_display_state_never_contains_secrets(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    # Model a live, online bot whose environment carried real secrets.
    h.store.record_starting(
        mode="live", model="deepseek-v4-flash", instance_id="pid-1", pid=1
    )
    h.store.record_online()
    h.controller.tick()
    state = h.controller.display_state()
    rendered = " ".join(
        [state.status, state.mode, state.model, state.uptime, state.last_heartbeat, state.error]
    )
    assert "sk-" not in rendered
    assert "token" not in rendered.lower()
    # Only the safe configured model name is shown, never a key.
    assert state.model == "deepseek-v4-flash"


def test_error_display_never_contains_raw_exception_text(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    # The status boundary stores only a safe class name, never exception text.
    h.store.record_error("LoginFailure")
    h.controller.tick()
    state = h.controller.display_state()
    assert "LoginFailure" in state.error
    assert "password" not in state.error
