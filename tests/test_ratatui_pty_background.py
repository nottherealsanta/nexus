"""Background subagents in a real controlling PTY (plans/done/NONBLOCKING_SUBAGENTS_PLAN.md phase 7).

Feeds the native binary the snapshots the Python projection builds and reads its screen: the
"N agents running" status row while the root is idle, an idle child's state on its card, a
labelled subagent-report row, and a stopped-by-system record. Build rust/tui first; needs
controlling-TTY access.
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
def test_idle_root_shows_running_agents_and_labelled_child_and_report_rows():
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (34, 120))
    screen = Screen(34, 120)
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

    def send(revision, **fields):
        process.stdin.write((json.dumps({"schema": 1, "revision": revision, "status": "idle", **fields}) + "\n").encode())
        process.stdin.flush()

    def wait_for(text):
        deadline = time.monotonic() + 3
        while text not in screen:
            assert time.monotonic() < deadline, f"{text!r} never appeared; screen:\n{screen.text()}"
            time.sleep(.02)

    task = {"id": "c1", "kind": "task", "title": "Explorer Subagent — find x", "agent_name": "explorer", "task": "find x",
            "status": "done", "state": "idle · run 2", "model": "gpt-6-mini", "tool_calls": 3}
    report = {"id": "m1:user", "kind": "user", "title": "report · s/child/1 · explorer · run 2 · completed",
              "text": "Found x in a.py:3", "status": "report", "number": 0}
    stopped = {**task, "id": "c2", "state": "stopped by system: evicted", "task": "find y"}
    try:
        send(1, blocks=[task, report, stopped], agents_running=2)
        wait_for("2 agents running")
        wait_for("/tasks to open or stop")
        wait_for("idle · run 2")
        wait_for("report · s/child/1 · explorer · run 2 · completed")
        wait_for("subagent report")
        wait_for("Found x in a.py:3")
        wait_for("stopped by system: evicted")
        os.write(master, b"\x11")
        deadline = time.monotonic() + 3
        assert process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0
    finally:
        kill_group(process)
        process.stdin.close()
        process.stdout.close()
        stop.set()
        reader.join(timeout=.5)
        os.close(master)
        os.close(slave)
