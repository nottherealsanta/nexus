"""Storage compaction preserves canonical state, shared bodies and recovery evidence."""
from __future__ import annotations

import json
import random
import sqlite3
import time
from types import SimpleNamespace

import pytest

from nexus.bus import Bus
from nexus.db_maintenance import compact_database
from nexus.eventlog import HistoryUnavailable, parse_cursor
from nexus.export import ExportView, stream
from nexus.live import LivePartial, partial_summary
from nexus.llmhub.types import Message, Text
from nexus.retention import Retention, Settings
from nexus.store import Store, StoreError


@pytest.fixture
def storage(tmp_path):
    store = Store(tmp_path / "nexus.db")
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title="storage", agent="build", snapshot={"system_prompt": "exact"})
    bus = Bus()
    bus.attach(store)
    yield store, bus, session
    store.close()


def test_requests_share_history_media_and_collect_components(storage):
    store, bus, session = storage
    rng = random.Random(1)
    image = "data:image/png;base64," + rng.randbytes(200_000).hex()
    history = [{"role": "user", "content": [{"image_url": image}]}]
    requests = []
    for n in range(30):
        history.append({"role": "assistant", "content": rng.randbytes(500).hex()})
        data = {"method": "POST", "headers": {}, "body": {"model": "m", "input": list(history)}}
        requests.append(bus.publish("wire_request", data, session_id=session.id))
    assert [r["data"] for r in store.events(session.id)] == [r["data"] for r in requests]
    assert store.storage_inventory()["body_bytes"] < 500_000
    assert sum(len(json.dumps(r["data"])) for r in requests) > 12_000_000
    with store.db:
        store.db.execute("delete from event where id != ?", (requests[-1]["id"],))
    assert store.events(session.id)[0]["data"] == requests[-1]["data"]
    store.schedule_wire_expiry(success_seconds=0, failure_seconds=0)
    store.expire_wire_payloads(now=time.time() + 10, byte_budget=0)
    assert store.storage_inventory()["bodies"] == 0
    assert store.storage_inventory()["body_bytes"] == 0
    assert not store.db.execute("pragma foreign_key_check").fetchall()


def test_legacy_requests_upgrade_losslessly(storage):
    from nexus import payloads
    store, bus, session = storage
    data = {"body": {"input": [{"content": "legacy"}]}}
    sha = payloads.store_body(store.db, data)
    event = bus.publish("probe", data, session_id=session.id)
    with store.db:
        store.db.execute("update event set type = 'wire_request', body_sha = ? where id = ?", (sha, event["id"]))
    assert store.deduplicate_requests(limit=1) == 1
    assert store.events(session.id)[0]["data"] == data
    assert store.deduplicate_requests(limit=1) == 0
    assert store.db.execute("select 1 from event_body where sha = ?", (sha,)).fetchone() is None


def test_partial_interleaves_text_reasoning_and_two_calls():
    p = LivePartial("r")
    events = [
        {"type": "reasoning_delta", "text": "think"}, {"type": "text_delta", "text": "hello"},
        {"type": "tool_call_started", "id": "a", "name": "read"},
        {"type": "tool_call_started", "id": "b", "name": "bash"},
        {"type": "tool_args_delta", "id": "a", "delta": '{"path":'},
        {"type": "tool_args_delta", "id": "b", "delta": '{}'},
        {"type": "part_done", "part": {"type": "text", "text": "hello world", "raw": {"exact": True}}},
        {"type": "part_done", "part": {"type": "tool_call", "id": "a", "name": "read", "arguments": '{"path":"."}'}},
    ]
    for seq, data in enumerate(events, 1):
        out = p.apply(data)
        assert out["request_id"] == "r" and out["seq"] == seq
    assert p.parts[1]["text"] == "hello world" and p.parts[1]["done"]
    assert p.parts[2]["arguments"] == '{"path":"."}' and p.parts[3]["arguments"] == '{}'
    summary = partial_summary(p.json())
    assert summary["counts"]["text_delta"]["chars"] == 5
    assert summary["parts"] == {"text": 1, "tool_call": 1} and len(summary["tool_calls"]) == 2


async def test_ephemeral_does_not_advance_cursor_or_overflow_watermark(storage):
    store, bus, session = storage
    bus.queue_max = 2
    sub = bus.open()
    durable = bus.publish("probe", {}, session_id=session.id)
    ephemeral = bus.publish("text_delta", {"text": "live"}, session_id=session.id, durable=False)
    bus.publish("text_delta", {"text": "overflow"}, session_id=session.id, durable=False)
    assert ephemeral["id"] is None and store.log_head() == durable["id"]
    assert await sub.get() == durable
    assert await sub.get() == ephemeral
    assert (await sub.get())["data"]["dropped_after"] == durable["id"]
    assert store.event_counts(session.id) == {"probe": 1}
    sub.close()


def _announce(store, bus, sid):
    msg = store.append_message(sid, Message("user", (Text("kept"),)))
    event = bus.publish("message.added", {"id": msg.id}, session_id=sid)
    store.link_message_event(msg.id, event["id"])


