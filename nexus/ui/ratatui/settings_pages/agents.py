"""Settings → Agents: the default root agent and the agent files (always global).

Editing an agent opens the existing agent page (model or tier, fallbacks, tiers, prompt file): the one
allowed drill-in. Built-in agents show an override when edited.
"""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support.completion import root_agents
from ....ui_support.settings_help import SETTINGS_HELP
from . import files

AREA = "agents"
#: The same three project worktree modes as the session modal (`worktree_pages.WORKTREE_MODES`).
WORKTREE_MODES = (("none", "No worktree"), ("ask", "Ask every time"), ("always", "Always worktree"))


async def build(workflows) -> dict:
    client = workflows.client
    top = [sp.heading("Default")]
    try:
        current = await client.default_agent()
        names = [row["name"] for row in root_agents(await client.list_agents())]
        top.append(sp.row("default-agent", "New sessions start with", sp.select(current, [(n, n) for n in names], sp.op(AREA, "default_agent")),
                          description="The root agent a new session begins with."))
    except Exception as exc:  # noqa: BLE001 - the agent files below are still editable
        top.append(sp.note(f"The default agent is unavailable right now: {exc}", "warning"))
    top.append(sp.gap())
    top.append(sp.heading("Session worktrees"))
    try:
        state = (await client.session_worktree_choice(workflows.shell.controller.session)).state
        mode = state.get("mode", "ask")
        top.append(sp.row("worktree-mode", "Worktree mode", sp.select(dict(WORKTREE_MODES).get(mode, mode), [(label, key) for key, label in WORKTREE_MODES], sp.op(AREA, "worktree_mode")),
                          description="Git projects: work in the main checkout, ask before a session's first message, or always give each session its own named worktree."))
    except Exception as exc:  # noqa: BLE001 - the agent files below are still editable
        top.append(sp.note(f"The worktree mode is unavailable right now: {exc}", "warning"))
    top.append(sp.gap())
    blocks, root = await files.file_blocks(workflows, AREA)
    return sp.page(AREA, "Agents", top + blocks, intro=SETTINGS_HELP[AREA], footer=files.footer(workflows, AREA, root))


async def handle(workflows, operation) -> None:
    if operation["key"] == "default_agent":
        await workflows.client.set_default_agent(operation["value"], "global")
        workflows.shell.flash(f"New sessions start with {operation['value']}", "success")
    elif operation["key"] == "worktree_mode":
        if operation["value"] not in {mode for mode, _ in WORKTREE_MODES}:
            raise ValueError("Unknown worktree mode")
        result = await workflows.client.session_worktree_choice(workflows.shell.controller.session, operation["value"])
        workflows.shell.flash(f"Worktree mode · {result.state.get('mode', operation['value'])}", "success")
    else:
        raise ValueError(f"Unknown Agents operation {operation['key']!r}")
