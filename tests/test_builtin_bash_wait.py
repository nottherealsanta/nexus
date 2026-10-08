"""Yield-instead-of-kill and wait-until-exit behavior of the unified ``bash`` tool.

Plan: plans/BASH_WAIT_PLAN.md (options A + B).
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ToolsSection
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.model.message import Text
from nexus.tools.builtin import _jobs, bash, bash_output
from nexus.tools.spec import ToolContext

JOB_ID_RE = re.compile(r"job_id: (job_[0-9a-f]+)")


def body(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


def job_id_of(result) -> str:
    match = JOB_ID_RE.search(body(result))
    assert match is not None, body(result)
    return match.group(1)


def make_ctx(workspace: Path, *, yield_s: float = 0.4, max_s: float = 60, **kw):
    config = Config(
        v2=ConfigV2(tools=ToolsSection(bash_yield_s=yield_s, bash_max_s=max_s))
    )
    return ToolContext(
        workspace=workspace, session_id="wait", turn_id="t", config=config, **kw
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def ctx(workspace):
    return make_ctx(workspace)


@pytest.fixture
async def registry():
    value = _jobs.JobRegistry()
    _jobs.set_default_registry(value)
    try:
        yield value
    finally:
        await value.aclose()
        _jobs.set_default_registry(None)


def alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- A: yield ---------------------------------------------------------------


async def test_run_yields_a_still_running_job_instead_of_killing(registry, ctx):
    result = await bash.run({"command": "echo started; sleep 1.5; echo done"}, ctx)
    text = body(result)
    assert result.is_error is False
    assert "status: running" in text and "moved to background" in text
    assert "started" in text
    job = registry.job(job_id_of(result), session_id=ctx.session_id)
    assert job is not None and not job.done and job.background
    assert alive(job.pid)
    await job.wait(10)
    assert job.exit_code == 0


async def test_fast_command_does_not_yield(registry, ctx):
    result = await bash.run({"command": "echo quick"}, ctx)
    assert "exit_code: 0" in body(result) and "moved to background" not in body(result)


async def test_timeout_below_yield_window_still_kills(registry, workspace):
    ctx = make_ctx(workspace, yield_s=5)
    result = await bash.run({"command": "sleep 30", "timeout_s": 0.3}, ctx)
    assert result.is_error is True and "timed out" in body(result)
    job = registry.jobs(session_id=ctx.session_id)[0]
    assert job.status is _jobs.JobStatus.TIMED_OUT and not alive(job.pid)


async def test_timeout_above_yield_window_is_a_hard_limit_after_yielding(
    registry, workspace
):
    ctx = make_ctx(workspace, yield_s=0.2)
    result = await bash.run({"command": "sleep 30", "timeout_s": 0.8}, ctx)
    job = registry.job(job_id_of(result), session_id=ctx.session_id)
    assert "moved to background" in body(result)
    await job.wait(10)
    assert job.status is _jobs.JobStatus.TIMED_OUT and not alive(job.pid)


async def test_timeout_s_is_capped_at_bash_max_s(registry, workspace):
    ctx = make_ctx(workspace, yield_s=0.2, max_s=0.8)
    result = await bash.run({"command": "sleep 30", "timeout_s": 600}, ctx)
    job = registry.job(job_id_of(result), session_id=ctx.session_id)
    await job.wait(10)
    assert job.status is _jobs.JobStatus.TIMED_OUT


async def test_background_job_is_bounded_by_bash_max_s(registry, workspace):
    ctx = make_ctx(workspace, max_s=0.5)
    started = await bash.run({"command": "sleep 30", "run_in_background": True}, ctx)
    job = registry.job(job_id_of(started), session_id=ctx.session_id)
    await job.wait(10)
    assert job.status is _jobs.JobStatus.TIMED_OUT


async def test_cancel_during_blocking_window_kills_the_group(registry, workspace):
    token = CancelToken()
    ctx = make_ctx(workspace, yield_s=30, cancel_token=token)
    task = asyncio.ensure_future(bash.run({"command": "sleep 30"}, ctx))
    await asyncio.sleep(0.3)
    token.cancel("stop")
    with pytest.raises(OperationCancelled):
        await task
    job = registry.jobs(session_id=ctx.session_id)[0]
    assert job.done and not alive(job.pid)


# -- B: wait ----------------------------------------------------------------


async def test_wait_until_exit_returns_only_after_exit(registry, ctx):
    started = await bash.run(
        {"command": "echo first; sleep 1; echo last", "run_in_background": True}, ctx
    )
    t0 = time.monotonic()
    waited = await bash.run({"action": "wait", "job_id": job_id_of(started)}, ctx)
    assert time.monotonic() - t0 >= 0.8
    text = body(waited)
    assert "status: completed" in text and "last" in text


async def test_wait_until_exit_respects_wait_s_and_reports_running(registry, ctx):
    started = await bash.run({"command": "sleep 30", "run_in_background": True}, ctx)
    waited = await bash.run(
        {"action": "wait", "job_id": job_id_of(started), "wait_s": 0.3}, ctx
    )
    assert "status: running" in body(waited)
    await bash.run({"action": "stop", "job_id": job_id_of(started)}, ctx)


async def test_wait_until_output_returns_on_new_output(registry, ctx):
    started = await bash.run(
        {"command": "sleep 0.3; echo hi; sleep 30", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    t0 = time.monotonic()
    waited = await bash.run(
        {"action": "wait", "job_id": job_id, "until": "output", "wait_s": 10}, ctx
    )
    assert time.monotonic() - t0 < 5
    assert "hi" in body(waited) and "status: running" in body(waited)
    await bash.run({"action": "stop", "job_id": job_id}, ctx)


async def test_read_cursor_never_repeats_output(registry, ctx):
    started = await bash.run(
        {"command": "echo one; sleep 0.5; echo two", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    first = await bash.run(
        {"action": "wait", "job_id": job_id, "until": "output", "wait_s": 5}, ctx
    )
    assert "one" in body(first)
    second = await bash.run({"action": "wait", "job_id": job_id}, ctx)
    assert "two" in body(second) and "one" not in body(second).split("stdout:")[-1]
    third = await bash.run({"action": "status", "job_id": job_id}, ctx)
    assert "(no new output)" in body(third)
    # Explicit offsets override the cursor.
    full = await bash.run(
        {"action": "status", "job_id": job_id, "stdout_offset": 0}, ctx
    )
    assert "one" in body(full) and "two" in body(full)


async def test_yielded_output_is_not_repeated_by_wait(registry, ctx):
    result = await bash.run({"command": "echo early; sleep 1; echo late"}, ctx)
    assert "early" in body(result)
    waited = await bash.run({"action": "wait", "job_id": job_id_of(result)}, ctx)
    assert "late" in body(waited) and "early" not in body(waited)


async def test_cancel_during_wait_leaves_the_job_running(registry, workspace):
    token = CancelToken()
    ctx = make_ctx(workspace, cancel_token=token)
    started = await bash.run({"command": "sleep 30", "run_in_background": True}, ctx)
    job_id = job_id_of(started)
    task = asyncio.ensure_future(bash.run({"action": "wait", "job_id": job_id}, ctx))
    await asyncio.sleep(0.3)
    token.cancel("stop")
    with pytest.raises(OperationCancelled):
        await task
    job = registry.job(job_id, session_id=ctx.session_id)
    assert not job.done and alive(job.pid)
    await job.terminate()


async def test_legacy_bash_output_keeps_output_semantics(registry, ctx):
    started = await bash.run(
        {"command": "echo a; sleep 30", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    await asyncio.sleep(0.3)
    result = await bash_output.run({"job_id": job_id, "wait_s": 5}, ctx)
    assert "a" in body(result) and "status: running" in body(result)
    again = await bash_output.run({"job_id": job_id}, ctx)
    assert "a" in body(again)  # no cursor: offset 0 each time
    await bash.run({"action": "stop", "job_id": job_id}, ctx)


# -- output and progress ----------------------------------------------------


async def test_truncation_keeps_head_and_tail_and_saves_the_rest(
    registry, workspace, tmp_path, monkeypatch
):
    monkeypatch.setenv("NEXUS_OUTPUT_DIR", str(tmp_path / "spill"))
    job = await registry.spawn("seq 1 20000; echo SUMMARY-LINE", cwd=workspace)
    await job.wait(10)
    text = _jobs.format_job_output(job)
    assert "[output too large: 20002 lines" in text
    assert "\n1\n" in text and "SUMMARY-LINE" in text
    assert len(text.splitlines()) <= 2000 + 5
    assert len(text.encode()) <= 50 * 1024 + 512
    saved = list((tmp_path / "spill").iterdir())
    assert len(saved) == 1 and str(saved[0]) in text
    full = saved[0].read_text()
    assert "\n12345\n" in full and full.rstrip().endswith("SUMMARY-LINE")


async def test_progress_is_throttled_and_reports_last_line(registry, workspace):
    events: list[tuple[str, dict]] = []
    ctx = make_ctx(workspace, yield_s=30, emit=lambda t, d=None: events.append((t, d)))
    orig = _jobs.PROGRESS_INTERVAL_S
    # tick_s is bound at definition; drive await_job with a fast tick via tool.
    job = await registry.spawn(
        "for i in 1 2 3 4 5 6; do echo line-$i; sleep 0.2; done", cwd=workspace
    )
    tick = bash_output._progress_tick(job, ctx)
    await _jobs.await_job(job, tick=tick, tick_s=0.25)
    texts = [d["text"] for t, d in events if t == "tool.progress"]
    assert texts and texts[-1].startswith("line-")
    assert len(texts) == len(set(texts))  # never repeats a line
    assert orig == _jobs.PROGRESS_INTERVAL_S


async def test_progress_is_capped(registry, workspace, monkeypatch):
    events: list = []
    ctx = make_ctx(workspace, emit=lambda t, d=None: events.append(t))
    monkeypatch.setattr(_jobs, "MAX_PROGRESS_EVENTS", 3)
    job = await registry.spawn("sleep 30", cwd=workspace)
    tick = bash_output._progress_tick(job, ctx)
    for i in range(10):
        async with job._condition:
            job.stdout.append(f"l{i}\n".encode())
        await tick()
    assert len(events) == 3
    await job.terminate()


def test_spec_guides_the_model_to_wait_once():
    desc = bash.SPEC.description
    assert "action=wait" in desc and "Don't poll" in desc
    assert bash.SPEC.input_schema["properties"]["until"]["default"] == "exit"
