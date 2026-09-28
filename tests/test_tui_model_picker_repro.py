"""Focused end-to-end reproduction for the Textual model picker command."""

from __future__ import annotations

import pytest

from nexus.host import protocol as p
from nexus.session.manager import SessionSummary
from nexus.ui.cli.client import Client
from nexus.ui.tui.agent_picker import AgentPicker, AgentPickerPanel
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.widgets import ChatEditor, RootAgentBar


class ModelPickerTransport:
    """Small protocol fake with authoritative, mutable model metadata."""

    def __init__(self) -> None:
        self.commands: list[p.Command] = []
        self.current = ("fake", "initial")
        self.current_by_session: dict[str, tuple[str, str]] = {}
        self.reject_selection = False
        self.reject_effort_selection = False
        self.stored_override: str | None = None
        self.agent_default: str | None = None
        self.models = [
            {"provider": "fake", "id": "chosen", "name": "A Chosen Display Name",
             "tier": "medium", "supported_efforts": ["low", "high"]}
        ]

    async def request(self, command):
        self.commands.append(command)
        if isinstance(command, p.Health):
            return p.HealthResult()
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(SessionSummary(id=command.session))
        if isinstance(command, p.SessionState):
            return p.SessionStateResult(session=command.session, view={})
        if isinstance(command, p.AgentCurrent):
            provider, model = self.current_by_session.get(command.session, self.current)
            selected = next(
                (row for row in self.models if row["provider"] == provider and row["id"] == model),
                {},
            )
            supported = selected.get("supported_efforts", ["low", "high"])
            effective = (
                self.stored_override if self.stored_override in supported
                else self.agent_default if self.agent_default in supported else None
            )
            return p.AgentCurrentResult(
                session=command.session,
                name="general",
                source="default",
                provider=provider,
                model=model,
                supported_levels=supported,
                reasoning_effort=effective,
                stored_override=self.stored_override,
                reasoning_effort_source=(
                    "session" if self.stored_override and effective == self.stored_override
                    else "agent" if effective == self.agent_default and effective else None
                ),
            )
        if isinstance(command, p.AgentsList):
            return p.AgentsListResult(
                agents=[{"name": "general", "description": "General", "contexts": ["root"]}]
            )
        if isinstance(command, p.ModelsList):
            return p.ModelsListResult(
                count=len(self.models),
                models=self.models,
            )
        if isinstance(command, p.ModelSelect):
            if self.reject_selection:
                return p.ErrorResult(kind="InvalidModel", message="selection rejected")
            provider, _, model = command.ref.partition("/")
            selected = next(
                (row for row in self.models if row["provider"] == provider and row["id"] == model),
                None,
            )
            if selected is None:
                return p.ErrorResult(kind="InvalidModel", message=f"unknown model {command.ref}")
            self.current = (provider, model)
            self.current_by_session[command.session] = self.current
            return p.ModelSelectResult(
                session=command.session,
                reference=command.ref,
                provider=provider,
                model=model,
                tier=selected.get("tier", "medium"),
            )
        if isinstance(command, p.ReasoningEffortSelect):
            if self.reject_effort_selection:
                return p.ErrorResult(kind="InvalidEffort", message="effort rejected")
            self.stored_override = command.effort
            return p.ReasoningEffortSelectResult(
                session=command.session,
                stored_override=command.effort,
                effective_effort=(command.effort or self.agent_default),
                source="session" if command.effort else "agent" if self.agent_default else None,
                supported_levels=["low", "high"],
            )
        raise AssertionError(f"unexpected protocol command: {command!r}")

    async def _events(self, *_args, **_kwargs):
        if False:
            yield None

    def events(self, *args, **kwargs):
        return self._events(*args, **kwargs)

    async def aclose(self):
        return None


def _commands(transport, command_type):
    return [command for command in transport.commands if isinstance(command, command_type)]


async def _type_command(pilot, editor: ChatEditor, text: str) -> None:
    editor.focus()
    await pilot.pause()
    for key in text:
        await pilot.press(key)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_typing_model_and_enter_opens_picker_with_one_models_request():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="picker-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app.query_one(ChatEditor), "/model")

        model_list_requests = _commands(transport, p.ModelsList)
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert panel.display and len(model_list_requests) == 1, (
            "expected visible inline picker and exactly one ModelsList request after /model; "
            f"panel_visible={panel.display}, ModelsList requests={len(model_list_requests)}, "
            f"protocol commands={[type(command).__name__ for command in transport.commands]}, "
            f"editor={app.query_one(ChatEditor).text!r}, "
            f"completion_visible={app.query_one(ChatEditor).parent.completion_visible}"
        )

        await pilot.press("enter")
        await pilot.pause(0.1)
        assert p.ModelSelect(session="picker-session", ref="fake/chosen") in transport.commands
        root_bar = app.query_one(RootAgentBar)
        assert "chosen" in root_bar.summary().plain
        assert "fake" in root_bar.summary().plain


