"""Bounded foreground bash, plus daemon background jobs."""
import asyncio
import subprocess

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


async def test_foreground_bash_reports_failures_a_pipeline_hides(tmp_path):
    ctx = ToolContext(tmp_path)
    out = await bash({"command": "printf 'x\\n' | grep zzz | cat"}, ctx)  # grep fails in the middle
    assert out.startswith("[exit code 0 · pipeline exit codes 0 1 0, no output]")
    out = await bash({"command": "nexus-no-such-cmd --check 2>&1 | tail -3"}, ctx)
    assert out.startswith("[exit code 0 · pipeline exit codes 127 0 · command not found: nexus-no-such-cmd]")
    out = await bash({"command": "nexus-no-such-cmd 2>&1 | head -1; echo done"}, ctx)  # not the last pipeline
    assert out.startswith("[exit code 0 · command not found: nexus-no-such-cmd]")
    # Normal cases stay plain: SIGPIPE from an early-closing reader, single commands and real failures.
    assert (await bash({"command": "yes | head -1"}, ctx)).startswith("[exit code 0]\ny")
    assert (await bash({"command": "false"}, ctx)).startswith("[exit code 1, no output]")
    assert (await bash({"command": "false | true; exit 3"}, ctx)).startswith("[exit code 3, no output]")
    # Bash's own error text, line numbers included, is unchanged by the status trap.
    script = "echo a\necho b\nnexus-no-such-cmd"
    plain = (await asyncio.to_thread(subprocess.run, ["/bin/bash", "-c", script], check=False,
                                     capture_output=True, text=True, cwd=tmp_path)).stderr
    assert (await bash({"command": script}, ctx)).endswith(plain)
    assert not list(tmp_path.iterdir())  # nothing left in the workspace


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


async def test_background_job_uses_pipes_not_a_terminal(tmp_path):
    jobs = Jobs()
    finished = []
    try:
        job = await jobs.start(session_id="s", root="root",
                               command="printf ready; read value || printf eof; echo err >&2",
                               directory=tmp_path, timeout=3, on_finish=finished.append)
        await asyncio.wait_for(asyncio.shield(job.task), 4)
        assert job.output == "readyeoferr\n"
        assert finished == [job] and job.status == "done"
        assert "terminal_id" not in job.json()
        with pytest.raises(ToolError):
            jobs.get(job.id, root="other")
    finally:
        await jobs.shutdown()


@pytest.mark.parametrize("action", ["kill", "timeout"])
async def test_job_completion_once(tmp_path, action):
    jobs = Jobs()
    finished = []
    try:
        job = await jobs.start(session_id="s", root="r", command="printf early; sleep 30",
                               directory=tmp_path, timeout=0.2 if action == "timeout" else None,
                               on_finish=finished.append)
        for _ in range(200):
            if "early" in job.output:
                break
            await asyncio.sleep(0.01)
        assert "early" in job.output
        if action == "kill":
            await jobs.kill(job.id, root="r")
        await asyncio.wait_for(asyncio.shield(job.task), 3)
        assert finished == [job]
        assert job.status == {"kill": "killed", "timeout": "timeout"}[action]
    finally:
        await jobs.shutdown()


async def test_job_output_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.bash_jobs.MAX_OUTPUT", 300)
    jobs = Jobs()
    try:
        job = await jobs.start(session_id="s", root="r",
                               command="printf '\\342\\202\\254'; printf '%0500d' 0",
                               directory=tmp_path, timeout=3, on_finish=lambda _: None)
        await asyncio.wait_for(asyncio.shield(job.task), 4)
        assert job.output.startswith("€") and len(job.output) == 300 and job.dropped == 201
    finally:
        await jobs.shutdown()


async def test_job_records_its_turn_and_tool_call(tmp_path):
    jobs = Jobs()
    try:
        job = await jobs.start(session_id="s", root="r", command="true", directory=tmp_path, timeout=3,
                               on_finish=lambda _: None, turn_id="turn_1", tool_call_id="call_1")
        await asyncio.wait_for(asyncio.shield(job.task), 4)
        assert job.json()["turn_id"] == "turn_1" and job.json()["tool_call_id"] == "call_1"
    finally:
        await jobs.shutdown()
