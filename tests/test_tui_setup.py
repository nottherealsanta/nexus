"""First-run setup behavior in the Textual client."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Button, Input, OptionList, Static

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_setup import SetupScreen


class SetupTransport(FakeTransport):
    def __init__(self, *, required=True, connected=True):
        super().__init__()
        self.required = required
        self.connected = connected
        self.saved = None

    async def request(self, command):
        if isinstance(command, p.SetupStatus):
            self.trace.append("SetupStatus")
            return p.SetupStatusResult(
                required=self.required,
                global_model="" if self.required else "openai/gpt-5",
                effective_model="fake/current",
                providers=[
                    {"id": "openai", "label": "OpenAI", "connected": self.connected,
                     "instruction": "Set OPENAI_API_KEY in the daemon environment."},
                    {"id": "ollama", "label": "Ollama", "connected": self.connected,
                     "instruction": "Local provider; run Ollama locally."},
                ],
                models=[
                    {"provider": "openai", "id": "gpt-5", "name": "GPT 5", "date": "2026-01-01"},
                    {"provider": "openai", "id": "gpt-4.1", "name": "GPT 4.1", "date": "2025-04-01"},
                    {"provider": "ollama", "id": "llama3", "name": "Llama 3", "date": ""},
                ],
            )
        if isinstance(command, p.SetupSave):
            self.trace.append(("SetupSave", command.provider, command.model))
            self.saved = (command.provider, command.model)
            return p.SetupSaveResult(global_model=f"{command.provider}/{command.model}")
        return await super().request(command)


@pytest.mark.asyncio
async def test_startup_opens_setup_and_save_guides_daemon_restart():
    transport = SetupTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, SetupScreen)
        providers = screen.query_one("#setup-providers", OptionList)
        assert "OpenAI" in str(providers.get_option_at_index(0).prompt)
        assert "Set OPENAI_API_KEY" in screen.query_one("#setup-instruction", Static).render().plain
        models = screen.query_one("#setup-models", OptionList)
        assert models.option_count == 2
        models.focus()
        await pilot.press("down", "enter")
        assert not screen.query_one("#setup-save", Button).disabled
        await pilot.click("#setup-save")
        await pilot.pause()
        assert transport.saved == ("openai", "gpt-4.1")
        text = screen.query_one("#setup-message", Static).render().plain
        assert "restart the daemon after turns finish" in text


@pytest.mark.asyncio
async def test_setup_filters_models_and_never_sends_raw_credentials():
    transport = SetupTransport()
    client = _client(transport)
    app = NexusTextualApp(client, session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        screen.query_one("#setup-search", Input)
        await pilot.click("#setup-search")
        await pilot.press("g", "p", "t", "-", "4")
        await pilot.pause()
        assert screen.query_one("#setup-models", OptionList).option_count == 1
        assert "OPENAI_API_KEY" not in repr(transport.trace)
        assert all("secret" not in str(item).lower() for item in transport.trace)


@pytest.mark.asyncio
async def test_unconnected_provider_cannot_save_and_old_daemon_is_graceful():
    transport = SetupTransport(connected=False)
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        models = screen.query_one("#setup-models", OptionList)
        models.focus()
        await pilot.press("enter")
        assert screen.query_one("#setup-save", Button).disabled
        assert not any(isinstance(item, tuple) and item[0] == "SetupSave" for item in transport.trace)

    class LegacyTransport(FakeTransport):
        async def request(self, command):
            if isinstance(command, p.SetupStatus):
                raise AssertionError(f"unknown command {command!r}")  # noqa: TRY004
            return await super().request(command)

    legacy = LegacyTransport()
    app = NexusTextualApp(_client(legacy), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.15)
        assert not isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
async def test_first_run_does_not_submit_a_turn_when_setup_is_dismissed():
    transport = SetupTransport(connected=False)
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        await pilot.click("#setup-close")
        await pilot.pause()
        editor = app.query_one("#chat-editor")
        editor.focus()
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause()
        assert "start_turn" not in transport.trace
        assert editor.text == "hello"
        assert "Connect a provider" in app.query_one("#connection-status").render().plain
