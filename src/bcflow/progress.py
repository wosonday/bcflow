"""Progress events and cooperative cancellation shared by library and CLI callers.

Cancellation can be requested three ways: request_cancel() from code, Ctrl+C
in the CLI, or by creating `cancel_file`. The file is the portable choice
for a GUI that runs `bcflow` as a child process: on Windows a parent cannot
reliably send Ctrl+C to a child (console control events only reach
processes sharing a console, and Ctrl+Break arrives in Python as SIGBREAK,
not SIGINT).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic

EventHandler = Callable[[dict], None]


class RunCancelled(KeyboardInterrupt):
    """Cancellation must bypass handlers that isolate ordinary variable failures."""


class RunControl:
    def __init__(
        self,
        on_event: EventHandler | None = None,
        *,
        cancel_file: str | Path | None = None,
    ):
        self.on_event = on_event or (lambda event: None)
        self.cancelled = False
        self.started = monotonic()
        self.cancel_file = Path(cancel_file) if cancel_file is not None else None

    def request_cancel(self) -> None:
        self.cancelled = True

    def check_cancelled(self) -> None:
        if (
            not self.cancelled
            and self.cancel_file is not None
            and self.cancel_file.exists()
        ):
            self.cancelled = True
        if self.cancelled:
            raise RunCancelled()

    def emit(self, event: dict) -> None:
        self.on_event(
            {"elapsed_seconds": round(monotonic() - self.started, 2), **event}
        )

    def progress(self, variable: str, stage: str, **details) -> None:
        self.check_cancelled()
        self.emit(
            {"event": "progress", "variable": variable, "stage": stage, **details}
        )
        self.check_cancelled()
