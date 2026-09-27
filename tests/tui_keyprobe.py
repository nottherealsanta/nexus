"""Minimal app used by the real-PTY key-protocol regression test.

``tests/test_tui_keys.py`` launches this in a pseudo-terminal so the actual OS
read -> :class:`~nexus.ui.tui.keys.NexusDriver` -> :class:`NexusXTermParser` ->
app event path runs against raw terminal bytes, not simulated ``Key`` events. It
records every decoded ``event.key`` to a JSONL file and exits on ``Ctrl+Q``.

Kept outside ``test_*`` naming so pytest does not collect it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from nexus.ui.tui.keys import NexusDriver


class KeyProbeApp(App[None]):
    """Record decoded key names; exit on Ctrl+Q."""

    def __init__(self, log_path: str) -> None:
        super().__init__()
        self._log_path = Path(log_path)

    def get_driver_class(self):
        return NexusDriver

    def compose(self) -> ComposeResult:
        yield Static("keyprobe ready")

    def _record(self, record: dict) -> None:
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def on_mount(self) -> None:
        self._record({"event": "ready"})

    def on_key(self, event) -> None:
        self._record({"key": event.key})
        if event.key == "ctrl+q":
            self.exit()


def main(argv: list[str]) -> None:
    KeyProbeApp(argv[1]).run()


if __name__ == "__main__":
    main(sys.argv)
