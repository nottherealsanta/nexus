"""Phase 8b2 UI client: pure client, renderer, one-shot, interactive, JSONL.

No daemon and no prompt_toolkit are needed. A fake transport over a fake facade
protocol exercises the same seams a real Unix socket and daemon will: handshake,
version refusal, auto-start, streaming with reconnect, first-responder
approvals, and Ctrl-C cancellation. The layering of the client itself is proven
separately in ``test_ui_layering.py``.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from nexus.events import Event
from nexus.host import PROTOCOL_VERSION
from nexus.host import DaemonUnavailable as HostDaemonUnavailable
from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from nexus.ui import jsonl
from nexus.ui.cli import (
    Approver,
    ChatSession,
    CliDependencyError,
    Client,
    ClientError,
    DaemonUnavailable,
    FacadeError,
    ProtocolVersionError,
    TerminalRenderer,
    Transport,
    TransportClosed,
    UdsTransport,
    available,
    commands,
    keys,
    run_chat,
    run_once,
)
from nexus.ui.cli.render import exit_code

# ---------------------------------------------------------------------------
# Fake facade protocol + fake transport
# ---------------------------------------------------------------------------


class FakeFacade:
    """A scripted stand-in for :class:`nexus.host.facade.HostFacade`."""

    def __init__(self) -> None:
        self.commands: list[object] = []
        self.health_version = PROTOCOL_VERSION
        self.summaries: dict[str, SessionSummary] = {}
        self.scripts: dict[str, list[Event]] = {}
        self.subscriptions: list[tuple[str, int, str | None]] = []
        self.claims: set[str] = set()
        self.error_for: dict[str, p.ErrorResult] = {}
        self.models = [{"provider": "openai", "id": "gpt-5"}]
        self.tools = [{"name": "Read", "bundle": "fs", "mutates": False, "description": "r"}]
        self.export_content = "exported body"

    async def handle(self, command):
        self.commands.append(command)
        override = self.error_for.get(type(command).__name__)
        if override is not None:
            return override
        if isinstance(command, p.Health):
            return p.HealthResult(
                ok=True, version=self.health_version, sessions=len(self.summaries)
            )
        if isinstance(command, p.SessionList):
            return p.SessionListResult(
                sessions=[self.summaries[k] for k in sorted(self.summaries)]
            )
        if isinstance(command, p.SessionOpen):
            summary = self.summaries.setdefault(
                command.session, SessionSummary(id=command.session)
            )
            return p.SessionOpenResult(session=summary)
        if isinstance(command, p.SessionStart):
            return p.SessionStartResult(session=command.session, turn_id="turn-1")
        if isinstance(command, p.SessionEnqueue):
            return p.SessionEnqueueResult(
                session=command.session, queued_id="q1", turn_id="turn-1"
            )
        if isinstance(command, p.SessionCancel):
            return p.SessionCancelResult(
                session=command.session, cancelled=True, dropped=0
            )
        if isinstance(command, p.SessionFork):
            child = command.new_id or f"{command.session}-fork"
            summary = self.summaries.setdefault(child, SessionSummary(id=child))
            return p.SessionForkResult(session=summary)
        if isinstance(command, p.SessionDelete):
            return p.SessionDeleteResult(session=command.session, trash_id="t1")
        if isinstance(command, p.SessionRestore):
            return p.SessionRestoreResult(session=command.trash_id)
        if isinstance(command, p.SessionExport):
            return p.SessionExportResult(
                session=command.session,
                format=command.format,
                content=self.export_content,
            )
        if isinstance(command, p.SessionState):
            return p.SessionStateResult(session=command.session, seq=0, view={})
        if isinstance(command, p.PermissionResolve):
            won = command.request_id not in self.claims
            self.claims.add(command.request_id)
            return p.PermissionResolveResult(
                session=command.session,
                request_id=command.request_id,
                resolved=won,
                client_id=command.client_id,
            )
        if isinstance(command, p.ExtensionsReload):
            return p.ExtensionsReloadResult(generation=2, changed=True)
        if isinstance(command, p.ExtensionsList):
            return p.ExtensionsListResult(generation=2, extensions=[{"name": "m"}])
        if isinstance(command, p.ExtensionsValidate):
            return p.ExtensionsValidateResult(
                generation=2, valid=True, checked=1, results=[{"ok": True}]
            )
        if isinstance(command, p.ExtensionsTrash):
            return p.ExtensionsTrashResult(
                target=command.target,
                trash_id="t1",
                source_path=command.target,
                changed=True,
                generation=3,
                previous_generation=2,
                reason=command.reason,
            )
        if isinstance(command, p.ModelsRefresh):
            return p.ModelsRefreshResult(status={"source": "cache"})
        if isinstance(command, p.ModelsList):
            return p.ModelsListResult(count=len(self.models), models=self.models)
        if isinstance(command, p.ModelShow):
            return p.ModelShowResult(
                ref=command.ref, found=True, model={"id": command.ref}
            )
        if isinstance(command, p.ModelTiers):
            return p.ModelTiersResult(
                order=["low", "medium", "high"], default="medium"
            )
        if isinstance(command, p.AgentsList):
            return p.AgentsListResult(generation=2, agents=[{"name": "explore"}])
        if isinstance(command, p.ToolsList):
            return p.ToolsListResult(count=len(self.tools), tools=self.tools)
        if isinstance(command, p.Doctor):
            return p.DoctorResult(ok=True, report={"workspace": "ws"})
        if isinstance(command, p.Shutdown):
            return p.ShutdownResult(stopping=True, reason=command.reason)
        return p.ErrorResult(kind="Unhandled", message=type(command).__name__)


class FakeTransport:
    """A transport backed by a :class:`FakeFacade`, no sockets involved."""

    def __init__(self, facade: FakeFacade) -> None:
        self.facade = facade
        self.closed = False
        self.interrupt_on_start = False

    async def request(self, command):
        if self.interrupt_on_start and isinstance(command, p.SessionStart):
            raise KeyboardInterrupt
        return await self.facade.handle(command)

    def events(self, session, from_seq=0, *, follow=True, client_id=None):
        self.facade.subscriptions.append((session, from_seq, client_id))
        script = list(self.facade.scripts.get(session, []))

        async def gen():
            for event in script:
                if event.seq > from_seq:
                    yield event

        return gen()

    async def aclose(self):
        self.closed = True


def ev(type_, seq, *, session="s", turn="t", **data) -> Event:
    return Event(type=type_, data=data, seq=seq, session=session, turn=turn)


def make_client(facade: FakeFacade | None = None) -> tuple[Client, FakeFacade, FakeTransport]:
    facade = facade or FakeFacade()
    transport = FakeTransport(facade)
    return Client(transport), facade, transport


def scripted_reader(*lines):
    iterator = iter(lines)
    prompts: list[str] = []

    async def read(prompt: str) -> str:
        prompts.append(prompt)
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    read.prompts = prompts  # type: ignore[attr-defined]
    return read


COMPLETED = [
    ev("model.started", 1, provider="openai", model="gpt-5"),
    ev("text.delta", 2, text="Hel"),
    ev("text.delta", 3, text="lo"),
    ev("text", 4, text="Hello"),
    ev("turn.completed", 5),
]


# ---------------------------------------------------------------------------
# The client over an injected transport
# ---------------------------------------------------------------------------


def test_client_satisfies_the_transport_protocol():
    _, _, transport = make_client()
    assert isinstance(transport, Transport)


async def test_handshake_returns_health():
    client, _, _ = make_client()
    health = await client.handshake()
    assert health.ok and health.version == PROTOCOL_VERSION
    assert client.health is health


async def test_handshake_refuses_a_version_mismatch():
    facade = FakeFacade()
    facade.health_version = PROTOCOL_VERSION + 7
    client, _, _ = make_client(facade)
    with pytest.raises(ProtocolVersionError) as excinfo:
        await client.handshake()
    assert excinfo.value.expected == PROTOCOL_VERSION
    assert excinfo.value.actual == PROTOCOL_VERSION + 7


async def test_error_result_becomes_a_facade_error():
    facade = FakeFacade()
    facade.error_for["SessionList"] = p.ErrorResult(
        kind="SessionError", message="no such session"
    )
    client, _, _ = make_client(facade)
    with pytest.raises(FacadeError) as excinfo:
        await client.list_sessions()
    assert excinfo.value.kind == "SessionError"


async def test_client_methods_send_the_matching_commands():
    client, facade, _ = make_client()
    facade.summaries["s"] = SessionSummary(id="s", last_seq=3)
    await client.list_sessions()
    await client.open_session("s")
    await client.start_turn("s", "hi")
    await client.enqueue("s", "later")
    await client.cancel("s")
    await client.fork("s")
    await client.delete("s")
    await client.restore("t1")
    await client.export("s")
    await client.state("s")
    await client.list_models()
    await client.list_extensions()
    await client.validate_extensions("a.py")
    await client.show_model("p/m")
    await client.model_tiers()
    await client.list_agents()
    await client.reload_extensions()
    await client.refresh_models()
    await client.doctor(explain_reload=True)
    await client.shutdown("bye")
    kinds = [type(command).__name__ for command in facade.commands]
    assert kinds == [
        "SessionList",
        "SessionOpen",
        "SessionStart",
        "SessionEnqueue",
        "SessionCancel",
        "SessionFork",
        "SessionDelete",
        "SessionRestore",
        "SessionExport",
        "SessionState",
        "ModelsList",
        "ExtensionsList",
        "ExtensionsValidate",
        "ModelShow",
        "ModelTiers",
        "AgentsList",
        "ExtensionsReload",
        "ModelsRefresh",
        "Doctor",
        "Shutdown",
    ]


async def test_client_trash_extensions_sends_the_command():
    client, facade, _ = make_client()
    result = await client.trash_extensions(
        ".nexus/tools/foo.py", reason="cleanup", force=True
    )
    assert isinstance(result, p.ExtensionsTrashResult)
    assert result.trash_id == "t1" and result.reason == "cleanup"
    command = facade.commands[-1]
    assert isinstance(command, p.ExtensionsTrash)
    assert command.target == ".nexus/tools/foo.py"
    assert command.force is True


async def test_client_stream_passes_the_cursor_and_client_id():
    client, facade, _ = make_client()
    events = [event async for event in client.stream("s", 4, follow=False)]
    assert events == []
    assert facade.subscriptions[-1][0] == "s"
    assert facade.subscriptions[-1][1] == 4
    assert facade.subscriptions[-1][2] == client.client_id


# ---------------------------------------------------------------------------
# Renderer: dedup and safety
# ---------------------------------------------------------------------------


def test_renderer_dedups_streamed_text_then_final():
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO())
    for event in COMPLETED:
        renderer.render(event)
    assert out.getvalue() == "Hello\n"


def test_renderer_ignores_replayed_events():
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO())
    for event in COMPLETED:
        renderer.render(event)
    for event in COMPLETED:
        assert renderer.render(event) is False
    assert out.getvalue() == "Hello\n"


def test_renderer_prints_a_final_only_response_once():
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO())
    renderer.render(ev("model.started", 1))
    renderer.render(ev("text", 2, text="only final"))
    assert out.getvalue() == "only final\n"


def test_renderer_escapes_control_characters():
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO())
    renderer.render(ev("tool.requested", 1, tool="evil\x1b]0;x\x07"))
    assert "\x1b" not in out.getvalue()
    assert "\\x1b" in out.getvalue()


def test_renderer_reports_failure_and_cancellation():
    err = io.StringIO()
    renderer = TerminalRenderer(io.StringIO(), stderr=err)
    renderer.render(ev("turn.failed", 1, error="boom"))
    renderer.render(ev("turn.cancelled", 2, reason="user"))
    assert "boom" in err.getvalue()
    assert "Cancelled." in err.getvalue()
    assert exit_code(ev("turn.cancelled", 3)) == 130
    assert exit_code(None) == 1


# ---------------------------------------------------------------------------
# One-shot runs
# ---------------------------------------------------------------------------


async def test_run_once_completed_exit_zero():
    client, facade, _ = make_client()
    facade.scripts["s"] = COMPLETED
    out, err = io.StringIO(), io.StringIO()
    code = await run_once(client, session="s", content="hi", stdout=out, stderr=err)
    assert code == 0
    assert out.getvalue() == "Hello\n"
    assert any(isinstance(c, p.SessionStart) for c in facade.commands)


async def test_run_once_failed_and_cancelled_exit_codes():
    for kind, expected in (("turn.failed", 1), ("turn.cancelled", 130)):
        client, facade, _ = make_client()
        facade.scripts["s"] = [ev("model.started", 1), ev(kind, 2, error="x", reason="x")]
        code = await run_once(
            client,
            session="s",
            content="hi",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert code == expected


async def test_run_once_jsonl_emits_full_envelopes_and_never_prompts():
    client, facade, _ = make_client()
    facade.scripts["s"] = [
        ev("model.started", 1, provider="openai", model="gpt-5"),
        ev("permission.requested", 2, id="r1", tool="Bash"),
        ev("turn.completed", 3),
    ]
    out, err = io.StringIO(), io.StringIO()
    code = await run_once(
        client,
        session="s",
        content="hi",
        stdout=out,
        stderr=err,
        json_output=True,
    )
    assert code == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [item["type"] for item in lines] == [
        "model.started",
        "permission.requested",
        "turn.completed",
    ]
    for item in lines:
        assert {"type", "data", "seq", "ts", "session", "turn", "id"} <= set(item)
    assert not any(isinstance(c, p.PermissionResolve) for c in facade.commands)


async def test_run_once_prompts_and_resolves_a_permission():
    client, facade, _ = make_client()
    facade.scripts["s"] = [
        ev("permission.requested", 1, id="r1", tool="Bash", key="cmd"),
        ev("turn.completed", 2),
    ]
    err = io.StringIO()
    approver = Approver(scripted_reader("y"), stderr=err)
    code = await run_once(
        client,
        session="s",
        content="hi",
        stdout=io.StringIO(),
        stderr=err,
        approver=approver,
    )
    assert code == 0
    resolves = [c for c in facade.commands if isinstance(c, p.PermissionResolve)]
    assert resolves and resolves[0].decision == "allow_once"


async def test_run_once_ctrl_c_cancels_the_turn():
    client, facade, transport = make_client()
    transport.interrupt_on_start = True
    err = io.StringIO()
    code = await run_once(
        client,
        session="s",
        content="hi",
        stdout=io.StringIO(),
        stderr=err,
    )
    assert code == 130
    assert any(isinstance(c, p.SessionCancel) for c in facade.commands)
    assert "Cancelled." in err.getvalue()


async def test_run_once_reused_session_does_not_replay_the_old_turn():
    facade = FakeFacade()
    facade.summaries["s"] = SessionSummary(id="s", last_seq=5)
    facade.scripts["s"] = [
        ev("text.delta", 1, text="old"),
        ev("turn.completed", 5),
        ev("text.delta", 6, text="new"),
        ev("turn.completed", 7),
    ]
    client, facade, _ = make_client(facade)
    out, err = io.StringIO(), io.StringIO()
    code = await run_once(client, session="s", content="hi", stdout=out, stderr=err)
    assert code == 0
    # The subscription starts strictly after the session's current end, so the
    # previous turn's terminal event never ends this run early.
    assert facade.subscriptions[-1][1] == 5
    assert "new" in out.getvalue()
    assert "old" not in out.getvalue()


# ---------------------------------------------------------------------------
# Interactive chat
# ---------------------------------------------------------------------------


def make_chat(reader, *, session="s", facade=None, stderr=None):
    client, facade, transport = make_client(facade)
    out = io.StringIO()
    err = stderr if stderr is not None else io.StringIO()
    chat = ChatSession(
        client,
        session,
        reader=reader,
        stdout=out,
        stderr=err,
        approver=Approver(reader, stderr=err),
    )
    return chat, facade, transport, out, err


async def test_chat_help_and_exit():
    reader = scripted_reader("/help", "/exit")
    chat, _, _, out, _ = make_chat(reader)
    assert await chat.run() == 0
    assert "/sessions" in out.getvalue()
    assert chat.closed is True


async def test_chat_multiline_continuation_sends_one_message():
    reader = scripted_reader("first \\", "second", "/exit")
    chat, facade, _, _, _ = make_chat(reader)
    facade.scripts["s"] = COMPLETED
    await chat.run()
    starts = [c for c in facade.commands if isinstance(c, p.SessionStart)]
    assert starts and starts[0].content == "first \nsecond"


async def test_chat_slash_commands_dispatch():
    reader = scripted_reader(
        "/new work",
        "/model",
        "/sessions",
        "/export json",
        "/cancel",
        "/fork",
        "/exit",
    )
    chat, facade, _, out, _ = make_chat(reader)
    await chat.run()
    kinds = [type(c).__name__ for c in facade.commands]
    assert "ModelsList" in kinds
    assert "SessionExport" in kinds
    assert "SessionCancel" in kinds
    assert "SessionFork" in kinds
    assert "exported body" in out.getvalue()
    assert chat.session == "work-fork"


async def test_chat_tools_lists_seen_tools():
    reader = scripted_reader("/tools", "/exit")
    chat, _, _, out, _ = make_chat(reader)
    chat._ingest(ev("tool.requested", 1, call_id="c1", tool="Read"))
    chat._ingest(ev("tool.completed", 2, call_id="c1", tool="Read"))
    await chat.run()
    assert "Read" in out.getvalue()


async def test_chat_status_line_reports_model_and_usage():
    reader = scripted_reader("/exit")
    chat, _, _, _, _ = make_chat(reader)
    chat._ingest(ev("model.started", 1, provider="openai", model="gpt-5"))
    chat._ingest(ev("model.usage", 2, input=10, output=5))
    line = chat.status_line()
    assert "gpt-5" in line
    assert "10" in line and "5" in line
    assert "running" in line


async def test_chat_session_switcher_marks_unread_sessions():
    reader = scripted_reader("/exit")
    chat, facade, _, out, _ = make_chat(reader)
    facade.summaries = {
        "s": SessionSummary(id="s", last_seq=5),
        "other": SessionSummary(id="other", last_seq=9),
    }
    chat._cursors["s"] = 5
    await chat._cmd_sessions(())
    assert "* other" in out.getvalue()


async def test_chat_approval_first_responder_wins():
    facade = FakeFacade()
    facade.scripts["s"] = [
        ev("permission.requested", 1, id="r1", tool="Write"),
        ev("turn.completed", 2),
    ]
    first, _, _, _, first_err = make_chat(scripted_reader("y"), facade=facade)
    second, _, _, _, second_err = make_chat(scripted_reader("y"), facade=facade)
    await first._turn("go")
    await second._turn("go")
    assert "-> allow_once" in first_err.getvalue()
    assert "answered this request first" in second_err.getvalue()


async def test_chat_reconnects_from_the_last_rendered_seq():
    reader = scripted_reader("/exit")
    chat, facade, _, _, _ = make_chat(reader)
    chat._ingest(ev("text.delta", 1, text="a"))
    chat._ingest(ev("text.delta", 2, text="b"))
    chat._ingest(ev("turn.completed", 3))
    facade.scripts["s"] = [ev("text", 4, text="tail"), ev("turn.completed", 5)]
    await chat._turn("again")
    assert facade.subscriptions[-1][1] == 3


async def test_run_chat_end_to_end():
    client, facade, _ = make_client()
    facade.scripts["s"] = COMPLETED
    out, err = io.StringIO(), io.StringIO()
    reader = scripted_reader("hello", "/exit")
    code = await run_chat(
        client, session="s", reader=reader, stdout=out, stderr=err
    )
    assert code == 0
    assert "Hello" in out.getvalue()
    starts = [c for c in facade.commands if isinstance(c, p.SessionStart)]
    assert starts and starts[0].content == "hello"


async def test_run_chat_resumed_session_does_not_replay_the_old_turn():
    facade = FakeFacade()
    facade.summaries["s"] = SessionSummary(id="s", last_seq=3)
    facade.scripts["s"] = [
        ev("text.delta", 1, text="old"),
        ev("turn.completed", 3),
        ev("text.delta", 4, text="new"),
        ev("turn.completed", 5),
    ]
    client, facade, _ = make_client(facade)
    out, err = io.StringIO(), io.StringIO()
    reader = scripted_reader("again", "/exit")
    code = await run_chat(client, session="s", reader=reader, stdout=out, stderr=err)
    assert code == 0
    assert facade.subscriptions[-1][1] == 3
    assert "new" in out.getvalue()
    assert "old" not in out.getvalue()


async def test_chat_switch_to_existing_session_uses_its_end():
    facade = FakeFacade()
    facade.summaries = {
        "s": SessionSummary(id="s", last_seq=0),
        "prior": SessionSummary(id="prior", last_seq=4),
    }
    facade.scripts["prior"] = [
        ev("text.delta", 1, text="stale"),
        ev("turn.completed", 4),
        ev("text.delta", 5, text="fresh"),
        ev("turn.completed", 6),
    ]
    chat, facade, _, out, _ = make_chat(scripted_reader("/exit"), facade=facade)
    await chat._cmd_sessions(("prior",))
    await chat._turn("again")
    assert facade.subscriptions[-1][0] == "prior"
    assert facade.subscriptions[-1][1] == 4
    assert "fresh" in out.getvalue()
    assert "stale" not in out.getvalue()


async def test_uds_transport_translates_stream_failures():
    class _GoneClient:
        client_id = "c"

        async def subscribe(self, session, from_seq=0, *, follow=True):
            class _Sub:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise HostDaemonUnavailable("daemon gone")

                async def aclose(self):
                    return None

            return _Sub()

        async def close(self):
            return None

    transport = UdsTransport(_GoneClient())
    with pytest.raises(TransportClosed):
        _ = [event async for event in transport.events("s", 0, follow=True)]


async def test_client_list_tools_round_trips():
    client, facade, _ = make_client()
    tools = await client.list_tools()
    assert tools[0]["name"] == "Read"
    assert any(isinstance(command, p.ToolsList) for command in facade.commands)


# ---------------------------------------------------------------------------
# Optional dependency and command parsing
# ---------------------------------------------------------------------------


def test_optional_dependency_absent_degrades_cleanly(monkeypatch):
    def boom():
        raise ImportError("no prompt_toolkit")

    monkeypatch.setattr(keys, "_load", boom)
    assert available() is False
    reader = keys.make_reader()
    assert isinstance(reader, keys.StdinReader)
    with pytest.raises(CliDependencyError):
        keys.require()


def test_commands_parse_and_help():
    assert commands.is_command("/new")
    assert not commands.is_command("hello")
    parsed = commands.parse("/export json")
    assert parsed is not None
    assert parsed.name == "/export"
    assert parsed.args == ("json",)
    assert commands.parse("hello") is None
    assert "/fork" in commands.help_text()
    assert commands.BY_NAME["/cancel"].summary


def test_continuation_rules():
    assert commands.is_continuation("keep going \\")
    assert not commands.is_continuation("done")
    assert commands.strip_continuation("keep going \\") == "keep going "
    assert commands.is_continuation('code """')


