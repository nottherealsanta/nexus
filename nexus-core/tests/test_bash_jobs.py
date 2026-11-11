"""Bounded foreground bash, plus daemon background jobs."""
import asyncio
import os
import signal

import pytest

from nexus.bash_jobs import Job, Jobs
from nexus.tools import ToolContext, ToolError
from nexus.tools.bash import report
from nexus.tools.bash import run as bash


async def test_foreground_bash_is_bounded_and_accepts_an_explicit_timeout(tmp_path, monkeypatch):
    from nexus.tools import bash as module

    ctx = ToolContext(tmp_path)
    assert (await bash({"command": "printf hi"}, ctx)).startswith("[exit code 0]\nhi")
    started = asyncio.get_running_loop().time()
    assert "killed after 0.1s timeout" in await bash({"command": "sleep 5", "timeout": 0.1}, ctx)
    assert asyncio.get_running_loop().time() - started < 2
    for bad in (0, -1, True, "2", float("inf"), 601):
        with pytest.raises(ToolError, match="timeout"):
            await bash({"command": "true", "timeout": bad}, ctx)
    with pytest.raises(ToolError, match="requires the daemon"):
        await bash({"command": "true", "background": True}, ctx)
    with pytest.raises(ToolError, match="boolean"):
        await bash({"command": "true", "background": "false"}, ctx)
    monkeypatch.setattr(module, "DEFAULT_FOREGROUND_TIMEOUT", 0.05)
    assert "killed after 0.05s timeout" in await bash({"command": "sleep 5"}, ctx)
    assert module.timeout_value(None) is None


async def test_job_output_finish_timeout_kill_and_tree_scope(tmp_path):
    jobs = Jobs()
    finished: list[Job] = []
    job = await jobs.start(session_id="ses", root="root", command="printf hello; sleep 0.2; printf world",
                           directory=tmp_path, timeout=None, on_finish=finished.append)
    assert job.status == "running"
    await asyncio.sleep(0.5)
    assert job.status == "done" and job.output == "helloworld" and finished == [job]
    assert jobs.get(job.id, root="root") is job
    with pytest.raises(ToolError, match="no bash job"):
        jobs.get(job.id, root="another-tree")
    first = report(job, {"offset": 0, "limit": 5})
    assert "hello" in first and "showing 0–5 of 10" in first and "5 more characters" in first
    assert "world" in report(job, {"offset": 5, "limit": 5})
    for bad in ({"limit": 0}, {"limit": 30_001}, {"offset": -1}, {"offset": True}):
        with pytest.raises(ToolError):
            report(job, bad)

    slow = await jobs.start(session_id="ses", root="root", command="printf before; sleep 5",
                            directory=tmp_path, timeout=0.15, on_finish=finished.append)
    await asyncio.sleep(0.5)
    assert slow.status == "timeout" and slow.output == "before" and slow.exit_code is not None

    killed = await jobs.start(session_id="ses", root="root", command="sleep 5", directory=tmp_path,
                              timeout=None, on_finish=finished.append)
    await jobs.kill(killed.id, root="root")
    assert killed.status == "killed"
    await jobs.shutdown()


async def test_jobs_are_cancelled_with_their_session_and_not_the_whole_tree(tmp_path):
    jobs = Jobs()
    finished: list[Job] = []
    mine = await jobs.start(session_id="child", root="root", command="sleep 5", directory=tmp_path,
                            timeout=None, on_finish=finished.append)
    sibling = await jobs.start(session_id="sibling", root="root", command="sleep 5", directory=tmp_path,
                               timeout=None, on_finish=finished.append)
    await jobs.cancel_for({"child"})
    assert mine.status == "killed" and sibling.status == "running"
    assert finished == []  # cancellation for a deleted session is silent
    await jobs.shutdown()
    assert sibling.status == "killed" and finished == []


async def test_concurrent_starts_respect_live_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.bash_jobs.MAX_LIVE", 1)
    jobs = Jobs()
    try:
        results = await asyncio.gather(*(
            jobs.start(session_id="ses", root="root", command="sleep 30",
                       directory=tmp_path, timeout=None, on_finish=lambda _: None)
            for _ in range(3)
        ), return_exceptions=True)
        assert sum(isinstance(result, Job) for result in results) == 1
        assert sum(isinstance(result, ToolError) for result in results) == 2
        assert len(jobs.items) == 1
    finally:
        await jobs.shutdown()


async def test_timeout_kills_descendant_after_shell_exits(tmp_path):
    jobs = Jobs()
    try:
        job = await jobs.start(session_id="ses", root="root", command="sleep 30 &",
                               directory=tmp_path, timeout=0.2, on_finish=lambda _: None)
        await asyncio.wait_for(asyncio.shield(job.task), timeout=2)
        assert job.timed_out and job.finished is not None
    finally:
        await jobs.shutdown()


async def test_job_decodes_utf8_across_reads(tmp_path):
    jobs = Jobs()
    try:
        job = await jobs.start(session_id="ses", root="root",
                               command="printf '\\342'; sleep 0.1; printf '\\202\\254'",
                               directory=tmp_path, timeout=None, on_finish=lambda _: None)
        await asyncio.wait_for(asyncio.shield(job.task), timeout=2)
        assert job.output == "€"
    finally:
        await jobs.shutdown()


