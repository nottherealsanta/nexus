"""Browser smoke and screenshot capture for the real Textual Nexus shell.

Run with ``python tests/visual_tui_check.py`` after installing ``.[dev]``.
Browser binaries are intentionally external to the repository; install once with
``python -m playwright install chromium`` when Playwright reports they are absent.
"""

from __future__ import annotations

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


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
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