@pytest.mark.asyncio
async def test_direct_model_reference_uses_model_select_without_opening_picker():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="direct-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app.query_one(ChatEditor), "/model fake/chosen")

        assert not app.query_one("#inline-picker", AgentPickerPanel).display
        assert len(_commands(transport, p.ModelsList)) <= 1
        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="direct-session", ref="fake/chosen")
        ]
        assert "chosen" in app.query_one(RootAgentBar).summary().plain


@pytest.mark.asyncio
async def test_picker_keyboard_selection_sends_model_select_and_updates_root_metadata():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="picker-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert panel.display
        assert len(_commands(transport, p.ModelsList)) == 1
        option = panel.query_one("#agent-options").get_option_at_index(0)
        assert "A Chosen Display Name" in str(option.prompt)
        assert "Fake/chosen" not in str(option.prompt)

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert p.ModelSelect(session="picker-session", ref="fake/chosen") in transport.commands
        assert transport.current == ("fake", "chosen")
        root_bar = app.query_one(RootAgentBar)
        assert "chosen" in root_bar.summary().plain
        assert "fake" in root_bar.summary().plain


@pytest.mark.asyncio
async def test_inline_picker_owns_navigation_escape_and_restores_composer_focus():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="focus-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        await app._push_model_picker()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        options = panel.query_one("#agent-options")
        assert panel.region.y + panel.region.height <= editor.region.y
        assert app.focused is options

        await pilot.press("down")
        assert app.focused is panel.query_one("#agent-options")
        await pilot.press("escape")
        await pilot.pause()

        assert not panel.display
        assert app.focused is editor
        assert _commands(transport, p.ModelSelect) == []


@pytest.mark.asyncio
async def test_picker_uses_canonical_ids_when_display_name_is_missing_and_uppercases_agent_label():
    transport = ModelPickerTransport()

    async def list_models(*, selectable_only=False, **_kwargs):
        return [{"provider": "fake", "id": "fallback", "name": "", "tier": "low"}]

    transport_client = Client(transport)
    transport_client.list_models = list_models
    app = NexusTextualApp(transport_client, session="canonical-picker")
    selected: list[str | None] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(
            AgentPicker(
                [{"name": "general", "description": "General", "contexts": ["root"]}],
                current="general",
            ),
            callback=selected.append,
        )
        await pilot.pause()
        picker = app.screen
        option = picker.query_one("#agent-options").get_option_at_index(0)
        assert str(option.prompt).startswith("General  General ◀")
        await pilot.press("enter")
        await pilot.pause()
        assert selected == ["general"]

        await app._push_model_picker()
        await pilot.pause()
        option = app.query_one("#inline-picker", AgentPickerPanel).query_one("#agent-options").get_option_at_index(0)
        assert str(option.prompt).startswith("fake/fallback")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert p.ModelSelect(session="canonical-picker", ref="fake/fallback") in transport.commands


@pytest.mark.asyncio
async def test_reasoning_effort_cycle_uses_only_advertised_levels():
    from types import SimpleNamespace

    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="effort-session")
    current = SimpleNamespace(
        name="general", source="default", supported_levels=["low", "xhigh"],
        reasoning_effort="low", provider=None, model=None,
    )
    selections: list[str | None] = []

    async def current_agent(_session):
        return current

    async def select_effort(_session, effort):
        selections.append(effort)

    app.controller.client.current_agent = current_agent
    app.controller.client.select_reasoning_effort = select_effort
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#chat-editor").focus()
        await app.action_cycle_reasoning_effort()
        assert selections == ["xhigh"]

        current.supported_levels = []
        await app.action_cycle_reasoning_effort()
        assert selections == ["xhigh"]


