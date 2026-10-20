"""P2 ownership tree boundary, redaction, boundedness and non-recovery."""
from __future__ import annotations

import json

import msgspec
import pytest

from nexus.errors import SessionError
from nexus.events import Event
from nexus.host import HostFacade, protocol as p
from nexus.session.lock import SessionLock
from test_session_ownership import abandoned, managers, message, owned


def test_tree_is_read_only_explicit_and_fork_independent(tmp_path):
    root, agents = managers(tmp_path)
    parent = root.open("root")
    parent.append_message(message())
    child = owned(agents, "child", "main", "root")
    child.append_message(message())
    grandchild = owned(agents, "grandchild", "agents", "child")
    abandoned(grandchild)
    fork = root.fork("root", new_id="fork")
    unknown = agents.open("legacy")
    unknown.append_message(message())
    child.append_event(Event(type="session.title", data={"title": "unused"}))
    with root.store.db.transaction() as conn:
        conn.execute("UPDATE sessions SET title=? WHERE namespace='agents' AND id='child'",
                     ("Authorization: Bearer sk-secret1234567\x1b[31m",))
    before = root.store.db._connection().total_changes
    handles = (set(root._handles), set(agents._handles))
    lock_paths = set(tmp_path.rglob("*.lock"))
    tree = root.tree("grandchild", namespace="agents", max_depth=8)
    node = tree["nodes"][0]
    assert node["id"] == "root"
    assert node["children"][0]["id"] == "child"
    grand = node["children"][0]["children"][0]
    assert grand["id"] == "grandchild" and grand["status"] == "interrupted"
    assert grand["owner_namespace"] == "agents" and grand["owner_id"] == "child"
    assert grand["owner_turn_id"] == "root-turn"
    assert grand["owner_spawn_seq"] is None  # unknown is not invented
    assert grand["actions"] == {"transcript": True, "request": True, "context": True,
                                 "send": False, "resume": False, "fork": False}
    assert tree["unattached"][0]["id"] == "legacy"
    assert tree["unattached"][0]["kind"] == "unknown"
    assert "sk-secret" not in json.dumps(tree) and "\\u001b" not in json.dumps(tree)
    assert root.tree(fork.id)["nodes"][0]["children"] == []
    assert root.store.db._connection().total_changes == before
    assert handles == (set(root._handles), set(agents._handles))
    assert set(tmp_path.rglob("*.lock")) == lock_paths
    assert not any(e.type == "turn.failed" for e in grandchild.events)
    assert root.tree("root", max_depth=0)["truncated"]
    assert {item.namespace for item in root.list(include_children=True)} == {"main", "agents"}
    assert next(item for item in root.list() if item.id == "root").child_count == 1
    assert root.store.unattached_count() == 1
    root.close_all(); agents.close_all()


def test_tree_liveness_uses_os_lock_not_stale_event(tmp_path):
    root, agents = managers(tmp_path)
    root.open("root").append_message(message())
    child = owned(agents, "child", "main", "root")
    abandoned(child)
    lock = SessionLock(agents.store.lock_path("child"))
    lock.acquire(shared=False)
    try:
        tree = root.tree("root")
        assert tree["nodes"][0]["children"][0]["status"] == "running"
        assert tree["nodes"][0]["running_children"] == 1
        assert root.list()[0].running_children == 1
    finally:
        lock.release()
    assert root.tree("root")["nodes"][0]["children"][0]["status"] == "interrupted"
    assert root.list()[0].running_children == 0
    root.close_all(); agents.close_all()


def test_tree_cycles_depth_and_project_scope(tmp_path):
    root, agents = managers(tmp_path)
    root.open("root").append_message(message())
    owned(agents, "a", "agents", "b").append_message(message())
    owned(agents, "b", "agents", "a").append_message(message())
    assert len(agents.tree("a", namespace="agents")["nodes"]) == 1
    assert root.tree("root")["nodes"][0]["children"] == []
    with pytest.raises(SessionError):
        root.tree("missing")
    with pytest.raises(ValueError):
        root.tree("root", namespace="other")
    with pytest.raises(ValueError):
        root.tree("root", max_depth=65)
    root.close_all(); agents.close_all()


