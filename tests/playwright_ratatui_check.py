"""Capture the real native terminal client with identical deterministic events.

Run after cargo build: PYTHONPATH=. python tests/playwright_ratatui_check.py.
Screenshots prove rendered/interactive terminals, not complete feature parity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/ratatui-parity"
MAX_GROK_CAPTURES = 24
GROK_ROWS = 50
# Text a live scene must show; missing ones are reported in the JSON, not asserted (the parent reviews them).
GROK_MARKERS = {"dashboard": ["New Agent", "Waiting for response", "just now", "Worktree"], "mermaid": ["mermaid", "Start"],
                "groups": ["failed", "Thinking"], "context": ["System prompt", "Tools", "prompt tail"]}


def server_env(state, meta):
    """Child environment; Grok captures pin NEXUS_ASCII on or off explicitly, never inherited."""
    env = dict(os.environ, PYTHONPATH=str(ROOT), NEXUS_RATATUI_STATE=state,
               NEXUS_TUI_BINARY=os.environ.get("NEXUS_TUI_BINARY", str(ROOT / "rust/tui/target/debug/nexus-ratatui")))
    if meta is not None:
        env.pop("NEXUS_ASCII", None)
        if meta["ascii"]:
            env["NEXUS_ASCII"] = "1"
    return env


def dump_terminal(page, name, meta):
    """Write the xterm buffer as text + JSON so a parent can check the scene is not blank."""
    lines = page.evaluate("""() => { const b = term.buffer.active, out = [];
        for (let i = 0; i < term.rows; i++) { const l = b.getLine(i); out.push(l ? l.translateToString(true) : ''); } return out; }""")
    text = "\n".join(lines)
    nonblank = sum(1 for line in lines if line.strip())
    markers = GROK_MARKERS.get(meta["scene"], [])
    report = {"name": name, **meta, "term_cols": page.evaluate("term.cols"), "term_rows": page.evaluate("term.rows"),
              "nonblank_lines": nonblank, "non_ascii_chars": sum(ord(c) > 127 for c in text),
              "missing_markers": [m for m in markers if m.lower() not in text.lower()], "lines": lines}
    (ARTIFACTS / f"{name}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    (ARTIFACTS / f"{name}.txt").write_text("\n".join(lines).rstrip() + "\n")
    return report


def freeze_geometry(page, cols, rows):
    """Pin the xterm grid and PTY to cols x rows, then size the viewport exactly to the terminal screen."""
    import math
    page.evaluate("document.fonts.ready.then(() => document.fonts.load('16px monospace'))")
    page.wait_for_function("typeof socket !== 'undefined' && socket.readyState === WebSocket.OPEN")
    page.evaluate("([c, r]) => window.__nexusResize(c, r)", [cols, rows])
    page.wait_for_function("([c, r]) => term.cols === c && term.rows === r && window.__nexusTerminalSize"
                           " && window.__nexusTerminalSize.width === c && window.__nexusTerminalSize.height === r",
                           arg=[cols, rows], timeout=3000)
    screen = page.locator(".xterm-screen").bounding_box()
    page.set_viewport_size({"width": math.ceil(screen["width"]), "height": math.ceil(screen["height"])})
    page.wait_for_timeout(400)  # the page's onresize is now a no-op, so the grid must stay pinned
    geometry = measure_geometry(page)
    assert (geometry["term_cols"], geometry["term_rows"]) == (cols, rows), f"xterm grid {geometry} != {cols}x{rows}"
    assert geometry["pty_size"] == {"width": cols, "height": rows}, f"PTY size {geometry['pty_size']} != {cols}x{rows}"
    assert geometry["fits"], f"terminal screen does not fit the viewport: {geometry}"
    page.wait_for_timeout(350)


def measure_geometry(page):
    return page.evaluate("""() => { const s = document.querySelector('.xterm-screen').getBoundingClientRect();
        const r = document.querySelector('.xterm-rows'); const rr = r ? r.getBoundingClientRect() : null;
        const vw = innerWidth, vh = innerHeight;
        return {term_cols: term.cols, term_rows: term.rows, pty_size: window.__nexusTerminalSize || null,
            viewport: {width: vw, height: vh}, device_pixel_ratio: devicePixelRatio,
            screen_css: {x: s.x, y: s.y, width: s.width, height: s.height},
            cell_css: {width: s.width / term.cols, height: s.height / term.rows},
            rows_css: rr && {width: rr.width, height: rr.height},
            fonts_ready: document.fonts.status === 'loaded',
            fits: s.x >= 0 && s.y >= 0 && s.right <= vw + 0.5 && s.bottom <= vh + 0.5 &&
                  vw - s.width < 2 && vh - s.height < 2}; }""")


def write_geometry(page, name, cols, rows):
    """Sidecar next to the screenshot: image pixel size, grid, css screen box and whether it fits the viewport."""
    geometry = measure_geometry(page)
    geometry.update(name=name, wanted={"cols": cols, "rows": rows})
    png = (ARTIFACTS / f"{name}.png").read_bytes()
    geometry["image_px"] = {"width": int.from_bytes(png[16:20], "big"), "height": int.from_bytes(png[20:24], "big")}
    (ARTIFACTS / f"{name}.geometry.json").write_text(json.dumps(geometry, indent=1) + "\n")
    assert (geometry["term_cols"], geometry["term_rows"]) == (cols, rows) and geometry["fits"], f"{name} geometry: {geometry}"


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
            captures += tuple((f"ratatui-settings-{suffix}", "tests/ratatui_browser_demo.py", f"settings-{suffix}")
                              for suffix in ("tier", "default", "titles", "light", "tier-narrow"))
            captures += tuple((f"ratatui-live-{suffix}", "tests/ratatui_browser_demo.py", f"live-{suffix}")
                              for suffix in ("connecting", "listening", "speaking", "working", "muted", "paused", "error",
                                             "ended", "listening-open"))
            if os.environ.get("NEXUS_RATATUI_MATRIX"):
                captures = [(f"redesign-{theme}-{sidebars}-{cols}", "tests/ratatui_browser_demo.py", f"redesign-{theme}-{sidebars}-Session")
                    for theme in ("dark","light") for sidebars in ("00","10","01","11") for cols in (80,120,200)]
                captures += [(f"redesign-dark-11-{mode}-200", "tests/ratatui_browser_demo.py", f"redesign-dark-11-{mode}") for mode in ("Files","MCP","Logs","expanded","popover","running")]
            grok = {}
            if os.environ.get("NEXUS_RATATUI_GROK_MATRIX"):
                # Env: _SCENES, _WIDTHS, _THEMES (comma lists), _ASCII (1 default | 0 unicode | both). Hard cap keeps runs short.
                scenes = [s for s in os.environ.get("NEXUS_RATATUI_GROK_SCENES", "dashboard,mermaid,groups,context").split(",") if s]
                widths = [int(w) for w in os.environ.get("NEXUS_RATATUI_GROK_WIDTHS", "50,90,140").split(",") if w]
                themes = [t for t in os.environ.get("NEXUS_RATATUI_GROK_THEMES", "dark,light").split(",") if t]
                modes = {"1": ["ascii"], "0": ["unicode"], "both": ["ascii", "unicode"]}[os.environ.get("NEXUS_RATATUI_GROK_ASCII", "1")]
                captures = [(f"grok-{scene}-{theme}-{cols}-{mode}", "tests/ratatui_browser_demo.py", f"grok-{scene}-{theme}")
                            for scene in scenes for theme in themes for cols in widths for mode in modes]
                if len(captures) > MAX_GROK_CAPTURES:
                    raise SystemExit(f"{len(captures)} grok captures exceeds the cap of {MAX_GROK_CAPTURES}; narrow scenes/widths/themes")
                for name, _, state in captures:
                    scene, theme, cols, mode = name.split("-")[1:]
                    grok[name] = {"scene": scene, "theme": theme, "cols": int(cols), "ascii": mode == "ascii", "rows": GROK_ROWS}
            failures = []
            for name, command, state in captures:
                if capture_names and name not in capture_names:
                    continue
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                server = subprocess.Popen([sys.executable, "tests/browser_serve.py", "--port", str(port), "--command", f"{sys.executable} {command}"], cwd=ROOT, env=server_env(state, grok.get(name)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
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
                    assert server.poll() is None, "terminal process failed to start"
                    if state.startswith(("redesign-", "grok-")):
                        cols = grok[name]["cols"] if name in grok else int(name.rsplit("-",1)[-1])
                        rows = grok[name]["rows"] if name in grok else 50
                        freeze_geometry(page, cols, rows)
                    if state.startswith("settings"):
                        if "narrow" in state:
                            page.set_viewport_size({"width": 850, "height": 620})
                            page.wait_for_timeout(500)
                        page.get_by_role("textbox", name="Terminal input").click()
                        page.keyboard.press("ArrowLeft")
                        page.wait_for_timeout(200)
                    if name in grok and grok[name]["scene"] == "context":
                        page.get_by_role("textbox", name="Terminal input").click()
                        page.keyboard.type("prompt tail draft")
                        page.wait_for_timeout(300)
                    page.screenshot(path=str(ARTIFACTS / f"{name}.png"))
                    if state.startswith(("redesign-", "grok-")):
                        write_geometry(page, name, cols, rows)
                    if name in grok:
                        report = dump_terminal(page, name, grok[name])
                        flag = "BLANK" if report["nonblank_lines"] == 0 else "missing " + ",".join(report["missing_markers"]) if report["missing_markers"] else "ok"
                        if report["nonblank_lines"] == 0:
                            failures.append(name)
                        print(f"{name}: captured {report['term_cols']}x{report['term_rows']} nonblank={report['nonblank_lines']} {flag}")
                        continue
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
    if failures:
        raise SystemExit(f"blank terminal captures: {', '.join(failures)}")


if __name__ == "__main__":
    main()