@pytest.mark.asyncio
async def test_model_picker_escape_cancels_without_selecting():
    transport = ModelPickerTransport()
    transport.stored_override = "high"
    app = NexusTextualApp(Client(transport), session="cancel-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        assert app.query_one("#inline-picker", AgentPickerPanel).display

        await pilot.press("escape")
        await pilot.pause()

        assert not app.query_one("#inline-picker", AgentPickerPanel).display
        assert _commands(transport, p.ModelsList)
        assert _commands(transport, p.ModelSelect) == []
        assert _commands(transport, p.ReasoningEffortSelect) == []
        assert transport.stored_override == "high"


@pytest.mark.asyncio
async def test_model_picker_effort_is_pending_until_enter_and_commits_after_model():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="pending-effort")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        options = panel.query_one("#agent-options")
        assert app.focused is options
        assert "model default" in panel.query_one("#picker-help").render().plain

        await pilot.press("right")
        await pilot.pause()
        assert "Effort: low" in panel.query_one("#picker-help").render().plain
        assert _commands(transport, p.ModelSelect) == []
        assert _commands(transport, p.ReasoningEffortSelect) == []

        await pilot.press("right")
        assert "Effort: high" in panel.query_one("#picker-help").render().plain
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="pending-effort", ref="fake/chosen")
        ]
        assert _commands(transport, p.ReasoningEffortSelect) == [
            p.ReasoningEffortSelect(session="pending-effort", effort="high")
        ]
        assert transport.commands.index(_commands(transport, p.ModelSelect)[0]) < transport.commands.index(
            _commands(transport, p.ReasoningEffortSelect)[0]
        )


@pytest.mark.asyncio
async def test_enter_on_current_model_preserves_stored_effort_without_effort_mutation():
    transport = ModelPickerTransport()
    transport.current = ("fake", "chosen")
    transport.current_by_session["keep-current"] = transport.current
    transport.stored_override = "high"
    app = NexusTextualApp(Client(transport), session="keep-current")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert "Effort: high (keep)" in panel.query_one("#picker-help").render().plain

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="keep-current", ref="fake/chosen")
        ]
        assert _commands(transport, p.ReasoningEffortSelect) == []
        assert app.controller.stored_override == "high"


@pytest.mark.asyncio
async def test_enter_on_current_model_preserves_effective_agent_default_effort():
    transport = ModelPickerTransport()
    transport.current = ("fake", "chosen")
    transport.current_by_session["keep-agent-default"] = transport.current
    transport.agent_default = "low"
    app = NexusTextualApp(Client(transport), session="keep-agent-default")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert "Effort: low (agent; preserve)" in panel.query_one("#picker-help").render().plain

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="keep-agent-default", ref="fake/chosen")
        ]
        assert _commands(transport, p.ReasoningEffortSelect) == [
            p.ReasoningEffortSelect(session="keep-agent-default", effort="low")
        ]
        assert app.controller.reasoning_effort == "low"
        assert app.controller.stored_override == "low"


@pytest.mark.asyncio
async def test_new_model_keeps_supported_stored_effort_without_reapplying_it():
    transport = ModelPickerTransport()
    transport.current = ("fake", "initial")
    transport.stored_override = "high"
    transport.models = [
        {"provider": "fake", "id": "chosen", "name": "Chosen", "tier": "medium",
         "supported_efforts": ["low", "high"]},
        {"provider": "fake", "id": "other", "name": "Other", "tier": "medium",
         "supported_efforts": ["low", "high"]},
    ]
    app = NexusTextualApp(Client(transport), session="keep-override")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="keep-override", ref="fake/chosen")
        ]
        assert _commands(transport, p.ReasoningEffortSelect) == []
        assert transport.stored_override == "high"


@pytest.mark.asyncio
async def test_incompatible_stored_effort_is_kept_dormant_and_not_sent_to_new_model():
    transport = ModelPickerTransport()
    transport.stored_override = "high"
    transport.models = [
        {"provider": "fake", "id": "limited", "name": "Limited", "tier": "medium",
         "supported_efforts": ["low"]},
    ]
    app = NexusTextualApp(Client(transport), session="dormant-override")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert "Dormant high (kept; unsupported)" in panel.query_one("#picker-help").render().plain

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="dormant-override", ref="fake/limited")
        ]
        assert _commands(transport, p.ReasoningEffortSelect) == []
        assert transport.stored_override == "high"
        assert app.controller.stored_override == "high"
        assert app.controller.reasoning_effort is None


