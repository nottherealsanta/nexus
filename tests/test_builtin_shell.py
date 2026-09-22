"""Integration tests for the shell/job-control built-ins (plan section 5.3).

These drive real, harmless subprocesses through the public ``run`` coroutines
and the owning :class:`JobRegistry`. Everything runs in ``tmp_path``; no test
reaches outside its temporary workspace. Determinism is preferred over speed
where the two conflict (short sleeps, bounded polling).
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.model.message import Text
from nexus.tools.builtin import _jobs, bash, bash_output, kill_shell
from nexus.tools.spec import ToolContext

JOB_ID_RE = re.compile(r"job_id: (job_[0-9a-f]+)")
STDOUT_NEXT_RE = re.compile(r"stdout_offset: \d+ -> (\d+)")
STDERR_NEXT_RE = re.compile(r"stderr_offset: \d+ -> (\d+)")
CHILD_PID_RE = re.compile(r"CHILD:(\d+)")


def body(result) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(block, Text)
    )


def job_id_of(result) -> str:
    match = JOB_ID_RE.search(body(result))
    assert match is not None, body(result)
    return match.group(1)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_until_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return not pid_alive(pid)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        turn_id="turn-test",
        config=Config(),
    )


@pytest.fixture
async def registry():
    reg = _jobs.JobRegistry()
    _jobs.set_default_registry(reg)
    try:
        yield reg
    finally:
        await reg.aclose()
        _jobs.set_default_registry(None)


# ---------------------------------------------------------------------------
# Foreground execution
# ---------------------------------------------------------------------------


async def test_stdout_and_stderr_are_separate_and_deterministic(
    registry, ctx
):
    result = await bash.run({"command": "printf hello; printf oops 1>&2"}, ctx)
    assert result.is_error is False
    text = body(result)
    assert "exit_code: 0" in text
    assert "stdout:\nhello" in text
    assert "stderr:\noops" in text


async def test_cwd_is_the_canonical_workspace(registry, ctx, workspace):
    result = await bash.run({"command": "pwd"}, ctx)
    assert result.is_error is False
    assert str(workspace) in body(result)


async def test_nonzero_exit_is_a_model_visible_result(registry, ctx):
    result = await bash.run({"command": "exit 7"}, ctx)
    assert result.is_error is True
    text = body(result)
    assert "exit_code: 7" in text
    assert "status: failed" in text


async def test_high_volume_streams_do_not_deadlock(registry, ctx):
    command = "yes A | head -c 3000000; yes B | head -c 3000000 1>&2"
    result = await bash.run({"command": command}, ctx)
    assert result.is_error is False
    job = registry.jobs(admin=True)[0]
    assert job.stdout.total == 3_000_000
    assert job.stderr.total == 3_000_000
    assert job.stdout.truncated is True
    assert job.stderr.truncated is True
    assert len(job.stdout) == _jobs.DEFAULT_OUTPUT_LIMIT


async def test_env_overlay_is_applied(registry, ctx):
    result = await bash.run(
        {"command": 'printf "%s" "$NEXUS_TEST_VAR"', "env": {"NEXUS_TEST_VAR": "ok"}},
        ctx,
    )
    assert body(result).endswith("ok")


async def test_process_is_reaped_after_completion(registry, ctx):
    result = await bash.run({"command": "true"}, ctx)
    assert result.is_error is False
    job = registry.jobs(admin=True)[0]
    assert job.done is True
    assert job.exit_code == 0
    assert job.pid is not None
    assert pid_alive(job.pid) is False


# ---------------------------------------------------------------------------
# Timeouts and cancellation
# ---------------------------------------------------------------------------


async def test_timeout_terminates_process_group(registry, ctx):
    started = time.monotonic()
    result = await bash.run(
        {"command": "sleep 30", "timeout_s": 0.3}, ctx
    )
    elapsed = time.monotonic() - started
    assert result.is_error is True
    assert "timed out after 0.3s" in body(result)
    assert "status: timed_out" in body(result)
    assert elapsed < 10
    assert registry.active(admin=True) == ()


async def test_timeout_kills_descendants(registry, ctx):
    command = "sleep 30 & child=$!; echo CHILD:$child; wait"
    result = await bash.run(
        {"command": command, "timeout_s": 0.4}, ctx
    )
    match = CHILD_PID_RE.search(body(result))
    assert match is not None, body(result)
    child = int(match.group(1))
    assert await wait_until_dead(child) is True


async def test_external_cancellation_cleans_up(registry, workspace):
    token = CancelToken()
    ctx = ToolContext(
        workspace=workspace,
        session_id="s",
        turn_id="t",
        config=Config(),
        cancel_token=token,
    )
    task = asyncio.create_task(bash.run({"command": "sleep 30"}, ctx))
    await asyncio.sleep(0.2)
    assert len(registry.active(admin=True)) == 1
    token.cancel("stop")
    with pytest.raises(OperationCancelled):
        await task
    assert registry.active(admin=True) == ()


async def test_task_cancellation_cleans_up(registry, ctx):
    task = asyncio.create_task(bash.run({"command": "sleep 30"}, ctx))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.active(admin=True) == ()


# ---------------------------------------------------------------------------
# Background jobs, polling, killing
# ---------------------------------------------------------------------------


async def test_background_job_polling_and_incremental_reads(registry, ctx):
    started = await bash.run(
        {"command": "sleep 0.2; echo finished", "run_in_background": True},
        ctx,
    )
    assert started.is_error is False
    assert "status: running" in body(started)
    job_id = job_id_of(started)

    job = registry.job(job_id, admin=True)
    await job.wait(5)
    assert job.done is True

    full = await bash_output.run({"job_id": job_id}, ctx)
    assert full.is_error is False
    text = body(full)
    assert "status: completed" in text
    assert "finished" in text

    stdout_next = int(STDOUT_NEXT_RE.search(text).group(1))
    stderr_next = int(STDERR_NEXT_RE.search(text).group(1))
    again = await bash_output.run(
        {
            "job_id": job_id,
            "stdout_offset": stdout_next,
            "stderr_offset": stderr_next,
        },
        ctx,
    )
    assert "no new output" in body(again)


async def test_bash_output_wait_s_blocks_for_new_output(registry, ctx):
    started = await bash.run(
        {"command": "sleep 0.4; echo late", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    polled = await bash_output.run({"job_id": job_id, "wait_s": 5}, ctx)
    assert "late" in body(polled)


async def test_kill_shell_terminates_and_is_idempotent(registry, ctx):
    started = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    killed = await kill_shell.run({"job_id": job_id}, ctx)
    assert killed.is_error is False
    assert "terminated" in body(killed)
    assert registry.job(job_id, admin=True).done is True

    second = await kill_shell.run({"job_id": job_id}, ctx)
    assert second.is_error is False
    assert "already finished" in body(second)


async def test_kill_shell_rejects_unknown_and_raw_pids(registry, ctx):
    unknown = await kill_shell.run({"job_id": "job_000000000000"}, ctx)
    assert unknown.is_error is True
    assert "unknown job_id" in body(unknown)

    raw_pid = await kill_shell.run({"job_id": "12345"}, ctx)
    assert raw_pid.is_error is True


async def test_bash_output_rejects_unknown_and_raw_pids(registry, ctx):
    unknown = await bash_output.run({"job_id": "job_000000000000"}, ctx)
    assert unknown.is_error is True
    raw_pid = await bash_output.run({"job_id": "4321"}, ctx)
    assert raw_pid.is_error is True


# ---------------------------------------------------------------------------
# Bounds and cleanup
# ---------------------------------------------------------------------------


async def test_output_truncation_is_explicit(workspace, ctx):
    reg = _jobs.JobRegistry(output_limit=64)
    _jobs.set_default_registry(reg)
    try:
        result = await bash.run(
            {"command": "yes x | head -c 1000"}, ctx
        )
        job = reg.jobs(admin=True)[0]
        assert job.stdout.truncated is True
        assert len(job.stdout) == 64
        assert job.stdout.dropped == 1000 - 64
        assert "truncated" in body(result)
    finally:
        await reg.aclose()
        _jobs.set_default_registry(None)


async def test_cleanup_all_terminates_owned_jobs(workspace, ctx):
    reg = _jobs.JobRegistry()
    _jobs.set_default_registry(reg)
    try:
        for _ in range(2):
            await bash.run(
                {"command": "sleep 30", "run_in_background": True}, ctx
            )
        assert len(reg.active(admin=True)) == 2
        await reg.aclose()
        assert reg.active(admin=True) == ()
        assert reg.closed is True
    finally:
        _jobs.set_default_registry(None)


async def test_close_default_registry_seam(workspace, ctx):
    reg = _jobs.JobRegistry()
    _jobs.set_default_registry(reg)
    await bash.run({"command": "sleep 30", "run_in_background": True}, ctx)
    assert len(reg.active(admin=True)) == 1
    await _jobs.close_default_registry()
    assert reg.active(admin=True) == ()
    assert _jobs.get_default_registry() is not reg


async def test_invalid_arguments_are_model_visible_errors(registry, ctx):
    empty = await bash.run({"command": "  "}, ctx)
    assert empty.is_error is True

    bad_timeout = await bash.run(
        {"command": "true", "timeout_s": 0}, ctx
    )
    assert bad_timeout.is_error is True

    bad_env = await bash.run(
        {"command": "true", "env": {"OK": 5}}, ctx
    )
    assert bad_env.is_error is True

    started = await bash.run(
        {"command": "sleep 5", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    bad_offset = await bash_output.run(
        {"job_id": job_id, "stdout_offset": -1}, ctx
    )
    assert bad_offset.is_error is True
    bad_wait = await bash_output.run({"job_id": job_id, "wait_s": 0}, ctx)
    assert bad_wait.is_error is True


# ---------------------------------------------------------------------------
# Contracts: specs, permission keys, registry seam
# ---------------------------------------------------------------------------


def test_specs_and_permission_keys():
    assert bash.SPEC.name == "Bash"
    assert bash.SPEC.bundle == "shell"
    assert bash.SPEC.mutates is True
    assert bash.SPEC.resolve_permission_key({"command": "git status"}) == "git status"

    assert bash_output.SPEC.name == "BashOutput"
    assert bash_output.SPEC.bundle == "shell"
    assert bash_output.SPEC.mutates is False
    assert bash_output.SPEC.resolve_permission_key({"job_id": "job_x"}) == "job_x"

    assert kill_shell.SPEC.name == "KillShell"
    assert kill_shell.SPEC.bundle == "shell"
    assert kill_shell.SPEC.resolve_permission_key({"job_id": "job_x"}) == "job_x"


def test_registry_resolution_order():
    explicit_reg = _jobs.JobRegistry()
    default_reg = _jobs.JobRegistry()
    bound_reg = _jobs.JobRegistry()

    class Plain:
        pass

    class WithField:
        job_registry = explicit_reg

    _jobs.set_default_registry(default_reg)
    try:
        assert _jobs.registry_for(Plain()) is default_reg
        assert _jobs.registry_for(WithField()) is explicit_reg
        token = _jobs.bind_registry(bound_reg)
        try:
            assert _jobs.registry_for(Plain()) is bound_reg
            assert _jobs.registry_for(WithField()) is explicit_reg
        finally:
            _jobs.reset_registry(token)
        assert _jobs.registry_for(Plain()) is default_reg
    finally:
        _jobs.set_default_registry(None)


async def test_await_job_timeout_marks_timed_out(registry, workspace):
    job = await registry.spawn("sleep 30", cwd=workspace)
    outcome = await _jobs.await_job(job, timeout=0.2)
    assert outcome == "timeout"
    assert job.status is _jobs.JobStatus.TIMED_OUT