def test_ui_client_imports_do_not_require_prompt_toolkit():
    # The optional extra is never imported at module scope, so the client works
    # with prompt_toolkit absent and `available()` is simply False.
    import sys

    assert "prompt_toolkit" not in sys.modules or not available()
    stream = io.StringIO()
    writer = jsonl.JsonlWriter(stream)
    writer.write(ev("text", 1, text="hi"))
    assert json.loads(stream.getvalue())["type"] == "text"
    assert jsonl.TERMINAL_EVENTS is not None


def test_jsonl_run_once_never_builds_an_approver():
    # A JSONL run passes no approver; the daemon applies on_unattended.
    client, facade, _ = make_client()
    facade.scripts["s"] = [*COMPLETED, ev("permission.requested", 6, id="r1")]
    asyncio.run(
        run_once(
            client,
            session="s",
            content="hi",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            json_output=True,
        )
    )
    assert not any(isinstance(c, p.PermissionResolve) for c in facade.commands)


def test_exit_code_helper():
    assert exit_code(ev("turn.completed", 1)) == 0
    assert exit_code(ev("turn.failed", 1)) == 1
    assert exit_code(ev("turn.cancelled", 1)) == 130


def test_client_error_hierarchy():
    assert issubclass(FacadeError, ClientError)
    assert issubclass(ProtocolVersionError, ClientError)
    assert issubclass(DaemonUnavailable, ClientError)
    assert Path(__file__).exists()
