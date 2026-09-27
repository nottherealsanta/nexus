"""Tests for the built-in ``TodoWrite`` tool and its session-scoped store."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.errors import ToolError
from nexus.tools.builtin import todo
from nexus.tools.builtin.todo import (
    PRIORITIES,
    STATUSES,
    TODO_SPEC,
    TodoItem,
    TodoStore,
)
from nexus.tools.spec import ToolContext


def make_ctx(workspace: Path, *, session_id: str = "s1", store: TodoStore | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id=session_id,
        turn_id="t1",
        config=Config(),
        todo_store=store if store is not None else TodoStore(),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def items(*triples) -> list[dict]:
    return [
        {"id": i, "content": c, "status": s, "priority": p}
        for (i, c, s, p) in triples
    ]


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


def test_spec_shape():
    assert TODO_SPEC.name == "todowrite"
    assert TODO_SPEC.bundle == "task"
    assert TODO_SPEC.mutates is False
    assert TODO_SPEC.concurrency == "exclusive"
    assert TODO_SPEC.resolve_permission_key({}) is None
    schema = TODO_SPEC.input_schema
    assert schema["required"] == ["todos"]
    assert schema["additionalProperties"] is False
    item_schema = schema["properties"]["todos"]["items"]
    assert item_schema["required"] == ["id", "content", "status"]
    assert item_schema["properties"]["status"]["enum"] == list(STATUSES)
    assert item_schema["properties"]["priority"]["enum"] == list(PRIORITIES)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_store_is_session_scoped():
    store = TodoStore()
    a = TodoItem(id="1", content="a", status="pending")
    b = TodoItem(id="2", content="b", status="completed")
    store.replace("s1", [a])
    store.replace("s2", [b])
    assert store.get("s1") == (a,)
    assert store.get("s2") == (b,)
    assert store.get("missing") == ()
    assert set(store.sessions()) == {"s1", "s2"}


def test_store_replace_returns_frozen_and_tracks_revision():
    store = TodoStore()
    item = TodoItem(id="1", content="a", status="pending")
    assert store.replace("s", [item]) == (item,)
    assert store.revision("s") == 1
    store.replace("s", [])
    assert store.revision("s") == 2


def test_store_clear_session_removes_only_that_sessions_agents():
    store = TodoStore()
    root = TodoItem("root", "root work", "pending")
    child = TodoItem("child", "child work", "in_progress")
    sibling = TodoItem("sibling", "sibling work", "pending")
    store.replace("parent", [root])
    store.replace("child-session", [child], agent_id="child-agent")
    store.replace("child-session", [TodoItem("other", "other work", "pending")])
    store.replace("sibling-session", [sibling], agent_id="sibling-agent")

    store.clear_session("child-session")

    assert store.get("parent") == (root,)
    assert store.get("child-session", "child-agent") == ()
    assert store.revision("child-session", "child-agent") == 0
    assert store.get("child-session") == ()
    assert store.get("sibling-session", "sibling-agent") == (sibling,)


def test_store_snapshot_restore_round_trip():
    store = TodoStore()
    store.replace("s1", [TodoItem(id="1", content="a", status="pending", priority="high")])
    store.replace(
        "s1",
        [TodoItem(id="2", content="child", status="in_progress")],
        agent_id="child-1",
    )
    snapshot = store.snapshot()
    restored = TodoStore()
    restored.restore(snapshot)
    assert restored.get("s1") == store.get("s1")
    assert restored.get("s1", "child-1") == store.get("s1", "child-1")
    assert restored.snapshot() == snapshot


def test_store_restore_accepts_legacy_session_snapshot():
    restored = TodoStore()
    restored.restore({"s1": [{"id": "1", "content": "root", "status": "pending"}]})
    assert restored.get("s1") == (TodoItem("1", "root", "pending"),)
    assert restored.get("s1", "child-1") == ()


def test_store_restore_rejects_malformed():
    with pytest.raises(ToolError):
        TodoStore().restore({"s": [{"id": "1", "content": "a", "status": "nope"}]})
    with pytest.raises(ToolError):
        TodoStore().restore("not-a-mapping")


def test_store_replay_restores_latest_revision_per_agent_and_is_idempotent():
    session_id = "session-a"

    def event(agent_id, revision, todos, *, sid=session_id, event_session=session_id):
        return SimpleNamespace(
            type="todo.updated",
            session=event_session,
            data={"session": sid, "agent_id": agent_id, "revision": revision, "todos": todos},
        )

    events = [
        event("root", 1, [{"id": "old", "content": "old", "status": "pending"}]),
        event("child", 3, [{"id": "child", "content": "child work", "status": "in_progress"}]),
        event("root", 2, [{"id": "new", "content": "new", "status": "completed"}]),
        event("other", 8, [{"id": "other", "content": "other", "status": "pending"}], sid="session-b"),
    ]
    store = TodoStore()
    session = SimpleNamespace(id=session_id, events=events)

    store.replay(session)
    expected_root = (TodoItem("new", "new", "completed"),)
    expected_child = (TodoItem("child", "child work", "in_progress"),)
    assert store.get(session_id) == expected_root
    assert store.revision(session_id) == 2
    assert store.get(session_id, "child") == expected_child
    assert store.revision(session_id, "child") == 3
    assert store.get("session-b") == ()

    store.replay(session)
    assert store.get(session_id) == expected_root
    assert store.revision(session_id) == 2
    assert store.revision(session_id, "child") == 3


def test_store_replay_empty_list_is_authoritative_clear():
    session = SimpleNamespace(
        id="s",
        events=[
            SimpleNamespace(
                type="todo.updated",
                session="s",
                data={"session": "s", "agent_id": "root", "revision": 4, "todos": []},
            )
        ],
    )
    store = TodoStore()
    store.replace("s", [TodoItem("1", "work", "pending")])
    store.replay(session)
    assert store.get("s") == ()
    assert store.revision("s") == 4


def test_store_replay_does_not_replace_a_newer_live_revision():
    session = SimpleNamespace(
        id="s",
        events=[
            SimpleNamespace(
                type="todo.updated",
                session="s",
                data={
                    "session": "s",
                    "agent_id": "root",
                    "revision": 2,
                    "todos": [{"id": "disk", "content": "stale", "status": "pending"}],
                },
            )
        ],
    )
    store = TodoStore()
    live = TodoItem("live", "fresh", "completed")
    store.replace("s", [TodoItem("older", "older", "pending")])
    store.replace("s", [live])
    store.replace("s", [live])

    store.replay(session)

    assert store.get("s") == (live,)
    assert store.revision("s") == 3


def test_store_replay_ignores_malformed_unrelated_and_relayed_events():
    good = {"session": "s", "agent_id": "root", "revision": 2, "todos": []}
    events = [
        SimpleNamespace(type="tool.progress", session="s", data=good),
        SimpleNamespace(type="todo.updated", session="elsewhere", data=good),
        SimpleNamespace(
            type="todo.updated",
            session="s",
            data={**good, "session": "child-session", "todos": [
                {"id": "relayed", "content": "child", "status": "pending"}
            ]},
        ),
        SimpleNamespace(type="todo.updated", session="s", data={**good, "revision": True}),
        SimpleNamespace(type="todo.updated", session="s", data={**good, "todos": "bad"}),
        SimpleNamespace(type="todo.updated", session="s", data={**good, "agent_id": ""}),
    ]
    store = TodoStore()
    store.replay(SimpleNamespace(id="s", events=events))
    assert store.get("s") == ()
    assert store.revision("s") == 0


def test_todo_item_validation():
    with pytest.raises(ToolError):
        TodoItem(id="", content="a", status="pending")
    with pytest.raises(ToolError):
        TodoItem(id="1", content="", status="pending")
    with pytest.raises(ToolError):
        TodoItem(id="1", content="a", status="nope")
    with pytest.raises(ToolError):
        TodoItem(id="1", content="a", status="pending", priority="urgent")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def test_run_stores_and_renders_deterministically(workspace: Path):
    store = TodoStore()
    ctx = make_ctx(workspace, store=store)
    result = await todo.run(
        {
            "todos": items(
                ("1", "first", "pending", "high"),
                ("2", "second", "in_progress", "medium"),
                ("3", "third", "completed", "low"),
            )
        },
        ctx,
    )
    assert result.is_error is False
    assert store.get("s1") == (
        TodoItem("1", "first", "pending", "high"),
        TodoItem("2", "second", "in_progress", "medium"),
        TodoItem("3", "third", "completed", "low"),
    )
    text = result.content[0].text
    assert "[ ] 1: first (high)" in text
    assert "[~] 2: second (medium)" in text
    assert "[x] 3: third (low)" in text
    assert result.metrics["counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 1,
    }
    assert result.metrics["todos"] == [item.to_dict() for item in store.get("s1")]


async def test_run_replaces_the_whole_list(workspace: Path):
    store = TodoStore()
    ctx = make_ctx(workspace, store=store)
    await todo.run({"todos": items(("1", "a", "pending", "medium"))}, ctx)
    await todo.run({"todos": items(("2", "b", "completed", "medium"))}, ctx)
    assert [item.id for item in store.get("s1")] == ["2"]


async def test_run_empty_list_clears(workspace: Path):
    store = TodoStore()
    ctx = make_ctx(workspace, store=store)
    await todo.run({"todos": items(("1", "a", "pending", "medium"))}, ctx)
    result = await todo.run({"todos": []}, ctx)
    assert result.is_error is False
    assert store.get("s1") == ()
    assert "cleared" in result.content[0].text


async def test_run_emits_full_revision_for_replace_and_clear(workspace: Path):
    store = TodoStore()
    emitted = []
    ctx = replace(
        make_ctx(workspace, store=store),
        emit=lambda event_type, data: emitted.append((event_type, data)),
    )

    await todo.run({"todos": [{"id": "1", "content": "a", "status": "pending"}]}, ctx)
    await todo.run({"todos": []}, ctx)

    updates = [(kind, data) for kind, data in emitted if kind == "todo.updated"]
    assert [data["revision"] for _, data in updates] == [1, 2]
    assert updates[0][1]["todos"] == [
        {"id": "1", "content": "a", "status": "pending", "priority": "medium"}
    ]
    assert updates[1][1]["todos"] == []
    assert [kind for kind, _ in emitted] == [
        "todo.updated",
        "tool.progress",
        "todo.updated",
        "tool.progress",
    ]


async def test_run_invalid_input_emits_no_event(workspace: Path):
    emitted = []
    ctx = replace(
        make_ctx(workspace),
        emit=lambda event_type, data: emitted.append((event_type, data)),
    )

    result = await todo.run({"todos": [{"id": "1", "content": "", "status": "pending"}]}, ctx)

    assert result.is_error is True
    assert emitted == []


async def test_run_event_failure_restores_previous_revision(workspace: Path):
    store = TodoStore()
    original = [{"id": "old", "content": "old", "status": "pending"}]
    await todo.run({"todos": original}, make_ctx(workspace, store=store))
    async def fail(event_type, _data):
        if event_type == "todo.updated":
            raise OSError("append failed")

    ctx = replace(make_ctx(workspace, store=store), emit=fail)
    with pytest.raises(ToolError, match="previous list was restored"):
        await todo.run(
            {"todos": [{"id": "new", "content": "new", "status": "completed"}]},
            ctx,
        )

    assert store.get("s1") == (TodoItem("old", "old", "pending"),)
    assert store.revision("s1") == 1


async def test_run_defaults_priority(workspace: Path):
    store = TodoStore()
    ctx = make_ctx(workspace, store=store)
    result = await todo.run({"todos": [{"id": "1", "content": "a", "status": "pending"}]}, ctx)
    assert result.is_error is False
    assert store.get("s1")[0].priority == "medium"


@pytest.mark.parametrize(
    "payload",
    [
        {"todos": [{"id": "1", "content": "a", "status": "pending"}, {"id": "1", "content": "b", "status": "pending"}]},
        {"todos": [{"id": "1", "content": "a", "status": "bogus"}]},
        {"todos": [{"id": "1", "content": "a", "status": "pending", "priority": "urgent"}]},
        {"todos": [{"id": "", "content": "a", "status": "pending"}]},
        {"todos": [{"id": "1", "content": "", "status": "pending"}]},
        {"todos": [{"id": 1, "content": "a", "status": "pending"}]},
        {"todos": "not-a-list"},
        {},
        [],
    ],
)
async def test_run_rejects_invalid_and_leaves_state_untouched(workspace: Path, payload):
    store = TodoStore()
    ctx = make_ctx(workspace, store=store)
    result = await todo.run(payload, ctx)
    assert result.is_error is True
    assert store.get("s1") == ()


async def test_run_is_session_scoped(workspace: Path):
    store = TodoStore()
    await todo.run(
        {"todos": items(("1", "a", "pending", "medium"))},
        make_ctx(workspace, session_id="s1", store=store),
    )
    await todo.run(
        {"todos": items(("2", "b", "pending", "medium"))},
        make_ctx(workspace, session_id="s2", store=store),
    )
    assert [item.id for item in store.get("s1")] == ["1"]
    assert [item.id for item in store.get("s2")] == ["2"]


async def test_run_is_agent_scoped_within_session_and_reports_agent(workspace: Path):
    store = TodoStore()
    progress = []
    root_ctx = make_ctx(workspace, session_id="s1", store=store)
    child_ctx = ToolContext(
        workspace=workspace,
        session_id="s1",
        turn_id="child-turn",
        config=Config(),
        todo_store=store,
        agent_id="s1/sub/1",
        emit=lambda _event, data: progress.append(data),
    )

    await todo.run({"todos": items(("root", "root work", "pending", "medium"))}, root_ctx)
    child_result = await todo.run(
        {"todos": items(("child", "child work", "in_progress", "high"))},
        child_ctx,
    )
    await todo.run({"todos": []}, root_ctx)

    assert store.get("s1") == ()
    assert [item.id for item in store.get("s1", "s1/sub/1")] == ["child"]
    assert store.revision("s1") == 2
    assert store.revision("s1", "s1/sub/1") == 1
    assert child_result.metrics["agent_id"] == "s1/sub/1"
    assert progress[0]["agent_id"] == "s1/sub/1"


async def test_run_never_writes_a_workspace_file(workspace: Path):
    before = sorted(p.name for p in workspace.iterdir())
    await todo.run({"todos": items(("1", "a", "pending", "medium"))}, make_ctx(workspace))
    assert sorted(p.name for p in workspace.iterdir()) == before


async def test_run_uses_default_store_when_context_has_none(workspace: Path):
    todo.set_default_store(None)
    ctx = ToolContext(
        workspace=workspace, session_id="s1", turn_id="t1", config=Config()
    )
    try:
        result = await todo.run({"todos": items(("1", "a", "pending", "medium"))}, ctx)
        assert result.is_error is False
        assert todo.get_default_store().get("s1")[0].id == "1"
    finally:
        todo.set_default_store(None)
