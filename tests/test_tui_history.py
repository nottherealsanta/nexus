"""Prompt recall is bounded and survives a new terminal client."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.widgets import ChatEditor
from nexus.ui_support.tui_history import append_history, load_history


def test_prompt_history_bounds_and_round_trip(tmp_path):
    path = tmp_path / "prompt_history"
    for index in range(505):
        append_history(f"prompt {index}", path)
    rows = load_history(path)
    assert len(rows) == 500
    assert rows[0] == "prompt 5" and rows[-1] == "prompt 504"
    append_history("x" * 4097, path)
    assert load_history(path) == rows


@pytest.mark.asyncio
async def test_empty_editor_recalls_history_and_down_restores_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    append_history("first prompt")
    append_history("second prompt")
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        await pilot.press("up")
        assert editor.text == "second prompt"
        await pilot.press("up")
        assert editor.text == "first prompt"
        await pilot.press("down", "down")
        assert editor.text == ""
