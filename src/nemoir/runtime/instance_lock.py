"""OS-released single-instance lock for the Discord bot process.

The lock is held by an open file descriptor. On Windows it uses ``msvcrt``
byte-range locking and on POSIX it uses ``fcntl.flock``; both mechanisms are
released automatically by the operating system when the owning process exits,
even if it crashes, so a stale status file can never permanently block a new
bot. A portable ``O_EXCL`` file fallback with mtime-based reclamation covers
platforms without either primitive.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None  # type: ignore[assignment]

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

DEFAULT_LOCK_STALE = timedelta(seconds=60.0)


class LockBusyError(Exception):
    """Another live process already holds the single-instance lock."""


def _lock_file_is_stale(path: Path, stale_after: timedelta) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return True
    return (datetime.now(timezone.utc) - mtime) > stale_after


def _acquire_windows(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        raise LockBusyError(f"Bot instance lock is already held: {path}") from None
    return fd


def _release_windows(handle: int) -> None:
    try:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(handle)


def _acquire_posix(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LockBusyError(f"Bot instance lock is already held: {path}") from None
    return fd


def _release_posix(handle: int) -> None:
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _acquire_portable(path: Path, stale_after: timedelta) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)

    def _create() -> int:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)

    try:
        fd = _create()
    except FileExistsError:
        if not _lock_file_is_stale(path, stale_after):
            raise LockBusyError(f"Bot instance lock is already held: {path}") from None
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        try:
            fd = _create()
        except FileExistsError:
            raise LockBusyError(f"Bot instance lock is already held: {path}") from None
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)
    return str(path)


def _release_portable(handle: str) -> None:
    try:
        os.unlink(handle)
    except FileNotFoundError:
        pass


def _acquire_os_lock(path: Path, *, stale_after: timedelta) -> object:
    if msvcrt is not None:
        return _acquire_windows(path)
    if fcntl is not None:
        return _acquire_posix(path)
    return _acquire_portable(path, stale_after)


def _release_os_lock(handle: object) -> None:
    if msvcrt is not None:
        _release_windows(handle)  # type: ignore[arg-type]
    elif fcntl is not None:
        _release_posix(handle)  # type: ignore[arg-type]
    else:
        _release_portable(handle)  # type: ignore[arg-type]


class InstanceLock:
    """A non-blocking, re-entrant-per-object single-instance lock."""

    def __init__(
        self,
        path: str | Path,
        *,
        stale_after: timedelta = DEFAULT_LOCK_STALE,
    ) -> None:
        self.path = Path(path)
        self._stale_after = stale_after
        self._handle: object | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> bool:
        """Acquire the lock; return False (never raise) if it is held."""
        if self._acquired:
            return True
        if self._handle is not None:
            return False
        try:
            self._handle = _acquire_os_lock(self.path, stale_after=self._stale_after)
        except LockBusyError:
            self._handle = None
            return False
        self._acquired = True
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self._acquired = False
        if handle is not None:
            _release_os_lock(handle)

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise LockBusyError(f"Bot instance lock is already held: {self.path}")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
