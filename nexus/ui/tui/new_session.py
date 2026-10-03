"""Open a new session and persist the current root agent selection there."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import NexusTextualApp


async def open_new_session(app: NexusTextualApp, session: str) -> None:
    """Open ``session`` and durably reuse the current root agent."""
    agent_name = app.controller.agent_name
    await app._switch_session(session)
    await app._apply_agent_selection(agent_name)
