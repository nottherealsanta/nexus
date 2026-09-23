"""Deterministic UI rendering: colour, redaction, details, and commands.

The renderer and the pure ``details`` module must produce byte-identical plain
text for a pipe/capture and only add ANSI when colour is explicitly enabled, so
these tests snapshot plain output and assert the colour path is opt-in. The
``/details`` and ``/reconnect`` commands are driven through a fake transport, so
no daemon and no prompt_toolkit are needed.
"""
from __future__ import annotations

import io

from nexus.events import Event
from nexus.ui.cli import ChatSession, Client, TerminalRenderer, details, theme
from nexus.ui.cli.render import sanitize
from nexus.view import apply, initial_state


class FakeTransport:
    """A scripted transport for the reconnect path; requests are unused here."""

    def __init__(self, script=()):
        self.script = list(script)
        self.subscriptions: list[tuple[str, int]] = []

    async def request(self, command):  # pragma: no cover - not reached
        raise AssertionError(type(command).__name__)

    def events(self, session, from_seq=0, *, follow=True, client_id=None):
        self.subscriptions.append((session, from_seq))

        async def gen():
            for event in self.script:
                if event.seq > from_seq:
                    yield event

        return gen()

    async def aclose(self):
        return None


def ev(type_, seq, *, session="s", turn="t", **data) -> Event:
    return Event(type=type_, data=data, seq=seq, session=session, turn=turn)


def view_with(*events) -> object:
    state = initial_state("s")
    for event in events:
        state = apply(state, event)
    return state


# -- colour is opt-in --------------------------------------------------------


def test_color_enabled_honours_the_environment():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert theme.color_enabled(Tty(), {}) is True
    assert theme.color_enabled(Tty(), {"NO_COLOR": "1"}) is False
    assert theme.color_enabled(io.StringIO(), {"FORCE_COLOR": "1"}) is True
    assert theme.color_enabled(Tty(), {"TERM": "dumb"}) is False
    assert theme.color_enabled(io.StringIO(), {}) is False
    # ``NO_COLOR`` is honoured by presence, regardless of its value.
    assert theme.color_enabled(Tty(), {"NO_COLOR": ""}) is False
    assert theme.color_enabled(Tty(), {"NO_COLOR": "0"}) is False


def test_paint_is_plain_when_disabled():
    assert theme.paint("hi", "tool", enabled=False) == "hi"
    assert theme.paint("hi", "unknown", enabled=True) == "hi"
    assert theme.paint("hi", "tool", enabled=True).startswith("\x1b[")


def test_renderer_is_plain_by_default_and_coloured_when_forced():
    # Pass an explicit empty environment so the assertion does not depend on
    # the ambient FORCE_COLOR/NO_COLOR of whatever runs the suite.
    plain = io.StringIO()
    TerminalRenderer(plain, stderr=io.StringIO(), environ={}).render(
        ev("tool.completed", 1, tool="Read", duration_ms=12)
    )
    assert "\x1b[" not in plain.getvalue()
    assert "[tool] Read done (12ms)" in plain.getvalue()

    coloured = io.StringIO()
    TerminalRenderer(coloured, stderr=io.StringIO(), color=True).render(
        ev("tool.completed", 1, tool="Read")
    )
    assert "\x1b[" in coloured.getvalue()


def test_renderer_no_color_env_wins_even_for_a_tty(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    out = Tty()
    renderer = TerminalRenderer(out, stderr=io.StringIO(), environ={"NO_COLOR": "1"})
    renderer.render(ev("tool.completed", 1, tool="Read"))
    assert "\x1b[" not in out.getvalue()


def test_renderer_dedups_per_session_across_a_switch():
    """A new session's lower ``seq`` is not a replay of an earlier session."""
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO())
    renderer.render(ev("text.delta", 50, session="a", text="A"))
    assert renderer.render(ev("text.delta", 1, session="b", text="B")) is True
    assert "B" in out.getvalue()
    # Within a session the monotonic rule still suppresses a replay.
    assert renderer.render(ev("text.delta", 1, session="b", text="B")) is False


# -- untrusted text ----------------------------------------------------------


def test_sanitize_redacts_secrets_bytes_and_structures():
    redacted = sanitize("api_key=sk-abcd1234 live")
    assert "sk-abcd1234" not in redacted
    assert "\u2026" in redacted
    assert sanitize(b"\x00\x01") == "<2 bytes>"
    assert sanitize({"a": 1}) == "dict"


def test_tool_events_show_status_not_an_invented_preview():
    """The tool events carry no result/preview, so none is fabricated."""
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO(), environ={})
    renderer.render(ev("tool.requested", 1, tool="Bash", preview="ignored=secret"))
    renderer.render(ev("tool.completed", 2, tool="Bash", duration_ms=12, result="secret"))
    text = out.getvalue()
    assert "[tool] Bash" in text
    assert "done (12ms)" in text
    assert "ignored=secret" not in text
    assert "secret" not in text


def test_tool_error_escapes_controls_and_redacts():
    """A real ``tool.failed`` payload is escaped and credential-redacted."""
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO(), environ={})
    renderer.render(
        ev("tool.failed", 1, tool="Bash", error="boom\x1b[31m token=secretvalue")
    )
    text = out.getvalue()
    assert "\x1b[31m" not in text
    assert "\\x1b" in text
    assert "secretvalue" not in text


def test_permission_preview_escapes_controls_and_redacts():
    out = io.StringIO()
    renderer = TerminalRenderer(out, stderr=io.StringIO(), environ={})
    renderer.render(
        ev("permission.requested", 1, tool="Bash", key="rm\x1b[31m sk-abcd1234")
    )
    text = out.getvalue()
    assert "\x1b[31m" not in text
    assert "\\x1b" in text
    assert "sk-abcd1234" not in text


