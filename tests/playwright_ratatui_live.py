"""Drive the real native client against a real daemon in dev mode (mock provider).

    PYTHONPATH=. python tests/playwright_ratatui_live.py "/mock hello" "/mock question" "blue wins"

Each argument is typed and submitted in turn (or `click:X,Y` / `key:Control+p` / `type:text` (no Enter) / `drag:X1,Y1,X2,Y2`); a screenshot follows each under
artifacts/ratatui-live/. Needs cargo build first. Dev mode isolates state
in ~/.nexus/dev; no provider credentials or network are used.
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/ratatui-live"; OUT.mkdir(exist_ok=True, parents=True)
ws = tempfile.mkdtemp()
with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
env = dict(os.environ, PYTHONPATH=str(ROOT), NEXUS_DEV="1", TERM="xterm-256color", XDG_CONFIG_HOME=tempfile.mkdtemp())  # fresh preferences: both sidebars on
env.pop("FORCE_COLOR", None)
cmd = f"{sys.executable} tests/ratatui_browser_demo.py --bridge {sys.executable} -m nexus --dev --workspace {ws} chat --renderer ratatui"
server = subprocess.Popen([sys.executable, "tests/browser_serve.py", "--port", str(port), "--command", cmd], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
with sync_playwright() as pw:
    b = pw.chromium.launch(); page = b.new_page(viewport={"width": 1900, "height": 900})
    try:
        for _ in range(40):
            try:
                page.goto(f"http://127.0.0.1:{port}?fontsize=14", timeout=2000)
                page.get_by_role("textbox", name="Terminal input").wait_for(timeout=2000); break
            except Exception:
                page.wait_for_timeout(250)
        page.wait_for_timeout(14000)
        page.screenshot(path=str(OUT / "1-start.png"))
        page.evaluate("document.querySelector('.xterm-helper-textarea').focus()")  # a click would hit the terminal UI
        for step, text in enumerate(sys.argv[1:], 2):
            if text.startswith("click:"):  # click:X,Y in page pixels
                x, y = (float(v) for v in text[6:].split(","))
                page.mouse.click(x, y); page.wait_for_timeout(1500)
            elif text.startswith("drag:"):  # drag:X1,Y1,X2,Y2 with the left button
                x1, y1, x2, y2 = (float(v) for v in text[5:].split(","))
                page.mouse.move(x1, y1); page.mouse.down(); page.mouse.move((x1 + x2) / 2, (y1 + y2) / 2); page.mouse.move(x2, y2)
                page.wait_for_timeout(300); page.screenshot(path=str(OUT / f"{step}-held.png")); page.mouse.up(); page.wait_for_timeout(1500)
            elif text.startswith("type:"):  # type without pressing Enter
                page.keyboard.type(text[5:]); page.wait_for_timeout(1500)
            elif text.startswith("key:"):  # key:Control+p
                page.keyboard.press(text[4:]); page.wait_for_timeout(1500)
            else:
                page.keyboard.type(text); page.keyboard.press("Enter"); page.wait_for_timeout(9000)
            page.screenshot(path=str(OUT / f"{step}.png"))
    finally:
        page.close(); os.killpg(server.pid, signal.SIGTERM); b.close()
        print(server.stderr.read().decode()[-2000:])
