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
    if state == "permission":
        snapshot["prompt"] = {"kind": "permission", "id": "p1",
            "lines": ["Tool: Shell", "Command: rm -rf build", "Path: /private/tmp/nexus-ratatui-prototype"],
            "choices": [{"label": "Allow once", "value": "allow_once", "key": "y", "disabled": False},
                        {"label": "Always allow", "value": "allow_always", "key": "a", "disabled": False},
                        {"label": "Deny", "value": "deny_once", "key": "n", "disabled": False}]}
    elif state == "picker":
        snapshot["panel_title"] = "Models · Ctrl+F favorite · Ctrl+R refresh"
        snapshot["items"] = [{"label": label, "command": "", "operation": {"kind": "noop"}} for label in (
            "★ GPT-6 Luna · openai/gpt-6-luna", "Claude Opus 5.5 · anthropic/claude-opus-5-5",
            "Claude Sonnet 5.5 · anthropic/claude-sonnet-5-5", "Haiku 4.5 · anthropic/claude-haiku-4-5")]
    elif state == "diff":
        from nexus.ui_support.timeline import diff_split_rows
        hunk = ("@@ -8,7 +8,8 @@\n def total(values):\n-    return sum(values)\n+    result = sum(values)\n+    return int(result)\n \n \n"
                " def clamp(value, low, high):\n-    return max(low, min(high, value))\n+    return min(high, max(low, value))\n"
                "@@ -40,3 +41,4 @@\n def slug(text):\n     text = text.strip()\n+    text = text.lower()\n     return text")
        snapshot["blocks"].append({"id": "d", "kind": "diff", "title": "src/util.py", "added": 5, "removed": 2,
            "diff_rows": [list(row) for row in diff_split_rows(hunk)], "operation": {"kind": "noop"}})
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
