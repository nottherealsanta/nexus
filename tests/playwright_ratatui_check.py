"""Capture both real terminal clients with identical deterministic events.

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
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for name, command, state in (("textual", "tests/visual_tui_demo.py --state reference", ""), ("ratatui", "tests/ratatui_browser_demo.py", ""),
                                         ("textual-permission", "tests/visual_tui_demo.py --state permission", ""), ("ratatui-permission", "tests/ratatui_browser_demo.py", "permission"),
                                         ("textual-picker", "tests/visual_tui_demo.py --state picker", ""), ("ratatui-picker", "tests/ratatui_browser_demo.py", "picker"),
                                         ("ratatui-panel", "tests/ratatui_browser_demo.py", "panel"), ("ratatui-light", "tests/ratatui_browser_demo.py", "light"), ("ratatui-diff", "tests/ratatui_browser_demo.py", "diff")):
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                server = subprocess.Popen([sys.executable, "tests/browser_serve.py", "--port", str(port), "--command", f"{sys.executable} {command}"], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT), NEXUS_RATATUI_STATE=state), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
                page = browser.new_page(viewport={"width": 1100, "height": 850})
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
                    assert page.locator(".intro-dialog").is_hidden(), "terminal process failed to start"
                    page.screenshot(path=str(ARTIFACTS / f"{name}.png"))
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
