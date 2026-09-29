"""``TodoWrite``: agent-scoped in-memory task list (bundle ``task``).

The model uses this to plan and track work. State lives in a
:class:`TodoStore` owned by the tool manager (or bound for the current context),
never in a workspace file. The store is process-memory only; an agent-writable
``todos.json`` would be an uncontrolled mutation surface.

Injection mirror of :mod:`nexus.tools.builtin._jobs`: :func:`todo_store_for`
resolves, in order, ``ctx.todo_store`` (the explicit :class:`ToolContext` seam),
a :mod:`contextvars` binding installed by :func:`bind_store`, and a process-wide
default store. The result payload carries the full serialized state in
``metrics["todos"]`` and emits durable ``todo.updated`` and ``tool.progress``
records. The store itself remains in-memory.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from ...errors import SessionError, ToolError
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
DEFAULT_AGENT_ID = "root"

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
    """An in-memory map keyed by ``(session_id, agent_id)``.

    ``replace`` is atomic from the caller's point of view and returns the stored
    frozen tuple, so a tool can render exactly what was committed. ``snapshot``
    and ``restore`` are in-memory inspection helpers, not session replay.
    """

    def __init__(self) -> None:
        self._by_agent: dict[tuple[str, str], tuple[TodoItem, ...]] = {}
        self._revisions: dict[tuple[str, str], int] = {}

    def get(
        self, session_id: str, agent_id: str = DEFAULT_AGENT_ID
    ) -> tuple[TodoItem, ...]:
        return self._by_agent.get((session_id, agent_id), ())

    def replace(
        self,
        session_id: str,
        items: Iterable[TodoItem],
        agent_id: str = DEFAULT_AGENT_ID,
    ) -> tuple[TodoItem, ...]:
        frozen = tuple(items)
        for item in frozen:
            if not isinstance(item, TodoItem):
                raise ToolError("TodoStore.replace expects TodoItem instances")
        key = (session_id, agent_id)
        self._by_agent[key] = frozen
        self._revisions[key] = self._revisions.get(key, 0) + 1
        return frozen

    def clear(self, session_id: str, agent_id: str = DEFAULT_AGENT_ID) -> None:
        key = (session_id, agent_id)
        self._by_agent.pop(key, None)
        self._revisions.pop(key, None)

    def clear_session(self, session_id: str) -> None:
        """Drop all in-memory agent todo state for one closed session."""
        keys = [key for key in self._by_agent if key[0] == session_id]
        for key in keys:
            self._by_agent.pop(key, None)
            self._revisions.pop(key, None)

    def revision(self, session_id: str, agent_id: str = DEFAULT_AGENT_ID) -> int:
        return self._revisions.get((session_id, agent_id), 0)

    def replay(self, session: object) -> None:
        """Restore each agent's latest valid update from this session's log.

        Replayed revisions are assigned verbatim. A newer in-memory revision
        wins over an older log view, which makes repeated opens safe during a
        live runtime (and prevents a replay from undoing a write in flight).
        Empty todo lists are stored with their revision as authoritative clears.
        """
        session_id = getattr(session, "id", None)
        if not isinstance(session_id, str) or not session_id:
            return

        latest: dict[str, tuple[int, tuple[TodoItem, ...]]] = {}
        try:
            events = session.events
        except SessionError:
            return
        if callable(events):
            try:
                events = events()
            except SessionError:
                return
        for event in events or ():
            if getattr(event, "type", None) != "todo.updated":
                continue
            # Relayed child events carry the parent session envelope, but retain
            # their originating session in the payload. Never replay those into
            # the parent's agent store.
            if getattr(event, "session", None) != session_id:
                continue
            data = getattr(event, "data", None)
            if not isinstance(data, Mapping) or data.get("session") != session_id:
                continue
            agent_id = data.get("agent_id")
            revision = data.get("revision")
            if (
                not isinstance(agent_id, str)
                or not agent_id
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                continue
            parsed, errors = _parse_todos(data.get("todos"))
            if errors:
                continue
            latest[agent_id] = (revision, tuple(parsed))

        for agent_id, (revision, items) in latest.items():
            key = (session_id, agent_id)
            if self._revisions.get(key, 0) >= revision:
                continue
            self._by_agent[key] = items
            self._revisions[key] = revision

    def _rollback_if_revision(
        self,
        session_id: str,
        agent_id: str,
        *,
        expected_revision: int,
        previous_items: tuple[TodoItem, ...],
        previous_revision: int,
    ) -> bool:
        """Restore a failed write only if no later replacement has occurred.

        There is no await in this compare-and-restore, so it is atomic with
        respect to other event-loop tasks using this store.
        """
        key = (session_id, agent_id)
        if self._revisions.get(key, 0) != expected_revision:
            return False
        if previous_revision:
            self._by_agent[key] = previous_items
            self._revisions[key] = previous_revision
        else:
            self._by_agent.pop(key, None)
            self._revisions.pop(key, None)
        return True

    def sessions(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(session for session, _agent in self._by_agent))

    def snapshot(self) -> dict[str, dict[str, list[dict[str, str]]]]:
        return {
            session: {
                agent: [item.to_dict() for item in items]
                for (stored_session, agent), items in self._by_agent.items()
                if stored_session == session
            }
            for session in self.sessions()
        }

    def restore(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise ToolError("TodoStore.restore expects a mapping")
        restored: dict[tuple[str, str], tuple[TodoItem, ...]] = {}
        for session, agents_or_items in data.items():
            if not isinstance(session, str):
                raise ToolError("TodoStore.restore session keys must be strings")
            # Older in-memory snapshots used session -> item-list. Map them to
            # the explicit root identity when restoring.
            if isinstance(agents_or_items, Sequence) and not isinstance(
                agents_or_items, (str, bytes)
            ):
                agents = {DEFAULT_AGENT_ID: agents_or_items}
            elif isinstance(agents_or_items, Mapping):
                agents = agents_or_items
            else:
                raise ToolError(
                    "TodoStore.restore values must be agent mappings or sequences"
                )
            for agent, items in agents.items():
                if not isinstance(agent, str) or not agent:
                    raise ToolError(
                        "TodoStore.restore agent keys must be non-empty strings"
                    )
                if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                    raise ToolError("TodoStore.restore todo values must be sequences")
                restored[(session, agent)] = tuple(
                    TodoItem.from_dict(item) for item in items
                )
        self._by_agent = restored
        self._revisions = {key: 1 for key in restored}


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
    name="todowrite",
    description=(
        "Create or update this agent's task list. Use it for work with three or "
        "more distinct steps, keep one item in progress at a time, and mark items "
        "done as you finish them; skip it for small changes. Send the complete "
        "list each time; omitted items are removed. State is in-memory for this "
        "agent."
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
    previous_items = store.get(ctx.session_id, ctx.agent_id)
    previous_revision = store.revision(ctx.session_id, ctx.agent_id)
    items = store.replace(ctx.session_id, parsed, ctx.agent_id)
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
        "revision": store.revision(ctx.session_id, ctx.agent_id),
        "session": ctx.session_id,
        "agent_id": ctx.agent_id,
    }
    try:
        if ctx.emit is not None:
            outcome = ctx.emit("todo.updated", payload)
            if inspect.isawaitable(outcome):
                await outcome
    except BaseException as exc:
        rolled_back = store._rollback_if_revision(
            ctx.session_id,
            ctx.agent_id,
            expected_revision=payload["revision"],
            previous_items=previous_items,
            previous_revision=previous_revision,
        )
        if not isinstance(exc, Exception):
            raise
        detail = (
            "the previous list was restored"
            if rolled_back
            else "a later list revision is retained in memory"
        )
        raise ToolError(
            f"TodoWrite: durable todo.updated event failed at revision "
            f"{payload['revision']}; {detail}"
        ) from exc

    await ctx.report("todos updated", payload)

    return ToolExecutionResult.text(
        body,
        display=f"TodoWrite: {len(items)} item(s)",
        metrics={
            "counts": counts,
            "todos": payload["todos"],
            "revision": payload["revision"],
            "agent_id": ctx.agent_id,
        },
    )
