"""Native input uses the controlling PTY while stdin carries bridge updates.

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
import time
import threading

import pytest

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_native_bridge_keyboard_and_terminal_restoration():
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 100))
    before = termios.tcgetattr(slave)
    stop_drain = threading.Event()
    def drain_terminal():
        while not stop_drain.is_set():
            try:
                if select.select([master], [], [], .1)[0]:
                    os.read(master, 65536)
            except OSError:
                return
    drain = threading.Thread(target=drain_terminal, daemon=True)
    drain.start()

    # Run setup after exec: preexec_fn can deadlock in threaded test runners.
    setup = (
        "import os,fcntl,termios; os.setsid(); "
        "fcntl.ioctl(2,termios.TIOCSCTTY,0); "
        "import subprocess,json; before=termios.tcgetattr(2); "
        "result=subprocess.run([" + repr(str(BINARY)) + "]); "
        "print(json.dumps({'restored':(termios.tcgetattr(2)[3] & (termios.ICANON|termios.ECHO|termios.ISIG))==(before[3] & (termios.ICANON|termios.ECHO|termios.ISIG))}),flush=True); "
        "raise SystemExit(result.returncode)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", setup], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=slave,
    )
    action_buffer = bytearray()
    def read_action():
        deadline = time.monotonic() + 3
        while b"\n" not in action_buffer:
            assert select.select([process.stdout], [], [], max(0, deadline-time.monotonic()))[0], "native action timed out"
            line = os.read(process.stdout.fileno(), 65536)
            assert line, "native input reader exited"
            action_buffer.extend(line)
        line, _, rest = action_buffer.partition(b"\n")
        action_buffer[:] = rest
        return json.loads(line)

    try:
        process.stdin.write((json.dumps({"schema": 1, "revision": 1,
            "title": "Nexus PTY", "status": "idle", "lines": ["User: hello"]}) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 3
        while termios.tcgetattr(slave) == before and process.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(.01)
        os.write(master, b"hello\r")
        assert read_action() == {"type": "submit", "text": "hello", "mode": "queue", "generation": 0}
        os.write(master, b"/m")  # no Tab: completion is requested after a short pause
        assert read_action() == {"type": "complete", "text": "/m", "prefix": "/m", "generation": 0}
        os.write(master, b"\t")
        assert read_action() == {"type": "complete", "text": "/m", "prefix": "/m", "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 2, "title": "Nexus PTY", "status": "idle",
            "completion_query": "/m", "completions": ["/model", "/mcp"]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"\x1b[B\r")  # Enter on a standalone /command runs the highlighted one, like Textual
        assert read_action() == {"type": "submit", "text": "/mcp", "mode": "queue", "generation": 0}
        # Keyboard focus: Tab on an empty draft focuses the last clickable block; Enter opens it.
        process.stdin.write((json.dumps({"schema": 1, "revision": 3, "title": "Nexus PTY", "status": "idle",
            "blocks": [{"id": "t1", "kind": "tool", "text": "Read a.py", "operation": {"kind": "tool_page", "id": "t1"}},
                       {"id": "t2", "kind": "tool", "text": "Read b.py", "operation": {"kind": "tool_page", "id": "t2"}}]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.2)
        os.write(master, b"\t\x1b[A\r")
        assert read_action() == {"type": "operation", "operation": {"kind": "tool_page", "id": "t1"}, "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 3,
            "title": "Nexus PTY", "status": "awaiting_permission", "lines": [],
            "prompt": {"kind": "permission", "id": "permission-1", "lines": ["Run shell?"],
                       "choices": [{"label": "Allow once", "value": "allow_once", "key": "y", "disabled": False}]}}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"y")
        assert read_action() == {"type": "answer", "text": "permission-1", "value": "allow_once", "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 4,
            "title": "Nexus", "status": "idle", "panel_title": "Settings file",
            "form": {"id": "form-1", "body": "initial", "secret": False, "autosave": False}}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"!\x13")
        assert read_action() == {"type": "save", "form": "form-1", "body": "initial!", "revision": 1, "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 5,
            "title": "Nexus", "status": "idle", "panel_title": "API key",
            "form": {"id": "secret-1", "body": "", "secret": True, "autosave": False}}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"secret-value\x1b")
        # Escape cancels a credential form; only explicit Ctrl+S may save it.
        assert read_action() == {"type": "dismiss", "text": ""}
        os.write(master, b"\x11")
        assert read_action()["type"] == "quit"
        assert read_action() == {"restored": True}
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
        stop_drain.set()
        drain.join(timeout=.5)
        os.close(master)
        os.close(slave)
