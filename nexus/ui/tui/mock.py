"""``/mock`` in the Textual shell (dev mode only; MOCK_PLAN §3.2)."""
from __future__ import annotations

import asyncio

from ...ui_support.mock_args import format_scenarios, parse_mock_args
from ...ui_support.tui_context_header import ContextModal

_FOLLOW_ATTEMPTS = 60
_FOLLOW_DELAY = 0.05


class MockCommandsMixin:
    """List, run, or clean mock scenarios through the host client."""

    async def _mock_command(self, args: tuple[str, ...]) -> None:
        parsed = parse_mock_args(args)
        client = self.controller.client
        if parsed.error:
            await self._show_notice(parsed.error)
        elif parsed.action == "list":
            # Multi-line output needs a modal: notices are a single status row.
            self.push_screen(ContextModal("Mock scenarios", format_scenarios((await client.mock_list()).scenarios)))
        elif parsed.action == "clean":
            await client.mock_clean()
            await self._show_notice("Sandbox restored to its seeded state")
        else:
            started = await client.mock_start(parsed.scenario, speed=parsed.speed, seed=parsed.seed)
            await self._switch_session(started.session)
            await self._follow_mock_run(started.session)

    async def _follow_mock_run(self, session: str) -> None:
        """Tail the run the way ``/reconnect`` does; the turn may not have started yet."""
        for _ in range(_FOLLOW_ATTEMPTS):
            if session != self.controller.session:
                return
            view = self.controller.view
            if view.active_turn is not None:
                self.controller.resume(self._post_event)
                self._sync_status("Mock run · Ctrl+C cancels")
                return
            if view.turns:  # already finished
                return
            await asyncio.sleep(_FOLLOW_DELAY)
            await self.controller.bootstrap()
            await self._sync_timeline()
