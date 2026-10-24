"""The pinned Manager entry in a real PTY (plan 02, docs/manager.md).

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
ROOTS = [
    {"id": "root", "title": "Fix the flaky sync test", "workspace": "/w", "status": "idle",
     "bucket": "Today", "updated": 2, "age": "2m"},
    {"id": "other", "title": "Audit licences", "workspace": "/w", "status": "working",
     "bucket": "Today", "updated": 1, "age": "now"},
]
READY = {
    "known": True, "enabled": True, "active": False, "session": "nexus-manager", "status": "idle",
    "tone": "ok", "model": "codex/gpt-6-luna", "summary": "Ready · 2 sessions launched",
    "live": "Voice ready", "live_ready": True, "managed": 2, "can_open": True,
    "privacy": "Voice calls also share other sessions' titles, status and reports with OpenAI",
}
OFF = {
    "known": True, "enabled": False, "active": False, "session": "", "status": "idle", "tone": "off",
    "model": "codex/gpt-6-luna", "summary": "Off · [manager] enabled = false",
    "notes": ["Turn it on with [manager] enabled = true in nexus.toml, then restart the daemon."],
}


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
        time.sleep(.5)

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


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_pinned_manager_is_drawn_apart_from_the_roots_and_opens_by_key():
    t = Terminal(36, 120)
    try:
        t.send(dashboard={"rows": ROOTS, "query": "", "selected": "root", "manager": READY}, generation=1)
        time.sleep(.4)
        t.redraw()
        text = t.text()
        assert "Manager" in text and "pinned" in text, text
        assert "Ready · 2 sessions launched" in text
        assert "codex/gpt-6-luna" in text and "Voice ready" in text, "model and Live readiness"
        assert "share other sessions' titles" in text, "the cross-session privacy line"
        assert "Voice M" in text
        # Pinned above, and separate from, the ordinary roots.
        assert text.index("Manager") < text.index("Fix the flaky sync test")
        t.key(b"m")
        assert t.action() == {"type": "operation", "operation": {"kind": "manager_open"}, "generation": 1}
        t.key(b"M")
        assert t.action() == {"type": "operation", "operation": {"kind": "manager_live"}, "generation": 1}
        t.key(b"\x11")
        assert t.action()["type"] == "quit"
    finally:
        t.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_disabled_manager_is_visible_with_the_fix_and_still_answers():
    t = Terminal(36, 120)
    try:
        t.send(dashboard={"rows": ROOTS, "query": "", "selected": "root", "manager": OFF}, generation=1)
        time.sleep(.4)
        t.redraw()
        text = t.text()
        assert "Manager" in text and "Off · [manager] enabled = false" in text, text
        assert "enabled = true in nexus.toml" in text
        assert "Voice M" not in text, "no voice while it is off"
        t.key(b"m")  # opening explains why it is off
        assert t.action()["operation"] == {"kind": "manager_open"}
    finally:
        t.close()
