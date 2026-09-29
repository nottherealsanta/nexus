"""Pilot tests for the only interactive Nexus chat surface."""

from __future__ import annotations

import asyncio
import importlib.resources
import importlib.util
import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import TextArea

from nexus.events import Event
from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from nexus.ui.cli.client import Client, TransportClosed
from nexus.ui.tui.agent_picker import AgentPicker
from nexus.ui.tui.agent_transcript import AgentTranscriptScreen
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.messages import EventReceived
from nexus.ui.tui.timeline import (
    ConversationTimeline,
    TaskActivityWidget,
    ToolActivityWidget,
    format_arguments,
)


class FakeTransport:
    def __init__(self, events=(), *, fail_stream: bool = False) -> None:
        self.events_log = list(events)
        self.trace: list[object] = []
        self.fail_stream = fail_stream
        self.closed = False
        self.started = asyncio.Event()
        self.permission_resolved = asyncio.Event()
        self.cancelled = False
        self.permission_decision: str | None = None
        self.permission_wins = True
        self.last_input: str | None = None
        self.agent_name = "general"
        self.agent_source = "default"

    async def request(self, command):
        self.trace.append(type(command).__name__)
        if isinstance(command, p.Health):
            return p.HealthResult()
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(
                SessionSummary(id=command.session, last_seq=len(self.events_log))
            )
        if isinstance(command, p.SessionState):
            return p.SessionStateResult(
                session=command.session,
                seq=len(self.events_log),
                view=_view(self.events_log),
            )
        if isinstance(command, p.LogsRead):
            return p.LogsReadResult(
                daemon=p.DaemonLogPage(next_cursor=f"{'a' * 32}:0"),
                session=p.SessionLogPage(),
            )
        if isinstance(command, p.AgentCurrent):
            return p.AgentCurrentResult(
                session=command.session,
                name=self.agent_name,
                source=self.agent_source,
            )
        if isinstance(command, p.ContextInspect):
            return p.ContextInspectResult(
                session=command.session,
                mode="next_turn_preview",
                actually_sent=False,
                redacted_for_display=True,
                agent={"name": "general", "source": "default", "instructions_included": True},
                provider="fake",
                model="preview-model",
                system_files={
                    "soul": {"configured": True, "loaded": True, "included": True, "included_nonempty": True, "source": "SOUL.md"},
                    "memory": {"configured": True, "loaded": True, "included": True, "included_nonempty": True, "source": "MEMORY.md"},
                },
                system_text="Selected system prompt",
                included_parts=[
                    {"name": "system", "text": "Selected system prompt"},
                    {"name": "soul", "text": "SOUL instructions"},
                    {"name": "memory", "text": "MEMORY notes"},
                ],
                tools=[{"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}}],
                messages=[{"role": "user", "blocks": [{"type": "text", "text": "Earlier request"}]}],
                history_included=True,
                request_context={"used_tokens": 23500, "input_budget": 100000},
                skills_index=[{"name": "search", "description": "Search workspace", "included": True}],
                mcp_index="mcp__docs__search: Search documentation",
                budget={"used_tokens": 23500, "input_budget": 100000},
                omitted=["draft input", "conversation history"],
            )
        if isinstance(command, p.AgentsList):
            return p.AgentsListResult(agents=[
                {"name": "general", "description": "General coding", "contexts": ["root"]},
                {"name": "plan", "description": "Plan work", "contexts": ["root"]},
                {"name": "build", "description": "Build work", "contexts": ["root"]},
                {"name": "explore", "description": "Explore work", "contexts": ["root"]},
                {"name": "zebra", "description": "Custom root", "contexts": ["root"]},
                {"name": "subonly", "description": "Not root eligible", "contexts": ["subagent"]},
            ])
        if isinstance(command, p.AgentTranscript):
            return p.AgentTranscriptResult(
                session=command.session,
                agent_id=command.agent_id,
                found=True,
                status="running",
                view={},
            )
        if isinstance(command, p.SessionStart):
            self.trace.append("start_turn")
            self.last_input = command.content
            self.started.set()
            return p.SessionStartResult(session=command.session, turn_id="turn-1")
        if isinstance(command, p.QuestionAnswer):
            self.question_answer = (command.call_id, command.answer)
            return p.QuestionAnswerResult(session=command.session, call_id=command.call_id, resolved=True)
        if isinstance(command, p.PermissionResolve):
            self.permission_decision = command.decision
            self.permission_resolved.set()
            return p.PermissionResolveResult(
                session=command.session,
                request_id=command.request_id,
                resolved=self.permission_wins,
            )
        if isinstance(command, p.SessionCancel):
            self.cancelled = True
            return p.SessionCancelResult(session=command.session, cancelled=True)
        if isinstance(command, p.AgentSelect):
            self.agent_name = command.name
            self.agent_source = "session"
            return p.AgentSelectResult(
                session=command.session, name=self.agent_name, source=self.agent_source
            )
        if isinstance(command, p.AgentReset):
            self.agent_name = "general"
            self.agent_source = "default"
            return p.AgentSelectResult(
                session=command.session, name=self.agent_name, source=self.agent_source
            )
        if isinstance(command, p.ModelsList):
            return p.ModelsListResult(models=[{"provider": "fake", "id": "m", "tier": "medium"}])
        if isinstance(command, p.ModelSelect):
            return p.ModelSelectResult(session=command.session, provider="fake", model=command.ref)
        if isinstance(command, p.ReasoningEffortSelect):
            return p.ReasoningEffortSelectResult(
                session=command.session, effort=command.effort, supported_levels=[]
            )
        if isinstance(command, p.SessionExport):
            return p.SessionExportResult(session=command.session, format=command.format, content="# export")
        if isinstance(command, p.SessionFork):
            return p.SessionForkResult(session=SessionSummary(id="forked"))
        if isinstance(command, p.SessionList):
            return p.SessionListResult(sessions=[SessionSummary(id="s"), SessionSummary(id="other")])
        raise AssertionError(f"unexpected command {command!r}")

    async def _stream(self, session, from_seq=0, *, follow=True, client_id=None):
        self.trace.append(("subscribe", session, from_seq, follow))
        if self.fail_stream and follow:
            raise TransportClosed("gone")
        for event in self.events_log:
            if event.seq > from_seq:
                yield event
                if follow and event.type == "permission.requested":
                    await self.permission_resolved.wait()
        if follow:
            await self.started.wait()
            for event in self.events_log:
                if event.seq > from_seq:
                    yield event

    def events(self, session, from_seq=0, *, follow=True, client_id=None):
        return self._stream(session, from_seq, follow=follow, client_id=client_id)

    async def aclose(self):
        self.closed = True


def _view(events):
    from nexus.view import apply, initial_state

    view = initial_state("s")
    for event in events:
        view = apply(view, event)
    return view.to_dict()


def _event(kind, seq, data=None):
    return Event(type=kind, data=data or {}, seq=seq, session="s", turn="turn-1")


def _baseline_events():
    return [
        _event("turn.started", 1),
        _event("model.started", 2, {"provider": "fake", "model": "m"}),
        _event("text", 3, {"text": "prior answer"}),
        _event("turn.completed", 4),
    ]


def _client(transport):
    return Client(transport)


@pytest.mark.asyncio
async def test_app_boot_reduces_baseline_and_subscribes_after_state():
    transport = FakeTransport(_baseline_events())
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        timeline = app.query_one("#conversation", ConversationTimeline)
        assert timeline.query_one(".timeline-assistant")._rendered == "prior answer"
        assert app.controller.cursor == app.controller.view.last_seq == 4
        assert app.controller.view.turns[-1].messages[-1].text == "prior answer"
        assert transport.trace.index("SessionState") < next(
            i for i, item in enumerate(transport.trace)
            if isinstance(item, tuple) and item[0] == "subscribe" and item[3] is False
        )


@pytest.mark.asyncio
async def test_pilot_multiline_stream_finalization_and_subscribe_before_start():
    events = [
        _event("turn.started", 1), _event("model.started", 2),
        _event("text.delta", 3, {"text": "Hello **"}),
        _event("text.delta", 4, {"text": "world**"}),
        _event("text", 5, {"text": "Hello **world**"}),
        _event("turn.completed", 6),
    ]
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        transport.events_log.extend(events)
        editor = app.query_one("#chat-editor", TextArea)
        editor.text = "hello\nworld"
        await pilot.press("enter")
        await pilot.pause(0.1)
        subscribe = next(i for i, row in enumerate(transport.trace) if isinstance(row, tuple) and row[0] == "subscribe" and row[1] == "s" and row[2] == 0 and row[3])
        assert subscribe < transport.trace.index("start_turn")
        assert transport.last_input == "hello\nworld"
        assert app.controller.cursor == 6


@pytest.mark.asyncio
async def test_permission_resolution_keeps_stream_attached_until_terminal():
    from nexus.ui.tui.controller import TuiController

    transport = FakeTransport()
    controller = TuiController(_client(transport), "s")
    await controller.bootstrap()
    transport.events_log.extend([
        _event("turn.started", 1),
        _event("permission.requested", 2, {"id": "p", "tool": "Write", "key": "file"}),
        _event("permission.resolved", 3, {"id": "p", "decision": "allow_once"}),
        _event("turn.completed", 4),
    ])
    posted = []

    async def receive(event):
        posted.append(event)
        if event is not None and event.type == "permission.requested":
            await controller.client.resolve_permission("s", "p", "allow_once")
        if event is not None:
            controller.ingest(event)

    controller.start_turn("go", receive)
    await asyncio.wait_for(controller._task, 1)
    assert [event.type for event in posted] == [
        "turn.started", "permission.requested", "permission.resolved", "turn.completed"
    ]
    assert controller.cursor == 4


@pytest.mark.asyncio
async def test_cancel_quit_and_connection_feedback():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.controller.running = True
        await app.action_cancel_turn()
        await pilot.pause()
        assert transport.cancelled
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.controller._closed


@pytest.mark.asyncio
async def test_composer_is_keyboard_only_without_send_or_cancel_buttons():
    from textual.widgets import Button

    from nexus.ui.tui.widgets import ChatEditor, ChatInput

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one(ChatInput)
        assert list(composer.query(Button)) == []
        assert list(app.query("#send")) == []
        assert list(app.query("#cancel")) == []
        editor = app.query_one(ChatEditor)
        assert app.focused is editor
        # Enter still submits even though no Send control exists.
        editor.text = "keyboard only"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert transport.last_input == "keyboard only"


@pytest.mark.asyncio
async def test_root_agent_picker_mouse_and_next_turn_feedback():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        for key in "plan":
            await pilot.press(key)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert app.controller.agent_name == "plan"
        assert "applies next turn" not in str(app.query_one("#connection-status").render())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "selection"),
    [("/agent", "mouse"), ("/agent list", "keyboard")],
)
async def test_agent_command_opens_picker_from_exact_editor_command(command, selection):

    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.focus()
        for key in command:
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.query_one("#inline-picker").display
        assert "AgentsList" in transport.trace
        assert transport.trace.count("AgentsList") == 2  # bootstrap and command

        if selection == "mouse":
            await pilot.click("#agent-options", offset=(2, 1))
        else:
            for key in "plan":
                await pilot.press(key)
            await pilot.pause()
            await pilot.press("enter")
        await pilot.pause(0.1)
        assert transport.agent_name == "plan"
        assert "AgentSelect" in transport.trace


