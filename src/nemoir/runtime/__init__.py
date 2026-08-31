"""Runtime status, single-instance lock, and shutdown request primitives.

This package is the shared boundary between CLI-started bots and the
``nemoir gui`` control panel. It deliberately contains no Discord, DeepSeek,
or Tkinter imports so it can be exercised without a display or a network.
"""

from .instance_lock import InstanceLock, LockBusyError
from .shutdown import ShutdownRequest
from .status import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_STALE_AFTER,
    MODES,
    OFFLINE,
    STATES,
    RuntimeStatusStore,
    StatusSnapshot,
    utc_now,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_STALE_AFTER",
    "InstanceLock",
    "LockBusyError",
    "MODES",
    "OFFLINE",
    "STATES",
    "RuntimeStatusStore",
    "ShutdownRequest",
    "StatusSnapshot",
    "utc_now",
]
