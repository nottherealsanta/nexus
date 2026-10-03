"""Settings → Models, Session titles and the agent Tiers row in the Textual shell.

Plan: plans/SESSION_TITLE_PLAN.md. Same rows and help text as the native client;
every change is a host command.
"""
from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Button, Checkbox, OptionList, Static, Switch, TextArea

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_models import ModelsPane, TierEditorScreen, TitlesPane
from nexus.ui_support.tui_settings import SettingsConsole

AGENT = "---\nname: helper\ndescription: Helps.\ncontexts: [subagent]\ntiers: [low, medium]\n---\nWork.\n"


def _rows():
    return [
        {"name": "low", "refs": ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], "source": "your list",
         "resolved": "openai/gpt-5-mini", "runnable": True, "editable": True},
        {"name": "medium", "refs": [], "source": "by price", "resolved": "openai/gpt-5", "runnable": True, "editable": True},
        {"name": "high", "refs": ["anthropic/claude-opus-5"], "source": "built-in", "resolved": "", "runnable": False, "editable": True},
    ]


class TierTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.rows = _rows()
        self.max_tier = "high"
        self.titles = {"enabled": True, "model": "low", "resolved": "openai/gpt-5-mini", "message": ""}
        self.agent_body = AGENT
        self.writes: list[tuple] = []

    def _tiers(self):
        return p.ModelTiersResult(order=["low", "medium", "high"], default="medium", tiers=self.rows, max_tier=self.max_tier)

    async def request(self, command):
        if isinstance(command, p.ModelTiers):
            return self._tiers()
        if isinstance(command, p.ModelTierSet):
            self.writes.append(("ModelTierSet", command.tier, list(command.refs)))
            row = next(row for row in self.rows if row["name"] == command.tier)
            row.update(refs=list(command.refs), source="your list")
            return self._tiers()
        if isinstance(command, p.ModelTierReset):
            self.writes.append(("ModelTierReset", command.tier))
            next(row for row in self.rows if row["name"] == command.tier).update(source="built-in")
            return self._tiers()
        if isinstance(command, p.AgentMaxTierSet):
            self.writes.append(("AgentMaxTierSet", command.tier))
            self.max_tier = command.tier
            return self._tiers()
        if isinstance(command, p.SessionTitleSettings):
            return p.SessionTitleSettingsResult(**self.titles)
        if isinstance(command, p.SessionTitleSettingsSet):
            self.writes.append(("SessionTitleSettingsSet", command.enabled, command.model))
            if command.enabled is not None:
                self.titles["enabled"] = command.enabled
            if command.model is not None:
                self.titles["model"] = command.model
            return p.SessionTitleSettingsResult(**self.titles)
        if isinstance(command, p.SettingsInventory):
            return p.SettingsInventoryResult(
                scope=command.scope, root_display="~/.nexus",
                categories=[p.SettingsCategory(key="agents", label="Agents", count=1)],
                items=[p.SettingsItem(category="agents", id="helper", label="helper", summary="Helps.")])
        if isinstance(command, p.SettingsRead):
            return p.SettingsReadResult(body=self.agent_body, rel_path="agents/helper.md", builtin=False, sha256="h1")
        if isinstance(command, p.SettingsWrite):
            self.writes.append(("SettingsWrite", command.body))
            self.agent_body = command.body
            return p.SettingsWriteResult(status="written", sha256="h2")
        if isinstance(command, p.ModelShow):
            return p.ModelShowResult(ref=command.ref, found=True, model={"tier": "high"})
        return await super().request(command)