@pytest.mark.asyncio
async def test_command_palette_and_migrated_chat_command_parity():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        provider_classes = [factory() for factory in NexusTextualApp.COMMANDS]
        provider_class = next(
            cls for cls in provider_classes if cls.__name__ == "ChatCommandProvider"
        )
        provider = provider_class(app.screen)
        hits = [hit async for hit in provider.search("details")]
        assert hits and "details" in str(hits[0].match_display).lower()

        await app._dispatch_chat_command("/model fake/new-model")
        await app._dispatch_chat_command("/agent plan")
        await app._dispatch_chat_command("/details")
        await app._dispatch_chat_command("/export markdown")
        await app._dispatch_chat_command("/fork")
        names = [row for row in transport.trace if isinstance(row, str)]
        assert "ModelSelect" in names and "AgentSelect" in names
        assert "SessionExport" in names and "SessionFork" in names
        assert app.controller.session == "forked"


@pytest.mark.asyncio
async def test_fresh_session_is_empty_and_editor_focused_without_agent_panel():
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(48, 24)) as pilot:
        await pilot.pause()
        timeline = app.query_one("#conversation", ConversationTimeline)
        assert [child.id for child in timeline.children] == ["context-header"]
        assert app.focused is app.query_one("#chat-editor", TextArea)
        assert not app.query("#agent-tracker")
        assert not app.query_one("#context-preview").display
        await pilot.pause()
        assert "Selected system prompt" in app.query_one("#context-prompt").render().plain
        assert "Read" in app.query_one("#context-tools").render().plain


@pytest.mark.asyncio
async def test_context_entry_opens_readable_next_turn_preview_and_is_keyboard_accessible():
    from nexus.ui.tui.widgets import ContextDetailsScreen

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="new-session")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        entry = app.query_one("#context-usage")
        assert entry.render().plain == "0 (0%)"
        await pilot.press("ctrl+i")
        await pilot.pause()
        assert isinstance(app.screen, ContextDetailsScreen)
        summary = app.screen.query_one("#context-details").render().plain
        assert "general · fake/preview-model" in summary and "Provider call: No" in summary
        assert "system ~" in summary and "tools ~" in summary and "1 turn(s)" in summary
        groups = [str(group._title.label) for group in app.screen.query("Collapsible.context-group")]
        assert [title.split("  ")[0] for title in groups] == ["System prompt", "Tools", "Turn 1", "Request details"]
        entries = [str(entry._title.label).split("  ")[0] for entry in app.screen.query("Collapsible.context-entry")]
        for expected in ("system", "soul", "memory", "Skills index", "MCP index", "Full system prompt",
                         "Read", "User message", "Accounting", "System files", "Display limitations"):
            assert expected in entries
        # Everything starts collapsed; Enter on a focused title expands it.
        assert all(group.collapsed for group in app.screen.query("Collapsible.context-group"))
        first = app.screen.query_one("Collapsible.context-group")
        first._title.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not first.collapsed
        assert "ContextInspect" in transport.trace
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ContextDetailsScreen)


@pytest.mark.asyncio
async def test_empty_context_preview_shows_ten_lines_then_full_details_with_safe_controls():
    from nexus.ui.tui.widgets import ContextDetailsScreen

    transport = FakeTransport()
    original_request = transport.request

    async def request(command):
        result = await original_request(command)
        if isinstance(command, p.ContextInspect):
            return p.ContextInspectResult(
                session=command.session,
                mode="next_turn_preview",
                actually_sent=False,
                redacted_for_display=True,
                agent={"name": "general", "source": "default", "instructions_included": True},
                provider="fake-provider",
                model="fake-model",
                system_text="first\nsecond\nthird\nfourth\nfifth\nsixth\nseventh\neighth\nninth\ntenth\neleventh\x1b[31munsafe",
                tools=[{"name": "Read", "description": "Read files", "input_schema": {"type": "object", "properties": {f"field-{index}": {"type": "string"} for index in range(12)}}}],
                messages=[{"role": "user", "blocks": [{"type": "text", "text": "\n".join(f"Request line {index}" for index in range(1, 13))}]},
                          {"role": "assistant", "blocks": [{"type": "text", "text": "Earlier reply"}]}],
                history_included=True,
                request_context={"used_tokens": 800, "input_budget": 8000},
                skills_index=[
                    {"name": "available-skill", "description": "Available", "included": False},
                    {"name": "active-skill", "description": "Included", "included": True},
                ],
                included_parts=[{"name": "long-part", "text": "\n".join(f"Part line {index}" for index in range(1, 13))}],
                mcp_index="docs-search",
                omitted=["conversation history"],
            )
        return result

    transport.request = request
    app = NexusTextualApp(_client(transport), session="new-session")
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause()
        preview = app.query_one("#context-prompt").render().plain
        assert "first" in preview and "fifth" in preview
        assert "+6 more lines" in preview
        assert "sixth" not in preview
        assert "Read" in app.query_one("#context-tools").render().plain
        assert "active-skill" in app.query_one("#context-skills").render().plain
        assert "available-skill" not in app.query_one("#context-skills").render().plain
        assert app.query_one("#context-header") in app.query_one("#conversation").children

        await pilot.click("#context-prompt")
        await pilot.pause()
        assert app.screen.query_one("#context-modal-title").render().plain == "System prompt"
        assert "eleventh" in app.screen.query_one("#context-modal-body").source
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ContextDetailsScreen)


@pytest.mark.asyncio
async def test_context_main_pane_yields_to_an_existing_conversation():
    from nexus.view import apply

    client = _client(FakeTransport())
    app = NexusTextualApp(client, session="existing")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        view = app.controller.view
        app.controller.view = apply(view, _event("turn.started", 1))
        app.controller.view = apply(app.controller.view, _event("text", 2, {"text": "Done"}))
        app.controller.view = apply(app.controller.view, _event("turn.completed", 3))
        await app._sync_timeline()
        await pilot.pause()
        # The inline preview introduces a new session; once there is a
        # conversation the chat owns the pane and Ctrl+I opens full context.
        assert not app.query_one("#context-preview").display


@pytest.mark.asyncio
async def test_agent_selection_refreshes_cached_context_request():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="selection-context")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app._context_preview is not None
        prior = app._context_preview
        before = transport.trace.count("ContextInspect")
        await app._agent_command(("plan",))
        await pilot.pause()
        assert transport.trace.count("ContextInspect") > before
        assert app._context_preview is not prior
        assert app._context_preview_session == "selection-context"


@pytest.mark.asyncio
async def test_inline_context_preview_discards_late_response_after_session_switch():
    entered_old, release_old = asyncio.Event(), asyncio.Event()
    client = _client(FakeTransport())

    async def inspect_context(session):
        if session == "old":
            entered_old.set()
            await release_old.wait()
            prompt = "stale old prompt"
        else:
            prompt = f"prompt for {session}"
        return p.ContextInspectResult(
            session=session,
            system_text=prompt,
            provider="fake",
            model=session,
        )

    client.inspect_context = inspect_context
    app = NexusTextualApp(client, session="old")
    async with app.run_test(size=(90, 30)) as pilot:
        await entered_old.wait()
        await app._switch_session("new")
        await pilot.pause()
        assert "prompt for new" in app.query_one("#context-prompt").render().plain
        release_old.set()
        await pilot.pause()
        rendered = app.query_one("#context-prompt").render().plain
        assert "prompt for new" in rendered
        assert "stale old prompt" not in rendered
        assert app.controller.session == "new"


@pytest.mark.asyncio
async def test_context_preview_refusal_shows_graceful_error_and_retry_hint():
    from nexus.ui.cli.client import ClientError
    from nexus.ui.tui.widgets import ContextDetailsScreen

    client = _client(FakeTransport())

    async def inspect_context(session):
        raise ClientError("context preview is unavailable while the session is active")

    client.inspect_context = inspect_context
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_open_context()
        await pilot.pause()
        assert isinstance(app.screen, ContextDetailsScreen)
        rendered = app.screen.query_one("#context-details").render().plain
        assert "Preview unavailable" in rendered
        assert "session is active" in rendered
        assert "Try again after the active turn finishes" in rendered
        assert "Actually sent" not in rendered


