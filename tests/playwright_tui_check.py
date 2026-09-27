"""Playwright-driven functional check of the real Textual Nexus shell.

This drives the app Textual's local web server exposes (the same TUI a real
terminal runs) through a real browser: it types into the editor, presses Enter,
and asserts on the rendered terminal plus captured screenshots. It exercises the
fixture transport, so no model provider or daemon is involved.

Textual serves through xterm's canvas renderer, so terminal cells are not
readable from the DOM. The checks therefore use reliable signals instead of
screen scraping: captured websocket ``stdin`` frames (the exact bytes the real
browser sends), the fixture transport's submission log (what the app actually
submitted, newlines included), and screenshot pixel signatures (a hash of the
rendered PNG bytes). The deterministic reducer/editor semantics themselves are
pinned by the Textual pilot tests in ``tests/test_ui_tui.py``; this module is
the real-browser confirmation and screenshot capture.

The multiline contract *is* checked here. Plain ``textual serve`` collapses
Enter, Shift+Enter and Ctrl+Enter onto a bare carriage return, so the browser
check drives the app through ``tests/browser_serve.py``, the serving layer that
adds the small keyboard bridge described there. It asserts that Shift+Enter puts
the draft on a new line (nothing submitted) and that Enter submits the whole
multiline draft.

Run with::

    python tests/playwright_tui_check.py

Browser binaries are intentionally external to the repository; install once
with ``python -m playwright install chromium`` when Playwright reports them
absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "visual-tui"
PORT = 8129


def _shot(page: Page, name: str) -> bytes:
    data = page.screenshot(path=str(ARTIFACTS / name))
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 100, (
        f"screenshot {name} was empty or not a PNG ({len(data)} bytes)"
    )
    return data


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _text_pixels(page: Page) -> int:
    """Count lit pixels on xterm's text layer, a browser-readable content measure.

    Textual paints terminal cells onto the ``.xterm-text-layer`` canvas, so the
    screen text never enters the DOM. The number of non-background pixels there
    is a genuine signal of how much is rendered: the empty shell settles at a
    low chrome floor, while a populated transcript is far above it. Returns
    ``-1`` when the canvas renderer is unavailable so callers fail loudly rather
    than passing on a missing signal.
    """
    return page.evaluate(
        """() => {
            const canvas = document.querySelector('canvas.xterm-text-layer');
            if (!canvas) return -1;
            const context = canvas.getContext('2d');
            if (!context) return -1;
            const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
            let lit = 0;
            for (let i = 0; i < data.length; i += 4) {
                if (data[i] > 40 || data[i + 1] > 40 || data[i + 2] > 40) lit += 1;
            }
            return lit;
        }"""
    )


def _empty_text_pixels(browser: Browser) -> int:
    """Baseline lit-pixel count for the empty shell, which carries chrome only."""
    server = _start("empty")
    page = browser.new_page(viewport={"width": 900, "height": 1100}, device_scale_factor=1)
    try:
        _wait_ready(page, PORT, server)
        page.wait_for_timeout(800)
        pixels = _text_pixels(page)
        assert pixels > 0, "empty shell exposed no xterm text-layer canvas"
        return pixels
    finally:
        page.close()
        _stop(server)


def _wait_ready(
    page: Page, port: int, server: subprocess.Popen[str], *, font_size: int | None = None
) -> None:
    deadline = time.monotonic() + 30
    time.sleep(2)
    while time.monotonic() < deadline:
        try:
            query = f"?fontsize={font_size}" if font_size is not None else ""
            page.goto(f"http://127.0.0.1:{port}{query}", wait_until="domcontentloaded", timeout=10_000)
            page.wait_for_timeout(800)
            box = page.get_by_role("textbox", name="Terminal input")
            if box.count():
                box.click()
                page.wait_for_timeout(500)
                return
        except Exception:  # noqa: BLE001 - retry transient browser-server startup failures
            time.sleep(0.25)
    if server.poll() is not None:
        raise RuntimeError(server.stderr.read() or f"browser server exited {server.returncode}")
    raise RuntimeError("browser server did not expose terminal input in time")


def _start(state: str, *, submit_log: Path | None = None) -> subprocess.Popen[str]:
    """Serve a fixture through the bridge-serving layer (not raw ``textual serve``).

    Plain ``textual serve``'s frontend cannot report Shift+Enter, so the browser
    runs go through ``tests/browser_serve.py``, which injects the CSI-u keyboard
    bridge. ``submit_log`` enables the fixture's app-level submission record.
    """
    env = dict(os.environ)
    if submit_log is not None:
        submit_log.parent.mkdir(parents=True, exist_ok=True)
        submit_log.write_text("", encoding="utf-8")
        env["NEXUS_VISUAL_SUBMIT_LOG"] = str(submit_log)
    return subprocess.Popen(
        [
            "uv", "run", "python", "tests/browser_serve.py",
            "--host", "127.0.0.1", "--port", str(PORT),
            "--command", f"uv run python tests/visual_tui_demo.py --state {state}",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )


def _start_acceptance(
    command_log: Path, *, turn_gate: Path | None = None
) -> subprocess.Popen[str]:
    """Serve the real shell against the dedicated deterministic command fixture."""
    command_log.parent.mkdir(parents=True, exist_ok=True)
    command_log.write_text("", encoding="utf-8")
    env = dict(os.environ, NEXUS_TUI_ACCEPTANCE_LOG=str(command_log))
    if turn_gate is not None:
        turn_gate.unlink(missing_ok=True)
        env["NEXUS_TUI_ACCEPTANCE_TURN_GATE"] = str(turn_gate)
    return subprocess.Popen(
        [
            "uv", "run", "python", "tests/browser_serve.py",
            "--host", "127.0.0.1", "--port", str(PORT),
            "--command", "uv run python tests/tui_acceptance_fixture.py",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )


def _sent_stdin(frames: list[str]) -> str:
    """Reconstruct the stdin byte stream from captured websocket frames."""
    chunks: list[str] = []
    for frame in frames:
        try:
            envelope = json.loads(frame)
        except ValueError:
            continue
        if isinstance(envelope, list) and len(envelope) == 2 and envelope[0] == "stdin":
            chunks.append(envelope[1])
    return "".join(chunks)


def _received_terminal_text(frames: list[str]) -> str:
    """Extract textual websocket payloads from the served terminal stream."""
    chunks: list[str] = []
    for frame in frames:
        if isinstance(frame, bytes):
            chunks.append(frame.decode("utf-8", errors="replace"))
            continue
        try:
            envelope = json.loads(frame)
        except ValueError:
            continue
        if (
            isinstance(envelope, list)
            and len(envelope) == 2
            and envelope[0] in {"stdout", "output", "terminal"}
            and isinstance(envelope[1], str)
        ):
            chunks.append(envelope[1])
    text = "".join(chunks)
    # The textual-serve websocket emits terminal escape sequences directly;
    # remove styling/cursor controls while preserving their rendered text.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return text


def _read_submissions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_acceptance_log(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wait_acceptance_rows(path: Path, predicate, timeout: float = 10.0) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_acceptance_log(path)
        if predicate(rows):
            return rows
        time.sleep(0.1)
    return _read_acceptance_log(path)


def _wait_submissions(path: Path, count: int, timeout: float = 10.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_submissions(path)
        if len(rows) >= count:
            return rows
        time.sleep(0.1)
    return _read_submissions(path)


def _terminal_grid(page: Page) -> dict[str, float]:
    """Return live xterm cell geometry (not the browser wrapper's font metrics)."""
    geometry = page.evaluate(
        """() => {
            const screen = document.querySelector('.xterm-screen');
            const cell = document.querySelector('.xterm-helper-textarea');
            if (!screen || !cell) return null;
            const rect = screen.getBoundingClientRect();
            const cellRect = cell.getBoundingClientRect();
            // xterm positions this helper textarea at a cell and sizes it to one
            // cell. The outer .xterm wrapper reports the page's unrelated
            // default Times font, so it cannot be used to infer the grid.
            const cellWidth = cellRect.width;
            const cellHeight = cellRect.height;
            if (!cellWidth || !cellHeight) return null;
            return {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                    cellWidth, cellHeight, cols: Math.floor(rect.width / cellWidth),
                    rows: Math.floor(rect.height / cellHeight),
                    cursorColumn: Math.round((cellRect.x - rect.x) / cellWidth),
                    cursorRow: Math.round((cellRect.y - rect.y) / cellHeight)};
        }"""
    )
    assert geometry is not None, "xterm live cell/screen geometry unavailable"
    return geometry


def _terminal_cell_point(page: Page, column: int, row: int) -> tuple[float, float]:
    """Return the center of a terminal cell using xterm's live cell geometry."""
    geometry = _terminal_grid(page)
    assert 0 <= column < geometry["cols"] and 0 <= row < geometry["rows"], (
        f"terminal cell ({column}, {row}) outside live geometry {geometry!r}"
    )
    return (
        geometry["x"] + (column + 0.5) * geometry["cellWidth"],
        geometry["y"] + (row + 0.5) * geometry["cellHeight"],
    )


def _click_inline_picker_option(page: Page, index: int, option_count: int) -> dict[str, float]:
    """Click a visible inline-picker option using its live terminal row.

    The focused OptionList owns xterm's live helper textarea/cursor. Its first
    visible option starts at the list's top row, so this follows live geometry.
    """
    geometry = _terminal_grid(page)
    assert 0 <= index < option_count, f"picker option index {index} outside {option_count} options"
    # The editor cursor sits two rows into its three-row editor. The inline
    # picker has a one-row border above the list and one below it; options end
    # immediately before the editor, so include the upper border in the offset.
    row = geometry["cursorRow"] - option_count - 2 + index
    x, y = _terminal_cell_point(page, geometry["cursorColumn"] + 1, row)
    page.mouse.click(x, y)
    return {
        **geometry, "option_row": row, "click_column": geometry["cursorColumn"] + 1,
        "click_x": x, "click_y": y,
    }


def _stop(server: subprocess.Popen[str]) -> None:
    if server.poll() is None:
        os.killpg(server.pid, signal.SIGTERM)
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait(timeout=10)
    time.sleep(0.5)


def _check_transcript(playwright, browser: Browser) -> None:
    baseline = _empty_text_pixels(browser)
    server = _start("transcript")
    page = browser.new_page(viewport={"width": 900, "height": 1100}, device_scale_factor=1)
    received: list[str] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: received.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        page.wait_for_timeout(1_200)
        _shot(page, "functional-transcript.png")
        # The seeded user message and the Read/Edit tool cards must render as real
        # content. Terminal text lives on the xterm canvas, not in the DOM, so the
        # genuine signal is the lit-pixel count: a populated transcript paints far
        # more than the empty shell's chrome.
        pixels = _text_pixels(page)
        assert pixels > baseline, (
            f"transcript rendered no more content than the empty shell "
            f"({pixels} <= {baseline} lit pixels)"
        )
        text = _received_terminal_text(received)
        assert (
            "0.0s" in text
            and "Elapsed" not in text
            and "Model demo/nexus-small" in text
            and "Effort Default" in text
        ), (
            f"completed transcript omitted its turn summary: {text!r}"
        )
        print("transcript: screenshot captured (user message + Read/Edit tool cards + completed turn summary)")
    finally:
        page.close()
        _stop(server)


def _check_reference_terminal(playwright, browser: Browser) -> None:
    """Capture the deterministic reference transcript at its source viewport."""
    server = _start("reference")
    page = browser.new_page(
        viewport={"width": 2000, "height": 1521},
        device_scale_factor=1,
        reduced_motion="reduce",
    )
    received: list[str] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: received.append(payload)))
    try:
        # textual-serve's supported font-size query configures xterm before its
        # websocket starts, which resizes both the canvas and terminal grid.
        _wait_ready(page, PORT, server, font_size=24)
        # Hide xterm's empty-draft block cursor for the still-live visual demo;
        # this is preview-only and does not change Textual's terminal behavior.
        page.add_style_tag(content=".xterm-cursor-layer { visibility: hidden !important; }")
        page.wait_for_timeout(1_000)
        image = _shot(page, "reference-terminal-tui.png")
        pixels = _text_pixels(page)
        assert pixels > 10_000, f"reference transcript rendered too little terminal text ({pixels} lit pixels)"
        assert len(image) > 10_000, f"reference screenshot unexpectedly small ({len(image)} bytes)"
        assert page.get_by_role("textbox", name="Terminal input", include_hidden=True).count() == 1, (
            "reference screenshot lost the live terminal input"
        )
        assert page.locator(".xterm").count() == 1, "reference screenshot has no active xterm"
        assert not page.get_by_text("Session ended.").is_visible(), (
            "reference screenshot is the terminal-server restart fallback"
        )
        terminal_text = _received_terminal_text(received)
        # Canvas text is inaccessible as DOM text; pin major transcript content
        # through the screenshot's real websocket output stream instead.
        assert "Hi! What can I help you with?" in terminal_text
        assert "how to start the daemon" in terminal_text
        print(f"reference TUI: screenshot captured at 2000x1521 ({pixels} lit text pixels)")
    finally:
        page.close()
        _stop(server)


