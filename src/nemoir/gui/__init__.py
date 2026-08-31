"""Nemoir Control Panel: a compact Tkinter desktop window over the runtime status.

The controller layer (``controller``) is display-free and fully injectable so
it can be tested without a display, Discord, or any live connection. The
``panel`` module owns the Tkinter widgets and the subprocess/dialog adapters.
"""

from .controller import (
    ConfigReadiness,
    DisplayState,
    LaunchRequest,
    assess_readiness,
    build_launch_request,
    pid_is_alive,
)

__all__ = [
    "ConfigReadiness",
    "DisplayState",
    "LaunchRequest",
    "assess_readiness",
    "build_launch_request",
    "pid_is_alive",
]
