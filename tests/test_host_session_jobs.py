"""Host job commands, durable job events on the owning session, and the lifetime guards (SHELL_PLAN P4).

``SessionJobs``/``SessionJobLog``/``SessionJobStop``/``SessionJobBackground`` are
answered from the runtime's ``JobRegistry`` partition of the named session only.
Everything runs real, harmless subprocesses inside ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ToolsSection
from nexus.errors import SessionBusy
from nexus.events import Event
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host.daemon import Daemon
from nexus.host_support.session_archive import sweep_stale_sessions
from nexus.model.message import Message, MessageMeta, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.session.manager import SessionManager
from nexus.tools.builtin import _shell_logs, shell
from nexus.tools.spec import ToolContext
from nexus.view import fold


@pytest.fixture
async def facade(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = Config.load(workspace, home=tmp_path / "home", environ={})
    runtime = Runtime(workspace, config=config,
                      providers={"scripted": ScriptedProvider(text_response("unused"))})
    host = HostFacade(runtime)
    try:
        yield host
    finally:
        await host.shutdown()
        await runtime.aclose()


async def spawn(facade, session: str, command: str, **kwargs):
    """A job in ``session``'s partition with a real log, as the shell tool starts it."""
    runtime = facade.runtime
    return await runtime.job_registry.spawn(
        command, session_id=session, cwd=runtime.workspace,
        log_dir=_shell_logs.session_dir(runtime.workspace, session), **kwargs,
    )


def types(handle) -> list[str]:
    return [e.type for e in handle.events if e.type.startswith("shell.job_")]


