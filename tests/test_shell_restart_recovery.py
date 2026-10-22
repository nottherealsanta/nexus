"""SHELL_PLAN P7: restart recovery of shell job process groups and their events.

Real, harmless ``sleep`` processes and real ``kill -9`` of a registry-owning
process. What is pinned: a dead owner's verified groups are stopped at the next
sweep (also when the shell leader already exited, through a surviving member); a
live owner, a reused pid, a different start time and every malformed record are
left alone; a reopened session records exactly one ``interrupted`` exit per lost
job without queueing anything or starting a turn; and a graceful shutdown leaves
no files behind.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.paths import project_state_dir
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host.daemon import Daemon
from nexus.model.message import Message, Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.builtin import _job_recovery as recovery
from nexus.tools.builtin import _jobs

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION = "s1"
FAKE_JOB = "job_aaaaaaaaaaaa"
OWNER = "owner_" + "0" * 16
#: A well-formed start time in the whitespace-normalized form ``recovery.identify`` yields.
STALE_START = "Mon Jan 1 00:00:00 2001"


# -- helpers ----------------------------------------------------------------------


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def spawn_orphan(*, ignore_term: bool = False) -> int:
    """A ``sleep`` in its own process group whose group leader (the shell) is gone."""
    trap = "trap '' TERM; " if ignore_term else ""
    done = subprocess.run(
        ["/bin/sh", "-c", f"{trap}sleep 300 >/dev/null 2>&1 & echo $!"],
        capture_output=True, text=True, check=True, timeout=10,
        start_new_session=True,
    )
    pid = int(done.stdout.strip())
    assert alive(pid)
    return pid


def identity(pid: int) -> recovery.Proc:
    proc = recovery.identify(pid)
    assert proc is not None
    return proc


def write_owner(
    directory: Path,
    *,
    owner: str = OWNER,
    jobs: dict | None = None,
    record: dict | bytes | None = None,
    locked: bool = False,
):
    """Write an owner lock + record by hand. Returns the held fd when ``locked``."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lock = directory / f"{owner}.lock"
    lock.write_bytes(b"")
    os.chmod(lock, 0o600)
    path = directory / f"{owner}.json"
    if isinstance(record, bytes):
        path.write_bytes(record)
    else:
        body = record if record is not None else {
            "v": 1, "owner": owner, "pid": 1, "uid": os.getuid(), "jobs": jobs or {},
        }
        path.write_text(json.dumps(body))
    os.chmod(path, 0o600)
    if not locked:
        return None
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def job_entry(proc: recovery.Proc, **overrides) -> dict:
    entry = {
        "session": SESSION,
        "pgid": proc.pgid,
        "persistent": True,
        "members": [
            {"pid": proc.pid, "pgid": proc.pgid, "uid": proc.uid, "start": proc.start}
        ],
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def owners(tmp_path):
    return tmp_path / "shell-owners"


@pytest.fixture(autouse=True)
def _reap_strays():
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def sweep_fast(directory: Path) -> recovery.SweepResult:
    return recovery.sweep(directory, budget_s=10.0, grace_s=0.5, kill_wait_s=3.0)


# -- the sweep: what it stops --------------------------------------------------


def test_a_dead_owners_group_is_stopped_through_a_surviving_member(owners, _reap_strays):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    proc = identity(pid)
    write_owner(owners, jobs={FAKE_JOB: job_entry(proc)})

    result = sweep_fast(owners)

    assert wait_gone(pid)
    assert (result.dead_owners, result.jobs, result.terminated) == (1, 1, 1)
    assert result.live_owners == 0 and result.failed == 0
    assert list(owners.iterdir()) == []  # a settled record is deleted


def test_a_group_that_ignores_sigterm_is_killed_after_reverification(owners, _reap_strays):
    pid = spawn_orphan(ignore_term=True)
    _reap_strays.append(pid)
    write_owner(owners, jobs={FAKE_JOB: job_entry(identity(pid))})

    result = sweep_fast(owners)

    assert wait_gone(pid)
    assert result.killed == 1 and result.terminated == 0


def test_a_live_owner_is_not_swept(owners, _reap_strays):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    fd = write_owner(owners, jobs={FAKE_JOB: job_entry(identity(pid))}, locked=True)
    try:
        result = sweep_fast(owners)
        assert alive(pid)
        assert (result.live_owners, result.dead_owners, result.jobs) == (1, 0, 0)
        assert (owners / f"{OWNER}.json").exists()
    finally:
        os.close(fd)


def test_a_reused_pid_with_a_different_start_time_is_not_signalled(owners, _reap_strays):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    proc = identity(pid)
    stale = recovery.Proc(proc.pid, proc.pgid, proc.uid, STALE_START)
    write_owner(owners, jobs={FAKE_JOB: job_entry(stale)})

    result = sweep_fast(owners)

    assert alive(pid)
    assert (result.jobs, result.gone) == (1, 1)
    assert result.terminated == result.killed == result.unverified == 0


def test_a_start_time_not_in_ps_form_is_dropped_and_signals_nothing(owners, _reap_strays):
    """``ps`` output is whitespace-normalized; a padded ``lstart`` is not a valid identity."""
    pid = spawn_orphan()
    _reap_strays.append(pid)
    proc = identity(pid)
    padded = recovery.Proc(proc.pid, proc.pgid, proc.uid, "Mon Jan  1 00:00:00 2001")
    write_owner(owners, jobs={FAKE_JOB: job_entry(padded)})

    result = sweep_fast(owners)

    assert alive(pid)
    assert (result.jobs, result.gone) == (0, 0)
    assert result.terminated == result.killed == result.failed == 0


def test_a_group_id_alone_is_never_enough(owners, _reap_strays):
    """A record whose member is gone does not license a signal to the live group."""
    pid = spawn_orphan()
    _reap_strays.append(pid)
    proc = identity(pid)
    other = recovery.Proc(proc.pid + 1_000_000, proc.pgid, proc.uid, proc.start)
    write_owner(owners, jobs={FAKE_JOB: job_entry(other)})

    result = sweep_fast(owners)

    assert alive(pid) and result.gone == 1


def test_records_of_another_user_or_group_are_never_signalled(owners, _reap_strays):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    proc = identity(pid)
    foreign = job_entry(proc)
    foreign["members"][0]["uid"] = proc.uid + 1
    other_group = job_entry(proc, pgid=proc.pgid + 1)
    own_group = job_entry(proc, pgid=os.getpgrp())
    write_owner(
        owners,
        jobs={
            "job_bbbbbbbbbbbb": foreign,
            "job_cccccccccccc": other_group,
            "job_dddddddddddd": own_group,
        },
    )

    result = sweep_fast(owners)

    assert alive(pid)
    assert result.terminated == result.killed == result.failed == 0


def _record(proc: recovery.Proc, **overrides) -> dict:
    body = {
        "v": 1, "owner": OWNER, "pid": 1, "uid": os.getuid(),
        "jobs": {FAKE_JOB: job_entry(proc)},
    }
    body.update(overrides)
    return body


def _bad_member(proc: recovery.Proc) -> dict:
    entry = job_entry(proc)
    entry["members"][0]["start"] = "not a start time"
    return {FAKE_JOB: entry}


#: name -> (record for a live victim, whether the whole record counts as invalid)
MALFORMED = {
    "garbage": (lambda p: b"not json", True),
    "list": (lambda p: b"[]", True),
    "oversized": (lambda p: b"x" * (recovery.MAX_RECORD_BYTES + 10), True),
    "version": (lambda p: _record(p, v=2), True),
    "owner-mismatch": (lambda p: _record(p, owner="owner_" + "1" * 16), True),
    "other-uid": (lambda p: _record(p, uid=os.getuid() + 1), True),
    "jobs-list": (lambda p: _record(p, jobs=[]), True),
    "bad-job-id": (lambda p: _record(p, jobs={"../etc": job_entry(p)}), False),
    "bad-start": (lambda p: _record(p, jobs=_bad_member(p)), False),
    "too-many-members": (
        lambda p: _record(
            p,
            jobs={FAKE_JOB: job_entry(p, members=job_entry(p)["members"] * 9)},
        ),
        False,
    ),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_records_are_ignored_and_signal_nothing(owners, _reap_strays, name):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    build, invalid = MALFORMED[name]
    write_owner(owners, record=build(identity(pid)))

    result = sweep_fast(owners)

    assert alive(pid)
    assert result.terminated == result.killed == result.failed == 0
    assert result.invalid == (1 if invalid else 0)


def test_a_symlinked_record_or_directory_is_ignored(tmp_path, _reap_strays):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    proc = identity(pid)
    real = tmp_path / "real"
    write_owner(real, jobs={FAKE_JOB: job_entry(proc)})

    # The whole directory is a symlink.
    link = tmp_path / "link"
    link.symlink_to(real)
    assert recovery.sweep(link, budget_s=3.0).dead_owners == 0
    assert alive(pid)

    # One record is a symlink to a valid-looking file.
    guarded = tmp_path / "guarded"
    write_owner(guarded, jobs={})
    (guarded / f"{OWNER}.json").unlink()
    (guarded / f"{OWNER}.json").symlink_to(real / f"{OWNER}.json")
    result = sweep_fast(guarded)
    assert alive(pid) and result.terminated == 0


def test_a_spent_budget_escalates_to_kill_instead_of_waiting(owners, _reap_strays):
    pid = spawn_orphan(ignore_term=True)
    _reap_strays.append(pid)
    write_owner(owners, jobs={FAKE_JOB: job_entry(identity(pid))})

    started = time.monotonic()
    result = recovery.sweep(owners, budget_s=1.5, grace_s=30.0, kill_wait_s=5.0)

    assert time.monotonic() - started < 8.0  # not the 30 s grace
    assert wait_gone(pid) and result.killed == 1


def test_a_sweep_with_no_budget_left_signals_nothing_and_keeps_the_record(
    owners, _reap_strays
):
    pid = spawn_orphan()
    _reap_strays.append(pid)
    write_owner(owners, jobs={FAKE_JOB: job_entry(identity(pid))})

    result = recovery.sweep(owners, budget_s=0.0)

    assert alive(pid)
    assert (owners / f"{OWNER}.json").exists()
    assert result.terminated == result.killed == 0


# -- the reader side ----------------------------------------------------------


def test_job_may_be_live_is_true_only_for_a_live_owners_record(owners):
    assert recovery.job_may_be_live(owners, SESSION, FAKE_JOB) is False  # no directory
    proc = recovery.Proc(4242, 4242, os.getuid(), STALE_START)
    fd = write_owner(owners, jobs={FAKE_JOB: job_entry(proc)}, locked=True)
    try:
        assert recovery.job_may_be_live(owners, SESSION, FAKE_JOB) is True
        assert recovery.job_may_be_live(owners, "other", FAKE_JOB) is False
        assert recovery.job_may_be_live(owners, SESSION, "job_eeeeeeeeeeee") is False
    finally:
        os.close(fd)
    # The owner died: its record no longer proves anything is running.
    assert recovery.job_may_be_live(owners, SESSION, FAKE_JOB) is False


def test_an_unreadable_record_of_a_live_owner_fails_closed(owners):
    fd = write_owner(owners, record=b"garbage", locked=True)
    try:
        assert recovery.job_may_be_live(owners, SESSION, FAKE_JOB) is True
    finally:
        os.close(fd)


# -- the owner side -----------------------------------------------------------


def test_owner_files_are_private_and_nothing_exists_until_needed(tmp_path):
    directory = tmp_path / "state" / "shell-owners"
    owner = recovery.OwnerRecord(directory)
    assert not directory.exists()
    assert owner.activate()
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    names = sorted(p.name for p in directory.iterdir())
    assert names == [f"{owner.owner_id}.json", f"{owner.owner_id}.lock"]
    for path in directory.iterdir():
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    owner.release()
    assert list(directory.iterdir()) == []


def test_a_sweep_between_open_and_flock_cannot_hide_a_live_owner(tmp_path, monkeypatch):
    directory = tmp_path / "shell-owners"
    real_lock = recovery._lock_nb
    injected: list[str] = []

    def lock_after_sweep(fd: int) -> bool:
        if not injected:
            injected.append("sweep")
            # The sweep wins the brand-new lock, deletes it and lets go -- all
            # between the owner's open() and its flock().
            result = recovery.sweep(directory, budget_s=5.0)
            assert result.dead_owners == 1
        return real_lock(fd)

    monkeypatch.setattr(recovery, "_lock_nb", lock_after_sweep)
    owner = recovery.OwnerRecord(directory)
    try:
        assert owner.activate() is True
        monkeypatch.setattr(recovery, "_lock_nb", real_lock)
        assert injected == ["sweep"]
        assert (directory / f"{owner.owner_id}.lock").exists()
        proc = recovery.Proc(4242, 4242, os.getuid(), STALE_START)
        assert owner.track(FAKE_JOB, SESSION, proc.pgid, [proc])
        assert recovery.job_may_be_live(directory, SESSION, FAKE_JOB) is True
        assert recovery.sweep(directory).live_owners == 1
    finally:
        owner.release()


def test_an_empty_member_entry_of_a_live_owner_may_be_live_and_is_never_signalled(owners):
    owner = recovery.OwnerRecord(owners)
    try:
        assert owner.track(FAKE_JOB, SESSION, 4242, [])
        assert recovery.job_may_be_live(owners, SESSION, FAKE_JOB) is True
        assert recovery.sweep(owners).live_owners == 1
        owner.release(keep_record=True)
        result = recovery.sweep(owners)
        assert (result.dead_owners, result.unverified, result.terminated) == (1, 1, 0)
        assert any(p.suffix == ".json" for p in owners.iterdir())  # kept, retried later
    finally:
        owner.release()


async def test_a_job_whose_identity_could_not_be_read_still_reads_as_live(
    owners, tmp_path, monkeypatch
):
    monkeypatch.setattr(recovery, "identify", lambda pid: None)
    registry = _jobs.JobRegistry(state_dir=owners)
    try:
        job = await registry.spawn("sleep 300", session_id=SESSION, cwd=tmp_path, background=True)
        key = registry._session_key(SESSION)
        (path,) = [p for p in owners.iterdir() if p.suffix == ".json"]
        assert json.loads(path.read_text())["jobs"][job.job_id]["members"] == []
        # An independent probe (another process's view): the live owner lists it.
        assert recovery.job_may_be_live(owners, key, job.job_id) is True
    finally:
        await registry.aclose()


def test_a_sweep_stays_inside_its_budget_with_many_groups(owners, _reap_strays):
    jobs = {}
    for index in range(3):
        pid = spawn_orphan(ignore_term=True)
        _reap_strays.append(pid)
        jobs[f"job_{index:012x}"] = job_entry(identity(pid))
    write_owner(owners, jobs=jobs)

    started = time.monotonic()
    result = recovery.sweep(owners, budget_s=2.0, grace_s=30.0, kill_wait_s=30.0)

    assert time.monotonic() - started < 2.0 + recovery._SWEEP_SLACK_S + 1.0
    assert result.jobs == 3 and result.killed + result.unverified + result.failed == 3


def test_a_symlinked_owner_directory_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "shell-owners"
    link.symlink_to(target)
    owner = recovery.OwnerRecord(link)
    assert owner.activate() is False and owner.error
    assert list(target.iterdir()) == []


# -- the registry ----------------------------------------------------------------


async def test_a_graceful_shutdown_leaves_no_files_and_no_process(owners, tmp_path):
    registry = _jobs.JobRegistry(state_dir=owners)
    job = await registry.spawn(
        "sleep 300", session_id=SESSION, cwd=tmp_path, background=True, persistent=True
    )
    assert any(p.suffix == ".json" for p in owners.iterdir())
    assert registry.job_may_be_live(SESSION, job.job_id) is True

    await registry.aclose()

    assert list(owners.iterdir()) == []
    assert wait_gone(job.pid)


async def test_a_finished_job_is_no_longer_recorded(owners, tmp_path):
    registry = _jobs.JobRegistry(state_dir=owners)
    try:
        job = await registry.spawn("sleep 300", session_id=SESSION, cwd=tmp_path, background=True)
        (path,) = [p for p in owners.iterdir() if p.suffix == ".json"]
        assert job.job_id in json.loads(path.read_text())["jobs"]
        await registry.kill(job.job_id, session_id=SESSION)
        assert job.job_id not in json.loads(path.read_text())["jobs"]
        assert registry.job_may_be_live(SESSION, job.job_id) is False
    finally:
        await registry.aclose()


async def test_a_registry_without_a_state_dir_tracks_nothing_and_proves_nothing(tmp_path):
    registry = _jobs.JobRegistry()
    try:
        assert registry.recovery_dir is None
        assert registry.job_may_be_live(SESSION, FAKE_JOB) is True  # fail closed
        assert await registry.recover_orphans() is None
    finally:
        await registry.aclose()


OWNER_SCRIPT = r'''
import asyncio, json, sys
from pathlib import Path
from nexus.tools.builtin import _jobs

_jobs._MEMBER_REFRESH_S = 0.2


async def main(state, cwd, command):
    registry = _jobs.JobRegistry(state_dir=Path(state))
    job = await registry.spawn(
        command, session_id="s1", cwd=Path(cwd), background=True, persistent=True
    )
    deadline = asyncio.get_running_loop().time() + 20
    pid = None
    while asyncio.get_running_loop().time() < deadline:
        text = job.stdout.text()[0].strip()
        if text.isdigit() and pid is None:
            pid = int(text)
        for path in Path(state).glob("owner_*.json"):
            try:
                jobs = json.loads(path.read_text())["jobs"]
            except (ValueError, OSError, KeyError):
                continue
            members = jobs.get(job.job_id, {}).get("members", [])
            if pid is not None and any(m["pid"] == pid for m in members):
                print("READY", pid, job.pgid, flush=True)
                await asyncio.sleep(3600)
        await asyncio.sleep(0.05)
    print("TIMEOUT", flush=True)


asyncio.run(main(*sys.argv[1:4]))
'''


def run_owner(tmp_path: Path, owners: Path, command: str) -> tuple[subprocess.Popen, int, int]:
    script = tmp_path / "owner_script.py"
    script.write_text(OWNER_SCRIPT)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, str(script), str(owners), str(work), command],
        stdout=subprocess.PIPE, text=True, env=env, cwd=str(work),
    )
    line = proc.stdout.readline().split()
    assert line and line[0] == "READY", f"owner did not become ready: {line!r}"
    return proc, int(line[1]), int(line[2])


