"""The sessions sidebar is keyboard-operable in a real controlling PTY (docked and drawer).

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
        self.process = subprocess.Popen([sys.executable, "-c", SETUP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.slave)
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
        return json.loads(line)

    def redraw(self):
        """A resize repaints every cell, so what we then read is the whole screen."""
        self.screen.clear()
        self.rows += 1
        termios.tcsetwinsize(self.slave, (self.rows, self.cols))
        time.sleep(.5)

    def text(self):
        raw = bytes(self.screen).decode("utf-8", "replace")
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.stop.set()
        self.reader.join(timeout=.5)
        os.close(self.master)
        os.close(self.slave)


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_docked_sidebar_keyboard_navigation_opens_a_session():
    t = Terminal(30, 120)
    try:
        t.send(sessions_sidebar=True, sessions=SESSIONS, composer_key="w/s1", generation=1)
        time.sleep(.4)
        t.redraw()
        assert "Fix token refresh" in t.text() and "Filter sessions" in t.text()
        t.key(b"\x02")  # Ctrl+B: the sidebar is open, so this moves focus into it
        t.redraw()
        assert "type to filter" in t.text(), "focus is visible in the sidebar"
        t.key(b"\x1b[B")  # Down: s1 is current and selected first
        t.key(b"\r")
        assert t.action() == {"type": "session_open", "workspace": "/w", "text": "s2", "generation": 1}
        # Typing filters; the first match is selected; Escape clears and then leaves.
        t.key(b"\x02")
        t.key(b"lic")
        t.key(b"\r")
        assert t.action() == {"type": "session_open", "workspace": "/w2", "text": "s3", "generation": 1}
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_narrow_terminal_opens_a_drawer_only_on_request():
    t = Terminal(24, 70)
    try:
        t.send(sessions_sidebar=True, sessions=SESSIONS, composer_key="w/s1", generation=1)
        time.sleep(.4)
        t.redraw()
        assert "Fix token refresh" not in t.text(), "the saved preference alone never opens a drawer"
        t.key(b"\x02")
        t.redraw()
        assert "Fix token refresh" in t.text() and "Refresh docs" in t.text(), "Ctrl+B opens the drawer"
        t.key(b"\x1b[B")
        t.key(b"\x1b[B")
        t.key(b"\r")
        assert t.action() == {"type": "session_open", "workspace": "/w2", "text": "s3", "generation": 1}
        t.redraw()
        assert "Audit licences" not in t.text(), "choosing a session closes the drawer"
        # /sessions from Python (a bumped request) opens it again, without touching the preference.
        t.send(sessions_sidebar=True, sessions=SESSIONS, sessions_request=1, composer_key="w/s1", generation=1)
        time.sleep(.4)
        t.redraw()
        assert "Fix token refresh" in t.text()
        t.key(b"\x1b")
        t.redraw()
        assert "Fix token refresh" not in t.text(), "Escape closes the drawer"
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()
