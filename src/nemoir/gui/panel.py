"""Tkinter view and adapters for the Nemoir Control Panel.

This module is the only place that imports Tkinter. It is a thin view over
``ControlPanelController`` and owns the real subprocess runner, message-box
dialogs, and Tk scheduling.
"""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from nemoir.config import Settings
from nemoir.gui.controller import (
    ConfigReadiness,
    ControlPanelController,
    DisplayState,
    ProcessRunner,
    assess_readiness,
    make_default_launch_planner,
    pid_is_alive,
)
from nemoir.runtime import InstanceLock, RuntimeStatusStore, utc_now

_STATUS_COLOURS = {
    "OFFLINE": "#7A7A7A",
    "STARTING": "#C79100",
    "ONLINE — SYNTHETIC": "#2E7D32",
    "ONLINE — CHANNEL TEST": "#7B1FA2",
    "ONLINE — LIVE": "#1565C0",
    "ONLINE — AUTONOMOUS TEST": "#00695C",
    "ONLINE — AUTONOMOUS LIVE": "#4527A0",
    "RECONNECTING": "#EF6C00",
    "STOPPING": "#C62828",
    "ERROR": "#C62828",
}

_LIVE_NOTE = (
    "Live mode enables paid model calls only when a model-backed Discord "
    "command is used (for example /prompt, claim discovery, or analysis). "
    "Autonomous Test sorts with the synthetic provider (no DeepSeek) but "
    "creates real shared channels; Autonomous Live makes paid DeepSeek sort "
    "calls per sealed capture and creates real shared channels."
)

_LIVE_CONFIRM = (
    "Start the live bot?\n\n"
    "Connecting to Discord itself is free, but the /prompt command, claim "
    "discovery, and analysis make paid DeepSeek model calls per use. Starting "
    "the bot does not make a model request by itself."
)

_CLOSE_CONFIRM = (
    "The bot is still running. Stop the bot before closing the panel?"
)

_CHANNEL_TEST_CONFIRM = (
    "Start the synthetic channel-writing test?\n\n"
    "This connects to Discord with the deterministic synthetic provider (no "
    "DeepSeek calls) and enables channel-write permission for this child bot "
    "process only. Running `/publish-bundle confirm:true` will create REAL "
    "Discord text channels in the configured development category. Use it only "
    "in a development guild you are happy to modify."
)

_AUTONOMOUS_TEST_CONFIRM = (
    "Start Autonomous Test?\n\n"
    "This connects to Discord with the deterministic synthetic autonomous sort "
    "(never reads or calls DeepSeek) and enables channel-write permission for "
    "this child bot process only. Sealing a recipient-free capture automatically "
    "sorts it and creates REAL shared Discord text channels in the configured "
    "development category. Use it only in a development guild you are happy to "
    "modify."
)

_AUTONOMOUS_LIVE_CONFIRM = (
    "Start Autonomous Live?\n\n"
    "This enables paid DeepSeek autonomous-sort calls (one paid request per "
    "sealed recipient-free capture) and channel-write permission for this child "
    "bot process only. Sealing a capture automatically sorts it with DeepSeek "
    "and creates REAL shared Discord text channels in the configured development "
    "category. Confirm only for an approved live run."
)

_TICK_MS = 500


class TkDialogService:
    """Message-box based confirmation/message adapter for the controller."""

    def confirm_live(self) -> bool:
        return messagebox.askyesno("Start Live", _LIVE_CONFIRM, parent=None)

    def confirm_channel_test(self) -> bool:
        return messagebox.askyesno("Start Channel Test", _CHANNEL_TEST_CONFIRM, parent=None)

    def confirm_autonomous_test(self) -> bool:
        return messagebox.askyesno("Start Autonomous Test", _AUTONOMOUS_TEST_CONFIRM, parent=None)

    def confirm_autonomous_live(self) -> bool:
        return messagebox.askyesno("Start Autonomous Live", _AUTONOMOUS_LIVE_CONFIRM, parent=None)

    def confirm_close(self) -> bool:
        return messagebox.askyesno("Bot still running", _CLOSE_CONFIRM, parent=None)

    def show_message(self, text: str) -> None:
        messagebox.showwarning("Nemoir Control Panel", text, parent=None)


