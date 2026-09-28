"""Bounded functional journeys through the Textual shell and client seam."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client, _event
from textual.widgets import Static

from nexus.events import Event
from nexus.host import protocol as p
from nexus.ui.cli.client import Client
from nexus.ui.tui.agent_picker import AgentPickerPanel
from nexus.ui.tui.agent_transcript import AgentTranscriptScreen
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.messages import EventReceived
from nexus.ui.tui.permission import PermissionScreen
from nexus.ui.tui.timeline import ConversationTimeline, TaskActivityWidget
from nexus.ui.tui.widgets import ChatEditor, RootAgentBar
from nexus.ui_support.tui_panels import SessionsScreen
from nexus.view import apply, initial_state


class JourneyTransport(FakeTransport):
    """Keep the shared deterministic behavior and expose real protocol commands."""

    def __init__(self, events=()):
        super().__init__(events)
        self.commands: list[p.Command] = []
        self.selected_model = (None, None)

    async def request(self, command):
        self.commands.append(command)
        result = await super().request(command)
        if isinstance(command, p.AgentCurrent):
            return p.AgentCurrentResult(
                session=command.session,
                name=self.agent_name,
                source=self.agent_source,
                provider=self.selected_model[0],
                model=self.selected_model[1],
            )
        if isinstance(command, p.ModelSelect):
            provider, model = command.ref.split("/", 1)
            self.selected_model = (provider, model)
            return p.ModelSelectResult(
                session=command.session, reference=command.ref,
                provider=provider, model=model,
            )
        if isinstance(command, p.ReasoningEffortSelect):
            return p.ReasoningEffortSelectResult(
                session=command.session, effort=command.effort,
                supported_levels=[],
            )
        return result



def _session_option_ids(screen) -> list[str]:
    from textual.widgets import OptionList

    options = screen.query_one("#sessions-options", OptionList)
    return [options.get_option_at_index(i).id for i in range(options.option_count)
            if options.get_option_at_index(i).id]

@pytest.fixture
def journey_transport():
    return JourneyTransport()


@pytest.mark.asyncio
async def test_session_lifecycle_shortcuts_render_the_selected_session(journey_transport):
    app = NexusTextualApp(_client(journey_transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        created = app.controller.session
        assert created != "s"
        assert p.SessionOpen(session=created) in journey_transport.commands

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert p.SessionList() in journey_transport.commands
        # Ctrl+O opens the searchable Sessions dialog listing every session.
        assert isinstance(app.screen, SessionsScreen)
        assert "other" in _session_option_ids(app.screen)
        await pilot.press("escape")
        await pilot.pause()

        editor = app.query_one(ChatEditor)
        editor.text = "/sesssion other"
        editor.move_cursor((0, len(editor.text)))
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert app.controller.session == "other"
        assert p.SessionList() in journey_transport.commands

        editor.text = "/session"
        editor.move_cursor((0, len(editor.text)))
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, SessionsScreen)
        assert {"s", "other"} <= set(_session_option_ids(app.screen))
        assert journey_transport.commands.count(p.SessionList()) >= 3
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("ctrl+f")
        await pilot.pause()
        assert app.controller.session == "forked"
        assert p.SessionFork(session="other") in journey_transport.commands
        assert p.SessionOpen(session="forked") in journey_transport.commands
        assert app.query_one("#connection-status", Static).render().plain == ""


@pytest.mark.asyncio
async def test_agent_and_model_picker_mouse_selection_updates_host_and_root_bar(journey_transport):
    app = NexusTextualApp(_client(journey_transport), session="pick")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_pick_agent()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert panel.display
        # Inline option rows are now packed to one terminal row each with no
        # inset padding; click the visible second row directly.
        await pilot.click("#agent-options", offset=(2, 1))
        await pilot.pause(0.05)
        assert p.AgentSelect(session="pick", name="plan") in journey_transport.commands
        assert "Plan" in app.query_one(RootAgentBar).summary().plain

        await app._push_model_picker()
        await pilot.pause()
        assert panel.display
        await pilot.click("#agent-options", offset=(2, 0))
        await pilot.pause(0.05)
        assert p.ModelSelect(session="pick", ref="fake/m") in journey_transport.commands
        assert "fake" in app.query_one(RootAgentBar).summary().plain
        assert "m" in app.query_one(RootAgentBar).summary().plain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "decision", "wins"),
    [
        ("y", "allow_once", True),
        ("a", "allow_always", True),
        ("n", "deny_once", True),
        ("d", "deny_always", True),
        ("n", "deny_once", False),
    ],
)
async def test_permission_decisions_and_lost_first_responder_render_feedback(
    journey_transport, key, decision, wins
):
    journey_transport.permission_wins = wins
    app = NexusTextualApp(_client(journey_transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        app.post_message(EventReceived(_event(
            "permission.requested", 1, {"id": "approval", "tool": "Write", "key": "note"}
        )))
        await pilot.pause()
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press(key)
        await pilot.pause()
        assert journey_transport.permission_decision == decision
        assert any(isinstance(row, p.PermissionResolve) and row.decision == decision
                   for row in journey_transport.commands)
        status = app.query_one("#connection-status", Static).render().plain
        if wins:
            assert f"Permission {decision}" in status
        else:
            assert "Another view answered first" in status


@pytest.mark.asyncio
async def test_export_details_reconnect_and_error_are_visible_and_traced(journey_transport):
    app = NexusTextualApp(_client(journey_transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        view = initial_state("s")
        view = apply(view, _event("turn.started", 1))
        view = apply(view, _event("tool.requested", 2, {
            "call_id": "read-1", "tool": "Read", "input": {"path": "note.md"}
        }))
        view = apply(view, _event("tool.completed", 3, {
            "call_id": "read-1", "tool": "Read", "result": {"display": "2 lines"}
        }))
        app.controller.view = view
        await app._dispatch_chat_command("/tools")
        assert "Read" in app.query_one(
            "#connection-status", Static
        ).render().plain

        await app._dispatch_chat_command("/export markdown")
        assert p.SessionExport(session="s", format="markdown") in journey_transport.commands
        assert "# export" in app.query_one("#connection-status", Static).render().plain

        await app._dispatch_chat_command("/details")
        assert "session s" in app.query_one("#connection-status", Static).render().plain

        old = journey_transport
        replacement = JourneyTransport()

        async def reconnect():
            return Client(replacement)

        app._reconnect_factory = reconnect
        await app.action_reconnect()
        await pilot.pause()
        assert old.closed
        assert any(isinstance(row, p.Health) for row in replacement.commands)
        assert any(isinstance(row, p.SessionOpen) and row.session == "s"
                   for row in replacement.commands)

        error = Event(type="error", data={"message": "scripted failure"}, seq=1, session="s")
        await app._event_received(EventReceived(error, None))
        assert "Error · scripted failure" in app.query_one(
            "#connection-status", Static
        ).render().plain


@pytest.mark.asyncio
async def test_task_child_transcript_opens_from_rendered_link_and_escape_returns():
    transport = JourneyTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        view = initial_state("s")
        for event in (
            _event("turn.started", 1),
            _event("tool.requested", 2, {"call_id": "task", "tool": "Task", "input": {"task": "inspect"}}),
            _event("agent.spawned", 3, {"agent": {
                "id": "child", "parent": "s", "parent_call_id": "task",
                "root_turn_id": "turn-1", "type": "explore", "task": "inspect", "session": "child",
            }}),
        ):
            view = apply(view, event)
        app.controller.view = view
        await app._sync_timeline()
        await pilot.pause()
        task_widget = app.query_one(ConversationTimeline).query_one(TaskActivityWidget)
        task_widget.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        from nexus.ui.tui.tool_details import ToolDetailsScreen

        assert isinstance(app.screen, ToolDetailsScreen)
        await pilot.click("#tool-details-agent")
        await pilot.pause()
        assert isinstance(app.screen, AgentTranscriptScreen)
        assert any(isinstance(row, p.AgentTranscript) and row.agent_id == "child"
                   for row in transport.commands)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
        assert app.query_one(ChatEditor).is_mounted


@pytest.mark.asyncio
async def test_narrow_resize_keeps_composer_bounded_and_focused(journey_transport):
    app = NexusTextualApp(_client(journey_transport), session="resize")
    async with app.run_test(size=(90, 26)) as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "draft survives resize"
        await pilot.resize_terminal(44, 18)
        await pilot.pause()
        assert app.size.width == 44
        assert editor.region.right <= 44
        assert editor.region.height >= 3
        assert editor.text == "draft survives resize"
        assert app.focused is editor
