"""Capture the real native terminal client with identical deterministic events.

Run after cargo build: PYTHONPATH=. python tests/playwright_ratatui_check.py.
Screenshots prove rendered/interactive terminals, not complete feature parity.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/ratatui-parity"


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    capture_names = set(filter(None, os.environ.get("NEXUS_RATATUI_CAPTURE", "").split(",")))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            captures = (("ratatui", "tests/ratatui_browser_demo.py", ""),
                                         ("ratatui-permission", "tests/ratatui_browser_demo.py", "permission"),
                                         ("ratatui-picker", "tests/ratatui_browser_demo.py", "picker"),
                                         ("ratatui-panel", "tests/ratatui_browser_demo.py", "panel"), ("ratatui-light", "tests/ratatui_browser_demo.py", "light"), ("ratatui-diff", "tests/ratatui_browser_demo.py", "diff"), ("ratatui-tool", "tests/ratatui_browser_demo.py", "tool"), ("ratatui-settings", "tests/ratatui_browser_demo.py", "settings"),
                                         ("ratatui-agents", "tests/ratatui_browser_demo.py", "agents"), ("ratatui-markdown", "tests/ratatui_browser_demo.py", "markdown"),
                                         ("ratatui-usage", "tests/ratatui_browser_demo.py", "usage"), ("ratatui-completion", "tests/ratatui_browser_demo.py", "completion"),
                                         ("ratatui-subagent", "tests/ratatui_browser_demo.py", "subagent"),
                                         ("ratatui-local", "tests/ratatui_browser_demo.py", "local_disclosure"))
            if os.environ.get("NEXUS_RATATUI_MATRIX"):
                captures = [(f"redesign-{theme}-{sidebars}-{cols}", "tests/ratatui_browser_demo.py", f"redesign-{theme}-{sidebars}-Session")
                    for theme in ("dark","light") for sidebars in ("00","10","01","11") for cols in (80,120,200)]
                captures += [(f"redesign-dark-11-{mode}-200", "tests/ratatui_browser_demo.py", f"redesign-dark-11-{mode}") for mode in ("Files","MCP","Logs","expanded","popover","running")]
            for name, command, state in captures:
                if capture_names and name not in capture_names:
                    continue
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                server = subprocess.Popen([sys.executable, "tests/browser_serve.py", "--port", str(port), "--command", f"{sys.executable} {command}"], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT), NEXUS_RATATUI_STATE=state, NEXUS_TUI_BINARY=os.environ.get("NEXUS_TUI_BINARY", str(ROOT / "rust/tui/target/debug/nexus-ratatui"))), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
                page = browser.new_page(viewport={"width": 1100, "height": 850})
                page.add_init_script("""const originalSend = WebSocket.prototype.send;
                    WebSocket.prototype.send = function(data) {
                        if (typeof data === 'string') { try { const message=JSON.parse(data);
                            if (message[0]==='resize') window.__nexusTerminalSize=message[1]; } catch (_) {} }
                        return originalSend.call(this,data);
                    };""")
                try:
                    for attempt in range(40):
                        try:
                            page.goto(f"http://127.0.0.1:{port}?fontsize=16", timeout=2000)
                            page.get_by_role("textbox", name="Terminal input").wait_for(timeout=2000)
                            break
                        except Exception:
                            if server.poll() is not None:
                                raise RuntimeError(server.stderr.read().decode())
                            page.wait_for_timeout(250)
                    else:
                        raise RuntimeError("Terminal server did not become ready")
                    page.wait_for_timeout(1500)
                    assert server.poll() is None, "terminal process failed to start"
                    if state.startswith("redesign-"):
                        import math
                        cols = int(name.rsplit("-",1)[-1])
                        screen = page.locator(".xterm-screen").bounding_box()
                        size = page.evaluate("window.__nexusTerminalSize")
                        page.set_viewport_size({"width":math.ceil(cols*screen["width"]/size["width"]+2),"height":math.ceil(50*screen["height"]/size["height"])})
                        page.wait_for_timeout(300)
                        page.evaluate("size => window.__nexusSockets.at(-1).send(JSON.stringify(['resize',size]))", {"width":cols,"height":50})
                        page.wait_for_timeout(350)
                    page.screenshot(path=str(ARTIFACTS / f"{name}.png"))
                    if state == "local_disclosure":
                        page.get_by_role("textbox", name="Terminal input").click()
                        page.keyboard.press("Tab")
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(150)
                        page.screenshot(path=str(ARTIFACTS / f"{name}-group.png"))
                        page.keyboard.press("Tab")
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(150)
                        page.screenshot(path=str(ARTIFACTS / f"{name}-detail.png"))
                    if state or name != name.split("-")[0]:
                        print(f"{name}: captured")
                        continue
                    page.get_by_role("textbox", name="Terminal input").click()
                    page.keyboard.type("visual draft")
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(ARTIFACTS / f"{name}-draft.png"))
                    page.set_viewport_size({"width": 620, "height": 850})
                    page.wait_for_timeout(400)
                    page.screenshot(path=str(ARTIFACTS / f"{name}-narrow.png"))
                    if name == "ratatui":
                        page.keyboard.press("Control+q")
                finally:
                    page.close()
                    os.killpg(server.pid, signal.SIGTERM)
                    server.wait(timeout=5)
                print(f"{name}: real terminal captured at wide/narrow widths with draft input")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
