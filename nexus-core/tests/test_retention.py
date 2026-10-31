"""Bounded event and lifecycle retention, floor semantics and recovery pins (Step 11.6)."""
from __future__ import annotations

import json

import pytest

from nexus.bus import Bus
from nexus.eventlog import HistoryUnavailable, Stream, parse_cursor
from nexus.export import ExportView, stream
from nexus.llmhub.types import Message, Text, ToolCall, Usage
from nexus.store import Store


def _store(tmp_path):
    store = Store(tmp_path / "nexus.db")
    bus = Bus()
    bus.attach(store)
    return store, bus


def _session(store, tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir(exist_ok=True)
    project, _ = store.project_for_path(str(workspace))
    return store.create_session(project, title="t", agent="build")


def test_age_pruning_moves_the_floor_but_keeps_the_head_and_generation(tmp_path):
    store, bus = _store(tmp_path)
    ids = [bus.publish("probe", {"i": i})["id"] for i in range(5)]
    store.db.execute("update event set time = 1000")
    store.db.commit()
    generation = store.log_generation()
    result = store.prune_events(now=2000, age_seconds=500, byte_budget=None)
    assert result["removed"] == 5 and result["floor"] == ids[-1]
    assert store.log_floor() == ids[-1] and store.log_head() == ids[-1]
    assert store.replay(0, ids[-1], 10) == []
    assert parse_cursor(f"{generation}:{ids[-1]}", generation=generation,
                        head=ids[-1], floor=store.log_floor()) == ids[-1]
    with pytest.raises(HistoryUnavailable):
        parse_cursor(f"{generation}:{ids[-1] - 1}", generation=generation,
                     head=ids[-1], floor=store.log_floor())
    store.close()

    reopened = Store(tmp_path / "nexus.db")
    assert reopened.log_generation() == generation
    assert reopened.log_floor() == ids[-1] and reopened.log_head() == ids[-1]
    new = Bus()
    new.attach(reopened)
    fresh = new.publish("probe", {"i": "after"})["id"]
    assert fresh == ids[-1] + 1  # ids are never reused
    assert reopened.log_head() == fresh
    reopened.close()


def test_an_inflight_replay_pins_its_range(tmp_path):
    store, bus = _store(tmp_path)
    for i in range(4):
        bus.publish("probe", {"i": i})
    store.db.execute("update event set time = 1000")
    store.db.commit()
    stream = Stream.open(store, bus, after=0)
    try:
        result = store.prune_events(now=2000, age_seconds=1, byte_budget=None)
        assert result["removed"] == 0 and result["pinned"] is True
    finally:
        stream.close()
    result = store.prune_events(now=2000, age_seconds=1, byte_budget=None)
    assert result["removed"] == 4 and result["floor"] == store.log_head()


def test_an_unfinished_turn_protects_its_session_events(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    bus.publish("probe", {"i": 1}, session_id=session.id)
    turn = store.start_turn(session.id)
    store.db.execute("update event set time = 1000")
    store.db.commit()
    result = store.prune_events(now=2000, age_seconds=1, byte_budget=None,
                                protect_sessions=store.protected_sessions() | {session.id})
    assert result["removed"] == 0
    store.finish_turn(turn["id"], "interrupted")
    assert session.id in store.protected_sessions()  # recovery is still pending
    store.set_turn_recovery(turn["id"], "done")
    assert session.id not in store.protected_sessions()
    result = store.prune_events(now=2000, age_seconds=1, byte_budget=None,
                                protect_sessions=store.protected_sessions())
    assert result["removed"] == 1


def test_open_waits_and_jobs_are_protected(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    bus.publish("bash.started", {"id": "job_1"}, session_id=session.id)
    assert session.id in store.protected_sessions()
    bus.publish("bash.finished", {"id": "job_1"}, session_id=session.id)
    assert session.id not in store.protected_sessions()
    bus.publish("permission.asked", {"id": "per_1", "call_id": "c1"}, session_id=session.id)
    assert session.id in store.protected_sessions()
    bus.publish("permission.replied", {"id": "per_1", "outcome": "allowed"}, session_id=session.id)
    assert session.id not in store.protected_sessions()


def test_byte_budget_removes_only_a_prefix_and_reports_pressure(tmp_path):
    store, bus = _store(tmp_path)
    ids = [bus.publish("probe", {"pad": "x" * 1000})["id"] for _ in range(5)]
    result = store.prune_events(now=10_000, age_seconds=None, byte_budget=2500)
    assert result["removed"] >= 1 and result["floor"] == ids[result["removed"] - 1]
    assert result["floor"] < ids[-1]
    assert result["over_budget"] is False and result["retained_bytes"] <= 2500
    assert all(row["id"] > result["floor"] for row in store.replay(0, store.log_head(), 100))


def test_terminal_turns_and_resolved_mutations_are_pruned(tmp_path):
    store, _ = _store(tmp_path)
    session = _session(store, tmp_path)
    old = store.start_turn(session.id, started=1000)
    store.finish_turn(old["id"], "interrupted")
    store.set_turn_recovery(old["id"], "done")
    keep = store.start_turn(session.id)
    store.finish_turn(keep["id"], "completed")
    store.set_turn_recovery(keep["id"], "done")
    store.db.execute("update turn set started = 1000, finished = 1000 where id = ?", (old["id"],))
    store.db.commit()
    result = store.prune_turns(now=5000, age_seconds=1000, keep_per_session=1)
    assert result["turns_removed"] == 1
    assert [turn["id"] for turn in store.turns_for_session(session.id)] == [keep["id"]]

    store.record_mutation("old", "fp", "op", "/x", status=204, lifetime=-10)
    store.record_mutation("new", "fp", "op", "/x", status=204, lifetime=10_000)
    accepted = store.start_turn(session.id, mutation={
        "key": "accepted", "fingerprint": "fp", "operation": "prompt", "target": "/p",
        "state": "accepted", "status": 202, "body": {"status": "busy"}})
    assert store.prune_mutations() == 1
    assert store.mutation("new") is not None and store.mutation("accepted") is not None
    store.finish_turn(accepted["id"], "interrupted")
    store.resolve_mutations_for_turn(accepted["id"], lifetime=-1)
    assert store.prune_mutations() == 1
    assert store.mutation("accepted") is None


def test_tool_calls_and_request_rows_cascade_with_a_pruned_turn(tmp_path):
    store, _ = _store(tmp_path)
    session = _session(store, tmp_path)
    turn = store.start_turn(session.id)
    request = store.start_request(turn["id"], session.id, 1)
    call = ToolCall("c1", "bash", "{}")
    store.save_assistant_message(session.id, Message("assistant", (call,)), turn_id=turn["id"],
                                 request_id=request["id"])
    store.mark_tool_started(turn["id"], session.id, call)
    store.finish_turn(turn["id"], "completed")
    store.set_turn_recovery(turn["id"], "done")
    store.db.execute("update turn set started = 1000, finished = 1000 where id = ?", (turn["id"],))
    store.db.commit()
    # Even a terminal turn can contain a dangling tool call. Retain evidence
    # until its result enters context, so continuation cannot lose a known output.
    assert store.prune_turns(now=5000, age_seconds=1000, keep_per_session=0)["turns_removed"] == 0
    assert session.id in store.protected_sessions()
    from nexus.llmhub.types import ToolResult
    store.save_tool_message(session.id, Message("tool", (ToolResult("c1", "done"),)), turn_id=turn["id"])
    store.interrupt_request(request["id"], reason="finished test")
    assert store.prune_turns(now=5000, age_seconds=1000, keep_per_session=0)["turns_removed"] == 1
    assert store.turn(turn["id"]) is None
    assert store.requests_for_turn(turn["id"]) == []
    assert store.tools_for_turn(turn["id"]) == []


def test_large_pruning_uses_bounded_batches(tmp_path):
    store, bus = _store(tmp_path)
    for i in range(2500):
        bus.publish("probe", {"i": i})
    store.db.execute("update event set time = 1000")
    store.db.commit()
    result = store.prune_events(now=2000, age_seconds=1, byte_budget=None, batch_size=500)
    assert result["removed"] == 500 and result["batches"] == 1
    assert store.log_floor() < store.log_head()
    for _ in range(4):
        result = store.prune_events(now=2000, age_seconds=1, byte_budget=None, batch_size=500)
        assert result["removed"] == 500
    assert store.log_floor() == result["floor"] == store.log_head()
    assert store.replay(0, store.log_head(), 10) == []


def test_an_export_keeps_its_snapshot_while_pruning_runs(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    for i in range(3):
        bus.publish("probe", {"i": i}, session_id=session.id)
    store.db.execute("update event set time = 1000")
    store.db.commit()
    with ExportView(store.path, session.id, "session") as view:
        store.prune_events(now=2000, age_seconds=1, byte_budget=None)
        assert store.log_floor() > 0
        # The export holds one read snapshot; committed pruning does not half-empty it.
        assert [event["type"] for event in view.events(session.id)] == ["probe", "probe", "probe"]


def test_an_export_states_that_earlier_events_expired(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    bus.publish("probe", {"i": 1}, session_id=session.id)
    store.db.execute("update event set time = 1000")
    store.db.commit()
    store.prune_events(now=2000, age_seconds=1, byte_budget=None)
    with ExportView(store.path, session.id, "session") as view:
        meta = view.meta()
        assert meta["boundary"]["floor"] == store.log_floor() > 0
        assert meta["boundary"]["history_truncated"] is True
        assert meta["boundary"]["event_id"] == store.log_head()
        assert meta["boundary"]["counts"][session.id]["events"] == 0
        markdown = "".join(stream(view, "markdown"))
    assert "earlier diagnostic events expired" in markdown



def test_byte_limit_applies_even_when_no_events_are_old(tmp_path):
    store, bus = _store(tmp_path)
    for _ in range(5):
        bus.publish("probe", {"pad": "é" * 1000})
    result = store.prune_events(now=0, age_seconds=1000, byte_budget=7000)
    assert result["removed"] == 4
    assert result["retained_bytes"] <= 7000


async def test_replay_pin_is_released_at_the_live_boundary(tmp_path):
    import asyncio
    store, bus = _store(tmp_path)
    first = bus.publish("probe", {"i": 1})
    stream = Stream.open(store, bus, after=0)
    events = stream.events()
    assert (await anext(events))["id"] == first["id"]
    pending = asyncio.create_task(anext(events))
    for _ in range(10):
        if not store._pins:
            break
        await asyncio.sleep(0)
    assert store._pins == {}
    result = store.prune_events(now=10**12, age_seconds=1, byte_budget=None)
    assert result["removed"] == 1
    second = bus.publish("probe", {"i": 2})
    assert (await pending)["id"] == second["id"]
    await events.aclose()


# -- diagnostic wire payload lifecycle ---------------------------------------


def _wire_body(pad: int = 10_000) -> dict:
    return {"type": "wire_request", "method": "POST", "url": "https://api.example/v1",
            "headers": {"content-type": "application/json"},
            "body": {"instructions": "x" * pad, "messages": [{"role": "user", "content": "hello"}]}}


def _terminal_wire_turn(store, bus, session, *, status: str, finished: float, pad: int = 10_000):
    turn = store.start_turn(session.id)
    wire = bus.publish("wire_request", _wire_body(pad), session_id=session.id, turn_id=turn["id"])
    semantic = bus.publish("text_delta", {"type": "text_delta", "text": "kept"}, session_id=session.id,
                           turn_id=turn["id"])
    store.finish_turn(turn["id"], status)
    store.set_turn_recovery(turn["id"], "none")
    store.db.execute("update turn set finished = ? where id = ?", (finished, turn["id"]))
    store.db.commit()
    return turn, wire, semantic


def test_wire_payloads_are_compressed_deduped_and_lossless(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    body = _wire_body()
    first = bus.publish("wire_request", body, session_id=session.id)
    second = bus.publish("wire_request", body, session_id=session.id)

    event_rows = store.db.execute("select * from event where id in (?, ?) order by id",
                                  (first["id"], second["id"])).fetchall()
    assert all(row["payload_state"] == "full" and row["body_sha"] for row in event_rows)
    assert event_rows[0]["stored_bytes"] < event_rows[0]["payload_bytes"]
    body_row = store.db.execute("select refs, raw_bytes, stored_bytes from event_body").fetchone()
    assert body_row["refs"] == 2 and body_row["stored_bytes"] < body_row["raw_bytes"]
    assert store.db.execute("select count(*) from event_body").fetchone()[0] == 1
    # Both replay and the live event keep the exact payload, not the storage marker.
    assert [event["data"] for event in store.replay(0, store.log_head(), 10)] == [body, body]
    assert first["data"] == second["data"] == body
    inventory = store.storage_inventory()
    assert inventory["stored_event_bytes"] == store.db.execute(
        "select coalesce(sum(length(cast(data as blob))), 0) from event").fetchone()[0]
    assert inventory["body_bytes"] == store.db.execute(
        "select coalesce(sum(stored_bytes), 0) from event_body").fetchone()[0]
    assert inventory["stored_event_bytes"] > inventory["body_bytes"]  # two body references
    store.close()


def test_wire_payload_expiry_follows_terminal_outcome_and_keeps_envelopes(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    _, wire, semantic = _terminal_wire_turn(store, bus, session, status="completed", finished=now - 7200)
    failed_session = _session(store, tmp_path)
    _, failed_wire, _ = _terminal_wire_turn(store, bus, failed_session, status="failed", finished=now - 7200)

    assert store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)["scheduled"] == 2
    result = store.expire_wire_payloads(now=now, byte_budget=10**9)
    assert result["expired"] == 1 and result["expired_bytes"] > 0

    events = {event["id"]: event for event in store.events(session.id)}
    expired = events[wire["id"]]
    assert expired["type"] == "wire_request" and expired["payload_state"] == "expired"
    assert expired["expired"] is True and expired["data"]["$nexus_payload"] == "expired"
    assert "full payload is no longer recoverable" in expired["data"]["note"]
    # A normalized semantic event is recovery evidence and stays complete.
    assert events[semantic["id"]]["payload_state"] == "full"
    assert events[semantic["id"]]["data"]["text"] == "kept"
    # The failed turn uses the longer lifetime, so its payload is still live.
    failed = {event["id"]: event for event in store.events(failed_session.id)}
    assert failed[failed_wire["id"]]["payload_state"] == "full"
    store.close()


def test_active_and_recovery_pending_sessions_block_payload_expiry(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    turn = store.start_turn(session.id, started=now - 10_000)
    wire = bus.publish("wire_request", _wire_body(), session_id=session.id, turn_id=turn["id"])
    store.db.execute("update event set time = ?, expires = ? where id = ?", (now - 10_000, now - 1, wire["id"]))
    store.db.commit()
    assert store.protected_sessions() == {session.id}
    result = store.expire_wire_payloads(now=now, byte_budget=0,
                                        protect_sessions=store.protected_sessions())
    assert result["expired"] == 0 and result["blocked"] is True
    assert {event["id"]: event for event in store.events(session.id)}[wire["id"]]["payload_state"] == "full"
    store.finish_turn(turn["id"], "interrupted")
    assert session.id in store.protected_sessions()  # recovery is still pending
    assert store.expire_wire_payloads(now=now, byte_budget=0,
                                      protect_sessions=store.protected_sessions())["expired"] == 0
    store.set_turn_recovery(turn["id"], "done")
    store.db.execute("update turn set finished = ? where id = ?", (now - 2 * 24 * 3600, turn["id"]))
    store.db.commit()
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    assert store.expire_wire_payloads(now=now, byte_budget=10**9,
                                      protect_sessions=store.protected_sessions())["expired"] == 1
    store.close()


def test_wire_payload_budget_expires_oldest_eligible_first(tmp_path):
    store, bus = _store(tmp_path)
    now = 100_000.0
    sessions = [_session(store, tmp_path) for _ in range(3)]
    wires = []
    for index, session in enumerate(sessions):
        _, wire, _ = _terminal_wire_turn(store, bus, session, status="completed",
                                         finished=now - 100, pad=20_000)
        wires.append(wire)
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    # Budget pressure expires the oldest terminal payload before its deadline.
    budget = store.storage_inventory()["wire_bytes"] - 1
    result = store.expire_wire_payloads(now=now - 1, byte_budget=budget)
    assert result["over_budget"] is True
    statuses = [({event["id"]: event for event in store.events(session.id)})[wire["id"]]["payload_state"]
                for session, wire in zip(sessions, wires, strict=True)]
    assert statuses == ["expired", "full", "full"]  # oldest eligible first, stop at budget
    store.close()


def test_replay_and_export_show_an_explicit_expired_payload_marker(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    _, wire, _ = _terminal_wire_turn(store, bus, session, status="completed", finished=now - 7200)
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    store.expire_wire_payloads(now=now, byte_budget=10**9)
    store.close()

    store, bus = _store(tmp_path)  # reopen: the envelope and marker survive
    replay = {event["id"]: event for event in store.replay(0, store.log_head(), 10)}
    assert replay[wire["id"]]["payload_state"] == "expired"
    assert replay[wire["id"]]["data"]["state"] == "expired"
    with ExportView(store.path, session.id, "session") as view:
        assert view.boundary.expired_payloads == 1
        exported = list(view.events(session.id))
        assert exported[0]["id"] == wire["id"] and exported[0]["payload_state"] == "expired"
        assert exported[0]["data"]["$nexus_payload"] == "expired"
        meta = view.meta()
        assert meta["boundary"]["expired_payloads"] == 1
        markdown = "".join(stream(view, "markdown"))
    assert "Wire payload retention: 1 diagnostic event payload" in markdown
    store.close()


def test_usage_and_canonical_messages_are_independent_of_wire_expiry(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    turn = store.start_turn(session.id)
    request = store.start_request(turn["id"], session.id, 1)
    store.record_usage(request["id"], None, Usage(input_tokens=10, output_tokens=3))
    store.append_message(session.id, Message("user", (Text("keep me"),)))
    wire = bus.publish("wire_request", _wire_body(), session_id=session.id, turn_id=turn["id"])
    now = 100_000.0
    store.finish_turn(turn["id"], "completed")
    store.set_turn_recovery(turn["id"], "none")
    store.db.execute("update turn set finished = ? where id = ?", (now - 7200, turn["id"]))
    store.db.execute("update event set time = ? where id = ?", (now - 7200, wire["id"]))
    store.db.commit()
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    store.expire_wire_payloads(now=now, byte_budget=10**9)

    assert [m.message.parts[0].text for m in store.messages(session.id)] == ["keep me"]
    assert store.session_usage(session.id)["models"]["unknown"]["input"] == 10
    assert store.request(request["id"])["usage"] == {"input": 10, "cached": 0, "output": 3, "reasoning": 0}
    store.close()


def test_existing_v1_event_rows_gain_payload_storage_in_resumable_batches(tmp_path):
    import sqlite3

    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    body = _wire_body()
    first = bus.publish("wire_request", body, session_id=session.id)
    second = bus.publish("wire_request", body, session_id=session.id)
    store.append_message(session.id, Message("user", (Text("canonical"),)))
    ids = (first["id"], second["id"])
    store.close()

    db = sqlite3.connect(tmp_path / "nexus.db")
    import json

    for event_id in ids:
        db.execute("update event set data = ? where id = ?", (json.dumps(body), event_id))
    for (name,) in db.execute("select name from sqlite_master where type = 'trigger'").fetchall():
        db.execute(f"drop trigger {name}")
    for name in ("event_time", "event_wire_expiry", "event_turn", "event_wire_order", "event_lifecycle"):
        db.execute(f"drop index if exists {name}")
    for column in ("expires", "turn_id", "body_sha", "payload_state", "stored_bytes", "payload_bytes",
                   "turn_link_checked"):
        db.execute(f"alter table event drop column {column}")
    db.execute("drop table event_body")
    db.execute("drop table storage_metrics")
    db.execute("pragma user_version = 1")
    db.commit()
    db.close()

    store = Store(tmp_path / "nexus.db")
    assert store.backup is None
    columns = {row["name"] for row in store.db.execute("pragma table_info(event)")}
    assert {"payload_bytes", "payload_state", "body_sha", "turn_id", "expires"} <= columns
    assert store.storage_inventory()["unmeasured_events"] == 2
    assert store.storage_inventory()["indexes_ready"] is False
    assert not store.db.execute(
        "select 1 from sqlite_master where type = 'index' and name = 'event_lifecycle'").fetchone()
    assert store.ensure_payload_indexes(limit=1)["built"] == 1
    for _ in range(4):
        store.ensure_payload_indexes(limit=1)
    assert store.storage_inventory()["indexes_ready"] is True
    assert store.backfill_event_payloads(limit=1)["rows"] == 1
    assert store.storage_inventory()["unmeasured_events"] == 1
    assert store.backfill_event_payloads(limit=1)["remaining"] == 0
    assert [event["id"] for event in store.replay(0, store.log_head(), 10)] == list(ids)
    assert [event["data"] for event in store.replay(0, store.log_head(), 10)] == [body, body]
    assert [m.message.parts[0].text for m in store.messages(session.id)] == ["canonical"]
    assert store.db.execute("select count(*) from event_body").fetchone()[0] == 1
    assert store.db.execute("select refs from event_body").fetchone()[0] == 2
    assert store.storage_inventory()["stored_event_bytes"] == store.db.execute(
        "select coalesce(sum(length(cast(data as blob))), 0) from event").fetchone()[0]
    store.db.execute("delete from event where id = ?", (ids[0],))
    store.db.commit()
    assert store.db.execute("select refs from event_body").fetchone()[0] == 1
    assert [event["data"] for event in store.replay(0, store.log_head(), 10)] == [body]
    store.close()


def test_maintenance_catches_up_many_batches_without_one_giant_transaction(tmp_path):
    from nexus.retention import Retention, Settings

    store, bus = _store(tmp_path)

    class FakeRuntime:
        def __init__(self):
            self.turns: dict = {}
            self.questions: dict = {}

    runtime = FakeRuntime()
    runtime.store, runtime.bus = store, bus
    now = 100_000.0
    with store.db:
        rows = [("probe", i, now - 1000, "probe", json.dumps({"i": i}), 12, 12)
                for i in range(20_000)]
        store.db.executemany(
            "insert into event (session_id, seq, time, type, data, payload_bytes, stored_bytes)"
            " values (NULL, ?, ?, ?, ?, ?, ?)", [(r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows])
        store.db.execute("update log set head_high = (select max(id) from event)")
    settings = Settings(event_age_seconds=1, event_bytes=10**12, turn_age_seconds=10**9,
                        mutation_lifetime=10**9, batch_size=500, event_batches=4,
                        interval_seconds=10**9)
    retention = Retention(runtime, settings)
    removed = 0
    passes = 0
    while store.log_floor() < store.log_head() and passes < 50:
        summary = retention.run_once()
        removed += summary["events"]["removed"]
        passes += 1
    assert removed == 20_000
    assert store.log_floor() >= 20_000
    assert passes > 1  # bounded short batches, not one unbounded delete
    store.close()


def test_inflight_replay_pins_wire_payload_expiry(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    _, wire, _ = _terminal_wire_turn(store, bus, session, status="completed", finished=now - 7200)
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    stream = Stream.open(store, bus, after=0)
    try:
        assert store.expire_wire_payloads(now=now, byte_budget=10**9)["expired"] == 0
        assert {e["id"]: e for e in store.events(session.id)}[wire["id"]]["payload_state"] == "full"
    finally:
        stream.close()
    assert store.expire_wire_payloads(now=now, byte_budget=10**9)["expired"] == 1
    store.close()


def test_explicit_debug_retention_extends_wire_payloads(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    _, wire, _ = _terminal_wire_turn(store, bus, session, status="completed", finished=now - 7200)
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600,
                               debug_seconds=48 * 3600)
    # Completed at now-2h: the normal hour already passed, the debug extension has not.
    assert store.expire_wire_payloads(now=now, byte_budget=10**9)["expired"] == 0
    assert {e["id"]: e for e in store.events(session.id)}[wire["id"]]["payload_state"] == "full"
    store.db.execute("update event set expires = ? where id = ?", (now - 1, wire["id"]))
    store.db.commit()
    assert store.expire_wire_payloads(now=now, byte_budget=10**9)["expired"] == 1
    store.close()


def test_a_running_turn_does_not_block_an_older_completed_turns_payload(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    _, old_wire, _ = _terminal_wire_turn(store, bus, session, status="completed", finished=now - 7200)
    active = store.start_turn(session.id)
    active_wire = bus.publish("wire_request", _wire_body(500), session_id=session.id, turn_id=active["id"])
    assert store.protected_sessions() == {session.id}
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    result = store.expire_wire_payloads(now=now, byte_budget=10**9,
                                        protect_sessions=store.protected_sessions())
    assert result["expired"] == 1  # the completed turn's payload, not the running one
    events = {event["id"]: event for event in store.events(session.id)}
    assert events[old_wire["id"]]["payload_state"] == "expired"
    assert events[active_wire["id"]]["payload_state"] == "full"
    store.close()


def test_inventory_and_backfill_do_not_count_scan_event_payloads(tmp_path):
    store, _bus = _store(tmp_path)
    now = 100_000.0
    with store.db:
        store.db.executemany(
            "insert into event (session_id, seq, time, type, data, payload_bytes, stored_bytes)"
            " values (NULL, ?, ?, 'probe', '{}', -1, 0)", [(i, now, ) for i in range(500)])
        store.db.execute("update storage_metrics set pending_events = 500, event_count = 500 where id = 1")
    calls: list[str] = []
    store.db.set_trace_callback(calls.append)
    inventory = store.storage_inventory()
    store.db.set_trace_callback(None)
    assert inventory["unmeasured_events"] == 500
    assert not any(" from event" in sql.lower() for sql in calls)
    store.close()


def test_unlinked_turn_backfill_retires_rows_and_cannot_starve(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    for i in range(5):
        bus.publish("wire_request", _wire_body(100 + i), session_id=session.id)
    assert store.backfill_wire_turn_ids(limit=2) == 2
    assert store.backfill_wire_turn_ids(limit=2) == 2
    assert store.backfill_wire_turn_ids(limit=2) == 1
    assert store.backfill_wire_turn_ids(limit=2) == 0
    rows = store.db.execute(
        "select turn_link_checked, turn_id from event where type = 'wire_request'").fetchall()
    assert len(rows) == 5 and all(row["turn_link_checked"] == 1 for row in rows)
    assert all(row["turn_id"] is None for row in rows)
    store.close()


def test_turn_backfill_uses_a_bounded_request_range(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    turn = store.start_turn(session.id)
    store.start_request(turn["id"], session.id, 1)
    inside = bus.publish("wire_request", _wire_body(100), session_id=session.id)  # no turn id: legacy shape
    # An event from before the request watermark must not be attributed forward.
    before = store.db.execute(
        "insert into event (session_id, seq, time, type, data, payload_bytes, stored_bytes, turn_link_checked)"
        " values (?, 0, 0, 'wire_request', '{}', 2, 2, 0) returning id",
        (session.id,)).fetchone()["id"]
    store.db.commit()
    assert store.backfill_wire_turn_ids(limit=10) == 2
    linked = {row["id"]: row for row in store.db.execute(
        "select id, turn_id, turn_link_checked from event where id in (?, ?)", (inside["id"], before,))}
    assert linked[inside["id"]]["turn_id"] == turn["id"]
    assert linked[before]["turn_id"] is None and linked[before]["turn_link_checked"] == 1
    store.close()


def test_expiry_exclusions_do_not_starve_later_eligible_payloads(tmp_path):
    store, bus = _store(tmp_path)
    now = 100_000.0
    protected = _session(store, tmp_path)
    running = store.start_turn(protected.id)
    for _ in range(3):
        event = bus.publish("wire_request", _wire_body(200), session_id=protected.id, turn_id=running["id"])
        store.db.execute("update event set expires = ? where id = ?", (now - 1, event["id"]))
    eligible_session = _session(store, tmp_path)
    _, eligible, _ = _terminal_wire_turn(store, bus, eligible_session, status="completed",
                                         finished=now - 7200, pad=200)
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    result = store.expire_wire_payloads(now=now, byte_budget=10**9, batch_size=2,
                                        protect_sessions={protected.id})
    assert result["expired"] == 1
    assert {e["id"]: e for e in store.events(eligible_session.id)}[eligible["id"]]["payload_state"] == "expired"
    store.close()


def test_replay_pin_exclusions_do_not_starve_later_eligible_payloads(tmp_path):
    store, bus = _store(tmp_path)
    now = 100_000.0
    session = _session(store, tmp_path)
    early = []
    for _ in range(3):
        early.append(bus.publish("wire_request", _wire_body(200), session_id=session.id))
    _, later, _ = _terminal_wire_turn(store, bus, session, status="completed",
                                      finished=now - 7200, pad=200)
    store.db.execute("update event set expires = ? where id in (?, ?, ?)",
                     (now - 1, *(event["id"] for event in early)))
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    store.pin_events(early[0]["id"], early[-1]["id"])
    result = store.expire_wire_payloads(now=now, byte_budget=10**9, batch_size=2)
    assert result["expired"] == 1
    assert {e["id"]: e for e in store.events(session.id)}[later["id"]]["payload_state"] == "expired"
    store.close()


def test_export_payload_expiry_notice_is_scoped_to_its_sessions(tmp_path):
    store, bus = _store(tmp_path)
    clean = _session(store, tmp_path)
    dirty = _session(store, tmp_path)
    now = 100_000.0
    _, _wire, _ = _terminal_wire_turn(store, bus, dirty, status="completed", finished=now - 7200)
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    store.expire_wire_payloads(now=now, byte_budget=10**9)
    with ExportView(store.path, clean.id, "session") as view:
        assert view.boundary.expired_payloads == 0
        assert view.boundary.wire_payload_bytes == 0
        markdown = "".join(stream(view, "markdown"))
    assert "Wire payload retention" not in markdown
    with ExportView(store.path, dirty.id, "session") as view:
        assert view.boundary.expired_payloads == 1
    store.close()


def test_byte_pruning_is_bounded_and_catches_up(tmp_path):
    store, _bus = _store(tmp_path)
    now = 100_000.0
    with store.db:
        for i in range(10):
            store.db.execute(
                "insert into event (session_id, seq, time, type, data, payload_bytes, stored_bytes)"
                " values (NULL, ?, ?, 'probe', '{}', 1000, 1000)", (i, now))
        store.db.execute("update log set head_high = (select max(id) from event)")
    result = store.prune_events(now=now, age_seconds=None, byte_budget=3500, batch_size=2)
    assert result["removed"] <= 2 and result["over_budget"] is True
    passes = 1
    while result["over_budget"] and passes < 20:
        result = store.prune_events(now=now, age_seconds=None, byte_budget=3500, batch_size=2)
        passes += 1
    assert store.storage_inventory()["payload_bytes"] <= 3500
    assert passes > 1
    store.close()


def test_maintenance_interval_and_pressure_hook_are_bounded(tmp_path):
    from nexus.retention import Settings

    assert Settings().interval_seconds <= 60
    store, bus = _store(tmp_path)
    calls: list[int] = []
    store.on_pressure = lambda: calls.append(1)
    store.pressure_bytes = 1
    bus.publish("wire_request", _wire_body(100), session_id=_session(store, tmp_path).id)
    assert calls == [1]
    bus.publish("wire_request", _wire_body(101), session_id=_session(store, tmp_path).id)
    assert calls == [1]  # one wake until retention observes the budget again
    store.close()


async def test_impossible_budget_cannot_busy_spin_or_flood_log_retention(tmp_path):
    import asyncio

    from nexus.retention import Retention, Settings

    store, bus = _store(tmp_path)

    class FakeRuntime:
        def __init__(self):
            self.turns: dict = {}
            self.questions: dict = {}

    runtime = FakeRuntime()
    runtime.store, runtime.bus = store, bus
    with store.db:
        store.db.executemany(
            "insert into event (session_id, seq, time, type, data, payload_bytes, stored_bytes)"
            " values (NULL, ?, 1, 'probe', '{}', 1000, 1000)", [(i,) for i in range(20)])
        store.db.execute("update log set head_high = (select max(id) from event)")
    retention = Retention(runtime, Settings(
        event_age_seconds=10**9, event_bytes=0, turn_age_seconds=10**9,
        mutation_lifetime=10**9, batch_size=2, event_batches=4, interval_seconds=0.2,
        catchup_delay_seconds=0.01, announce_seconds=60))
    retention.start()
    await asyncio.sleep(0.35)
    await retention.stop()
    announcements = store.db.execute(
        "select count(*) from event where type = 'log.retention'").fetchone()[0]
    assert announcements <= 2
    assert store.storage_inventory()["events"] < 20
    store.close()


def test_expiry_stores_a_compact_marker_and_still_explains_it_on_read(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    now = 100_000.0
    _, wire, _ = _terminal_wire_turn(store, bus, session, status="completed",
                                     finished=now - 7200, pad=2000)
    before = store.storage_inventory()
    assert before["bodies"] == 1 and before["stored_event_bytes"] > 0
    store.schedule_wire_expiry(success_seconds=3600, failure_seconds=24 * 3600)
    store.expire_wire_payloads(now=now, byte_budget=10**9)
    after = store.storage_inventory()
    row = store.db.execute("select data, stored_bytes from event where id = ?", (wire["id"],)).fetchone()
    assert "note" not in row["data"] and "resync" not in row["data"]
    assert after["bodies"] == 0  # the deduplicated body was freed
    assert row["stored_bytes"] == len(row["data"].encode("utf-8"))
    before_total = before["stored_event_bytes"] + before["body_bytes"]
    after_total = after["stored_event_bytes"] + after["body_bytes"]
    assert after_total < before_total
    decoded = {e["id"]: e for e in store.events(session.id)}[wire["id"]]["data"]
    assert decoded["$nexus_payload"] == "expired"
    assert "full payload is no longer recoverable" in decoded["note"]
    assert decoded["resync"] == "/api/snapshot"
    store.close()


def test_lifecycle_queries_and_backfill_selection_use_small_indexes(tmp_path):
    store, _bus = _store(tmp_path)
    assert store.storage_inventory()["indexes_ready"] is True
    assert store.protected_sessions() == set()
    calls: list[str] = []
    store.db.set_trace_callback(calls.append)
    store.protected_sessions()
    store.db.set_trace_callback(None)
    assert any("indexed by event_lifecycle" in sql.lower() for sql in calls)
    plan = " ".join(row["detail"] for row in store.db.execute(
        "explain query plan select id from event where id > 100 and payload_bytes < 0"
        " order by id limit 10"))
    assert "INTEGER PRIMARY KEY" in plan.upper()
    plan = " ".join(row["detail"] for row in store.db.execute(
        "explain query plan select id from event indexed by event_lifecycle"
        " where type in ('bash.started', 'bash.finished', 'permission.asked', 'permission.replied',"
        " 'question.asked', 'question.replied')"))
    assert "event_lifecycle" in plan
    store.close()


def test_global_unlinked_wire_payload_survives_a_nonempty_protected_set(tmp_path):
    store, bus = _store(tmp_path)
    protected = _session(store, tmp_path)
    store.start_turn(protected.id)
    now = 100_000.0
    event = bus.publish("wire_request", _wire_body(200))
    assert event["session_id"] is None
    store.db.execute("update event set expires = ? where id = ?", (now - 1, event["id"]))
    store.db.commit()
    result = store.expire_wire_payloads(
        now=now, byte_budget=10**9, protect_sessions={protected.id}, batch_size=1)
    assert result["expired"] == 1
    row = store.db.execute("select payload_state from event where id = ?", (event["id"],)).fetchone()
    assert row["payload_state"] == "expired"
    store.close()


def test_inventory_event_bytes_match_serialized_rows_and_bodies_are_deduplicated(tmp_path):
    store, bus = _store(tmp_path)
    session = _session(store, tmp_path)
    body = _wire_body(3000)
    bus.publish("wire_request", body, session_id=session.id)
    bus.publish("wire_request", body, session_id=session.id)
    inventory = store.storage_inventory()
    assert inventory["events"] == 2 and inventory["bodies"] == 1
    assert inventory["stored_event_bytes"] == store.db.execute(
        "select coalesce(sum(length(cast(data as blob))), 0) from event").fetchone()[0]
    assert inventory["body_bytes"] == store.db.execute(
        "select coalesce(sum(stored_bytes), 0) from event_body").fetchone()[0]
    assert inventory["stored_event_bytes"] == sum(
        row["stored_bytes"] for row in store.db.execute("select stored_bytes from event"))
    store.close()