def _check_multiline_enter(playwright, browser: Browser, submit_log: Path) -> None:
    """Real-browser proof of the Enter/Shift+Enter contract.

    Shift+Enter must reach the app as Kitty CSI-u ``shift+enter`` (not a bare CR)
    and must not submit; Enter then submits the whole multiline draft. Signals:
    the exact websocket ``stdin`` frames and the fixture's submission log.
    """
    server = _start("functional", submit_log=submit_log)
    page = browser.new_page(viewport={"width": 900, "height": 1100}, device_scale_factor=1)
    sent: list[str] = []
    page.on("websocket", lambda ws: ws.on("framesent", lambda payload: sent.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        page.wait_for_timeout(1_200)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("alpha")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("beta")
        page.wait_for_timeout(800)
        draft = _shot(page, "functional-draft-newline.png")

        # The real browser sent the Kitty shift+enter sequence, and the app has
        # not submitted anything yet: the draft is genuinely a second line.
        stream = _sent_stdin(sent)
        assert "\x1b[13;2u" in stream, f"Shift+Enter did not send CSI-u shift+enter: {stream!r}"
        assert "alpha\x1b[13;2ubeta" in stream, f"typing/Shift+Enter order unexpected: {stream!r}"
        assert _read_submissions(submit_log) == [], "Shift+Enter submitted the draft"

        page.keyboard.press("Enter")
        page.wait_for_timeout(3_000)
        submitted = _shot(page, "functional-after-enter.png")

        # Enter sent a bare CR and the app submitted the draft with its newline.
        stream = _sent_stdin(sent)
        assert "\r" in stream, "Enter did not send a carriage return"
        rows = _wait_submissions(submit_log, 1)
        assert rows == ["alpha\nbeta"], f"app submitted {rows!r}, not the multiline draft"
        assert _hash(draft) != _hash(submitted), "screen did not change after Enter"
        print("multiline: Shift+Enter newlined (unsubmitted) then Enter sent 'alpha\\nbeta'")
    finally:
        page.close()
        _stop(server)


def _check_commands_shortcuts(playwright, browser: Browser) -> None:
    server = _start("empty")
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    try:
        _wait_ready(page, PORT, server)
        page.wait_for_timeout(800)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        closed = _shot(page, "functional-palette-closed.png")
        page.keyboard.press("Control+p")
        page.wait_for_timeout(1_000)
        palette = _shot(page, "functional-command-palette.png")
        assert _hash(closed) != _hash(palette), "Ctrl+P did not open the command palette"
        # The palette is open and searchable; typing browses the shortcut entry,
        # and Enter activates it (a real invocation, not just a query match).
        page.keyboard.type("shortcuts")
        page.wait_for_timeout(800)
        filtered = _shot(page, "functional-command-palette-shortcuts.png")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1_000)
        opened = _shot(page, "functional-keyboard-shortcuts.png")
        assert _hash(opened) != _hash(filtered), (
            "the Show keyboard shortcuts command did not open the reference"
        )
        print("commands: Ctrl+P palette opened and Show keyboard shortcuts rendered")
    finally:
        page.close()
        _stop(server)


def _check_slash_suggestions_keyboard(playwright, browser: Browser, command_log: Path) -> None:
    """Render the complete seven-row slash page and execute a non-first item."""
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    received: list[str] = []
    sent: list[str] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: received.append(payload)))
    page.on("websocket", lambda ws: ws.on("framesent", lambda payload: sent.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("/")
        page.wait_for_timeout(300)
        before = _shot(page, "functional-slash-suggestions.png")
        page.keyboard.press("ArrowDown", delay=30)
        page.keyboard.press("ArrowDown", delay=30)
        page.keyboard.press("ArrowDown", delay=30)
        page.keyboard.press("ArrowDown", delay=30)
        page.keyboard.press("ArrowDown", delay=30)
        page.keyboard.press("Enter")
        deadline = time.monotonic() + 5
        terminal = ""
        while time.monotonic() < deadline:
            terminal = _received_terminal_text(received)
            if "session visual" in terminal:
                break
            page.wait_for_timeout(100)
        after = _shot(page, "functional-slash-details-executed.png")
        assert before != after, "selecting /details did not change the rendered screen"
        assert "session visual" in terminal, (
            f"non-first /details suggestion did not execute: {terminal!r}"
        )
        stream = _sent_stdin(sent)
        assert "/" in stream and "\x1b[B" in stream and "\r" in stream, stream
        print("slash suggestions: seven rows rendered; ArrowDown×5 + Enter executed /details")
    finally:
        page.close()
        _stop(server)


def _check_slash_new_visible(playwright, browser: Browser, command_log: Path) -> None:
    """Show /new immediately for /n and execute it with Enter, without Down."""
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    received: list[str] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: received.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        page.wait_for_timeout(500)
        text_layer = page.locator("canvas.xterm-text-layer").first
        baseline_lit = text_layer.evaluate(
            """canvas => {
              const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              let lit = 0;
              for (let i = 0; i < data.length; i += 4) {
                if (data[i] > 40 || data[i + 1] > 40 || data[i + 2] > 40) lit++;
              }
              return lit;
            }"""
        )
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("/n", delay=60)
        page.wait_for_timeout(600)
        suggestion = _shot(page, "functional-slash-new-suggestion.png")
        suggestion_lit = text_layer.evaluate(
            """canvas => {
              const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
              let lit = 0;
              for (let i = 0; i < data.length; i += 4) {
                if (data[i] > 40 || data[i + 1] > 40 || data[i + 2] > 40) lit++;
              }
              return lit;
            }"""
        )
        assert suggestion_lit > baseline_lit + 30, (
            "typing /n produced no light suggestion text in the completion row; "
            f"text-layer lit pixels changed {baseline_lit} -> {suggestion_lit}; "
            f"terminal stream={_received_terminal_text(received)!r}"
        )

        # No navigation key: Enter must choose the visible default suggestion.
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "SessionOpen"
                and str(row.get("active_session", "")).startswith("session-")
                for row in current
            ),
        )
        assert any(
            row["command"] == "SessionOpen"
            and str(row.get("active_session", "")).startswith("session-")
            for row in rows
        ), f"Enter on the visible /new suggestion did not open a session: {rows!r}"
        opened = _shot(page, "functional-slash-new-opened.png")
        assert suggestion != opened, "Enter on /new did not change the visible session"
        print("slash /n: /new painted immediately; Enter opened a new session without Down")
    finally:
        page.close()
        _stop(server)


