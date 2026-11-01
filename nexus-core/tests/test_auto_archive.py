"""Automatic archive of root session trees idle longer than the configured threshold."""
import asyncio

import pytest

from nexus.autoarchive import AutoArchive, configured_idle_seconds
from nexus.bus import Bus
from nexus.config import ConfigFile
from nexus.runtime import Runtime
from nexus.store import Store

NOW = 1_000_000.0
HOUR = 3600.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NEXUS10_HOME", str(home))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    store = Store(tmp_path / "nexus.db")
    project, _ = store.project_for_path(str(workspace))
    rt = Runtime(store, Bus())
    return rt, store, project, home


def _session(store, project, title, *, idle):
    session = store.create_session(project, title=title, agent="build")
    with store.db:
        store.db.execute("update session set updated = ? where id = ?", (NOW - idle, session.id))
    return session


def test_default_threshold_is_one_hour_and_strict(env):
    rt, store, project, home = env
    assert ConfigFile(home / "config.toml").read()["values"]["sessions"]["auto_archive_idle_seconds"] == 3600
    assert configured_idle_seconds(ConfigFile(home / "config.toml")) == HOUR
    created = {name: _session(store, project, name, idle=seconds)
               for name, seconds in (("fresh", 60), ("exactly", HOUR), ("just-over", HOUR + 1), ("old", 5 * HOUR))}

    assert rt.autoarchive.sweep(now=NOW) == [created["old"].id, created["just-over"].id]
    sessions = {s.title: s for s in store.sessions(archived="include")}
    assert sessions["fresh"].archived == 0 and sessions["exactly"].archived == 0
    assert sessions["just-over"].archived == 1 and sessions["old"].archived == 1
    events = [e for e in store.events(created["old"].id, limit=50) if e["type"] == "session.archived"]
    assert events[0]["data"] == {"root": created["old"].id, "sessions": [created["old"].id], "archived": True}


def test_zero_disables_and_the_threshold_is_read_live(env):
    rt, store, project, home = env
    session = _session(store, project, "old", idle=5 * HOUR)
    config = ConfigFile(home / "config.toml")
    config.patch(config.read()["revision"], {"sessions.auto_archive_idle_seconds": 0}, [])
    assert rt.autoarchive.sweep(now=NOW) == []
    assert store.session(session.id).archived == 0

    config = ConfigFile(home / "config.toml")
    config.patch(config.read()["revision"], {"sessions.auto_archive_idle_seconds": int(2 * HOUR)}, [])
    assert rt.autoarchive.sweep(now=NOW) == [session.id]
    assert store.session(session.id).archived == 1


def test_activity_in_a_child_keeps_the_whole_tree(env):
    rt, store, project, _ = env
    root = _session(store, project, "root", idle=5 * HOUR)
    child = store.create_session(project, title="child", agent="build", parent=root)
    with store.db:
        store.db.execute("update session set updated = ? where id = ?", (NOW - 2 * HOUR, root.id))
        store.db.execute("update session set updated = ? where id = ?", (NOW - 60, child.id))
    assert rt.autoarchive.sweep(now=NOW) == []
    assert store.session(root.id).archived == 0 and store.session(child.id).archived == 0

    with store.db:
        store.db.execute("update session set updated = ? where id = ?", (NOW - 2 * HOUR, child.id))
    assert rt.autoarchive.sweep(now=NOW) == [root.id]
    assert store.session(root.id).archived == 1 and store.session(child.id).archived == 1


