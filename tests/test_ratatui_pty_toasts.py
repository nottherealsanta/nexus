"""Native toasts in a real controlling PTY: they appear, run their action, and dismiss.

Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import json
import os
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


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_toast_shows_runs_its_action_and_closes_by_click_or_chord():
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (30, 120))
    screen = bytearray()
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            try:
                if select.select([master], [], [], .05)[0]:
                    screen.extend(os.read(master, 65536))
            except OSError:
                return

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    process = subprocess.Popen([sys.executable, "-c", SETUP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave)
    pending = bytearray()

    def read_action():
        deadline = time.monotonic() + 3
        while b"\n" not in pending:
            assert select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0], "no action"
            pending.extend(os.read(process.stdout.fileno(), 65536))
        line, _, rest = pending.partition(b"\n")
        pending[:] = rest
        return json.loads(line)

    def send(revision, **fields):
        process.stdin.write((json.dumps({"schema": 1, "revision": revision, "status": "idle", **fields}) + "\n").encode())
        process.stdin.flush()

    rows = [30]

    def full_redraw():
        """A resize repaints every cell, so absence afterwards is a real absence."""
        screen.clear()
        rows[0] += 1
        termios.tcsetwinsize(slave, (rows[0], 120))
        time.sleep(.5)

    def wait_for(text, present=True):
        deadline = time.monotonic() + 3
        while (text.encode() in bytes(screen)) != present:
            assert time.monotonic() < deadline, f"{text!r} {'never appeared' if present else 'never left'}"
            time.sleep(.02)

    try:
        undo = {"kind": "tier_undo"}
        send(1, toasts=[{"id": 7, "level": "warning", "title": "Removed haiku-4-5", "body": "from the Low tier",
                         "action": {"label": "Undo", "operation": undo}}])
        wait_for("Removed haiku-4-5")
        wait_for("from the Low tier")
        # Ctrl+X A runs the newest toast's action through the normal operation path.
        os.write(master, b"\x18a")
        assert read_action() == {"type": "operation", "operation": undo, "generation": 0}
        # The same list in the next snapshot must not bring a dismissed toast back.
        send(2, toasts=[{"id": 7, "level": "warning", "title": "Removed haiku-4-5", "body": "from the Low tier"}])
        full_redraw()
        assert b"Removed haiku-4-5" not in bytes(screen), "an acted-on toast stays dismissed"
        # New toasts: Ctrl+X X dismisses them all.
        send(3, toasts=[{"id": 8, "level": "success", "title": "Saved to nexus.toml"}, {"id": 9, "level": "error", "title": "Could not reach OpenAI"}])
        wait_for("Could not reach OpenAI")
        full_redraw()
        assert b"Could not reach OpenAI" in bytes(screen), "control: a full redraw repaints a live toast"
        os.write(master, b"\x18x")
        time.sleep(.3)
        send(4, toasts=[{"id": 8, "level": "success", "title": "Saved to nexus.toml"}, {"id": 9, "level": "error", "title": "Could not reach OpenAI"}])
        full_redraw()
        assert b"Could not reach OpenAI" not in bytes(screen) and b"Saved to nexus.toml" not in bytes(screen)
        os.write(master, b"\x11")
        assert read_action()["type"] == "quit"
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
        stop.set()
        reader.join(timeout=.5)
        os.close(master)
        os.close(slave)