def _check_model_picker_keyboard(playwright, browser: Browser, command_log: Path) -> None:
    """Submit /model once, then select through actual browser keyboard input."""
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    sent: list[str] = []
    page.on("websocket", lambda ws: ws.on("framesent", lambda payload: sent.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("/model")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "ModelsList" for row in current),
        )
        assert sum(row["command"] == "ModelsList" for row in rows) == 1, (
            f"one-Enter /model should issue exactly one ModelsList: rows={rows!r}, "
            f"stdin={_sent_stdin(sent)!r}"
        )
        _shot(page, "functional-model-picker-keyboard.png")
        assert page.get_by_role("textbox", name="Search agents or models…").count() == 0, (
            "inline model picker should not render a separate search field"
        )
        # The list owns focus immediately; ArrowDown selects beta.
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "ModelSelect" and row.get("selected_model") == "beta"
                for row in current
            ),
        )
        assert any(
            row["command"] == "ModelSelect"
            and row.get("active_session") == "visual"
            and row.get("selected_model") == "beta"
            for row in rows
        ), f"keyboard picker selection did not reach host as fixture/beta: {rows!r}"
        stream = _sent_stdin(sent)
        assert "/model\r" in stream, f"browser did not send /model followed by Enter: {stream!r}"
        assert "\x1b[B" in stream, f"browser did not send ArrowDown to the picker: {stream!r}"
        print("model picker keyboard: one-Enter /model opened one model list; ArrowDown+Enter selected fixture/beta")
    finally:
        page.close()
        _stop(server)


