"""First-run setup behavior in the Textual client: connect a provider, then chat."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Button, Static

from nexus.client.protocol import ClientError
from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_providers import ProvidersPane
from nexus.ui_support.tui_setup import SetupScreen


class SetupTransport(FakeTransport):
    def __init__(self, *, connected=(), restart=False, fail=False):
        super().__init__()
        self.connected = set(connected)
        self.restart = restart
        self.fail = fail
        self.saved: list[tuple[str, str]] = []

    async def request(self, command):
        if isinstance(command, p.SetupStatus):
            self.trace.append("SetupStatus")
            return p.SetupStatusResult(
                required=not self.saved or self.fail,
                effective_model="fake/current",
                providers=[
                    {"id": provider, "label": label, "connected": provider in self.connected,
                     "instruction": f"Connect {label}.", "auto": provider != "ollama"}
                    for provider, label in (("ollama", "Ollama"), ("codex", "ChatGPT (Codex)"),
                                            ("openai", "OpenAI"), ("anthropic", "Anthropic"))
                ],
            )
        if isinstance(command, p.ProvidersStatus):
            return p.ProvidersStatusResult(providers=[
                {"id": "codex", "label": "ChatGPT (Codex)", "connected": "codex" in self.connected, "help": ""},
            ])
        if isinstance(command, p.SetupSave):
            self.trace.append(("SetupSave", command.provider, command.model))
            if self.fail:
                raise ClientError("catalogue unavailable")
            self.saved.append((command.provider, command.model))
            return p.SetupSaveResult(global_model=f"{command.provider}/newest", restart_required=self.restart)
        return await super().request(command)


def _saves(transport):
    return [item for item in transport.trace if isinstance(item, tuple) and item[0] == "SetupSave"]


@pytest.mark.asyncio
async def test_connected_provider_completes_setup_with_its_newest_model_without_asking():
    # Ollama is listed first and reachable, but setup never picks an Ollama model.
    transport = SetupTransport(connected=("ollama", "openai", "anthropic"))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.3)
        assert _saves(transport) == [("SetupSave", "openai", "")]
        assert not isinstance(app.screen, SetupScreen)
        # The dismissed screen is really removed; a stuck removal froze the app and quit.
        assert not app.query(SetupScreen) and not any(isinstance(node, SetupScreen) for node in app.walk_children())
        await pilot.press("ctrl+q")
        await pilot.pause(0.2)
        assert not app.is_running
        assert app._setup_required is False
        assert "Using openai/newest" in app.query_one("#connection-status").render().plain


@pytest.mark.asyncio
async def test_setup_asks_to_connect_then_finishes_with_the_first_provider_connected():
    transport = SetupTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, SetupScreen)
        assert screen.query_one("#setup-title", Static).render().plain == "Connect a provider"
        # Sign-in reuses the Settings → Providers cards; env-key providers are listed below.
        assert screen.query_one(ProvidersPane)
        assert "Connect OpenAI." in screen.query_one("#setup-env", Static).render().plain
        assert not _saves(transport)
        transport.connected.add("codex")
        # The screen's own status poll notices the sign-in.
        await pilot.pause(2.5)
        assert _saves(transport) == [("SetupSave", "codex", "")]
        assert not isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
async def test_setup_falls_back_to_a_restart_message_while_turns_run():
    transport = SetupTransport(connected=("codex",), restart=True)
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        # The saved config makes setup "done", but the message must stay until the restart.
        await pilot.pause(2.5)
        screen = app.screen
        assert isinstance(screen, SetupScreen)
        assert "Restart the daemon" in screen.query_one("#setup-message", Static).render().plain
        assert len(_saves(transport)) == 1


@pytest.mark.asyncio
async def test_failed_save_is_not_retried_until_try_again():
    transport = SetupTransport(connected=("openai",), fail=True)
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.3)
        screen = app.screen
        assert isinstance(screen, SetupScreen)
        assert "catalogue unavailable" in screen.query_one("#setup-message", Static).render().plain
        await pilot.pause(2.5)
        assert len(_saves(transport)) == 1
        assert screen.query_one("#setup-retry", Button).display
        transport.fail = False
        await pilot.click("#setup-retry")
        await pilot.pause(0.3)
        assert len(_saves(transport)) == 2
        assert not isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
async def test_old_daemon_without_setup_is_graceful():
    class LegacyTransport(FakeTransport):
        async def request(self, command):
            if isinstance(command, p.SetupStatus):
                raise AssertionError(f"unknown command {command!r}")  # noqa: TRY004
            return await super().request(command)

    app = NexusTextualApp(_client(LegacyTransport()), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.15)
        assert not isinstance(app.screen, SetupScreen)


@pytest.mark.asyncio
async def test_dismissed_setup_keeps_the_draft_and_reopens_on_send():
    transport = SetupTransport()
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
        assert isinstance(app.screen, SetupScreen)


@pytest.mark.parametrize("cls", [SetupScreen, ProvidersPane])
def test_own_state_never_shadows_textual_internals(cls):
    # SetupScreen once kept its own ``_closed`` flag; Textual reads that name as
    # "message pump closed", so removing the dismissed screen hung the app.
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cls.__init__)))
    assigned = {node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name) and node.value.id == "self"}
    base = cls.__mro__[1]
    textual = set(vars(base())) | set(dir(base))
    assert assigned and not assigned & textual


@pytest.mark.asyncio
async def test_claude_signs_in_from_its_setup_card_not_the_env_list():
    class ClaudeSetupTransport(SetupTransport):
        async def request(self, command):
            result = await super().request(command)
            if isinstance(command, p.SetupStatus):
                return p.SetupStatusResult(required=True, providers=[{
                    "id": "claude-agent", "label": "Claude Pro/Max", "connected": False,
                    "auto": True, "instruction": "Run claude auth login."}])
            return result
    app = NexusTextualApp(_client(ClaudeSetupTransport()), session="s")
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause(0.2)
        assert isinstance(app.screen, SetupScreen)
        # Claude signs in from its own card now, so it is not an environment row.
        assert app.screen.query_one("#provider-claude-agent")
        text = app.screen.query_one("#setup-env", Static).render().plain
        assert "Claude Pro/Max" not in text
