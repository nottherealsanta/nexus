"""Settings → MCP servers: Global then Project, each with its servers and its mcp.json files.

There is no scope tab: both scopes are shown one after another, so every server and the file that
defines it is visible at once. Each server is a section of labelled read-only rows (status, transport,
command or URL, defined in, tools, filters, ignored keys) above its Enabled switch and tool loading.

On/Off is persistent; the tool loading mode applies to new sessions. Both go through the same host
commands as before and carry the file hash, so a concurrent edit is reported, never overwritten.
"""
from __future__ import annotations

import json
import re

from ....ui_support import settings_page as sp
from ....ui_support.settings_help import SETTINGS_HELP
from . import files

AREA = "mcp"
LOADING = ("search", "all")
#: One-server starter shown in the Add server editor.
ADD_TEMPLATE = """{
  "my-server": {
    "command": "npx",
    "args": ["-y", "@scope/server"]
  }
}
"""
_WRAPPERS = ("servers", "mcpServers", "mcp", "context_servers")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")  # keep in step with host_support.settings_inventory.MCP_ADD_NAME


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("Duplicate JSON key; each server and field may appear once")
        seen[key] = value
    return seen


def parse_add_draft(body: str) -> tuple[str, dict]:
    """``(name, entry)`` from the editor text: ``{name: entry}`` or exactly one server in a wrapper map.

    The wrapper may be any map the loader reads (``mcpServers``, ``servers``, ``mcp``,
    ``context_servers``). Raises ``ValueError`` with a message that never repeats the draft.
    """
    def bad_constant(_name):
        raise ValueError("NaN and Infinity are not JSON")

    try:
        document = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=bad_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    except RecursionError:
        raise ValueError("JSON is nested too deeply") from None
    if not isinstance(document, dict):
        raise ValueError("Enter one JSON object: {\"name\": {…server…}}")
    if len(document) == 1:
        (key, inner), = document.items()
        if key in _WRAPPERS and isinstance(inner, dict) and inner and all(isinstance(item, dict) for item in inner.values()):
            document = inner
    if len(document) != 1:
        raise ValueError(f"Add exactly one server per time (found {len(document)})")
    (name, entry), = document.items()
    if not _NAME.fullmatch(name):
        raise ValueError("Server name must be 1-32 characters: letters, digits, '_', '.', '-' (starting with a letter or digit)")
    if not isinstance(entry, dict) or not entry:
        raise ValueError("The server must be a non-empty JSON object")
    return name, entry


def _tone(status: str) -> str:
    s = str(status).lower()
    return "error" if any(w in s for w in ("fail", "error")) else "success" if s in {"ready", "running", "connected", "ok"} else ""


async def _servers(workflows) -> list[dict]:
    try:
        preview = await workflows.client.inspect_context(workflows.shell.controller.session)
    except Exception as exc:  # noqa: BLE001 - the file list below still works without live server state
        workflows.shell.flash(f"MCP server state is unavailable: {exc}", "warning")
        return []
    return [row for row in getattr(preview, "mcp_servers", []) if isinstance(row, dict)]


def _status_tone(row: dict) -> str:
    if row.get("invalid") or row.get("file_error"):
        return "error"
    return _tone(str(row.get("status") or ""))


def _readout(id_: str, label: str, value: str, description: str = "") -> dict:
    return sp.row(id_, label, sp.readout(value), description=description)


