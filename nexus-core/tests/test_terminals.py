import asyncio
import base64
import os
import shlex
import sqlite3
import sys

import pytest

from nexus.runtime import Conflict
from nexus.terminals import Screen, TerminalService


def output(term):
    return b"".join(base64.b64decode(frame["data"]) for frame, _ in term.frames if frame["type"] == "output")


async def test_name_persists_and_legacy_metadata_defaults(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("SHELL", "/bin/sh")
    db = sqlite3.connect(":memory:")
    service = TerminalService(db)
    term = service.create(project_id="p", workspace=str(tmp_path), creator="one", session_id=None, rows=24, cols=80)
    service.rename(term.id, "Named")
    assert term.frames[-1][0]["type"] == "rename"
    await service.shutdown()
    restarted = TerminalService(db)
    assert restarted.metadata(term.id)["name"] == "Named"
    restarted.rename(term.id, "After restart")
    assert TerminalService(db).metadata(term.id)["name"] == "After restart"
    metadata = restarted.metadata(term.id)
    del metadata["name"]
    db.execute("update terminal_resource set metadata=? where id=?", (json.dumps(metadata), term.id))
    assert restarted.list("p")[0]["name"] == "Terminal"


async def wait_for(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("terminal output did not arrive")


async def test_shell_io_control_resize_and_close(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    service = TerminalService()
    term = service.create(project_id="project", workspace=str(tmp_path), creator="one", session_id=None, rows=24, cols=80)
    a, b = term.attach("one"), term.attach("two")
    term.attachments[a].connected = term.attachments[b].connected = True
    try:
        term.control(a, "acquire")
        with pytest.raises(Conflict):
            term.control(b, "acquire")
        term.input(a, 1, b"printf 'NEXUS_TEST_MARKER\\n'\n")
        term.input(a, 1, b"printf 'NEXUS_TEST_MARKER\\n'\n")
        await wait_for(lambda: b"NEXUS_TEST_MARKER\r\n" in output(term))
        term.resize(a, 30, 100)
        term.input(a, 2, b"stty size\n")
        await wait_for(lambda: b"30 100" in output(term))
        term.detach(a)
        term.control(b, "acquire")
        with pytest.raises(Conflict):
            term.input(a, 2, b"x")
    finally:
        await service.shutdown()
    assert term.status == "closed"
    with pytest.raises(ProcessLookupError):
        os.kill(term.pid, 0)


async def test_command_terminal_accepts_interactive_input(tmp_path):
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="job", session_id="s",
                          rows=24, cols=80, command="read value; printf 'received:%s\\n' \"$value\"")
    aid = term.attach("viewer")
    term.attachments[aid].connected = True
    try:
        term.control(aid, "acquire")
        term.input(aid, 1, b"hello\n")
        await wait_for(lambda: b"received:hello" in output(term))
        await term.waiter
        assert term.exit_code == 0
    finally:
        await service.shutdown()


async def test_output_frames_precede_exit_for_large_fast_output(tmp_path):
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="job", session_id="s",
                          rows=24, cols=80, command="head -c 50000 /dev/zero | tr '\\0' X")
    try:
        await asyncio.wait_for(term.waiter, 5)
        frames = [frame for frame, _ in term.frames]
        assert len(output(term)) == 50000
        assert frames[-1]["type"] == "exit"
    finally:
        await service.shutdown()


def test_screen_split_utf8_and_alt_screen():
    import pyte

    screen = Screen(40, 4)
    stream = pyte.ByteStream(screen)
    stream.feed(b"marker\x1b[?1049h")
    for byte in "界".encode():
        stream.feed(bytes([byte]))
    assert "界" in screen.display[0]
    stream.feed(b"\x1b[?1049l")
    assert "marker" in screen.display[0]


async def test_real_program_alt_screen_and_split_utf8(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    program = tmp_path / "alt.py"
    program.write_text(
        "import os,time\n"
        "os.write(1,b'\\x1b[?1049h')\n"
        "os.write(1,b'\\xe7'); time.sleep(.2); os.write(1,b'\\x95\\x8c')\n"
        "os.read(0,10)\n"
        "os.write(1,b'\\x1b[?1049l')\n"
    )
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="one", session_id=None, rows=24, cols=80)
    aid = term.attach("one")
    term.attachments[aid].connected = True
    term.control(aid, "acquire")
    try:
        term.input(aid, 1, f"{shlex.quote(sys.executable)} {shlex.quote(str(program))}\n".encode())
        await wait_for(lambda: term.screen._normal is not None and any("界" in row for row in term.screen.display))
        term.input(aid, 2, b"\n")
        await wait_for(lambda: term.screen._normal is None)
    finally:
        await service.shutdown()