@pytest.mark.asyncio
async def test_empty_turn_render_lines_are_blank_and_current_failed_turn_shows_error():
    from textual.geometry import Region
    from textual.widgets import Static

    from nexus.view import apply, initial_state

    view = apply(initial_state("s"), _event("turn.started", 1))
    app = NexusTextualApp(_client(FakeTransport()))
    # Tall enough for the new-session context preview plus the timeline.
    async with app.run_test(size=(60, 40)) as pilot:
        await pilot.pause()
        timeline = app.query_one("#conversation", ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()
        turn_widget = timeline._turns["turn-1"]
        area = Region(0, 0, turn_widget.size.width, turn_widget.size.height)
        active_lines = turn_widget.render_lines(area)
        assert not any(line.text.strip() for line in active_lines)

        view = apply(
            view,
            _event("input.queued", 2, {
                "queued_id": "queued-1",
                "content": [{"type": "text", "text": "keep my prompt"}],
            }),
        )
        view = apply(
            view,
            _event("input.consumed", 3, {"queued_id": "queued-1", "turn": "turn-1"}),
        )
        error = "ConfigError: No model was specified and no default model is configured"
        view = apply(view, _event("turn.failed", 4, {"error": error}))
        await timeline.set_view(view)
        await pilot.pause()

        turn_widget = timeline._turns["turn-1"]
        area = Region(0, 0, turn_widget.size.width, turn_widget.size.height)
        failed_lines = turn_widget.render_lines(area)
        assert not any("turn-1" in line.text for line in failed_lines)
        error_widget = turn_widget.query_one(".timeline-error", Static)
        error_area = Region(0, 0, error_widget.size.width, error_widget.size.height)
        rendered_error = " ".join(
            "".join(line.text for line in error_widget.render_lines(error_area)).strip("").replace("▏", "").replace("▎", "").replace("▌", "").split()
        )
        assert rendered_error == f"Error: {error}"
        user_message = turn_widget.query_one(".timeline-user", Static)
        rendered_user = user_message.render().plain
        assert "keep my prompt" in rendered_user


@pytest.mark.asyncio
async def test_completed_turn_summary_uses_frozen_replayed_metadata_and_is_idempotent():
    from textual.widgets import Static

    from nexus.view import apply_many, initial_state

    events = [
        Event(
            type="turn.started",
            data={"agent": {"name": "plan"}},
            seq=1,
            ts=10.0,
            session="s",
            turn="summary-turn",
        ),
        Event(
            type="input.started",
            data={"input_id": "input-1", "content": [{"type": "text", "text": "prompt"}]},
            seq=2,
            ts=10.0,
            session="s",
            turn="summary-turn",
        ),
        Event(
            type="model.started",
            data={"provider": "saved-provider", "model": "saved-model", "reasoning_effort": "high"},
            seq=3,
            ts=11.0,
            session="s",
            turn="summary-turn",
        ),
        Event(
            type="text",
            data={"text": "done"},
            seq=4,
            ts=12.0,
            session="s",
            turn="summary-turn",
        ),
        Event(
            type="turn.completed",
            data={},
            seq=5,
            ts=13.0,
            session="s",
            turn="summary-turn",
        ),
        # A later selection must not be mistaken for the completed turn's
        # frozen metadata.
        Event(
            type="model.selected",
            data={"provider": "later-provider", "model": "later-model"},
            seq=6,
            ts=14.0,
            session="s",
        ),
        Event(
            type="agent.selected",
            data={"name": "later-agent"},
            seq=7,
            ts=15.0,
            session="s",
        ),
        Event(
            type="reasoning_effort.selected",
            data={"effort": "low"},
            seq=8,
            ts=16.0,
            session="s",
        ),
    ]
    pending_view = apply_many(initial_state("s"), events[:3])
    view = apply_many(initial_state("s"), events)
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(pending_view)
        await pilot.pause()
        assert not timeline._turns["summary-turn"].query(".timeline-summary")

        await timeline.set_view(view)
        await pilot.pause()

        summary = timeline._turns["summary-turn"].query_one(
            ".timeline-summary", Static
        ).render().plain
        # The agent is shown in the composer, not repeated per turn.
        assert summary == "saved-model · 2.0s"
        assert "later-model" not in summary
        assert "Elapsed" not in summary
        assert "later-agent" not in summary
        assert " low" not in summary

        # Re-rendering the same replayed state updates the single summary card
        # instead of duplicating it.
        await timeline.set_view(view)
        await pilot.pause()
        assert len(timeline._turns["summary-turn"].query(".timeline-summary")) == 1


@pytest.mark.asyncio
async def test_completed_turn_summary_omits_unknown_duration_and_describes_unset_metadata():
    from textual.widgets import Static

    from nexus.view import apply_many, initial_state

    events = [
        Event(
            type="turn.started", data={}, seq=1, ts=100.0, session="s", turn="missing-ts"
        ),
        Event(
            type="input.started",
            data={"input_id": "input-1", "content": [{"type": "text", "text": "prompt"}]},
            seq=2,
            ts=float("nan"),
            session="s",
            turn="missing-ts",
        ),
        Event(
            type="model.started",
            data={},
            seq=3,
            ts=102.0,
            session="s",
            turn="missing-ts",
        ),
        Event(
            type="text", data={"text": "answer"}, seq=4, ts=float("nan"), session="s", turn="missing-ts"
        ),
        Event(
            type="turn.completed", data={}, seq=5, ts=145.0, session="s", turn="missing-ts"
        ),
    ]
    view = apply_many(initial_state("s"), events)
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()

        summary = timeline._turns["missing-ts"].query_one(
            ".timeline-summary", Static
        ).render().plain
        assert "Elapsed" not in summary
        assert "45.0s" not in summary and "44.0s" not in summary
        assert summary.strip() == ""


@pytest.mark.asyncio
async def test_superseded_setup_errors_and_pre_prompt_greetings_are_hidden_only_in_timeline():
    from nexus.view import apply, initial_state

    view = initial_state("s")
    seq = 0

    def emit(kind, turn_id, data=None):
        nonlocal seq, view
        seq += 1
        view = apply(
            view,
            Event(type=kind, data=data or {}, seq=seq, session="s", turn=turn_id),
        )

    # This exact canned assistant greeting is historical setup noise only
    # because it precedes the first submitted prompt.
    emit("turn.started", "welcome")
    emit("text", "welcome", {"text": "Hi! How can I help?"})
    emit("turn.completed", "welcome")

    failed_prompts = {
        "config": "Please search my notes for credentials and unauthorized access.",
        "provider": "Explain why the credentials document says unauthorized.",
    }
    for turn_id, error in (
        ("config", "ConfigError: No model was specified"),
        ("provider", "ProviderError: authentication_error"),
    ):
        emit("turn.started", turn_id)
        emit("input.queued", turn_id, {
            "queued_id": f"q-{turn_id}",
            "content": [{"text": failed_prompts[turn_id]}],
        })
        emit("input.consumed", turn_id, {"queued_id": f"q-{turn_id}", "turn": turn_id})
        emit("tool.requested", turn_id, {
            "call_id": f"preview-{turn_id}",
            "tool": "Read",
            "input": {"path": "notes.txt"},
        })
        emit("tool.completed", turn_id, {
            "call_id": f"preview-{turn_id}",
            "tool": "Read",
            "result": {
                "content": [{"type": "text", "text": "credentials reference: unauthorized"}],
                "display": "credentials reference: unauthorized",
            },
        })
        emit("text", turn_id, {"text": "I’m Nexus, your assistant. I found the requested note."})
        emit("turn.failed", turn_id, {"error": error})

    emit("turn.started", "fixed")
    emit("input.queued", "fixed", {"queued_id": "q-fixed", "content": [{"text": "real request"}]})
    emit("input.consumed", "fixed", {"queued_id": "q-fixed", "turn": "fixed"})
    emit("text", "fixed", {"text": "I’m Nexus, ready to help with the real request."})
    emit("turn.completed", "fixed")

    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(72, 26)) as pilot:
        await pilot.pause()
        timeline = app.query_one(ConversationTimeline)
        await timeline.set_view(view)
        await pilot.pause()
        assert len(view.turns) == len(timeline._turns) == 4
        messages = [
            widget for turn in timeline._turns.values() for widget in turn._items.values()
        ]
        rendered = "\n".join(
            getattr(widget, "_rendered", str(widget.render())) for widget in messages
        )
        assert "ConfigError" not in rendered and "ProviderError" not in rendered
        assert "Hi! How can I help?" not in rendered
        assert "I’m Nexus, your assistant. I found the requested note." in rendered
        assert "I’m Nexus, ready to help" in rendered
        for prompt in failed_prompts.values():
            assert prompt in rendered
        for turn_id in ("config", "provider"):
            tool = timeline._turns[turn_id]._items[f"tool:preview-{turn_id}"]
            assert "credentials reference: unauthorized" in str(
                tool.query_one("#tool-header").render()
            )
        assert view.turns[1].error and view.turns[2].error
        # The canonical reducer still owns every failed turn for inspection/export.
        assert [turn.phase for turn in view.turns] == ["completed", "failed", "failed", "completed"]
        from nexus.ui.cli.details import detail_lines
        details = "\n".join(detail_lines("s", view))
        assert "ConfigError: No model was specified" in details
        assert "ProviderError: authentication_error" in details


@pytest.mark.asyncio
async def test_unresolved_error_event_stays_in_status_and_details():
    from nexus.ui.cli.details import detail_lines
    from nexus.view import apply, initial_state

    event = Event(
        type="error",
        data={"message": "ProviderError: live authentication rejected"},
        seq=1,
        session="s",
    )
    view = apply(initial_state("s"), event)
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._event_received(EventReceived(event, None))
        status = str(app.query_one("#connection-status").render())
        assert "ProviderError" in status and "live authentication rejected" in status
        assert "ProviderError" in "\n".join(detail_lines("s", view))


@pytest.mark.asyncio
async def test_switch_session_detaches_an_active_prior_stream():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.controller.start_turn("old", app._post_event)
        await transport.started.wait()
        await app._switch_session("other")
        transport.events_log.extend([_event("text", 1, {"text": "old session text"})])
        await pilot.pause()
        assert app.controller.session == "other"
        assert app.controller.view.session_id == "other"
        assert not app.controller.view.messages


@pytest.mark.asyncio
async def test_subagent_page_mirrors_the_root_with_its_sent_context():
    from nexus.ui_support.tui_context_header import ContextBlock
    from nexus.ui_support.tui_panels import DetailsSidebar, TopBar
    from nexus.view import AgentView

    context = {
        "mode": "sent_request",
        "agent": {"name": "quick", "color": "#14B8A6", "source": "subagent"},
        "system_text": "You are Quick, a fast worker.",
        "tools": [{"name": "read", "description": "Read a file", "input_schema": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
        "tools_supported": True,
        "messages": [{"role": "user", "blocks": [{"type": "text", "text": "Read README.md and summarize it."}]}],
        "unknown_future_field": 1,
    }
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        agent = AgentView(id="child", type="quick", task="read a file", model="codex/gpt-6-luna")
        screen = AgentTranscriptScreen(agent, context)
        app.push_screen(screen)
        await pilot.pause(0.2)
        # A full page with the root's chrome, not a dialog.
        assert screen.query_one(TopBar) and screen.query_one(DetailsSidebar).display
        assert "quick" in str(screen.query_one("#topbar-crumb").render())
        prompt = screen.query_one("#context-prompt", ContextBlock)
        tools = screen.query_one("#context-tools", ContextBlock)
        assert "You are Quick" in prompt.detail
        assert "read — Read a file" in tools.detail and "path" in tools.detail
        assert tools.result is not None and tools.result.tools[0]["input_schema"]["required"] == ["path"]
        assert "gpt-6-luna" in str(screen.query_one("#details-session").render())
        # System prompt, then the Task the root agent wrote, then Tools.
        task = screen.query_one("#context-task", ContextBlock)
        assert task.display and task.has_class("-task")
        assert "Read README.md and summarize it." in task.body
        order = [block.id for block in screen.query(ContextBlock)]
        assert order[:3] == ["context-prompt", "context-task", "context-tools"]
        # The root conversation has no Task block.
        assert not app.screen_stack[0].query_one("#context-task", ContextBlock).display


@pytest.mark.asyncio
async def test_inspector_tail_follow_obeys_user_scroll_state():
    from textual.worker import WorkerCancelled

    from nexus.ui.tui.timeline import ConversationTimeline
    from nexus.view import AgentView, BlockView, ConversationView, MessageView, TurnView

    body = ConversationView(
        session_id="agent",
        turns=[TurnView(id=f"t{i}", messages=[MessageView(role="assistant", blocks=[BlockView(text=f"line {i}")])]) for i in range(40)],
    )
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AgentTranscriptScreen(AgentView(id="agent", body=body))
        app.push_screen(screen)
        await pilot.pause(0.2)
        timeline = screen.query_one("#agent-timeline", ConversationTimeline)
        assert screen.scroll_at_bottom
        # Repeated inspector refreshes replace exclusive workers. Grow the
        # transcript during that churn so newly mounted turns exercise the
        # same reconciliation path as live child-agent updates.
        workers = []
        for count in range(41, 48):
            refreshed = ConversationView(
                session_id="agent",
                turns=[
                    TurnView(
                        id=f"t{i}",
                        messages=[
                            MessageView(
                                role="assistant",
                                blocks=[BlockView(text=f"line {i}")],
                            )
                        ],
                    )
                    for i in range(count)
                ],
            )
            screen.refresh_agent(AgentView(id="agent", body=refreshed))
            workers.append(screen._timeline_worker)
            await pilot.pause(0.01)
        for worker in workers:
            if worker is None:
                continue
            try:
                await worker.wait()
            except WorkerCancelled:
                pass
        timeline.scroll_home(animate=False)
        await pilot.pause(0.1)
        screen.refresh_agent(AgentView(id="agent", body=body))
        assert screen._timeline_worker is not None
        await screen._timeline_worker.wait()
        assert timeline.scroll_y == 0 and not screen.scroll_at_bottom

        screen.refresh_agent(
            AgentView(
                id="agent",
                body=ConversationView(
                    session_id="agent",
                    turns=[
                        TurnView(
                            id=f"unmount-{i}",
                            messages=[
                                MessageView(
                                    role="assistant",
                                    blocks=[BlockView(text=f"line {i}")],
                                )
                            ],
                        )
                        for i in range(200)
                    ],
                ),
            )
        )
        unmount_worker = screen._timeline_worker
        await pilot.pause(0)
        app.pop_screen()
        if unmount_worker is not None:
            try:
                await unmount_worker.wait()
            except WorkerCancelled:
                pass


@pytest.mark.asyncio
async def test_permission_attended_reply_and_first_responder_loss():
    transport = FakeTransport()
    transport.permission_wins = False
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event("permission.requested", 1, {"id": "p", "tool": "Write", "key": "file"})))
        await pilot.pause()
        assert app.screen.__class__.__name__ == "PermissionScreen"
        await pilot.press("n")
        await pilot.pause()
        assert transport.permission_decision == "deny_once"
        assert "Another view answered first" in str(app.query_one("#connection-status").render())


