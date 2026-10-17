"""Native snapshot keeps labelled tool data and terminal-safe text."""
from types import SimpleNamespace

from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import TurnView, ToolCallView, MessageView, BlockView


def test_snapshot_title_uses_current_host_summary_without_brand_or_id():
    from nexus.ui.ratatui.actions import ShellActions

    controller = SimpleNamespace(view=initial_state("opaque-session-id"), session="opaque-session-id")
    shell = ShellActions(controller)
    shell.refresh_session_tabs([
        {"id": "other", "title": "Other session", "workspace": ".", "state": "idle", "status": "idle"},
        {"id": "opaque-session-id", "title": "Fix the parser", "workspace": ".", "state": "idle", "status": "idle"},
    ])
    assert project(controller, 1, shell=shell)["title"] == "Fix the parser"
    shell.sessions[1]["title"] = "Renamed session"
    assert project(controller, 2, shell=shell)["title"] == "Renamed session"
    shell.sessions[1]["title"] = ""
    assert project(controller, 3, shell=shell)["title"] == "New session"
    assert project(controller, 4)["title"] == "New session"


def test_session_title_escapes_terminal_controls_and_ignores_other_sessions():
    from nexus.ui.ratatui.prototype import _session_title

    shell = SimpleNamespace(sessions=[{"id": "current", "title": "Hello\x1b[2J"}])
    assert "\x1b" not in _session_title("current", shell)
    assert _session_title("unknown", shell) == "New session"


def test_projection_preserves_tool_fields_and_escapes_controls():
    view = initial_state("native")
    view.turns.append(TurnView(index=1, messages=[MessageView(blocks=[
        BlockView(text="hello\x1b[2J")])], tools=[ToolCallView(
            name="Read", input={"path": "sample.py"},
            result=[{"type": "text", "text": "entire output"}])]))
    snapshot = project(SimpleNamespace(view=view), 3)
    text = "\n".join(snapshot["lines"])
    assert snapshot["schema"] == 1
    assert snapshot["revision"] == 3
    assert "sample.py" in text
    assert "entire output" in text
    assert "Parameters" in text
    assert "\x1b" not in text


def test_native_blocks_follow_durable_conversation_order():
    view = initial_state("ordered")
    view.turns.append(TurnView(id="turn", messages=[
        MessageView(id="first", role="assistant", event_seq=2, blocks=[BlockView(kind="thinking", text="inspect")]),
        MessageView(id="last", role="assistant", event_seq=5, blocks=[BlockView(text="answer")])],
        tools=[ToolCallView(call_id="read", name="Read", event_seq=3)]))
    controller = SimpleNamespace(view=view)
    from nexus.ui.ratatui.actions import ShellActions
    shell = ShellActions(controller)
    shell.local_transcript = True
    blocks = project(controller, 1, shell=shell)["blocks"]
    ids = [block["id"] for block in blocks]
    assert ids.index("turn:activity:firstthinking") < ids.index("lasttext")
    group = next(block for block in blocks if block["kind"] == "tool_group")
    assert [member["id"] for member in group["members"]] == ["firstthinking", "read"]
    assert group["text"] == "Read 1 file"


def test_shutdown_cancel_renders_no_error_row_but_failures_still_do():
    from nexus.events import Event, SHUTDOWN_CANCEL_REASON
    from nexus.view import fold

    def blocks(kind, data):
        events = [Event(id=f"e{i}", session="s", seq=i, ts=float(i), type=t, turn="t", data=d)
                  for i, (t, d) in enumerate([("turn.started", {}), (kind, {**data, "iterations": 1})], 1)]
        view = fold(events)
        return project(SimpleNamespace(view=view, session="s"), 1)["blocks"]

    assert not any(b["kind"] == "error" for b in blocks("turn.cancelled", {"reason": SHUTDOWN_CANCEL_REASON}))
    assert any(b.get("text") == "Error: boom" for b in blocks("turn.failed", {"error": "boom"}))


def test_refresh_branch_reads_current_workspace_and_follows_tab_changes(tmp_path):
    import asyncio
    from nexus.ui.ratatui.prototype import refresh_branch

    def repo(name, branch):
        root = tmp_path / name
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        return root

    a, b, plain = repo("a", "main"), repo("b", "feature"), tmp_path / "plain"
    plain.mkdir()
    shell = SimpleNamespace(workspace=str(a), breadcrumb="", branch="", worktree="x")

    async def scenario():
        await refresh_branch(shell)
        assert shell.breadcrumb == f"{a} › main" and shell.branch == "main" and shell.worktree == ""
        (a / ".git" / "HEAD").write_text("ref: refs/heads/topic\n")  # a shell command switched branch
        await refresh_branch(shell)
        assert shell.breadcrumb == f"{a} › topic"
        shell.workspace = str(b)  # the active tab moved to another workspace
        await refresh_branch(shell)
        assert shell.breadcrumb == f"{b} › feature"
        shell.workspace = str(plain)
        await refresh_branch(shell)
        assert shell.breadcrumb == str(plain) and shell.branch == ""

    asyncio.run(scenario())


def test_projection_cache_reuses_unchanged_turns_and_invalidates_replacements(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import Mock
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui import prototype
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("cached")
    view.turns = [TurnView(id="old", messages=[MessageView(id="m", blocks=[BlockView(text="safe\x1b[2J")])]), TurnView(id="active")]
    controller = SimpleNamespace(view=view, session="cached")
    shell = ShellActions(controller)
    original = prototype._project_turn
    spy = Mock(wraps=original)
    monkeypatch.setattr(prototype, "_project_turn", spy)
    first = project(controller, 1, shell=shell)
    assert spy.call_count == 2
    second = project(controller, 2, shell=shell)
    assert spy.call_count == 2
    assert first["blocks"] == second["blocks"]
    assert "\x1b" not in str(second["blocks"])
    view.turns[1] = replace(view.turns[1], error="changed", phase="failed")
    third = project(controller, 3, shell=shell)
    assert spy.call_count == 3
    assert any(block.get("text") == "Error: changed" for block in third["blocks"])
    assert shell.turn_cache_bytes <= 8 * 1024 * 1024
