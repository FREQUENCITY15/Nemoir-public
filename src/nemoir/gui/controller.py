"""Display-free controller for the Nemoir Control Panel.

Every external dependency — subprocess launching, the single-instance lock,
confirmation dialogs, configuration readiness, time, and PID liveness — is
injected so the controller can be tested without Tkinter, Discord, a display,
or any live connection. The Tkinter layer is only a thin view over this class.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from nemoir.config import Settings
from nemoir.runtime import (
    DEFAULT_STALE_AFTER,
    InstanceLock,
    RuntimeStatusStore,
    ShutdownRequest,
    StatusSnapshot,
)

# Discord's READY signal is the only "online" truth; these are the closed
# status strings the panel renders.
_STATUS_TEXT = {
    "OFFLINE": "OFFLINE",
    "STARTING": "STARTING",
    "RECONNECTING": "RECONNECTING",
    "STOPPING": "STOPPING",
    "ERROR": "ERROR",
}

_ERROR_DESCRIPTIONS = {
    "MALFORMED_STATUS": "The status file could not be read.",
    "UNREADABLE": "The status file could not be read.",
    "UNKNOWN_STATE": "The status file held an unrecognised state.",
}

_DISCORD_SCOPE_KEYS = (
    ("DISCORD_BOT_TOKEN", "Discord bot token"),
    ("NEMOIR_GUILD_ID", "development guild"),
    ("NEMOIR_INTAKE_CHANNEL_ID", "intake channel"),
    ("NEMOIR_ANEMONE_CATEGORY_ID", "anemone category"),
    ("NEMOIR_NURSERY_CHANNEL_ID", "nursery channel"),
)


def pid_is_alive(pid: int) -> bool:
    """Return True when an OS process with the given PID exists."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process_query_limited = 0x1000
            handle = kernel32.OpenProcess(process_query_limited, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            # A PID that cannot be inspected is treated as not alive so the
            # panel falls back to the (truthful) offline display.
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class ConfigReadiness:
    ready: bool
    missing: tuple[str, ...]


def assess_readiness(settings: Settings, *, live: bool) -> ConfigReadiness:
    """Check configuration presence without ever touching secret values."""
    missing: list[str] = []
    for key, label in _DISCORD_SCOPE_KEYS:
        value = getattr(settings, _setting_attr(key))
        if not value:
            missing.append(label)
    if live and not settings.deepseek_api_key:
        missing.append("DeepSeek API key")
    return ConfigReadiness(ready=not missing, missing=tuple(missing))


def _setting_attr(env_key: str) -> str:
    return {
        "DISCORD_BOT_TOKEN": "discord_bot_token",
        "NEMOIR_GUILD_ID": "guild_id",
        "NEMOIR_INTAKE_CHANNEL_ID": "intake_channel_id",
        "NEMOIR_ANEMONE_CATEGORY_ID": "anemone_category_id",
        "NEMOIR_NURSERY_CHANNEL_ID": "nursery_channel_id",
    }[env_key]


@dataclass(frozen=True)
class LaunchRequest:
    """A fully-resolved child launch, ready for a process runner."""

    mode: str
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    instance_id: str
    no_window: bool


def build_launch_request(
    mode: str,
    *,
    python_exe: str,
    repo_root: str | Path,
    runtime_dir: str | Path,
    base_env: Mapping[str, str],
    instance_id: str,
) -> LaunchRequest:
    """Build a child launch command and environment for one mode.

    ``base_env`` is copied, never mutated, so the user's environment is not
    persistently changed. Synthetic mode strips the DeepSeek key and the live
    gate; live mode sets ``NEMOIR_ALLOW_LIVE_DEEPSEEK=true`` only in the child
    environment.
    """
    if mode not in (
        "synthetic",
        "live",
        "channel-test",
        "autonomous-test",
        "autonomous-live",
    ):
        raise ValueError(f"Unknown launch mode: {mode}")
    root = Path(repo_root)
    env = dict(base_env)
    env["NEMOIR_RUNTIME_DIR"] = str(Path(runtime_dir).resolve())
    env["NEMOIR_INSTANCE_ID"] = instance_id

    if mode == "synthetic":
        module = "discord-pilot"
        # Never enable DeepSeek for the synthetic pilot, even if the parent
        # environment happens to carry a key or the live gate.
        env.pop("NEMOIR_ALLOW_LIVE_DEEPSEEK", None)
        env.pop("DEEPSEEK_API_KEY", None)
    elif mode == "channel-test":
        module = "discord-channel-test"
        # Channel-test mode uses the deterministic synthetic provider and never
        # reads DeepSeek configuration, but it may create real Discord channels,
        # so the channel-write gate is enabled only for this child process.
        env.pop("NEMOIR_ALLOW_LIVE_DEEPSEEK", None)
        env.pop("DEEPSEEK_API_KEY", None)
        env["NEMOIR_ALLOW_CHANNEL_WRITE"] = "true"
    elif mode == "autonomous-test":
        module = "discord-autonomous-test"
        # Autonomous Test uses the deterministic synthetic sort (never DeepSeek)
        # but publishes into REAL shared channels, so only the channel-write
        # gate is enabled for this child process.
        env.pop("NEMOIR_ALLOW_LIVE_DEEPSEEK", None)
        env.pop("DEEPSEEK_API_KEY", None)
        env["NEMOIR_ALLOW_CHANNEL_WRITE"] = "true"
    elif mode == "autonomous-live":
        module = "discord-autonomous-live"
        # Autonomous Live makes paid DeepSeek sort calls per sealed capture AND
        # creates real shared channels; both gates exist only in the child.
        env["NEMOIR_ALLOW_LIVE_DEEPSEEK"] = "true"
        env["NEMOIR_ALLOW_CHANNEL_WRITE"] = "true"
    else:
        module = "discord"
        env["NEMOIR_ALLOW_LIVE_DEEPSEEK"] = "true"

    argv = (python_exe, "-m", "nemoir", module, "--confirm-live")
    return LaunchRequest(
        mode=mode,
        argv=argv,
        env=env,
        cwd=str(root),
        instance_id=instance_id,
        no_window=True,
    )


class ChildProcess:
    """Minimal subprocess handle surface used by the controller.

    ``subprocess.Popen`` satisfies this interface directly; tests supply a
    fake with the same four methods.
    """

    pid: int
    poll: Callable[[], int | None]
    terminate: Callable[[], None]
    kill: Callable[[], None]
    wait: Callable[..., int]


@dataclass(frozen=True)
class DisplayState:
    status: str = "OFFLINE"
    mode: str = ""
    model: str = ""
    uptime: str = ""
    last_heartbeat: str = ""
    error: str = ""
    stale: bool = False
    can_start: bool = True
    can_stop: bool = False


def _format_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_ago(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds <= 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago"


def _describe_error(error_class: str | None) -> str:
    if not error_class:
        return ""
    if error_class in _ERROR_DESCRIPTIONS:
        return _ERROR_DESCRIPTIONS[error_class]
    if error_class.startswith("DISCORD_EVENT:"):
        return "A Discord event reported an error."
    return f"The bot reported an error ({error_class})."


def _display_status(snapshot: StatusSnapshot) -> str:
    if snapshot.state == "ONLINE":
        if snapshot.mode == "synthetic":
            return "ONLINE — SYNTHETIC"
        if snapshot.mode == "channel-test":
            return "ONLINE — CHANNEL TEST"
        if snapshot.mode == "autonomous-test":
            return "ONLINE — AUTONOMOUS TEST"
        if snapshot.mode == "autonomous-live":
            return "ONLINE — AUTONOMOUS LIVE"
        if snapshot.mode == "live":
            return "ONLINE — LIVE"
        return "ONLINE"
    return _STATUS_TEXT.get(snapshot.state, snapshot.state)


class ControlPanelController:
    def __init__(
        self,
        *,
        status_store: RuntimeStatusStore,
        runner: Callable[[LaunchRequest], Any],
        build_request: Callable[[str, str], LaunchRequest],
        lock_factory: Callable[[], InstanceLock],
        dialog: Any,
        assess: Callable[[bool], ConfigReadiness],
        now: Callable[[], datetime],
        pid_alive: Callable[[int], bool] = pid_is_alive,
        new_instance_id: Callable[[], str] | None = None,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        graceful_timeout: timedelta = timedelta(seconds=15.0),
        terminate_timeout: timedelta = timedelta(seconds=5.0),
        notify: Callable[[DisplayState], None] | None = None,
        notify_message: Callable[[str], None] | None = None,
    ) -> None:
        self._status_store = status_store
        self._runner = runner
        self._build_request = build_request
        self._lock_factory = lock_factory
        self._dialog = dialog
        self._assess = assess
        self._now = now
        self._pid_alive = pid_alive
        self._new_instance_id = new_instance_id or (lambda: f"gui-{uuid4().hex[:12]}")
        self._stale_after = stale_after
        self._graceful_timeout = graceful_timeout
        self._terminate_timeout = terminate_timeout
        self._notify = notify
        self._notify_message = notify_message

        self._child: Any | None = None
        self._instance_id: str | None = None
        self._mode: str | None = None
        self._stop_requested = False
        self._stop_deadline: datetime | None = None
        self._terminate_deadline: datetime | None = None
        self._close_after_stop = False
        self._last_display = self._compute_display()

    # -- actions ----------------------------------------------------------

    def start_synthetic(self) -> None:
        readiness = self._assess(False)
        if not readiness.ready:
            self._message(
                "Discord is not fully configured yet. Missing: "
                + ", ".join(readiness.missing)
                + "."
            )
            return
        self._start("synthetic")

    def start_channel_test(self) -> None:
        if not self._dialog.confirm_channel_test():
            return
        readiness = self._assess(False)
        if not readiness.ready:
            self._message(
                "Discord is not fully configured yet. Missing: "
                + ", ".join(readiness.missing)
                + "."
            )
            return
        self._start("channel-test")

    def start_autonomous_test(self) -> None:
        if not self._dialog.confirm_autonomous_test():
            return
        readiness = self._assess(False)
        if not readiness.ready:
            self._message(
                "Discord is not fully configured yet. Missing: "
                + ", ".join(readiness.missing)
                + "."
            )
            return
        self._start("autonomous-test")

    def start_autonomous_live(self) -> None:
        if not self._dialog.confirm_autonomous_live():
            return
        readiness = self._assess(True)
        if not readiness.ready:
            self._message(
                "Autonomous Live mode is not ready yet. Missing: "
                + ", ".join(readiness.missing)
                + "."
            )
            return
        self._start("autonomous-live")

    def start_live(self) -> None:
        if not self._dialog.confirm_live():
            return
        readiness = self._assess(True)
        if not readiness.ready:
            self._message(
                "Live mode is not ready yet. Missing: "
                + ", ".join(readiness.missing)
                + "."
            )
            return
        self._start("live")

    def stop_bot(self) -> None:
        if self._child is None or self._child.poll() is not None:
            return
        self._begin_stop()

    def begin_close(self) -> None:
        if self._child is not None and self._child.poll() is None:
            if self._dialog.confirm_close():
                self._begin_stop()
                self._close_after_stop = True
            # Otherwise the user cancelled the close; leave the panel open.
        else:
            self._close_after_stop = True

    # -- polling ----------------------------------------------------------

    def tick(self) -> None:
        self._reconcile_child()
        self._drive_stop()
        self._emit()

    def display_state(self) -> DisplayState:
        return self._last_display

    @property
    def ready_to_close(self) -> bool:
        if not self._close_after_stop:
            return False
        if self._child is None:
            return True
        return self._child.poll() is not None

    # -- internals --------------------------------------------------------

    def _start(self, mode: str) -> None:
        if self._child is not None and self._child.poll() is None:
            self._message("A bot is already running.")
            return
        if self._external_bot_live():
            self._message(
                "Another bot is already running (for example from PowerShell). "
                "Stop it before starting a new one."
            )
            return
        instance_id = self._new_instance_id()
        request = self._build_request(mode, instance_id)
        child = self._runner(request)
        self._child = child
        self._instance_id = instance_id
        self._mode = mode
        self._stop_requested = False
        self._stop_deadline = None
        self._terminate_deadline = None
        self._close_after_stop = False
        self._emit()

    def _external_bot_live(self) -> bool:
        snapshot = self._status_store.read()
        if snapshot.state not in ("OFFLINE", "ERROR"):
            if not self._status_store.is_stale(
                snapshot, stale_after=self._stale_after, now=self._now()
            ):
                if snapshot.pid is None or self._pid_alive(snapshot.pid):
                    return True
        lock = self._lock_factory()
        acquired = lock.acquire()
        if acquired:
            lock.release()
            return False
        return True

    def _begin_stop(self) -> None:
        if self._child is None or self._child.poll() is not None:
            return
        ShutdownRequest(self._status_store.directory, self._instance_id or "").request()
        self._stop_requested = True
        self._stop_deadline = self._now() + self._graceful_timeout
        self._terminate_deadline = None

    def _reconcile_child(self) -> None:
        if self._child is not None and self._child.poll() is not None:
            self._clear_child()

    def _drive_stop(self) -> None:
        if not self._stop_requested or self._child is None:
            return
        child = self._child
        if child.poll() is not None:
            self._finish_stop()
            return
        snapshot = self._status_store.read()
        if snapshot.state == "OFFLINE":
            self._finish_stop()
            return
        now = self._now()
        if now >= (self._stop_deadline or now):
            if self._terminate_deadline is None:
                child.terminate()
                self._terminate_deadline = now + self._terminate_timeout
            elif now >= self._terminate_deadline:
                child.kill()
                self._finish_stop()

    def _finish_stop(self) -> None:
        self._clear_child()

    def _clear_child(self) -> None:
        self._child = None
        self._instance_id = None
        self._mode = None
        self._stop_requested = False
        self._stop_deadline = None
        self._terminate_deadline = None

    def _snapshot_is_stale_or_dead(self, snapshot: StatusSnapshot) -> bool:
        if snapshot.state in ("OFFLINE", "ERROR"):
            return False
        if self._status_store.is_stale(
            snapshot, stale_after=self._stale_after, now=self._now()
        ):
            return True
        if snapshot.pid is not None and not self._pid_alive(snapshot.pid):
            return True
        return False

    def _compute_display(self) -> DisplayState:
        snapshot = self._status_store.read()
        now = self._now()
        own_running = self._child is not None and self._child.poll() is None

        if snapshot.state == "ERROR":
            status = "ERROR"
            error = _describe_error(snapshot.error_class)
            stale = False
        elif self._snapshot_is_stale_or_dead(snapshot):
            status = "OFFLINE"
            error = ""
            stale = True
        else:
            status = _display_status(snapshot)
            error = ""
            stale = False

        mode = snapshot.mode if snapshot.state not in ("OFFLINE", "ERROR") and not stale else ""
        model = snapshot.model if snapshot.state == "ONLINE" and snapshot.mode == "live" and not stale else ""

        uptime = ""
        if snapshot.started_at is not None and not stale and snapshot.state not in ("OFFLINE", "ERROR"):
            uptime = _format_duration(now - snapshot.started_at)

        heartbeat = ""
        if snapshot.last_heartbeat_at is not None and not stale and snapshot.state not in ("OFFLINE", "ERROR"):
            heartbeat = _format_ago(snapshot.last_heartbeat_at, now)

        external_live = snapshot.state not in ("OFFLINE", "ERROR") and not self._snapshot_is_stale_or_dead(snapshot)
        can_start = not own_running and not external_live
        can_stop = own_running

        return DisplayState(
            status=status,
            mode=mode,
            model=model,
            uptime=uptime,
            last_heartbeat=heartbeat,
            error=error,
            stale=stale,
            can_start=can_start,
            can_stop=can_stop,
        )

    def _emit(self) -> None:
        self._last_display = self._compute_display()
        if self._notify is not None:
            self._notify(self._last_display)

    def _message(self, text: str) -> None:
        if self._notify_message is not None:
            self._notify_message(text)


def make_default_launch_planner(
    *,
    python_exe: str | None = None,
    repo_root: str | Path | None = None,
    runtime_dir: str | Path,
    base_env: Mapping[str, str] | None = None,
) -> Callable[[str, str], LaunchRequest]:
    """Build the production launch planner bound to the current interpreter."""
    exe = python_exe or sys.executable
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
    env = dict(base_env) if base_env is not None else dict(os.environ)

    def planner(mode: str, instance_id: str) -> LaunchRequest:
        return build_launch_request(
            mode,
            python_exe=exe,
            repo_root=root,
            runtime_dir=runtime_dir,
            base_env=env,
            instance_id=instance_id,
        )

    return planner


class ProcessRunner:
    """Real subprocess launcher; hides the child console on Windows."""

    def __init__(self) -> None:
        self._launches: list[subprocess.Popen[Any]] = []

    def __call__(self, request: LaunchRequest) -> subprocess.Popen[Any]:
        kwargs: dict[str, Any] = {"cwd": request.cwd, "env": request.env}
        if request.no_window and os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(list(request.argv), **kwargs)
        self._launches.append(process)
        return process
