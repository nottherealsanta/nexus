"""Large composer pastes stay editable and expand only when submitted."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.events import Paste

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.widgets import (
    MAX_PASTED_CONTENT_ATTACHMENTS,
    MAX_PASTED_CONTENT_CHARS,
    ChatEditor,
    is_large_paste,
)


def composer_marker(app, number: int) -> str:
    return app.query_one("#chat-input")._paste_markers[number]


def test_large_paste_threshold_uses_lines_or_characters():
    assert not is_large_paste("\n".join(["x"] * 20))
    assert is_large_paste("\n".join(["x"] * 21))
    assert not is_large_paste("x" * 2000)
    assert is_large_paste("x" * 2001)


@pytest.mark.asyncio
async def test_small_paste_stays_inline_and_large_paste_is_an_editable_attachment():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        app.post_message(Paste("short paste"))
        await pilot.pause()
        assert editor.text == "short paste"
        assert app.query_one("#paste-attachments").display is False

        large = "\n".join(f"row {index}" for index in range(21))
        await editor._on_paste(Paste(large))
        await pilot.pause()
        composer = app.query_one("#chat-input")
        assert editor.text == "short paste" + composer_marker(app, 1)
        assert large not in editor.text
        assert composer._pasted_content == {1: large}
        assert "21 lines" in str(app.query_one(".pasted-content-label").render())

        await pilot.click(".pasted-content-label")
        await pilot.pause()
        modal = app.screen
        assert modal.__class__.__name__ == "PastedContentScreen"
        modal.query_one("#pasted-content-editor").text = "edited attachment [Pasted #2]"
        await pilot.click("#pasted-content-save")
        await pilot.pause()
        assert composer._pasted_content[1] == "edited attachment [Pasted #2]"
        assert composer._expand_pasted_content(
            "literal [Pasted #1] before " + composer_marker(app, 1) + " after"
        ) == (
            "literal [Pasted #1] before edited attachment [Pasted #2] after"
        )

        composer.add_pasted_content("second large body", editor)
        await pilot.pause()
        marker2 = composer_marker(app, 2)
        assert marker2 in editor.text
        await pilot.click("#pasted-content-remove-2")
        await pilot.pause()
        assert 2 not in composer._pasted_content
        assert marker2 not in editor.text

        composer.add_pasted_content("third large body", editor)
        await pilot.pause()
        marker3 = composer_marker(app, 3)
        editor.text = editor.text.replace(marker3, "")
        await pilot.pause()
        assert 3 not in composer._pasted_content
        assert marker3 not in editor.text
        assert not app.query("#pasted-content-remove-3")

        before_rejected_paste = editor.text
        composer.add_pasted_content("x" * (MAX_PASTED_CONTENT_CHARS + 1), editor)
        assert composer._pasted_content == {1: "edited attachment [Pasted #2]"}
        assert editor.text == before_rejected_paste
        for number in range(2, MAX_PASTED_CONTENT_ATTACHMENTS + 1):
            composer.add_pasted_content(f"body {number}", editor)
        composer.add_pasted_content("one too many", editor)
        assert len(composer._pasted_content) == MAX_PASTED_CONTENT_ATTACHMENTS

        editor.text = "ask " + composer_marker(app, 1)
        editor.move_cursor((0, len(editor.text)))
        editor.focus()
        await pilot.press("shift+enter")
        await pilot.pause()
        assert transport.last_input is None
        editor.text = "ask " + composer_marker(app, 1)
        editor.move_cursor((0, len(editor.text)))
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "start_turn" in transport.trace, transport.trace
        assert transport.last_input == "ask edited attachment [Pasted #2]"
        assert composer._pasted_content == {}


@pytest.mark.asyncio
async def test_small_paste_submits_unchanged_on_enter():
    transport = FakeTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        app.post_message(Paste("short paste"))
        editor.focus()
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert transport.last_input == "short paste"
