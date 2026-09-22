"""``TodoWrite``: session-scoped in-memory task list (bundle ``task``).

The model uses this to plan and track work. State lives in a
:class:`TodoStore` owned by the tool manager (or bound for the current context),
never in a workspace file: the plan defers durable snapshots to Phase 3, and an
agent-writable ``todos.json`` would be an uncontrolled mutation surface.

Injection mirror of :mod:`nexus.tools.builtin._jobs`: :func:`todo_store_for`
resolves, in order, ``ctx.todo_store`` (the explicit :class:`ToolContext` seam),
a :mod:`contextvars` binding installed by :func:`bind_store`, and a process-wide
default store. The result payload carries the full serialized state in
``metrics["todos"]`` and emits a ``tool.progress`` record, which is the durable
event/state seam Phase 3 can persist or replay.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from ...errors import ToolError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec

__all__ = [
    "PRIORITIES",
    "STATUSES",
    "TODO_SPEC",
    "TodoItem",
    "TodoStore",
    "bind_store",
    "get_default_store",
    "render_todos",
    "reset_store",
    "run",
    "set_default_store",
    "todo_store_for",
    "use_store",
]

STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed")
PRIORITIES: tuple[str, ...] = ("low", "medium", "high")
_DEFAULT_PRIORITY = "medium"

_STATUS_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


@dataclass(frozen=True)
class TodoItem:
    """One normalized todo entry. Immutable and JSON-serializable."""

    id: str
    content: str
    status: str
    priority: str = _DEFAULT_PRIORITY

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ToolError("todo id must be a non-empty string")
        if not isinstance(self.content, str) or not self.content:
            raise ToolError("todo content must be a non-empty string")
        if self.status not in STATUSES:
            raise ToolError(f"todo status must be one of {', '.join(STATUSES)}")
        if self.priority not in PRIORITIES:
            raise ToolError(f"todo priority must be one of {', '.join(PRIORITIES)}")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TodoItem:
        if not isinstance(data, Mapping):
            raise ToolError("todo must be an object")
        return cls(
            id=data.get("id"),
            content=data.get("content"),
            status=data.get("status"),
            priority=data.get("priority", _DEFAULT_PRIORITY),
        )


class TodoStore:
    """A keyed, in-memory map of ``session_id -> tuple[TodoItem, ...]``.

    ``replace`` is atomic from the caller's point of view and returns the stored
    frozen tuple, so a tool can render exactly what was committed. ``snapshot``/
    ``restore`` are the Phase 3 persistence seam; nothing here touches disk.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, tuple[TodoItem, ...]] = {}
        self._revisions: dict[str, int] = {}

    def get(self, session_id: str) -> tuple[TodoItem, ...]:
        return self._by_session.get(session_id, ())

    def replace(
        self, session_id: str, items: Iterable[TodoItem]
    ) -> tuple[TodoItem, ...]:
        frozen = tuple(items)
        for item in frozen:
            if not isinstance(item, TodoItem):
                raise ToolError("TodoStore.replace expects TodoItem instances")
        self._by_session[session_id] = frozen
        self._revisions[session_id] = self._revisions.get(session_id, 0) + 1
        return frozen

    def clear(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)
        self._revisions.pop(session_id, None)

    def revision(self, session_id: str) -> int:
        return self._revisions.get(session_id, 0)

    def sessions(self) -> tuple[str, ...]:
        return tuple(self._by_session)

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        return {
            session: [item.to_dict() for item in items]
            for session, items in self._by_session.items()
        }

    def restore(self, data: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        if not isinstance(data, Mapping):
            raise ToolError("TodoStore.restore expects a mapping")
        restored: dict[str, tuple[TodoItem, ...]] = {}
        for session, items in data.items():
            if not isinstance(session, str):
                raise ToolError("TodoStore.restore session keys must be strings")
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                raise ToolError("TodoStore.restore values must be sequences")
            restored[session] = tuple(TodoItem.from_dict(item) for item in items)
        self._by_session = restored
        self._revisions = {session: 1 for session in restored}


_default_store: TodoStore | None = None
_current_store: ContextVar[TodoStore | None] = ContextVar(
    "nexus_todo_store", default=None
)


def get_default_store() -> TodoStore:
    global _default_store
    if _default_store is None:
        _default_store = TodoStore()
    return _default_store


def set_default_store(store: TodoStore | None) -> None:
    global _default_store
    _default_store = store


def bind_store(store: TodoStore) -> Token[TodoStore | None]:
    if not isinstance(store, TodoStore):
        raise TypeError("store must be a TodoStore")
    return _current_store.set(store)


def reset_store(token: Token[TodoStore | None]) -> None:
    _current_store.reset(token)


@contextmanager
def use_store(store: TodoStore):
    token = bind_store(store)
    try:
        yield store
    finally:
        reset_store(token)


def todo_store_for(ctx: object) -> TodoStore:
    """Resolve the todo store for a ``ToolContext`` (see module docstring)."""
    explicit = getattr(ctx, "todo_store", None)
    if isinstance(explicit, TodoStore):
        return explicit
    bound = _current_store.get()
    if bound is not None:
        return bound
    return get_default_store()


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

_TODO_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "description": "Stable identifier used to update this item later.",
        },
        "content": {
            "type": "string",
            "minLength": 1,
            "description": "Imperative description of the task.",
        },
        "status": {
            "type": "string",
            "enum": list(STATUSES),
            "description": "Lifecycle state of the task.",
        },
        "priority": {
            "type": "string",
            "enum": list(PRIORITIES),
            "description": "Optional priority; defaults to 'medium'.",
        },
    },
    "required": ["id", "content", "status"],
    "additionalProperties": False,
}

