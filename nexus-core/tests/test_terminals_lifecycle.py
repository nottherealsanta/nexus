import asyncio
import base64
import os
import shlex
import signal
import sqlite3
import sys

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


async def test_shutdown_survives_process_group_permission_race(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="client", session_id=None, rows=24, cols=80)
    killpg = os.killpg
    denied = []

    def deny_hangup(pgid, sig):
        if sig == signal.SIGHUP:
            denied.append(pgid)
            raise PermissionError("process group changed during shutdown")
        killpg(pgid, sig)

    try:
        await wait_until(lambda: os.tcgetpgrp(term.fd) == term.pid)
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", deny_hangup)
            await service.shutdown()
        assert term.pid in denied
        assert term.waiter.done()
        assert term.output_done.is_set()
        assert term._fd_closed
        assert term.status == "closed"
    finally:
        await service.shutdown()


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


def test_restart_marks_running_metadata_ended():
    db = sqlite3.connect(":memory:")
    service = TerminalService(db)
    service.save({"id": "t", "project_id": "p", "status": "running"})
    recovered = TerminalService(db).metadata("t")
    assert recovered["status"] == "ended"
    assert recovered["reason"] == "core_restart"


async def test_core_death_hangs_up_foreground_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    script = """
import asyncio, os, sys
from nexus.terminals import TerminalService
async def main():
    t = TerminalService().create(project_id='p', workspace=sys.argv[1], creator='c', session_id=None, rows=24, cols=80)
    while True:
        try:
            if os.tcgetpgrp(t.fd) == t.pid:
                break
        except OSError:
            pass
        await asyncio.sleep(.01)
    print(t.pid, flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    core = await asyncio.create_subprocess_exec(sys.executable, "-c", script, str(tmp_path),
                                                stdout=asyncio.subprocess.PIPE)
    shell_pid = 0
    try:
        assert core.stdout is not None
        shell_pid = int(await asyncio.wait_for(core.stdout.readline(), 5))
        core.kill()
        await core.wait()

        def shell_gone():
            try:
                os.kill(shell_pid, 0)
            except ProcessLookupError:
                return True
            return False

        await wait_until(shell_gone)
        shell_pid = 0
    finally:
        if core.returncode is None:
            core.kill()
            await core.wait()
        if shell_pid:
            try:
                os.killpg(shell_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_close_kills_separately_grouped_foreground_job(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    pidfile = tmp_path / "job.pid"
    script = tmp_path / "job.py"
    script.write_text("import os,signal,time\n"
                      "signal.signal(signal.SIGHUP,signal.SIG_IGN)\n"
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                      f"open({str(pidfile)!r},'w').write(str(os.getpid()))\n"
                      "time.sleep(60)\n")
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="c", session_id=None, rows=24, cols=80)
    aid = term.attach("c")
    term.attachments[aid].connected = True
    term.control(aid, "acquire")
    job_pid = 0
    try:
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}\n".encode()
        term.input(aid, 1, command)
        await wait_until(pidfile.exists)
        job_pid = int(pidfile.read_text())
        assert os.tcgetpgrp(term.fd) == job_pid and job_pid != term.pid
        await service.shutdown()

        def gone():
            try:
                os.kill(job_pid, 0)
            except ProcessLookupError:
                return True
            return False

        await wait_until(gone)
        job_pid = 0
    finally:
        await service.shutdown()
        if job_pid:
            try:
                os.killpg(job_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
