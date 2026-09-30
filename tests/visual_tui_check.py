"""Browser smoke and screenshot capture for the real Textual Nexus shell.

Run with ``python tests/visual_tui_check.py`` after installing ``.[dev]``.
Browser binaries are intentionally external to the repository; install once with
``python -m playwright install chromium`` when Playwright reports they are absent.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "visual-tui"
PORT = 8128
STATES = (
    ("empty-desktop", "empty", 800, 1000),
    ("empty-narrow", "empty", 430, 900),
    ("transcript-desktop", "transcript", 800, 1000),
    ("permission-desktop", "permission", 800, 1000),
    ("picker-narrow", "picker", 430, 900),
)


def _wait_for_server(page, port: int, server: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    # textual-serve needs a moment to start its web process before the first
    # websocket connection; retrying an in-flight page navigation is unreliable.
    time.sleep(2)
    while time.monotonic() < deadline:
        try:
            page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded", timeout=10_000)
            page.wait_for_timeout(500)
            if page.get_by_role("textbox", name="Terminal input").count():
                return
        except Exception:  # The server normally needs one short startup retry.
            time.sleep(0.25)
    if server.poll() is not None:
        raise RuntimeError(server.stderr.read() or f"textual serve exited with {server.returncode}")
    raise RuntimeError(f"Textual serve did not expose terminal input on port {port} within 30 seconds")


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path} is not a PNG")
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


async def capture_settings() -> list[Path]:
    """Capture every Settings pane from the actual shell's deterministic fixture."""
    from test_tui_panels import PanelTransport, _client
    from nexus.ui.tui.app import NexusTextualApp
    from nexus.ui_support.tui_settings import SettingsConsole
    from textual.widgets import Button

    app = NexusTextualApp(_client(PanelTransport()), session="s")
    outputs = []
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        app.action_open_settings()
        await pilot.pause()
        screen = app.screen
        for category, _ in SettingsConsole.SECTIONS:
            if category is None:
                continue
            screen.query_one("#settings-sections").highlighted = screen._section_index(category)
            await screen._change_category(category)
            await pilot.pause()
            for button in screen.query(Button):
                if button.visible and button.region.height:
                    assert button.region.height == 1, (category, button.id, button.region)
            output = ARTIFACTS / f"settings-{category}.svg"
            output.write_text(app.export_screenshot(), encoding="utf-8")
            outputs.append(output)
    return outputs


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    settings = asyncio.run(capture_settings())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1800, "height": 1300})
        for svg in settings:
            page.goto(svg.as_uri())
            page.locator("svg").screenshot(path=str(svg.with_suffix(".png")))
        browser.close()
        for name, state, width, height in STATES:
            command = [
                "uv", "run", "textual", "serve", "--host", "127.0.0.1", "--port", str(PORT), "--command",
                f"uv run python tests/visual_tui_demo.py --state {state}",
            ]
            server = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            try:
                _wait_for_server(page, PORT, server)
                page.wait_for_timeout(1_000)
                output = ARTIFACTS / f"{name}.png"
                page.screenshot(path=str(output))
                assert _png_size(output) == (width, height)
                print(f"{output.relative_to(ROOT)} {width}x{height} state={state}")
            finally:
                page.close()
                browser.close()
                if server.poll() is None:
                    os.killpg(server.pid, signal.SIGTERM)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    if server.poll() is None:
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=10)
                time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"visual TUI check failed: {exc}") from exc
