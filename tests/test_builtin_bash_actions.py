"""Focused contract tests for the unified bash action interface."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.model.message import Text
from nexus.tools.builtin import _jobs, bash, bash_output, kill_shell
from nexus.tools.spec import ToolContext

JOB_ID_RE = re.compile(r"job_id: (job_[0-9a-f]+)")


def body(result) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(block, Text)
    )


def job_id_of(result) -> str:
    match = JOB_ID_RE.search(body(result))
    assert match is not None, body(result)
    return match.group(1)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="bash-actions",
        turn_id="turn",
        config=Config(),
    )


@pytest.fixture
async def registry():
    value = _jobs.JobRegistry()
    _jobs.set_default_registry(value)
    try:
        yield value
    finally:
        await value.aclose()
        _jobs.set_default_registry(None)


def test_schema_defaults_to_run_and_exposes_actions():
    schema = bash.SPEC.input_schema
    props = schema["properties"]
    assert props["action"]["enum"] == ["run", "status", "wait", "stop"]
    assert props["action"]["default"] == "run"
    assert "required" not in schema
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("data", [{"command": "true"}, {"action": "run", "command": "true"}])
async def test_run_defaults_and_explicit_action_execute_command(registry, ctx, data):
    result = await bash.run(data, ctx)
    assert result.is_error is False
    assert "exit_code: 0" in body(result)
    job = registry.jobs(session_id=ctx.session_id)[0]
    assert job.cwd == ctx.workspace.resolve()
    assert job.shell is None


async def test_explicit_allowlisted_shell_runs_without_arguments(registry, ctx):
    result = await bash.run(
        {"command": "printf shell-ok", "shell": "/bin/sh"}, ctx
    )
    assert result.is_error is False
    assert "shell-ok" in body(result)
    assert registry.jobs(session_id=ctx.session_id)[0].shell == "/bin/sh"


async def test_run_workdir_resolves_nested_directory(registry, ctx, workspace):
    nested = workspace / "nested" / "child"
    nested.mkdir(parents=True)
    result = await bash.run({"command": "pwd", "workdir": "nested/child"}, ctx)
    assert result.is_error is False
    assert str(nested) in body(result)
    assert registry.jobs(session_id=ctx.session_id)[0].cwd == nested


@pytest.mark.parametrize("workdir", ["../outside", "missing", "file"])
async def test_run_rejects_escape_missing_or_non_directory_workdir(
    registry, ctx, workspace, workdir
):
    (workspace / "file").write_text("not a directory")
    result = await bash.run(
        {"command": "touch should-not-run", "workdir": workdir}, ctx
    )
    assert result.is_error is True
    assert registry.jobs(session_id=ctx.session_id) == ()
    assert not (workspace / "should-not-run").exists()


async def test_run_rejects_symlink_escape(registry, ctx, workspace, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    result = await bash.run(
        {"command": "touch should-not-run", "workdir": "escape"}, ctx
    )
    assert result.is_error is True
    assert registry.jobs(session_id=ctx.session_id) == ()
    assert not (workspace / "should-not-run").exists()


async def test_run_rechecks_workdir_immediately_before_spawn(
    registry, ctx, workspace, tmp_path, monkeypatch
):
    inside = workspace / "inside"
    inside.mkdir()
    link = workspace / "selected"
    link.symlink_to(inside, target_is_directory=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-at-spawn"
    outside.mkdir()
    original_spawn = registry.spawn

    async def swap_link_then_spawn(*args, **kwargs):
        link.unlink()
        link.symlink_to(outside, target_is_directory=True)
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(registry, "spawn", swap_link_then_spawn)
    result = await bash.run(
        {"command": "touch should-not-run", "workdir": "selected"}, ctx
    )
    assert result.is_error is True
    assert registry.jobs(session_id=ctx.session_id) == ()
    assert not (outside / "should-not-run").exists()


@pytest.mark.parametrize("shell", ["/bin/bash -c", "/tmp/untrusted-shell", "bash"])
async def test_run_rejects_shell_arguments_and_unlisted_shells(registry, ctx, shell):
    result = await bash.run(
        {"command": "touch should-not-run", "shell": shell}, ctx
    )
    assert result.is_error is True
    assert registry.jobs(session_id=ctx.session_id) == ()
    assert not (ctx.workspace / "should-not-run").exists()


async def test_status_wait_and_stop_use_registry_handlers(registry, ctx):
    started = await bash.run(
        {"command": "sleep 0.2; printf ready", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)

    status = await bash.run({"action": "status", "job_id": job_id}, ctx)
    assert status.is_error is False
    assert "status: running" in body(status)

    waited = await bash.run(
        {"action": "wait", "job_id": job_id, "wait_s": 5}, ctx
    )
    assert waited.is_error is False
    assert "ready" in body(waited)
    await registry.job(job_id, session_id=ctx.session_id).wait(5)

    stopped = await bash.run({"action": "stop", "job_id": job_id}, ctx)
    assert stopped.is_error is False
    assert "already finished" in body(stopped)


async def test_stop_running_job_is_idempotent(registry, ctx):
    started = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    first = await bash.run({"action": "stop", "job_id": job_id}, ctx)
    second = await bash.run({"action": "stop", "job_id": job_id}, ctx)
    assert first.is_error is False and "terminated" in body(first)
    assert second.is_error is False and "already finished" in body(second)


@pytest.mark.parametrize(
    "args",
    [
        {"action": "run"},
        {"action": "run", "command": "true", "job_id": "job_x"},
        {"action": "status"},
        {"action": "status", "job_id": "job_x", "command": "true"},
        {"action": "status", "job_id": "job_x", "workdir": "nested"},
        {"action": "status", "job_id": "job_x", "shell": "/bin/bash"},
        {"action": "wait", "job_id": "job_x", "wait_s": 1, "workdir": "nested"},
        {"action": "wait", "job_id": "job_x", "wait_s": 1, "shell": "/bin/bash"},
        {"action": "wait", "job_id": "job_x"},
        {"action": "stop", "job_id": "job_x", "workdir": "nested"},
        {"action": "stop", "job_id": "job_x", "shell": "/bin/bash"},
        {"action": "stop", "job_id": "job_x", "wait_s": 1},
        {"action": "bogus", "command": "touch side-effect"},
    ],
)
async def test_invalid_shapes_fail_before_side_effects(registry, ctx, args):
    result = await bash.run(args, ctx)
    assert result.is_error is True
    assert registry.jobs(session_id=ctx.session_id) == ()


@pytest.mark.parametrize(
    "args",
    [
        {"action": "status", "job_id": "job_missing", "stdout_offset": -1},
        {"action": "status", "job_id": "job_missing", "stderr_offset": True},
        {"action": "wait", "job_id": "job_missing", "wait_s": 0},
        {"action": "wait", "job_id": "job_missing", "wait_s": 31, "until": "output"},
        {"action": "wait", "job_id": "job_missing", "wait_s": 3601},
        {"action": "wait", "job_id": "job_missing", "until": "never"},
        {"action": "wait", "job_id": "job_missing", "wait_s": float("inf")},
    ],
)
async def test_offsets_and_wait_are_validated_before_lookup(registry, ctx, args):
    result = await bash.run(args, ctx)
    assert result.is_error is True
    assert "unknown job_id" not in body(result)


@pytest.mark.parametrize("action", ["status", "wait", "stop"])
async def test_job_actions_cannot_cross_sessions(registry, ctx, workspace, action):
    started = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    foreign = ToolContext(
        workspace=workspace,
        session_id="another-session",
        turn_id="turn",
        config=Config(),
        job_registry=registry,
    )
    args = {"action": action, "job_id": job_id}
    if action == "wait":
        args["wait_s"] = 0.1
    result = await bash.run(args, foreign)
    assert result.is_error is True
    assert "unknown job_id" in body(result)
    assert registry.job(job_id, session_id=ctx.session_id).done is False


def test_permission_keys_preserve_run_and_qualify_job_actions():
    key = bash.SPEC.resolve_permission_key
    assert key({"command": "git status"}) == "git status"
    assert key({"action": "run", "command": "git status"}) == "git status"
    assert key({"action": "status", "job_id": "job_x"}) == "status:job_x"
    assert key({"action": "wait", "job_id": "job_x"}) == "wait:job_x"
    assert key({"action": "stop", "job_id": "job_x"}) == "stop:job_x"
    assert len({
        key({"action": action, "job_id": "job_x"})
        for action in ("status", "wait", "stop")
    }) == 3


async def test_legacy_job_tools_remain_compatible(registry, ctx):
    started = await bash.run(
        {"command": "echo legacy; sleep 30", "run_in_background": True}, ctx
    )
    job_id = job_id_of(started)
    await registry.job(job_id, session_id=ctx.session_id).wait_for_output(
        lambda: registry.job(job_id, session_id=ctx.session_id).stdout.total > 0,
        5,
    )
    output = await bash_output.run({"job_id": job_id}, ctx)
    assert output.is_error is False
    assert "legacy" in body(output)
    stopped = await kill_shell.run({"job_id": job_id}, ctx)
    assert stopped.is_error is False
    assert "terminated" in body(stopped)
