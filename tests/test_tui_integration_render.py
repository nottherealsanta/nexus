"""Real runtime tool events rendered by the Textual conversation timeline."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from textual.widgets import Static
from textual_diff_view import DiffView

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime
from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import ToolActivityWidget
from nexus.ui.tui.tool_details import ToolDetailsScreen


class _FacadeTransport:
    """Adapt the real facade to the same client transport used by the TUI."""

    def __init__(self, facade: HostFacade) -> None:
        self.facade = facade

    async def request(self, command: p.Command) -> p.Result:
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
        # The Runtime is owned by the test so the facade remains valid until the
        # app's run_test context has completed.
        return None


@pytest.mark.asyncio
async def test_real_read_edit_events_render_as_timeline_tool_cards(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("hello\nworld\n", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("read-render", "Read", {"path": "note.md"})),
        tool_response(
            (
                "edit-render",
                "Edit",
                {"path": "note.md", "old_string": "world", "new_string": "nexus"},
            )
        ),
        text_response("done"),
    )
    config = Config(
        model="scripted/render-test",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/render-test"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            tools=ToolsSection(),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("render")
    await facade.enqueue("render", "read and edit note.md")
    await facade.wait_idle(timeout=10.0)
    app = NexusTextualApp(Client(_FacadeTransport(facade)), session="render")

    try:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert not app.controller.running
            assert target.read_text(encoding="utf-8") == "hello\nnexus\n"
            assert app.controller.view.messages[0].text == "read and edit note.md"
            assert app.controller.view.messages[-1].text == "done"

            cards = {card.call_id: card for card in app.query(ToolActivityWidget)}
            assert set(cards) == {"read-render", "edit-render"}

            read_card = cards["read-render"]
            read_header = read_card.query_one("#tool-header", Static).render().plain
            # "→ Read path" headers; a finished call carries no spinner or failure mark.
            assert read_header == "→ Read note.md"
            details = read_card._details_text()
            assert "hello" in details and "world" in details
            assert len(read_card.children) == 1
            assert len(read_header.splitlines()) == 1

            edit_card = cards["edit-render"]
            edit_header = edit_card.query_one("#tool-header", Static).render().plain
            assert edit_header == "← Edit note.md · Edit note.md: 1 replacement(s)"
            assert edit_card.tool.diff["hunk"].endswith("-world\n+nexus")
            # The edit's diff renders inline under its row via textual-diff-view.
            (diff_view,) = edit_card.query(DiffView)
            assert (diff_view.code_original, diff_view.code_modified) == ("hello\nworld", "hello\nnexus")

            # Full input, result, and unified diff are also in the modal.
            edit_card.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ToolDetailsScreen)
            detail = app.screen.query_one("#tool-details-body", Static).render().plain
            assert "path: note.md" in detail
            assert "-world" in detail and "+nexus" in detail
    finally:
        await runtime.aclose()
