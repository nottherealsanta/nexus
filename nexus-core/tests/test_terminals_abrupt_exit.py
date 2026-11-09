"""Exercise the kernel PTY hangup path without a cooperative Core shutdown."""
import asyncio
import os
import shlex
import signal
import sys


async def test_core_death_hangs_up_separately_grouped_foreground_job(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    ready = tmp_path / "ready"
    hung_up = tmp_path / "hung-up"
    job = tmp_path / "foreground.py"
    job.write_text(
        "import os, signal, time\n"
        "def hangup(sig, frame):\n"
        f"    open({str(hung_up)!r}, 'w').write(str(os.getpid()))\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGHUP, hangup)\n"
        f"open({str(ready)!r}, 'w').write(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(job))}\n"
    core_script = """
import asyncio, os, sys
from pathlib import Path
from nexus.terminals import TerminalService
async def main():
    term = TerminalService().create(project_id='p', workspace=sys.argv[1], creator='c', session_id=None, rows=24, cols=80)
    aid = term.attach('c')
    term.attachments[aid].connected = True
    term.control(aid, 'acquire')
    term.input(aid, 1, sys.argv[2].encode())
    ready = Path(sys.argv[3])
    while not ready.exists():
        await asyncio.sleep(.01)
    job_pid = int(ready.read_text())
    while os.tcgetpgrp(term.fd) != job_pid:
        await asyncio.sleep(.01)
    assert job_pid != term.pid
    print(term.pid, job_pid, flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    core = await asyncio.create_subprocess_exec(
        sys.executable, "-c", core_script, str(tmp_path), command, str(ready),
        stdout=asyncio.subprocess.PIPE,
    )
    pids = []
    try:
        assert core.stdout is not None
        line = await asyncio.wait_for(core.stdout.readline(), 10)
        pids = [int(pid) for pid in line.split()]
        assert len(pids) == 2
        core.kill()
        await asyncio.wait_for(core.wait(), 5)
        async with asyncio.timeout(10):
            while not hung_up.exists():
                await asyncio.sleep(.01)
        assert int(hung_up.read_text()) == pids[1]
    finally:
        if core.returncode is None:
            core.kill()
            await core.wait()
        if ready.exists() and not pids:
            pids.append(int(ready.read_text()))
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
