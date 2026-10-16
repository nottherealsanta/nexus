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
        assert "SESSIONS" not in text and "Filter sessions" not in text and "Refresh docs" not in text
        assert "Fix token refresh" in text and "☰" in text and "◫" in text and "1/2" not in text, "top bar"
        assert text.index("~/w") < text.index("main"), "path comes before the branch"
        click(t, 113, 2)  # inside the session-list icon's padded hit target
        assert t.action() == {"type": "command", "text": "/sessions"}
        t.key(b"\x02")  # Ctrl+B is the dashboard too
        assert t.action() == {"type": "command", "text": "/sessions"}
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()
