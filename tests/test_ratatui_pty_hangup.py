"""The native client exits when its terminal goes away instead of spinning (idle-cost section of
docs/ratatui-parity.md). Closing the PTY master hangs up the slave: crossterm's `poll` then
reports ready forever, which once left a debug client at 100% CPU for 19.5 hours."""
import os
import pty
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from pty_cleanup import kill_group

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_client_exits_quickly_when_the_terminal_is_lost(tmp_path):
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (30, 100))
    # The hangup also sends SIGHUP to the session; the real orphan survived it (ignored, and
    # the disposition survives exec), so reproduce that.
    setup = ("import os,fcntl,termios,signal; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
             "os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); "
             "os.execv(%r,[%r])" % (str(BINARY), str(BINARY)))
    env = {**os.environ, "NEXUS_HOME": str(tmp_path)}
    process = subprocess.Popen([sys.executable, "-c", setup], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=slave, env=env)
    try:
        time.sleep(4.0)  # raw mode entered, splash over, idle loop running
        assert process.poll() is None, "client must stay alive while the terminal exists"
        os.close(master)
        master = -1
        started = time.monotonic()
        cpu_before = os.times()
        process.wait(timeout=4)
        assert time.monotonic() - started < 3
        log = (tmp_path / "tui-panic.log").read_text()
        assert "terminal-lost" in log
        assert os.times().children_user + os.times().children_system - cpu_before.children_user - cpu_before.children_system < 1.5
    finally:
        kill_group(process)
        process.stdin.close()
        process.stdout.close()
        if master >= 0:
            os.close(master)
        os.close(slave)