def _check_model_picker_mouse(playwright, browser: Browser, command_log: Path) -> None:
    """Select the first offered model with a real pointer click on xterm."""
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    sent: list[str] = []
    page.on("websocket", lambda ws: ws.on("framesent", lambda payload: sent.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("/model")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "ModelsList" for row in current),
        )
        assert sum(row["command"] == "ModelsList" for row in rows) == 1
        page.wait_for_timeout(500)
        _shot(page, "functional-model-picker-mouse-open.png")
        geometry = _click_inline_picker_option(page, 0, option_count=2)
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "ModelSelect" for row in current),
        )
        selected = [row for row in rows if row["command"] == "ModelSelect"]
        assert selected and selected[-1].get("selected_model") == "alpha", (
            f"mouse click did not select first fixture model alpha: {selected!r}; "
            f"target_geometry={geometry!r}; mouse bytes={_sent_stdin(sent)!r}"
        )
        _shot(page, "functional-model-picker-mouse-selected.png")
        print("model picker mouse: pointer click on first terminal option selected fixture/alpha")
    finally:
        page.close()
        _stop(server)


def _check_agent_picker_command(playwright, browser: Browser, command_log: Path) -> None:
    """One Enter opens /agent; keyboard and pointer selections reach the host."""
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    sent: list[str] = []
    page.on("websocket", lambda ws: ws.on("framesent", lambda payload: sent.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        initial_rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "AgentsList" for row in current),
        )
        # Let asynchronous shell bootstrap finish before recording the baseline;
        # otherwise its initial AgentsList can race the /agent request below.
        page.wait_for_timeout(400)
        initial_rows = _read_acceptance_log(command_log)
        initial_agent_lists = sum(row["command"] == "AgentsList" for row in initial_rows)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("/agent")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: sum(row["command"] == "AgentsList" for row in current)
            > initial_agent_lists,
        )
        assert sum(row["command"] == "AgentsList" for row in rows) == initial_agent_lists + 1, (
            f"one-Enter /agent should issue exactly one additional AgentsList: {rows!r}"
        )
        _shot(page, "functional-agent-picker-open.png")
        # The list owns focus immediately; ArrowUp selects General.
        page.keyboard.press("ArrowUp")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "AgentSelect" for row in current),
        )
        assert any(
            row["command"] == "AgentSelect"
            and row.get("active_session") == "visual"
            and row.get("selected_agent") == "general"
            for row in rows
        ), f"keyboard agent picker selection did not reach host as general: {rows!r}"
        stream = _sent_stdin(sent)
        assert "/agent\r" in stream, f"browser did not send /agent followed by one Enter: {stream!r}"
        assert stream.count("/agent\r") == 1, f"/agent was submitted more than once: {stream!r}"

        # Exercise the same rendered option rows through the real browser
        # pointer path, choosing Explore independently of keyboard selection.
        page.wait_for_timeout(300)
        box.click()
        page.keyboard.type("/agent")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: sum(row["command"] == "AgentsList" for row in current)
            > initial_agent_lists + 1,
        )
        assert sum(row["command"] == "AgentsList" for row in rows) == initial_agent_lists + 2, (
            f"second /agent did not open one additional picker: rows={rows!r}; "
            f"stdin={_sent_stdin(sent)!r}"
        )
        _shot(page, "functional-agent-picker-mouse-open.png")
        # OptionList mounts scrolled to the previous selection; explicitly
        # reveal the third row before translating its viewport cell position.
        page.keyboard.press("ArrowDown")
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(100)
        geometry = _click_inline_picker_option(page, 2, option_count=3)
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "AgentSelect" and row.get("selected_agent") == "explore"
                for row in current
            ),
        )
        assert any(
            row["command"] == "AgentSelect"
            and row.get("active_session") == "visual"
            and row.get("selected_agent") == "explore"
            for row in rows
        ), f"mouse agent picker selection missed Explore: {rows!r}; target={geometry!r}"
        print("agent picker: keyboard selected general; pointer selected explore from the live option row")
    finally:
        page.close()
        _stop(server)