TODO_SPEC = ToolSpec(
    name="TodoWrite",
    description=(
        "Create or update the session's task list. Send the complete list each "
        "time; omitted items are removed. State is in-memory for this session."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": _TODO_ITEM_SCHEMA,
                "description": "The complete desired todo list; pass [] to clear.",
            }
        },
        "required": ["todos"],
        "additionalProperties": False,
    },
    bundle="task",
    mutates=False,
    concurrency="exclusive",
    max_result_tokens=25_000,
)


def _parse_todos(raw: object) -> tuple[list[TodoItem], list[str]]:
    if not isinstance(raw, list):
        return [], ["'todos' must be an array"]
    parsed: list[TodoItem] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            errors.append(f"todos[{index}] must be an object")
            continue
        item_id = item.get("id")
        content = item.get("content")
        status = item.get("status")
        priority = item.get("priority", _DEFAULT_PRIORITY)
        if not isinstance(item_id, str) or not item_id:
            errors.append(f"todos[{index}].id must be a non-empty string")
            continue
        if not isinstance(content, str) or not content:
            errors.append(f"todos[{index}].content must be a non-empty string")
            continue
        if status not in STATUSES:
            errors.append(
                f"todos[{index}].status must be one of {', '.join(STATUSES)}"
            )
            continue
        if priority not in PRIORITIES:
            errors.append(
                f"todos[{index}].priority must be one of {', '.join(PRIORITIES)}"
            )
            continue
        if item_id in seen:
            errors.append(f"todos[{index}].id {item_id!r} is duplicated")
            continue
        seen.add(item_id)
        parsed.append(
            TodoItem(id=item_id, content=content, status=status, priority=priority)
        )
    return parsed, errors


def _counts(items: Sequence[TodoItem]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for item in items:
        counts[item.status] += 1
    return counts


def render_todos(items: Sequence[TodoItem]) -> str:
    """Deterministic model-facing rendering in declaration order."""
    if not items:
        return "(no todos)"
    lines = []
    for item in items:
        mark = _STATUS_MARK.get(item.status, "[?]")
        lines.append(f"{mark} {item.id}: {item.content} ({item.priority})")
    return "\n".join(lines)


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return ToolExecutionResult.text(
            "TodoWrite: arguments must be an object", is_error=True
        )
    parsed, errors = _parse_todos(args.get("todos"))
    if errors:
        return ToolExecutionResult.text(
            "TodoWrite: invalid todos: " + "; ".join(errors[:10]), is_error=True
        )

    store = todo_store_for(ctx)
    items = store.replace(ctx.session_id, parsed)
    counts = _counts(items)
    summary = ", ".join(
        f"{counts[status]} {status.replace('_', ' ')}" for status in STATUSES
    )
    first_line = (
        f"TodoWrite: {len(items)} item(s) updated"
        if items
        else "TodoWrite: 0 items (cleared)"
    )
    body = f"{first_line} ({summary})\n{render_todos(items)}"

    payload = {
        "todos": [item.to_dict() for item in items],
        "counts": counts,
        "revision": store.revision(ctx.session_id),
        "session": ctx.session_id,
    }
    await ctx.report("todos updated", payload)

    return ToolExecutionResult.text(
        body,
        display=f"TodoWrite: {len(items)} item(s)",
        metrics={"counts": counts, "todos": payload["todos"], "revision": payload["revision"]},
    )