def test_streamed_text_escapes_controls_but_keeps_newlines():
    out = io.StringIO()
    TerminalRenderer(out, stderr=io.StringIO(), environ={}).render(
        ev("text.delta", 1, text="line1\nline2\tboom\x1b[31m")
    )
    assert "line1\nline2\tboom" in out.getvalue()
    assert "\x1b[31m" not in out.getvalue()

    # A finalized ``text`` on its own is rendered (deltas did not already print).
    out2 = io.StringIO()
    TerminalRenderer(out2, stderr=io.StringIO(), environ={}).render(
        ev("text", 1, text="a\rb\x07")
    )
    assert "a\\x0db\\x07" in out2.getvalue()
    assert "\x07" not in out2.getvalue()


# -- pure details ------------------------------------------------------------


def test_status_line_reports_reducer_model_and_context():
    view = view_with(
        ev("turn.started", 1),
        ev("model.started", 2, provider="anthropic", model="claude-x", iteration=1),
        ev(
            "context.assembled",
            3,
            iteration=1,
            messages=2,
            provider="anthropic",
            model="claude-x",
            tools=3,
            context={"used_tokens": 100, "input_budget": 1000, "history_budget": 800},
        ),
        ev("model.usage", 4, input=100, output=20),
    )
    line = details.status_line("s", view)
    assert "anthropic/claude-x" in line
    assert "100/1000 (10%)" in line
    assert "100\u2191 20\u2193" in line


def test_model_selected_marks_selection_until_started():
    selected = view_with(
        ev("model.selected", 1, reference="high", provider="openai", model="gpt-5", tier="high")
    )
    assert details.model_text(selected) == "openai/gpt-5 [high] (selected, next turn)"
    started = apply(selected, ev("model.started", 2, provider="openai", model="gpt-4o"))
    assert details.model_text(started) == "openai/gpt-4o"


def test_detail_lines_include_queue_permissions_agents_and_compaction():
    parent = {"id": "a1", "parent": None, "task": "research", "type": "explore", "depth": 0}
    child = {"id": "a2", "parent": "a1", "task": "dig", "type": "explore", "depth": 1}
    view = view_with(
        ev("turn.started", 1),
        ev(
            "context.assembled",
            2,
            iteration=1,
            context={
                "used_tokens": 50,
                "input_budget": 500,
                "compacted": {"strategy": "drop_oldest", "dropped": 4},
            },
        ),
        ev("input.queued", 3, queued_id="q1", queue_depth=1,
           content=[{"type": "text", "text": "later"}]),
        ev("agent.spawned", 4, agent=parent, tier="high"),
        ev("agent.spawned", 5, agent=child),
        ev("permission.requested", 6, id="r1", tool="Bash", key="rm -rf /"),
    )
    text = "\n".join(details.detail_lines("s", view))
    assert "compacted: drop_oldest" in text
    assert "queue #1 later" in text
    assert "approval Bash" in text
    assert "- spawned research" in text
    assert "  - spawned dig" in text


# -- slash commands over a fake transport ------------------------------------


def make_chat(script=()):
    async def read(prompt: str) -> str:
        raise EOFError

    transport = FakeTransport(script)
    out, err = io.StringIO(), io.StringIO()
    chat = ChatSession(
        Client(transport), "s", reader=read, stdout=out, stderr=err, color=False
    )
    return chat, transport, out, err


async def test_details_and_reconnect_commands():
    chat, transport, out, _err = make_chat(
        [ev("text.delta", 3, text="tail"), ev("turn.completed", 4)]
    )
    chat._ingest(ev("model.started", 1, provider="openai", model="gpt-5"))
    chat._cursors["s"] = 2
    await chat._cmd_details(())
    await chat._cmd_reconnect(())
    assert "model: openai/gpt-5" in out.getvalue()
    assert "reconnected from seq 2" in out.getvalue()
    assert "tail" in out.getvalue()
    assert transport.subscriptions == [("s", 2)]


async def test_run_chat_builds_a_live_status_toolbar(monkeypatch):
    """`run_chat` builds the editor with the status toolbar the CLI relies on.

    The CLI pre-building the reader is what once kept the toolbar and
    ``patch_stdout`` wiring dead; this pins that ``run_chat`` passes a callable
    ``bottom_toolbar`` and the chosen stream to the editor it builds.
    """
    import nexus.ui.cli.app as app_mod
    from nexus.host import protocol as p
    from nexus.session.manager import SessionSummary
    from nexus.ui.cli import run_chat

    class Facade:
        async def request(self, command):
            if isinstance(command, p.Health):
                return p.HealthResult(ok=True, version=p.PROTOCOL_VERSION, sessions=0)
            if isinstance(command, p.SessionOpen):
                return p.SessionOpenResult(session=SessionSummary(id="s"))
            raise AssertionError(type(command).__name__)

        def events(self, session, from_seq=0, *, follow=True, client_id=None):
            async def gen():
                for _ in ():
                    yield

            return gen()

        async def aclose(self):
            return None

    seen: dict = {}

    def fake_make_reader(**kwargs):
        seen.update(kwargs)

        async def read(prompt):
            raise EOFError

        return read

    monkeypatch.setattr(app_mod, "make_reader", fake_make_reader)
    out, err = io.StringIO(), io.StringIO()
    code = await run_chat(Client(Facade()), session="s", stdout=out, stderr=err)
    assert code == 0
    assert seen["stdout"] is out
    assert callable(seen["bottom_toolbar"])
    assert seen["bottom_toolbar"]().startswith("[s]")
