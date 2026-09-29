"""Settings → Providers in the Textual shell, over the host Provider* commands."""
from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Button, Input, Static

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support import tui_providers
from nexus.ui_support.tui_providers import ProvidersPane
from nexus.ui_support.tui_settings import SettingsConsole


class ProvidersTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.connected = {"codex": True, "github-copilot": False, "opencode-go": False}
        self.polls = 0

    async def request(self, command):
        if isinstance(command, p.ProvidersStatus):
            return p.ProvidersStatusResult(providers=[
                {"id": key, "label": key, "methods": [], "help": f"help for {key}", "connected": value,
                 "detail": "github.com" if key == "github-copilot" and value else "", "login": None}
                for key, value in self.connected.items()
            ])
        if isinstance(command, p.ProviderLogin):
            self.trace.append(("ProviderLogin", command.provider, command.method, command.domain))
            return p.ProviderLoginResult(login_id="L1", provider=command.provider, method=command.method,
                                         url="https://github.com/login/device", user_code="WXYZ-0000")
        if isinstance(command, p.ProviderLoginPoll):
            self.polls += 1
            if self.polls < 2:
                return p.ProviderLoginResult(login_id="L1", provider="github-copilot", url="https://github.com/login/device",
                                             user_code="WXYZ-0000")
            self.connected["github-copilot"] = True
            return p.ProviderLoginResult(login_id="L1", provider="github-copilot", status="connected",
                                         message="Connected. Restart the daemon to use it (nexus daemon stop).")
        if isinstance(command, p.ProviderKeySet):
            self.trace.append(("ProviderKeySet", command.provider, len(command.key)))
            self.connected[command.provider] = True
            return p.ProviderAuthResult(provider=command.provider, connected=True, message="Connected.")
        if isinstance(command, p.ProviderLogout):
            self.trace.append(("ProviderLogout", command.provider))
            self.connected[command.provider] = False
            return p.ProviderAuthResult(provider=command.provider, connected=False, message="Disconnected.")
        return await super().request(command)


def _text(pane: ProvidersPane, provider: str, selector: str) -> str:
    return pane.query_one(f"#provider-{provider}").query_one(selector, Static).render().plain


async def _settle(pilot, times: int = 6) -> None:
    for _ in range(times):
        await pilot.pause()


@pytest.mark.asyncio
async def test_settings_providers_sign_in_save_key_and_disconnect(monkeypatch):
    monkeypatch.setattr(tui_providers, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ProvidersPane, "open_links", False)
    transport = ProvidersTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        app.action_open_settings("providers")
        await _settle(pilot)
        screen = app.screen
        assert isinstance(screen, SettingsConsole) and screen.category == "providers"
        pane = screen.query_one(ProvidersPane)
        assert pane.display and _text(pane, "codex", ".provider-state") == "● connected"
        assert _text(pane, "opencode-go", ".provider-help") == "help for opencode-go"
        assert pane.query_one("#provider-codex").query_one(".provider-logout", Button).display
        assert not pane.query_one("#provider-opencode-go").query_one(".provider-logout", Button).display

        assert not pane.query("#provider-copilot-domain")
        copilot_device = pane.query_one("#provider-github-copilot").query_one(".provider-action", Button)
        assert copilot_device.name == "github-copilot|device"
        assert str(copilot_device.label) == "Use a device code"

        key = pane.query_one("#provider-go-key", Input)
        key.value = "sk-go-0123456789"
        await pane._save_key("opencode-go")
        assert ("ProviderKeySet", "opencode-go", 16) in transport.trace
        assert key.value == "" and key.password
        assert _text(pane, "opencode-go", ".provider-state") == "● connected"

        await pane._logout("codex")
        assert ("ProviderLogout", "codex") in transport.trace
        assert _text(pane, "codex", ".provider-state") == "○ not connected"
