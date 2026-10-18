"""Native splash in a real controlling PTY: wordmark and bar first, then the chat; typing works after the wipe.

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

from pty_cleanup import kill_group

from ratatui_screen import Screen

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"
SETUP = (
    "import os,fcntl,termios; os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); "
    "import subprocess; raise SystemExit(subprocess.run([%r]).returncode)" % str(BINARY)
)


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_splash_shows_wordmark_and_bar_then_wipes_into_the_chat(monkeypatch):
    monkeypatch.delenv("NEXUS_SPLASH", raising=False)  # the suite default skips the motion
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (30, 120))
    screen = Screen(30, 120)
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            try:
                if select.select([master], [], [], .05)[0]:
                    screen.feed(os.read(master, 65536))
            except OSError:
                return

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    process = subprocess.Popen([sys.executable, "-c", SETUP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave)

    def wait_for(text, timeout=3):
        deadline = time.monotonic() + timeout
        while text not in screen:
            assert time.monotonic() < deadline, f"{text!r} never appeared; screen:\n{screen.text()}"
            time.sleep(.02)

    try:
        # No snapshot yet: the loading bar answers the launch (the particles have not converged).
        wait_for("20%")
        assert "Splash title" not in screen
        # Regression: the bar sat at 20% until the first snapshot. Host progress lines move it
        # without being parsed as snapshots (no chat, no error).
        process.stdin.write(b'{"startup":2}\n')
        process.stdin.flush()
        wait_for("40%")
        process.stdin.write(b'{"startup":3}\n')
        process.stdin.flush()
        wait_for("60%")
        assert "Splash title" not in screen and process.poll() is None
        process.stdin.write((json.dumps({"schema": 1, "revision": 1, "status": "idle", "title": "Splash title"}) + "\n").encode())
        process.stdin.flush()
        # Bar completes, the particles settle into the wordmark, it flashes, the curtain wipes the chat in.
        wait_for("100%", timeout=5)
        wait_for("█▀▀")
        wait_for("Splash title", timeout=8)
        # Regression: input used to stay held for seconds after the chat appeared; once the
        # wipe has finished typed text must reach the composer.
        time.sleep(.6)
        os.write(master, b"after the wipe")
        wait_for("after the wipe", timeout=3)
        os.write(master, b"\x11")
        assert process.wait(timeout=5) == 0
    finally:
        kill_group(process)
        process.stdin.close()
        process.stdout.close()
        stop.set()
        reader.join(timeout=.5)
        os.close(master)
        os.close(slave)
