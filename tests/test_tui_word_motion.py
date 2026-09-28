"""Option/Alt+Left/Right move the composer cursor by word, like Ctrl+Left/Right."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.keys import NexusXTermParser
from nexus.ui.tui.widgets import ChatEditor


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [("\x1b[1;3D", "alt+left"), ("\x1b[1;3C", "alt+right"), ("\x1b[1;4D", "alt+shift+left")],
)
def test_terminal_option_arrows_decode_to_alt_keys(sequence: str, expected: str):
    assert [event.key for event in NexusXTermParser().feed(sequence)] == [expected]


@pytest.mark.asyncio
async def test_alt_arrows_jump_and_select_by_word():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.focus()
        editor.text = "hello brave world"
        editor.move_cursor((0, 17))
        await pilot.press("alt+left")
        assert editor.cursor_location == (0, 12)
        await pilot.press("alt+left")
        assert editor.cursor_location == (0, 6)
        await pilot.press("alt+right")
        assert editor.cursor_location == (0, 11)
        await pilot.press("alt+shift+right")
        assert editor.selected_text == " world"
        assert editor.text == "hello brave world"
