"""Foreground bash without a default limit, plus daemon background jobs."""
import asyncio

import pytest

from nexus.bash_jobs import Job, Jobs
from nexus.tools import ToolContext, ToolError
from nexus.tools.bash import report
from nexus.tools.bash import run as bash


async def test_foreground_bash_has_no_default_timeout_and_accepts_one(tmp_path):
    ctx = ToolContext(tmp_path)
    assert (await bash({"command": "printf hi"}, ctx)).startswith("[exit code 0]\nhi")
    started = asyncio.get_running_loop().time()
    assert "killed after 0.1s timeout" in await bash({"command": "sleep 5", "timeout": 0.1}, ctx)
    assert asyncio.get_running_loop().time() - started < 2
    for bad in (0, -1, True, "2", float("inf")):
        with pytest.raises(ToolError, match="timeout"):
            await bash({"command": "true", "timeout": bad}, ctx)
    with pytest.raises(ToolError, match="requires the daemon"):
        await bash({"command": "true", "background": True}, ctx)
    with pytest.raises(ToolError, match="boolean"):
        await bash({"command": "true", "background": "false"}, ctx)


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
