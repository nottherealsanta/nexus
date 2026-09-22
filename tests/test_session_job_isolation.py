"""Session-scoped shell-job isolation (PLAN Phase 3.5, section 14.5).

One ``Runtime`` shares a single :class:`~nexus.tools.builtin._jobs.JobRegistry`
across every concurrent session. These tests drive real, harmless subprocesses
through the public shell tools and the registry to prove that a session can only
see, read, and kill its own jobs; that concurrent execution does not cross-talk;
that the same raw job id in two sessions stays isolated; that retention and
cleanup are per session; and that closing a session (or the runtime) reclaims
exactly the right processes.

Everything runs in ``tmp_path``; no test reaches outside its temporary
workspace.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.model.message import Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
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


def ctx_for(
    workspace: Path,
    session_id: str,
    registry: _jobs.JobRegistry,
    *,
    turn_id: str = "turn",
) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id=session_id,
        turn_id=turn_id,
        config=Config(),
        job_registry=registry,
    )


def scripted_config(model: str = "scripted/session-jobs") -> Config:
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(model=ModelSection(default=model)),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
async def registry():
    reg = _jobs.JobRegistry()
    try:
        yield reg
    finally:
        await reg.aclose()


# ---------------------------------------------------------------------------
# Concurrent execution and isolated polling
# ---------------------------------------------------------------------------


async def test_two_sessions_run_and_poll_concurrently(workspace, registry):
    a = ctx_for(workspace, "session-a", registry)
    b = ctx_for(workspace, "session-b", registry)

    started_a, started_b = await asyncio.gather(
        bash.run(
            {"command": "sleep 0.2; echo A-DONE", "run_in_background": True}, a
        ),
        bash.run(
            {"command": "sleep 0.2; echo B-DONE", "run_in_background": True}, b
        ),
    )
    id_a = job_id_of(started_a)
    id_b = job_id_of(started_b)

    assert registry.job(id_a, session_id="session-a") is not None
    assert registry.job(id_b, session_id="session-b") is not None
    assert registry.job(id_a, session_id="session-b") is None
    assert registry.job(id_b, session_id="session-a") is None

    out_a, out_b = await asyncio.gather(
        bash_output.run({"job_id": id_a, "wait_s": 5}, a),
        bash_output.run({"job_id": id_b, "wait_s": 5}, b),
    )
    assert out_a.is_error is False and out_b.is_error is False
    assert "A-DONE" in body(out_a)
    assert "B-DONE" not in body(out_a)
    assert "B-DONE" in body(out_b)
    assert "A-DONE" not in body(out_b)


async def test_bash_output_cannot_read_another_session(workspace, registry):
    a = ctx_for(workspace, "a", registry)
    b = ctx_for(workspace, "b", registry)
    started = await bash.run(
        {"command": "echo SECRET-A; sleep 30", "run_in_background": True}, a
    )
    id_a = job_id_of(started)

    leaked = await bash_output.run({"job_id": id_a}, b)
    assert leaked.is_error is True
    assert "unknown job_id" in body(leaked)
    assert "SECRET-A" not in body(leaked)


async def test_kill_shell_cannot_reach_another_session(workspace, registry):
    a = ctx_for(workspace, "a", registry)
    b = ctx_for(workspace, "b", registry)
    started = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, a
    )
    id_a = job_id_of(started)
    job_a = registry.job(id_a, session_id="a")
    assert job_a is not None and job_a.done is False

    foreign = await kill_shell.run({"job_id": id_a}, b)
    assert foreign.is_error is True
    assert "unknown job_id" in body(foreign)
    assert job_a.done is False  # the cross-session kill did not touch it

    owned = await kill_shell.run({"job_id": id_a}, a)
    assert owned.is_error is False
    assert "terminated" in body(owned)
    assert job_a.done is True


async def test_unknown_ids_are_errors_within_the_owner_session(
    workspace, registry
):
    a = ctx_for(workspace, "a", registry)
    unknown = await bash_output.run({"job_id": "job_000000000000"}, a)
    assert unknown.is_error is True
    assert "unknown job_id" in body(unknown)
    raw_pid = await kill_shell.run({"job_id": "12345"}, a)
    assert raw_pid.is_error is True
    assert "unknown job_id" in body(raw_pid)


# ---------------------------------------------------------------------------
# Same raw id in two sessions
# ---------------------------------------------------------------------------


async def test_same_raw_id_in_two_sessions_stays_isolated(
    workspace, registry, monkeypatch
):
    # Force both sessions to allocate the same opaque id. Session-scoped lookup
    # is the defense: the id is only ever resolved inside its own session.
    monkeypatch.setattr(
        _jobs.JobRegistry,
        "_new_id",
        lambda self, partition: "job_deadbeefcafe",
    )
    a = ctx_for(workspace, "a", registry)
    b = ctx_for(workspace, "b", registry)

    started_a, started_b = await asyncio.gather(
        bash.run(
            {"command": "echo A-MARK; sleep 30", "run_in_background": True}, a
        ),
        bash.run(
            {"command": "echo B-MARK; sleep 30", "run_in_background": True}, b
        ),
    )
    id_a = job_id_of(started_a)
    id_b = job_id_of(started_b)
    assert id_a == id_b == "job_deadbeefcafe"

    job_a = registry.job(id_a, session_id="a")
    job_b = registry.job(id_b, session_id="b")
    assert job_a is not None and job_b is not None
    assert job_a is not job_b
    assert job_a.command != job_b.command
    # Session-scoped enumeration returns exactly one job per side.
    assert registry.jobs(session_id="a") == (job_a,)
    assert registry.jobs(session_id="b") == (job_b,)
    assert len(registry.jobs(admin=True)) == 2

    out_a, out_b = await asyncio.gather(
        bash_output.run({"job_id": id_a, "wait_s": 5}, a),
        bash_output.run({"job_id": id_b, "wait_s": 5}, b),
    )
    assert "A-MARK" in body(out_a) and "B-MARK" not in body(out_a)
    assert "B-MARK" in body(out_b) and "A-MARK" not in body(out_b)

    # Killing "the same id" in session b kills b's job, never a's.
    await kill_shell.run({"job_id": id_b}, b)
    assert job_b.done is True
    assert job_a.done is False


async def test_concurrent_spawns_in_one_session_get_unique_ids(
    workspace, registry
):
    a = ctx_for(workspace, "a", registry)
    started = await asyncio.gather(
        *(
            bash.run(
                {"command": "sleep 30", "run_in_background": True}, a
            )
            for _ in range(8)
        )
    )
    ids = [job_id_of(result) for result in started]
    assert len(set(ids)) == 8
    assert {job.job_id for job in registry.jobs(session_id="a")} == set(ids)
    assert len(registry.active(session_id="a")) == 8


# ---------------------------------------------------------------------------
# Cleanup isolation
# ---------------------------------------------------------------------------


async def test_aclose_session_only_releases_that_session(workspace, registry):
    a = ctx_for(workspace, "a", registry)
    b = ctx_for(workspace, "b", registry)
    started_a = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, a
    )
    started_b = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, b
    )
    id_a = job_id_of(started_a)
    id_b = job_id_of(started_b)
    job_b = registry.job(id_b, session_id="b")

    assert await registry.aclose_session("a") is True
    assert registry.active(session_id="a") == ()
    assert registry.job(id_a, session_id="a") is None
    assert len(registry.active(session_id="b")) == 1
    assert registry.job(id_b, session_id="b") is job_b
    assert job_b.done is False

    # The released session's tools now see the id as unknown.
    released = await bash_output.run({"job_id": id_a}, a)
    assert released.is_error is True
    assert "unknown job_id" in body(released)

    # Releasing an unknown session is a harmless no-op.
    assert await registry.aclose_session("a") is False


async def test_release_during_spawn_does_not_register_a_job(
    workspace, registry, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    real_start = _jobs.ShellJob.start
    seen: list[_jobs.ShellJob] = []

    async def delayed_start(self) -> None:
        seen.append(self)
        entered.set()
        await release.wait()
        await real_start(self)

    monkeypatch.setattr(_jobs.ShellJob, "start", delayed_start)

    task = asyncio.create_task(
        registry.spawn("sleep 30", session_id="s", cwd=workspace)
    )
    await entered.wait()
    # Release the session while spawn() is parked before job.start().
    assert await registry.aclose_session("s") is True
    release.set()
    with pytest.raises(_jobs.JobRegistryError):
        await task
    # Nothing is registered for the session, and the guard in start() kept the
    # terminated job from ever spawning a process that nothing would track.
    assert registry.jobs(session_id="s") == ()
    assert registry.active(admin=True) == ()
    assert seen and seen[0].pid is None
    # Defensive cleanup if the guard ever regresses: do not leak a live sleep.
    if seen[0].pid is not None:
        os.killpg(seen[0].pid, signal.SIGKILL)


async def test_aclose_terminates_every_session(workspace, registry):
    for session_id in ("a", "b", "c"):
        await bash.run(
            {
                "command": "sleep 30",
                "run_in_background": True,
            },
            ctx_for(workspace, session_id, registry),
        )
    assert len(registry.active(admin=True)) == 3

    await registry.aclose()

    assert registry.closed is True
    assert registry.active(admin=True) == ()
    assert registry.jobs(admin=True) == ()
    assert registry.sessions(admin=True) == ()


# ---------------------------------------------------------------------------
# Retention bounds
# ---------------------------------------------------------------------------


async def test_retention_is_bounded_per_session(workspace):
    reg = _jobs.JobRegistry(
        max_completed_jobs=2, max_total_completed_jobs=1000
    )
    ids: dict[str, list[str]] = {"a": [], "b": []}
    try:
        for session_id in ("a", "b"):
            for _ in range(3):
                job = await reg.spawn(
                    "true", session_id=session_id, cwd=workspace
                )
                ids[session_id].append(job.job_id)
                await job.wait()

        for session_id in ("a", "b"):
            assert reg.job(ids[session_id][0], session_id=session_id) is None
            assert reg.job(ids[session_id][1], session_id=session_id) is not None
            assert reg.job(ids[session_id][2], session_id=session_id) is not None
            assert len(reg.jobs(session_id=session_id)) == 2
        # Neither session evicted the other's retained jobs.
        assert set(reg.sessions(admin=True)) == {"a", "b"}
    finally:
        await reg.aclose()


async def test_global_retention_across_sessions(workspace):
    reg = _jobs.JobRegistry(
        max_completed_jobs=100, max_total_completed_jobs=3
    )
    order: list[tuple[str, str]] = []
    try:
        for session_id in ("a", "b", "a", "b"):
            job = await reg.spawn("true", session_id=session_id, cwd=workspace)
            order.append((session_id, job.job_id))
            await job.wait()

        # Oldest completed across all sessions is evicted first.
        first_session, first_id = order[0]
        assert reg.job(first_id, session_id=first_session) is None
        for session_id, job_id in order[1:]:
            assert reg.job(job_id, session_id=session_id) is not None
    finally:
        await reg.aclose()


async def test_empty_partitions_are_pruned(workspace):
    reg = _jobs.JobRegistry(
        max_completed_jobs=1, max_total_completed_jobs=1000
    )
    try:
        for _ in range(2):
            job = await reg.spawn("true", session_id="solo", cwd=workspace)
            await job.wait()
        # Second completion evicts the first, leaving one retained job.
        assert len(reg.jobs(session_id="solo")) == 1
        await reg.aclose_session("solo")
        assert reg.sessions(admin=True) == ()
    finally:
        await reg.aclose()


# ---------------------------------------------------------------------------
# Runtime integration
# ---------------------------------------------------------------------------


async def test_runtime_releases_one_session_and_then_all(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=scripted_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    registry = runtime.job_registry
    assert isinstance(registry, _jobs.JobRegistry)

    job_a = await registry.spawn("sleep 30", session_id="a", cwd=tmp_path)
    job_b = await registry.spawn("sleep 30", session_id="b", cwd=tmp_path)
    assert job_a.done is False and job_b.done is False

    assert await runtime.close_session_jobs("a") is True
    assert job_a.done is True
    assert job_b.done is False
    assert registry.job(job_a.job_id, session_id="a") is None
    assert registry.job(job_b.job_id, session_id="b") is job_b

    assert await runtime.close_session_jobs("missing") is False

    await runtime.aclose()
    assert job_b.done is True
    assert registry.closed is True
    assert runtime.job_registry.closed is True


# ---------------------------------------------------------------------------
# Explicit admin-only cross-session access; shell never widens
# ---------------------------------------------------------------------------


async def test_unscoped_lookup_is_not_cross_session_without_admin(workspace, registry):
    a = ctx_for(workspace, "a", registry)
    started = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, a
    )
    id_a = job_id_of(started)

    # Omitting the session id scopes to the default partition: no widening.
    assert registry.job(id_a) is None
    assert registry.jobs() == ()
    assert registry.active() == ()

    # Cross-session access is explicit and admin-only.
    assert registry.job(id_a, admin=True) is not None
    assert len(registry.jobs(admin=True)) == 1
    assert registry.sessions(admin=True) == ("a",)

    with pytest.raises(_jobs.JobRegistryError):
        registry.sessions()
    with pytest.raises(_jobs.JobRegistryError):
        registry.job(id_a, session_id="a", admin=True)


async def test_shell_tools_reject_missing_or_empty_session_id(workspace, registry):
    real = ctx_for(workspace, "real", registry)
    started = await bash.run(
        {"command": "sleep 30", "run_in_background": True}, real
    )
    id_real = job_id_of(started)

    for bad in ("", None):
        bad_ctx = ToolContext(
            workspace=workspace,
            session_id=bad,
            turn_id="t",
            config=Config(),
            job_registry=registry,
        )
        spawned = await bash.run({"command": "true"}, bad_ctx)
        assert spawned.is_error is True
        polled = await bash_output.run({"job_id": id_real}, bad_ctx)
        assert polled.is_error is True
        killed = await kill_shell.run({"job_id": id_real}, bad_ctx)
        assert killed.is_error is True

    # The rejected calls never reached the real session's job.
    job_real = registry.job(id_real, session_id="real")
    assert job_real is not None and job_real.done is False
