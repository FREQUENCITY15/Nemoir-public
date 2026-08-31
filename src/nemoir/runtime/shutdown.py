"""Instance-scoped shutdown request used for graceful bot shutdown.

A stop request is a tiny marker file named for one bot instance, so the
control panel can ask *its* child to stop without disturbing a bot that was
started elsewhere (for example from PowerShell). The marker carries no
secrets and is cleared when honoured or on the next clean startup.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _request_filename(instance_id: str) -> str:
    # Defensive sanitisation so a hostile/odd instance id cannot escape the
    # runtime directory through path traversal.
    safe = "".join(ch for ch in instance_id if ch.isalnum() or ch in "-_")
    return f"shutdown-{safe or 'unknown'}.request"


class ShutdownRequest:
    def __init__(self, directory: str | Path, instance_id: str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / _request_filename(instance_id)

    def request(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.directory), prefix=".shutdown-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("stop\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def is_requested(self) -> bool:
        return self.path.exists()
