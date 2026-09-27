"""Deterministic Textual browser fixtures for visual and functional checks.

This module is deliberately outside the shipped CLI: it exercises the real
daemon-client shell with a local transport and never starts a model provider.

Two families of fixture share one transport:

* ``empty`` / ``transcript`` / ``permission`` / ``picker`` seed the initial
  screen from a fixed event log for screenshot and layout checks.
* ``functional`` additionally accepts a ``SessionStart`` command, so the same
  shell can be driven through a real Enter-submitted turn (read a file, edit a
  file, render the tool cards) without a model or daemon. When
  ``NEXUS_VISUAL_SUBMIT_LOG`` names a path, every submitted prompt is appended
  there as one JSON string per line, so a browser check can observe exactly what
  the shell submitted (newlines included).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import Label, Static, TextArea

from nexus.events import Event
from nexus.host import protocol as p
from nexus.ui.cli.client import Client
from nexus.ui.tui.agent_picker import AgentPicker
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.permission import PermissionScreen
from nexus.ui.tui.timeline import ToolActivityWidget

#: The file the functional fixture "reads" and "edits". Its content is a small,
#: inert stylesheet snippet so the render is stable and the edit is safe.
FIXTURE_PATH = "nexus/ui/tui/app.tcss"
FIXTURE_BEFORE = "Screen {\n    color: #d6d3d1;\n}\n"


def _event(kind: str, seq: int, data: dict | None = None, *, turn: str = "turn-1") -> Event:
    return Event(type=kind, data=data or {}, seq=seq, session="visual", turn=turn)


def _seed_events(state: str) -> list[Event]:
    if state in ("empty", "functional"):
        return []
    if state == "reference":
        first = "reference-1"
        second = "reference-2"

        def event(kind: str, seq: int, data: dict | None = None, *, turn: str = first, ts: float = 1_759_000_000.0) -> Event:
            return Event(type=kind, data=data or {}, seq=seq, session="visual", turn=turn, ts=ts)

        return [
            event("input.queued", 1, {"queued_id": "hi", "content": [{"text": "hi"}]}, ts=1_759_000_000.0),
            event("turn.started", 2, {}, ts=1_759_000_000.1),
            event("input.consumed", 3, {"queued_id": "hi", "turn": first}, ts=1_759_000_000.2),
            event("model.started", 4, {"provider": "openai", "model": "GPT-6 Luna", "streaming": True}, ts=1_759_000_000.3),
            event("text", 5, {"text": "Hi! What can I help you with?"}, ts=1_759_000_003.5),
            event("model.stopped", 6, {"stop_reason": "end_turn"}, ts=1_759_000_003.5),
            event("turn.completed", 7, {"stop_reason": "end_turn"}, ts=1_759_000_003.5),
            event("input.queued", 8, {"queued_id": "daemon", "content": [{"text": "how to start the daemon"}]}, turn=second, ts=1_759_000_100.0),
            event("turn.started", 9, {}, turn=second, ts=1_759_000_100.1),
            event("input.consumed", 10, {"queued_id": "daemon", "turn": second}, turn=second, ts=1_759_000_100.2),
            event("model.started", 11, {"provider": "openai", "model": "GPT-6 Luna", "streaming": True}, turn=second, ts=1_759_000_100.3),
            event("thinking", 12, {"text": "I’ll check the project setup first."}, turn=second, ts=1_759_000_100.7),
            event("model.stopped", 13, {"stop_reason": "tool_use"}, turn=second, ts=1_759_000_101.0),
            event("tool.requested", 14, {"call_id": "glob", "tool": "Glob", "input": {"pattern": "**/*", "path": "."}}, turn=second, ts=1_759_000_101.1),
            event("tool.completed", 15, {"call_id": "glob", "tool": "Glob", "duration_ms": 88, "result": {"display": "100 matches"}}, turn=second, ts=1_759_000_101.2),
            event("tool.requested", 16, {"call_id": "grep", "tool": "Grep", "input": {"pattern": "daemon|serve", "path": "."}}, turn=second, ts=1_759_000_101.3),
            event("tool.completed", 17, {"call_id": "grep", "tool": "Grep", "duration_ms": 41, "result": {"display": "56 matches"}}, turn=second, ts=1_759_000_101.4),
            event("tool.requested", 18, {"call_id": "read", "tool": "Read", "input": {"path": "README.md", "offset": 90, "limit": 38}}, turn=second, ts=1_759_000_101.5),
            event("tool.completed", 19, {"call_id": "read", "tool": "Read", "duration_ms": 24, "result": {"display": "38 lines"}}, turn=second, ts=1_759_000_101.6),
            event("model.started", 20, {"provider": "openai", "model": "GPT-6 Luna", "iteration": 1, "streaming": True}, turn=second, ts=1_759_000_101.7),
            event("text", 21, {"text": "You usually don’t need to start it manually: running a Nexus command such as `nexus doctor` or `nexus run \"...\"` starts the workspace daemon automatically if it isn’t already running.\n\nTo check it or manage it directly:\n\n```text\nnexus daemon status\nnexus daemon logs\nnexus daemon stop\n```\n\nThe daemon shuts itself down after 300 seconds idle by default."}, turn=second, ts=1_759_000_111.8),
            event("model.stopped", 22, {"stop_reason": "end_turn"}, turn=second, ts=1_759_000_111.8),
            event("turn.completed", 23, {"stop_reason": "end_turn"}, turn=second, ts=1_759_000_111.8),
        ]
    return [
        _event("turn.started", 1),
        _event("input.queued", 2, {"queued_id": "input-1", "content": [{"text": "Give the login flow a quick visual pass."}]}),
        _event("input.consumed", 3, {"queued_id": "input-1", "turn": "turn-1"}),
        _event("model.started", 4, {"provider": "demo", "model": "nexus-small", "streaming": True}),
        _event("thinking", 5, {"text": "I will inspect the current shell and make a focused update."}),
        _event("text", 6, {"text": "I tightened the shell around the conversation and kept the interaction model intact."}),
        _event("tool.requested", 7, {"call_id": "read-1", "tool": "Read", "input": {"path": FIXTURE_PATH}}),
        _event("tool.completed", 8, {"call_id": "read-1", "tool": "Read", "duration_ms": 24, "result": {"content": [{"type": "text", "text": FIXTURE_BEFORE}], "display": "3 lines"}}),
        _event("tool.requested", 9, {"call_id": "edit-1", "tool": "Edit", "input": {"path": FIXTURE_PATH}}),
        _event("tool.completed", 10, {"call_id": "edit-1", "tool": "Edit", "duration_ms": 38, "result": {"display": "updated", "diff": {"path": FIXTURE_PATH, "added_lines": 3, "removed_lines": 2, "hunk": f"--- a/{FIXTURE_PATH}\n+++ b/{FIXTURE_PATH}\n@@ -1,2 +1,3 @@\n-Screen {{\n+Screen {{\n+    background: #0c0b0b;\n     color: #d6d3d1;"}}}),
        _event("text", 11, {"text": "The diff preview remains bounded, and the composer stays ready for the next turn."}),
        _event("turn.completed", 12, {"stop_reason": "end_turn"}),
    ]


def _functional_turn(content: str) -> list[Event]:
    """A real read-file, edit-file turn driven by the submitted prompt.

    Every event carries the same turn id, including the ``input.consumed``
    payload, so the user prompt and the assistant/tool output land in one turn
    in the correct order rather than opening a second turn for the prompt.
    """
    turn = "turn-live"

    def event(kind: str, seq: int, data: dict | None = None) -> Event:
        return _event(kind, seq, data, turn=turn)

    return [
        event("input.queued", 1, {"queued_id": "q-live", "content": [{"text": content}]}),
        event("turn.started", 2),
        event("input.consumed", 3, {"queued_id": "q-live", "turn": turn}),
        event("model.started", 4, {"provider": "demo", "model": "nexus-small", "streaming": True}),
        event("text", 5, {"text": "Reading the file, then applying the requested change."}),
        event("tool.requested", 6, {"call_id": "read-live", "tool": "Read", "input": {"path": FIXTURE_PATH}}),
        event("tool.completed", 7, {"call_id": "read-live", "tool": "Read", "duration_ms": 12, "result": {"content": [{"type": "text", "text": FIXTURE_BEFORE}], "display": "3 lines"}}),
        event("tool.requested", 8, {"call_id": "edit-live", "tool": "Edit", "input": {"path": FIXTURE_PATH}}),
        event("tool.completed", 9, {"call_id": "edit-live", "tool": "Edit", "duration_ms": 20, "result": {"display": "updated", "diff": {"path": FIXTURE_PATH, "added_lines": 1, "removed_lines": 0, "hunk": f"--- a/{FIXTURE_PATH}\n+++ b/{FIXTURE_PATH}\n@@ -1,2 +1,3 @@\n Screen {{\n+    background: #0c0b0b;\n     color: #d6d3d1;"}}}),
        event("text", 10, {"text": "Edit applied."}),
        event("turn.completed", 11, {"stop_reason": "end_turn"}),
    ]


def _record_submission(content: str) -> None:
    """Append a submitted prompt to ``NEXUS_VISUAL_SUBMIT_LOG`` when set.

    This gives the browser check an app-level signal for what the shell actually
    submitted (the xterm helper textarea is always empty), including embedded
    newlines. One JSON string per line keeps multi-line drafts unambiguous.
    """
    path = os.environ.get("NEXUS_VISUAL_SUBMIT_LOG")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(content) + "\n")


class DemoTransport:
    """A deterministic host transport shared by every visual fixture."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.events_log = _seed_events(state)
        self._live: list[Event] = []
        self._started = False

    async def request(self, command: p.Command) -> p.Result:
        if isinstance(command, p.Health):
            return p.HealthResult(sessions=1)
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(session=SimpleNamespace(id=command.session))
        if isinstance(command, p.SessionState):
            return p.SessionStateResult(session=command.session, seq=len(self.events_log), view={})
        if isinstance(command, p.AgentCurrent):
            return p.AgentCurrentResult(
                session=command.session,
                name="build",
                source="visual",
                provider="OpenAI",
                model="GPT-6 Luna",
                reasoning_effort="not exposed",
                thinking_budget=2048,
            )
        if isinstance(command, p.AgentsList):
            return p.AgentsListResult(agents=[
                {"name": "general", "description": "General implementation", "contexts": ["root"]},
                {"name": "build", "description": "Focused product changes", "contexts": ["root"]},
                {"name": "explore", "description": "Read-only investigation", "contexts": ["root"]},
            ])
        if isinstance(command, p.SessionStart):
            # A functional turn: later subscribers replay the scripted events.
            _record_submission(command.content)
            self._live = _functional_turn(command.content)
            self._started = True
            return p.SessionStartResult(session=command.session, turn_id="turn-live")
        if isinstance(command, p.SessionCancel):
            return p.SessionCancelResult(session=command.session, cancelled=True)
        if isinstance(command, p.PermissionResolve):
            return p.PermissionResolveResult(session=command.session, request_id=command.request_id, resolved=True)
        raise AssertionError(f"visual fixture received unsupported command: {command!r}")

    async def _stream(
        self, _session: str, from_seq: int = 0, *, follow: bool = True, **_kwargs
    ) -> AsyncIterator[Event]:
        for event in self.events_log:
            if event.seq > from_seq:
                yield event
        if not follow:
            return
        # A live subscription is opened before ``SessionStart`` is issued, so
        # wait briefly for the scripted functional turn to be armed, then
        # deliver it. Bound the wait so a state that never starts still ends.
        deadline = time.monotonic() + 5
        while not self._started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        for event in self._live:
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
            self.push_screen(PermissionScreen({"tool": "Edit", "key": FIXTURE_PATH}))
        elif self.visual_state == "picker":
            self.push_screen(AgentPicker(self._agents, current=self.controller.agent_name))
        elif self.visual_state == "transcript":
            card = self.query(ToolActivityWidget)
            if card:
                self.run_worker(card[-1].toggle(), group="visual-diff", exclusive=True)
        elif self.visual_state == "reference":
            self._present_reference()
            # Agent metadata sync may run again as replay settles; keep the
            # fixture's compact, faithful composer label stable throughout.
            self._reference_label_timer = self.set_interval(0.25, self._present_reference)

    def _present_reference(self) -> None:
        """Apply screenshot-only labels and metadata to the real timeline widgets."""
        self.screen.add_class("reference-demo")
        self.query_one("#context-preview").display = False
        self.query_one("#connection-status").display = False
        editor = self.query_one("#chat-editor", TextArea)
        editor.cursor_blink = False
        composer_label = Text("Build", style="#6ba8ff")
        composer_label.append(" · ", style="#777777")
        composer_label.append("GPT-6 Luna OpenAI", style="#aaaaaa")
        self.query_one("#root-agent-name").update(composer_label)
        self.query_one("#bottom-info").display = True
        self.query_one("#cwd-path").update("/Users/santa/repos/nexus")
        self.query_one("#context-usage").update("13.3K (1%)  ctrl+p commands")
        for selector in (
            "#root-separator-model", "#root-model", "#root-separator-provider",
            "#root-provider", "#root-separator-effort", "#root-effort",
        ):
            self.query_one(selector).display = False

        turns = self.query(".turn")
        user_times = iter(("9:46 AM", "9:51 AM"))
        for index, turn in enumerate(turns):
            user = turn.query_one(".timeline-user")
            body = next(
                (message.text for message in turn.turn.messages if message.role == "user"),
                "",
            )
            user_text = Text(body, style="#f1f1f1")
            user_text.append("\n" + next(user_times), style="#777777")
            user.update(user_text)

            assistants = list(turn.query(".timeline-assistant"))
            if index == 1 and assistants:
                assistants[0].update("Thought · 402ms")
                assistants[0].styles.color = "#dca64d"
                if len(assistants) > 1:
                    assistant = assistants[1]
                    assistant.update(
                        "You usually don’t need to start it manually: running a Nexus command "
                        "such as `nexus doctor` or `nexus run \"...\"` starts the workspace "
                        "daemon automatically if it isn’t already running.\n\n"
                        "To check it or manage it directly:\n\n"
                        "```text\n"
                        "nexus daemon status\n"
                        "nexus daemon logs\n"
                        "nexus daemon stop\n"
                        "```\n\n"
                        "The daemon shuts itself down after 300 seconds idle by default."
                    )
                    fences = assistant.query("MarkdownFence")
                    if fences:
                        code = fences[0].query_one("#code-content", Label)
                        code.update(Text(
                            "nexus daemon status\n"
                            "nexus daemon logs\n"
                            "nexus daemon stop",
                            style="#d77b72",
                        ))

            summary = turn._summary_widget
            if summary is not None:
                meta = Text("▪ ", style="#6ba8ff")
                meta.append("Build", style="#f1f1f1")
                meta.append(" · GPT-6 Luna", style="#888888")
                if index == 0:
                    meta.append(" · 3.3s", style="#888888")
                else:
                    meta.append(" · 11.6s", style="#888888")
                summary.update(meta)

        for turn in turns:
            for tool in turn.query(ToolActivityWidget):
                command = {
                    "glob": '* Glob "**/*" in . (100 matches)',
                    "grep": '* Grep "daemon|serve" in . (56 matches)',
                    "read": "→ Read README.md [offset=90, limit=38]",
                }.get(tool.call_id, "")
                tool.query_one("#tool-header").update(Text(command, style="#9b9b9b"))
                tool.query_one("#tool-detail").update("")
                tool.query_one("#tool-expanded").update("")
                tool.styles.height = 1



def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a deterministic Nexus Textual visual fixture")
    parser.add_argument(
        "--state",
        choices=("empty", "transcript", "permission", "picker", "functional", "reference"),
        default="empty",
    )
    args = parser.parse_args()
    VisualDemoApp(args.state).run()


if __name__ == "__main__":
    main()