@pytest.mark.asyncio
async def test_permission_modal_shows_all_multi_target_paths():
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    targets = [
        {"role": "source", "path": f"src/{index:02}.py", "reason": "Read required."}
        for index in range(64)
    ]
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event(
            "permission.requested", 1, {"id": "many", "tool": "Move", "targets": targets}
        )))
        await pilot.pause()
        description = app.screen.query_one("#prompt-body", Static).render().plain
        assert "targets (64)" in description
        for target in targets:
            assert target["path"] in description
        scroll = app.screen.query_one("#prompt-body-scroll", VerticalScroll)
        assert scroll.max_scroll_y > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "targets",
    [
        [{"role": "source", "path": "src/a.py"}],
        [{"role": "source", "path": "p" * 4097, "reason": "Read required."}],
    ],
    ids=["incomplete", "huge-path"],
)
@pytest.mark.parametrize("key", ["y", "a"])
async def test_permission_unavailable_targets_fail_closed_for_keyboard_allow(targets, key):
    from textual.widgets import Static

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event(
            "permission.requested", 1,
            {"id": "invalid-targets", "tool": "Move", "targets": targets},
        )))
        await pilot.pause()
        screen = app.screen
        description = screen.query_one("#prompt-body", Static).render().plain
        assert "targets: unavailable" in description
        assert "approval: unavailable; this request will be denied" in description
        assert [c.disabled for c in screen.choices] == [True, True, False, False]

        await pilot.press(key)
        await pilot.pause()
        assert transport.permission_decision == "deny_once"


@pytest.mark.asyncio
async def test_permission_unavailable_targets_preserve_explicit_deny():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event(
            "permission.requested", 1,
            {"id": "invalid-targets", "tool": "Move", "targets": []},
        )))
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert transport.permission_decision == "deny_once"


@pytest.mark.asyncio
async def test_permission_valid_complete_multi_target_list_allows_normal_choice():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    targets = [
        {"role": "source", "path": "src/a.py", "reason": "Read required."},
        {"role": "destination", "path": "src/b.py", "reason": "Write required."},
    ]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event(
            "permission.requested", 1,
            {"id": "valid-targets", "tool": "Move", "targets": targets},
        )))
        await pilot.pause()
        assert not any(choice.disabled for choice in app.screen.choices)
        await pilot.press("a")
        await pilot.pause()
        assert transport.permission_decision == "allow_always"


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_input", "keys", "answer"), [
    ({"question": "Which DB?", "options": ["Postgres", "SQLite"]}, ["2"], "2"),
    ({"question": "Name it?"}, ["n", "o", "v", "a", "enter"], "nova"),
])
async def test_question_opens_list_prompt_and_answers_by_call_id(tool_input, keys, answer):
    from nexus.ui.tui.permission import QuestionScreen

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event("tool.requested", 1, {"call_id": "q1", "tool": "question", "input": tool_input})))
        app.post_message(EventReceived(_event("tool.started", 2, {"call_id": "q1", "tool": "question"})))
        await pilot.pause()
        assert isinstance(app.screen, QuestionScreen)
        assert tool_input["question"] in app.screen.query_one("#prompt-body").render().plain
        for key in keys:
            await pilot.press(key)
        await pilot.pause()
        assert transport.question_answer == ("q1", answer)
        assert not isinstance(app.screen, QuestionScreen)


@pytest.mark.asyncio
async def test_permission_scalar_approval_still_allows_once():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event(
            "permission.requested", 1,
            {"id": "scalar", "tool": "Write", "key": "file"},
        )))
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert transport.permission_decision == "allow_once"


@pytest.mark.asyncio
async def test_task_details_include_child_agents_and_modal_can_open_child():
    from nexus.view import apply, initial_state

    view = initial_state("s")
    view = apply(view, _event("turn.started", 1))
    view = apply(view, _event("tool.requested", 2, {"call_id": "task-1", "tool": "Task", "input": {"task": "inspect"}}))
    view = apply(view, _event("agent.spawned", 3, {"agent": {"id": "a", "parent": "s", "parent_call_id": "task-1", "root_turn_id": "turn-1", "type": "explore", "task": "inspect", "session": "a"}}))
    view = apply(view, _event("agent.spawned", 4, {"agent": {"id": "b", "parent": "s", "parent_call_id": "task-1", "root_turn_id": "turn-1", "type": "build", "task": "implement", "session": "b"}}))
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.controller.view = view
        await app._sync_timeline()
        card = app.query_one(TaskActivityWidget)
        details = card._details_text()
        assert "explore" in details and "build" in details
        await card.open_details()  # a spawned Task opens its sub agent page directly
        await pilot.pause()
        assert app.screen.__class__.__name__ == "AgentTranscriptScreen"


@pytest.mark.asyncio
async def test_child_modal_renders_with_the_root_timeline_widgets():
    from textual.app import App

    from nexus.ui.tui.timeline import ToolActivityWidget, TurnWidget
    from nexus.view import (
        AgentView,
        BlockView,
        ConversationView,
        MessageView,
        ToolCallView,
        TurnView,
    )

    body = ConversationView(turns=[TurnView(
        id="child",
        messages=[MessageView(role="assistant", blocks=[BlockView(kind="text", text="child answer")])],
        tools=[ToolCallView(call_id="w", name="Write", status="failed", input={"secret": "x" * 6000}, error="failed")],
    )])
    async with App().run_test() as pilot:
        screen = AgentTranscriptScreen(AgentView(id="a", body=body))
        await pilot.app.push_screen(screen)
        await pilot.pause(0.2)
        assert screen.query(TurnWidget)
        (tool,) = screen.query(ToolActivityWidget)
        assert tool.has_class("-failed")
        assert "x" * 6000 not in " ".join(str(w.render()) for w in screen.query("#agent-timeline Static"))


@pytest.mark.asyncio
async def test_child_transcript_does_not_leak_write_content():
    from textual.app import App

    from nexus.view import AgentView, ConversationView, ToolCallView, TurnView

    secret = "do-not-render-this-write-payload"
    agent = AgentView(
        id="a",
        body=ConversationView(turns=[TurnView(
            id="child", tools=[ToolCallView(
                call_id="w", name="Write", status="completed", input={"path": "secret.txt", "content": secret},
                display=secret, result=[{"type": "text", "text": secret}],
            )]
        )]),
    )
    async with App().run_test() as pilot:
        screen = AgentTranscriptScreen(agent)
        await pilot.app.push_screen(screen)
        await pilot.pause(0.2)
        rendered = " ".join(str(w.render()) for w in screen.query("#agent-timeline Static"))
        assert "secret.txt" in rendered
        assert secret not in rendered


def test_inline_tool_formatters_are_bounded_and_literal():
    from nexus.view import ToolCallView

    write = ToolCallView(name="Write", input={"path": "x.md", "content": "secret\nvalue"})
    bash = ToolCallView(name="Bash", input={"command": "printf '[bold]x[/bold]'"}, display="[red]literal[/red]\x1b[31m")
    unknown = ToolCallView(name="mcp__x__y", input={"x": "[unsafe]"}, display="result")
    assert format_arguments(write) == "x.md (2 lines)"
    assert "secret" not in format_arguments(write)
    assert "[bold]" in format_arguments(bash)
    assert "[unsafe]" in format_arguments(unknown)