def _check_model_picker_rejection(playwright, browser: Browser, command_log: Path) -> None:
    """A host-rejected model selection leaves the prior model and shows feedback."""
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    received: list[str] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: received.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("/model")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "ModelsList" for row in current),
        )
        assert sum(row["command"] == "ModelsList" for row in rows) == 1
        picker = _shot(page, "functional-model-picker-rejection-open.png")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "ModelSelect"
                and row.get("selected_model") == "beta"
                and row.get("accepted") is False
                for row in current
            ),
        )
        rejection = [
            row for row in rows
            if row["command"] == "ModelSelect" and row.get("accepted") is False
        ]
        assert rejection and rejection[-1].get("prior_model") == "alpha", (
            f"fixture did not reject beta while alpha was active: {rows!r}"
        )
        status = _shot(page, "functional-model-picker-rejection-status.png")
        assert _hash(status) != _hash(picker), "model rejection produced no visible status change"
        assert _text_pixels(page) > 0, "model rejection status rendered no terminal text"
        terminal_text = _received_terminal_text(received)
        assert "Model selection failed" in terminal_text and "fixture rejected beta" in terminal_text, (
            f"host rejection was not visible in terminal output: {terminal_text!r}; "
            f"received frames={received!r}"
        )
        current_rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "AgentCurrent" and row.get("model") == "alpha"
                for row in current
            ),
        )
        assert any(
            row["command"] == "AgentCurrent" and row.get("model") == "alpha"
            for row in current_rows
        ), f"rejected model selection changed the host's prior model: {current_rows!r}"
        print("model picker rejection: beta rejected, visible status changed, active model remained alpha")
    finally:
        page.close()
        _stop(server)


