"""Bounded event and lifecycle retention, floor semantics and recovery pins (Step 11.6)."""
from __future__ import annotations

import pytest

from nexus.bus import Bus
from nexus.eventlog import HistoryUnavailable, Stream, parse_cursor
from nexus.export import ExportView, stream
from nexus.llmhub.types import Message, ToolCall
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
