"""Native rendering of an external Claude Code worker page in a real controlling PTY.

Build rust/tui first. The snapshot is the real Python projection of replayed
``external.*`` events, so Python rows and Rust drawing are checked together.
"""
import json
import os
import pty
import re
import select
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pty_cleanup import kill_group

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import fold
from test_ratatui_external import CHILD, MANUAL, _events

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"
SETUP = (
    "import os,fcntl,termios; os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); "
    "import subprocess; raise SystemExit(subprocess.run([%r]).returncode)" % str(BINARY)
)


def _snapshot(tmp_path, monkeypatch, status, attention):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    controller = SimpleNamespace(view=fold(_events(status, attention)), session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    shell.workflows.agent_page_id = CHILD
    shell.workflows.agent_context = {}
    return project(controller, 1, shell=shell, literal=False)


def _screen_of(snapshot, wanted):
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (60, 140))
    screen = bytearray()
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            try:
                if select.select([master], [], [], .05)[0]:
                    screen.extend(os.read(master, 65536))
            except OSError:
                return

    threading.Thread(target=drain, daemon=True).start()
    process = subprocess.Popen([sys.executable, "-c", SETUP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave)
    try:
        process.stdin.write((json.dumps({**snapshot, "schema": 1}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 5
        def plain():  # styled label/value spans are separate escape runs
            return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", bytes(screen).decode(errors="replace"))

        while not all(text in plain() for text in wanted):
            assert time.monotonic() < deadline, f"missing: {[t for t in wanted if t not in plain()]}"
            time.sleep(.05)
        return plain()
    finally:
        stop.set()
        kill_group(process)
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_native_draws_authority_tool_provenance_status_and_unknown_usage(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path, monkeypatch, "completed", None)
    _screen_of(snapshot, ["Claude Code worker", "Permission mode: dontAsk", "Authority:", "did not mediate",
                          "Claude Code completed", "Usage: unknown", "Transcript:"])


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_native_draws_needs_attention_with_the_manual_instruction(tmp_path, monkeypatch):
    snapshot = _snapshot(tmp_path, monkeypatch, "needs_attention", MANUAL)
    _screen_of(snapshot, ["NEEDS ATTENTION", "Needs attention:", "folder-trust prompt"])
