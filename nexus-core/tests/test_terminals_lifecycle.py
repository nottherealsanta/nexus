import asyncio
import base64
import os
import signal

import pytest

from nexus.runtime import Conflict
from nexus.terminals import TerminalService


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_high_volume_discard_and_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="client", session_id=None, rows=24, cols=80)
    aid = term.attach("client")
    term.attachments[aid].connected = True
    term.control(aid, "acquire")
    try:
        term.input(aid, 1, b"head -c 1500000 /dev/zero | tr '\\000' X; printf DONE; exit 7\n")
        await wait_until(lambda: term.status == "exited")
        assert term.exit_code == 7
        assert term.discarded > 0
        assert term.retained <= 1024 * 1024
        assert any(frame["type"] == "discard" for frame, _ in term.frames)
        assert b"DONE" in b"".join(base64.b64decode(f["data"]) for f, _ in term.frames if f["type"] == "output")
    finally:
        await service.shutdown()


async def test_detach_reconnect_and_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="client", session_id=None, rows=24, cols=80)
    first = term.attach("first")
    term.attachments[first].connected = True
    term.control(first, "acquire")
    term.detach(first)
    second = term.attach("second")
    term.attachments[second].connected = True
    term.control(second, "acquire")
    with pytest.raises(Conflict):
        term.input(first, 1, b"no")
    await service.shutdown()
    assert term.status == "closed"


async def test_controlling_terminal_and_signalled_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="client", session_id=None, rows=24, cols=80)
    def has_controlling_terminal():
        try:
            return os.tcgetpgrp(term.fd) == term.pid
        except OSError:
            return False

    try:
        await wait_until(has_controlling_terminal)
        assert os.getsid(term.pid) == term.pid
        os.killpg(term.pid, signal.SIGKILL)
        await wait_until(lambda: term.status == "exited")
        assert term.signal == signal.SIGKILL
        assert term.exit_code is None
    finally:
        await service.shutdown()