def _check_running_turn_draft(playwright, browser: Browser, command_log: Path) -> None:
    """Hold the first turn open and verify Enter preserves, but does not send, a draft."""
    gate = ARTIFACTS / "acceptance-turn-complete.gate"
    server = _start_acceptance(command_log, turn_gate=gate)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    received: list[str] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: received.append(payload)))
    try:
        _wait_ready(page, PORT, server)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()
        page.keyboard.type("first turn")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "SessionStart" for row in current),
        )
        assert sum(row["command"] == "SessionStart" for row in rows) == 1, (
            f"first prompt did not start exactly one turn: {rows!r}"
        )
        # Event replay reaches turn.completed but the fixture withholds it, so
        # controller.running remains true while the second draft is submitted.
        page.keyboard.type("kept draft")
        page.keyboard.press("Enter")
        page.wait_for_timeout(700)
        held_rows = _read_acceptance_log(command_log)
        starts = [row for row in held_rows if row["command"] == "SessionStart"]
        enqueues = [row for row in held_rows if row["command"] == "SessionEnqueue"]
        assert len(starts) == 1 and not enqueues, (
            f"Enter during the held turn sent another prompt: starts={starts!r}, enqueues={enqueues!r}"
        )
        held_screen = _shot(page, "functional-running-turn-draft-held.png")
        assert held_screen, "running-turn screenshot unexpectedly empty"
        terminal_text = _received_terminal_text(received)
        assert "Turn running · message not sent" in terminal_text, (
            f"running-turn feedback was not visible in terminal output: {terminal_text!r}; "
            f"received frames={received!r}"
        )

        gate.write_text("release", encoding="utf-8")
        page.wait_for_timeout(1_000)
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: sum(row["command"] == "SessionStart" for row in current) >= 2,
        )
        starts = [row for row in rows if row["command"] == "SessionStart"]
        assert [row.get("content") for row in starts] == ["first turn", "kept draft"], (
            f"held-turn draft was not retained for later submission: {starts!r}"
        )
        assert not [row for row in rows if row["command"] == "SessionEnqueue"], (
            f"running-turn Enter unexpectedly enqueued a message: {rows!r}"
        )
        print("running turn: second Enter sent no SessionStart/SessionEnqueue and retained draft for later submission")
    finally:
        gate.write_text("release", encoding="utf-8")
        page.close()
        _stop(server)


