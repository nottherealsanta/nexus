"""Dashboard order follows the newest USER prompt, never agent or tool activity."""
from __future__ import annotations

import time

from nexus.events import Event
from nexus.model.message import Message, MessageMeta, Text, ToolResult
from nexus.session.db import SqliteSessionStore, StateDatabase, _backfill_last_user_at
from nexus.session.manager import SessionManager, SessionSummary
from nexus.ui_support.session_groups import _session_groups


def _msg(text: str, ts: float, role: str = "user") -> Message:
    return Message(role=role, content=[Text(text=text)], meta=MessageMeta(ts=ts))


def _tool_reply(ts: float) -> Message:
    return Message(
        role="user",
        content=[ToolResult(tool_use_id="t", content=[Text(text="out")])],
        meta=MessageMeta(ts=ts),
    )


def _store(tmp_path) -> SqliteSessionStore:
    return SqliteSessionStore(StateDatabase(tmp_path / "nexus.db"), "p", root="/w")


def _order(store: SqliteSessionStore) -> list[str]:
    return [row["id"] for row in store.db.project_sessions()]


def test_agent_and_tool_updates_do_not_reorder_but_user_messages_do(tmp_path):
    store = _store(tmp_path)
    for name, ts in (("old", 100.0), ("new", 200.0)):
        store.create(name)
        store.append_message(name, _msg("hi", ts))
    assert _order(store) == ["new", "old"]

    # Much later agent text, tool results and events on the older session.
    store.append_message("old", _msg("working", 900.0, "assistant"))
    store.append_message("old", _tool_reply(910.0))
    store.append_event("old", Event("turn.completed", {}, ts=920.0))
    row = store.session_row("old")
    assert row["last_activity"] == 920.0  # general activity still advances
    assert row["last_user_at"] == 100.0
    assert _order(store) == ["new", "old"]

    store.append_message("old", _msg("follow up", 1000.0))
    assert _order(store) == ["old", "new"]


def test_queued_input_counts_as_a_user_submission(tmp_path):
    store = _store(tmp_path)
    for name, ts in (("a", 100.0), ("b", 200.0)):
        store.create(name)
        store.append_message(name, _msg("hi", ts))
    store.append_event("a", Event("input.queued", {"queued_id": "q"}, ts=500.0))
    assert _order(store) == ["a", "b"]


def test_equal_user_times_tie_break_deterministically(tmp_path):
    store = _store(tmp_path)
    for name in ("b", "a", "c"):
        store.create(name)
        store.append_message(name, _msg("hi", 100.0))
    assert _order(store) == ["a", "b", "c"]


def test_never_messaged_session_falls_back_to_creation_time():
    summary = SessionSummary(id="x", created_at=50.0, last_activity=999.0)
    assert summary.order_time == 50.0
    assert SessionSummary(id="y", last_activity=7.0).order_time == 7.0


def test_backfill_restores_last_user_at_from_prompts_only(tmp_path):
    store = _store(tmp_path)
    store.create("s")
    store.append_message("s", _msg("hi", 100.0))
    store.append_message("s", _msg("reply", 300.0, "assistant"))
    store.append_message("s", _tool_reply(400.0))
    conn = store.db._connection()
    conn.execute("UPDATE sessions SET last_user_at=0")
    _backfill_last_user_at(conn)
    assert store.session_row("s")["last_user_at"] == 100.0


def test_backfill_counts_queued_input_like_live_appends(tmp_path):
    store = _store(tmp_path)
    store.create("s")
    store.append_message("s", _msg("hi", 100.0))
    store.append_event("s", Event("input.queued", {"queued_id": "q"}, ts=500.0))
    store.append_message("s", _msg("reply", 900.0, "assistant"))
    live = store.session_row("s")["last_user_at"]
    conn = store.db._connection()
    conn.execute("UPDATE sessions SET last_user_at=0")
    _backfill_last_user_at(conn)
    assert store.session_row("s")["last_user_at"] == live == 500.0


def test_group_ties_break_by_workspace_then_id():
    a = SessionSummary(id="a", last_user_at=100.0)
    b = SessionSummary(id="b", last_user_at=100.0)
    c = SessionSummary(id="c", last_user_at=100.0)
    groups = _session_groups([("/z", a), ("/y", c), ("/y", b)], "")
    assert [s.id for _, items in groups for s in items] == ["b", "c", "a"]


def test_manager_list_and_summary_use_user_time(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.store
    for name, ts in (("old", 100.0), ("new", 200.0)):
        store.create(name)
        store.append_message(name, _msg("hi", ts))
    store.append_message("old", _msg("later agent reply", time.time(), "assistant"))
    assert [s.id for s in manager.list()] == ["new", "old"]
    assert manager.summary("old").last_user_at == 100.0


def test_native_groups_sort_by_user_time():
    old = SessionSummary(id="old", last_user_at=100.0, last_activity=9000.0)
    new = SessionSummary(id="new", last_user_at=200.0, last_activity=300.0)
    groups = _session_groups([("/w", old), ("/w", new)], "")
    assert [s.id for _, items in groups for s in items] == ["new", "old"]