@pytest.mark.asyncio
async def test_tool_card_lifecycle_updates_in_place_and_diff_falls_back_narrow():
    from textual.widgets import Static

    from nexus.view import ToolCallView

    app = NexusTextualApp(_client(FakeTransport()))
    tool = ToolCallView(call_id="e", name="Edit", status="running", input={"path": "f.py"})
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        card = ToolActivityWidget(tool)
        await app.query_one("#conversation", ConversationTimeline).mount(card)
        await pilot.pause()
        await card.set_tool(ToolCallView(
            call_id="e", name="Edit", status="completed", duration_ms=7,
            diff={"path": "f.py", "hunk": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new", "added_lines": 1, "removed_lines": 1, "truncated": True},
        ))
        await card.open_details()
        from nexus.ui.tui.tool_details import ToolDetailsScreen

        assert isinstance(app.screen, ToolDetailsScreen)
        details = str(app.screen.query_one("#tool-details-body", Static).render())
        assert "@@ -1 +1 @@" in details and "-old" in details and "+new" in details


@pytest.mark.asyncio
async def test_tool_diff_is_mounted_inline_and_stays_available_in_modal():
    from textual.widgets import Static

    from nexus.view import ToolCallView

    app = NexusTextualApp(_client(FakeTransport()))
    tool = ToolCallView(
        call_id="e", name="Edit", status="completed",
        diff={"path": "f.py", "hunk": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new"},
    )
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        card = ToolActivityWidget(tool)
        await app.query_one("#conversation", ConversationTimeline).mount(card)
        await pilot.pause()
        await card.set_tool(tool)
        from textual_diff_view import DiffView

        (view,) = card.query(DiffView)
        assert (view.path_modified, view.code_original, view.code_modified) == ("f.py", "old", "new")
        await card.open_details()
        await pilot.pause()
        details = str(app.screen.query_one("#tool-details-body", Static).render())
        assert "@@ -1 +1 @@" in details and "-old" in details and "+new" in details


@pytest.mark.asyncio
async def test_shift_tab_cycles_host_root_agents_and_wraps_from_active_editor():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is app.query_one("#chat-editor", TextArea)
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.controller.agent_name == "plan"
        for _ in range(4):
            await pilot.press("shift+tab")
        await pilot.pause()
        assert app.controller.agent_name == "general"
        assert transport.trace.count("AgentSelect") == 5


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX driver only")
def test_app_installs_the_terminal_key_protocol_driver():
    from nexus.ui.tui.keys import NexusDriver

    app = NexusTextualApp(_client(FakeTransport()))
    assert app.driver_class is NexusDriver


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX driver only")
def test_app_get_driver_class_honors_textual_driver_override(monkeypatch):
    # An explicit TEXTUAL_DRIVER must win over the Nexus key-protocol driver, as
    # it does for Textual's own App.get_driver_class.
    from textual import constants

    from nexus.ui.tui.keys import NexusDriver

    app = NexusTextualApp(_client(FakeTransport()))
    assert app.get_driver_class() is NexusDriver
    monkeypatch.setattr(
        constants, "DRIVER", "textual.drivers.headless_driver:HeadlessDriver"
    )
    assert app.get_driver_class().__name__ == "HeadlessDriver"


def test_cli_chat_textual_only_tty_guard_and_noninteractive_run():
    from nexus.cli import build_parser, main

    parser = build_parser()
    assert parser.parse_args(["chat"]).session is None  # a new session per launch

    class NonTTY(io.StringIO):
        def isatty(self):
            return False

    import nexus.cli as cli_module
    old_in, old_out, old_err = cli_module.sys.stdin, cli_module.sys.stdout, cli_module.sys.stderr
    try:
        cli_module.sys.stdin = NonTTY()
        cli_module.sys.stdout = NonTTY()
        err = NonTTY()
        cli_module.sys.stderr = err
        assert main(["chat"]) == 2
        assert "nexus run" in err.getvalue()
    finally:
        cli_module.sys.stdin, cli_module.sys.stdout, cli_module.sys.stderr = old_in, old_out, old_err


def test_cli_chat_reports_missing_textual_clearly(monkeypatch):
    import nexus.cli as cli_module

    class TTY(io.StringIO):
        def isatty(self):
            return True

    err = TTY()
    monkeypatch.setattr(cli_module.sys, "stdin", TTY())
    monkeypatch.setattr(cli_module.sys, "stdout", TTY())
    monkeypatch.setattr(cli_module.sys, "stderr", err)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(cli_module.importlib.util, "find_spec", lambda name: None)
    assert cli_module.main(["chat"]) == 1
    assert "Textual is required" in err.getvalue()


def test_cli_chat_tty_launch_returns_textual_exit_status(monkeypatch):
    import nexus.cli as cli_module

    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli_module.sys, "stdin", TTY())
    monkeypatch.setattr(cli_module.sys, "stdout", TTY())
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(cli_module.importlib.util, "find_spec", lambda name: object())
    seen = []
    monkeypatch.setattr(cli_module, "_chat_entry", lambda workspace, *, session: seen.append((workspace, session)) or 0)
    assert cli_module.main(["chat", "--session", "work"]) == 0
    assert seen and seen[0][1] == "work"


@pytest.mark.asyncio
async def test_textual_ctrl_c_cancels_and_quit_is_safe():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.controller.running = True
        app.controller._task = asyncio.create_task(asyncio.sleep(10))
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert transport.cancelled
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.controller._closed
        assert transport.closed


@pytest.mark.asyncio
async def test_idle_ctrl_c_clears_draft_then_requires_second_press_to_quit():
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one("#chat-editor", TextArea)
        editor.text = "draft"
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert editor.text == ""
        assert not app.controller._closed
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert "Press ctrl+c again" in app.query_one("#connection-status").render().plain
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.controller._closed


def test_textual_package_css_and_base_cli_import_seam():
    css = importlib.resources.files("nexus.ui.tui").joinpath("app.tcss")
    assert css.is_file()
    assert "#conversation" in css.read_text(encoding="utf-8")
    assert "#agent-tracker" not in css.read_text(encoding="utf-8")
    process = subprocess.run(
        [sys.executable, "-c", "import sys, nexus.ui.cli; assert 'textual' not in sys.modules"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stderr


def test_opencode_visual_shell_tokens_and_fixture_states_are_available():
    css = importlib.resources.files("nexus.ui.tui").joinpath("app.tcss").read_text(encoding="utf-8")
    # Colors come from theme variables so dark and light themes both apply.
    assert "$nx-accent" in css and "$nx-purple" in css and "$nx-success" in css
    from nexus.ui.tui.theme import NEXUS_DARK, NEXUS_LIGHT
    for theme in (NEXUS_DARK, NEXUS_LIGHT):
        assert {"nx-accent", "nx-purple", "nx-success", "nx-bg"} <= set(theme.variables)
    import runpy

    VisualDemoApp = runpy.run_path(
        Path(__file__).with_name("visual_tui_demo.py")
    )["VisualDemoApp"]
    for state in ("empty", "transcript", "permission", "picker"):
        app = VisualDemoApp(state)
        assert app.visual_state == state


def test_wheel_includes_textual_css_and_base_dependency(tmp_path):
    import shutil
    import zipfile

    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(root / "nexus", source / "nexus", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(root / "README.md", source / "README.md")
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from setuptools.build_meta import build_wheel; print(build_wheel(sys.argv[1]))",
            str(tmp_path),
        ],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    wheel = tmp_path / process.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(wheel) as archive:
        assert "nexus/ui/tui/app.tcss" in archive.namelist()
        assert "nexus/ui/cli/app.py" not in archive.namelist()
        assert "nexus/ui/cli/keys.py" not in archive.namelist()
        assert "nexus/ui/cli/theme.py" not in archive.namelist()
        assert "nexus/ui/cli/completion.py" not in archive.namelist()
        assert "nexus/ui/cli/keys.py" not in archive.namelist()
        assert "nexus/ui/cli/app.py" not in archive.namelist()
        metadata_path = next(name for name in archive.namelist() if name.endswith("METADATA"))
        metadata = archive.read(metadata_path).decode()
        assert "Requires-Dist: textual" in metadata
        assert "Requires-Dist: textual-diff-view" in metadata
        assert "prompt-toolkit" not in metadata

    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(wheel)
    installed_import = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.resources; from nexus.cli import build_parser; assert build_parser().prog == 'nexus'; assert importlib.resources.files('nexus.ui.tui').joinpath('app.tcss').is_file()",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed_import.returncode == 0, installed_import.stderr


def test_textual_command_palette_uses_command_registry():
    from nexus.ui.cli import commands

    assert commands.BY_NAME["/details"].summary
    providers = {factory().__name__ for factory in NexusTextualApp.COMMANDS}
    assert "ChatCommandProvider" in providers
    assert "ShortcutsCommandProvider" in providers


@pytest.mark.asyncio
async def test_enter_sends_shift_enter_newlines_and_editor_clears():
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        assert app.focused is editor
        editor.text = "first line"
        await pilot.press("shift+enter")
        editor.insert("second line")
        await pilot.pause()
        assert "\n" in editor.text and transport.last_input is None
        editor.text = "send\nme"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert transport.last_input == "send\nme"
        assert editor.text == ""
        assert app.focused is editor


@pytest.mark.asyncio
async def test_enter_while_turn_running_restores_draft_and_shows_not_sent_notice():
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "keep this unsent draft"
        app.controller.running = True
        before = list(transport.trace)

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert editor.text == "keep this unsent draft"
        assert "start_turn" not in transport.trace
        assert "SessionStart" not in transport.trace
        assert "SessionEnqueue" not in transport.trace
        assert transport.trace == before
        assert "Turn running · message not sent" in app.query_one(
            "#connection-status"
        ).render().plain


@pytest.mark.asyncio
async def test_empty_enter_does_not_submit_or_insert_newline():
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        await pilot.press("enter")
        await pilot.pause()
        assert editor.text == ""
        assert transport.last_input is None


@pytest.mark.asyncio
async def test_shift_enter_keeps_existing_draft_across_a_newline():
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "alpha"
        editor.move_cursor((0, 5))
        await pilot.press("shift+enter")
        editor.insert("beta")
        await pilot.pause()
        assert editor.text == "alpha\nbeta"
        assert transport.last_input is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effective", "expected"),
    [("high", "low"), (None, "low")],
)
async def test_ctrl_t_cycles_root_reasoning_effort_and_preserves_draft(effective, expected):
    from nexus.ui.tui.widgets import ChatEditor

    client = _client(FakeTransport())
    current_calls = 0
    selected = []
    metadata = SimpleNamespace(
        name="general",
        source="default",
        color=None,
        provider="fake",
        model="m",
        reasoning_effort=effective,
        supported_levels=["low", "high"],
        stored_override=effective,
        reasoning_effort_source="session" if effective else None,
        thinking_budget=None,
    )

    async def current_agent(session):
        nonlocal current_calls
        current_calls += 1
        return metadata

    async def select_reasoning_effort(session, effort):
        selected.append((session, effort))
        metadata.reasoning_effort = effort
        metadata.stored_override = effort
        metadata.reasoning_effort_source = "session"
        return SimpleNamespace(effective_effort=effort)

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "keep this draft"
        editor.move_cursor((0, 5))
        await pilot.press("ctrl+t")
        await pilot.pause()

        # Ctrl+T cycles in place; the picker belongs to /effort.
        assert editor.text == "keep this draft"
        assert not app.query_one("#inline-picker").display
        assert app.focused is editor
        assert selected == [("s", expected)]
        assert current_calls == 3  # bootstrap, cycle query, then metadata refresh
        assert app.controller.reasoning_effort == expected
        assert expected in str(app.query_one("#root-agent").summary())
        assert "applies next turn" not in str(app.query_one("#connection-status").render())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/effort", "/reasoning"])
async def test_effort_command_opens_picker_and_selects(command):
    from textual.widgets import OptionList

    from nexus.ui.tui.widgets import ChatEditor

    client = _client(FakeTransport())
    selected = []
    metadata = SimpleNamespace(
        name="general", supported_levels=["low", "high"], reasoning_effort="low"
    )

    async def current_agent(session):
        return metadata

    async def select_reasoning_effort(session, effort):
        selected.append((session, effort))
        metadata.reasoning_effort = effort

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        await app._dispatch_chat_command(command)
        await pilot.pause()
        assert app.query_one("#inline-picker").display
        assert app.focused is app.query_one("#agent-options", OptionList)
        assert selected == []
        await pilot.press("down", "enter")
        await pilot.pause()
        assert selected == [("s", "high")]
        assert app.focused is editor

        await app._dispatch_chat_command(f"{command} low")
        await pilot.pause()
        assert selected == [("s", "high"), ("s", "low")]
        await app._dispatch_chat_command(f"{command} extreme")
        await pilot.pause()
        assert len(selected) == 2
        assert "Use /effort low|high" in str(app.query_one("#connection-status").render())


@pytest.mark.asyncio
async def test_ctrl_t_with_no_supported_effort_leaves_status_and_host_unchanged():
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    client = _client(transport)
    client.current_agent = _async_value(
        SimpleNamespace(name="general", supported_levels=[], reasoning_effort=None)
    )
    selections = []

    async def select_reasoning_effort(session, effort):
        selections.append((session, effort))

    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "unchanged"
        status_before = str(app.query_one("#connection-status").render())
        commands_before = list(transport.trace)
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert selections == []
        assert transport.trace == commands_before
        assert editor.text == "unchanged"
        assert app.focused is editor
        assert str(app.query_one("#connection-status").render()) == status_before


@pytest.mark.asyncio
async def test_ctrl_t_reports_host_selection_errors():
    from nexus.ui.cli.client import ClientError
    from nexus.ui.tui.widgets import ChatEditor

    client = _client(FakeTransport())

    async def current_agent(session):
        return SimpleNamespace(
            name="general", supported_levels=["low", "high"], reasoning_effort="low"
        )

    async def select_reasoning_effort(session, effort):
        raise ClientError("selection rejected")

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "keep me"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert editor.text == "keep me"
        assert not app.query_one("#inline-picker").display
        assert app.focused is editor
        status = str(app.query_one("#connection-status").render())
        assert "Reasoning effort selection failed" in status
        assert "selection rejected" in status


@pytest.mark.asyncio
async def test_ctrl_t_ignores_rapid_duplicate_while_current_agent_is_delayed():
    from nexus.ui.tui.widgets import ChatEditor

    client = _client(FakeTransport())
    entered = asyncio.Event()
    release = asyncio.Event()
    selection_entered = asyncio.Event()
    release_selection = asyncio.Event()
    calls = []
    selected = []
    metadata = SimpleNamespace(
        name="general", supported_levels=["low", "high"], reasoning_effort="low"
    )

    async def current_agent(session):
        calls.append(session)
        if len(calls) > 1:
            entered.set()
            await release.wait()
        return metadata

    async def select_reasoning_effort(session, effort):
        selected.append((session, effort))
        selection_entered.set()
        await release_selection.wait()

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "draft stays"
        editor.move_cursor((0, 5))

        first = asyncio.create_task(app.action_cycle_reasoning_effort())
        await entered.wait()
        await app.action_cycle_reasoning_effort()
        assert calls == ["s", "s"]  # bootstrap plus the one in-flight cycle
        release.set()
        await selection_entered.wait()
        await app.action_cycle_reasoning_effort()
        assert len(selected) == 1
        release_selection.set()
        await first

        assert selected == [("s", "high")]
        assert editor.text == "draft stays"
        assert app.focused is editor
        assert not app._reasoning_effort_in_flight


@pytest.mark.asyncio
async def test_ctrl_t_aborts_when_session_switches_during_current_agent_lookup():
    from nexus.ui.tui.widgets import ChatEditor

    client = _client(FakeTransport())
    entered = asyncio.Event()
    release = asyncio.Event()
    selected = []

    current_calls = 0

    async def current_agent(session):
        nonlocal current_calls
        current_calls += 1
        if session == "s" and current_calls > 1:
            entered.set()
            await release.wait()
        return SimpleNamespace(
            name="general", supported_levels=["low", "high"], reasoning_effort="low"
        )

    async def select_reasoning_effort(session, effort):
        selected.append((session, effort))

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "keep draft"
        task = asyncio.create_task(app.action_cycle_reasoning_effort())
        await entered.wait()
        await app.controller.switch_session("other")
        release.set()
        await task

        assert app.controller.session == "other"
        assert selected == []
        assert not str(app.query_one("#connection-status").render()).strip()
        assert editor.text == "keep draft"
        assert app.focused is editor
        assert not app._reasoning_effort_in_flight


@pytest.mark.asyncio
async def test_ctrl_t_error_clears_in_flight_guard_for_retry():
    from nexus.ui.cli.client import ClientError

    client = _client(FakeTransport())
    attempts = 0
    selected = []

    async def current_agent(session):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ClientError("temporary lookup failure")
        return SimpleNamespace(
            name="general", supported_levels=["low", "high"], reasoning_effort="low"
        )

    async def select_reasoning_effort(session, effort):
        selected.append((session, effort))

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_cycle_reasoning_effort()
        assert not app._reasoning_effort_in_flight
        assert "temporary lookup failure" in str(
            app.query_one("#connection-status").render()
        )

        await app.action_cycle_reasoning_effort()
        assert not app._reasoning_effort_in_flight
        assert attempts == 4  # bootstrap, failed lookup, retry, metadata refresh
        assert selected == [("s", "high")]


def _async_value(value):
    async def result(*_args, **_kwargs):
        return value

    return result


@pytest.mark.asyncio
async def test_ctrl_t_is_only_handled_by_main_composer():
    from nexus.ui.tui.app import KEYBOARD_SHORTCUTS, SHORTCUTS
    from nexus.ui.tui.widgets import ChatEditor

    client = _client(FakeTransport())
    selections = []

    async def current_agent(session):
        return SimpleNamespace(
            name="general", supported_levels=["low", "high"], reasoning_effort="low"
        )

    async def select_reasoning_effort(session, effort):
        selections.append((session, effort))

    client.current_agent = current_agent
    client.select_reasoning_effort = select_reasoning_effort
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not any(binding[0] == "ctrl+t" for binding in app.BINDINGS)
        assert next(row for row in SHORTCUTS if row[0] == "ctrl+t")[1] is None
        assert "Ctrl+T" in "\n".join(KEYBOARD_SHORTCUTS)

        picker = AgentPicker([], current="general")
        app.push_screen(picker)
        await pilot.pause()
        from textual.widgets import OptionList

        picker.query_one("#agent-options", OptionList).focus()
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert selections == []

        picker.dismiss(None)
        await pilot.pause()
        assert isinstance(app.focused, ChatEditor)

        await pilot.press("ctrl+p")
        await pilot.pause()
        palette = app.screen
        palette_dialog = palette.query_one("#--container")
        assert palette_dialog.region.width < app.size.width
        assert palette_dialog.region.height < app.size.height
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert selections == []
        await pilot.press("escape")
        await pilot.pause()

        from nexus.view import AgentView
        app.push_screen(AgentTranscriptScreen(AgentView(id="child")))
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert selections == []
        app.action_back_from_agent()
        await pilot.pause()
        assert isinstance(app.focused, ChatEditor)

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert selections == [("s", "high")]
        assert not app.query_one("#inline-picker").display


async def _feed_terminal(app, sequence: str, parser=None):
    """Deliver raw terminal bytes the way the driver does after decoding.

    The bytes go through the real ``NexusXTermParser`` and the resulting events
    are posted to the app, exercising decode + focused-widget routing without a
    terminal. The PTY test in ``tests/test_tui_keys.py`` covers the OS read.
    """
    from nexus.ui.tui.keys import NexusXTermParser

    parser = NexusXTermParser() if parser is None else parser
    for event in list(parser.feed(sequence)) + list(parser.tick()):
        event.set_sender(app)
        app.post_message(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "newline_sequence",
    [
        "\x1b[13;2u",  # Kitty CSI-u Shift+Enter
        "\x1b[13;3u",  # Kitty CSI-u Alt+Enter
        "\x1b[27;2;13~",  # xterm modifyOtherKeys Shift+Enter
        "\x1b[27;3;13~",  # xterm modifyOtherKeys Alt+Enter
        "\x1b[27;5;13~",  # xterm modifyOtherKeys Ctrl+Enter
        "\n",  # legacy Ctrl+J (LF): the fallback when Shift+Enter is bare CR
    ],
)
async def test_raw_terminal_newline_keeps_draft_then_enter_sends(newline_sequence):
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        assert app.focused is editor

        await _feed_terminal(app, "alpha")
        await pilot.pause()
        await _feed_terminal(app, newline_sequence)
        await pilot.pause()
        assert editor.text == "alpha\n", "modified Enter did not insert a newline"
        assert transport.last_input is None, "a newline must not submit the draft"

        await _feed_terminal(app, "beta")
        await pilot.pause()
        assert editor.text == "alpha\nbeta"
        assert transport.last_input is None

        await _feed_terminal(app, "\r")  # Enter is always a bare CR
        await pilot.pause(0.1)
        assert transport.last_input == "alpha\nbeta"
        assert editor.text == ""
        assert app.focused is editor


@pytest.mark.asyncio
async def test_bare_carriage_return_is_indistinguishable_and_submits():
    # Documents the hard limit: a legacy terminal sends CR for both Enter and
    # Shift+Enter, so the shell must treat CR as submit; Ctrl+J is the newline.
    from nexus.ui.tui.widgets import ChatEditor

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        await _feed_terminal(app, "one line")
        await pilot.pause()
        await _feed_terminal(app, "\r")
        await pilot.pause(0.1)
        assert transport.last_input == "one line"
        assert editor.text == ""


@pytest.mark.asyncio
async def test_show_keyboard_shortcuts_provider_action_opens_full_reference():
    from textual.widgets import Static

    from nexus.ui.tui.app import KEYBOARD_SHORTCUTS, ShortcutsScreen
    from nexus.ui.tui.widgets import ChatInput

    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        # The shortcut hints are not painted anywhere on the normal screen.
        assert not app.query("#input-help")
        agent_bar = app.query_one("#root-agent")
        assert "Ctrl+" not in str(agent_bar.render())
        # They are reachable through Commands → Show keyboard shortcuts, and the
        # palette entry actually opens the reference (not just matches a query).
        provider_class = next(
            factory() for factory in NexusTextualApp.COMMANDS
            if factory().__name__ == "ShortcutsCommandProvider"
        )
        provider = provider_class(app.screen)
        hits = [hit async for hit in provider.search("shortcuts")]
        assert hits and "shortcuts" in str(hits[0].match_display).lower()

        hits[0].command()
        await pilot.pause()
        assert isinstance(app.screen, ShortcutsScreen)
        shown = str(app.screen.query_one("#shortcuts", Static).render())
        for expected in ("Enter", "Shift+Enter", "Ctrl+P", "Ctrl+N", "Ctrl+O", "Ctrl+F", "Ctrl+G"):
            assert expected in shown, f"{expected!r} missing from the shortcut reference"
        assert "Send message" in shown
        for line in KEYBOARD_SHORTCUTS:
            assert line in shown
        app.screen.dismiss(None)
        await pilot.pause()
        assert not isinstance(app.screen, ShortcutsScreen)
        assert app.query_one(ChatInput) is not None


def test_keyboard_reference_and_bindings_share_one_source():
    from nexus.ui.tui.app import KEYBOARD_SHORTCUTS, SHORTCUTS

    # The rendered reference is exactly one line per shortcut, same order, with
    # the same description; and every actionable entry is a live binding.
    assert KEYBOARD_SHORTCUTS[0] == "Keyboard shortcuts"
    rendered = KEYBOARD_SHORTCUTS[1:]
    assert len(rendered) == len(SHORTCUTS)
    for line, (key, action, description) in zip(rendered, SHORTCUTS):
        assert line.strip().startswith(key.title())
        assert line.split(maxsplit=1)[1] == description
    assert list(NexusTextualApp.BINDINGS) == [
        (key, action, description)
        for key, action, description in SHORTCUTS
        if action is not None
    ]
    # The keys the prior reference omitted are present.
    reference = "\n".join(KEYBOARD_SHORTCUTS)
    for key in ("Ctrl+N", "Ctrl+O", "Ctrl+F", "Ctrl+G"):
        assert key in reference


def test_visual_demo_functional_state_drives_real_read_edit_turn():
    import runpy

    demo = runpy.run_path(Path(__file__).with_name("visual_tui_demo.py"))
    _functional_turn = demo["_functional_turn"]
    from nexus.view import apply, initial_state

    view = initial_state("visual")
    for event in _functional_turn("read and edit a file"):
        view = apply(view, event)
    # The prompt and the assistant/tool output share one turn, in order.
    assert [turn.id for turn in view.turns] == ["turn-live"]
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[0].text == "read and edit a file"
    tools = view.tools
    assert [tool.name for tool in tools] == ["Read", "Edit"]
    assert tools[0].display == "3 lines"
    assert tools[1].diff and tools[1].diff["hunk"]


@pytest.mark.asyncio
async def test_real_backend_read_edit_enqueue_renders_user_and_tool_cards(tmp_path):
    """The real host+runtime read/edit path drives the TUI render projection.

    The browser fixtures replay a scripted transport; this test instead runs the
    real builtin Read/Edit tools against a real file through the host facade's
    queued-input path and folds the resulting protocol events through the same
    ``nexus.view`` reducer the Textual shell renders. It pins that the prompt,
    the real tool display text, and the durable Edit diff all reach the view.
    """
    from nexus.config import Config
    from nexus.config.schema import (
        AgentSection,
        ConfigV2,
        ModelSection,
        PermissionsSection,
        ToolsSection,
    )
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import (
        ScriptedProvider,
        text_response,
        tool_response,
    )
    from nexus.runtime import Runtime
    from nexus.view import fold

    target = tmp_path / "note.md"
    target.write_text("hello\nworld\n", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("read-1", "Read", {"path": "note.md"})),
        tool_response(
            ("edit-1", "Edit", {"path": "note.md", "old_string": "world", "new_string": "nexus"})
        ),
        text_response("done"),
    )
    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            tools=ToolsSection(),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    try:
        await facade.enqueue("s", "read then edit note.md")
        await facade.wait_idle(timeout=10.0)
        events = [event async for event in facade.subscribe("s", 0, follow=False)]
    finally:
        await runtime.aclose()

    # A real file was read and edited, not a replayed fixture.
    assert target.read_text(encoding="utf-8") == "hello\nnexus\n"
    view = fold(events)
    assert len(view.turns) == 1 and view.input_queue == []
    assert view.messages[0].role == "user"
    assert view.messages[0].text == "read then edit note.md"
    assert {message.role for message in view.messages[1:]} == {"assistant"}
    assert view.messages[-1].text == "done"
    tools = {tool.call_id: tool for tool in view.tools}
    assert tools["read-1"].name == "read" and tools["read-1"].status == "completed"
    assert tools["read-1"].display == "Read note.md: 2 of 2 lines"
    edit = tools["edit-1"]
    assert edit.name == "edit" and edit.status == "completed"
    assert edit.display == "Edit note.md: 1 replacement(s)"
    assert edit.diff and edit.diff["hunk"].endswith("-world\n+nexus")


@pytest.mark.asyncio
async def test_worktrees_review_and_mutation_confirmation_modal_are_host_backed():
    from nexus.ui.tui.widgets import WorktreeConfirmScreen, WorktreesScreen

    client = _client(FakeTransport())
    commands = []
    record = {
        "child_id": "child-1",
        "lifecycle": "finalized",
        "dirty": True,
        "review_id": "a" * 32,
        "digest": "b" * 64,
        "acknowledged": False,
    }
    ack_state = {"acknowledged": False}

    async def list_worktrees():
        commands.append("list")
        return SimpleNamespace(worktrees=[record], has_more=False)

    async def inspect_worktree(child_id):
        commands.append(("inspect", child_id))
        inspected_record = dict(record)
        inspected_record["acknowledged"] = ack_state["acknowledged"]
        return SimpleNamespace(child_id=child_id, status="finalized", record=inspected_record)

    async def review_worktree(child_id, *, review_id=None, cursor=0, limit=1):
        commands.append(("review", child_id, review_id, cursor, limit))
        return SimpleNamespace(
            child_id=child_id,
            status="finalized",
            review_id="a" * 32,
            digest="b" * 64,
            entries=[{"path": "src/safe.py", "change": "modified"}, {"path": "notes\x1b[2J.txt", "change": "added"}],
            diff=[{
                "path": "src/safe.py" if cursor == 0 else "notes\x1b[2J.txt",
                "patch": "@@ -1 +1 @@\n-old\n+new" if cursor == 0 else "@@ -0,0 +1 @@\n+note",
                "binary": False,
            }],
            cursor=cursor,
            has_more=cursor == 0,
        )

    async def integrate_worktree(child_id, review_id, digest, *, confirmation_token=""):
        commands.append(("integrate", child_id, review_id, digest, confirmation_token))
        return SimpleNamespace(
            child_id=child_id,
            status="requires_confirmation" if not confirmation_token else "recovery_required",
            operation="integrate",
            review_id=review_id,
            digest=digest,
            confirmation_token="host-preview-token",
            impact={"parent_clean": True, "child_dirty": True, "summary": "host preview"},
            transaction_id="txn-1",
            changed_paths=["src/safe.py"],
            error="manual recovery required" if confirmation_token else None,
        )

    async def acknowledge_worktree(child_id, review_id, digest):
        commands.append(("acknowledge", child_id, review_id, digest))
        ack_state["acknowledged"] = True
        return SimpleNamespace(child_id=child_id, status="acknowledged", review_id=review_id, digest=digest)

    async def discard_worktree(child_id, *, force=False, review_id=None, confirmation_token=""):
        commands.append(("discard", child_id, force, review_id, confirmation_token))
        return SimpleNamespace(
            child_id=child_id,
            status="requires_confirmation" if not confirmation_token else "cleanup_pending",
            operation="discard",
            review_id=review_id,
            digest=None,
            confirmation_token="discard-preview-token",
            impact={"child_dirty": True, "summary": "host discard preview"},
            transaction_id="txn-discard",
            changed_paths=[],
            error=None,
        )

    client.list_worktrees = list_worktrees
    client.inspect_worktree = inspect_worktree
    client.review_worktree = review_worktree
    client.acknowledge_worktree = acknowledge_worktree
    client.integrate_worktree = integrate_worktree
    client.discard_worktree = discard_worktree
    app = NexusTextualApp(client, session="s")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await app._dispatch_chat_command("/worktrees")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, WorktreesScreen)
        assert screen.selected_id is None
        await pilot.click("#worktree-row-0")
        await pilot.pause(0.1)
        detail = app.screen.query_one("#worktrees-detail").render().plain
        assert "lifecycle: finalized" in detail
        assert "Digest: " + "b" * 64 in detail
        assert "src/safe.py" in detail
        assert "notes\\x1b[2J.txt" in detail
        assert "@@ -1 +1 @@" in detail and "+new" in detail
        assert screen.query_one("#worktrees-next").disabled is False
        await pilot.click("#worktrees-next")
        await pilot.pause(0.1)
        detail = app.screen.query_one("#worktrees-detail").render().plain
        assert "Review cursor: 1" in detail
        assert "@@ -0,0 +1 @@" in detail and "+note" in detail
        assert ("review", "child-1", "a" * 32, 1, 8) in commands
        assert screen.query_one("#worktrees-ack").disabled is False
        # Acknowledgement has not occurred just by opening or reading a review.
        assert not any(isinstance(call, tuple) and call[0] == "acknowledge" for call in commands)
        await pilot.click("#worktrees-ack")
        await pilot.pause(0.1)
        assert ("acknowledge", "child-1", "a" * 32, "b" * 64) in commands
        assert screen.query_one("#worktrees-integrate").disabled is False
        await pilot.click("#worktrees-integrate")
        await pilot.pause(0.1)
        assert isinstance(app.screen, WorktreeConfirmScreen)
        modal_text = app.screen.query_one("#worktree-confirm-description").render().plain
        assert "Operation: integrate" in modal_text
        assert "parent_clean: True" in modal_text
        assert "src/safe.py" in modal_text
        assert "Confirm" in app.screen.query_one("#worktree-confirm-accept").label.plain
        assert commands[-1] == ("integrate", "child-1", "a" * 32, "b" * 64, "")

        await pilot.click("#worktree-confirm-accept")
        await pilot.pause(0.2)
        assert ("integrate", "child-1", "a" * 32, "b" * 64, "host-preview-token") in commands
        assert "recovery_required" in app.screen.query_one("#worktrees-detail").render().plain
        assert "manual recovery required" in app.screen.query_one("#worktrees-detail").render().plain
        assert "host may have completed" not in app.screen.query_one("#worktrees-status").render().plain

        await pilot.click("#worktree-row-0")
        await pilot.pause(0.1)
        await pilot.click("#worktrees-force-discard")
        await pilot.pause(0.1)
        assert isinstance(app.screen, WorktreeConfirmScreen)
        discard_modal = app.screen.query_one("#worktree-confirm-description").render().plain
        assert "WARNING: force discard removes the child worktree, including dirty files." in discard_modal
        assert "child_dirty: True" in discard_modal
        await pilot.click("#worktree-confirm-cancel")
        await pilot.pause()
        assert not any(
            isinstance(call, tuple) and call[0] == "discard" and call[-1] == "discard-preview-token"
            for call in commands
        )


