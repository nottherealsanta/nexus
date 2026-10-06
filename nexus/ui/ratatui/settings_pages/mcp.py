"""Settings → MCP servers: each server's switch and tool loading, then the mcp.json files.

On/Off is persistent; the tool loading mode applies to new sessions. Both go through the same host
commands as before and carry the file hash, so a concurrent edit is reported, never overwritten.
"""
from __future__ import annotations

from ....ui_support import settings_page as sp
from ....ui_support.settings_help import SETTINGS_HELP
from . import files

AREA = "mcp"
LOADING = ("search", "all")


def _tone(status: str) -> str:
    s = str(status).lower()
    return "error" if any(w in s for w in ("fail", "error")) else "success" if s in {"ready", "running", "connected", "ok"} else ""


async def _servers(workflows, scope: str) -> list[dict]:
    try:
        preview = await workflows.client.inspect_context(workflows.shell.controller.session)
    except Exception as exc:  # noqa: BLE001 - the file list below still works without live server state
        workflows.shell.flash(f"MCP server state is unavailable: {exc}", "warning")
        return []
    return [row for row in getattr(preview, "mcp_servers", []) if row.get("scope") == scope]


async def build(workflows) -> dict:
    scope = files.scope_of(workflows, AREA)
    blocks: list[dict] = []
    servers = await _servers(workflows, scope)
    if servers:
        blocks.append(sp.heading(f"SERVERS · {scope}"))
    for row in servers:
        name = row["name"]
        enabled = row.get("config_enabled", True)
        loading = row.get("config_tool_loading", "search")
        status = str(row.get("status") or "unknown")
        tokens = row.get("schema_tokens", 0)
        inner = [
            sp.row(f"{name}:on", "Enabled", sp.toggle(enabled, sp.op(AREA, "enabled", scope=scope, name=name)),
                   description="Saved in mcp.json; applies to new sessions."),
            sp.row(f"{name}:load", "Tool loading",
                   sp.segmented(["Search", "All"], LOADING.index(loading) if loading in LOADING else 0, sp.op(AREA, "loading", scope=scope, name=name), values=list(LOADING)),
                   description=f"Search finds tools on demand; All puts every schema in the prompt (~{tokens} tokens)."),
        ]
        blocks.append(sp.section(f"srv-{name}", name, inner, summary=f"{status} · {row.get('tool_count', 0)} tools" + ("" if enabled else " · off"),
                                 tone=_tone(status), open_=True))
    if servers:
        blocks.append(sp.gap())
    file_blocks, root = await files.file_blocks(workflows, AREA, empty="No mcp.json yet. New file creates one.")
    blocks += file_blocks
    return sp.page(AREA, "MCP servers", blocks, intro=SETTINGS_HELP[AREA], footer=files.footer(workflows, AREA, root),
                   scope=files.scope_control(workflows, AREA))


async def handle(workflows, operation) -> None:
    if await files.handle_scope(workflows, operation):
        return
    key = operation["key"]
    if key not in {"enabled", "loading"}:
        raise ValueError(f"Unknown MCP operation {key!r}")
    client = workflows.client
    scope, name = operation["scope"], operation["name"]
    file = await client.settings_read(scope, "mcp", "mcp.json")
    if key == "enabled":
        result = await client.settings_mcp_enabled_set(scope, name, bool(operation["value"]), file.sha256)
    else:
        if operation["value"] not in LOADING:
            raise ValueError("Unknown tool loading mode")
        result = await client.settings_mcp_loading_set(scope, name, operation["value"], file.sha256)
    if result.status == "conflict":
        workflows.shell.flash("mcp.json changed on disk; reload this page and try again", "warning")
    else:
        workflows.shell.flash(f"{name} saved · applies to new sessions", "success")
