"""Atomic runtime-status boundary shared by CLI bots and the control panel.

The status file stores only safe operational data: the closed ``state``
vocabulary, the mode (synthetic/live), the configured live model name, an
instance identifier and OS PID, timestamps, and a safe error *class* name.
It never stores Discord tokens, DeepSeek keys, prompts, responses, or raw
exception text.

Writes are atomic (temp file + ``os.replace``) so a GUI or bot restart can
never observe a half-written file, and reads tolerate absent, malformed,
stale, and legacy files without raising.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

STATES: tuple[str, ...] = (
    "OFFLINE",
    "STARTING",
    "ONLINE",
    "RECONNECTING",
    "STOPPING",
    "ERROR",
)
MODES: tuple[str, ...] = (
    "synthetic",
    "live",
    "channel-test",
    "autonomous-test",
    "autonomous-live",
)

OFFLINE = "OFFLINE"
ONLINE = "ONLINE"
ERROR = "ERROR"

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
DEFAULT_STALE_AFTER = timedelta(seconds=30.0)

_SCHEMA_VERSION = 1

_TERMINAL_OR_OFFLINE = frozenset({"OFFLINE", "ERROR"})


def utc_now() -> datetime:
    """Time source; replaced with a fake in tests."""
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_aware(parsed)


def _parse_pid(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


@dataclass(frozen=True)
class StatusSnapshot:
    """One immutable, safe snapshot of the bot's runtime status."""

    state: str = OFFLINE
    mode: str | None = None
    model: str | None = None
    instance_id: str | None = None
    pid: int | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    error_class: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "state": self.state,
            "mode": self.mode,
            "model": self.model,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
            ),
            "error_class": self.error_class,
        }


def _parse_snapshot(raw: str) -> StatusSnapshot:
    """Parse a status file body into a snapshot, never raising.

    Malformed, non-object, or unknown-state files degrade to a safe ``ERROR``
    snapshot with a non-leaking ``error_class``. Unknown extra keys are
    ignored so a newer writer does not break an older reader.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return StatusSnapshot(state=ERROR, error_class="MALFORMED_STATUS")
    if not isinstance(data, dict):
        return StatusSnapshot(state=ERROR, error_class="MALFORMED_STATUS")

    raw_state = data.get("state")
    if raw_state in STATES:
        state = raw_state
        error_class = None
    elif isinstance(raw_state, str) and raw_state:
        state = ERROR
        error_class = "UNKNOWN_STATE"
    else:
        state = ERROR
        error_class = "MALFORMED_STATUS"

    mode = data.get("mode")
    if mode not in MODES:
        mode = None

    model = data.get("model")
    if not isinstance(model, str) or not model:
        model = None

    instance_id = data.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        instance_id = None

    stored_error = data.get("error_class")
    if error_class is None and isinstance(stored_error, str) and stored_error:
        error_class = stored_error

    return StatusSnapshot(
        state=state,
        mode=mode,
        model=model,
        instance_id=instance_id,
        pid=_parse_pid(data.get("pid")),
        started_at=_parse_datetime(data.get("started_at")),
        updated_at=_parse_datetime(data.get("updated_at")),
        last_heartbeat_at=_parse_datetime(data.get("last_heartbeat_at")),
        error_class=error_class,
    )


class RuntimeStatusStore:
    """Read and atomically write the shared runtime-status file."""

    def __init__(
        self,
        directory: str | Path,
        *,
        filename: str = "bot-status.json",
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.directory = Path(directory)
        self.path = self.directory / filename
        self._now = now

    def read(self) -> StatusSnapshot:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return StatusSnapshot()
        except OSError:
            return StatusSnapshot(state=ERROR, error_class="UNREADABLE")
        return _parse_snapshot(raw)

    def write(self, snapshot: StatusSnapshot) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.directory), prefix=".bot-status-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _record(self, **changes: object) -> StatusSnapshot:
        current = self.read()
        now = self._now()
        snapshot = replace(current, updated_at=now, **changes)
        self.write(snapshot)
        return snapshot

    def record_starting(
        self,
        *,
        mode: str,
        model: str | None = None,
        instance_id: str,
        pid: int,
    ) -> StatusSnapshot:
        now = self._now()
        snapshot = StatusSnapshot(
            state="STARTING",
            mode=mode,
            model=model,
            instance_id=instance_id,
            pid=pid,
            started_at=now,
            updated_at=now,
            last_heartbeat_at=None,
            error_class=None,
        )
        self.write(snapshot)
        return snapshot

    def record_online(self) -> StatusSnapshot:
        current = self.read()
        now = self._now()
        snapshot = replace(
            current,
            state=ONLINE,
            updated_at=now,
            last_heartbeat_at=now,
            error_class=None,
        )
        self.write(snapshot)
        return snapshot

    def record_heartbeat(self) -> StatusSnapshot:
        current = self.read()
        now = self._now()
        snapshot = replace(current, updated_at=now, last_heartbeat_at=now)
        self.write(snapshot)
        return snapshot

    def record_reconnecting(self) -> StatusSnapshot:
        current = self.read()
        now = self._now()
        snapshot = replace(
            current,
            state="RECONNECTING",
            updated_at=now,
            last_heartbeat_at=now,
            error_class=None,
        )
        self.write(snapshot)
        return snapshot

    def record_stopping(self) -> StatusSnapshot:
        current = self.read()
        snapshot = replace(
            current,
            state="STOPPING",
            updated_at=self._now(),
            error_class=None,
        )
        self.write(snapshot)
        return snapshot

    def record_offline(self) -> StatusSnapshot:
        current = self.read()
        now = self._now()
        snapshot = StatusSnapshot(
            state=OFFLINE,
            mode=current.mode,
            model=current.model,
            instance_id=current.instance_id,
            pid=current.pid,
            started_at=current.started_at,
            updated_at=now,
            last_heartbeat_at=None,
            error_class=None,
        )
        self.write(snapshot)
        return snapshot

    def record_error(self, error_class: str) -> StatusSnapshot:
        current = self.read()
        snapshot = replace(
            current,
            state=ERROR,
            updated_at=self._now(),
            error_class=error_class,
        )
        self.write(snapshot)
        return snapshot

    def is_stale(
        self,
        snapshot: StatusSnapshot,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        now: datetime | None = None,
    ) -> bool:
        """Return True when a non-offline, non-error status is no longer fresh.

        A bot that wrote ``STARTING``/``ONLINE``/``RECONNECTING``/``STOPPING``
        and then died without a heartbeat is stale. ``OFFLINE`` and ``ERROR``
        are intentional terminal records and are never reclassified as stale.
        """
        if snapshot.state in _TERMINAL_OR_OFFLINE:
            return False
        marker = snapshot.last_heartbeat_at or snapshot.updated_at
        if marker is None:
            return True
        current = now if now is not None else self._now()
        return (current - marker) > stale_after