def body(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


# -- durable events on the owning session -------------------------------------


async def test_job_events_are_persisted_on_the_owning_session_and_replay(facade):
    facade.open_session("s1")
    facade.open_session("s2")
    job = await spawn(facade, "s1", 'echo "$(printf zz%s yy)"')
    await job.wait(10)
    handle = facade.runtime.sessions.open("s1")
    assert types(handle) == ["shell.job_started", "shell.job_exited"]
    assert types(facade.runtime.sessions.open("s2")) == []
    replayed = fold(handle.events)
    (row,) = replayed.jobs
    assert row.job_id == job.job_id and row.status == "completed" and row.exit_code == 0
    live, _seq = facade.state("s1")
    assert live.to_dict()["jobs"] == replayed.to_dict()["jobs"]
    assert "zzyy" not in repr([e.data for e in handle.events if e.type.startswith("shell.job_")])


async def test_a_composer_bang_run_is_not_a_job_event(facade):
    facade.open_session("s1")
    await facade.handle(p.SessionShell(session="s1", command="echo hi"))
    handle = facade.runtime.sessions.open("s1")
    for _ in range(200):
        if any(e.type == "shell.completed" for e in handle.events):
            break
        await asyncio.sleep(0.02)
    assert types(handle) == []


async def test_stopping_the_session_records_the_exit_before_the_handle_closes(facade):
    facade.open_session("s1")
    await spawn(facade, "s1", "sleep 30")
    assert await facade.runtime.aclose_session("s1") is True
    reopened = facade.runtime.sessions.open("s1", create=False)
    assert types(reopened) == ["shell.job_started", "shell.job_exited"]
    assert not facade.runtime.session_has_active_jobs("s1")


# -- partitioned commands -----------------------------------------------------


async def test_session_jobs_never_shows_another_sessions_jobs(facade):
    a = await spawn(facade, "s1", "sleep 30")
    b = await spawn(facade, "s2", "sleep 30")
    one = await facade.handle(p.SessionJobs(session="s1"))
    two = await facade.handle(p.SessionJobs(session="s2"))
    nobody = await facade.handle(p.SessionJobs(session="s3"))
    assert isinstance(one, p.SessionJobsResult)
    assert [row.job_id for row in one.jobs] == [a.job_id] and one.running == 1
    assert [row.job_id for row in two.jobs] == [b.job_id]
    assert nobody.jobs == []
    row = one.jobs[0]
    assert row.status == "running" and row.pid == a.pid and row.source == "tool"
    assert row.command == "sleep 30" and row.can_background is False


async def test_job_log_reads_only_the_owning_session_and_is_bounded(facade):
    job = await spawn(facade, "s1", "seq 1 2000")
    await job.wait(10)
    mine = await facade.handle(p.SessionJobLog(session="s1", job_id=job.job_id, tail_lines=3))
    assert isinstance(mine, p.SessionJobLogResult) and mine.status == "ok"
    assert mine.text.splitlines() == ["1998", "1999", "2000"]
    assert mine.end == mine.size and mine.next_offset is None

    page = await facade.handle(
        p.SessionJobLog(session="s1", job_id=job.job_id, offset=0, limit=10)
    )
    assert page.text == "1\n2\n3\n4\n5\n" and page.next_offset == 10

    other = await facade.handle(p.SessionJobLog(session="s2", job_id=job.job_id))
    assert other.status == "unknown" and other.text == ""

    # Evicted from memory: still readable, still only through its own session.
    assert facade.runtime.job_registry.discard(job.job_id, session_id="s1") is True
    again = await facade.handle(p.SessionJobLog(session="s1", job_id=job.job_id, tail_lines=1))
    assert again.status == "ok" and again.text.strip() == "2000"
    assert (await facade.handle(p.SessionJobLog(session="s2", job_id=job.job_id))).status == "unknown"


@pytest.mark.parametrize(
    "command",
    [
        p.SessionJobLog(session="s1", job_id="../../etc/passwd"),
        p.SessionJobLog(session="s1", job_id="/etc/passwd"),
        p.SessionJobLog(session="s1", job_id="12345"),
        p.SessionJobStop(session="s1", job_id="../x"),
        p.SessionJobBackground(session="s1", job_id="job_zz"),
        p.SessionJobLog(session="../other", job_id="job_0123456789ab"),
        p.SessionJobLog(session="s1", job_id="job_0123456789ab", tail_lines=0),
        p.SessionJobLog(session="s1", job_id="job_0123456789ab", tail_lines=2001),
        p.SessionJobLog(session="s1", job_id="job_0123456789ab", offset=-1),
        p.SessionJobLog(session="s1", job_id="job_0123456789ab", offset=0, limit=50 * 1024 + 1),
    ],
    ids=lambda c: f"{type(c).__name__}-{getattr(c, 'job_id', '')}-{getattr(c, 'session', '')}",
)
async def test_job_commands_refuse_paths_and_unbounded_requests(facade, command):
    result = await facade.handle(command)
    assert isinstance(result, p.ErrorResult)


async def test_stop_is_partitioned_and_records_one_exit(facade):
    facade.open_session("s1")
    job = await spawn(facade, "s1", "sleep 30")
    wrong = await facade.handle(p.SessionJobStop(session="s2", job_id=job.job_id))
    assert wrong.outcome == "unknown" and not job.done

    stopped = await facade.handle(p.SessionJobStop(session="s1", job_id=job.job_id))
    assert isinstance(stopped, p.SessionJobStopResult) and stopped.outcome == "killed"
    assert job.done and job.status.value == "killed"
    again = await facade.handle(p.SessionJobStop(session="s1", job_id=job.job_id))
    assert again.outcome == "finished"
    handle = facade.runtime.sessions.open("s1")
    assert types(handle) == ["shell.job_started", "shell.job_exited"]


async def test_background_interrupts_a_foreground_call_and_is_partitioned(facade):
    facade.open_session("s1")
    runtime = facade.runtime
    ctx = ToolContext(
        workspace=runtime.workspace, session_id="s1", turn_id="t",
        config=Config(v2=ConfigV2(tools=ToolsSection(bash_yield_s=30, bash_max_s=60))),
        job_registry=runtime.job_registry,
    )
    call = asyncio.create_task(shell.run({"command": "sleep 30"}, ctx))
    row = None
    for _ in range(200):
        rows = (await facade.handle(p.SessionJobs(session="s1"))).jobs
        if rows and rows[0].can_background:
            row = rows[0]
            break
        await asyncio.sleep(0.02)
    assert row is not None and row.background is False

    wrong = await facade.handle(p.SessionJobBackground(session="s2", job_id=row.job_id))
    assert wrong.outcome == "unknown" and wrong.requested is False
    assert not call.done()

    asked = await facade.handle(p.SessionJobBackground(session="s1", job_id=row.job_id))
    assert asked.outcome == "requested" and asked.requested is True
    result = await asyncio.wait_for(call, 10)
    assert "at the user's request" in body(result)

    after = (await facade.handle(p.SessionJobs(session="s1"))).jobs[0]
    assert after.background is True and after.can_background is False and after.status == "running"
    done = await facade.handle(p.SessionJobBackground(session="s1", job_id=row.job_id))
    assert done.outcome == "already_background" and done.requested is False
    handle = runtime.sessions.open("s1")
    assert types(handle) == ["shell.job_started", "shell.job_yielded"]
    await facade.handle(p.SessionJobStop(session="s1", job_id=row.job_id))
    assert types(handle).count("shell.job_exited") == 1


async def test_background_has_nothing_to_interrupt_without_an_awaiting_call(facade):
    job = await spawn(facade, "s1", "sleep 30")
    result = await facade.handle(p.SessionJobBackground(session="s1", job_id=job.job_id))
    assert result.outcome == "not_foreground" and result.requested is False


# -- lifetime guards ------------------------------------------------------------


async def test_runtime_reports_active_jobs_per_session_and_overall(facade):
    runtime = facade.runtime
    assert runtime.has_active_shell_jobs() is False
    job = await spawn(facade, "s1", "sleep 30")
    assert runtime.has_active_shell_jobs() is True
    assert runtime.session_has_active_jobs("s1") is True
    assert runtime.session_has_active_jobs("s2") is False
    await runtime.job_registry.kill(job.job_id, session_id="s1")
    assert runtime.has_active_shell_jobs() is False
    assert runtime.session_has_active_jobs("s1") is False


def test_the_daemon_does_not_idle_exit_with_a_loaded_project(tmp_path):
    daemon = Daemon(tmp_path, home=tmp_path, socket_path=tmp_path / "d.sock", idle_timeout=5)
    daemon._last_activity = time.time() - 1000
    daemon.projects.hosts["project"] = SimpleNamespace()
    assert daemon._idle() is False
    daemon.projects.hosts.clear()
    assert daemon._idle() is False  # An empty process gets its own idle window.
    daemon._last_activity = time.time() - 1000
    assert daemon._idle() is True


# -- session manager guards ------------------------------------------------------


def _persisted(manager: SessionManager, session_id: str, last_ts: float):
    handle = manager.open(session_id)
    handle.append_message(Message(role="user", content=[Text(text="hi")], meta=MessageMeta(ts=0)))
    handle.append_event(Event(type="turn.completed", ts=last_ts))
    return handle


def test_a_session_with_running_jobs_is_neither_evicted_nor_archived(tmp_path):
    busy = {"s1"}
    manager = SessionManager(tmp_path, busy_check=lambda sid: sid in busy)
    now = 1_000_000.0
    _persisted(manager, "s1", now - 5 * 3600)
    _persisted(manager, "s2", now - 5 * 3600)

    with pytest.raises(SessionBusy):
        manager.evict("s1")
    with pytest.raises(SessionBusy):
        manager.archive("s1")
    assert [r.session_id for r in manager.archive_stale(now=now)] == ["s2"]
    assert "s1" in manager.live_sessions

    busy.clear()
    assert manager.evict("s1") is not None
    assert [r.session_id for r in manager.archive_stale(now=now)] == ["s1"]


async def test_the_archive_sweep_leaves_a_session_with_a_running_job_open(tmp_path):
    busy = {"s1"}
    manager = SessionManager(tmp_path, busy_check=lambda sid: sid in busy)
    now = 1_000_000.0
    _persisted(manager, "s1", now - 5 * 3600)
    assert await sweep_stale_sessions(manager, 3600, now=now) == 0
    assert manager.archived() == [] and "s1" in manager.live_sessions
    busy.clear()
    assert await sweep_stale_sessions(manager, 3600, now=now) == 1


def test_delete_refuses_a_session_with_running_jobs_even_forced(tmp_path):
    busy = {"s1"}
    manager = SessionManager(tmp_path, busy_check=lambda sid: sid in busy)
    _persisted(manager, "s1", 1.0)
    for force in (False, True):
        with pytest.raises(SessionBusy, match="shell jobs"):
            manager.delete("s1", force=force)
    assert manager.list_trashed() == [] and "s1" in manager.live_sessions
    busy.clear()
    assert manager.delete("s1").session_id == "s1"


async def test_a_refused_close_leaves_the_jobs_running(facade):
    runtime = facade.runtime
    handle = runtime.sessions.open("s1")
    handle._viewers += 1  # a live viewer makes the manager refuse to close
    job = await spawn(facade, "s1", "sleep 30")
    with pytest.raises(SessionBusy):
        await runtime.aclose_session("s1")
    assert not job.done and runtime.session_has_active_jobs("s1")
    handle._viewers -= 1
    assert await runtime.aclose_session("s1") is True
    assert job.done


async def test_running_jobs_alone_do_not_block_a_close(facade):
    facade.open_session("s1")
    job = await spawn(facade, "s1", "sleep 30")
    assert await facade.runtime.aclose_session("s1") is True and job.done


def test_a_failing_busy_check_fails_closed(tmp_path):
    def boom(_sid):
        raise RuntimeError("registry gone")

    manager = SessionManager(tmp_path, busy_check=boom)
    _persisted(manager, "s1", 1.0)
    with pytest.raises(SessionBusy):
        manager.evict("s1")


def test_manager_emit_uses_only_a_live_handle(tmp_path):
    manager = SessionManager(tmp_path)
    assert manager.emit_session_event("ghost", "shell.job_started", {"job_id": "j"}) is False
    assert manager.emit_session_event("../bad", "shell.job_started", {}) is False
    handle = _persisted(manager, "s1", 1.0)
    assert manager.emit_session_event("s1", "shell.job_started", {"job_id": "j"}) is True
    assert [e.type for e in handle.events][-1] == "shell.job_started"
    manager.close_all()
    assert manager.emit_session_event("s1", "shell.job_exited", {"job_id": "j"}) is False
