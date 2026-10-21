"""Native input uses the controlling PTY while stdin carries bridge updates.

Build rust/tui first. This developer integration test needs controlling-TTY access.
"""
import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import termios
import time
import threading

import pytest

from pty_cleanup import kill_group

BINARY = Path(__file__).resolve().parents[1] / "rust/tui/target/debug/nexus-ratatui"


def _screen_row(probe, text):
    from ratatui_screen import Screen
    screen = Screen(40, 140)
    screen.feed(bytes(probe.output))
    matches = [i + 1 for i, row in enumerate(screen.text().splitlines()) if text in row]
    assert matches, screen.text()
    return matches[0]


def _copy_column(probe):
    from ratatui_screen import Screen
    screen = Screen(40, 140)
    screen.feed(bytes(probe.output))
    row = next(row for row in screen.text().splitlines() if "Copy" in row)
    return row.index("Copy") + 2


def _click_row(probe, text, x=12, double=False):
    row = _screen_row(probe, text)
    click = f"\x1b[<0;{x};{row}M\x1b[<0;{x};{row}m".encode()
    probe.keys(click * (2 if double else 1))


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_native_bridge_keyboard_and_terminal_restoration():
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 100))
    before = termios.tcgetattr(slave)
    stop_drain = threading.Event()
    bells = []
    rendered = bytearray()
    def drain_terminal():
        while not stop_drain.is_set():
            try:
                if select.select([master], [], [], .1)[0]:
                    output = os.read(master, 65536)
                    bells.extend([True] * output.count(b"\x07"))
                    rendered.extend(output)
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
                "composer_key": key, "generation": generation, "status": "idle",
                "title": chr(65 + generation) * 12}) + "\n").encode())
            process.stdin.flush()
            deadline = time.monotonic() + 5
            while (chr(65 + generation) * 12).encode() not in rendered:
                assert process.poll() is None
                assert time.monotonic() < deadline, "session snapshot did not render"
                time.sleep(.01)
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
        assert read_action() == {"type": "complete", "text": "/m", "prefix": "/m", "directories": True, "generation": 0}
        os.write(master, b"\t")
        assert read_action() == {"type": "complete", "text": "/m", "prefix": "/m", "directories": True, "generation": 0}
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
        assert not bells, "the Python shell plays the completion cue; the native client never rings"
        os.write(master, b"\t\x1b[A\r")
        assert read_action() == {"type": "operation", "operation": {"kind": "tool_page", "id": "t1"}, "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 5,
            "title": "Nexus PTY", "status": "awaiting_permission", "lines": [],
            "prompt": {"kind": "permission", "id": "permission-1", "lines": ["Run shell?"],
                       "choices": [{"label": "Allow once", "value": "allow_once", "key": "y", "disabled": False}]}}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"y")
        assert read_action() == {"type": "answer", "text": "permission-1", "value": "allow_once", "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 6,
            "title": "Nexus", "status": "idle", "panel_title": "Settings file",
            "form": {"id": "form-1", "body": "initial", "secret": False, "autosave": False}}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"!\x13")
        assert read_action() == {"type": "save", "form": "form-1", "body": "initial!", "revision": 1, "generation": 0}
        process.stdin.write((json.dumps({"schema": 1, "revision": 7,
            "title": "Nexus", "status": "idle", "panel_title": "API key",
            "form": {"id": "secret-1", "body": "", "secret": True, "autosave": False}}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"secret-value\x1b")
        # Escape cancels a credential form; only explicit Ctrl+S may save it.
        assert read_action() == {"type": "dismiss", "text": ""}
        # Bounded modal: mouse selects an item; outside click dismisses without touching chat.
        process.stdin.write((json.dumps({"schema": 1, "revision": 8, "status": "idle",
            "panel_title": "Commands", "panel_layout": "modal", "restore": "keep this draft",
            "items": [{"label": "Context", "command": "/context"}]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"\x1b[<0;10;12M")  # first item row (inside the modal frame); moves with the composer height and the top bar (4 rows at 24+ terminal rows)
        assert read_action() == {"type": "pick", "text": "/context", "generation": 0}
        os.write(master, b"\x1b[<0;1;1M")
        assert read_action() == {"type": "dismiss", "text": ""}
        process.stdin.write((json.dumps({"schema": 1, "revision": 9, "status": "idle",
            "panel_title": "Provider usage", "panel_layout": "modal", "panel_loading": True,
            "panel_lines": ["Cached limits"]}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"r")
        assert read_action() == {"type": "command", "text": "/usage"}
        # A child page is read-only; keys and paste cannot change the root draft.
        process.stdin.write((json.dumps({"schema": 1, "revision": 10, "status": "done",
            "agent_page": "child", "title": "advisor · Inspect", "sessions_sidebar": False}) + "\n").encode())
        process.stdin.flush()
        time.sleep(.1)
        os.write(master, b"\x1b[<0;1;2M")  # header y=1 (zero-based), SGR coordinates are one-based
        assert read_action() == {"type": "dismiss", "text": ""}
        os.write(master, b"ignored\r\x1b[200~pasted\x1b[201~")
        time.sleep(.1)
        os.write(master, b"\x1b")
        assert read_action() == {"type": "dismiss", "text": ""}
        process.stdin.write((json.dumps({"schema": 1, "revision": 11, "status": "idle",
            "title": "Root page ready"}) + "\n").encode())
        process.stdin.flush()
        # Snapshot updates and terminal keys arrive on separate channels. Wait
        # for the root frame, not an assumed 100 ms snapshot-processing window.
        deadline = time.monotonic() + 5
        while b"Root page ready" not in rendered:
            assert process.poll() is None
            assert time.monotonic() < deadline, "root page did not render"
            time.sleep(.01)
        os.write(master, b"\r")
        assert read_action() == {"type": "submit", "text": "keep this draft", "mode": "steer", "generation": 0}
        os.write(master, b"\x1b[<0;90;22M")  # controls row: above the half-row padding and the meter row
        assert read_action() == {"type": "command", "text": "/context"}
        os.write(master, b"\x18c")
        assert read_action() == {"type": "context_popover", "text": ""}
        os.write(master, b"\x0c")
        assert read_action() == {"type": "toggle", "key": "details_sidebar"}
        os.write(master, b"\x11")
        assert read_action()["type"] == "quit"
        assert read_action() == {"restored": True}
        assert process.wait(timeout=3) == 0
    finally:
        kill_group(process)
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
        kill_group(process)
        process.stdin.close()
        process.stdout.close()
        stop.set()
        thread.join(timeout=.5)
        os.close(master)
        os.close(slave)


def _plain(data) -> str:
    """Decode terminal output with CSI/OSC escape sequences removed."""
    text = bytes(data).decode("utf-8", "replace")
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)


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
        assert re.search(r"Bash\s+ls", _plain(probe.output))
        probe.keys(b"\t\r")
        time.sleep(.1)
        assert not probe.read_actions()
        assert b"command" in probe.output
        # A double click on the output label opens the rest without a Python snapshot.
        _click_row(probe, "Result:", double=True)
        time.sleep(.1)
        assert not probe.read_actions()
        from ratatui_screen import Screen
        current = Screen(40, 140)
        current.feed(bytes(probe.output))
        assert "UNIQUE LOCAL RESULT" in current, current.text()
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
def test_tool_single_click_selects_double_click_toggles_locally():
    from ratatui_latency_probe import NativeProbe
    probe = NativeProbe(BINARY)
    inspect = {"kind": "tool_details", "id": "c"}
    block = {"id":"g", "kind":"tool_group", "text":"Bash · 1 call", "local_ui":True,
             "operation":{"kind":"block_toggle","id":"g"}, "members":[
                 {"id":"c", "kind":"tool", "heading":"Bash ls", "local_ui":True,
                  "operation":{"kind":"block_toggle","id":"c:detail"},
                  "output_operation":{"kind":"block_toggle","id":"c:output"},
                  "inspect_operation":inspect,
                  "fold_lines":2, "local_detail":"  command: ls\nResult:\nUNIQUE LOCAL RESULT"}]}
    def screen_text():
        from ratatui_screen import Screen
        current = Screen(40, 140)
        current.feed(bytes(probe.output))
        return current.text()

    try:
        probe.send({"schema":3,"reset":True,"revision":1,"generation":1,"blocks":[block],"sessions_sidebar":False,"details_sidebar":False})
        deadline=time.monotonic()+4
        while b"Bash" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        probe.keys(b"\t\r")  # open the group and the call, as in the disclosure test
        time.sleep(.1)
        probe.keys(b"\t\r")
        time.sleep(.1)
        probe.read_actions()
        assert "command" in screen_text()  # the call is open, folded to its first two rows
        # One click only selects: no toggle, no Python operation.
        _click_row(probe, "Bash ls")
        time.sleep(.5)  # longer than the 300 ms double-click window
        assert not probe.read_actions()
        assert "command" in screen_text()
        # Two clicks on the heading within 300 ms toggle the call locally (it folds shut)...
        time.sleep(.35)  # expire the earlier single-click selection
        _click_row(probe, "Bash ls", double=True)
        time.sleep(.15)
        assert not probe.read_actions()
        assert "command" not in screen_text()
        # ...and again open it. The tool's own result stays folded until the output label opens it.
        time.sleep(.35)
        _click_row(probe, "Bash ls", double=True)
        time.sleep(.15)
        assert not probe.read_actions()
        assert "command" in screen_text()
        assert "UNIQUE LOCAL RESULT" not in screen_text()
        time.sleep(.35)
        _click_row(probe, "Result:", double=True)
        time.sleep(.15)
        assert not probe.read_actions()
        assert "UNIQUE LOCAL RESULT" in screen_text(), screen_text()
        # The keyboard `i` inspector is still a Python operation.
        probe.keys(b"i")
        time.sleep(.15)
        ops = [a["operation"] for a in probe.read_actions() if a.get("type") == "operation"]
        assert ops == [inspect]
        assert probe.process.poll() is None
    finally:
        probe.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_hover_copy_buttons_on_user_cards_and_code_fences():
    from ratatui_latency_probe import NativeProbe
    probe = NativeProbe(BINARY)
    blocks = [{"id":"m:user", "kind":"user", "title":"COPY ME", "text":"second", "number":1,
               "operation":{"kind":"turn_toggle","id":"t"}},
              {"id":"r", "kind":"markdown", "gap":1, "text":"Intro\n\n```rust\nlet x = 1;\n```"}]
    try:
        probe.send({"schema":3,"reset":True,"revision":1,"generation":1,"blocks":blocks,"sessions_sidebar":False,"details_sidebar":False})
        deadline=time.monotonic()+4
        while b"COPY ME" not in probe.output and probe.process.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        time.sleep(.1)
        assert b"Copy" not in probe.output  # hidden until the pointer is over a block
        mark = len(probe.output)
        row = _screen_row(probe, "COPY ME")
        probe.keys(f"\x1b[<35;100;{row}M".encode())  # pointer over the card body
        time.sleep(.1)
        assert b"Copy" in probe.output[mark:]
        _click_row(probe, "Copy", x=_copy_column(probe))  # the card's button, top-right
        time.sleep(.1)
        copied = [a for a in probe.read_actions() if a.get("type") == "copy_selection"]
        assert [a["text"] for a in copied] == ["COPY ME\nsecond"]
        row = _screen_row(probe, "rust")
        probe.keys(f"\x1b[<35;100;{row}M".encode())
        time.sleep(.1)
        _click_row(probe, "Copy", x=_copy_column(probe))  # the fence header's button
        time.sleep(.1)
        copied = [a for a in probe.read_actions() if a.get("type") == "copy_selection"]
        assert [a["text"] for a in copied] == ["let x = 1;"]
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
        probe.keys(b"\x0c")
        time.sleep(.08)
        first=probe.read_actions()
        assert first[0]["type"]=="toggle" and first[0]["value"] is True
        probe.keys(b"\x0c")
        time.sleep(.08)
        second=probe.read_actions()
        assert second[0]["value"] is False
        # Delayed persistence echo from the first click must not reopen the sidebar.
        probe.send({"schema":3,"revision":2,"generation":1,"details_sidebar":True,"ui_ack":first[0]["ui_sequence"]})
        time.sleep(.08)
        probe.keys(b"\x0c")
        time.sleep(.08)
        third=probe.read_actions()
        assert third[0]["value"] is True
        assert third[0]["ui_sequence"]>second[0]["ui_sequence"]
    finally:
        probe.close()


@pytest.mark.skipif(not BINARY.exists(), reason="build the native prototype first")
def test_settings_native_focus_reorder_and_loading_guard():
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (32, 122))
    before = termios.tcgetattr(slave)
    setup = "import os,fcntl,termios; os.setsid(); fcntl.ioctl(2,termios.TIOCSCTTY,0); os.execv(" + repr(str(BINARY)) + ", [" + repr(str(BINARY)) + "])"
    process = subprocess.Popen([sys.executable, "-c", setup], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave)
    stop = threading.Event()
    def drain():
        while not stop.is_set():
            if select.select([master], [], [], .05)[0]:
                try: os.read(master, 65536)
                except OSError: return
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    buffer = bytearray()
    def action():
        deadline = time.monotonic() + 3
        while b"\n" not in buffer:
            assert select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]
            buffer.extend(os.read(process.stdout.fileno(), 65536))
        line, _, rest = buffer.partition(b"\n")
        buffer[:] = rest
        return json.loads(line)
    def snapshot(revision, loading=False):
        value = {"schema": 1, "revision": revision, "generation": 1, "title": "Settings PTY", "status": "idle",
            "panel_title": "Models", "panel_layout": "modal", "panel_loading": loading,
            "panel_lines": ["Global model routing"],
            "nav": {"items": [["GENERAL", "", True], ["Models", "models", False], ["CONFIGURE", "", True], ["Providers", "providers", False]], "selected": 1},
            "items": [{"label": "Model", "command": "", "operation": {"kind": "tier_ref"}, "move_down": {"kind": "tier_move", "delta": 1}}]}
        process.stdin.write((json.dumps(value) + "\n").encode()); process.stdin.flush()
        time.sleep(.15)
    try:
        snapshot(1)
        deadline = time.monotonic() + 3
        while termios.tcgetattr(slave) == before:
            assert time.monotonic() < deadline
            time.sleep(.01)
        # Settings opens at the root list: Animate, Monochrome, then the Models drill row.
        os.write(master, b"\x1b[B\x1b[B\r")
        assert action()["type"] == "nav_select"
        os.write(master, b"\x1b[C\r")
        assert action()["operation"]["kind"] == "tier_ref"
        os.write(master, b"\x1b[1;3B")
        assert action()["operation"]["kind"] == "tier_move"
        snapshot(2, loading=True)
        os.write(master, b"\r\x1b[D\x1b[B")
        assert not select.select([process.stdout], [], [], .25)[0], "loading must suppress stale actions"
        snapshot(3)
        os.write(master, b"\x1b")  # a drilled page: Escape returns to the root list
        time.sleep(.2)
        os.write(master, b"\x1b")  # the root list: Escape closes Settings
        assert action()["type"] == "dismiss"
    finally:
        kill_group(process)
        stop.set(); thread.join(timeout=1)
        process.stdin.close(); process.stdout.close()
        os.close(master); os.close(slave)
