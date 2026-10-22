"""Daemon-owned automatic archive of sessions idle for more than one hour."""

import asyncio

import pytest

from nexus.events import Event
from nexus.host import protocol as _host_protocol  # noqa: F401  (load nexus.host before host_support)
from nexus.host_support.session_archive import (
    reap_with_archive_sweep,
    sweep_stale_sessions,
)
from nexus.model.message import Message, MessageMeta, Text
from nexus.session.manager import AUTO_ARCHIVE_IDLE_SECONDS, SessionManager
from nexus.session.session import register_live_agent, unregister_live_agent

NOW = 1_000_000.0
HOUR = 3600.0


def _session(manager, session_id, last_ts):
    handle = manager.open(session_id)
    handle.append_message(Message(role="user", content=[Text(text="hi")], meta=MessageMeta(ts=0)))
    handle.append_event(Event(type="turn.completed", ts=last_ts))
    return handle


def _ids(records):
    return [record.session_id for record in records]


def test_threshold_is_one_hour_and_strict(tmp_path):
    assert AUTO_ARCHIVE_IDLE_SECONDS == HOUR
    manager = SessionManager(tmp_path)
    for name, idle in (("fresh", 60), ("exactly", HOUR), ("just-over", HOUR + 1), ("old", 5 * HOUR)):
        _session(manager, name, NOW - idle)
    manager.close_all()
    archived = manager.archive_stale(now=NOW)
    assert _ids(archived) == ["old", "just-over"]
    assert {row.reason for row in archived} == {"auto"}
    assert {s.id for s in manager.list()} == {"fresh", "exactly"}


def test_archive_survives_restart(tmp_path):
    manager = SessionManager(tmp_path)
    _session(manager, "stale", NOW - 2 * HOUR)
    assert _ids(manager.archive_stale(now=NOW)) == ["stale"]
    restarted = SessionManager(tmp_path)
    assert [(r.session_id, r.reason) for r in restarted.archived()] == [("stale", "auto")]
    assert restarted.list() == []
    assert restarted.archive_stale(now=NOW) == []


@pytest.mark.parametrize(
    "event_type,data",
    [
        ("turn.started", {}),
        ("input.queued", {"queued_id": "q1"}),
        ("permission.requested", {"id": "p1"}),
    ],
)
def test_running_queued_or_waiting_sessions_are_never_archived(tmp_path, event_type, data):
    manager = SessionManager(tmp_path)
    handle = _session(manager, "busy", NOW - 5 * HOUR)
    handle.append_event(Event(type=event_type, data=data, ts=NOW - 4 * HOUR))
    assert manager.archive_stale(now=NOW) == []
    assert SessionManager(tmp_path).archive_stale(now=NOW) == []


def test_subagent_hosting_sessions_are_skipped(tmp_path):
    manager = SessionManager(tmp_path)
    _session(manager, "hosting", NOW - 5 * HOUR)
    register_live_agent("hosting", "agent-1")
    try:
        assert manager.archive_stale(now=NOW) == []
    finally:
        unregister_live_agent("hosting", "agent-1")
    assert _ids(manager.archive_stale(now=NOW)) == ["hosting"]


def test_an_idle_session_with_a_viewer_is_still_archived(tmp_path):
    manager = SessionManager(tmp_path)
    viewed = _session(manager, "viewed", NOW - 5 * HOUR)
    viewed._viewers = 1
    assert _ids(manager.archive_stale(now=NOW)) == ["viewed"]
    assert manager.list() == []


def test_workspace_manager_is_never_archived(tmp_path):
    manager = SessionManager(tmp_path)
    mgr = manager.open("nexus-manager", recover=False, ownership={"kind": "manager"})
    mgr.append_message(Message(role="user", content=[Text(text="plan")], meta=MessageMeta(ts=0)))
    mgr.append_event(Event(type="turn.completed", ts=NOW - 10 * HOUR))
    _session(manager, "plain", NOW - 10 * HOUR)
    assert _ids(manager.archive_stale(now=NOW)) == ["plain"]
    assert manager.manager_summary() is not None
    assert manager.kind_of("nexus-manager") == "manager"


def test_reads_listings_and_presence_do_not_refresh_activity(tmp_path):
    manager = SessionManager(tmp_path)
    handle = _session(manager, "quiet", NOW - 2 * HOUR)
    before = manager.summary("quiet").last_activity
    manager.list()
    manager.list(include_archived=True)
    manager.summary("quiet")
    manager.export("quiet")
    handle.read(force=True)
    # Viewing writes presence events; they are not genuine activity.
    for kind in ("presence.joined", "presence.changed", "presence.left"):
        handle.append_event(Event(type=kind, data={"viewers": 1}, ts=NOW))
    assert manager.summary("quiet").last_activity == before
    assert _ids(manager.archive_stale(now=NOW)) == ["quiet"]


def test_genuine_activity_defers_archive(tmp_path):
    manager = SessionManager(tmp_path)
    handle = _session(manager, "chat", NOW - 2 * HOUR)
    handle.append_event(Event(type="text", data={"text": "hello"}, ts=NOW - 60))
    assert manager.archive_stale(now=NOW) == []
    assert _ids(manager.archive_stale(now=NOW + 2 * HOUR)) == ["chat"]


def test_sweep_archives_closes_handles_and_honours_the_switch(tmp_path):
    async def scenario():
        manager = SessionManager(tmp_path)
        _session(manager, "stale", NOW - 2 * HOUR)
        _session(manager, "fresh", NOW - 60)
        assert await sweep_stale_sessions(manager, 0, now=NOW) == 0
        assert await sweep_stale_sessions(manager, lambda: 0, now=NOW) == 0
        assert manager.archived() == []
        assert await sweep_stale_sessions(manager, lambda: 3600, now=NOW) == 1
        assert [r.session_id for r in manager.archived()] == ["stale"]
        assert manager.live_sessions == ("fresh",)
        assert await sweep_stale_sessions(manager, 3600, now=NOW) == 0

    asyncio.run(scenario())


def test_reaper_repeats_the_sweep_on_its_interval():
    async def scenario():
        stopped = asyncio.Event()
        calls = []

        async def sweep():
            calls.append("sweep")
            if len(calls) == 3:
                stopped.set()

        await reap_with_archive_sweep(
            stopped, 0.001, sweep, lambda: False, lambda reason: None, lambda msg: None,
            sweep_interval=0.0,
        )
        assert calls == ["sweep"] * 3

    asyncio.run(scenario())