@pytest.mark.asyncio
async def test_default_clear_is_committed_only_after_explicit_effort_cycle():
    transport = ModelPickerTransport()
    transport.current = ("fake", "chosen")
    transport.current_by_session["clear-override"] = transport.current
    transport.stored_override = "high"
    app = NexusTextualApp(Client(transport), session="clear-override")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert "Default / clear (apply)" in app.query_one(
            "#inline-picker", AgentPickerPanel
        ).query_one("#picker-help").render().plain
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ReasoningEffortSelect) == [
            p.ReasoningEffortSelect(session="clear-override", effort=None)
        ]
        assert transport.stored_override is None


@pytest.mark.asyncio
async def test_header_agent_and_model_targets_are_distinct_and_keyboard_focusable():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="header-targets")
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.query_one(ChatEditor)
        editor.text = "preserve this draft"
        agent_link = app.query_one("#root-agent-name")
        agent_link.focus()
        await pilot.press("enter")
        await pilot.pause()
        panel = app.query_one("#inline-picker", AgentPickerPanel)
        assert panel.display and app._inline_picker_kind == "agent"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is agent_link
        assert editor.text == "preserve this draft"

        model_link = app.query_one("#root-model")
        model_link.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert panel.display and app._inline_picker_kind == "model"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is model_link
        assert editor.text == "preserve this draft"


@pytest.mark.asyncio
async def test_effort_failure_after_model_selection_refreshes_authoritative_state():
    transport = ModelPickerTransport()
    transport.reject_effort_selection = True
    app = NexusTextualApp(Client(transport), session="partial-commit")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        await pilot.press("right", "enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="partial-commit", ref="fake/chosen")
        ]
        assert _commands(transport, p.ReasoningEffortSelect) == [
            p.ReasoningEffortSelect(session="partial-commit", effort="low")
        ]
        assert app.controller.model == "chosen"
        status = app.query_one("#connection-status").render().plain
        assert "Model selected, but effort update failed" in status
        assert "effort rejected" in status


@pytest.mark.asyncio
async def test_model_then_effort_commit_aborts_second_step_after_session_switch():
    import asyncio

    from nexus.ui.tui.controller import TuiController

    transport = ModelPickerTransport()
    client = Client(transport)
    selected = asyncio.Event()
    release = asyncio.Event()
    effort_calls = []

    async def select_model(session, ref):
        selected.set()
        await release.wait()
        return await Client(transport).select_model(session, ref)

    async def select_effort(session, effort):
        effort_calls.append((session, effort))

    client.select_model = select_model
    client.select_reasoning_effort = select_effort
    controller = TuiController(client, "old-session")
    task = asyncio.create_task(controller.select_model_and_effort("fake/chosen", "high"))
    await selected.wait()
    await controller.switch_session("new-session")
    release.set()
    await task

    assert controller.session == "new-session"
    assert effort_calls == []


@pytest.mark.asyncio
async def test_invalid_direct_model_reference_is_reported_in_visible_status():
    transport = ModelPickerTransport()
    app = NexusTextualApp(Client(transport), session="invalid-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app.query_one(ChatEditor), "/model absent/nope")

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="invalid-session", ref="absent/nope")
        ]
        status = app.query_one("#connection-status").render().plain
        assert "Command failed" in status
        assert "unknown model absent/nope" in status


@pytest.mark.asyncio
async def test_picker_model_rejection_reports_error_and_preserves_current_metadata():
    transport = ModelPickerTransport()
    transport.reject_selection = True
    app = NexusTextualApp(Client(transport), session="rejected-session")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._push_model_picker()
        await pilot.pause()
        assert app.query_one("#inline-picker", AgentPickerPanel).display

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert _commands(transport, p.ModelSelect) == [
            p.ModelSelect(session="rejected-session", ref="fake/chosen")
        ]
        root_bar = app.query_one(RootAgentBar)
        assert "initial" in root_bar.summary().plain
        assert "fake" in root_bar.summary().plain
        status = app.query_one("#connection-status").render().plain
        assert "Model selection failed" in status
        assert "selection rejected" in status
        assert "model ->" not in status


@pytest.mark.asyncio
async def test_session_switch_refreshes_model_metadata_from_host():
    transport = ModelPickerTransport()
    transport.current_by_session["next-session"] = ("fresh", "session-model")
    app = NexusTextualApp(Client(transport), session="first-session")
    async with app.run_test() as pilot:
        await pilot.pause()

        await app._dispatch_chat_command("/new next-session")
        await pilot.pause()

        assert p.AgentCurrent(session="next-session") in transport.commands
        assert app.controller.provider == "fresh"
        assert app.controller.model == "session-model"
        visible = app.query_one(RootAgentBar).summary().plain
        assert "fresh" in visible and "session-model" in visible
