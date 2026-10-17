"""End to end: the real Python bridge loop + the real native binary + a real host, driven by keystrokes.

No hand-built snapshots: Settings is opened and operated exactly as a user does, so a mismatch between
what the host offers and what the bridge accepts (the tier tabs once did nothing) fails here.
Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import os
import re
import select
import subprocess
import sys
import termios
import textwrap
import threading
import time
import pty
from pathlib import Path

import pytest

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"
ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent('''
    import asyncio, sys
    from pathlib import Path
    from nexus.client.protocol import Client
    from nexus.config import Config
    from nexus.config.schema import AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime
    from nexus.ui.ratatui.run import run

    class FacadeTransport:
        def __init__(self, facade): self.facade = facade
        async def request(self, command): return await self.facade.handle(command)
        def events(self, session, from_seq=0, *, follow=True, client_id=None):
            return self.facade.subscribe(session, from_seq, follow=follow, client_id=client_id)
        async def aclose(self): pass

    async def main():
        workspace = Path(sys.argv[1])
        config = Config(model="scripted/native", version=2, v2=ConfigV2(
            model=ModelSection(default="scripted/native"), agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"), tools=ToolsSection()))
        runtime = Runtime(workspace, config=config, providers={"scripted": ScriptedProvider(text_response("ok"))})
        client = Client(FacadeTransport(HostFacade(runtime)))
        await run(client, session="e2e", workspace=workspace)

    asyncio.run(main())
''')


class App:
    def __init__(self, tmp_path, rows=44, cols=140):
        env = {**os.environ, "NEXUS_TUI_BINARY": str(BINARY), "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
               "XDG_STATE_HOME": str(tmp_path / "state"), "PYTHONPATH": str(ROOT), "NEXUS_COMPLETION_SOUNDS": "off", "NO_COLOR": ""}
        env.pop("NO_COLOR")
        self.master, self.slave = pty.openpty()
        termios.tcsetwinsize(self.slave, (rows, cols))
        self.rows, self.cols = rows, cols
        self.screen = bytearray()
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()
        script = tmp_path / "child.py"
        script.write_text(CHILD)
        setup = ("import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); "
                 "os.execv(sys.executable,[sys.executable,%r,%r])" % (str(script), str(tmp_path)))
        self.process = subprocess.Popen([sys.executable, "-c", setup], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=self.slave, env=env, cwd=str(tmp_path))

    def _drain(self):
        while not self.stop.is_set():
            try:
                if select.select([self.master], [], [], .05)[0]:
                    self.screen.extend(os.read(self.master, 65536))
            except OSError:
                return

    def key(self, data: bytes, wait=.25):
        os.write(self.master, data)
        time.sleep(wait)

    def text(self):
        """The screen as a terminal would show it, one string per row (a small ANSI emulator)."""
        import unicodedata
        data = bytes(self.screen).decode("utf-8", "replace")
        grid = [[" "] * self.cols for _ in range(self.rows)]
        r = c = 0
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b" and data[i + 1:i + 2] == "[":
                m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", data[i:])
                if not m:
                    i += 1
                    continue
                args, final = m.group(1), m.group(2)
                nums = [int(n) if n.isdigit() else 0 for n in args.lstrip("?").split(";")] if args else []
                if final in "Hf":
                    r = (nums[0] if nums and nums[0] else 1) - 1
                    c = (nums[1] if len(nums) > 1 and nums[1] else 1) - 1
                elif final == "J" and nums[:1] in ([2], [3]):
                    grid = [[" "] * self.cols for _ in range(self.rows)]
                elif final == "K":
                    for x in range(c, self.cols):
                        grid[min(r, self.rows - 1)][x] = " "
                elif final == "C":
                    c += nums[0] if nums and nums[0] else 1
                elif final == "D":
                    c -= nums[0] if nums and nums[0] else 1
                elif final == "A":
                    r -= nums[0] if nums and nums[0] else 1
                elif final == "B":
                    r += nums[0] if nums and nums[0] else 1
                i += len(m.group(0))
                continue
            if ch == "\x1b":
                i += 2
                continue
            if ch == "\r":
                c = 0
            elif ch == "\n":
                r += 1
            elif ch >= " ":
                if 0 <= r < self.rows and 0 <= c < self.cols:
                    grid[r][c] = ch
                c += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            i += 1
        return "\n".join("".join(row).rstrip() for row in grid)

    def redraw(self, wait=.7):
        """A resize repaints every cell, so the text read next is the whole screen."""
        self.screen.clear()
        self.rows += 1
        termios.tcsetwinsize(self.slave, (self.rows, self.cols))
        time.sleep(wait)
        return self.text()

    def wait_for(self, needle, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.redraw(.5):
                return True
        raise AssertionError(f"{needle!r} never appeared:\n{self.text()[-2500:]}")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.stop.set()
        self.reader.join(timeout=.5)
        os.close(self.master)
        os.close(self.slave)


@pytest.fixture
def app(tmp_path):
    if not BINARY.exists():
        pytest.skip("build the native prototype first")
    a = App(tmp_path)
    try:
        yield a
    finally:
        a.close()


def open_settings(app, area_key: bytes, expect: str):
    time.sleep(2.0)  # the bridge and the daemon-less host start up
    app.key(b"\x18s", .8)  # Ctrl+X S: Settings (opens on Appearance)
    app.wait_for("Appearance")
    app.key(area_key, .8)  # Alt+N jumps to the N-th area
    app.wait_for(expect)


def ordered_rows(screen: str):
    # A trailing ┃/│ is the scrollbar thumb, which moves with the page height.
    return [" ".join(line.split()).rstrip("┃│ ") for line in screen.splitlines() if "⋮⋮" in line]


def test_opening_models_shows_the_page_from_the_real_host(app):
    open_settings(app, b"\x1b5", "Default Model")  # Alt+5: Models
    screen = app.redraw()
    for want in ["Subagent Tiers", "Low", "Session Titles", "Name new sessions"]:
        assert want in screen, f"{want!r} missing:\n{screen}"
    assert "attribute" not in screen and "Error" not in screen, "no error toast"


def test_all_tiers_are_reachable_by_scrolling_through_the_real_bridge(app):
    open_settings(app, b"\x1b5", "Default Model")
    seen = app.redraw()
    assert "Low" in seen
    for _ in range(60):
        app.key(b"\x1b[<65;80;22M", .03)
        seen += app.redraw()
    assert "Medium" in seen and "High" in seen
    assert "Default" in seen and "Highest tier for subagents" not in seen


def _toggles(screen):
    import re
    rows = [line for line in screen.splitlines() if "▸ " in line]
    on = sum(bool(re.search(r"▸ .*\bon +[│┃]", line)) for line in rows)
    off = sum(bool(re.search(r"▸ .*\boff +[│┃]", line)) for line in rows)
    return on, off


def test_a_toggle_saves_through_the_real_bridge_and_the_page_shows_it(app):
    open_settings(app, b"\x1b2", "Show context header")  # Alt+2: Layout
    first = app.redraw()
    assert _toggles(first) == (2, 1)  # Centered conversation is off by default
    app.key(b" ", 1.0)  # Space on the first row: Details sidebar off
    after = app.redraw()
    assert _toggles(after) == (1, 2), "the host applied it and rebuilt the page"


def test_the_wheel_scrolls_the_keyboard_page_past_its_second_section(app):
    """The reported bug: scrolling stopped before the third section, as every frame snapped back to the focused row."""
    open_settings(app, b"\x1b3", "Shortcuts")  # Alt+3: Keyboard
    top = app.redraw()
    assert "In Lists and Settings" not in top and "Jump to an area" not in top
    for _ in range(40):
        app.key(b"\x1b[<65;80;22M", .03)  # mouse wheel down over the page
    time.sleep(.5)
    bottom = app.redraw()
    for want in ["In Lists and Settings", "Jump to an area", "Show this page", "Read-only"]:
        assert want in bottom, f"{want!r} missing after scrolling:\n{bottom}"
    for _ in range(80):
        app.key(b"\x1b[<64;80;22M", .02)  # and back up
    assert "Shortcuts" in app.redraw()


def _models_focus(screen):
    """What the focused Models stop shows: the ✕ row of a focused list item, or a focused row's description.

    The focused stop is otherwise only a background fill, so these texts are the visible evidence of where focus is.
    """
    item = next((" ".join(line.split()) for line in screen.splitlines() if "✕" in line), "")
    item = re.sub(r"\s*[↑↓]?\s*[↑↓]?\s*✕.*$", "", item).rstrip("┃│ ")
    # The title model's description is shown whether or not it is focused (and depends on ambient config), so it is not a focus signal.
    return (item, "Your first message is sent" in screen, "models known." in screen)


def test_models_down_and_up_visit_each_stop_once_and_clamp_at_both_ends(app):
    """Reported: Up/Down looped through entries. Walking the real page down then up must end at the ends and stay there."""
    open_settings(app, b"\x1b5", "Default Model")
    app.key(b"\x1b[C", .2)  # Right: focus moves from the area list into the page
    for _ in range(40):
        app.key(b"\x1b[A", .02)  # already at, or clamped to, the first stop
    top = _models_focus(app.redraw(.4))
    assert not top[2], "the last stop (Catalogue) must not be focused at the top"

    down = [top]
    for _ in range(80):
        app.key(b"\x1b[B", .08)
        down.append(_models_focus(app.redraw(.3)))
        if down[-1][2]:
            break
    assert down[-1][2], f"Down never reached the Catalogue row: {down}"
    last = len(down) - 1
    assert last > 3, f"Down walked only {last} stops: {down}"
    assert sum(s[1] for s in down) == 1, f"the titles row is a stop once: {down}"
    assert not down[last][0], "no list item keeps focus at the end"

    for _ in range(25):  # past the end: no wrap to the first stop
        app.key(b"\x1b[B", .02)
    assert _models_focus(app.redraw(.4)) == down[last], "Down at the last stop stays on it"

    up = [down[last]]
    for _ in range(last):
        app.key(b"\x1b[A", .08)
        up.append(_models_focus(app.redraw(.3)))
    assert up == down[::-1], f"Up retraces the same stops in reverse:\n up={up}\n down={down}"

    for _ in range(25):  # past the start: no wrap to the Catalogue row
        app.key(b"\x1b[A", .02)
    assert _models_focus(app.redraw(.4)) == top, "Up at the first stop stays on it"


def test_the_keyboard_page_also_scrolls_with_the_arrow_keys(app):
    open_settings(app, b"\x1b3", "Shortcuts")
    app.key(b"\x1b[C", .2)  # Right: focus moves from the area list into the page
    for _ in range(90):
        app.key(b"\x1b[B", .02)
    bottom = app.redraw()
    assert "Jump to an area" in bottom and "Show this page" in bottom, bottom
