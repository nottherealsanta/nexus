"""Composer metadata layout and agent identity-color contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.events import Event
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.controller import TuiController
from nexus.ui.tui.widgets import ChatEditor, ChatInput, RootAgentBar
from nexus.view import apply


@pytest.mark.asyncio
async def test_host_agent_color_is_limited_to_identity_and_composer_has_no_edge():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(88, 22)) as pilot:
        await pilot.pause()
        app.controller.agent_name = "custom"
        app.controller.agent_color = "#22aacc"
        app.controller.provider = "vendor"
        app.controller.model = "model-x"
        app.controller.reasoning_effort = "high"
        app._sync_agent()
        await pilot.pause()

        composer = app.query_one(ChatInput)
        editor = app.query_one(ChatEditor)
        metadata = app.query_one(RootAgentBar)
        context = app.query_one("#context-usage")

        assert composer.styles.border_left[0] == ""
        assert editor.styles.border_left[0] == ""
        rendered = metadata.render()
        assert rendered.plain == "Custom  ·  model-x  ·  vendor  ·  high"
        assert "#22aacc" in str(rendered.spans[0].style)
        assert all("#22aacc" not in str(span.style) for span in rendered.spans[1:])
        assert context.display
        assert context.region.right <= app.query_one("#bottom-info").region.right
        assert context.region.width >= len(context.render().plain)
        assert app.query_one("#cwd-path").render().plain
        assert metadata.region.y < context.region.y
        assert str(app.query_one("#connection-status").render()).strip() == ""

        # Switching away from and back to a colored agent never paints a
        # composer stripe; focus transitions must not restore one either.
        app.controller.agent_name = "general"
        app.controller.agent_color = None
        app._sync_agent()
        await pilot.pause()
        assert composer.styles.border_left[0] == ""
        app.controller.agent_name = "custom"
        app.controller.agent_color = "#22aacc"
        app._sync_agent()
        metadata.focus()
        await pilot.pause()
        editor.focus()
        await pilot.pause()
        assert composer.styles.border_left[0] == ""


@pytest.mark.asyncio
async def test_narrow_composer_keeps_context_entry_visible_and_routine_status_hidden():
    app = NexusTextualApp(_client(FakeTransport()), session="s")
    async with app.run_test(size=(48, 20)) as pilot:
        await pilot.pause()
        app.controller.view = apply(
            app.controller.view,
            Event(type="turn.started", data={}, seq=411, session="s", turn="t"),
        )
        app.controller.cursor = 411
        app._sync_status()
        await pilot.pause()

        assert app.query_one("#context-usage").display
        assert "Unsupported" in app.query_one("#root-agent").render().plain
        assert str(app.query_one("#connection-status").render()).strip() == ""
        assert app.query_one(ChatInput).region.width <= 48
        assert app.query_one(ChatEditor).region.right <= 48


@pytest.mark.asyncio
async def test_agent_and_model_selections_refresh_authoritative_metadata():
    client = _client(FakeTransport())
    current = SimpleNamespace(
        name="general",
        source="default",
        color="#1188aa",
        provider="initial-provider",
        model="initial-model",
        reasoning_effort="high",
        thinking_budget=2048,
    )

    async def current_agent(session: str):
        return current

    async def select_agent(session: str, name: str):
        nonlocal current
        current = SimpleNamespace(
            name=name,
            source="session",
            color="#22bb66",
            provider="agent-provider",
            model="agent-model",
            reasoning_effort=None,
            thinking_budget=None,
        )
        # Selection response intentionally has no metadata beyond identity.
        return SimpleNamespace(name=name, source="session")

    async def select_model(session: str, ref: str):
        nonlocal current
        provider, model = ref.split("/", 1)
        current = SimpleNamespace(
            name=current.name,
            source=current.source,
            color=current.color,
            provider=provider,
            model=model,
            reasoning_effort="medium",
            thinking_budget=current.thinking_budget,
        )
        return SimpleNamespace(provider=provider, model=model)

    client.current_agent = current_agent
    client.select_agent = select_agent
    client.select_model = select_model
    app = NexusTextualApp(client, session="s")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._agent_command(("custom",))
        assert app.controller.agent_name == "custom"
        assert app.controller.agent_color == "#22bb66"
        assert app.controller.provider == "agent-provider"
        assert app.controller.model == "agent-model"
        assert app.controller.reasoning_effort is None
        assert app.controller.thinking_budget is None

        await app._model_command(("next-provider/next-model",))
        assert app.controller.agent_name == "custom"
        assert app.controller.provider == "next-provider"
        assert app.controller.model == "next-model"
        assert app.controller.reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_stale_metadata_refresh_cannot_overwrite_selection():
    client = _client(FakeTransport())
    stale_started = asyncio.Event()
    release_stale = asyncio.Event()
    current = SimpleNamespace(
        name="custom",
        source="session",
        color="#22bb66",
        provider="fresh-provider",
        model="fresh-model",
        reasoning_effort="high",
        thinking_budget=None,
    )
    refresh_count = 0

    async def current_agent(session: str):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 1:
            stale_started.set()
            await release_stale.wait()
            return SimpleNamespace(
                name="general",
                source="default",
                color=None,
                provider="stale-provider",
                model="stale-model",
                reasoning_effort="low",
                thinking_budget=None,
            )
        return current

    async def select_agent(session: str, name: str):
        return SimpleNamespace(name=name, source="session")

    client.current_agent = current_agent
    client.select_agent = select_agent
    controller = TuiController(client, "s")
    stale_refresh = asyncio.create_task(controller.refresh_agent_metadata())
    await asyncio.wait_for(stale_started.wait(), 1)

    await controller.select_agent("custom")
    release_stale.set()
    await stale_refresh

    assert controller.agent_name == "custom"
    assert controller.agent_color == "#22bb66"
    assert controller.provider == "fresh-provider"
    assert controller.model == "fresh-model"
    assert controller.reasoning_effort == "high"
