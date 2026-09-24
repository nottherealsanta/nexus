"""Pilot tests for the only interactive Nexus chat surface."""

from __future__ import annotations

import asyncio
import importlib.resources
import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.events import Click
from textual.command import Provider
from textual.widgets import Input, TextArea

from nexus.events import Event
from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from nexus.ui.cli.client import Client, TransportClosed
from nexus.ui.tui.agent_picker import AgentPicker
from nexus.ui.tui.agent_transcript import AgentTranscriptScreen, render_agent
from nexus.ui.tui.app import ChatCommandProvider, NexusTextualApp
from nexus.ui.tui.messages import AgentOpenRequested, EventReceived, StreamDisconnected
from nexus.ui.tui.permission import PermissionScreen
from nexus.ui.tui.timeline import AgentActivityLink, ConversationTimeline, TaskActivityWidget, ToolActivityWidget, format_arguments


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
        if isinstance(command, p.AgentCurrent):
            return p.AgentCurrentResult(
                session=command.session, name="general", source="default"
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
            return p.AgentSelectResult(session=command.session, name=command.name, source="session")
        if isinstance(command, p.AgentReset):
            return p.AgentSelectResult(session=command.session, name="general", source="default")
        if isinstance(command, p.ModelsList):
            return p.ModelsListResult(models=[{"provider": "fake", "id": "m", "tier": "medium"}])
        if isinstance(command, p.ModelSelect):
            return p.ModelSelectResult(session=command.session, provider="fake", model=command.ref)
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
        await pilot.press("ctrl+enter")
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
async def test_root_agent_picker_mouse_and_next_turn_feedback():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        app.screen.query_one("#agent-search", Input).value = "plan"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert app.controller.agent_name == "plan"
        assert "applies next turn" in str(app.query_one("#connection-status").render())


@pytest.mark.asyncio
async def test_command_palette_and_migrated_chat_command_parity():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport))
    async with app.run_test() as pilot:
        await pilot.pause()
        provider_type = next(iter(NexusTextualApp.COMMANDS))()
        provider = provider_type(app.screen)
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
        assert not list(timeline.children)
        assert app.focused is app.query_one("#chat-editor", TextArea)
        assert not app.query("#agent-tracker")


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
async def test_inspector_tail_follow_obeys_user_scroll_state():
    from nexus.view import AgentView, ConversationView, MessageView, TurnView

    body = ConversationView(
        session_id="agent",
        turns=[TurnView(id=f"t{i}", messages=[MessageView(role="assistant")]) for i in range(20)],
    )
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = AgentTranscriptScreen(AgentView(id="agent", body=body))
        app.push_screen(screen)
        await pilot.pause(0.1)
        scroll = screen.query_one("#agent-inspector-scroll")
        assert screen.scroll_at_bottom
        screen._at_bottom = False
        calls = []
        original = scroll.scroll_end
        scroll.scroll_end = lambda **kwargs: calls.append(True)
        screen.refresh_agent(AgentView(id="agent", body=body))
        await pilot.pause(0.1)
        assert calls == []
        screen._at_bottom = True
        screen.refresh_agent(AgentView(id="agent", body=body))
        await pilot.pause(0.1)
        assert calls == [True]
        scroll.scroll_end = original


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
async def test_task_cards_are_inline_and_open_their_linked_live_child():
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
        assert "explore" in card._expanded_text()
        assert len(card.query(AgentActivityLink)) == 2
        await app.on_agent_open_requested(AgentOpenRequested("a"))
        await pilot.pause()
        assert app.screen.__class__.__name__ == "AgentTranscriptScreen"


def test_child_transcript_safe_bounded_content():
    from nexus.view import AgentView, BlockView, ConversationView, MessageView, ToolCallView, TurnView

    body = ConversationView(turns=[TurnView(
        id="child",
        messages=[MessageView(role="assistant", blocks=[BlockView(kind="text", text="child **answer**\x1b[31m")])],
        tools=[ToolCallView(name="Write", status="failed", input={"secret": "x" * 6000}, error="failed")],
    )])
    rendered = render_agent(AgentView(id="a", body=body))
    assert "child **answer**" in rendered
    assert "\\x1b" in rendered
    assert "Name/status: `Write` · `failed`" in rendered
    assert len(rendered) < 6000


def test_child_transcript_does_not_leak_write_content():
    from nexus.view import AgentView, ConversationView, ToolCallView, TurnView

    secret = "do-not-render-this-write-payload"
    agent = AgentView(
        id="a",
        body=ConversationView(turns=[TurnView(
            id="child", tools=[ToolCallView(
                name="Write", input={"path": "secret.txt", "content": secret},
                display=secret, result=[{"type": "text", "text": secret}],
            )]
        )]),
    )
    rendered = render_agent(agent)
    assert "secret.txt (1 lines)" in rendered
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
        await card.toggle()
        assert "+1 -1" in str(card.query_one("#tool-expanded").render())
        assert not card.query(".tool-diff-view")


@pytest.mark.asyncio
async def test_wide_diff_unmounts_when_the_card_becomes_narrow():
    from textual.widgets import Static

    from nexus.view import ToolCallView

    app = NexusTextualApp(_client(FakeTransport()))
    tool = ToolCallView(
        call_id="e", name="Edit", status="completed",
        diff={"path": "f.py", "hunk": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new"},
    )
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        card = ToolActivityWidget(tool)
        await app.query_one("#conversation", ConversationTimeline).mount(card)
        await pilot.pause()
        preview = Static(classes="tool-diff-view")
        await card.mount(preview)
        card._diff_widget = preview
        await card.set_tool(tool)
        assert not card.query(".tool-diff-view")


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


def test_cli_chat_textual_only_tty_guard_and_noninteractive_run():
    from nexus.cli import build_parser, main

    parser = build_parser()
    assert parser.parse_args(["chat"]).session == "default"

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
    assert "#d18a38" in css  # amber active/focus accent
    assert "#a78bfa" in css  # purple identity
    assert "#86b97a" in css  # success/task accent
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
    assert any(getattr(provider, "__name__", "") == "ChatCommandProvider" for provider in (item() for item in NexusTextualApp.COMMANDS))
