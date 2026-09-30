"""Real-browser TUI context controls against a real host and MCP subprocesses."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_tui_check import _free_port, _stop, _wait_ready

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/context-controls"


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def wait(page, path, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read(path)
        if predicate(data):
            return data
        page.wait_for_timeout(100)
    raise AssertionError(f"Telemetry condition failed: {read(path)}")


def click(page, path, widget):
    data = wait(page, path, lambda data: widget in data.get("widgets", {}))
    region = data["widgets"][widget]
    screen = page.locator(".xterm-screen").bounding_box()
    assert screen and data["columns"] and data["rows"], "Terminal geometry unavailable"
    column = region["x"] + min(3, region["width"] - 1)
    row = region["y"] + (0 if widget.startswith("context-") else min(1, region["height"] - 1))
    page.mouse.click(screen["x"] + (column + 0.5) * screen["width"] / data["columns"],
                     screen["y"] + (row + 0.5) * screen["height"] / data["rows"], delay=100)
    page.wait_for_timeout(500)


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for width, height in ((1440, 1100), (900, 1100)):
                path = ARTIFACTS / f"state-{width}.json"
                path.unlink(missing_ok=True)
                port = _free_port()
                env = dict(os.environ, PYTHONPATH=str(ROOT), NEXUS_CONTEXT_TELEMETRY=str(path), NEXUS_NO_UPDATE_CHECK="1")
                server_process = subprocess.Popen([sys.executable, "tests/browser_serve.py", "--host", "127.0.0.1", "--port", str(port), "--command", f"{sys.executable} tests/context_controls_serve.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
                page = browser.new_page(viewport={"width": width, "height": height})
                try:
                    _wait_ready(page, port, server_process)
                    data = wait(page, path, lambda data: "Project 1 | Global 1" in data.get("widgets", {}).get("context-skills", {}).get("text", ""))
                    assert "Project 1 | Global 1" in data["widgets"]["context-mcp"]["text"]
                    page.wait_for_timeout(800)
                    for category, index, name in (("skills", 0, "global-skill"), ("skills", 1, "project-skill"), ("mcp", 0, "global-server"), ("mcp", 1, "project-server")):
                        click(page, path, f"context-{category}")
                        wait(page, path, lambda data: "extension-0" in data.get("widgets", {}))
                        for expected in (False, True, False):
                            click(page, path, f"extension-{index}")
                            data = wait(page, path, lambda data: bool(data.get("calls")) and data["calls"][-1]["category"] == category and data["calls"][-1]["enabled"] == expected)
                            assert data["calls"][-1]["name"] == name
                        page.screenshot(path=str(ARTIFACTS / f"{category}-{index}-{width}.png"))
                        page.keyboard.press("Escape")
                        wait(page, path, lambda data: "extension-0" not in data.get("widgets", {}))
                    click(page, path, "chat-editor")
                    page.keyboard.type("hello")
                    page.keyboard.press("Enter")
                    wait(page, path, lambda data: data.get("locked") and data.get("turns") == 1)
                    # Scroll back to the context header after the completed response.
                    page.mouse.wheel(0, -3000)
                    page.wait_for_timeout(500)
                    for category in ("skills", "mcp"):
                        click(page, path, f"context-{category}")
                        data = wait(page, path, lambda data: "extension-0" in data.get("widgets", {}))
                        assert all(row["disabled"] for key, row in data["widgets"].items() if key.startswith("extension-") and key != "extension-note")
                        before = len(data["calls"])
                        click(page, path, "extension-0")
                        assert len(read(path)["calls"]) == before
                        page.screenshot(path=str(ARTIFACTS / f"{category}-locked-{width}.png"))
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                    assert not any(call["error"] for call in read(path)["calls"])
                    print(f"PASS context choices, repeated toggles and first-turn lock at {width}px")
                finally:
                    page.close()
                    _stop(server_process)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
