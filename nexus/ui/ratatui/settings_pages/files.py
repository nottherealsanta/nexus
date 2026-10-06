"""Shared builder for file-backed Settings areas (tools, skills, MCP config, agents).

Each area lists the files the host reports for the current scope, with Edit, New file and Reset
actions that reuse the host's editor pages (the one allowed drill-in). Areas that can differ per
project (tools, skills, MCP) get the Global/Project scope control; agents are always global.
"""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support.text import escape_controls

SCOPES = ("global", "project")
#: Areas that can differ per project (the page shows the scope control).
SCOPED = {"tools", "skills", "mcp"}
AGENT_ORDER = {"build": 0, "orchestrator": 1, "advisor": 2, "task": 3, "quick": 4}


def scope_of(workflows, area: str) -> str:
    return "global" if area == "agents" else workflows.settings_scope


def scope_control(workflows, area: str):
    if area not in SCOPED:
        return None
    return {"options": ["Global", "Project"], "value": SCOPES.index(scope_of(workflows, area)), "operation": sp.op(area, "scope")}


async def file_blocks(workflows, area: str, *, empty: str = "No files yet. New file creates one.") -> tuple[list[dict], str]:
    """Blocks listing this area's files, and the root the host reports (for the footer)."""
    scope = scope_of(workflows, area)
    inventory = await workflows.client.settings_inventory(scope)
    items = [item for item in inventory.items if item.category == area]
    if area == "agents":
        items.sort(key=lambda item: (AGENT_ORDER.get(item.id, 9), item.id.casefold()))
    blocks: list[dict] = [sp.heading(f"FILES · {scope}")]
    if not items:
        blocks.append(sp.note(empty))
    for item in items:
        tag = " · built-in" if item.builtin else " · edited" if getattr(item, "overrides_builtin", False) else ""
        read = {"kind": "settings_read", "scope": scope, "category": area, "id": item.id}
        blocks.append(sp.row(f"file:{item.id}", escape_controls(item.label) + tag, sp.button("Edit…", read),
                             description=escape_controls(str(getattr(item, "summary", "") or ""))[:160]))
    names = [item.id for item in items if not item.builtin and (area != "agents" or getattr(item, "overrides_builtin", False))]
    reset = {"kind": "confirm", "label": "Reset this category to default? Removed files move to trash.", "lines": names,
             "next": {"kind": "settings_reset", "scope": scope, "category": area}}
    blocks.append(sp.buttons("file-actions", [("New file", {"kind": "settings_new", "scope": scope, "category": area}, "primary"),
                                              ("Reset category…", reset, "ghost")]))
    return blocks, inventory.root_display


def footer(workflows, area: str, root: str) -> str:
    return f"Saved in {root}" + (" · agents are always global" if area == "agents" else "")


async def handle_scope(workflows, operation) -> bool:
    if operation["key"] == "scope":
        value = operation.get("value", 0)
        workflows.settings_scope = SCOPES[int(value)] if int(value) in (0, 1) else "global"
        return True
    return False