def _check_logs_reasoning_and_sessions(playwright, browser: Browser, command_log: Path) -> None:
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    try:
        _wait_ready(page, PORT, server)
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()

        # An empty session retains the compact context-preview footer without
        # rendering session identity or connection state into the transient bar.
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "AgentCurrent" for row in current),
        )
        empty_screen = _shot(page, "functional-empty-context-footer.png")
        assert empty_screen and rows, "empty context/footer screen was not captured"
        page.keyboard.type("/context")
        page.keyboard.press("Enter")
        details = _shot(page, "functional-empty-context-details.png")
        assert details, "context-details screenshot was empty"
        page.keyboard.press("Escape")
        box = page.get_by_role("textbox", name="Terminal input")
        box.click()

        page.keyboard.press("Control+e")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "LogsRead" for row in current),
        )
        opened_count = sum(row["command"] == "LogsRead" for row in rows)
        assert opened_count >= 1, f"Ctrl+E did not open/poll Logs drawer: {rows!r}"
        _shot(page, "functional-logs-open.png")
        page.keyboard.press("Control+e")
        page.wait_for_timeout(1_300)
        closed_count = sum(
            row["command"] == "LogsRead" for row in _read_acceptance_log(command_log)
        )
        assert closed_count <= opened_count + 1, (
            f"LogsRead continued polling after drawer close: {opened_count} -> {closed_count}"
        )
        _shot(page, "functional-logs-closed.png")

        box.click()
        page.keyboard.press("Control+t")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: len([row for row in current if row["command"] == "AgentCurrent"]) >= 4,
        )
        _shot(page, "functional-effort-picker-open.png")
        # The effort picker is a typeahead-filtered list above the composer.
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "ReasoningEffortSelect"
                and row.get("selected_effort") == "medium"
                for row in current
            ),
        )
        assert any(
            row["command"] == "ReasoningEffortSelect"
            and row.get("active_session") == "visual"
            and row.get("selected_effort") == "medium"
            for row in rows
        ), f"Ctrl+T failed to cycle supported effort low -> medium: {rows!r}"
        page.keyboard.press("Control+o")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "SessionList" for row in current),
        )
        assert any(row["command"] == "SessionList" for row in rows), (
            f"Ctrl+O did not request host session navigation list: {rows!r}"
        )
        page.keyboard.press("Control+n")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(
                row["command"] == "SessionOpen"
                and str(row.get("active_session", "")).startswith("session-")
                for row in current
            ),
        )
        assert any(
            row["command"] == "SessionOpen"
            and str(row.get("active_session", "")).startswith("session-")
            for row in rows
        ), f"Ctrl+N did not open a new host session: {rows!r}"
        print("logs/reasoning/session: Ctrl+E open/close, Ctrl+T opened effort picker and selected medium, Ctrl+O listed sessions, Ctrl+N opened a new host session")
    finally:
        page.close()
        _stop(server)


