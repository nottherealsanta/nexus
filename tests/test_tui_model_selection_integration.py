"""Model selection through the TUI, host facade, durable log, and runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from textual.widgets import OptionList, Static

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    ModelsSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response
from nexus.model.registry import ModelRegistry
from nexus.model.tiers import TierTable
from nexus.runtime import Runtime
from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.widgets import ChatEditor, RootAgentBar
from nexus.ui_support.tui_model_picker import ModelPickerScreen


class _FacadeTransport:
    """Expose the real HostFacade through the TUI Client transport contract."""

    def __init__(self, facade: HostFacade) -> None:
        self.facade = facade
        self.commands: list[p.Command] = []

    async def request(self, command: p.Command) -> p.Result:
        self.commands.append(command)
        return await self.facade.handle(command)

    def events(
        self,
        session: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        client_id: str | None = None,
    ) -> AsyncIterator:
        return self.facade.subscribe(
            session, from_seq, follow=follow, client_id=client_id
        )

    async def aclose(self) -> None:
        # The runtime/facade outlive each TUI instance for replay assertions.
        return None


def _make_runtime(tmp_path, provider: ScriptedProvider) -> Runtime:
    """Build two local catalogue refs, both routed to one offline script."""
    tiers = TierTable()
    registry = ModelRegistry(
        providers={"openai": {"kind": "openai"}},
        env={},
        snapshot_path=tmp_path / "no-model-snapshot.json",
        use_snapshot=False,
        offline=True,
        tier_table=tiers,
    )
    registry.install_raw(
        '{"openai":{"models":{"alpha":{"name":"Alpha",'
        '"modalities":{"output":["text"]}},"beta":{"name":"Beta",'
        '"modalities":{"output":["text"]}}}}}'
    )
    runtime = Runtime(
        tmp_path,
        config=Config(
            model="openai/alpha",
            version=2,
            v2=ConfigV2(
                agent=AgentSection(profile="coding"),
                model=ModelSection(default="openai/alpha"),
                models=ModelsSection(offline=True),
                permissions=PermissionsSection(mode="allow"),
                tools=ToolsSection(),
            ),
        ),
        providers={"openai": provider},
        registry=registry,
        tiers=tiers,
    )
    # Keep the installed in-memory fixture through Runtime's normal boundary
    # acquisition path; no catalogue fetch or model provider is used.
    runtime._registry_loaded = True
    return runtime


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


async def _type_command(pilot, editor: ChatEditor, text: str) -> None:
    editor.focus()
    await pilot.pause()
    for key in text:
        await pilot.press(key)
    await pilot.press("enter")
    await pilot.pause(0.05)


def _events(runtime: Runtime, session: str, kind: str):
    return [event for event in runtime.session(session).events if event.type == kind]


@pytest.mark.asyncio
async def test_startup_renders_checked_in_default_model_from_host_metadata(tmp_path):
    """Pin the full startup metadata path used by the CLI chat shell."""
    from pathlib import Path

    workspace = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    config = Config.load(workspace, home=home, environ={})
    assert config.model == "codex/gpt-6-luna"
    agents_dir = tmp_path / ".nexus" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "general.md").write_text(
        "---\nname: general\ndescription: test\ncontexts: [root]\nmodel: inherit\n---\n",
        encoding="utf-8",
    )
    registry = ModelRegistry(
        providers={"codex": config.v2.providers["codex"]},
        env={},
        snapshot_path=tmp_path / "no-model-snapshot.json",
        use_snapshot=False,
        offline=True,
        provider_aliases={"codex": "openai"},
    )
    registry.install_raw(
        '{"openai":{"models":{'
        '"gpt-6-luna":{"name":"GPT-6 Luna","reasoning":true,'
        '"modalities":{"input":["text"],"output":["text"]}},'
        '"gpt-5.6-luna":{"name":"GPT-5.6 Luna","reasoning":true,'
        '"modalities":{"input":["text"],"output":["text"]}}}}}'
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={
            "codex": ScriptedProvider(text_response("unused"), name="codex")
        },
        registry=registry,
    )
    runtime._registry_loaded = True
    facade = HostFacade(runtime)
    transport = _FacadeTransport(facade)
    app = NexusTextualApp(Client(transport), session="startup-model")

    try:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            header = app.query_one(RootAgentBar)
            assert config.model == "codex/gpt-6-luna"
            assert app.controller.model == "gpt-6-luna"
            assert header.summary().plain.startswith("Build  ·  GPT-6 Luna  ·  OpenAI")
            assert header.query_one("#root-model").render().plain == "GPT-6 Luna"
            assert header.query_one("#root-provider").render().plain == "OpenAI"
            assert header.query_one("#root-model").region.width > 0
            assert header.query_one("#root-provider").region.width > 0
            assert header.query_one("#root-model").region.x >= header.region.x
            assert header.query_one("#root-model").region.right <= header.region.right
            assert header.query_one("#root-provider").region.right <= header.region.right
            assert header.query_one("#root-separator-model").display
            assert header.query_one("#root-separator-provider").display
        assert p.AgentCurrent(session="startup-model") in transport.commands

        # A pre-existing durable model choice wins over the workspace default.
        await facade.handle(
            p.ModelSelect(session="persisted-model", ref="codex/gpt-5.6-luna")
        )
        persisted_app = NexusTextualApp(Client(transport), session="persisted-model")
        async with persisted_app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            persisted_header = persisted_app.query_one(RootAgentBar)
            assert persisted_app.controller.model == "gpt-5.6-luna"
            assert persisted_header.summary().plain.startswith(
                "Build  ·  GPT-5.6 Luna  ·  OpenAI"
            )
            assert runtime.session("persisted-model").model_selection.reference == (
                "codex/gpt-5.6-luna"
            )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_picker_selection_is_durable_replayed_and_session_scoped(tmp_path):
    provider = ScriptedProvider(name="openai")
    runtime = _make_runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    transport = _FacadeTransport(facade)
    facade.open_session("picker-model")
    facade.open_session("other-model")
    app = NexusTextualApp(Client(transport), session="picker-model")

    try:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await _type_command(pilot, app.query_one(ChatEditor), "/model")
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            await pilot.pause(0.1)
            options = screen.query_one("#model-picker-options", OptionList)
            selectable = runtime.registry.list(selectable_only=True)
            assert options.option_count == len(screen._visible_rows) == 3, [
                row.to_dict() for row in selectable
            ]
            assert [row["id"] if row else None for row in screen._visible_rows] == [
                None, "alpha", "beta"
            ]
            assert app.focused is app.screen.query_one("#model-picker-search")
            await pilot.press("down")
            assert app.focused is options
            await pilot.press("down", "enter")
            await _wait_for(
                lambda: any(
                    isinstance(command, p.ModelSelect)
                    for command in transport.commands
                ),
                timeout=1.0,
            )
            await _wait_for(lambda: bool(_events(runtime, "picker-model", "model.selected")))

            selected_commands = [
                command
                for command in transport.commands
                if isinstance(command, p.ModelSelect)
            ]
            assert selected_commands == [
                p.ModelSelect(session="picker-model", ref="openai/beta")
            ]
            assert "beta" in app.query_one(RootAgentBar).summary().plain
            assert "OpenAI" in app.query_one(RootAgentBar).summary().plain

        selected_event = _events(runtime, "picker-model", "model.selected")[0]
        assert selected_event.data["reference"] == "openai/beta"
        assert runtime.session("picker-model").model_selection.reference == "openai/beta"

        # A fresh handle rehydrates from the log; the facade state/replay path
        # also includes the selection in its canonical reduced projection.
        await runtime.sessions.aclose_session("picker-model")
        reopened = runtime.session("picker-model", create=False)
        assert reopened.model_selection.reference == "openai/beta"
        replayed = [
            event
            async for event in facade.subscribe("picker-model", 0, follow=False)
        ]
        assert replayed.count(selected_event) == 1
        view, _ = facade.state("picker-model")
        assert view.model["reference"] == "openai/beta"

        # Selection state belongs to its session; an unselected sibling keeps
        # the configured route and has no durable model.selected record.
        current = await facade.handle(p.AgentCurrent(session="other-model"))
        assert isinstance(current, p.AgentCurrentResult)
        assert (current.provider, current.model) == ("openai", "alpha")
        assert _events(runtime, "other-model", "model.selected") == []
        assert runtime.session("other-model").model_selection is None
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_direct_selection_rejects_invalid_and_freezes_active_turn_route(tmp_path):
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [*text_response("first answer")[:-1], Wait(gate), text_response("first answer")[-1]],
        text_response("next answer"),
        name="openai",
    )
    runtime = _make_runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    transport = _FacadeTransport(facade)
    facade.open_session("direct-model")
    app = NexusTextualApp(Client(transport), session="direct-model")

    try:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await _type_command(pilot, app.query_one(ChatEditor), "first turn")
            await _wait_for(lambda: len(provider.requests) == 1)
            assert app.controller.running
            assert (provider.requests[0].provider, provider.requests[0].model) == (
                "openai",
                "alpha",
            )
            first_started = _events(runtime, "direct-model", "model.started")
            assert [(event.data["provider"], event.data["model"]) for event in first_started] == [
                ("openai", "alpha")
            ]

            # The direct /model command persists a next-turn choice while the
            # provider is deliberately parked inside the current request.
            await _type_command(pilot, app.query_one(ChatEditor), "/model openai/beta")
            await _wait_for(lambda: len(_events(runtime, "direct-model", "model.selected")) == 1)
            assert runtime.session("direct-model").model_selection.reference == "openai/beta"
            assert (provider.requests[0].provider, provider.requests[0].model) == (
                "openai",
                "alpha",
            )

            # A failed replacement must not erase/pretend to replace the last
            # accepted choice or emit another durable success event.
            await _type_command(pilot, app.query_one(ChatEditor), "/model missing/nope")
            status = app.query_one("#connection-status", Static).render().plain
            assert "Command failed" in status and "missing/nope" in status
            assert len(_events(runtime, "direct-model", "model.selected")) == 1
            assert runtime.session("direct-model").model_selection.reference == "openai/beta"
            assert "beta" in app.query_one(RootAgentBar).summary().plain

            gate.set()
            await facade.wait_idle(timeout=5.0)
            await _wait_for(lambda: not app.controller.running)
            assert provider.requests[0].model == "alpha"

            await _type_command(pilot, app.query_one(ChatEditor), "second turn")
            await facade.wait_idle(timeout=5.0)
            await _wait_for(lambda: len(provider.requests) == 2 and not app.controller.running)

        assert (provider.requests[1].provider, provider.requests[1].model) == (
            "openai",
            "beta",
        )
        started = _events(runtime, "direct-model", "model.started")
        assert [(event.data["provider"], event.data["model"]) for event in started] == [
            ("openai", "alpha"),
            ("openai", "beta"),
        ]
    finally:
        gate.set()
        await runtime.aclose()