def _logs_result(daemon=(), session=(), *, daemon_truncated=False, session_truncated=False, daemon_more=False, session_more=False, daemon_cursor=None, session_cursor=0):
    if daemon_cursor is None:
        daemon_cursor = f"{'a' * 32}:0"
    return p.LogsReadResult(
        daemon=p.DaemonLogPage(
            entries=list(daemon), next_cursor=daemon_cursor,
            truncated=daemon_truncated, has_more=daemon_more,
        ),
        session=p.SessionLogPage(
            entries=list(session), next_cursor=session_cursor,
            truncated=session_truncated, has_more=session_more,
        ),
    )


def _log_entry(source, seq, *, level="info", kind="daemon.started", summary="safe summary"):
    return p.LogEntry(source=source, seq=seq, ts=1_700_000_000.0 + seq, level=level, kind=kind, summary=summary)


@pytest.mark.asyncio
async def test_ctrl_e_opens_closes_right_logs_drawer_and_preserves_composer_state():
    from nexus.ui.tui.widgets import ChatEditor, LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    calls = []

    async def read_logs(**kwargs):
        calls.append(kwargs)
        return _logs_result()

    app.controller.client.read_logs = read_logs
    async with app.run_test(size=(100, 28)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        timeline = app.query_one(ConversationTimeline)
        timeline.scroll_to(y=3, animate=False)
        await pilot.pause()
        saved_scroll = timeline.scroll_offset.y
        editor.text = "draft remains"
        editor.move_cursor((0, 5))
        await pilot.press("ctrl+e")
        await pilot.pause(0.05)
        drawer = app.query_one(LogsDrawer)
        assert drawer.display
        assert drawer.region.x >= 60
        assert drawer.region.right == app.size.width
        assert app.focused is editor
        assert editor.text == "draft remains"
        assert timeline.scroll_offset.y == saved_scroll
        assert len(calls) == 1 and calls[0]["session"] == "s"

        await pilot.press("ctrl+e")
        await pilot.pause()
        assert not drawer.display
        assert app.focused is editor
        assert timeline.scroll_offset.y == saved_scroll
        await pilot.pause(1.1)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_ctrl_e_narrow_drawer_keeps_conversation_and_draft_bounded_visible():
    from nexus.ui.tui.widgets import ChatEditor, LogsDrawer

    app = NexusTextualApp(_client(FakeTransport(_baseline_events())), session="s")
    async with app.run_test(size=(48, 24)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "draft survives narrow drawer"
        editor.move_cursor((0, 6))
        await pilot.press("ctrl+e")
        await pilot.pause(0.05)

        drawer = app.query_one(LogsDrawer)
        main = app.query_one("#main-column")
        timeline = app.query_one("#conversation", ConversationTimeline)
        assert drawer.region.x >= 24
        assert drawer.region.right == app.size.width
        assert main.region.x == 0 and main.region.right <= drawer.region.x
        assert timeline.region.width >= 20
        assert timeline.region.right <= drawer.region.x
        assert editor.region.width >= 20
        assert editor.region.right <= drawer.region.x
        assert app.focused is editor
        assert editor.text == "draft survives narrow drawer"
        assert any(
            getattr(item, "_rendered", "") == "prior answer"
            for item in timeline._turns["turn-1"]._items.values()
        )


@pytest.mark.asyncio
async def test_ctrl_e_works_from_non_editor_focus_but_not_modal_or_child_transcript():
    from nexus.ui.tui.widgets import RootAgentBar
    from nexus.view import AgentView

    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one(RootAgentBar).focus()
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert app.query_one("#logs-drawer").display

        app.action_close_logs()
        app.push_screen(AgentPicker([], current="general"))
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert not app.query_one("#logs-drawer").display
        app.screen.dismiss(None)
        await pilot.pause()

        app.push_screen(AgentTranscriptScreen(AgentView(id="child")))
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert not app.query_one("#logs-drawer").display
        assert isinstance(app.screen, AgentTranscriptScreen)


@pytest.mark.asyncio
async def test_logs_pages_render_separately_and_bound_rows_and_statuses():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(48, 24)) as pilot:
        await pilot.pause()
        drawer = app.query_one(LogsDrawer)
        drawer.display = True
        app._sync_logs_layout()
        await pilot.pause()
        drawer.set_session("s")
        drawer.add_page(_logs_result(
            daemon=[_log_entry("daemon", 1, level="warning", summary="daemon note")],
            session=[_log_entry("session", 2, level="error", kind="turn.failed", summary="session note")],
            daemon_truncated=True,
            session_more=True,
        ))
        rendered = app.query_one("#logs-content").render().plain
        assert "DAEMON" in rendered and "SESSION ID · s" in rendered
        assert "WARNING" in rendered and "ERROR" in rendered
        assert "daemon note" in rendered and "session note" in rendered
        assert "Earlier entries unavailable" in rendered
        assert "More entries available" in rendered
        assert drawer.size.width <= 32
        assert drawer.region.right == app.size.width

        drawer.add_page(_logs_result(daemon=[_log_entry("daemon", seq) for seq in range(2, 90)]))
        assert len(drawer.daemon_entries) == LogsDrawer.ROW_LIMIT
        drawer.set_session("other")
        assert len(drawer.daemon_entries) == LogsDrawer.ROW_LIMIT
        assert drawer.session_entries == []
        rendered = app.query_one("#logs-content").render().plain
        assert "No log entries" in rendered

        drawer.set_error("temporarily unavailable")
        rendered = app.query_one("#logs-content").render().plain
        assert "Read error" in rendered
        # The drawer renders local time, so derive the date the same way (a
        # hard-coded date only holds in timezones ahead of UTC).
        assert datetime.fromtimestamp(1_700_000_002.0).strftime("%Y-%m-%d") in rendered


@pytest.mark.asyncio
async def test_truncated_daemon_page_replaces_old_generation_only():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        drawer = app.query_one(LogsDrawer)
        drawer.set_session("s")
        drawer.add_page(_logs_result(
            daemon=[_log_entry("daemon", 9, summary="old generation")],
            session=[_log_entry("session", 1, summary="keep session")],
        ))
        drawer.add_page(_logs_result(
            daemon=[_log_entry("daemon", 1, summary="new generation")],
            daemon_truncated=True,
        ))

        rendered = app.query_one("#logs-content").render().plain
        assert [row.summary for row in drawer.daemon_entries] == ["new generation"]
        assert [row.summary for row in drawer.session_entries] == ["keep session"]
        assert "old generation" not in rendered and "new generation" in rendered
        assert "keep session" in rendered and "Earlier entries unavailable" in rendered


@pytest.mark.asyncio
async def test_transient_logs_poll_error_keeps_rows_and_clears_after_recovery():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    app.LOGS_POLL_INTERVAL = 0.01
    attempts = 0
    failed = asyncio.Event()
    recovered = asyncio.Event()

    async def read_logs(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary poll failure")
        recovered.set()
        return _logs_result(daemon=[_log_entry("daemon", 1, summary="retained daemon row")])

    app.controller.client.read_logs = read_logs
    async with app.run_test() as pilot:
        await pilot.pause()
        drawer = app.query_one(LogsDrawer)
        drawer.set_session("s")
        drawer.add_page(_logs_result(
            daemon=[_log_entry("daemon", 1, summary="retained daemon row")],
            session=[_log_entry("session", 1, summary="retained session row")],
        ))
        # Let the first read fail, and observe its banner before the recovery poll.
        async def wait_failure():
            while not drawer.poll_error:
                await asyncio.sleep(0)
            failed.set()

        watcher = asyncio.create_task(wait_failure())
        app.action_toggle_logs()
        await failed.wait()
        rendered = app.query_one("#logs-content").render().plain
        assert "Read error · temporary poll failure" in rendered
        assert "retained daemon row" in rendered and "retained session row" in rendered
        await recovered.wait()
        await pilot.pause()
        rendered = app.query_one("#logs-content").render().plain
        assert "Read error" not in rendered
        assert "retained daemon row" in rendered and "retained session row" in rendered
        watcher.cancel()


@pytest.mark.asyncio
async def test_logs_poll_discards_late_old_session_page_and_keeps_daemon_history():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def read_logs(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return _logs_result(
                daemon=[_log_entry("daemon", 1)],
                session=[_log_entry("session", 1, kind="old.session")],
            )
        return _logs_result()

    app.controller.client.read_logs = read_logs
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await entered.wait()
        drawer = app.query_one(LogsDrawer)
        drawer.add_page(_logs_result(
            daemon=[_log_entry("daemon", 1)],
            session=[_log_entry("session", 1)],
            daemon_truncated=True,
        ))
        await app._switch_session("other")
        release.set()
        await pilot.pause(0.05)
        assert not any(getattr(row, "kind", "") == "old.session" for row in drawer.session_entries)
        assert len(drawer.daemon_entries) == 1
        assert drawer.session_entries == []
        assert calls[-1]["session"] == "other"
        assert calls[-1]["session_cursor"] is None
        assert drawer.daemon_truncated
        assert not drawer.session_truncated


@pytest.mark.asyncio
async def test_logs_poll_ignores_response_after_drawer_closes():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    entered, release = asyncio.Event(), asyncio.Event()

    async def read_logs(**kwargs):
        entered.set()
        await release.wait()
        return _logs_result(daemon=[_log_entry("daemon", 1)])

    app.controller.client.read_logs = read_logs
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await entered.wait()
        app.action_close_logs()
        release.set()
        await pilot.pause(0.05)
        drawer = app.query_one(LogsDrawer)
        assert not drawer.display
        assert drawer.daemon_entries == []


@pytest.mark.asyncio
async def test_cancelled_late_logs_read_cannot_block_or_overwrite_reopened_drawer():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def read_logs(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Model an uninterruptible transport that completes after drawer close.
                await release.wait()
            return _logs_result(daemon=[_log_entry("daemon", 1, summary="stale result")])
        return _logs_result(daemon=[_log_entry("daemon", 2, summary="fresh result")])

    app.controller.client.read_logs = read_logs
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_toggle_logs()
        await entered.wait()
        app.action_close_logs()
        app.action_toggle_logs()
        await pilot.pause(0.05)

        drawer = app.query_one(LogsDrawer)
        rendered = app.query_one("#logs-content").render().plain
        assert calls >= 2
        assert "fresh result" in rendered
        assert "stale result" not in rendered
        release.set()
        await pilot.pause(0.05)
        rendered = app.query_one("#logs-content").render().plain
        assert "fresh result" in rendered
        assert "stale result" not in rendered
        assert drawer.display


@pytest.mark.asyncio
async def test_reopen_resets_stale_poll_status_and_cursors_but_retains_session_history():
    from nexus.ui.tui.widgets import LogsDrawer

    app = NexusTextualApp(_client(FakeTransport()), session="s")
    entered, release = asyncio.Event(), asyncio.Event()

    async def read_logs(**kwargs):
        if kwargs["daemon_cursor"] is None:
            entered.set()
            await release.wait()
            return _logs_result(daemon_truncated=True, session_truncated=True)
        return _logs_result()

    app.controller.client.read_logs = read_logs
    async with app.run_test() as pilot:
        await pilot.pause()
        drawer = app.query_one(LogsDrawer)
        drawer.set_session("s")
        drawer.add_page(_logs_result(
            daemon=[_log_entry("daemon", 1)],
            session=[_log_entry("session", 1)],
            daemon_truncated=True,
            session_truncated=True,
        ))
        drawer.set_error("old transient failure")
        app._logs_daemon_cursor = f"{'b' * 32}:4"
        app._logs_session_cursor = 7
        app.action_toggle_logs()
        await entered.wait()
        app.action_close_logs()
        app.action_toggle_logs()
        await pilot.pause()

        assert app._logs_daemon_cursor is None
        assert app._logs_session_cursor is None
        assert drawer.daemon_entries and drawer.session_entries
        assert not drawer.daemon_truncated and not drawer.session_truncated
        rendered = app.query_one("#logs-content").render().plain
        assert "old transient failure" not in rendered
        assert "Earlier entries unavailable" not in rendered
        release.set()