async def test_running_waiting_queued_and_job_sessions_are_skipped(env):
    rt, store, project, _ = env
    running = _session(store, project, "running", idle=5 * HOUR)
    approval = _session(store, project, "approval", idle=5 * HOUR)
    question = _session(store, project, "question", idle=5 * HOUR)
    queued = _session(store, project, "queued", idle=5 * HOUR)
    job = _session(store, project, "job", idle=5 * HOUR)
    idle = _session(store, project, "idle", idle=5 * HOUR)

    gate = asyncio.Event()

    async def blocked():
        await gate.wait()

    rt.turns[running.id] = asyncio.create_task(blocked())
    future = asyncio.get_running_loop().create_future()
    rt.approvals["per_1"] = (approval.id, future, {})
    rt.questions["qst_1"] = type("Question", (), {"session_id": question.id})()
    rt.inputs.add(queued.id, "queue", [{"type": "text", "text": "later"}])
    rt.jobs.items["job_1"] = type("Job", (), {"root": job.id, "finished": None, "started": 0.0})()

    try:
        assert rt.autoarchive.sweep(now=NOW) == [idle.id]
        assert {s.title for s in store.sessions(archived="only")} == {"idle"}
        for session in (running, approval, question, queued, job):
            assert store.session(session.id).archived == 0
    finally:
        rt.turns[running.id].cancel()
        gate.set()
        future.cancel()
        del rt.turns[running.id]
        rt.approvals.clear()
        rt.questions.clear()
        rt.jobs.items.clear()
        rt.inputs.remove(queued.id, rt.inputs.list(queued.id)[0]["id"])


def test_reads_and_listings_do_not_refresh_activity(env):
    rt, store, project, _ = env
    session = _session(store, project, "quiet", idle=2 * HOUR)
    before = store.session(session.id).updated
    store.sessions(archived="include")
    store.session(session.id)
    rt.session_json(store.session(session.id))
    assert store.session(session.id).updated == before == NOW - 2 * HOUR
    assert rt.autoarchive.sweep(now=NOW) == [session.id]


def test_excluded_busy_roots_do_not_fill_the_sweep_limit(env):
    _, store, project, _ = env
    oldest = _session(store, project, "oldest", idle=5 * HOUR)
    middle = _session(store, project, "middle", idle=4 * HOUR)
    newest = _session(store, project, "newest", idle=3 * HOUR)
    before = NOW - HOUR
    assert store.stale_root_sessions(before=before, limit=2) == [oldest.id, middle.id]
    assert store.stale_root_sessions(before=before, limit=2, exclude={oldest.id}) == [middle.id, newest.id]


def test_unarchive_starts_a_new_idle_window(env):
    rt, store, project, _ = env
    session = _session(store, project, "restored", idle=5 * HOUR)
    assert rt.autoarchive.sweep(now=NOW) == [session.id]
    restored = rt.archive_session(session.id, archived=False)
    assert restored.updated > NOW
    assert rt.autoarchive.sweep(now=NOW) == []
    assert store.session(session.id).archived == 0


def test_sweep_archives_once_and_reports_the_roots(env):
    rt, store, project, _ = env
    first = _session(store, project, "first", idle=3 * HOUR)
    second = _session(store, project, "second", idle=2 * HOUR)
    assert rt.autoarchive.sweep(now=NOW) == [first.id, second.id]
    assert rt.autoarchive.last_archived == [first.id, second.id]
    assert rt.autoarchive.sweep(now=NOW) == []
    events = [e for e in store.events(first.id, limit=100) if e["type"] == "session.archived"]
    assert len(events) == 1


async def test_daemon_maintenance_starts_the_sweeper(env):
    rt, store, project, _ = env
    stale = _session(store, project, "stale", idle=2 * HOUR)
    await rt.start_maintenance()
    try:
        for _ in range(100):
            if store.session(stale.id).archived:
                break
            await asyncio.sleep(0.01)
        assert store.session(stale.id).archived == 1
    finally:
        await rt.shutdown()


async def test_the_scheduler_sweeps_on_start_and_stops_cleanly():
    class Fake:
        def __init__(self):
            self.calls = []

        def archive_stale_sessions(self, *, older_than, now=None):
            self.calls.append(older_than)
            return []

    runtime = Fake()
    auto = AutoArchive(runtime, interval_seconds=1)
    auto.start()
    for _ in range(100):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)
    await auto.stop()
    assert runtime.calls == [HOUR]
    assert auto.last_error is None
    assert auto._task is None


def test_unreadable_config_keeps_the_default(env, tmp_path):
    home = tmp_path / "home"
    (home / "config.toml").write_text("[sessions]\nauto_archive_idle_seconds = -1\n")
    assert configured_idle_seconds(ConfigFile(home / "config.toml")) == HOUR
    (home / "config.toml").write_text("not toml")
    assert configured_idle_seconds(ConfigFile(home / "config.toml")) == HOUR
