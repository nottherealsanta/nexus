"""Persistent shell jobs: background without ``timeout_s`` (SHELL_PLAN P6)."""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ToolsSection
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.host.daemon import Daemon
from nexus.model.message import Text
from nexus.tools.builtin import _jobs, bash, shell
from nexus.tools.manager import BASH_TIMEOUT_GRACE_S, ToolManager
from nexus.tools.spec import ToolCall, ToolContext

JOB_ID_RE = re.compile(r"job_id: (job_[0-9a-f]{12})")


def body(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


def make_ctx(workspace: Path, *, session_id: str = "p6", **extra) -> ToolContext:
    tools = {k: extra.pop(k) for k in list(extra) if k.startswith("shell_")}
    config = Config(v2=ConfigV2(tools=ToolsSection(**tools))) if tools else Config()
    return ToolContext(
        workspace=workspace, session_id=session_id, turn_id="t", config=config, **extra
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
async def registry():
    events: list[tuple[str, str, dict]] = []
    value = _jobs.JobRegistry(event_sink=lambda s, t, d: events.append((s, t, d)))
    value.recorded = events  # type: ignore[attr-defined]
    _jobs.set_default_registry(value)
    try:
        yield value
    finally:
        await value.aclose()
        _jobs.set_default_registry(None)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def start(ctx: ToolContext, command: str, **args):
    return await shell.run({"command": command, "background": True, **args}, ctx)


def job_id_of(result) -> str:
    match = JOB_ID_RE.search(body(result))
    assert match, body(result)
    return match.group(1)


# -- kind and lifetime ---------------------------------------------------------------


async def test_background_without_timeout_is_persistent_and_has_no_lifetime_cap(
    registry, workspace
):
    ctx = make_ctx(workspace)
    result = await start(ctx, "sleep 30")
    assert not result.is_error
    text = body(result)
    assert "kind: persistent" in text and "timeout: none (runs until stopped)" in text
    job = registry.job(job_id_of(result), session_id=ctx.session_id)
    assert job.persistent and job.background and job.watchdog is None
    started = [d for _s, t, d in registry.recorded if t == "shell.job_started"]
    assert started and started[0]["persistent"] is True
    await shell.run({"action": "stop", "job_id": job.job_id}, ctx)


async def test_background_with_timeout_stays_finite_and_capped(registry, workspace):
    ctx = make_ctx(workspace)
    result = await start(ctx, "sleep 30", timeout_s=60)
    job = registry.job(job_id_of(result), session_id=ctx.session_id)
    assert not job.persistent and job.watchdog is not None
    assert "kind: persistent" not in body(result)
    await shell.run({"action": "stop", "job_id": job.job_id}, ctx)


async def test_legacy_run_in_background_is_still_capped_through_the_shim(
    registry, workspace
):
    ctx = make_ctx(workspace)
    result = await bash.run({"command": "sleep 30", "run_in_background": True}, ctx)
    job = registry.job(job_id_of(result), session_id=ctx.session_id)
    assert not job.persistent and job.watchdog is not None
    assert "kind: persistent" not in body(result)
    await shell.run({"action": "stop", "job_id": job.job_id}, ctx)


async def test_legacy_run_in_background_is_still_capped_through_the_manager(
    registry, workspace
):
    manager = ToolManager(Config(), workspace=workspace, job_registry=registry)
    ctx = make_ctx(workspace)
    try:
        batch = manager.prepare(
            [
                ToolCall(id="c1", name="Bash", input={"command": "sleep 30", "run_in_background": True}),
                ToolCall(id="c2", name="shell", input={"command": "sleep 31", "background": True}),
            ]
        )
        from nexus.tools.permissions import Decision

        results = await manager.dispatch(
            batch.with_decisions({"c1": Decision.ALLOW_ONCE, "c2": Decision.ALLOW_ONCE}),
            lambda call, spec: ctx,
        )
        legacy, modern = (registry.job(job_id_of(r), session_id=ctx.session_id) for r in results)
        assert not legacy.persistent and legacy.watchdog is not None
        assert modern.persistent
    finally:
        await manager.aclose()


async def test_a_subagent_cannot_start_a_persistent_job(registry, workspace):
    ctx = make_ctx(workspace, agent_id="child-1")
    refused = await start(ctx, "sleep 30")
    assert refused.is_error and "subagent" in body(refused)
    assert registry.jobs(session_id=ctx.session_id) == ()
    finite = await start(ctx, "sleep 30", timeout_s=30)
    assert not finite.is_error
    await shell.run({"action": "stop", "job_id": job_id_of(finite)}, ctx)


# -- limits and duplicates ------------------------------------------------------------


async def test_the_job_over_the_session_limit_is_refused_with_the_list(registry, workspace):
    ctx = make_ctx(workspace, shell_max_persistent_per_session=2, shell_max_persistent=8)
    first = job_id_of(await start(ctx, "sleep 30 # a"))
    second = job_id_of(await start(ctx, "sleep 30 # b"))
    refused = await start(ctx, "sleep 30 # c")
    assert refused.is_error
    text = body(refused)
    assert first in text and second in text and "action=stop" in text and "limit 2" in text
    assert len(registry.active(session_id=ctx.session_id)) == 2


async def test_the_default_limit_refuses_the_fifth_persistent_job(registry, workspace):
    ctx = make_ctx(workspace)
    ids = [job_id_of(await start(ctx, f"sleep 30 # {n}")) for n in range(4)]
    refused = await start(ctx, "sleep 30 # 5")
    assert refused.is_error and all(job_id in body(refused) for job_id in ids)


async def test_the_runtime_limit_counts_all_sessions_but_names_none_of_them(
    registry, workspace
):
    limits = {"shell_max_persistent_per_session": 2, "shell_max_persistent": 3}
    a = make_ctx(workspace, session_id="sess-a", **limits)
    b = make_ctx(workspace, session_id="sess-b", **limits)
    a_ids = [job_id_of(await start(a, f"sleep 30 # a{n}")) for n in range(2)]
    b_first = job_id_of(await start(b, "sleep 30 # b0"))
    refused = await start(b, "sleep 30 # b1")
    assert refused.is_error
    text = body(refused)
    assert "runtime-wide limit of 3" in text
    assert not any(job_id in text for job_id in a_ids)
    assert b_first in text  # only the caller's own job is listed


async def test_a_duplicate_command_and_cwd_is_an_error_naming_the_running_job(
    registry, workspace
):
    (workspace / "sub").mkdir()
    ctx = make_ctx(workspace)
    first = await start(ctx, "sleep 30")
    first_id = job_id_of(first)
    again = await start(ctx, "sleep 30")
    assert again.is_error
    assert job_id_of(again) == first_id  # the labelled line names the running job
    assert "not started" in body(again) and "action=stop" in body(again)
    assert len(registry.active(session_id=ctx.session_id)) == 1
    # A different cwd is a different job; so is another session.
    other_cwd = await start(ctx, "sleep 30", cwd="sub")
    assert not other_cwd.is_error and job_id_of(other_cwd) != first_id
    other_session = await start(make_ctx(workspace, session_id="p6-other"), "sleep 30")
    assert not other_session.is_error
    # Once stopped, the same command may start again.
    await shell.run({"action": "stop", "job_id": first_id}, ctx)
    restarted = await start(ctx, "sleep 30")
    assert not restarted.is_error and job_id_of(restarted) != first_id


async def test_concurrent_duplicate_starts_cannot_both_pass(registry, workspace):
    ctx = make_ctx(workspace)
    results = await asyncio.gather(*(start(ctx, "sleep 30") for _ in range(3)))
    assert sum(1 for r in results if not r.is_error) == 1
    assert len(registry.active(session_id=ctx.session_id)) == 1


# -- readiness: wait until=match -------------------------------------------------------


async def test_a_coloured_ready_banner_matches(registry, workspace):
    ctx = make_ctx(workspace)
    started = await start(ctx, "printf 'starting\\n\\033[32mREADY on 8080\\033[0m\\n'; sleep 30")
    result = await shell.run(
        {
            "action": "wait", "job_id": job_id_of(started), "until": "match",
            "pattern": r"^READY on \d+$", "wait_s": 10,
        },
        ctx,
    )
    text = body(result)
    assert not result.is_error and "match: READY on 8080" in text
    assert "status: running" in text and "kind: persistent" in text


async def test_a_banner_split_across_chunks_matches(registry, workspace):
    ctx = make_ctx(workspace)
    started = await start(ctx, "printf 'RE'; sleep 0.5; printf 'ADY go\\n'; sleep 30")
    began = time.monotonic()
    result = await shell.run(
        {
            "action": "wait", "job_id": job_id_of(started), "until": "match",
            "pattern": "READY go", "wait_s": 10,
        },
        ctx,
    )
    assert "match: READY go" in body(result)
    assert time.monotonic() - began < 8


async def test_a_match_timeout_leaves_the_job_running(registry, workspace):
    ctx = make_ctx(workspace)
    started = await start(ctx, "echo booting; sleep 30")
    job_id = job_id_of(started)
    result = await shell.run(
        {
            "action": "wait", "job_id": job_id, "until": "match",
            "pattern": "NEVER-PRINTED", "wait_s": 0.6,
        },
        ctx,
    )
    text = body(result)
    assert not result.is_error
    assert "match: none within 0.6s" in text and "booting" in text
    assert registry.job(job_id, session_id=ctx.session_id).done is False


async def test_match_returns_when_the_job_exits_first(registry, workspace):
    ctx = make_ctx(workspace)
    started = await start(ctx, "echo one; sleep 0.3")
    result = await shell.run(
        {
            "action": "wait", "job_id": job_id_of(started), "until": "match",
            "pattern": "NEVER", "wait_s": 10,
        },
        ctx,
    )
    assert "match: none (the job exited first)" in body(result)
    assert "status: completed" in body(result)


async def test_match_arguments_are_validated(registry, workspace):
    ctx = make_ctx(workspace)
    started = await start(ctx, "sleep 30")
    job_id = job_id_of(started)
    base = {"action": "wait", "job_id": job_id}
    missing = await shell.run({**base, "until": "match"}, ctx)
    assert missing.is_error and "pattern" in body(missing)
    stray = await shell.run({**base, "pattern": "x"}, ctx)
    assert stray.is_error and "only with until=match" in body(stray)
    bad = await shell.run({**base, "until": "match", "pattern": "("}, ctx)
    assert bad.is_error
    nested = await shell.run({**base, "until": "match", "pattern": "(a+)+$"}, ctx)
    assert nested.is_error and "backtrack" in body(nested)
    too_long = await shell.run({**base, "until": "match", "pattern": "x" * 257}, ctx)
    assert too_long.is_error
    too_slow = await shell.run(
        {**base, "until": "match", "pattern": "x", "wait_s": 121}, ctx
    )
    assert too_slow.is_error and "120" in body(too_slow)


def test_line_matcher_reassembles_lines_strips_ansi_and_flushes_the_last_one():
    matcher = _jobs.LineMatcher(_jobs.compile_match_pattern(r"^Local:\s+http://\S+$"))
    assert matcher.scan([b"\x1b[1mLoc", b"al:  \x1b[36mhttp://loc"]) is None
    assert matcher.scan([b"alhost:5173/\x1b[0m"]) is None  # no newline yet
    assert matcher.flush() == "Local:  http://localhost:5173/"
    crlf = _jobs.LineMatcher(_jobs.compile_match_pattern("ready"))
    assert crlf.scan([b"progress 10%\rprogress 90%\r\nready\r\n"]) == "ready"
    split_utf8 = _jobs.LineMatcher(_jobs.compile_match_pattern("é-ok"))
    assert split_utf8.scan([b"\xc3", b"\xa9-ok\n"]) == "é-ok"
    overlong = _jobs.LineMatcher(_jobs.compile_match_pattern("TAIL"))
    assert overlong.scan([b"x" * 5000 + b"TAIL\nTAIL\n"]) == "TAIL"


# -- process group ------------------------------------------------------------------


async def test_a_daemonised_child_keeps_the_job_running_and_stop_kills_the_group(
    registry, workspace
):
    ctx = make_ctx(workspace)
    started = await start(ctx, "sleep 60 & echo CHILD:$!")
    job_id = job_id_of(started)
    job = registry.job(job_id, session_id=ctx.session_id)
    await job.wait_for_output(lambda: b"CHILD:" in job.log.tail_bytes(4096), 5)
    pid = int(re.search(rb"CHILD:(\d+)", job.log.tail_bytes(4096)).group(1))
    await asyncio.sleep(0.6)  # the shell has exited; a finite job would be done now
    assert job._process.returncode == 0 and not job.done
    assert job.leftover_stopped is False and pid_alive(pid)
    stopped = await shell.run({"action": "stop", "job_id": job_id}, ctx)
    assert not stopped.is_error and job.done
    deadline = time.monotonic() + 3
    while pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not pid_alive(pid)


async def test_a_job_whose_whole_group_exits_finishes_with_the_shell_status(
    registry, workspace
):
    ctx = make_ctx(workspace)
    started = await start(ctx, "sleep 0.3 & wait; exit 4")
    job = registry.job(job_id_of(started), session_id=ctx.session_id)
    await job.wait(10)
    assert job.exit_code == 4 and job.status is _jobs.JobStatus.FAILED


async def test_a_shell_that_exits_while_a_child_lives_is_not_killed_as_a_leftover(
    registry, workspace
):
    ctx = make_ctx(workspace)
    started = await start(ctx, "sleep 1.2 & echo spawned")
    job = registry.job(job_id_of(started), session_id=ctx.session_id)
    await asyncio.sleep(0.6)
    assert not job.done
    await job.wait(10)  # the group empties on its own: the job then completes
    assert job.status is _jobs.JobStatus.COMPLETED and job.leftover_stopped is False


async def test_runtime_shutdown_kills_the_group(workspace):
    registry = _jobs.JobRegistry()
    ctx = make_ctx(workspace, job_registry=registry)
    started = await start(ctx, "sleep 60 & echo CHILD:$!")
    job = registry.job(job_id_of(started), session_id=ctx.session_id)
    await job.wait_for_output(lambda: b"CHILD:" in job.log.tail_bytes(4096), 5)
    pid = int(re.search(rb"CHILD:(\d+)", job.log.tail_bytes(4096)).group(1))
    await asyncio.sleep(0.4)
    assert pid_alive(pid)
    await registry.aclose()  # what Runtime.aclose (and so `daemon stop`) runs
    deadline = time.monotonic() + 3
    while pid_alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not pid_alive(pid) and job.done


# -- survival, cancel, waits -----------------------------------------------------------


async def test_a_persistent_job_survives_turn_end_and_a_cancelled_wait(registry, workspace):
    token = CancelToken()
    ctx = make_ctx(workspace, cancel_token=token)
    job_id = job_id_of(await start(ctx, "sleep 30"))
    job = registry.job(job_id, session_id=ctx.session_id)
    waiting = asyncio.ensure_future(
        shell.run({"action": "wait", "job_id": job_id, "wait_s": 30}, ctx)
    )
    await asyncio.sleep(0.3)
    token.cancel("user stop")
    with pytest.raises(OperationCancelled):
        await waiting
    assert not job.done and pid_alive(job.pid)
    status = await shell.run({"action": "status", "job_id": job_id}, make_ctx(workspace))
    assert "status: running" in body(status)


async def test_wait_for_exit_on_a_persistent_job_is_capped_at_600_seconds(registry, workspace):
    ctx = make_ctx(workspace)
    job_id = job_id_of(await start(ctx, "sleep 30"))
    result = await shell.run(
        {"action": "wait", "job_id": job_id, "wait_s": 601}, ctx
    )
    assert result.is_error and "must not exceed 600" in body(result)
    finite_id = job_id_of(await start(ctx, "sleep 30", timeout_s=100))
    allowed = await shell.run(
        {"action": "wait", "job_id": finite_id, "wait_s": 0.2}, ctx
    )
    assert not allowed.is_error


def test_the_manager_deadline_is_the_actions_own_bound():
    spec = shell.SPEC
    config = Config()
    manager = ToolManager(config, workspace=Path("."), job_registry=_jobs.JobRegistry())
    grace = BASH_TIMEOUT_GRACE_S
    assert manager._timeout_for(spec) == 3600 + grace  # unchanged without arguments
    assert manager._timeout_for(spec, {"action": "status", "job_id": "j"}) == 60 + grace
    assert manager._timeout_for(spec, {"action": "stop", "job_id": "j"}) == 60 + grace
    assert manager._timeout_for(
        spec, {"action": "wait", "job_id": "j", "until": "output"}
    ) == 30 + grace
    assert manager._timeout_for(
        spec, {"action": "wait", "job_id": "j", "wait_s": 45}
    ) == 45 + grace
    assert manager._timeout_for(
        spec, {"action": "wait", "job_id": "j", "until": "match", "pattern": "x", "wait_s": 60}
    ) == 60 + shell._MATCH_SCAN_ALLOWANCE_S + grace
    assert manager._timeout_for(spec, {"command": "x", "timeout_s": 90}) == 90 + grace
    assert manager._timeout_for(spec, {"command": "x"}) == 3600 + grace


# -- unexpected exit, stop reasons ------------------------------------------------------


async def test_an_unexpected_exit_is_delivered_once_and_a_stop_is_not(workspace):
    delivered: list[tuple[str, dict, str]] = []

    def sink(session: str, data: dict, text: str) -> bool:
        delivered.append((session, data, text))
        return True

    registry = _jobs.JobRegistry(completion_sink=sink, event_sink=lambda *_: None)
    ctx = make_ctx(workspace, job_registry=registry)
    try:
        dying = registry.job(
            job_id_of(await start(ctx, "echo up; sleep 0.3; exit 3")), session_id=ctx.session_id
        )
        await dying.wait(10)
        await asyncio.sleep(0.1)
        assert len(delivered) == 1
        session, data, text = delivered[0]
        assert session == ctx.session_id and data["status"] == "failed" and data["exit_code"] == 3
        assert "job_id: " + dying.job_id in text and "| up" in text
        stopped_id = job_id_of(await start(ctx, "sleep 30"))
        await shell.run({"action": "stop", "job_id": stopped_id}, ctx)
        await asyncio.sleep(0.1)
        assert len(delivered) == 1  # the stop result was the completion
    finally:
        await registry.aclose()


async def test_stop_persistent_records_why_on_the_exit_event(workspace):
    events: list[tuple[str, dict]] = []
    registry = _jobs.JobRegistry(event_sink=lambda s, t, d: events.append((t, d)))
    ctx = make_ctx(workspace, job_registry=registry)
    try:
        await start(ctx, "sleep 30 # a")
        finite = await start(ctx, "sleep 30", timeout_s=60)
        stopped = await registry.stop_persistent("no viewer for 1 h")
        assert stopped == 1
        exits = [d for t, d in events if t == "shell.job_exited"]
        assert len(exits) == 1 and exits[0]["stopped"] == "no viewer for 1 h"
        assert exits[0]["delivery"] == "none"
        assert registry.job(job_id_of(finite), session_id=ctx.session_id).done is False
    finally:
        await registry.aclose()


def test_the_view_and_host_row_carry_the_persistent_kind_and_stop_reason():
    from nexus.events import Event
    from nexus.host_support import session_jobs
    from nexus.view import fold, initial_state

    job_id = "job_0123456789ab"
    events = [
        Event(type="shell.job_started", seq=1, ts=1.0, session="a", data={
            "job_id": job_id, "command": "npm run dev", "cwd": "/w", "pid": 7,
            "background": True, "persistent": True, "started_at": 1.0,
        }),
        Event(type="shell.job_exited", seq=2, ts=2.0, session="a", data={
            "job_id": job_id, "status": "killed", "exit_code": -15, "signal": 15,
            "background": True, "stopped": "no viewer for 1 h",
        }),
    ]
    (view,) = fold(events, initial_state("a")).jobs
    assert view.persistent is True and view.stopped_reason == "no viewer for 1 h"
    job = _jobs.ShellJob(job_id, "sleep 1", Path("."), None, persistent=True)
    assert job.persistent and job.background
    assert session_jobs.summarize(job).persistent is True
    assert session_jobs.summarize(
        _jobs.ShellJob(job_id, "sleep 1", Path("."), None)
    ).persistent is False


# -- config -----------------------------------------------------------------------------


def test_the_config_defaults_and_validation():
    tools = ToolsSection()
    assert (tools.shell_max_persistent_per_session, tools.shell_max_persistent) == (4, 8)
    assert tools.shell_orphan_s == 3600
    assert _jobs.persistent_limits(Config()) == (4, 8)
    for bad in (
        {"shell_max_persistent_per_session": 0},
        {"shell_max_persistent": True},
        {"shell_orphan_s": -1},
        {"shell_orphan_s": float("inf")},
    ):
        with pytest.raises(ValueError):
            ToolsSection(**bad)
    assert ToolsSection(shell_orphan_s=0).shell_orphan_s == 0


def test_the_new_keys_are_typed_in_the_config_layers():
    from nexus.config import layers

    typed = [
        value
        for value in vars(layers).values()
        if isinstance(value, dict) and "tools.shell_max_auto_wakes" in value
    ]
    assert typed, "no typed-key table found in nexus.config.layers"
    table = typed[0]
    assert table["tools.shell_max_persistent_per_session"] is int
    assert table["tools.shell_max_persistent"] is int
    assert table["tools.shell_orphan_s"] is float


# -- daemon: orphan policy, status ---------------------------------------------------------


def _fake_clock(monkeypatch, now: list[float]) -> None:
    """Give the daemon module its own monotonic clock without touching asyncio's."""
    monkeypatch.setattr(
        "nexus.host.projects.time",
        SimpleNamespace(
            monotonic=lambda: now[0], time=time.time, strftime=time.strftime
        ),
    )


def _daemon(tmp_path: Path, *, viewers=0, running=0, jobs=1, orphan_s=3600.0):
    daemon = Daemon(tmp_path, home=tmp_path, socket_path=tmp_path / "d.sock", idle_timeout=5.0)
    state = SimpleNamespace(viewers=viewers, running=running, jobs=jobs, stopped=[])

    async def stop_jobs(reason):
        state.stopped.append(reason)
        return state.jobs

    facade = SimpleNamespace(
        presence=SimpleNamespace(total_viewers=lambda: state.viewers),
        supervisor=SimpleNamespace(running=state.running, queued=0),
    )
    runtime = SimpleNamespace(
        voice=None,
        has_active_shell_jobs=lambda: state.jobs > 0,
        persistent_shell_jobs=lambda limit=16: (state.jobs, []),
        shell_orphan_seconds=lambda: orphan_s,
        stop_persistent_shell_jobs=stop_jobs,
    )
    from nexus.host.projects import ProjectHost
    from nexus.host_support.project_identity import resolve_project
    from nexus.observability.daemon import DaemonDiagnostics
    facade.runtime = runtime
    facade.daemon_diagnostics = DaemonDiagnostics()
    daemon.project = ProjectHost(resolve_project(tmp_path), facade, "test")
    daemon.projects.hosts[daemon.project.identity.key] = daemon.project
    daemon.stops = []
    daemon.request_stop = daemon.stops.append  # type: ignore[method-assign]
    return daemon, state


async def test_no_viewer_and_no_turn_stops_persistent_jobs_and_records_why(tmp_path, monkeypatch):
    daemon, state = _daemon(tmp_path)
    now = [1000.0]
    _fake_clock(monkeypatch, now)
    await daemon.projects.check_orphans(daemon.project)  # the clock starts
    now[0] += 3599
    await daemon.projects.check_orphans(daemon.project)
    assert state.stopped == []
    now[0] += 2
    await daemon.projects.check_orphans(daemon.project)
    assert state.stopped == ["no viewer for 3600 s"]
    assert not daemon.stops, "one project orphan cleanup must not stop the shared process"
    log = daemon._log_file.read_text()
    assert "daemon.orphan_jobs_stopped" in log and "no viewer for 3600 s" in log


async def test_a_viewer_or_a_turn_resets_the_orphan_clock(tmp_path, monkeypatch):
    daemon, state = _daemon(tmp_path)
    now = [1000.0]
    _fake_clock(monkeypatch, now)
    await daemon.projects.check_orphans(daemon.project)
    now[0] += 3000
    state.viewers = 1
    await daemon.projects.check_orphans(daemon.project)
    assert daemon.project.orphan_since is None
    state.viewers = 0
    now[0] += 3000
    await daemon.projects.check_orphans(daemon.project)  # restarted at this tick, so 3000 s later it is not due
    now[0] += 3000
    await daemon.projects.check_orphans(daemon.project)
    assert state.stopped == []


async def test_the_orphan_policy_can_be_disabled_and_needs_a_persistent_job(tmp_path, monkeypatch):
    now = [1000.0]
    _fake_clock(monkeypatch, now)
    disabled, state = _daemon(tmp_path, orphan_s=0.0)
    await disabled.projects.check_orphans(disabled.project)
    now[0] += 10_000
    await disabled.projects.check_orphans(disabled.project)
    assert state.stopped == []
    none, state = _daemon(tmp_path, jobs=0)
    await none.projects.check_orphans(none.project)
    now[0] += 10_000
    await none.projects.check_orphans(none.project)
    assert none.project.orphan_since is None


def test_health_and_the_status_cli_list_persistent_jobs():
    from nexus.host import protocol as p

    row = {"session": "s", "job_id": "job_aaaaaaaaaaaa", "command": "npm run dev",
           "cwd": "/w", "pid": 1, "started_at": 1.0}
    health = p.HealthResult(persistent_jobs_total=2, persistent_jobs=[row])
    assert health.persistent_jobs_total == 2 and health.persistent_jobs[0]["job_id"] == row["job_id"]


async def test_runtime_reports_persistent_jobs_with_a_redacted_short_command(workspace):
    from nexus.runtime import Runtime

    registry = _jobs.JobRegistry()
    runtime = object.__new__(Runtime)
    runtime._job_registry = registry
    ctx = make_ctx(workspace, job_registry=registry)
    try:
        await start(ctx, "sleep 30 # " + "x" * 300)
        total, rows = runtime.persistent_shell_jobs()
        assert total == 1 and len(rows) == 1 and len(rows[0]["command"]) <= 120
        assert rows[0]["session"] == ctx.session_id and rows[0]["pid"]
        assert await runtime.stop_persistent_shell_jobs("test") == 1
        assert runtime.persistent_shell_jobs() == (0, [])
    finally:
        await registry.aclose()
