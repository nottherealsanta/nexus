"""Native snapshot keeps labelled tool data and terminal-safe text."""
from types import SimpleNamespace

from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import TurnView, ToolCallView, MessageView, BlockView


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
    assert "PARAMETERS" in text
    assert "\x1b" not in text


def test_native_blocks_follow_durable_conversation_order():
    view = initial_state("ordered")
    view.turns.append(TurnView(id="turn", messages=[
        MessageView(id="first", role="assistant", event_seq=2, blocks=[BlockView(kind="thinking", text="inspect")]),
        MessageView(id="last", role="assistant", event_seq=5, blocks=[BlockView(text="answer")])],
        tools=[ToolCallView(call_id="read", name="Read", event_seq=3)]))
    blocks = project(SimpleNamespace(view=view), 1)["blocks"]
    ids = [block["id"] for block in blocks]
    assert ids.index("firstthinking") < ids.index("read") < ids.index("lasttext")


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
