"""The top bar replaces the removed sessions sidebar: dashboard button and session arrows in a real PTY.

Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import json
import os
import re
from pathlib import Path
import pty
import select
import subprocess
import sys
import termios
import threading
import time

import pytest

from pty_cleanup import kill_group

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"
SETUP = (
    "import os,fcntl,termios; os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); "
    "import subprocess; raise SystemExit(subprocess.run([%r]).returncode)" % str(BINARY)
)
SESSIONS = [
    {"id": "s1", "title": "Fix token refresh", "workspace": "/w", "state": "idle", "status": "idle", "sub": "build · idle", "group": "~/w", "active": True},
    {"id": "s2", "title": "Refresh docs for models", "workspace": "/w", "state": "idle", "status": "done", "sub": "finished", "group": "~/w"},
    {"id": "s3", "title": "Audit licences", "workspace": "/w2", "state": "idle", "status": "idle", "sub": "build · idle", "group": "~/w2"},
]


class Terminal:
    def __init__(self, rows, cols):
        self.master, self.slave = pty.openpty()
        termios.tcsetwinsize(self.slave, (rows, cols))
        self.rows, self.cols = rows, cols
        self.screen = bytearray()
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self._drain, daemon=True)
        self.reader.start()
        self.process = subprocess.Popen([sys.executable, "-c", SETUP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.slave,
                                        env={**os.environ, "NEXUS_IMAGE_PROTOCOL": "halfblocks"})  # no terminal to answer the graphics probe
        self.pending = bytearray()
        self.revision = 0

    def _drain(self):
        while not self.stop.is_set():
            try:
                if select.select([self.master], [], [], .05)[0]:
                    self.screen.extend(os.read(self.master, 65536))
            except OSError:
                return

    def send(self, **fields):
        self.revision += 1
        self.process.stdin.write((json.dumps({"schema": 1, "revision": self.revision, "status": "idle", **fields}) + "\n").encode())
        self.process.stdin.flush()

    def key(self, data: bytes):
        os.write(self.master, data)
        time.sleep(.15)

    def action(self):
        deadline = time.monotonic() + 3
        while b"\n" not in self.pending:
            assert select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))[0], "no action"
            self.pending.extend(os.read(self.process.stdout.fileno(), 65536))
        line, _, rest = self.pending.partition(b"\n")
        self.pending[:] = rest
        row = json.loads(line)
        # Composer persistence reports are passive, not user actions.
        return self.action() if row.get("type") == "draft_state" else row

    def redraw(self):
        """A resize repaints every cell, so what we then read is the whole screen."""
        self.screen.clear()
        self.rows += 1
        termios.tcsetwinsize(self.slave, (self.rows, self.cols))
        # The setup process owns the controlling terminal; explicitly notify the
        # native child too instead of relying on the foreground process group.
        import signal
        os.killpg(self.process.pid, signal.SIGWINCH)
        deadline = time.monotonic() + 3
        while not self.screen and time.monotonic() < deadline:
            time.sleep(.02)
        time.sleep(.1)

    def text(self):
        raw = bytes(self.screen).decode("utf-8", "replace")
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)

    def close(self):
        kill_group(self.process)
        self.process.stdin.close()
        self.process.stdout.close()
        self.stop.set()
        self.reader.join(timeout=.5)
        os.close(self.master)
        os.close(self.slave)


def click(t, column, row):
    """A left click at 1-based terminal coordinates (SGR mouse)."""
    t.key(f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode())


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_there_is_no_sessions_sidebar_and_the_top_bar_opens_the_dashboard():
    t = Terminal(30, 120)
    try:
        t.send(sessions_sidebar=True, sessions=SESSIONS, tabs=SESSIONS[:2], title="Fix token refresh",
               breadcrumb="~/w \u203a main", composer_key="w/s1", generation=1)
        time.sleep(.4)
        t.redraw()
        text = t.text()
        # The second tab is deliberately a session named "Refresh docs for models"; it is
        # expected in the top bar, but should not reappear in the removed sidebar.
        assert "SESSIONS" not in text and "Filter sessions" not in text
        assert "Fix token refresh" in text and "▤" in text and "▥" in text and "1/2" not in text, "top bar"
        # Project strip contract: basename, branch, full workspace path.
        assert text.index("main") < text.index("~/w"), "branch comes before the full path"
        click(t, 113, 2)  # inside the session-list icon's padded hit target
        assert t.action() == {"type": "command", "text": "/sessions"}
        t.key(b"\x02")  # Ctrl+B is the dashboard too
        assert t.action() == {"type": "command", "text": "/sessions"}
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()

@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_ownership_tree_shortcut_and_read_only_node_menu():
    t = Terminal(30, 120)
    try:
        t.send(dashboard={"rows": [{"id": "root", "title": "Root", "workspace": "/w",
                                   "status": "idle", "bucket": "Today", "updated": 1}],
                          "query": "", "loaded": 1, "total": 1}, generation=1)
        time.sleep(.3)
        t.key(b"t")
        assert t.action() == {"type": "operation", "operation": {"kind": "ownership_tree", "id": "root"}, "generation": 1}
        child = {"kind": "ownership_node", "id": "child", "namespace": "agents", "facts": ["agents/child", "interrupted"]}
        t.send(dashboard=None, panel_title="Session ownership",
               panel_lines=["Ownership (not fork ancestry) · read only"], panel_layout="list",
               items=[{"label": "Root · idle", "operation": {"kind": "ownership_node", "id": "root", "namespace": "main", "facts": []}},
                     {"label": "  task · interrupted", "operation": child}], generation=2)
        time.sleep(.3)
        assert "Session ownership" in t.text()
        assert "interrupted" in t.text()
        t.key(b"\x1b[B\r")
        assert t.action() == {"type": "operation", "operation": child, "generation": 2}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_child_back_rail_and_vertical_tab_actions():
    t = Terminal(30, 140)
    try:
        t.send(agent_page="child", title="Child task", sessions_sidebar=True,
               tabs=SESSIONS[:2], tabs_vertical=True, generation=1)
        time.sleep(.4)
        click(t, 1, 10)
        assert t.action() == {"type": "dismiss", "text": ""}
        t.send(agent_page="", title="Root task", sessions_sidebar=False,
               tabs=SESSIONS[:2], tabs_vertical=True, generation=2)
        time.sleep(.4)
        click(t, 5, 3)
        assert t.action() == {"type": "session_open", "workspace": "/w", "text": "s2"}
        click(t, 22, 3)
        assert t.action() == {"type": "tab_close", "workspace": "/w", "text": "s2"}
        click(t, 5, 1)
        assert t.action() == {"type": "command", "text": "/new"}
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_vertical_tab_overflow_wheel_and_click_share_viewport():
    t = Terminal(30, 140)
    try:
        tabs = [dict(SESSIONS[0], id=f"s{i}", title=f"Tab {i}", active=i == 0)
                for i in range(40)]
        t.send(sessions_sidebar=False, tabs=tabs, tabs_vertical=True, generation=1)
        time.sleep(.4)
        # Wheel over the rail must move its viewport, not the conversation.
        t.key(b"\x1b[<65;5;3M")
        time.sleep(.2)
        click(t, 5, 2)
        assert t.action() == {"type": "session_open", "workspace": "/w", "text": "s3"}
        t.key(b"\x1b[<64;5;3M")
        time.sleep(.2)
        click(t, 5, 2)
        assert t.action() == {"type": "session_open", "workspace": "/w", "text": "s0"}
    finally:
        t.close()


@pytest.mark.parametrize("key", [b"x", b"\x1b[3~"])
@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_focused_notice_keyboard_dismissal(key):
    t = Terminal(30, 140)
    try:
        t.send(blocks=[{"kind": "notice", "id": "notice-1", "title": "Information",
                        "text": "Checkout kept", "operation": {"kind": "dismiss_notice", "id": "s:1"}}],
               generation=1)
        time.sleep(.4)
        t.key(b"\t")
        t.key(key)
        assert t.action() == {"type": "operation", "operation": {"kind": "dismiss_notice", "id": "s:1"}, "generation": 1}
    finally:
        t.close()


@pytest.mark.parametrize("columns", [24, 40, 140])
@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_child_has_one_back_affordance_and_complete_narrow_hint(columns):
    t = Terminal(30, columns)
    try:
        t.send(agent_page="child", title="Child task", generation=1)
        time.sleep(1.5)
        t.redraw()
        text = t.text()
        assert text.count("←") == 1
        assert ("Esc: back" if columns == 24 else "Esc/↑ back" if columns == 40
                else "Esc or ↑ returns to the parent") in text
        t.key(b"\x1b")
        assert t.action() == {"type": "dismiss", "text": ""}
    finally:
        t.close()
