"""Offline native review fixture using the real shared MCP workflows.

Run under ratatui_browser_demo --bridge for a terminal, or with --desktop after
building the desktop binary. No daemon, credentials or private sessions involved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

from nexus.host import protocol as p
from nexus.ui.cli.client import Client
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.controller import NativeController
from nexus.ui.ratatui.prototype import project
from visual_tui_demo import DemoTransport

ROOT = Path(__file__).resolve().parents[1]


async def main(desktop=False, state="context"):
    controller = NativeController(Client(DemoTransport("reference")), "visual")
    await controller.bootstrap()
    shell = ShellActions(controller)
    shell.preferences.values.update(context_preview=True, sessions_sidebar=False, details_sidebar=True, details_tab="MCP")
    shell.workspace = str(ROOT)
    preview = p.ContextInspectResult(session="visual", mcp_servers=[
        {"name": "github", "scope": "project", "status": "connected", "enabled": True,
         "tool_loading": "search", "config_tool_loading": "search", "tool_count": 41,
         "schema_tokens": 8200, "tools": ["mcp__github__create_issue", "mcp__github__get_issue"]},
        {"name": "filesystem", "scope": "project", "status": "connected", "enabled": True,
         "tool_loading": "all", "config_tool_loading": "all", "tool_count": 6,
         "schema_tokens": 1200, "tools": ["mcp__filesystem__read_file"]}])
    controller.client.inspect_context = AsyncMock(side_effect=lambda session: preview)
    controller.client.settings_inventory = AsyncMock(return_value=p.SettingsInventoryResult(
        scope="project", root_display="<project>/.agents", items=[p.SettingsItem("mcp", "mcp.json", "mcp.json", "MCP configuration")]))
    controller.client.settings_read = AsyncMock(return_value=p.SettingsReadResult(body="{}", rel_path="mcp.json", builtin=False, sha256="fixture"))

    async def select(session, name, mode):
        nonlocal preview
        preview = replace_preview(preview, name, mode)
        return preview

    controller.client.select_context_mcp_loading = select
    shell.preview = preview
    shell.mcp_report = {"mcp": {"servers": [dict(row, health="ready") for row in preview.mcp_servers]}}
    shell.notice = "MCP search · offline review fixture"
    if state == "context":
        await shell.workflows.context_extensions("mcp")
    elif state == "details":
        await shell.workflows.operate({"kind": "context_extension_details", "category": "mcp", "name": "github"})
    elif state == "settings":
        await shell.workflows.settings("project", "mcp")
    elif state == "loading":
        await shell.workflows.operate({"kind": "settings_mcp_loading", "scope": "project", "name": "github", "tokens": 8200})
    binary = os.environ.get("NEXUS_DESKTOP_BINARY") if desktop else os.environ.get("NEXUS_TUI_BINARY")
    binary = binary or str(ROOT / ("rust/desktop/target/debug/nexus-desktop" if desktop else "rust/tui/target/debug/nexus-ratatui"))
    process = await asyncio.create_subprocess_exec(binary, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    revision = 0
    try:
        while True:
            revision += 1
            process.stdin.write((json.dumps(project(controller, revision, shell=shell)) + "\n").encode())
            await process.stdin.drain()
            line = await process.stdout.readline()
            if not line:
                break
            action = json.loads(line)
            kind = action.get("type")
            if kind == "quit":
                break
            if kind == "operation":
                await shell.workflows.operate(action["operation"])
            elif kind == "dismiss":
                shell.dismiss()
    finally:
        process.stdin.close()
        await process.wait()
        await controller.close()


def replace_preview(preview, name, mode):
    import msgspec

    return msgspec.structs.replace(preview, mcp_servers=[
        dict(row, tool_loading=mode or row["config_tool_loading"]) if row["name"] == name else row
        for row in preview.mcp_servers])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--state", choices=("context", "details", "settings", "loading"), default="context")
    args = parser.parse_args()
    asyncio.run(main(args.desktop, args.state))
