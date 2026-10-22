"""Durable ``shell.job_*`` lifecycle events and the user "move to background" seam (SHELL_PLAN P4).

A shell job outlives its tool call, so its lifecycle is reported through a sink
on the ``JobRegistry`` rather than through the tool's event stream. These tests
use real, harmless subprocesses in ``tmp_path`` and a recording sink.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ToolsSection
from nexus.events import EVENT_TYPES, SHELL_EVENTS, Event
from nexus.model.message import Text
from nexus.tools.builtin import _jobs, shell
from nexus.tools.spec import ToolContext
from nexus.view import apply, fold, initial_state

JOB_EVENTS = ("shell.job_started", "shell.job_yielded", "shell.job_exited")


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def __call__(self, session_id: str, event_type: str, data: dict) -> None:
        self.events.append((session_id, event_type, data))

    @property
    def types(self) -> list[str]:
        return [event_type for _, event_type, _ in self.events]

    def of(self, event_type: str) -> list[dict]:
        return [data for _, kind, data in self.events if kind == event_type]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
async def registry(recorder):
    value = _jobs.JobRegistry(event_sink=recorder)
    try:
        yield value
    finally:
        await value.aclose()


def body(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, Text))


def make_ctx(workspace: Path, registry, *, yield_s: float = 0.3, session_id: str = "a"):
    config = Config(
        v2=ConfigV2(tools=ToolsSection(bash_yield_s=yield_s, bash_max_s=60))
    )
    return ToolContext(
        workspace=workspace, session_id=session_id, turn_id="t", config=config,
        job_registry=registry,
    )


def test_job_events_are_part_of_the_catalogue():
    for name in JOB_EVENTS:
        assert name in SHELL_EVENTS and name in EVENT_TYPES


# -- registry seam -----------------------------------------------------------


async def test_started_follows_spawn_and_exit_is_recorded_once(registry, recorder, workspace):
    job = await registry.spawn(
        'echo "$(printf zz%s yy)"', session_id="a", cwd=workspace,
        env={"SECRET_TOKEN": "hunter2"},
    )
    await job.wait(10)
    assert recorder.types == ["shell.job_started", "shell.job_exited"]
    assert {sid for sid, _, _ in recorder.events} == {"a"}
    (started,), (exited,) = recorder.of("shell.job_started"), recorder.of("shell.job_exited")
    assert started["job_id"] == exited["job_id"] == job.job_id
    assert started["pid"] == job.pid and started["background"] is False
    assert exited["status"] == "completed" and exited["exit_code"] == 0
    assert exited["log_bytes"] == len("zzyy\n")
    # Ids, status and counts only: never output or environment values.
    blob = repr(recorder.events)
    assert "hunter2" not in blob and "zzyy" not in blob


async def test_started_event_redacts_credentials_in_the_command(registry, recorder, workspace):
    job = await registry.spawn(
        "echo api_key=sk-live-secret-value >/dev/null", session_id="a", cwd=workspace,
    )
    await job.wait(10)
    (started,) = recorder.of("shell.job_started")
    assert "sk-live-secret-value" not in repr(recorder.events)
    assert started["command"].startswith("echo api_key=")


async def test_failed_spawn_records_nothing(registry, recorder, workspace):
    with pytest.raises(OSError):
        await registry.spawn("true", session_id="a", cwd=workspace / "missing")
    assert recorder.events == []


async def test_failed_spawn_closes_and_removes_its_empty_log(workspace):
    log = workspace / "logs" / "job_0123456789ab.log"
    job = _jobs.ShellJob(
        "job_0123456789ab", "true", workspace / "missing", None, log_path=log
    )
    with pytest.raises(OSError):
        await job.start()
    assert job.log._fd is None
    assert not log.exists()


@pytest.mark.parametrize("stop", ["kill", "release_session"])
async def test_stop_during_process_start_leaves_no_process(
    registry, recorder, workspace, monkeypatch, stop
):
    real = asyncio.create_subprocess_shell
    entered, release = asyncio.Event(), asyncio.Event()
    spawned: list = []

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        process = await real(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", slow)
    log_dir = workspace / "logs"
    task = asyncio.create_task(
        registry.spawn("sleep 30", session_id="a", cwd=workspace, log_dir=log_dir)
    )
    await entered.wait()
    (job,) = registry.jobs(session_id="a")
    if stop == "kill":
        await registry.kill(job.job_id, session_id="a")
    else:
        await registry.aclose_session("a")
    release.set()
    with pytest.raises(_jobs.JobRegistryError):
        await task
    (process,) = spawned
    assert process.returncode is not None  # killed and reaped, not escaped
    assert registry.jobs(session_id="a") == ()
    assert recorder.events == []  # no started event for a job that never ran
    assert list(log_dir.glob("*.log")) == []


async def test_concurrent_stops_record_exactly_one_exit(registry, recorder, workspace):
    job = await registry.spawn("sleep 30", session_id="a", cwd=workspace)
    await asyncio.gather(
        job.terminate(),
        job.terminate(),
        registry.kill(job.job_id, session_id="a"),
    )
    await job.aclose()
    assert recorder.types == ["shell.job_started", "shell.job_exited"]
    assert recorder.of("shell.job_exited")[0]["status"] == "killed"


async def test_a_job_that_finished_never_yields_afterwards(registry, recorder, workspace):
    """The finishes-in-the-same-tick race: backgrounding a finished job is refused."""
    job = await registry.spawn("true", session_id="a", cwd=workspace)
    await job.wait(10)
    assert job.move_to_background("yield_window") is False
    assert job.request_background() == "finished"
    assert recorder.types == ["shell.job_started", "shell.job_exited"]


async def test_moving_to_background_is_recorded_once_before_the_exit(registry, recorder, workspace):
    job = await registry.spawn("sleep 30", session_id="a", cwd=workspace)
    assert job.move_to_background("user") is True
    assert job.move_to_background("user") is False
    await registry.kill(job.job_id, session_id="a")
    assert recorder.types == ["shell.job_started", "shell.job_yielded", "shell.job_exited"]
    assert recorder.of("shell.job_yielded")[0]["reason"] == "user"
    assert recorder.of("shell.job_exited")[0]["background"] is True


async def test_a_background_spawn_announces_no_yield(registry, recorder, workspace):
    job = await registry.spawn("sleep 30", session_id="a", cwd=workspace, background=True)
    assert recorder.of("shell.job_started")[0]["background"] is True
    assert job.move_to_background("yield_window") is False
    assert job.request_background() == "already_background"
    await registry.kill(job.job_id, session_id="a")
    assert recorder.types == ["shell.job_started", "shell.job_exited"]


async def test_unannounced_jobs_are_silent(registry, recorder, workspace):
    job = await registry.spawn("true", session_id="a", cwd=workspace, announce=False)
    await job.wait(10)
    assert recorder.events == [] and job.announced is False


async def test_a_failing_sink_never_breaks_a_job(workspace):
    def boom(*_args):
        raise RuntimeError("sink down")

    registry = _jobs.JobRegistry(event_sink=boom)
    try:
        job = await registry.spawn("exit 3", session_id="a", cwd=workspace)
        await job.wait(10)
        assert job.exit_code == 3
    finally:
        await registry.aclose()


async def test_closing_the_registry_records_exit_for_running_jobs(recorder, workspace):
    registry = _jobs.JobRegistry(event_sink=recorder)
    await registry.spawn("sleep 30", session_id="a", cwd=workspace)
    await registry.aclose()
    assert recorder.types == ["shell.job_started", "shell.job_exited"]
    assert recorder.of("shell.job_exited")[0]["status"] == "killed"


async def test_closing_one_session_records_exits_for_that_session_only(registry, recorder, workspace):
    a = await registry.spawn("sleep 30", session_id="a", cwd=workspace)
    b = await registry.spawn("sleep 30", session_id="b", cwd=workspace)
    await registry.aclose_session("a")
    exits = [(sid, data["job_id"]) for sid, kind, data in recorder.events if kind == "shell.job_exited"]
    assert exits == [("a", a.job_id)]
    assert registry.has_active() is True and not b.done


# -- tool seam ---------------------------------------------------------------


async def test_yield_window_move_is_recorded_once(registry, recorder, workspace):
    ctx = make_ctx(workspace, registry, yield_s=0.3)
    result = await shell.run({"command": "sleep 30"}, ctx)
    assert "moved to background after" in body(result)
    assert recorder.types == ["shell.job_started", "shell.job_yielded"]
    assert recorder.of("shell.job_yielded")[0]["reason"] == "yield_window"
    job = registry.jobs(session_id="a")[0]
    await registry.kill(job.job_id, session_id="a")
    assert recorder.types.count("shell.job_exited") == 1


async def test_user_request_moves_a_blocking_foreground_call_to_background(registry, recorder, workspace):
    ctx = make_ctx(workspace, registry, yield_s=30)
    task = asyncio.create_task(shell.run({"command": "sleep 30"}, ctx))
    job = None
    for _ in range(200):
        jobs = registry.jobs(session_id="a")
        if jobs and jobs[0].foreground_awaited:
            job = jobs[0]
            break
        await asyncio.sleep(0.02)
    assert job is not None
    assert job.request_background() == "requested"
    result = await asyncio.wait_for(task, 10)
    assert "at the user's request" in body(result)
    assert "status: running" in body(result)
    assert job.background and job.background_reason == "user" and not job.done
    assert recorder.types == ["shell.job_started", "shell.job_yielded"]
    assert recorder.of("shell.job_yielded")[0]["reason"] == "user"
    assert job.request_background() == "already_background"
    await registry.kill(job.job_id, session_id="a")


async def test_nothing_to_interrupt_when_no_call_awaits_the_job(registry, workspace):
    job = await registry.spawn("sleep 30", session_id="a", cwd=workspace)
    assert job.request_background() == "not_foreground"
    assert not job.request_background_seen


async def test_completion_wins_over_a_pending_background_request(registry, workspace):
    job = await registry.spawn("true", session_id="a", cwd=workspace)
    await job.wait(10)
    job._background_requested.set()
    outcome = await _jobs.await_job(job, yield_after=5, background_signal=True)
    assert outcome == "completed"
    assert job.foreground_awaited is False


# -- view --------------------------------------------------------------------


def _events() -> list[Event]:
    def make(seq: int, kind: str, data: dict) -> Event:
        return Event(type=kind, data=data, seq=seq, ts=100.0 + seq, session="a")

    return [
        make(1, "shell.job_started", {
            "job_id": "job_0123456789ab", "command": "sleep 5", "cwd": "/w", "pid": 7,
            "background": False, "started_at": 100.5,
        }),
        make(2, "shell.job_yielded", {"job_id": "job_0123456789ab", "reason": "user"}),
        make(3, "shell.job_exited", {
            "job_id": "job_0123456789ab", "status": "killed", "exit_code": -15,
            "signal": 15, "duration_ms": 5000, "background": True, "log_bytes": 3,
            "log_lines": 1, "stderr_bytes": 0,
        }),
        # A doubled exit and a late yield must not change the folded row.
        make(4, "shell.job_exited", {"job_id": "job_0123456789ab", "status": "completed"}),
        make(5, "shell.job_yielded", {"job_id": "job_0123456789ab", "reason": "yield_window"}),
    ]


def test_replay_equals_live_and_the_first_exit_wins():
    events = _events()
    live = initial_state("a")
    for event in events:
        live = apply(live, event)
    replayed = fold(events, initial_state("a"))
    assert replayed.to_dict() == live.to_dict()
    (job,) = replayed.jobs
    assert job.status == "killed" and job.signal == 15 and job.exit_code == -15
    assert job.background is True and job.yielded_reason == "user"
    assert job.command == "sleep 5" and job.log_bytes == 3 and job.duration_ms == 5000
    assert replayed.diagnostics == []


def test_the_wire_shape_has_no_jobs_until_one_exists():
    assert "jobs" not in initial_state("a").to_dict()
    assert fold(_events()[:1], initial_state("a")).to_dict()["jobs"][0]["status"] == "running"
