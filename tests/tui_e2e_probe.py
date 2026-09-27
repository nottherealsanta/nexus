"""Real Nexus shell driven under a pseudo-terminal for the key-path test.

``tests/test_tui_keys.py`` launches this in a PTY so the whole interactive path
runs for real: the OS read -> :class:`~nexus.ui.tui.keys.NexusDriver` ->
:class:`~nexus.ui.tui.keys.NexusXTermParser` -> :class:`ChatEditor` -> the
daemon-client turn submission. It uses the deterministic fixture transport from
``tests/visual_tui_demo.py`` (no daemon, no model, no network).

The app appends one JSON object per line to the path given as ``argv[1]``:

* ``{"event": "ready"}`` once the editor is mounted and focused;
* ``{"event": "draft", "text": ...}`` on every editor change;
* ``{"event": "submitted", "content": ...}`` for each submitted turn.

so a caller can observe the exact draft and what was submitted. Exits on
``Ctrl+Q``, like the shell it instantiates.

Kept outside ``test_*`` naming so pytest does not collect it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from textual.widgets import TextArea
from visual_tui_demo import DemoTransport

from nexus.host import protocol as p
from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp


class _ProbeTransport(DemoTransport):
    """The fixture transport, with submissions mirrored into the probe log."""

    def __init__(self, state: str, record: Callable[[dict], None]) -> None:
        super().__init__(state)
        self._record = record

    async def request(self, command: p.Command) -> p.Result:
        if isinstance(command, p.SessionStart):
            self._record({"event": "submitted", "content": command.content})
        return await super().request(command)


class E2EProbeApp(NexusTextualApp):
    """The real shell over a local fixture transport, logging its editor state."""

    def __init__(self, log_path: str) -> None:
        self._log_path = Path(log_path)
        super().__init__(
            Client(_ProbeTransport("functional", self._record)), session="probe"
        )

    def _record(self, record: dict) -> None:
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    async def on_mount(self) -> None:
        await super().on_mount()
        self.query_one("#chat-editor", TextArea).focus()
        self._record({"event": "ready"})

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "chat-editor":
            self._record({"event": "draft", "text": event.text_area.text})


def main(argv: list[str]) -> None:
    E2EProbeApp(argv[1]).run()


if __name__ == "__main__":
    main(sys.argv)
