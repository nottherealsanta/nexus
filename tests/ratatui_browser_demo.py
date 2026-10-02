"""Native reference fixture and PTY bridge for the existing browser test server.

The browser server speaks Textual's framed development protocol. This adapter
owns a real controlling PTY and forwards its bytes; no production code imports
Textual. Only deterministic recorded events enter the native client.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios

ROOT = Path(__file__).resolve().parents[1]


async def fixture():
    from visual_tui_demo import DemoTransport
    from nexus.ui.cli.client import Client
    from nexus.ui.ratatui.controller import NativeController
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.prototype import project
    from nexus.ui.ratatui.run import binary_path

    controller = NativeController(Client(DemoTransport("reference")), "visual")
    await controller.bootstrap()
    shell = ShellActions(controller)
    shell.preferences.values.update(context_preview=True, sessions_sidebar=False, details_sidebar=True)
    shell.breadcrumb = str(ROOT)
    try:
        shell.preview = await controller.client.inspect_context("visual")
    except Exception:
        shell.preview = None
    process = await asyncio.create_subprocess_exec(str(binary_path()), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    snapshot = project(controller, 1, shell=shell)
    state = os.environ.get("NEXUS_RATATUI_STATE", "")
    if state.startswith("redesign-"):
        _, theme, sidebars, mode = state.split("-", 3)
        shell.preferences.values.update(theme=f"nexus-{theme}", sessions_sidebar=sidebars[0]=="1", details_sidebar=sidebars[1]=="1", details_tab=mode if mode in {"Session","Files","MCP","Logs"} else "Session")
        shell.workspace = str(ROOT)
        shell.sessions = [{"id":"visual","title":"Fix the parser","workspace":str(ROOT),"active":True,"status":"idle","sub":"12 · 5m","group":"NEXUS"},
                          {"id":"other","title":"Release notes","workspace":str(ROOT),"status":"idle","sub":"3 · 2d","group":"NEXUS"}]
        shell.tabs = shell.sessions[:]
        shell.mcp_report={"daemon":{"pid":1234,"socket":"/tmp/nexus/demo.sock"},"git":{"branch":"main"},"mcp":{"servers":[{"name":"docs","health":"ready","tool_count":6}]}}
        shell.logs.trace=["draw: p50 0.3 · p95 1.3 · max 1.6 ms"]
        if mode=="expanded":
            shell.verbose=True
        if mode=="popover":
            from datetime import datetime
            shell.preview_at=datetime.now().astimezone()
            shell.context_popover()
        snapshot=project(controller,1,shell=shell)
        snapshot.update(context_used=52140,context_window=1000000,context_tiers=[200000,1000000],context_label="52.1K · 26.1% · 5.2%")
        if mode=="running":
            snapshot["status"]="running"
        if mode=="Logs":
            snapshot["logs"]=["12:04:20 [ERROR] session · tool.failed · Parse failed", "4 routine entries folded · Ctrl+A toggle"]
    elif state == "subagent":
        from visual_tui_demo import subagent_fixture
        agent, context = subagent_fixture()
        controller.view.agents[agent.id] = agent
        shell.workflows.agent_page_id = agent.id
        shell.workflows.agent_context = context
        snapshot = project(controller, 1, shell=shell)
    elif state == "permission":
        snapshot["prompt"] = {"kind": "permission", "id": "p1",
            "lines": ["Tool: Shell", "Command: rm -rf build", "Path: /private/tmp/nexus-ratatui-prototype"],
            "choices": [{"label": "Allow once", "value": "allow_once", "key": "y", "disabled": False},
                        {"label": "Always allow", "value": "allow_always", "key": "a", "disabled": False},
                        {"label": "Deny", "value": "deny_once", "key": "n", "disabled": False}]}
    elif state == "picker":
        snapshot["panel_layout"] = "drawer"
        snapshot["panel_title"] = "Models · Ctrl+F favorite · Ctrl+R refresh"
        snapshot["items"] = [{"label": label, "command": "", "operation": {"kind": "noop"}} for label in (
            "★ GPT-6 Luna · openai/gpt-6-luna", "Claude Opus 5.5 · anthropic/claude-opus-5-5",
            "Claude Sonnet 5.5 · anthropic/claude-sonnet-5-5", "Haiku 4.5 · anthropic/claude-haiku-4-5")]
    elif state == "agents":
        snapshot["panel_title"] = "Agents"
        snapshot["panel_layout"] = "drawer"
        snapshot["items"] = [{"label": name, "command": "/agent " + name} for name in ("build", "advisor", "task")]
    elif state == "markdown":
        snapshot["panel_title"] = "AGENTS.md · /workspace/AGENTS.md"
        snapshot["panel_format"] = "markdown"
        snapshot["panel_lines"] = ["# Project rules", "", "Keep context visible.", "", "- Preserve newlines", "- Show every parameter", "", "```python", "value = 42", "```"]
    elif state == "usage":
        snapshot["panel_title"] = "Provider usage"
        snapshot["panel_loading"] = True
        snapshot["panel_lines"] = ["Codex · Plus", "  Session  ████░░░░░░░░  33% used", "Fetched 12:00:00", "r refresh · Esc close"]
        snapshot["panel_tones"] = ["title", "ok", "dim", "dim"]
    elif state == "completion":
        snapshot["restore"] = "/"
        snapshot["completion_query"] = "/"
        snapshot["completions"] = ["/agent", "/context", "/help", "/usage"]
    elif state == "diff":
        from nexus.ui_support.timeline import diff_split_rows
        hunk = ("@@ -8,7 +8,8 @@\n def total(values):\n-    return sum(values)\n+    result = sum(values)\n+    return int(result)\n \n \n"
                " def clamp(value, low, high):\n-    return max(low, min(high, value))\n+    return min(high, max(low, value))\n"
                "@@ -40,3 +41,4 @@\n def slug(text):\n     text = text.strip()\n+    text = text.lower()\n     return text")
        snapshot["blocks"].append({"id": "d", "kind": "diff", "title": "src/util.py", "added": 5, "removed": 2,
            "diff_rows": [list(row) for row in diff_split_rows(hunk)], "operation": {"kind": "noop"}})
    elif state == "tool":
        from nexus.ui_support.tool_details import styled_lines, tool_detail_sections
        from nexus.view.model import ToolCallView
        tool = ToolCallView(call_id="c", name="Edit", event_seq=2, status="completed", input={"path": "src/util.py", "old": "return sum(values)", "new": "return int(sum(values))"},
                            diff={"path": "src/util.py", "hunk": "--- a/src/util.py\n+++ b/src/util.py\n@@ -9,2 +9,2 @@\n def total(values):\n-    return sum(values)\n+    return int(sum(values))"})
        snapshot["panel_title"] = "Edit"
        snapshot["panel_lines"], snapshot["panel_tones"] = styled_lines(tool_detail_sections(tool))
    elif state == "settings":
        from nexus.ui_support.settings_help import SETTINGS_SECTIONS
        snapshot["panel_title"] = "Settings · global · agents"
        snapshot["panel_layout"] = "page"
        snapshot["panel_lines"] = ["~/.nexus", "Build is the default root agent; advisor, task and quick are subagents."]
        snapshot["items"] = [{"label": label, "command": "", "operation": {"kind": "noop"}} for label in (
            "New sessions start with…", "build · built-in", "advisor · built-in", "quick · built-in", "task · built-in", "New file", "Reset category…")]
        snapshot["nav"] = {"items": [[label, key or "", key is None] for key, label in SETTINGS_SECTIONS], "selected": 9}
    elif state == "light":
        snapshot["theme"] = "nexus-light"
    elif state == "panel":
        snapshot["panel_title"] = "Session details"
        snapshot["panel_lines"] = ["Status: idle", "Agent: build", "A long value " + "word " * 40]
    process.stdin.write((json.dumps(snapshot) + "\n").encode())
    await process.stdin.drain()
    while line := await process.stdout.readline():
        if json.loads(line).get("type") == "quit":
            break
    process.stdin.close()
    await process.wait()
    await controller.close()


def bridge(command=None):
    master, slave = pty.openpty()
    def resize(width, height):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    resize(int(os.getenv("COLUMNS", "122")), int(os.getenv("ROWS", "40")))
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
    env.pop("NO_COLOR", None)
    child = subprocess.Popen([sys.executable, __file__, *(["--exec", *command] if command else ["--child"])], stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    os.write(1, b"__GANGLION__\n")
    buffer = b""
    try:
        while child.poll() is None:
            ready, _, _ = select.select([master, 0], [], [], .1)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                os.write(1, b"D" + len(data).to_bytes(4, "big") + data)
            if 0 in ready:
                data = os.read(0, 65536)
                if not data:
                    break
                buffer += data
                while len(buffer) >= 5:
                    length = int.from_bytes(buffer[1:5], "big")
                    if length > 16 * 1024 * 1024:
                        raise ValueError("Browser input packet exceeds limit")
                    if len(buffer) < 5 + length:
                        break
                    kind, payload = buffer[:1], buffer[5:5 + length]
                    buffer = buffer[5 + length:]
                    if kind == b"D":
                        os.write(master, payload)
                    elif kind == b"M":
                        meta = json.loads(payload)
                        if meta.get("type") == "resize":
                            resize(int(meta["width"]), int(meta["height"]))
    finally:
        child.terminate()
        child.wait(timeout=5)
        os.close(master)


if __name__ == "__main__":
    if "--exec" in sys.argv:
        # Run any command (e.g. the real `nexus chat`) on a controlling PTY, for live browser checks.
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execvp(sys.argv[sys.argv.index("--exec") + 1], sys.argv[sys.argv.index("--exec") + 1:])
    elif "--child" in sys.argv:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        asyncio.run(fixture())
    else:
        bridge(sys.argv[sys.argv.index("--bridge") + 1:] if "--bridge" in sys.argv else None)