async def _settle(pilot, times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


def _text(widget) -> str:
    return widget.render().plain


def _button(root, name: str) -> Button:
    return next(button for button in root.query(Button) if button.name == name)


async def _open_agent(app, pilot):
    app.action_open_settings("agents")
    await _settle(pilot)
    screen = app.screen
    screen._visible_items = [p.SettingsItem(category="agents", id="helper", label="helper", summary="Helps.")]
    await screen._open_item(0)
    await _settle(pilot, 12)
    return screen


@pytest.mark.asyncio
async def test_models_pane_lists_tiers_and_edits_a_tier_through_host_commands():
    transport = TierTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app.action_open_settings("models")
        await _settle(pilot)
        screen = app.screen
        assert isinstance(screen, SettingsConsole) and screen.category == "models"
        pane = screen.query_one(ModelsPane)
        labels = [_text(item) for item in pane.query("#models-tiers .settings-label")]
        assert labels == [
            "low · your list · 2 models · runs on openai/gpt-5-mini",
            "medium · by price · runs on openai/gpt-5",
            "high · built-in · 1 model · no runnable model",
        ]
        pane.query(".models-edit").first().press()
        await _settle(pilot)
        editor = app.screen
        assert isinstance(editor, TierEditorScreen)
        _button(editor, "down|0").press()
        await _settle(pilot)
        assert transport.writes[-1] == ("ModelTierSet", "low", ["anthropic/claude-haiku-4-5", "openai/gpt-5-mini"])
        _button(editor, "remove|1").press()
        await _settle(pilot)
        assert transport.writes[-1] == ("ModelTierSet", "low", ["anthropic/claude-haiku-4-5"])
        _button(editor, "remove|0").press()  # the last model
        await _settle(pilot)
        assert "at least one model" in _text(editor.query_one("#tier-status", Static))
        assert transport.writes[-1][0] == "ModelTierSet" and transport.writes[-1][2] == ["anthropic/claude-haiku-4-5"]
        editor.query_one("#tier-reset", Button).press()
        await _settle(pilot)
        assert transport.writes[-1] == ("ModelTierReset", "low")


@pytest.mark.asyncio
async def test_models_pane_sets_the_subagent_ceiling():
    transport = TierTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app.action_open_settings("models")
        await _settle(pilot)
        pane = app.screen.query_one(ModelsPane)
        assert str(pane.query_one("#models-ceiling", Button).label) == "high"
        pane.query_one("#models-ceiling", Button).press()
        await _settle(pilot)
        choices = app.screen.query_one("#choice-list", OptionList)
        assert [str(choices.get_option_at_index(i).prompt) for i in range(3)] == ["low", "medium", "high · selected"]
        choices.highlighted = 1
        choices.action_select()
        await _settle(pilot)
        assert transport.writes[-1] == ("AgentMaxTierSet", "medium")


@pytest.mark.asyncio
async def test_titles_pane_explains_and_toggles():
    transport = TierTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app.action_open_settings("titles")
        await _settle(pilot)
        pane = app.screen.query_one(TitlesPane)
        note = _text(pane.query_one("#titles-note", Static))
        assert "first message to a fast, low-cost model (low → openai/gpt-5-mini)" in note
        assert "Turn this off to use the first line of your first message as the title." in note
        assert str(pane.query_one("#settings-titles-model", Button).label) == "low → openai/gpt-5-mini"
        assert pane.query_one("#settings-titles-enabled", Switch).value is True
        # Loading the page itself never writes.
        assert not [w for w in transport.writes if w[0] == "SessionTitleSettingsSet"]
        pane.query_one("#settings-titles-enabled", Switch).value = False
        await _settle(pilot)
        assert transport.writes[-1] == ("SessionTitleSettingsSet", False, None)


@pytest.mark.asyncio
async def test_agent_form_has_a_tiers_row_and_the_dialog_saves_the_file():
    transport = TierTransport()
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        screen = await _open_agent(app, pilot)
        assert str(screen.query_one("#agent-tiers", Button).label) == "low (default), medium"
        assert screen.query_one("#agent-tiers-row").display
        assert "Which model tiers the calling agent may run" in _text(screen.query_one("#agent-tiers-note", Static))
        screen.query_one("#agent-tiers", Button).press()
        await _settle(pilot, 10)
        dialog = app.screen
        boxes = {box.name: box for box in dialog.query(Checkbox)}
        assert [boxes[name].value for name in ("low", "medium", "high")] == [True, True, False]
        boxes["high"].value = True
        await _settle(pilot)
        _button(dialog, "high").press()
        await _settle(pilot)
        dialog.query_one("#agent-tiers-save", Button).press()
        await _settle(pilot, 10)
        editor = app.screen.query_one("#settings-file-editor", TextArea)
        assert "tiers: [high, low, medium]" in editor.text
        assert str(app.screen.query_one("#agent-tiers", Button).label) == "high (default), low, medium"


@pytest.mark.asyncio
async def test_the_last_tier_cannot_be_unchecked_in_the_dialog():
    transport = TierTransport()
    transport.agent_body = AGENT.replace("[low, medium]", "[low]")
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await _open_agent(app, pilot)
        app.screen.query_one("#agent-tiers", Button).press()
        await _settle(pilot, 10)
        dialog = app.screen
        box = next(box for box in dialog.query(Checkbox) if box.name == "low")
        box.value = False
        await _settle(pilot)
        assert box.value is True
        assert "At least one tier must stay checked." in _text(dialog.query_one("#agent-tiers-error", Static))


@pytest.mark.asyncio
async def test_a_conflict_between_model_and_tiers_is_called_out():
    transport = TierTransport()
    transport.agent_body = AGENT.replace("tiers:", "model: openai/gpt-6\ntiers:")
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await _open_agent(app, pilot)
        assert "pinned model is in the high tier" in _text(app.screen.query_one("#agent-tiers-note", Static))
