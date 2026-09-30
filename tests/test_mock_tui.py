"""``/mock`` in the real Textual shell over a real runtime (dev mode; MOCK_PLAN §8)."""
from __future__ import annotations

import asyncio
import importlib

import pytest
from textual.widgets import Static, TextArea

from nexus.devtools.mock.sandbox import ensure_sandbox
from nexus.host import HostFacade
from nexus.runtime import Runtime
from nexus.ui.cli import commands
from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import ToolActivityWidget
from nexus.ui_support.tui_context_header import ContextModal
from tests.test_tui_integration_render import _FacadeTransport


async def _submit(app, pilot, text: str) -> None:
    editor = app.query_one("#chat-editor", TextArea)
    editor.focus()
    editor.text = text
    await pilot.press("enter")
    await pilot.pause()


async def _until(pilot, predicate, timeout=30.0) -> None:
    async def wait():
        while not predicate():
            await pilot.pause(0.05)

    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
def dev_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DEV", "1")
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home"))
    importlib.reload(commands)  # /mock is gated on NEXUS_DEV at import
    yield tmp_path / "home"
    monkeypatch.delenv("NEXUS_DEV", raising=False)
    importlib.reload(commands)


@pytest.mark.asyncio
async def test_mock_command_lists_runs_and_switches_session(dev_env):
    sandbox = ensure_sandbox(dev_env)
    runtime = Runtime(sandbox, home=dev_env.parent, environ={"NEXUS_DEV": "1"})
    facade = HostFacade(runtime)
    facade.open_session("main")
    app = NexusTextualApp(Client(_FacadeTransport(facade)), session="main")
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for text in ("/mock", "/mock list"):
                await _submit(app, pilot, text)
                assert isinstance(app.screen, ContextModal)  # the scenario list is readable
                body = " ".join(str(w.render()) for w in app.screen.query(Static))
                assert "parallel-subagents" in body and "tool-marathon" in body
                await pilot.press("escape")
                await pilot.pause(0.2)
                assert not isinstance(app.screen, ContextModal)
            assert app.controller.session == "main" and not app.controller.running
            await pilot.pause(0.5)  # let the notice settle, as a user would before typing again

            await _submit(app, pilot, "/mock parallel-subagents --speed 0")
            await _until(pilot, lambda: app.controller.session.startswith("mock-parallel-subagents"))
            await _until(
                pilot,
                lambda: any("mock verdict" in (m.text or "") for m in app.controller.view.messages),
            )
            verdict = next(m.text for m in app.controller.view.messages if "mock verdict" in (m.text or ""))
            assert verdict.startswith(("✓", "All workers")) and "✗" not in verdict
            assert len(app.controller.view.agents) >= 5  # workers (+ grandchild) reached the UI
    finally:
        await facade.wait_idle(timeout=10.0)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_mock_parallel_tools_render_as_grouped_batches(dev_env):
    sandbox = ensure_sandbox(dev_env)
    runtime = Runtime(sandbox, home=dev_env.parent, environ={"NEXUS_DEV": "1"})
    facade = HostFacade(runtime)
    facade.open_session("main")
    app = NexusTextualApp(Client(_FacadeTransport(facade)), session="main")
    try:
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await _submit(app, pilot, "/mock parallel-tools --speed 0")
            await _until(
                pilot,
                lambda: any("mock verdict" in (m.text or "") for m in app.controller.view.messages),
            )
            await pilot.pause(0.5)
            cards = [c for c in app.query(ToolActivityWidget) if c.tool.name]
            by_position = [c.batch for c in sorted(cards, key=lambda c: c.tool.event_seq)]
            assert by_position[0] is None  # the lone glob has no gutter
            assert by_position[1] == "first" and "last" in by_position
            first = next(c for c in cards if c.batch == "first")
            assert str(first.query_one("#tool-header", Static).render()).startswith("┌ ")
            last = next(c for c in cards if c.batch == "last")
            assert str(last.query_one("#tool-header", Static).render()).startswith("└ ")
    finally:
        await facade.wait_idle(timeout=10.0)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_mock_is_an_unknown_command_outside_dev_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_DEV", raising=False)
    importlib.reload(commands)
    assert "/mock" not in commands.BY_NAME