def _check_narrow_resize(playwright, browser: Browser, command_log: Path) -> None:
    server = _start_acceptance(command_log)
    page = browser.new_page(viewport={"width": 900, "height": 900}, device_scale_factor=1)
    try:
        _wait_ready(page, PORT, server)
        page.get_by_role("textbox", name="Terminal input").click()
        page.keyboard.press("Control+e")
        rows = _wait_acceptance_rows(
            command_log,
            lambda current: any(row["command"] == "LogsRead" for row in current),
        )
        assert any(row["command"] == "LogsRead" for row in rows)
        _shot(page, "functional-logs-wide.png")
        wide = page.locator(".xterm-screen").first.bounding_box()
        assert wide is not None, "xterm screen missing before resize"
        before_resize = sum(
            row["command"] == "LogsRead" for row in _read_acceptance_log(command_log)
        )
        page.set_viewport_size({"width": 620, "height": 760})
        page.wait_for_timeout(800)
        _shot(page, "functional-logs-narrow.png")
        screen = page.locator(".xterm-screen").first.bounding_box()
        assert screen is not None and screen["width"] < wide["width"], (
            f"xterm terminal surface did not narrow with browser viewport: {wide} -> {screen}"
        )
        after_resize_rows = _wait_acceptance_rows(
            command_log,
            lambda current: sum(row["command"] == "LogsRead" for row in current)
            > before_resize,
            timeout=5,
        )
        after_resize = sum(row["command"] == "LogsRead" for row in after_resize_rows)
        assert after_resize > before_resize, (
            f"narrow resized app did not poll the open Logs drawer within 5s: "
            f"{before_resize} -> {after_resize}"
        )
        print("resize: viewport narrowed 900 -> 620 px with Logs open; real terminal remained responsive")
    finally:
        page.close()
        _stop(server)


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            checks = (
                ("transcript", lambda: _check_transcript(playwright, browser)),
                ("reference terminal", lambda: _check_reference_terminal(playwright, browser)),
                ("multiline Enter", lambda: _check_multiline_enter(
                    playwright, browser, ARTIFACTS / "submitted.jsonl"
                )),
                ("command palette", lambda: _check_commands_shortcuts(playwright, browser)),
                ("slash suggestions keyboard", lambda: _check_slash_suggestions_keyboard(
                    playwright, browser, ARTIFACTS / "acceptance-slash-suggestions.jsonl"
                )),
                ("slash /n immediate completion", lambda: _check_slash_new_visible(
                    playwright, browser, ARTIFACTS / "acceptance-slash-new.jsonl"
                )),
                ("model picker keyboard", lambda: _check_model_picker_keyboard(
                    playwright, browser, ARTIFACTS / "acceptance-model-keyboard.jsonl"
                )),
                ("model picker mouse", lambda: _check_model_picker_mouse(
                    playwright, browser, ARTIFACTS / "acceptance-model-mouse.jsonl"
                )),
                ("agent picker command", lambda: _check_agent_picker_command(
                    playwright, browser, ARTIFACTS / "acceptance-agent-picker.jsonl"
                )),
                ("model picker rejection", lambda: _check_model_picker_rejection(
                    playwright, browser, ARTIFACTS / "acceptance-model-rejection.jsonl"
                )),
                ("running turn draft", lambda: _check_running_turn_draft(
                    playwright, browser, ARTIFACTS / "acceptance-running-turn.jsonl"
                )),
                ("logs, reasoning, session navigation", lambda: _check_logs_reasoning_and_sessions(
                    playwright, browser, ARTIFACTS / "acceptance-commands.jsonl"
                )),
                ("narrow resize", lambda: _check_narrow_resize(
                    playwright, browser, ARTIFACTS / "acceptance-resize.jsonl"
                )),
            )
            failures: list[tuple[str, Exception]] = []
            for name, check in checks:
                try:
                    check()
                except Exception as exc:  # noqa: BLE001 - report each independent journey failure
                    failures.append((name, exc))
                    print(f"FAIL {name}: {exc!r}")
            if failures:
                summary = "; ".join(f"{name}: {exc!r}" for name, exc in failures)
                raise AssertionError(f"{len(failures)} browser acceptance check(s) failed: {summary}")
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"playwright TUI check failed: {exc!r}") from exc
