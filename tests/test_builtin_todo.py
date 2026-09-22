"""Tests for the built-in ``TodoWrite`` tool and its session-scoped store."""
from __future__ import annotations

from pathlib import Path

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
    assert TODO_SPEC.name == "TodoWrite"
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


def test_store_snapshot_restore_round_trip():
    store = TodoStore()
    store.replace("s1", [TodoItem(id="1", content="a", status="pending", priority="high")])
    snapshot = store.snapshot()
    restored = TodoStore()
    restored.restore(snapshot)
    assert restored.get("s1") == store.get("s1")
    assert restored.snapshot() == snapshot


def test_store_restore_rejects_malformed():
    with pytest.raises(ToolError):
        TodoStore().restore({"s": [{"id": "1", "content": "a", "status": "nope"}]})
    with pytest.raises(ToolError):
        TodoStore().restore("not-a-mapping")


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