async def test_host_tree_wire_and_configured_depth(tmp_path):
    from test_phase6_integration import make_runtime
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response

    runtime = make_runtime(tmp_path, ScriptedProvider(
        tool_response(("call", "Task", {"prompt": "child", "subagent_type": "general"})),
        text_response("report"), text_response("done"),
    ))
    try:
        async for _ in runtime.session("root").send("go"):
            pass
        host = HostFacade(runtime)
        command = p.SessionTree(session="root")
        assert msgspec.json.decode(msgspec.json.encode(command), type=p.Command) == command
        result = await host.handle(command)
        assert isinstance(result, p.SessionTreeResult)
        assert msgspec.json.decode(msgspec.json.encode(result), type=p.Result) == result
        child = result.tree["nodes"][0]["children"][0]
        assert child["root_id"] == "root"
        assert child["owner_spawn_seq"] > 0
        assert child["owner_agent_id"] == child["agent_id"]
        assert child["resolved_model"] and child["tier"]
        assert result.tree["max_depth"] == 8  # fixed: legacy nested sessions still render
        listing = await host.handle(p.SessionList(include_children=True))
        assert {item.namespace for item in listing.sessions} == {"main", "agents"}
    finally:
        await runtime.aclose()


async def test_child_cannot_spawn_so_tree_stays_one_level_deep(tmp_path):
    # Legacy nested ownership is covered with recorded stores above; a live
    # child that forges a Task call is refused and no grandchild is created.
    from test_phase6_integration import make_runtime
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response

    runtime = make_runtime(tmp_path, ScriptedProvider(
        tool_response(("outer", "Task", {"prompt": "delegate", "subagent_type": "general"})),
        tool_response(("inner", "Task", {"prompt": "nested", "subagent_type": "general"})),
        text_response("child report"), text_response("root done"),
    ))
    try:
        async for _ in runtime.session("root").send("go"):
            pass
        tree = (await HostFacade(runtime).handle(p.SessionTree(session="root"))).tree
        parent = tree["nodes"][0]["children"][0]
        assert parent["children"] == []
        assert parent["root_id"] == "root"
    finally:
        await runtime.aclose()


def test_tree_total_node_budget_and_no_cross_project_adoption(tmp_path):
    from nexus.session.db import SqliteSessionStore
    from nexus.session.manager import SessionManager

    root, agents = managers(tmp_path)
    root.open("root").append_message(message())
    # Bulk seed without histories: each store insertion is still transactional.
    for index in range(501):
        agents.store.create_from_records(f"child-{index}", [], meta={
            "kind": "child", "owner_namespace": "main", "owner_id": "root", "root_id": "root",
        })
    other = SessionManager(store=SqliteSessionStore(
        root.store.db, "other-project", "agents", lock_dir=root.store.lock_dir))
    other.store.create_from_records("foreign", [], meta={
        "kind": "child", "owner_namespace": "main", "owner_id": "root", "root_id": "root",
    })
    tree = root.tree("root")
    assert tree["truncated"] and len(tree["nodes"][0]["children"]) == 499
    assert "foreign" not in json.dumps(tree)
    root.close_all(); agents.close_all(); other.close_all()


def test_root_with_two_native_and_one_external_child_is_four_nodes(tmp_path):
    root, agents = managers(tmp_path)
    root.open("root").append_message(message())
    for name in ("native-a", "native-b"):
        owned(agents, name, "main", "root").append_message(message())
    agents.store.create_from_records("external-c", [], meta={
        "kind": "external", "owner_namespace": "main", "owner_id": "root", "root_id": "root",
    })
    tree = root.tree("root")
    top = tree["nodes"][0]
    children = top["children"]
    assert not tree["truncated"]
    assert 1 + len(children) == 4
    assert {child["id"] for child in children} == {"native-a", "native-b", "external-c"}
    assert next(c for c in children if c["id"] == "external-c")["kind"] == "external"
    root.close_all(); agents.close_all()