class ControlPanelWindow:
    def __init__(
        self,
        controller: ControlPanelController,
        *,
        tick_ms: int = _TICK_MS,
    ) -> None:
        self._controller = controller
        self._tick_ms = tick_ms

        self.root = tk.Tk()
        self.root.title("Nemoir Control Panel")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._status_var = tk.StringVar(value="OFFLINE")
        self._mode_var = tk.StringVar(value="")
        self._model_var = tk.StringVar(value="")
        self._uptime_var = tk.StringVar(value="")
        self._heartbeat_var = tk.StringVar(value="")
        self._error_var = tk.StringVar(value="")

        self._build_widgets()
        self._apply_state(controller.display_state())
        self.root.after(self._tick_ms, self._tick)

    def _build_widgets(self) -> None:
        frame = tk.Frame(self.root, padx=16, pady=14)
        frame.grid(sticky="nsew")

        badge = tk.Label(
            frame,
            textvariable=self._status_var,
            font=("Segoe UI", 22, "bold"),
            fg="white",
            bg=_STATUS_COLOURS["OFFLINE"],
            padx=24,
            pady=12,
            width=20,
        )
        badge.grid(row=0, column=0, sticky="ew")
        self._badge = badge

        details = tk.Frame(frame)
        details.grid(row=1, column=0, sticky="w", pady=(12, 0))

        rows = [
            ("Mode", self._mode_var),
            ("Live model", self._model_var),
            ("Uptime", self._uptime_var),
            ("Last heartbeat", self._heartbeat_var),
        ]
        self._detail_labels: list[tuple[str, tk.Label, tk.StringVar]] = []
        for index, (title, var) in enumerate(rows):
            tk.Label(details, text=f"{title}:", anchor="e", width=14).grid(
                row=index, column=0, sticky="e"
            )
            value_label = tk.Label(details, textvariable=var, anchor="w")
            value_label.grid(row=index, column=1, sticky="w")
            self._detail_labels.append((title, value_label, var))

        self._error_label = tk.Label(
            frame, textvariable=self._error_var, fg="#C62828", wraplength=380, justify="left"
        )

        buttons = tk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self._start_synthetic = tk.Button(
            buttons, text="Start Synthetic", command=self._controller.start_synthetic
        )
        self._start_synthetic.grid(row=0, column=0, padx=(0, 6))
        self._start_channel_test = tk.Button(
            buttons, text="Start Channel Test", command=self._controller.start_channel_test
        )
        self._start_channel_test.grid(row=0, column=1, padx=6)
        self._start_live = tk.Button(
            buttons, text="Start Live", command=self._controller.start_live
        )
        self._start_live.grid(row=0, column=2, padx=6)
        self._start_autonomous_test = tk.Button(
            buttons,
            text="Start Autonomous Test",
            command=self._controller.start_autonomous_test,
        )
        self._start_autonomous_test.grid(row=1, column=0, padx=(0, 6), pady=(6, 0))
        self._start_autonomous_live = tk.Button(
            buttons,
            text="Start Autonomous Live",
            command=self._controller.start_autonomous_live,
        )
        self._start_autonomous_live.grid(row=1, column=1, padx=6, pady=(6, 0))
        self._stop = tk.Button(
            buttons, text="Stop Bot", command=self._controller.stop_bot
        )
        self._stop.grid(row=0, column=3, padx=(6, 0))

        note = tk.Label(
            frame,
            text=_LIVE_NOTE,
            wraplength=380,
            justify="left",
            fg="#555555",
        )
        note.grid(row=4, column=0, sticky="w", pady=(12, 0))

    def _apply_state(self, state: DisplayState) -> None:
        self._status_var.set(state.status)
        self._badge.configure(bg=_STATUS_COLOURS.get(state.status, "#7A7A7A"))
        self._mode_var.set(state.mode if state.mode else "—")
        self._model_var.set(state.model if state.model else "—")
        self._uptime_var.set(state.uptime if state.uptime else "—")
        self._heartbeat_var.set(state.last_heartbeat if state.last_heartbeat else "—")
        self._error_var.set(state.error)

        if state.error:
            self._error_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
        else:
            self._error_label.grid_remove()

        start_state = tk.NORMAL if state.can_start else tk.DISABLED
        self._start_synthetic.configure(state=start_state)
        self._start_channel_test.configure(state=start_state)
        self._start_live.configure(state=start_state)
        self._start_autonomous_test.configure(state=start_state)
        self._start_autonomous_live.configure(state=start_state)
        self._stop.configure(state=tk.NORMAL if state.can_stop else tk.DISABLED)

    def _tick(self) -> None:
        self._controller.tick()
        self._apply_state(self._controller.display_state())
        if self._controller.ready_to_close:
            self.root.destroy()
            return
        self.root.after(self._tick_ms, self._tick)

    def _on_close(self) -> None:
        self._controller.begin_close()

    def mainloop(self) -> None:
        self.root.mainloop()


def _assess(live: bool) -> ConfigReadiness:
    settings = Settings.from_environment(include_deepseek=live)
    return assess_readiness(settings, live=live)


def run_gui() -> int:
    """Open the control panel window; returns when the window is closed."""
    repo_root = Path(__file__).resolve().parents[3]
    settings = Settings.from_environment()
    runtime_dir = settings.runtime_dir
    if not runtime_dir.is_absolute():
        runtime_dir = repo_root / runtime_dir
    runtime_dir = runtime_dir.resolve()

    status_store = RuntimeStatusStore(runtime_dir)
    planner = make_default_launch_planner(runtime_dir=runtime_dir, repo_root=repo_root)
    runner = ProcessRunner()
    dialog = TkDialogService()

    def lock_factory() -> InstanceLock:
        return InstanceLock(runtime_dir / "bot.lock")

    controller = ControlPanelController(
        status_store=status_store,
        runner=runner,
        build_request=planner,
        lock_factory=lock_factory,
        dialog=dialog,
        assess=_assess,
        now=utc_now,
        pid_alive=pid_is_alive,
        notify_message=dialog.show_message,
    )
    window = ControlPanelWindow(controller)
    window.mainloop()
    return 0
