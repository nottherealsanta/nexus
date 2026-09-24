"""Deterministic Textual browser fixtures for visual development and checks.

This module is deliberately outside the shipped CLI: it exercises the real
daemon-client shell with a local transport and never starts a model provider.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from types import SimpleNamespace

from nexus.events import Event
from nexus.host import protocol as p
from nexus.ui.cli.client import Client
from nexus.ui.tui.agent_picker import AgentPicker
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.permission import PermissionScreen
from nexus.ui.tui.timeline import ToolActivityWidget


def _event(kind: str, seq: int, data: dict | None = None) -> Event:
    return Event(type=kind, data=data or {}, seq=seq, session="visual", turn="turn-1")


def _events(state: str) -> list[Event]:
    if state == "empty":
        return []
    return [
        _event("turn.started", 1),
        _event("input.queued", 2, {"queued_id": "input-1", "content": [{"text": "Give the login flow a quick visual pass."}]}),
        _event("input.consumed", 3, {"queued_id": "input-1", "turn": "turn-1"}),
        _event("model.started", 4, {"provider": "demo", "model": "nexus-small", "streaming": True}),
        _event("thinking", 5, {"text": "I will inspect the current shell and make a focused update."}),
        _event("text", 6, {"text": "I tightened the shell around the conversation and kept the interaction model intact."}),
        _event("tool.requested", 7, {"call_id": "read-1", "tool": "Read", "input": {"path": "nexus/ui/tui/app.tcss"}}),
        _event("tool.completed", 8, {"call_id": "read-1", "tool": "Read", "duration_ms": 24, "result": {"display": "210 lines"}}),
        _event("tool.requested", 9, {"call_id": "edit-1", "tool": "Edit", "input": {"path": "nexus/ui/tui/app.tcss"}}),
        _event("tool.completed", 10, {"call_id": "edit-1", "tool": "Edit", "duration_ms": 38, "result": {"display": "updated", "diff": {"path": "nexus/ui/tui/app.tcss", "added_lines": 3, "removed_lines": 2, "hunk": "--- a/nexus/ui/tui/app.tcss\n+++ b/nexus/ui/tui/app.tcss\n@@ -1,2 +1,3 @@\n-Screen {\n+Screen {\n+    background: #0c0b0b;\n     color: #d6d3d1;"}}}),
        _event("text", 11, {"text": "The diff preview remains bounded, and the composer stays ready for the next turn."}),
        _event("turn.completed", 12, {"stop_reason": "end_turn"}),
    ]


class DemoTransport:
    def __init__(self, state: str) -> None:
        self.events_log = _events(state)

    async def request(self, command: p.Command) -> p.Result:
        if isinstance(command, p.Health):
            return p.HealthResult(sessions=1)
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(session=SimpleNamespace(id=command.session))
        if isinstance(command, p.SessionState):
            return p.SessionStateResult(session=command.session, seq=len(self.events_log), view={})
        if isinstance(command, p.AgentCurrent):
            return p.AgentCurrentResult(session=command.session, name="build", source="visual")
        if isinstance(command, p.AgentsList):
            return p.AgentsListResult(agents=[
                {"name": "general", "description": "General implementation", "contexts": ["root"]},
                {"name": "build", "description": "Focused product changes", "contexts": ["root"]},
                {"name": "explore", "description": "Read-only investigation", "contexts": ["root"]},
            ])
        if isinstance(command, p.SessionCancel):
            return p.SessionCancelResult(session=command.session, cancelled=True)
        if isinstance(command, p.PermissionResolve):
            return p.PermissionResolveResult(session=command.session, request_id=command.request_id, resolved=True)
        raise AssertionError(f"visual fixture received unsupported command: {command!r}")

    async def _stream(self, _session: str, from_seq: int = 0, **_kwargs) -> AsyncIterator[Event]:
        for event in self.events_log:
            if event.seq > from_seq:
                yield event

    def events(self, session: str, from_seq: int = 0, **kwargs) -> AsyncIterator[Event]:
        return self._stream(session, from_seq, **kwargs)

    async def aclose(self) -> None:
        return None


class VisualDemoApp(NexusTextualApp):
    """Real app shell with a fixture-only initial screen state."""

    def __init__(self, state: str) -> None:
        super().__init__(Client(DemoTransport(state)), session="visual")
        self.visual_state = state

    async def on_mount(self) -> None:
        await super().on_mount()
        self.call_after_refresh(self._present_fixture)

    def _present_fixture(self) -> None:
        if self.visual_state == "permission":
            self.push_screen(PermissionScreen({"tool": "Edit", "key": "nexus/ui/tui/app.tcss"}))
        elif self.visual_state == "picker":
            self.push_screen(AgentPicker(self._agents, current=self.controller.agent_name))
        elif self.visual_state == "transcript":
            card = self.query(ToolActivityWidget)
            if card:
                self.run_worker(card[-1].toggle(), group="visual-diff", exclusive=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a deterministic Nexus Textual visual fixture")
    parser.add_argument("--state", choices=("empty", "transcript", "permission", "picker"), default="empty")
    args = parser.parse_args()
    VisualDemoApp(args.state).run()


if __name__ == "__main__":
    main()