def test_compaction_keeps_context_waits_code_and_exports_and_allows_resume(storage):
    store, bus, session = storage
    _announce(store, bus, session.id)
    bus.publish("wire_request", {"body": {"input": ["past"]}}, session_id=session.id)
    bus.publish("question.asked", {"id": "q", "questions": []}, session_id=session.id)
    bus.publish("question.replied", {"id": "q", "outcome": "answered"}, session_id=session.id)
    bus.publish("code.cell", {}, session_id=session.id)
    before = (store.messages(session.id), store.snapshot(session.id), store.wait_lifecycle(session.id))
    head = store.log_head()
    store.set_tree_archived(session.id, True)
    while store.compact_session(session.id, now=time.time(), batch_size=2):
        pass
    assert before == (store.messages(session.id), store.snapshot(session.id), store.wait_lifecycle(session.id))
    assert store.code_used(session.id) and store.event_counts(session.id) == {"session.compacted": 1}
    assert store.compaction(session.id)["removed"] == head and store.storage_inventory()["bodies"] == 0
    with pytest.raises(HistoryUnavailable):
        parse_cursor(f"{store.log_generation()}:0", generation=store.log_generation(),
                     head=store.log_head(), floor=store.log_floor())
    store.set_tree_archived(session.id, False)
    assert bus.publish("probe", {}, session_id=session.id)["id"] > head
    assert store.compact_session(session.id, now=time.time()) is None
    exported = json.loads("".join(stream(ExportView(store.path, session.id, "session"), "json")))
    assert exported["sessions"][0]["events_compacted"]["removed"] == head
    assert len(exported["sessions"][0]["messages"]) == 1


@pytest.mark.parametrize("blocker", ["turn", "request", "wait", "job", "replay", "outbox", "keep", "recovery"])
def test_compaction_refuses_protected_sessions(storage, blocker):
    store, bus, session = storage
    bus.publish("probe", {}, session_id=session.id)
    store.set_tree_archived(session.id, True)
    if blocker in ("turn", "request", "recovery"):
        t = store.start_turn(session.id)
        if blocker == "request":
            store.start_request(t["id"], session.id, 1)
            store.finish_turn(t["id"], "completed")
            store.set_turn_recovery(t["id"], "none")
        elif blocker == "recovery":
            store.finish_turn(t["id"], "completed")
            store.set_turn_recovery(t["id"], "pending")
    elif blocker == "wait":
        bus.publish("question.asked", {"id": "q"}, session_id=session.id)
    elif blocker == "job":
        bus.publish("bash.started", {"id": "j"}, session_id=session.id)
    elif blocker == "replay":
        store.pin_events(1, store.log_head())
    elif blocker == "outbox":
        store.append_message(session.id, Message("user", (Text("unannounced"),)))
    elif blocker == "keep":
        store.keep_diagnostics(session.id, True)
    assert store.compact_session(session.id, now=time.time()) is None
    assert store.compaction(session.id) is None


def test_retention_compacts_behind_busy_session_and_delivers_record(storage):
    store, bus, session = storage
    t = store.start_turn(session.id)
    bus.publish("probe", {}, session_id=session.id)
    other = store.create_session(store.project(session.project_id), title="finished", agent="build")
    bus.publish("probe", {}, session_id=other.id)
    store.set_tree_archived(other.id, True)
    rt = SimpleNamespace(store=store, bus=bus, turns={session.id: t}, questions={})
    sub = bus.open()
    result = Retention(rt).run_once()
    assert [r["session_id"] for r in result["compacted"]] == [other.id]
    assert store.event_counts(session.id) == {"probe": 1}
    assert sub._queue.get_nowait()["type"] == "session.compacted"
    sub.close()


def test_new_database_incremental_vacuum_is_bounded(storage):
    store, bus, session = storage
    assert store.storage_inventory()["auto_vacuum"] == 2
    for _ in range(12):
        bus.publish("probe", {"pad": "x" * 200_000}, session_id=session.id)
    with store.db:
        store.db.execute("delete from event")
    before = store.storage_inventory()
    rt = SimpleNamespace(store=store, bus=bus, turns={}, questions={})
    result = Retention(rt, Settings(reclaim_threshold_bytes=0, reclaim_pages=8)).run_once()
    after = store.storage_inventory()
    assert result["reclaim"]["performed"]
    assert 0 < before["page_count"] - after["page_count"] <= 8


def test_offline_dry_run_lock_backup_and_swap(storage):
    store, bus, session = storage
    _announce(store, bus, session.id)
    bus.publish("probe", {"pad": "x" * 1_000_000}, session_id=session.id)
    generation = store.log_generation()
    before = store.path.read_bytes()
    assert compact_database(store.path)["dry_run"] and store.path.read_bytes() == before
    with pytest.raises(StoreError, match="in use"):
        compact_database(store.path, apply=True)
    store.close()
    result = compact_database(store.path, apply=True)
    assert result["integrity_check"] == "ok" and result["after_bytes"] < result["file_bytes"]
    with sqlite3.connect(result["backup"]) as db:
        assert db.execute("select count(*) from event").fetchone()[0] == 2
    reopened = Store(store.path)
    assert reopened.log_generation() == generation and len(reopened.messages(session.id)) == 1
    assert reopened.storage_inventory()["auto_vacuum"] == 2
    reopened.close()


def test_crash_recovery_uses_partial_checkpoint_without_delta_history(storage, monkeypatch, tmp_path):
    from nexus.server import make_runtime
    store, _bus, session = storage
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    turn = store.start_turn(session.id)
    request = store.start_request(turn["id"], session.id, 1)
    partial = LivePartial(request["id"])
    partial.apply({"type": "text_delta", "text": "diagnostic"})
    store.save_partial(request["id"], partial.json())
    rt = make_runtime(store)
    restored = rt.store.request(request["id"])
    assert restored["status"] == "interrupted" and restored["partial"]["parts"][0]["text"] == "diagnostic"
    messages = rt.store.messages(session.id)
    assert not any(m.message.role == "assistant" for m in messages)
    assert any("10 text characters" in str(m.message.parts) for m in messages)
