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
    bells = []
    def drain_terminal():
        while not stop_drain.is_set():
            try:
                if select.select([master], [], [], .1)[0]:
                    output = os.read(master, 65536)
                    bells.extend([True] * output.count(b"\x07"))
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
        assert read_action() == {"type": "submit", "text": "hello", "mode": "steer", "generation": 0}
        # Drafts belong to workspace/session, not to the currently visible tab.
        def session_snapshot(key, generation, revision):
            process.stdin.write((json.dumps({"schema": 1, "revision": revision,
                "composer_key": key, "generation": generation, "status": "idle"}) + "\n").encode())
            process.stdin.flush()
            time.sleep(.15)
        session_snapshot("workspace/a", 1, 1)
        os.write(master, b"keep my draft")
        time.sleep(.15)
        session_snapshot("workspace/b", 2, 1)
        os.write(master, b"other\r")
        assert read_action() == {"type": "submit", "text": "other", "mode": "steer", "generation": 2}
        session_snapshot("workspace/a", 3, 1)
        os.write(master, b"\r")
        assert read_action() == {"type": "submit", "text": "keep my draft", "mode": "steer", "generation": 3}
        session_snapshot("", 0, 1)
        os.write(master, b"/m")  # no Tab: completion is requested after a short pause
        assert read_action() == {"type": "complete", "text": "/m", "prefix": "/m", "generation": 0}
        os.write(master, b"\t")
        assert read_action() == {"type": "complete", "text": "/m", "prefix": "/m", "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 2, "title": "Nexus PTY", "status": "idle",
            "completion_query": "/m", "completions": ["/model", "/mcp"]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"\x1b[B\r")  # Enter on a standalone /command runs the highlighted one, like native terminal
        assert read_action() == {"type": "submit", "text": "/mcp", "mode": "steer", "generation": 0}
        # Keyboard focus: Tab on an empty draft focuses the last clickable block; Enter opens it.
        process.stdin.write((json.dumps({"schema": 3, "revision": 3, "title": "Nexus PTY", "status": "idle",
            "blocks": [{"id": "t1", "kind": "tool", "text": "Read a.py", "operation": {"kind": "tool_page", "id": "t1"}},
                       {"id": "t2", "kind": "tool", "text": "Read b.py", "operation": {"kind": "tool_page", "id": "t2"}}]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.2)
        assert not bells
        for revision in (4, 5):
            process.stdin.write((json.dumps({"schema": 3, "revision": revision,
                "completion_bell": 1, "blocks": [
                    {"id": "t1", "kind": "tool", "text": "Read a.py", "operation": {"kind": "tool_page", "id": "t1"}},
                    {"id": "t2", "kind": "tool", "text": "Read b.py", "operation": {"kind": "tool_page", "id": "t2"}}]}) + "\n").encode())
            process.stdin.flush()
            time.sleep(.1)
        assert bells == [True]
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
        # Bounded modal: mouse selects an item; outside click dismisses without touching chat.
        process.stdin.write((json.dumps({"schema": 1, "revision": 6, "status": "idle",
            "panel_title": "Commands", "panel_layout": "modal", "restore": "keep this draft",
            "items": [{"label": "Context", "command": "/context"}]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"\x1b[<0;10;10M")  # first item row; moves with the composer height
        assert read_action() == {"type": "pick", "text": "/context", "generation": 0}
        os.write(master, b"\x1b[<0;1;1M")
        assert read_action() == {"type": "dismiss", "text": ""}
        process.stdin.write((json.dumps({"schema": 1, "revision": 7, "status": "idle",
            "panel_title": "Provider usage", "panel_layout": "modal", "panel_loading": True,
            "panel_lines": ["Cached limits"]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"r")
        assert read_action() == {"type": "command", "text": "/usage"}
        # A child page is read-only; keys and paste cannot change the root draft.
        process.stdin.write((json.dumps({"schema": 1, "revision": 8, "status": "done",
            "agent_page": "child", "title": "advisor · Inspect", "sessions_sidebar": False}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"ignored\r\x1b[200~pasted\x1b[201~")
        time.sleep(.1)
        os.write(master, b"\x1b")
        assert read_action() == {"type": "dismiss", "text": ""}
        process.stdin.write((json.dumps({"schema": 1, "revision": 8, "status": "idle"}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"\r")
        assert read_action() == {"type": "submit", "text": "keep this draft", "mode": "steer", "generation": 0}
        os.write(master, b"\x1b[<0;90;21M")  # controls row: above padding, workspace and meter rows
        assert read_action() == {"type": "context_popover", "text": ""}
        os.write(master, b"\x18c")
        assert read_action() == {"type": "context_popover", "text": ""}
        os.write(master, b"\x0c")
        assert read_action() == {"type": "toggle", "key": "details_sidebar"}
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


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_context_dialog_toggle_keyboard_and_lock():
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (40, 140))
    setup = (
        "import os,fcntl,termios; os.setsid(); "
        "fcntl.ioctl(2,termios.TIOCSCTTY,0); os.execv(" + repr(str(BINARY)) + ", [" + repr(str(BINARY)) + "])"
    )
    process = subprocess.Popen([sys.executable, "-c", setup], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave)
    stop = threading.Event()
    def drain():
        while not stop.is_set():
            try:
                if select.select([master], [], [], .1)[0]:
                    os.read(master, 65536)
            except OSError:
                return
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    def action():
        assert select.select([process.stdout], [], [], 3)[0], "native action timed out"
        return json.loads(process.stdout.readline())
    def snapshot(locked=False):
        process.stdin.write((json.dumps({"schema": 1, "status": "idle", "panel_title": "Tools", "panel_layout": "context",
            "items": [{"label": "read", "operation": {"kind": "tool_definition"}, "toggle_enabled": True,
                       "toggle_locked": locked, "toggle_operation": {"kind": "context_toggle"}}]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.2)
    try:
        snapshot()
        os.write(master, b" ")
        assert action()["operation"]["kind"] == "context_toggle"
        os.write(master, b"\r")
        assert action()["operation"]["kind"] == "tool_definition"
        snapshot(locked=True)
        os.write(master, b" ")
        assert not select.select([process.stdout], [], [], .2)[0]
        os.write(master, b"\r")
        assert action()["operation"]["kind"] == "tool_definition"
    finally:
        process.kill()
        process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
        stop.set()
        thread.join(timeout=.5)
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_local_disclosure_without_python_reply():
    from ratatui_latency_probe import NativeProbe
    probe = NativeProbe(BINARY)
    block = {"id":"g", "kind":"tool_group", "text":"Bash · 1 call", "local_ui":True,
             "operation":{"kind":"block_toggle","id":"g"}, "members":[
                 {"id":"c", "kind":"tool", "heading":"Bash ls", "local_ui":True,
                  "operation":{"kind":"block_toggle","id":"c:detail"},
                  "output_operation":{"kind":"block_toggle","id":"c:output"},
                  "fold_lines":2, "local_detail":"  command: ls\nResult:\nUNIQUE LOCAL RESULT"}]}
    try:
        probe.send({"schema":3,"reset":True,"revision":1,"generation":1,"blocks":[block],"sessions_sidebar":False,"details_sidebar":False})
        deadline=time.monotonic()+4
        while b"Bash" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        assert probe.process.poll() is None, bytes(probe.output[-1000:])
        probe.keys(b"\t\r")
        time.sleep(.1)
        assert not probe.read_actions()
        assert b"Bash ls" in probe.output
        probe.keys(b"\t\r")
        time.sleep(.1)
        assert not probe.read_actions()
        assert b"command" in probe.output
        # Mouse on the output label opens the rest without a Python snapshot.
        probe.keys(b"\x1b[<0;12;4M\x1b[<0;12;4m")
        time.sleep(.1)
        assert not probe.read_actions()
        assert b"UNIQUE LOCAL RESULT" in probe.output
        # Ordered patches preserve local expansion state.
        updated = json.loads(json.dumps(block))
        updated["members"][0]["local_detail"] += "\nSTREAMED RESULT"
        probe.send({"schema":3,"revision":2,"generation":1,"blocks_from":0,"blocks":[updated]})
        time.sleep(.1)
        assert not probe.read_actions()
        assert b"STREAMED RESULT" in probe.output
        assert probe.process.poll() is None
    finally:
        probe.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_static_completion_and_optimistic_toggle_ignore_stale_echo():
    from ratatui_latency_probe import NativeProbe
    probe=NativeProbe(BINARY)
    try:
        probe.send({"schema":3,"reset":True,"revision":1,"generation":1,"status":"idle",
                    "local_ui_enabled":True,"sessions_sidebar":False,"details_sidebar":False,
                    "commands":[["/model",[]],["/mock",[]]],"blocks":[{"id":"a","kind":"markdown","text":"READY"}]})
        deadline=time.monotonic()+4
        while b"READY" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline: time.sleep(.02)
        probe.keys(b"/m")
        time.sleep(.15)
        assert not probe.read_actions()
        assert b"/model" in probe.output
        probe.keys(b"\x02")
        time.sleep(.08)
        first=probe.read_actions()
        assert first[0]["type"]=="toggle" and first[0]["value"] is True
        probe.keys(b"\x02")
        time.sleep(.08)
        second=probe.read_actions()
        assert second[0]["value"] is False
        # Delayed persistence echo from the first click must not reopen the sidebar.
        probe.send({"schema":3,"revision":2,"generation":1,"sessions_sidebar":True,"ui_ack":first[0]["ui_sequence"]})
        time.sleep(.08)
        probe.keys(b"\x02")
        time.sleep(.08)
        third=probe.read_actions()
        assert third[0]["value"] is True
        assert third[0]["ui_sequence"]>second[0]["ui_sequence"]
    finally:
        probe.close()
