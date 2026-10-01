"""Provider usage modal in the Textual shell (Ctrl+U, Ctrl+X U, /usage) and its shared formatting."""
from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Button, Input, Static, TextArea

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.usage import UsageScreen, render_usage
from nexus.ui_support import tui_providers, usage
from nexus.ui_support.tui_providers import ProvidersPane

NOW = 1_790_836_155.0
RESULT = p.ProvidersUsageResult(providers=[
    {"id": "codex", "label": "ChatGPT (Codex)", "plan": "Plus", "source": "chatgpt.com · wham/usage", "error": "",
     "notes": ["Limit reached: new requests wait for the next reset"],
     "windows": [{"label": "5-hour", "used_percent": 100.0, "resets_at": NOW + 2564, "reset_text": "", "detail": ""},
                 {"label": "Weekly", "used_percent": 41.0, "resets_at": NOW + 536762, "reset_text": "", "detail": ""}]},
    {"id": "claude-agent", "label": "Claude", "plan": "Max", "source": "claude CLI · /usage", "error": "", "notes": [],
     "windows": [{"label": "5-hour session", "used_percent": 59.0, "resets_at": None,
                  "reset_text": "Oct 1 at 12:19pm (Asia/Calcutta)", "detail": ""}]},
    {"id": "github-copilot", "label": "GitHub Copilot", "plan": "", "source": "", "notes": [], "windows": [],
     "error": "HTTP 401: sign in again in Settings → Providers"},
], not_connected=["OpenCode Go"], fetched_at=NOW)


class UsageTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.usage_calls = 0

    async def request(self, command):
        if isinstance(command, p.ProvidersUsage):
            self.usage_calls += 1
            return RESULT
        return await super().request(command)


def test_shared_formatting_words_a_window_the_same_way():
    window = {"used_percent": 41.0, "resets_at": NOW + 536762, "detail": "14,979 of 15,000 left"}
    assert usage.bar(window, 10) == "████░░░░░░"
    assert usage.tone(window) == "ok" and usage.tone({"used_percent": 95}) == "critical"
    assert usage.tone({"used_percent": None}) == "unknown"
    text = usage.summary(window, NOW)
    assert text.startswith("41% used · 59% left · resets in 6d 5h (") and text.endswith(") · 14,979 of 15,000 left")
    assert usage.summary({"used_percent": 0.2, "reset_text": "Oct 1 at 8pm"}) == "0.2% used · 99.8% left · resets Oct 1 at 8pm"
    assert [usage.duration(s) for s in (5, 600, 3600, 3900, 90000)] == ["now", "10m", "1h", "1h 5m", "1d 1h"]


def test_render_usage_lists_every_provider_window_note_source_and_error():
    text = render_usage(RESULT, NOW).plain
    for expected in ("ChatGPT (Codex) · Plus", "5-hour", "100% used · 0% left · resets in 42m", "Weekly",
                     "Limit reached", "Source: chatgpt.com · wham/usage", "Claude · Max", "5-hour session",
                     "resets Oct 1 at 12:19pm (Asia/Calcutta)", "GitHub Copilot", "Unavailable: HTTP 401",
                     "Not connected: OpenCode Go", "r refresh"):
        assert expected in text, expected
    empty = render_usage(p.ProvidersUsageResult()).plain
    assert "No connected provider reports usage" in empty and "Settings → Providers" in empty


@pytest.mark.asyncio
async def test_ctrl_u_opens_usage_from_the_composer_and_r_refreshes():
    transport = UsageTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        editor = app.query_one("#chat-editor", TextArea)
        editor.focus()
        editor.text = "keep this draft"
        await pilot.press("ctrl+u")
        await pilot.pause(0.1)
        assert isinstance(app.screen, UsageScreen)
        assert editor.text == "keep this draft"
        body = app.screen.query_one("#usage-body", Static).render().plain
        assert "ChatGPT (Codex) · Plus" in body and "Claude · Max" in body
        await pilot.press("r")
        await pilot.pause(0.1)
        assert transport.usage_calls == 2
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, UsageScreen)

        await pilot.press("ctrl+x", "u")
        await pilot.pause(0.1)
        assert isinstance(app.screen, UsageScreen)
        await pilot.press("escape")
        await pilot.pause()
        await app._dispatch_chat_command("/usage")
        await pilot.pause(0.1)
        assert isinstance(app.screen, UsageScreen)


class ClaudeProvidersTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.codes: list[tuple[str, int]] = []

    async def request(self, command):
        if isinstance(command, p.ProvidersStatus):
            return p.ProvidersStatusResult(providers=[
                {"id": "claude-agent", "label": "Claude (Pro/Max)", "methods": ["browser"], "help": "claude help",
                 "connected": True, "detail": "Max", "can_logout": False, "login": None}])
        if isinstance(command, p.ProviderLogin):
            return p.ProviderLoginResult(login_id="C1", provider="claude-agent", method="browser",
                                         url="https://claude.com/cai/oauth/authorize?code=true", code_entry=True)
        if isinstance(command, p.ProviderLoginCode):
            self.codes.append((command.login_id, len(command.code)))
            return p.ProviderLoginResult(login_id="C1", provider="claude-agent", message="Code sent. Finishing sign-in…",
                                         code_entry=True)
        if isinstance(command, p.ProviderLoginPoll):
            return p.ProviderLoginResult(login_id="C1", provider="claude-agent", code_entry=True)
        return await super().request(command)


@pytest.mark.asyncio
async def test_claude_card_signs_in_with_a_pasted_code_and_hides_disconnect(monkeypatch):
    monkeypatch.setattr(tui_providers, "_POLL_SECONDS", 30)
    monkeypatch.setattr(ProvidersPane, "open_links", False)
    transport = ClaudeProvidersTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        app.action_open_settings("providers")
        for _ in range(6):
            await pilot.pause()
        pane = app.screen.query_one(ProvidersPane)
        card = pane.query_one("#provider-claude-agent")
        assert card.query_one(".provider-state", Static).render().plain == "● connected · Max"
        assert not card.query_one(".provider-logout", Button).display
        field = pane.query_one("#provider-claude-code", Input)
        assert not field.display
        await pane._sign_in("claude-agent", "browser")
        await pilot.pause()
        assert field.display and field.password
        assert "paste the code" in card.query_one(".provider-flow", Static).render().plain
        field.value = "code#state"
        await pane._send_code("claude-agent")
        assert transport.codes == [("C1", 10)] and field.value == ""
        assert card.query_one(".provider-flow", Static).render().plain.startswith("Code sent")
