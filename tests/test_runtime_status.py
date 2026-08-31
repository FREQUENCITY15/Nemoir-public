"""Tests for the atomic runtime-status store, instance lock, and shutdown gate."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nemoir.runtime import (
    DEFAULT_STALE_AFTER,
    InstanceLock,
    LockBusyError,
    RuntimeStatusStore,
    ShutdownRequest,
    StatusSnapshot,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_missing_status_file_is_offline(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    snapshot = store.read()
    assert snapshot.state == "OFFLINE"
    assert snapshot.error_class is None


def test_malformed_status_file_is_safe_error(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{this is not json", encoding="utf-8")
    snapshot = store.read()
    assert snapshot.state == "ERROR"
    assert snapshot.error_class == "MALFORMED_STATUS"


def test_non_object_status_file_is_safe_error(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('["not", "a", "dict"]', encoding="utf-8")
    snapshot = store.read()
    assert snapshot.state == "ERROR"
    assert snapshot.error_class == "MALFORMED_STATUS"


def test_legacy_status_file_with_unknown_state_is_safe_error(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"state": "RUNNING", "pid": "99"}), encoding="utf-8")
    snapshot = store.read()
    assert snapshot.state == "ERROR"
    assert snapshot.error_class == "UNKNOWN_STATE"


def test_legacy_status_file_ignores_unknown_extra_keys(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"state": "ONLINE", "mode": "live", "model": "m", "obsolete_field": 1}),
        encoding="utf-8",
    )
    snapshot = store.read()
    assert snapshot.state == "ONLINE"
    assert snapshot.mode == "live"
    assert snapshot.model == "m"


def test_starting_online_heartbeat_lifecycle(tmp_path: Path) -> None:
    clock = Clock()
    store = RuntimeStatusStore(tmp_path / "runtime", now=clock)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    starting = store.read()
    assert starting.state == "STARTING"
    assert starting.mode == "synthetic"
    assert starting.started_at is not None

    clock.advance(2)
    online = store.record_online()
    assert online.state == "ONLINE"
    assert online.mode == "synthetic"  # preserved from STARTING
    assert online.last_heartbeat_at == clock.now

    clock.advance(5)
    heartbeat = store.record_heartbeat()
    assert heartbeat.state == "ONLINE"
    assert heartbeat.last_heartbeat_at == clock.now
    assert heartbeat.started_at == starting.started_at


def test_fresh_online_heartbeat_is_not_stale(tmp_path: Path) -> None:
    clock = Clock()
    store = RuntimeStatusStore(tmp_path / "runtime", now=clock)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    store.record_online()
    clock.advance(DEFAULT_STALE_AFTER.total_seconds() - 1)
    assert store.is_stale(store.read()) is False


def test_stale_heartbeat_is_stale(tmp_path: Path) -> None:
    clock = Clock()
    store = RuntimeStatusStore(tmp_path / "runtime", now=clock)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    store.record_online()
    clock.advance(DEFAULT_STALE_AFTER.total_seconds() + 1)
    assert store.is_stale(store.read()) is True


def test_reconnecting_stopping_offline_error_states(tmp_path: Path) -> None:
    clock = Clock()
    store = RuntimeStatusStore(tmp_path / "runtime", now=clock)
    store.record_starting(mode="live", model="deepseek-v4-flash", instance_id="pid-2", pid=2)
    assert store.record_reconnecting().state == "RECONNECTING"
    assert store.record_stopping().state == "STOPPING"
    offline = store.record_offline()
    assert offline.state == "OFFLINE"
    assert offline.mode == "live"
    assert offline.last_heartbeat_at is None
    error = store.record_error("LoginFailure")
    assert error.state == "ERROR"
    assert error.error_class == "LoginFailure"


def test_offline_and_error_are_never_reclassified_as_stale(tmp_path: Path) -> None:
    clock = Clock()
    store = RuntimeStatusStore(tmp_path / "runtime", now=clock)
    store.record_offline()
    clock.advance(10_000)
    assert store.is_stale(store.read()) is False

    store.record_error("SomeError")
    assert store.is_stale(store.read()) is False


def test_starting_without_heartbeat_becomes_stale(tmp_path: Path) -> None:
    clock = Clock()
    store = RuntimeStatusStore(tmp_path / "runtime", now=clock)
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    clock.advance(DEFAULT_STALE_AFTER.total_seconds() + 1)
    assert store.is_stale(store.read()) is True


def test_synthetic_and_live_mode_are_recorded(tmp_path: Path) -> None:
    store = RuntimeStatusStore(tmp_path / "runtime")
    store.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    assert store.read().mode == "synthetic"
    store.record_starting(mode="live", model="deepseek-v4-flash", instance_id="pid-2", pid=2)
    snapshot = store.read()
    assert snapshot.mode == "live"
    assert snapshot.model == "deepseek-v4-flash"


def test_status_snapshot_only_contains_safe_operational_fields() -> None:
    snapshot = StatusSnapshot(
        state="ONLINE",
        mode="live",
        model="deepseek-v4-flash",
        instance_id="pid-1",
        pid=1,
        error_class="SomethingSafe",
    )
    payload = snapshot.to_dict()
    assert set(payload) == {
        "schema_version",
        "state",
        "mode",
        "model",
        "instance_id",
        "pid",
        "started_at",
        "updated_at",
        "last_heartbeat_at",
        "error_class",
    }
    encoded = json.dumps(payload)
    for secret in ("sk-", "token", "DISCORD_BOT_TOKEN", "DEEPSEEK_API_KEY", "Bearer"):
        assert secret.lower() not in encoded.lower()


def test_status_survives_store_restart(tmp_path: Path) -> None:
    first = RuntimeStatusStore(tmp_path / "runtime")
    first.record_starting(mode="synthetic", instance_id="pid-1", pid=1)
    second = RuntimeStatusStore(tmp_path / "runtime")
    assert second.read().state == "STARTING"
    assert second.read().instance_id == "pid-1"


def test_duplicate_instance_lock_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    first = InstanceLock(path)
    assert first.acquire() is True
    second = InstanceLock(path)
    assert second.acquire() is False
    first.release()

    third = InstanceLock(path)
    assert third.acquire() is True
    third.release()


def test_instance_lock_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    with InstanceLock(path):
        assert InstanceLock(path).acquire() is False
    assert InstanceLock(path).acquire() is True


def test_lock_busy_error_from_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    first = InstanceLock(path)
    first.acquire()
    with pytest.raises(LockBusyError):
        with InstanceLock(path):
            pass
    first.release()


def test_lock_is_os_released_when_holder_process_dies(tmp_path: Path) -> None:
    import subprocess
    import sys
    import time

    lock_path = tmp_path / "bot.lock"
    ready_path = tmp_path / "ready"
    code = (
        "import sys, time, pathlib\n"
        "from nemoir.runtime import InstanceLock\n"
        "lock = InstanceLock(sys.argv[1])\n"
        "assert lock.acquire()\n"
        "pathlib.Path(sys.argv[2]).write_text('ok')\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path), str(ready_path)]
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not ready_path.exists():
            if proc.poll() is not None:
                raise AssertionError("child exited before acquiring the lock")
            time.sleep(0.02)
        assert ready_path.exists(), "child never acquired the lock"

        # While the child lives the lock is held...
        assert InstanceLock(lock_path).acquire() is False

        # ...and once the process dies the operating system releases it. The
        # release is asynchronous after the process handle is closed, so poll
        # with a bounded deadline rather than asserting immediately; the final
        # assertion is unchanged and still requires the lock to be acquirable.
        proc.kill()
        proc.wait(timeout=10)
        deadline = time.time() + 5
        acquired = False
        while time.time() < deadline:
            lock = InstanceLock(lock_path)
            acquired = lock.acquire()
            if acquired:
                lock.release()
                break
            time.sleep(0.05)
        assert acquired is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_shutdown_request_is_scoped_to_its_instance(tmp_path: Path) -> None:
    directory = tmp_path / "runtime"
    request_a = ShutdownRequest(directory, "gui-abc123")
    request_b = ShutdownRequest(directory, "gui-def456")
    assert request_a.is_requested() is False
    request_a.request()
    assert request_a.is_requested() is True
    assert request_b.is_requested() is False
    request_a.clear()
    assert request_a.is_requested() is False


def test_shutdown_request_clear_ignores_missing_file(tmp_path: Path) -> None:
    request = ShutdownRequest(tmp_path / "runtime", "gui-xyz")
    request.clear()  # must not raise
