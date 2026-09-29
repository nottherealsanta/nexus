"""Every Textual dialog closes on Escape and on a click outside it."""

from __future__ import annotations

import pytest
from test_tui_setup import SetupTransport
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_panels import SettingsScreen
from nexus.ui_support.tui_setup import SetupScreen
from nexus.ui_support.tui_widgets import PastedContentScreen


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["escape", "backdrop"])
async def test_setup_screen_closes_on_escape_and_backdrop_click(close):
    app = NexusTextualApp(_client(SetupTransport()), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        assert isinstance(app.screen, SetupScreen)
        await (pilot.press("escape") if close == "escape" else pilot.click(offset=(0, 0)))
        await pilot.pause()
        assert not isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("close", ["escape", "backdrop"])
async def test_pasted_content_closes_on_escape_and_backdrop_click(close):
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    results = []
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        app.push_screen(PastedContentScreen(1, "pasted"), results.append)
        await pilot.pause()
        await (pilot.press("escape") if close == "escape" else pilot.click(offset=(0, 0)))
        await pilot.pause()
        assert not isinstance(app.screen, PastedContentScreen)
        assert results == [None]


@pytest.mark.asyncio
async def test_click_inside_dialog_keeps_it_open():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        app.push_screen(PastedContentScreen(1, "pasted"))
        await pilot.pause()
        await pilot.click("#pasted-content-title")
        await pilot.pause()
        assert isinstance(app.screen, PastedContentScreen)


@pytest.mark.asyncio
async def test_settings_closes_on_backdrop_click():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)
