"""``/new``: pick the root agent a new session starts with (plan section 14.4).

The inline picker lists root-capable agents with the current one preselected.
The chosen agent is applied through the ordinary ``AgentSelect`` host command
once the new session is open; cancelling opens nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...client import ClientError
from .agent_picker import _display_name

if TYPE_CHECKING:
    from .app import NexusTextualApp


async def open_new_session_picker(app: NexusTextualApp, session: str) -> None:
    """Show the root-agent picker for ``session``; fall back to a plain switch."""
    try:
        app._agents = await app.controller.client.list_agents()
    except ClientError:
        await app._switch_session(session)
        return
    rows = [
        {**row, "_label": _display_name(row.get("name"))}
        for row in app._agents
        if row.get("name") and ("contexts" not in row or "root" in row.get("contexts", ()))
    ]
    if not rows:
        await app._switch_session(session)
        return
    app._new_session_id = session
    app._show_inline_picker("agent", rows)


async def apply_agent_choice(app: NexusTextualApp, agent: str) -> None:
    """Handle an agent-picker choice: start the pending session, or switch agent."""
    session, app._new_session_id = getattr(app, "_new_session_id", None), None
    if session is None:
        await app._apply_agent_selection(agent)
        return
    await app._switch_session(session)
    if session == app.controller.session and agent != app.controller.agent_name:
        await app._apply_agent_selection(agent)
