"""Ownership migration, atomic promotion and lease-safe descendant recovery."""
from __future__ import annotations

import sqlite3

from nexus.events import Event
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.runtime import _child_ownership, _child_session_id
from nexus.session.db import SCHEMA_VERSION, SqliteSessionStore, StateDatabase, _SCHEMA_STEPS
from nexus.session.manager import SessionManager


def message(text="go"):
    return Message(role="user", content=[Text(text=text)])


def test_v5_migration_backfills_only_matching_spawn_evidence(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for step in _SCHEMA_STEPS[:5]:
        for sql in step:
            conn.execute(sql)
    conn.execute("PRAGMA user_version=5")
    conn.commit()
    conn.close()
    # Seed through the old schema without invoking the v6 constructor.
    import msgspec
    from nexus.session.records import EventRecord

    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO projects(id,root,created_at,last_opened) VALUES('p','/p',0,0)")
    logical = "root/sub/1"
    child_id = _child_session_id(logical)
    for namespace, session in [("main", "root"), ("agents", child_id), ("agents", "unknown")]:
        conn.execute(
            "INSERT INTO sessions(project_id,namespace,id,created_at,last_seq,last_activity) VALUES('p',?,?,0,0,0)",
            (namespace, session),
        )
    event = Event(type="agent.spawned", turn="turn", data={"agent": {
        "id": logical, "session": logical, "root_turn_id": "turn", "parent_call_id": "call",
    }})
    conn.execute(
        "INSERT INTO records VALUES('p','main','root',1,'event',0,?)",
        (msgspec.json.encode(EventRecord(seq=1, ts=0, event=event)),),
    )
    conn.commit()
    conn.close()
    db = StateDatabase(path)
    store = SqliteSessionStore(db, "p", "agents")
    row = store.session_row(child_id)
    assert db.schema_version() == SCHEMA_VERSION == 7
    assert (row["owner_namespace"], row["owner_id"], row["owner_turn_id"], row["owner_call_id"]) == (
        "main", "root", "turn", "call",
    )
    assert row["logical_session_id"] == logical
    assert store.session_row("unknown")["owner_id"] == ""
    assert len(store.owned_rows("main", "root")) == 1
    assert SqliteSessionStore(db, "other", "main").owned_rows("main", "root") == []
    assert StateDatabase(path).schema_version() == 7


def managers(tmp_path):
    db = StateDatabase(tmp_path / "state.db")
    kwargs = dict(db=db, project=tmp_path, lock_dir=tmp_path / "locks")
    return SessionManager(tmp_path, **kwargs), SessionManager(tmp_path, namespace="agents", **kwargs)


def owned(manager, session_id, owner_namespace, owner_id):
    return manager.open(session_id, recover=False, ownership={
        "owner_namespace": owner_namespace, "owner_id": owner_id,
        "owner_turn_id": "root-turn", "owner_call_id": "call", "agent_id": session_id,
        "logical_session_id": "display-only",
    })


def abandoned(session):
    session.append_message(message())
    session.append_message(Message(role="assistant", content=[ToolUse(id="tool", name="read", input={})]))
    session.append_event(Event(type="turn.started", turn="abandoned", data={}))


def test_ownership_published_atomically_with_first_input_and_not_fork_lineage(tmp_path):
    root, agents = managers(tmp_path)
    child = owned(agents, "child", "main", "root")
    assert root.store.owned_rows("main", "root") == []
    assert not agents.store.db._connection().execute(
        "SELECT 1 FROM sessions WHERE id='child'"
    ).fetchone()
    child.append_message(message())
    row = agents.store.session_row("child")
    assert row["owner_id"] == "root" and row["owner_call_id"] == "call"
    assert not row["parent_id"]
    fork = agents.fork("child", new_id="fork")
    assert agents.store.session_row(fork.id)["owner_id"] == ""
    assert agents.store.session_row(fork.id)["parent_id"] == "child"


def test_owner_reopen_repairs_nested_logs_idempotently_without_creating_unknown_logs(tmp_path):
    root, agents = managers(tmp_path)
    parent = root.open("root")
    parent.append_message(message())
    child = owned(agents, "child", "main", "root")
    grandchild = owned(agents, "grandchild", "agents", "child")
    unknown = agents.open("unknown", recover=False)
    for session in (child, grandchild, unknown):
        abandoned(session)
    parent.recover_dangling_tool_uses()
    for session_id in ("child", "grandchild"):
        records = agents.store.read(session_id)
        assert [e.type for e in records.events()].count("turn.failed") == 1
        assert any(isinstance(block, ToolResult) for m in records.messages() for block in m.content)
    before = agents.store.read("grandchild").records
    parent.recover_dangling_tool_uses()
    assert agents.store.read("grandchild").records == before
    assert not any(e.type == "turn.failed" for e in agents.store.read("unknown").events())


def test_live_child_lease_skips_entire_subtree_until_released(tmp_path):
    root, agents = managers(tmp_path)
    parent = root.open("root")
    parent.append_message(message())
    child = owned(agents, "child", "main", "root")
    grandchild = owned(agents, "grandchild", "agents", "child")
    abandoned(child)
    abandoned(grandchild)
    lease = child.begin_turn()
    try:
        parent.recover_dangling_tool_uses()
        assert not any(e.type == "turn.failed" for e in agents.store.read("child").events())
        assert not any(e.type == "turn.failed" for e in agents.store.read("grandchild").events())
    finally:
        lease.release()
    parent.recover_dangling_tool_uses()
    assert any(e.type == "turn.failed" for e in agents.store.read("grandchild").events())


def test_runtime_ownership_uses_physical_parent_not_logical_prefix():
    from types import SimpleNamespace

    spec = SimpleNamespace(parent_agent_id="agent", parent_session="physical-parent", session_id="logical/sub/1",
                           agent_id="agent-child", root_turn_id="turn", parent_call_id="call")
    edge = _child_ownership(spec)
    assert edge["owner_namespace"] == "agents" and edge["owner_id"] == "physical-parent"
    assert edge["logical_session_id"] == "logical/sub/1"


def test_ownership_cycle_does_not_recurse_forever(tmp_path):
    _root, agents = managers(tmp_path)
    a = owned(agents, "a", "agents", "b")
    b = owned(agents, "b", "agents", "a")
    abandoned(a)
    abandoned(b)
    a.recover_dangling_tool_uses()
    assert sum(e.type == "turn.failed" for e in agents.store.read("a").events()) == 1
    assert sum(e.type == "turn.failed" for e in agents.store.read("b").events()) == 1


def test_live_owner_lease_prevents_descendant_repair(tmp_path):
    root, agents = managers(tmp_path)
    parent = root.open("root")
    parent.append_message(message())
    child = owned(agents, "child", "main", "root")
    abandoned(child)
    lease = parent.begin_turn()
    try:
        parent.recover_dangling_tool_uses()
        assert not any(e.type == "turn.failed" for e in agents.store.read("child").events())
    finally:
        lease.release()
    parent.recover_dangling_tool_uses()
    assert any(e.type == "turn.failed" for e in agents.store.read("child").events())


def _reserve_in_process(path, ready, start, output):
    """Each worker has its own DB object/SQLite connection, not a thread mock."""
    db = StateDatabase(path)
    store = SqliteSessionStore(db, "p", "agents")
    ready.put(True)
    start.wait(10)
    output.put([store.reserve_child_id("root") for _ in range(12)])
    db.close()


def test_allocation_is_durable_cross_process_and_skips_legacy_trash(tmp_path):
    import multiprocessing

    path = tmp_path / "allocation.db"
    db = StateDatabase(path)
    store = SqliteSessionStore(db, "p", "agents")
    store.create_from_records(_child_session_id("root/sub/1"), [])
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET trash_id='trash' WHERE namespace='agents'")
    ctx = multiprocessing.get_context("spawn")
    ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [ctx.Process(target=_reserve_in_process, args=(str(path), ready, start, output)) for _ in range(3)]
    for worker in workers:
        worker.start()
    try:
        for _ in workers:
            assert ready.get(timeout=15)
        start.set()
        allocated = [item for _ in workers for item in output.get(timeout=20)]
        assert len(set(logical for _, logical in allocated)) == 36
        assert sorted(index for index, _ in allocated) == list(range(2, 38))
        assert StateDatabase(path).schema_version() == 7
        reopened = SqliteSessionStore(StateDatabase(path), "p", "agents")
        assert reopened.reserve_child_id("root")[0] == 38
        assert store.reserve_child_id("other")[0] == 1
        other_project = SqliteSessionStore(db, "q", "agents")
        assert other_project.reserve_child_id("root")[0] == 1
        # Reserved but unpromoted children are deliberately absent from lists.
        assert store.list_rows() == []
    finally:
        for worker in workers:
            worker.join(10)
            if worker.is_alive():
                worker.terminate()
            assert worker.exitcode == 0
        db.close()


def test_resolver_never_rehashes_physical_child_and_detects_ambiguous_logical(tmp_path):
    import pytest
    from nexus.errors import SessionError

    db = StateDatabase(tmp_path / "resolve.db")
    store = SqliteSessionStore(db, "p", "agents")
    _, logical = store.reserve_child_id("root")
    physical = _child_session_id(logical)
    store.create_from_records(physical, [], meta={"logical_session_id": logical})
    assert store.resolve_child_id(logical) == physical
    assert store.resolve_child_id(physical) == physical
    store.create_from_records("duplicate", [], meta={"logical_session_id": logical})
    with pytest.raises(SessionError, match="Ambiguous"):
        store.resolve_child_id(logical)
    db.close()


def test_v5_nested_completed_and_request_evidence_conservative(tmp_path):
    import msgspec
    from nexus.session.records import EventRecord

    path = tmp_path / "nested-old.db"
    conn = sqlite3.connect(path)
    for step in _SCHEMA_STEPS[:5]:
        for sql in step:
            conn.execute(sql)
    conn.execute("PRAGMA user_version=5")
    conn.execute("INSERT INTO projects VALUES('p','/p',0,0)")
    logicals = ["root/sub/1", "nested/sub/1", "request/sub/1", "conflict/sub/1"]
    physicals = [_child_session_id(value) for value in logicals]
    for namespace, identity in [("main", "root"), ("main", "other"), *[("agents", value) for value in physicals]]:
        conn.execute(
            "INSERT INTO sessions(project_id,namespace,id,created_at,last_seq,last_activity) VALUES('p',?,?,0,0,0)",
            (namespace, identity),
        )
    def add(namespace, identity, seq, event_type, data):
        record = EventRecord(seq=seq, ts=0, event=Event(type=event_type, data=data))
        conn.execute("INSERT INTO records VALUES('p',?,?,?,'event',0,?)",
                     (namespace, identity, seq, msgspec.json.encode(record)))
    direct = {"id": logicals[0], "session": logicals[0], "parent_session": "root"}
    add("main", "root", 1, "agent.completed", {"agent": direct})
    nested = {"id": logicals[1], "session": logicals[1], "parent_session": physicals[0], "parent_agent_id": logicals[0]}
    add("agents", physicals[0], 7, "agent.spawned", {"agent": nested})
    add("main", "root", 8, "agent.spawned", {"agent": nested})  # relayed seq not owner seq
    request = {"id": logicals[2], "session": logicals[2], "parent_session": physicals[1], "parent_agent_id": logicals[1]}
    add("agents", physicals[2], 1, "context.assembled", {"request": {"agent": request}})
    conflicting = {"id": logicals[3], "session": logicals[3], "parent_session": "root"}
    add("main", "root", 9, "agent.spawned", {"agent": conflicting})
    add("main", "other", 1, "agent.completed", {"agent": {**conflicting, "parent_session": "other"}})
    conn.commit(); conn.close()
    db = StateDatabase(path)
    store = SqliteSessionStore(db, "p", "agents")
    assert store.session_row(physicals[0])["owner_id"] == "root"
    nested_row = store.session_row(physicals[1])
    assert nested_row["owner_id"] == physicals[0]
    assert nested_row["owner_spawn_seq"] == 7 and nested_row["root_id"] == "root"
    assert store.session_row(physicals[2])["owner_id"] == physicals[1]
    assert store.session_row(physicals[2])["root_id"] == "root"
    assert store.session_row(physicals[3])["kind"] == ""
    assert store.unattached_count() == 1
    db.close()