def _server_section(scope: str, row: dict) -> dict:
    name = str(row.get("name") or "?")
    status = str(row.get("status") or "unknown")
    error = str(row.get("error") or "")
    rid = f"{scope}:{name}"
    defined = str(row.get("source_path") or "")
    status_value = f"{status}: {error}" if error else status
    if row.get("invalid"):
        inner = [_readout(f"{rid}:status", "Status", status_value or "invalid"), _readout(f"{rid}:error", "Error", error or "invalid entry")]
        if defined:
            inner.append(_readout(f"{rid}:src", "Defined in", defined))
        return sp.section(f"srv-{rid}", name, inner, summary=f"{status or 'invalid'} · not loaded", tone="error", open_=True)
    enabled = row.get("config_enabled", True)
    loading = row.get("config_tool_loading", "search")
    tools = row.get("tool_count", 0)
    transport = str(row.get("transport") or "")
    summary = " · ".join(b for b in (status, transport, f"{tools} tools") if b) + ("" if enabled else " · off")
    inner = [_readout(f"{rid}:status", "Status", status_value)]
    if transport:
        inner.append(_readout(f"{rid}:transport", "Transport", transport))
    target = str(row.get("url") or row.get("command_label") or "")
    if target:
        inner.append(_readout(f"{rid}:target", "URL" if row.get("url") else "Command", target))
    if defined:
        inner.append(_readout(f"{rid}:src", "Defined in", defined))
    counts = f"{tools} tools" + "".join(f" · {row[k]} {label}" for k, label in (("resource_count", "resources"), ("prompt_count", "prompts")) if row.get(k))
    inner.append(_readout(f"{rid}:tools", "Tools", counts))
    filters = [f"{label} {', '.join(map(str, row[key]))}" for key, label in (("include_tools", "include"), ("exclude_tools", "exclude")) if row.get(key)]
    if filters:
        inner.append(_readout(f"{rid}:filters", "Tool filters", " · ".join(filters)))
    ignored = [str(k) for k in row.get("ignored_keys") or ()]
    if ignored:
        inner.append(_readout(f"{rid}:ignored", "Ignored keys", ", ".join(ignored),
                              "Accepted from another client's format; Nexus does not act on these."))
    tokens = row.get("schema_tokens", 0)
    inner += [
        sp.row(f"{rid}:on", "Enabled", sp.toggle(enabled, sp.op(AREA, "enabled", scope=scope, name=name)),
               description="Saved in mcp.json; applies to new sessions."),
        sp.row(f"{rid}:load", "Tool loading",
               sp.segmented(["Search", "All"], LOADING.index(loading) if loading in LOADING else 0, sp.op(AREA, "loading", scope=scope, name=name), values=list(LOADING)),
               description=f"Search finds tools on demand; All puts every schema in the prompt (~{tokens} tokens)."),
    ]
    return sp.section(f"srv-{rid}", name, inner, summary=summary, tone=_status_tone(row), open_=True)


async def build(workflows) -> dict:
    rows = await _servers(workflows)
    blocks: list[dict] = []
    roots: dict[str, str] = {}
    for scope in ("global", "project"):
        mine = [r for r in rows if r.get("scope") == scope]
        problems = [r for r in mine if r.get("file_error")]
        servers = [r for r in mine if not r.get("file_error")]
        inventory = await workflows.client.settings_inventory(scope)
        root = roots[scope] = inventory.root_display
        path = f"{root}/mcp.json"
        blocks.append(sp.heading(f"{scope.capitalize()} · {path}"))
        if scope == "project" and servers and all(str(r.get("source_path") or "").startswith(".nexus") for r in servers):
            blocks.append(sp.note("Read from the legacy .nexus/mcp.json (read-only fallback). Saving writes .agents/mcp.json."))
        for problem in problems:
            level = "error" if problem.get("status") == "invalid file" else "warning"
            blocks.append(sp.callout(level, f"{problem.get('name') or path}: {problem.get('error') or problem.get('status')}"))
        blocks += [_server_section(scope, r) for r in servers]
        if not mine:
            blocks.append(sp.note(f"No servers in {path}. New file creates one."))
        blocks.append(sp.buttons(f"{scope}:mcp-add", [("Add server…", {"kind": "mcp_add_open", "scope": scope}, "primary")]))
        file_blocks, _ = await files.file_blocks(workflows, AREA, scope=scope, heading=False,
                                                 empty="No mcp.json yet. New file creates one.")
        blocks += file_blocks
        blocks.append(sp.gap())
    footer = f"Saved in {roots['global']} (global) · {roots['project']} (project)"
    return sp.page(AREA, "MCP servers", blocks, intro=SETTINGS_HELP[AREA], footer=footer)


async def handle(workflows, operation) -> None:
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