async def test_terminal_job_control_output_and_scope(tmp_path):
    from nexus.runtime import Conflict
    from nexus.terminals import TerminalService

    jobs = Jobs()
    service = TerminalService()
    jobs.terminal_service = service
    finished = []
    try:
        job = await jobs.start(session_id="s", root="root", project_id="p",
                               command="printf ready; read value; printf 'result:%s' \"$value\"",
                               directory=tmp_path, timeout=3, on_finish=finished.append)
        term = job.terminal
        assert term is not None and job.json()["terminal_id"] == term.id
        assert service.list("other") == []
        assert service.list("p")[0]["background_job_id"] == job.id
        assert term.session_id == "s" and term.workspace == str(tmp_path)
        with pytest.raises(ToolError):
            jobs.get(job.id, root="other")
        a, b = term.attach("a"), term.attach("b")
        term.attachments[a].connected = term.attachments[b].connected = True
        term.control(a, "acquire")
        with pytest.raises(Conflict):
            term.control(b, "acquire")
        term.input(a, 1, b"hello\n")
        await asyncio.wait_for(asyncio.shield(job.task), 4)
        assert "ready" in job.output and "result:hello" in job.output
        assert finished == [job] and job.status == "done"
    finally:
        await jobs.shutdown()
        await service.shutdown()


@pytest.mark.parametrize("action", ["kill", "timeout", "close"])
async def test_terminal_job_completion_once(tmp_path, action):
    from nexus.terminals import TerminalService

    jobs = Jobs()
    service = TerminalService()
    jobs.terminal_service = service
    finished = []
    try:
        job = await jobs.start(session_id="s", root="r", project_id="p", command="printf early; sleep 30",
                               directory=tmp_path, timeout=0.2 if action == "timeout" else None,
                               on_finish=finished.append)
        for _ in range(200):
            if "early" in job.output:
                break
            await asyncio.sleep(0.01)
        assert "early" in job.output
        if action == "kill":
            await jobs.kill(job.id, root="r")
        elif action == "close":
            await job.terminal.close()
        await asyncio.wait_for(asyncio.shield(job.task), 3)
        assert finished == [job]
        assert job.status == {"kill": "killed", "timeout": "timeout", "close": "killed"}[action]
        assert job.terminal.status in {"closed", "exited"}
    finally:
        await jobs.shutdown()
        await service.shutdown()


async def test_terminal_job_output_is_independent_of_replay(tmp_path, monkeypatch):
    from nexus.terminals import TerminalService

    monkeypatch.setattr("nexus.terminals.RING_BYTES", 128)
    monkeypatch.setattr("nexus.bash_jobs.MAX_OUTPUT", 300)
    jobs = Jobs()
    service = TerminalService()
    jobs.terminal_service = service
    try:
        job = await jobs.start(session_id="s", root="r", project_id="p",
                               command="printf '\\342\\202\\254'; printf '%0500d' 0",
                               directory=tmp_path, timeout=3, on_finish=lambda _: None)
        await asyncio.wait_for(asyncio.shield(job.task), 4)
        assert job.output.startswith("€") and len(job.output) == 300 and job.dropped == 201
        assert job.terminal.discarded > 0
    finally:
        await jobs.shutdown()
        await service.shutdown()


async def test_many_finished_jobs_do_not_exhaust_live_terminal_limit(tmp_path, monkeypatch):
    from nexus.terminals import TerminalService

    monkeypatch.setattr("nexus.terminals.MAX_TERMINALS", 2)
    monkeypatch.setattr("nexus.terminals.MAX_RETAINED_TERMINALS", 3)
    jobs = Jobs()
    service = TerminalService()
    jobs.terminal_service = service
    try:
        for _ in range(8):
            job = await jobs.start(session_id="s", root="r", project_id="p", command="printf done",
                                   directory=tmp_path, timeout=3, on_finish=lambda _: None)
            await asyncio.wait_for(asyncio.shield(job.task), 4)
            assert job.output == "done"
        assert len(service.terminals) == 3
        assert len(service.list("p")) <= 6
    finally:
        await jobs.shutdown()
        await service.shutdown()


async def test_shutdown_with_detached_descendant_holding_pty(tmp_path):
    from nexus.terminals import TerminalService

    jobs = Jobs()
    service = TerminalService()
    jobs.terminal_service = service
    pid_file = tmp_path / "pid"
    command = ("python3 -c 'import os,time; p=os.fork(); "
               "(os.setsid(), open(\"pid\",\"w\").write(str(os.getpid())), time.sleep(30)) if p == 0 else None'")
    try:
        job = await jobs.start(session_id="s", root="r", project_id="p", command=command,
                               directory=tmp_path, timeout=None, on_finish=lambda _: None)
        async with asyncio.timeout(5):
            while not pid_file.exists() or not pid_file.read_text():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(jobs.shutdown(), 4)
        assert job.finished is not None
    finally:
        if pid_file.exists() and pid_file.read_text():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        await service.shutdown()