def test_sigkill_of_the_owner_then_sweep_stops_a_persistent_survivor(
    tmp_path, owners, _reap_strays
):
    """The daemon half of P7: ``kill -9`` while the shell leader already exited."""
    proc, member_pid, pgid = run_owner(
        tmp_path, owners, "sleep 300 >/dev/null 2>&1 & echo $!"
    )
    _reap_strays.append(member_pid)
    try:
        # The shell leader (pid == pgid) exited; only the survivor represents the job.
        assert alive(member_pid) and pgid != member_pid
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert alive(member_pid)  # kill -9 left the orphan behind

        result = sweep_fast(owners)

        assert wait_gone(member_pid)
        assert result.dead_owners == 1 and result.terminated + result.killed == 1
        assert list(owners.iterdir()) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def test_sigkill_of_the_owner_then_sweep_stops_a_live_leader_group(
    tmp_path, owners, _reap_strays
):
    proc, member_pid, _pgid = run_owner(tmp_path, owners, "sleep 300 & echo $!; wait")
    _reap_strays.append(member_pid)
    try:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert alive(member_pid)

        sweep_fast(owners)

        assert wait_gone(member_pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


# -- the runtime, daemon and sessions ----------------------------------------------


def _config() -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


def _runtime(workspace: Path, **kwargs) -> tuple[Runtime, ScriptedProvider]:
    provider = ScriptedProvider(*[text_response("ok") for _ in range(8)])
    runtime = Runtime(
        workspace, config=_config(), providers={"scripted": provider}, **kwargs
    )
    return runtime, provider


async def test_recovery_state_follows_the_home_override_and_project_key(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    runtime, _ = _runtime(workspace, home=home)
    try:
        expected = project_state_dir(workspace, home) / recovery.OWNERS_DIRNAME
        assert runtime.job_registry.recovery_dir == expected
        assert str(expected).startswith(str(home))
    finally:
        await runtime.aclose()


async def test_the_daemon_start_sweeps_a_killed_daemons_orphans(_reap_strays):
    base = Path(tempfile.mkdtemp(prefix="nexus-p7-", dir="/tmp"))
    workspace = base / "ws"
    workspace.mkdir()
    pid = spawn_orphan()
    _reap_strays.append(pid)
    owners = project_state_dir(workspace) / recovery.OWNERS_DIRNAME
    write_owner(owners, jobs={FAKE_JOB: job_entry(identity(pid))})

    def factory(ws, **_kwargs):
        return _runtime(ws)[0]

    daemon = Daemon(workspace, socket_path=base / "d.sock", runtime_factory=factory)
    try:
        await daemon.start()
        try:
            assert wait_gone(pid)
            assert not (owners / f"{OWNER}.json").exists()
        finally:
            await daemon.aclose()
        assert not list(owners.glob("owner_*.json"))
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def test_a_failing_sweep_never_stops_the_daemon_starting():
    base = Path(tempfile.mkdtemp(prefix="nexus-p7-", dir="/tmp"))
    try:
        workspace = base / "ws"
        workspace.mkdir()

        class Broken:
            async def recover_shell_orphans(self):
                raise RuntimeError("boom")

        daemon = Daemon(workspace, socket_path=base / "d.sock")
        daemon._runtime = Broken()
        await daemon._recover_shell_orphans()  # must not raise
    finally:
        shutil.rmtree(base, ignore_errors=True)


def exits(handle) -> list:
    return [e for e in handle.events if e.type == "shell.job_exited"]


def reopen(runtime: Runtime):
    runtime.sessions.evict(SESSION)
    return runtime.sessions.open(SESSION)


def started_event(handle, job_id: str) -> None:
    handle.emit_session_event(
        "shell.job_started",
        {"job_id": job_id, "command": "sleep 300", "cwd": "/", "pid": 4242,
         "background": True, "started_at": time.time() - 5, "persistent": True},
    )


async def test_a_reopened_session_records_one_interrupted_exit_and_wakes_nothing(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, provider = _runtime(workspace)
    try:
        handle = runtime.sessions.open(SESSION)
        handle.append_message(Message(role="user", content=[Text(text="start a server")]))
        started_event(handle, FAKE_JOB)
        before = len(handle.events)

        for _ in range(3):  # reopen repeatedly: still exactly one exit
            handle = reopen(runtime)
            await asyncio.sleep(0)
            (exit_event,) = exits(handle)
            assert exit_event.data["job_id"] == FAKE_JOB
            assert exit_event.data["status"] == "interrupted"
            assert exit_event.data["delivery"] == "none"
            assert exit_event.data["recovered"] is True

        assert len(handle.events) > before
        assert not [
            e for e in handle.events
            if e.type in ("input.queued", "input.consumed", "turn.started", "turn.completed")
        ]
        assert handle.queue_depth == 0
        assert provider.calls == 0
    finally:
        await runtime.aclose()


async def test_a_session_does_not_close_a_job_a_live_owner_still_runs(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, _ = _runtime(workspace)
    try:
        handle = runtime.sessions.open(SESSION)
        handle.append_message(Message(role="user", content=[Text(text="go")]))
        job = await runtime.job_registry.spawn(
            "sleep 300", session_id=SESSION, cwd=workspace, background=True,
            persistent=True,
        )
        assert runtime.shell_job_may_be_live(SESSION, job.job_id) is True
        # A fresh handle's recovery pass sees the started job and must leave it.
        handle._interrupted_checked = False
        handle.recover_dangling_tool_uses()
        assert exits(handle) == []

        # A job in a *different* live owner's record is protected the same way.
        proc = identity(job.pid)
        fd = write_owner(
            runtime.job_registry.recovery_dir,
            owner="owner_" + "2" * 16,
            jobs={"job_ffffffffffff": job_entry(proc)},
            locked=True,
        )
        try:
            started_event(handle, "job_ffffffffffff")
            handle._interrupted_checked = False
            handle.recover_dangling_tool_uses()
            assert exits(handle) == []
        finally:
            os.close(fd)
        # ... and once that owner is dead, it is closed.
        handle._interrupted_checked = False
        handle.recover_dangling_tool_uses()
        assert [e.data["job_id"] for e in exits(handle)] == ["job_ffffffffffff"]
    finally:
        await runtime.aclose()


async def test_a_session_without_a_probe_never_writes_a_recovery_event(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, _ = _runtime(workspace)
    try:
        handle = runtime.sessions.open(SESSION)
        handle.append_message(Message(role="user", content=[Text(text="go")]))
        started_event(handle, FAKE_JOB)
        handle._job_probe = None
        handle._interrupted_checked = False
        handle.recover_dangling_tool_uses()
        assert exits(handle) == []

        def broken(session_id, job_id):
            raise RuntimeError("probe failed")

        handle._job_probe = broken
        handle._interrupted_checked = False
        handle.recover_dangling_tool_uses()
        assert exits(handle) == []  # an unprovable job is left alone
    finally:
        await runtime.aclose()
