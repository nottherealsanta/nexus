"""Terminal key-protocol regression tests for the Textual chat shell.

These pin the byte-level contract the interactive CLI depends on. A real
terminal never delivers simulated ``Key`` objects: the driver reads raw bytes,
Textual's ``XTermParser`` decodes them, and only then does the focused editor
see an event. The tests here cover that whole path in two layers:

* ``NexusXTermParser`` fed the actual byte sequences for each emulator family
  (Kitty CSI-u, xterm ``modifyOtherKeys``, legacy ``CR``/``LF``).
* A real pseudo-terminal running :class:`~nexus.ui.tui.keys.NexusDriver` and the
  probe app in ``tests/tui_keyprobe.py``, so the OS read and driver wiring are
  exercised too.

The app-level draft/submit semantics live in ``tests/test_ui_tui.py``.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

from nexus.ui.tui.keys import (
    MODIFY_OTHER_KEYS_SPECIAL,
    NexusDriver,
    NexusXTermParser,
    normalize_modify_other_keys,
)

ROOT = Path(__file__).resolve().parents[1]
POSIX = sys.platform != "win32"

# The exact byte sequences each family sends for the Enter variants.
BARE_ENTER = "\r"  # legacy Enter (also what a collapsed Shift+Enter looks like)
LEGACY_NEWLINE = "\n"  # Ctrl+J, the LF byte every terminal reports distinctly
KITTY_SHIFT_ENTER = "\x1b[13;2u"
KITTY_CTRL_ENTER = "\x1b[13;5u"
KITTY_ALT_ENTER = "\x1b[13;3u"
MOK_SHIFT_ENTER = "\x1b[27;2;13~"
MOK_CTRL_ENTER = "\x1b[27;5;13~"
MOK_ALT_ENTER = "\x1b[27;3;13~"
MOK_CTRL_SHIFT_ENTER = "\x1b[27;6;13~"
MOK_PLAIN_ENTER = "\x1b[27;1;13~"


def _keys(sequence: str, parser=None) -> list[str]:
    parser = NexusXTermParser() if parser is None else parser
    messages = list(parser.feed(sequence)) + list(parser.tick()) + list(parser.feed(""))
    return [message.key for message in messages]


def test_normalization_translates_only_special_modify_other_keys():
    assert normalize_modify_other_keys(MOK_SHIFT_ENTER) == "\x1b[13;2u"
    assert normalize_modify_other_keys(MOK_CTRL_ENTER) == "\x1b[13;5u"
    assert normalize_modify_other_keys(MOK_ALT_ENTER) == "\x1b[13;3u"
    assert normalize_modify_other_keys(MOK_CTRL_SHIFT_ENTER) == "\x1b[13;6u"
    assert normalize_modify_other_keys("\x1b[27;5;9~") == "\x1b[9;5u"  # Ctrl+Tab
    # Printable "other" keys are left to Textual, which already decodes them; a
    # blind rewrite to CSI-u would drop the associated text.
    assert normalize_modify_other_keys("\x1b[27;2;70~") is None
    assert normalize_modify_other_keys("\x1b[13;2u") is None
    assert normalize_modify_other_keys("plain") is None


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        (BARE_ENTER, "enter"),
        (LEGACY_NEWLINE, "ctrl+j"),
        (KITTY_SHIFT_ENTER, "shift+enter"),
        (KITTY_CTRL_ENTER, "ctrl+enter"),
        (KITTY_ALT_ENTER, "alt+enter"),
        (MOK_SHIFT_ENTER, "shift+enter"),
        (MOK_CTRL_ENTER, "ctrl+enter"),
        (MOK_ALT_ENTER, "alt+enter"),
        (MOK_CTRL_SHIFT_ENTER, "ctrl+shift+enter"),
        (MOK_PLAIN_ENTER, "enter"),
    ],
)
def test_parser_decodes_every_terminal_encoding(sequence: str, expected: str):
    assert _keys(sequence) == [expected]


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[27;2;70~", "F"),  # modifyOtherKeys Shift+F keeps its text
        ("alpha", ["a", "l", "p", "h", "a"]),
        ("\x1b[A", "up"),
        ("\x1b[13;2~", "shift+f3"),  # a real F3 must not become enter
    ],
)
def test_parser_leaves_non_enter_sequences_intact(sequence: str, expected):
    keys = _keys(sequence)
    assert keys == ([expected] if isinstance(expected, str) else expected)


def test_special_keycode_set_is_the_control_keys_only():
    assert MODIFY_OTHER_KEYS_SPECIAL == frozenset({9, 13, 27, 127})


@pytest.mark.skipif(not POSIX, reason="pty is POSIX-only")
def test_driver_installs_the_modify_other_keys_parser():
    assert NexusDriver is not None
    assert NexusDriver.parser_class is NexusXTermParser


@pytest.mark.skipif(not POSIX, reason="pty is POSIX-only")
def test_real_pty_driver_decodes_raw_terminal_bytes(tmp_path):
    """The real OS read -> NexusDriver -> app path decodes emulator bytes.

    A bare parser test would still pass if the driver never installed
    ``NexusXTermParser``; this launches the probe in a pseudo-terminal so the
    line discipline, driver input thread, parser swap and app dispatch all run.
    """
    log = tmp_path / "keys.jsonl"
    log.write_text("", encoding="utf-8")

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=str(ROOT))
    env["TEXTUAL_ANIMATIONS"] = "none"

    process = subprocess.Popen(
        [sys.executable, "tests/tui_keyprobe.py", str(log)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=ROOT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    drained = bytearray()
    stop = threading.Event()

    def drain() -> None:  # pragma: no cover - background reader
        while not stop.is_set():
            ready, _, _ = select.select([master], [], [], 0.1)
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                drained.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    def records() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def wait_for(predicate, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(records()):
                return True
            if process.poll() is not None:
                return predicate(records())
            time.sleep(0.05)
        return predicate(records())

    try:
        assert wait_for(lambda rows: any(row.get("event") == "ready" for row in rows)), (
            f"probe app never booted; terminal output: {bytes(drained[-500:])!r}"
        )
        for sequence in (KITTY_SHIFT_ENTER, MOK_CTRL_ENTER, MOK_ALT_ENTER, MOK_SHIFT_ENTER, LEGACY_NEWLINE, BARE_ENTER):
            os.write(master, sequence.encode())
        assert wait_for(lambda rows: sum("key" in row for row in rows) >= 6), (
            f"not all keys decoded; got {records()!r}"
        )
        os.write(master, b"\x11")  # Ctrl+Q exits the probe
        process.wait(timeout=15)
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        os.close(master)

    assert [row["key"] for row in records() if "key" in row][:6] == [
        "shift+enter",  # Kitty CSI-u
        "ctrl+enter",  # modifyOtherKeys, only possible with our parser
        "alt+enter",  # modifyOtherKeys Alt+Enter, also only possible with our parser
        "shift+enter",  # modifyOtherKeys
        "ctrl+j",  # legacy LF fallback
        "enter",  # bare CR
    ]


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.skipif(not POSIX, reason="pty is POSIX-only")
def test_real_pty_shell_shift_enter_drafts_then_enter_sends(tmp_path):
    """End-to-end: the real app + driver turn terminal bytes into draft then send.

    Unlike the parser and pilot tests, this launches the actual
    :class:`~nexus.ui.tui.app.NexusTextualApp` (its real ``get_driver_class``
    installs :class:`NexusDriver`) over the deterministic fixture transport. The
    bytes a Kitty terminal sends for ``alpha``, ``Shift+Enter``, ``beta`` and
    ``Enter`` go through the OS read, the driver, the parser and the focused
    :class:`ChatEditor`. Shift+Enter must leave the two-line draft unsent; Enter
    must then submit exactly ``alpha\nbeta``.
    """
    log = tmp_path / "e2e.jsonl"
    log.write_text("", encoding="utf-8")

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=str(ROOT))
    env["TEXTUAL_ANIMATIONS"] = "none"

    process = subprocess.Popen(
        [sys.executable, "tests/tui_e2e_probe.py", str(log)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=ROOT,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    drained = bytearray()
    stop = threading.Event()

    def drain() -> None:  # pragma: no cover - background reader
        while not stop.is_set():
            ready, _, _ = select.select([master], [], [], 0.1)
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                drained.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    def wait_for(predicate, timeout: float = 25.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(_jsonl(log)):
                return True
            time.sleep(0.05)
        return predicate(_jsonl(log))

    try:
        assert wait_for(lambda rows: any(row.get("event") == "ready" for row in rows)), (
            f"shell never focused its editor; terminal output: {bytes(drained[-500:])!r}"
        )
        os.write(master, b"alpha")
        os.write(master, KITTY_SHIFT_ENTER.encode())
        os.write(master, b"beta")
        assert wait_for(
            lambda rows: any(
                row.get("event") == "draft" and row.get("text") == "alpha\nbeta"
                for row in rows
            )
        ), f"Shift+Enter did not leave a two-line draft: {_jsonl(log)!r}"
        assert not [row for row in _jsonl(log) if row.get("event") == "submitted"], (
            "a modified Enter submitted the draft instead of inserting a newline"
        )

        os.write(master, BARE_ENTER.encode())
        assert wait_for(
            lambda rows: any(
                row.get("event") == "submitted" and row.get("content") == "alpha\nbeta"
                for row in rows
            )
        ), f"Enter did not submit the multiline draft: {_jsonl(log)!r}"
        os.write(master, b"\x11")  # Ctrl+Q exits the shell
        process.wait(timeout=15)
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        os.close(master)
