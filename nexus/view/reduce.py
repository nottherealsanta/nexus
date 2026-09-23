"""The pure, synchronous event reducer (PLAN section 14.7).

``apply(state, event) -> state`` folds one :class:`~nexus.events.Event` into a
:class:`~nexus.view.model.ConversationView`. No I/O, no mutation, and only
``nexus.events`` plus this package's helpers, so the daemon, the CLI, and any
future frontend render identical semantics. Replay is idempotent (an already
applied ``seq`` is ignored), unknown events are retained as diagnostics, and a
relayed child event carrying an ``agent`` block is reduced into that agent's
nested conversation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..events import Event
from .fold import accumulate, finalize_text, finalize_thinking
from .model import (
    AgentView,
    BlockView,
    ContextView,
    ConversationView,
    DiagnosticView,
    ErrorView,
    ExtensionView,
    HookView,
    McpView,
    MessageView,
    PermissionView,
    PresenceView,
    QueuedInputView,
    RegistryView,
    RetryView,
    SkillView,
    ToolCallView,
    TurnView,
    UsageTotals,
    jsonable,
)

__all__ = ["apply", "apply_many", "initial_state"]

#: Legacy provider events kept by the pre-Phase-1 path.
_LEGACY = frozenset({"started", "message", "provider", "completed"})

def initial_state(session_id: str | None = None) -> ConversationView:
    """An empty view, optionally bound to a session id up front."""
    return ConversationView(session_id=session_id)

# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def _text(data: Mapping[str, Any]) -> str:
    value = data.get("text")
    return value if isinstance(value, str) else ""

def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value

def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None

def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None

def _key_int(data: Mapping[str, Any], key: str) -> int | None:
    return _opt_int(data.get(key))

def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

def _key_map(data: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    value = data.get(key)
    return jsonable(value) if isinstance(value, Mapping) else None

def _str_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value]

def _replace_at(items: Sequence[Any], index: int, item: Any) -> list[Any]:
    out = list(items)
    out[index] = item
    return out

def _turn_index(state: ConversationView, turn_id: str) -> int:
    for i, turn in enumerate(state.turns):
        if turn.id == turn_id:
            return i
    return -1

def _put(state: ConversationView, index: int, turn: TurnView) -> ConversationView:
    return replace(state, turns=_replace_at(state.turns, index, turn))

def _append_turn(state: ConversationView, turn: TurnView) -> ConversationView:
    return replace(state, turns=[*state.turns, turn])

def _turn_for(state: ConversationView, event: Event) -> tuple[ConversationView, int]:
    """The turn a content event belongs to.

    An explicit ``event.turn`` wins; otherwise the most recent turn is used (a
    relayed child event carries no turn id), and if none exists one is opened so
    a log that begins mid-stream still reduces.
    """
    if event.turn:
        index = _turn_index(state, event.turn)
        if index >= 0:
            return state, index
        turn = TurnView(
            id=event.turn,
            index=len(state.turns),
            phase="active",
            started_ts=event.ts,
            updated_ts=event.ts,
        )
        return _append_turn(state, turn), len(state.turns)
    if state.turns:
        return state, len(state.turns) - 1
    turn = TurnView(id=f"turn@{event.seq}", index=0, phase="active", started_ts=event.ts)
    return _append_turn(state, turn), 0

def _ensure_assistant(
    turn: TurnView, event: Event, data: Mapping[str, Any]
) -> tuple[TurnView, int]:
    """The open assistant message, opening one if the last is done or absent."""
    messages = list(turn.messages)
    if messages and messages[-1].role == "assistant" and not messages[-1].done:
        return turn, len(messages) - 1
    message = MessageView(
        role="assistant",
        iteration=_as_int(data.get("iteration"), turn.iteration),
        provider=_as_str(data.get("provider")),
        model=_as_str(data.get("model")),
        attempt=_as_int(data.get("attempt")),
    )
    return replace(turn, messages=[*messages, message]), len(messages)

def _with_message(
    turn: TurnView, message_index: int, message: MessageView
) -> TurnView:
    return replace(turn, messages=_replace_at(turn.messages, message_index, message))

def _update_tool(turn: TurnView, call_id: str, **changes: Any) -> TurnView:
    for i, tool in enumerate(turn.tools):
        if tool.call_id == call_id:
            return replace(turn, tools=_replace_at(turn.tools, i, replace(tool, **changes)))
    return turn

def _diagnostic(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    entry = DiagnosticView(
        type=event.type,
        seq=event.seq,
        ts=event.ts,
        data=jsonable(dict(data)),
        event_id=event.id,
    )
    return replace(state, diagnostics=[*state.diagnostics, entry])

def _error(state: ConversationView, event: Event, message: str, data: Mapping[str, Any]) -> ConversationView:
    entry = ErrorView(
        type=event.type,
        message=message,
        seq=event.seq,
        ts=event.ts,
        data=jsonable(dict(data)),
    )
    return replace(state, errors=[*state.errors, entry])

def _agent_meta(data: Mapping[str, Any]) -> dict[str, Any] | None:
    agent = data.get("agent")
    if not isinstance(agent, Mapping):
        return None
    identifier = agent.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    return dict(agent)

def _without_agent(event: Event) -> Event:
    data = {key: value for key, value in event.data.items() if key != "agent"}
    return Event(
        type=event.type,
        data=data,
        seq=event.seq,
        ts=event.ts,
        session=event.session,
        turn=event.turn,
        id=event.id,
    )

def _with_turn(event: Event, turn_id: str) -> Event:
    return Event(
        type=event.type,
        data=dict(event.data),
        seq=event.seq,
        ts=event.ts,
        session=event.session,
        turn=turn_id,
        id=event.id,
    )

def _expire_pending(state: ConversationView, ids: set[str]) -> ConversationView:
    if not ids:
        return state
    permissions = [
        replace(p, status="cancelled")
        if p.status == "pending" and p.id in ids
        else p
        for p in state.permissions
    ]
    return replace(state, permissions=permissions)

def _recompute_phase(state: ConversationView) -> ConversationView:
    if state.closed:
        return state if state.phase == "closed" else replace(state, phase="closed")
    if any(p.status == "pending" for p in state.permissions):
        return state if state.phase == "awaiting_permission" else replace(
            state, phase="awaiting_permission"
        )
    if state.active_turn is not None:
        return state if state.phase == "running" else replace(state, phase="running")
    if state.input_queue:
        return state if state.phase == "awaiting_input" else replace(
            state, phase="awaiting_input"
        )
    return state if state.phase == "idle" else replace(state, phase="idle")

# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------

def _on_turn_started(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    turn_id = event.turn or f"turn@{event.seq}"
    limits = dict(data.get("limits")) if isinstance(data.get("limits"), Mapping) else {}
    index = _turn_index(state, turn_id)
    if index >= 0:
        # A queued ``input.consumed`` can precede ``turn.started``; keep its message.
        turn = state.turns[index]
        turn = replace(
            turn, phase="active", limits=limits or turn.limits,
            started_ts=turn.started_ts or event.ts, updated_ts=event.ts,
        )
        return _put(state, index, turn)
    turn = TurnView(id=turn_id, index=len(state.turns), phase="active", limits=limits, started_ts=event.ts, updated_ts=event.ts)
    return _append_turn(state, turn)

def _on_turn_terminal(
    state: ConversationView,
    event: Event,
    data: Mapping[str, Any],
    phase: str,
) -> ConversationView:
    turn_id = event.turn
    if not turn_id and state.turns:
        turn_id = state.turns[-1].id
    if not turn_id:
        turn_id = f"turn@{event.seq}"
    state, index = _turn_for(state, _with_turn(event, turn_id))
    turn = state.turns[index]
    iteration = _as_int(data.get("iterations"), turn.iteration)
    messages = [
        replace(m, done=True) if m.role == "assistant" and not m.done else m
        for m in turn.messages
    ]
    changes: dict[str, Any] = {
        "phase": phase,
        "iteration": iteration,
        "messages": messages,
        "updated_ts": event.ts,
    }
    if phase == "completed":
        changes["stop_reason"] = _as_str(data.get("stop_reason"))
        usage = data.get("usage")
        if isinstance(usage, Mapping):
            changes["usage"] = UsageTotals.from_dict(dict(usage))
    elif phase == "failed":
        changes["error"] = _as_str(data.get("error"))
    else:
        changes["error"] = _as_str(data.get("reason"))
    turn = replace(turn, **changes)
    state = _put(state, index, turn)
    return _expire_pending(state, set(turn.permission_ids))

# ---------------------------------------------------------------------------
# Model output
# ---------------------------------------------------------------------------

def _on_model_selected(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    """Record the session's selected model until a turn reports the effective one.

    A selection is session-scoped and out-of-turn, so this replaces the
    ``model`` summary with the chosen reference/provider/model/tier. The next
    ``model.started`` overwrites it with the model the turn actually used.
    """
    selected = {
        "selected": True,
        "reference": _as_str(data.get("reference")),
        "provider": _as_str(data.get("provider")),
        "model": _as_str(data.get("model")),
        "tier": _as_str(data.get("tier")),
        "tier_source": _as_str(data.get("tier_source")),
        "clamped": data.get("clamped") is True,
    }
    return replace(state, model=selected)

def _on_model_started(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    iteration = _as_int(data.get("iteration"), turn.iteration)
    provider = _as_str(data.get("provider"))
    model = _as_str(data.get("model"))
    attempt = _as_int(data.get("attempt"))
    messages = list(turn.messages)
    if messages and messages[-1].role == "assistant" and not messages[-1].done and not messages[-1].blocks:
        messages[-1] = replace(
            messages[-1],
            iteration=iteration,
            provider=provider,
            model=model,
            attempt=attempt,
        )
    else:
        messages.append(
            MessageView(
                role="assistant",
                iteration=iteration,
                provider=provider,
                model=model,
                attempt=attempt,
            )
        )
    turn = replace(turn, messages=messages, iteration=iteration, updated_ts=event.ts)
    model_info = {
        "iteration": iteration,
        "provider": provider,
        "model": model,
        "tools": bool(data.get("tools")),
        "streaming": bool(data.get("streaming")),
        "attempt": attempt,
    }
    return replace(_put(state, index, turn), model=model_info)

def _on_text_delta(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn, message_index = _ensure_assistant(state.turns[index], event, data)
    message = turn.messages[message_index]
    blocks = accumulate(message.blocks, "text", _text(data))
    turn = _with_message(turn, message_index, replace(message, blocks=blocks))
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_text(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn, message_index = _ensure_assistant(state.turns[index], event, data)
    message = turn.messages[message_index]
    blocks = finalize_text(message.blocks, _text(data))
    turn = _with_message(turn, message_index, replace(message, blocks=blocks))
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_thinking_delta(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn, message_index = _ensure_assistant(state.turns[index], event, data)
    message = turn.messages[message_index]
    blocks = accumulate(message.blocks, "thinking", _text(data))
    turn = _with_message(turn, message_index, replace(message, blocks=blocks))
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_thinking(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn, message_index = _ensure_assistant(state.turns[index], event, data)
    message = turn.messages[message_index]
    blocks = finalize_thinking(
        message.blocks, _text(data), _as_str(data.get("signature"))
    )
    turn = _with_message(turn, message_index, replace(message, blocks=blocks))
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_model_usage(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    turn = replace(turn, usage=turn.usage.merge(UsageTotals.from_stream(dict(data))), updated_ts=event.ts)
    return _put(state, index, turn)

def _on_model_stopped(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    messages = list(turn.messages)
    if messages and messages[-1].role == "assistant" and not messages[-1].done:
        messages[-1] = replace(
            messages[-1], done=True, stop_reason=_as_str(data.get("stop_reason"))
        )
    turn = replace(turn, messages=messages, updated_ts=event.ts)
    return _put(state, index, turn)

def _on_model_retrying(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    retry = RetryView(
        iteration=_as_int(data.get("iteration"), turn.iteration),
        attempt=_as_int(data.get("attempt")),
        from_provider=_as_str(data.get("from_provider")),
        from_model=_as_str(data.get("from_model")),
        provider=_as_str(data.get("provider")),
        model=_as_str(data.get("model")),
        reason=_as_str(data.get("reason")),
    )
    turn = replace(turn, retries=[*turn.retries, retry], updated_ts=event.ts)
    return _put(state, index, turn)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _on_tool_requested(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    call_id = _as_str(data.get("call_id")) or ""
    name = _as_str(data.get("tool")) or ""
    existing = next((t for t in turn.tools if t.call_id == call_id), None)
    if existing is None:
        turn = replace(
            turn,
            tools=[
                *turn.tools,
                ToolCallView(
                    call_id=call_id,
                    name=name,
                    status="requested",
                    requested_ts=event.ts,
                ),
            ],
            updated_ts=event.ts,
        )
    else:
        turn = _update_tool(turn, call_id, name=name, status="requested")
    return _put(state, index, turn)

def _on_tool_started(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    call_id = _as_str(data.get("call_id")) or ""
    turn = _update_tool(
        turn,
        call_id,
        status="running",
        name=_as_str(data.get("tool")) or "",
        bundle=_as_str(data.get("bundle")),
        started_ts=event.ts,
    )
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_tool_progress(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    call_id = _as_str(data.get("call_id")) or ""
    for i, tool in enumerate(turn.tools):
        if tool.call_id == call_id:
            progress = [*tool.progress, _text(data)] if _text(data) else tool.progress
            turn = replace(turn, tools=_replace_at(turn.tools, i, replace(tool, progress=progress)))
            break
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_tool_completed(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    call_id = _as_str(data.get("call_id")) or ""
    turn = _update_tool(
        turn,
        call_id,
        status="completed",
        name=_as_str(data.get("tool")) or "",
        is_error=bool(data.get("is_error")),
        executed=bool(data.get("executed", True)),
        duration_ms=data.get("duration_ms") if isinstance(data.get("duration_ms"), int) else None,
        finished_ts=event.ts,
    )
    return _put(state, index, replace(turn, updated_ts=event.ts))

def _on_tool_failed(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    call_id = _as_str(data.get("call_id")) or ""
    error = _as_str(data.get("error"))
    turn = _update_tool(
        turn,
        call_id,
        status="failed",
        name=_as_str(data.get("tool")) or "",
        code=_as_str(data.get("code")),
        error=error,
        executed=bool(data.get("executed")),
        duration_ms=data.get("duration_ms") if isinstance(data.get("duration_ms"), int) else None,
        finished_ts=event.ts,
    )
    return _put(state, index, replace(turn, updated_ts=event.ts))

# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def _permission_view(data: Mapping[str, Any], status: str, *, ts: float | None = None) -> PermissionView:
    grant = data.get("grant")
    return PermissionView(
        id=_as_str(data.get("id")) or "", call_id=_as_str(data.get("call_id")),
        tool=_as_str(data.get("tool")), key=_as_str(data.get("key")),
        bundle=_as_str(data.get("bundle")), preview=_as_str(data.get("preview")),
        suggestions=_str_list(data.get("suggestions")), default_rule=_as_str(data.get("default_rule")),
        persistence_available=bool(data.get("persistence_available", True)), status=status,
        decision=_as_str(data.get("decision")), scope=_as_str(data.get("scope")),
        grant=jsonable(grant) if grant is not None else None, ts=ts,
    )

def _on_permission_requested(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    permission = _permission_view(data, "pending")
    state = replace(state, permissions=[*state.permissions, permission])
    turn_index = _turn_index(state, event.turn) if event.turn else (len(state.turns) - 1 if state.turns else -1)
    if turn_index >= 0 and permission.id not in state.turns[turn_index].permission_ids:
        turn = state.turns[turn_index]
        ids = [*turn.permission_ids, permission.id]
        state = _put(state, turn_index, replace(turn, permission_ids=ids, updated_ts=event.ts))
    return state

def _on_permission_resolved(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    request_id = _as_str(data.get("id")) or ""
    ts = _opt_float(data.get("ts")) or event.ts
    grant = data.get("grant")
    permissions = list(state.permissions)
    for i, permission in enumerate(permissions):
        if permission.id == request_id:
            permissions[i] = replace(
                permission, status="resolved", decision=_as_str(data.get("decision")),
                scope=_as_str(data.get("scope")) or permission.scope,
                grant=jsonable(grant) if grant is not None else permission.grant, ts=ts,
            )
            return replace(state, permissions=permissions)
    permissions.append(_permission_view(data, "resolved", ts=ts))
    return replace(state, permissions=permissions)

# ---------------------------------------------------------------------------
# Context / skills / hooks
# ---------------------------------------------------------------------------

def _on_context(state: ConversationView, event: Event, data: Mapping[str, Any], kind: str) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    payload = {key: value for key, value in data.items() if key != "iteration"}
    entry = ContextView(kind=kind, iteration=_as_int(data.get("iteration"), turn.iteration), data=jsonable(payload))
    state = _put(state, index, replace(turn, context=[*turn.context, entry], updated_ts=event.ts))
    if kind == "assembled":
        model_info = {
            "iteration": entry.iteration, "provider": _as_str(data.get("provider")),
            "model": _as_str(data.get("model")), "tools": _as_int(data.get("tools")),
            "messages": _as_int(data.get("messages")),
        }
        return replace(state, context=jsonable(payload), model=model_info)
    return state

def _on_skill_invoked(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    view = SkillView(
        skill=_as_str(data.get("skill")) or "",
        resource=_as_str(data.get("resource")),
        status="invoked",
    )
    turn = replace(turn, skills=[*turn.skills, view], updated_ts=event.ts)
    return _put(state, index, turn)

def _on_skill_completed(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn = state.turns[index]
    name = _as_str(data.get("skill")) or ""
    resource = _as_str(data.get("resource"))
    completed = SkillView(
        skill=name,
        resource=resource,
        status="completed",
        ok=bool(data.get("ok")),
        error=_as_str(data.get("error")),
        scoped_tools=_str_list(data.get("scoped_tools")),
        narrowed=_str_list(data.get("narrowed")),
        unknown_bundles=_str_list(data.get("unknown_bundles")),
    )
    skills = list(turn.skills)
    for i in range(len(skills) - 1, -1, -1):
        if skills[i].skill == name and skills[i].status == "invoked":
            skills[i] = completed
            break
    else:
        skills.append(completed)
    turn = replace(turn, skills=skills, updated_ts=event.ts)
    return _put(state, index, turn)

def _hook_view(data: Mapping[str, Any], *, status: str) -> HookView:
    return HookView(
        event=_as_str(data.get("event")) or "",
        hook=_as_str(data.get("hook")),
        kind=_as_str(data.get("kind")),
        action=_as_str(data.get("action")),
        reason=_as_str(data.get("reason")),
        status=status,
    )

def _on_hook(state: ConversationView, event: Event, data: Mapping[str, Any], *, status: str) -> ConversationView:
    view = _hook_view(data, status=status)
    if event.turn:
        state, index = _turn_for(state, event)
        turn = state.turns[index]
        turn = replace(turn, hooks=[*turn.hooks, view], updated_ts=event.ts)
        return _put(state, index, turn)
    return replace(state, hooks=[*state.hooks, view])

# ---------------------------------------------------------------------------
# Session / presence / input / extensions / MCP / registry
# ---------------------------------------------------------------------------

def _on_presence(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    viewers = _as_int(data.get("viewers"), state.presence.viewers)
    attended = bool(data.get("attended")) if "attended" in data else state.presence.attended
    if event.type in ("presence.joined", "presence.left"):
        attended = viewers > 0
    return replace(state, presence=PresenceView(viewers=viewers, attended=attended))

def _on_input(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    queue = list(state.input_queue)
    queued_id = _as_str(data.get("queued_id")) or ""
    if event.type == "input.queued":
        content = data.get("content")
        queue.append(
            QueuedInputView(
                queued_id=queued_id,
                content=jsonable(content) if content is not None else [],
                depth=_as_int(data.get("queue_depth"), len(queue) + 1),
            )
        )
        return replace(state, input_queue=queue)
    # consumed / dropped: remove the entry, remembering its content first.
    pending_content = next(
        (item.content for item in queue if item.queued_id == queued_id), None
    )
    queue = [item for item in queue if item.queued_id != queued_id]
    state = replace(state, input_queue=queue)
    if event.type == "input.consumed":
        turn_id = _as_str(data.get("turn"))
        if turn_id:
            state, index = _turn_for(state, _with_turn(event, turn_id))
            turn = state.turns[index]
            message = MessageView(role="user", blocks=[], iteration=turn.iteration, done=True)
            if pending_content:
                message = replace(
                    message,
                    blocks=[BlockView(kind="text", text=_content_text(pending_content), finalized=True)],
                )
            state = _put(state, index, replace(turn, messages=[message, *turn.messages], updated_ts=event.ts))
    return state

def _content_text(content: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)

def _on_ext(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    ext = dict(state.extensions)
    t = event.type
    if t == "ext.manifest_changed":
        ext["__manifest__"] = ExtensionView(
            name="__manifest__", trigger=_as_str(data.get("trigger")),
            summary=_key_map(data, "summary"), diff=_key_map(data, "diff"),
            previous_generation=_key_int(data, "previous_generation"),
            generation=_key_int(data, "generation"),
        )
        return replace(state, extensions=ext)
    name = _as_str(data.get("name")) or ""
    if t == "ext.loaded":
        ext[name] = ExtensionView(
            name=name, status="loaded", source=_as_str(data.get("source")),
            sha256=_as_str(data.get("sha256")), generation=_key_int(data, "generation"),
            origin=_as_str(data.get("origin")),
        )
    elif t == "ext.unloaded":
        ext[name] = ExtensionView(name=name, status="unloaded", source=_as_str(data.get("source")))
    elif t == "ext.failed":
        ext[name] = ExtensionView(
            name=name, status="failed", kind=_as_str(data.get("kind")),
            error=_as_str(data.get("error")), error_type=_as_str(data.get("error_type")),
        )
    elif t == "ext.tool_shadowed":
        ext[name] = ExtensionView(name=name, status="shadowed", source=_as_str(data.get("source")))
    return replace(state, extensions=ext)

def _on_mcp(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    mcp = dict(state.mcp)
    server = _as_str(data.get("server")) or ""
    current = mcp.get(server, McpView(server=server))
    generation = _key_int(data, "generation") or current.generation
    if event.type == "mcp.connected":
        current = replace(
            current, status="connected", version=_as_str(data.get("version")),
            tool_count=_as_int(data.get("tools"), current.tool_count or 0),
            cached=bool(data.get("cached")) if "cached" in data else current.cached,
            generation=generation, reason=None, error=None,
        )
    elif event.type == "mcp.disconnected":
        current = replace(current, status="disconnected", reason=_as_str(data.get("reason")))
    elif event.type == "mcp.failed":
        current = replace(
            current, status="failed", error=_as_str(data.get("error")),
            error_type=_as_str(data.get("error_type")),
            attempts=_as_int(data.get("attempts"), current.attempts or 0),
            health=_as_str(data.get("health")), retry_in_s=_opt_float(data.get("retry_in_s")),
        )
    elif event.type == "mcp.tools_changed":
        current = replace(current, tools=_str_list(data.get("tools")), generation=generation)
    mcp[server] = current
    return replace(state, mcp=mcp)

def _on_registry(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    status = event.type.split(".", 1)[1] if "." in event.type else "failed"
    detail = {k: v for k, v in data.items() if k not in ("source", "stale", "models", "generation", "error")}
    registry = RegistryView(
        status=status, source=_as_str(data.get("source")),
        stale=bool(data.get("stale")) if "stale" in data else None,
        models=_key_int(data, "models"), generation=_key_int(data, "generation"),
        error=_as_str(data.get("error")), detail=jsonable(detail),
    )
    return replace(state, registry=registry)

# ---------------------------------------------------------------------------
# Agent lifecycle and routing
# ---------------------------------------------------------------------------

def _on_agent_spawned(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    meta = _agent_meta(data) or {}
    agent_id = _as_str(meta.get("id")) or _as_str(data.get("id"))
    if not agent_id:
        return _diagnostic(state, event, data)
    turn_id = str(meta.get("task") or agent_id)
    body = ConversationView(session_id=_as_str(meta.get("session")))
    body = replace(
        body,
        turns=[TurnView(id=turn_id, index=0, phase="active", started_ts=event.ts)],
    )
    agent = AgentView(
        id=agent_id,
        parent=_as_str(meta.get("parent")),
        type=_as_str(meta.get("type")),
        task=_as_str(meta.get("task")),
        session=_as_str(meta.get("session")),
        depth=_as_int(meta.get("depth")),
        tier=_as_str(data.get("tier")) or _as_str(meta.get("tier")),
        requested_tier=_as_str(data.get("requested_tier")),
        index=_opt_int(meta.get("index")),
        description=_as_str(data.get("description")) or "",
        model=_as_str(data.get("model")),
        tools=_str_list(data.get("tools")),
        dropped_tools=_str_list(data.get("dropped_tools")),
        clamped=bool(data.get("clamped")),
        status="spawned",
        body=body,
        spawned_ts=event.ts,
    )
    agents = dict(state.agents)
    agents[agent_id] = agent
    order = list(state.agent_order)
    if agent_id not in order:
        order.append(agent_id)
    return replace(state, agents=agents, agent_order=order)

def _on_agent_completed(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    meta = _agent_meta(data) or {}
    agent_id = _as_str(meta.get("id")) or _as_str(data.get("id"))
    if not agent_id or agent_id not in state.agents:
        return _diagnostic(state, event, data)
    agent = state.agents[agent_id]
    status = _as_str(data.get("status")) or "completed"
    stop_reason = _as_str(data.get("stop_reason"))
    body = agent.body
    turns = [
        replace(turn, phase="completed", stop_reason=stop_reason or turn.stop_reason)
        if turn.phase == "active"
        else turn
        for turn in body.turns
    ]
    body = _recompute_phase(replace(body, turns=turns))
    agent = replace(
        agent,
        status=status,
        ok=bool(data.get("ok")) if "ok" in data else None,
        is_error=bool(data.get("is_error")) if "is_error" in data else None,
        error=_as_str(data.get("error")),
        iterations=_as_int(data.get("iterations"), agent.iterations),
        stop_reason=_as_str(data.get("stop_reason")),
        usage=UsageTotals.from_dict(data.get("usage")) if isinstance(data.get("usage"), Mapping) else agent.usage,
        dropped_tools=_str_list(data.get("dropped_tools")) or agent.dropped_tools,
        clamped=bool(data.get("clamped")) if "clamped" in data else agent.clamped,
        body=body,
        completed_ts=event.ts,
    )
    agents = dict(state.agents)
    agents[agent_id] = agent
    return replace(state, agents=agents, agent_order=list(state.agent_order))

def _on_agent_clamped(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    meta = _agent_meta(data) or {}
    agent_id = _as_str(meta.get("id")) or _as_str(data.get("id"))
    if not agent_id or agent_id not in state.agents:
        return _diagnostic(state, event, data)
    agent = state.agents[agent_id]
    agent = replace(
        agent,
        clamped=True,
        max_tier=_as_str(data.get("max_tier")) or agent.max_tier,
        requested_tier=_as_str(data.get("requested_tier")) or agent.requested_tier,
        diagnostics=[*agent.diagnostics, *_str_list(data.get("diagnostics"))],
    )
    agents = dict(state.agents)
    agents[agent_id] = agent
    return replace(state, agents=agents, agent_order=list(state.agent_order))

def _apply_agent(
    state: ConversationView, event: Event, meta: Mapping[str, Any]
) -> ConversationView:
    agent_id = _as_str(meta.get("id"))
    if not agent_id:
        return _diagnostic(state, event, event.data)
    agent = state.agents.get(agent_id)
    if agent is None:
        body = ConversationView(session_id=_as_str(meta.get("session")))
        body = replace(
            body,
            turns=[
                TurnView(
                    id=str(meta.get("task") or agent_id),
                    index=0,
                    phase="active",
                    started_ts=event.ts,
                )
            ],
        )
        agent = AgentView(
            id=agent_id,
            parent=_as_str(meta.get("parent")),
            type=_as_str(meta.get("type")),
            task=_as_str(meta.get("task")),
            session=_as_str(meta.get("session")),
            depth=_as_int(meta.get("depth")),
            tier=_as_str(meta.get("tier")),
            index=_opt_int(meta.get("index")),
            body=body,
        )
    body = apply(agent.body, _without_agent(event))
    agent = replace(agent, body=body)
    agents = dict(state.agents)
    agents[agent_id] = agent
    order = list(state.agent_order)
    if agent_id not in order:
        order.append(agent_id)
    return replace(state, agents=agents, agent_order=order)

# ---------------------------------------------------------------------------
# Legacy / unknown
# ---------------------------------------------------------------------------

def _on_legacy(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    if event.type == "message":
        return _on_legacy_message(state, event, data)
    if event.type == "completed":
        state = _on_legacy_message(state, event, {"text": data.get("text")})
        return _on_turn_terminal(state, event, {"stop_reason": "end_turn"}, "completed")
    if event.type == "started":
        state = replace(
            state,
            opened=True,
            session_id=_as_str(data.get("session")) or state.session_id,
        )
        return _on_turn_started(state, event, {})
    if event.type == "provider":
        return _diagnostic(state, event, data)
    if event.type == "error":
        message = _as_str(data.get("message")) or _as_str(data.get("error")) or str(data)
        return _error(state, event, message, data)
    return _diagnostic(state, event, data)

def _on_legacy_message(state: ConversationView, event: Event, data: Mapping[str, Any]) -> ConversationView:
    state, index = _turn_for(state, event)
    turn, message_index = _ensure_assistant(state.turns[index], event, data)
    message = turn.messages[message_index]
    text = _text(data)
    blocks = list(message.blocks)
    if text:
        blocks.append(BlockView(kind="text", text=text, finalized=True))
    turn = _with_message(turn, message_index, replace(message, blocks=blocks))
    return _put(state, index, replace(turn, updated_ts=event.ts))

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "turn.started": _on_turn_started,
    "turn.completed": lambda s, e, d: _on_turn_terminal(s, e, d, "completed"),
    "turn.failed": lambda s, e, d: _on_turn_terminal(s, e, d, "failed"),
    "turn.cancelled": lambda s, e, d: _on_turn_terminal(s, e, d, "cancelled"),
    "model.selected": _on_model_selected,
    "model.started": _on_model_started,
    "text.delta": _on_text_delta,
    "text": _on_text,
    "thinking.delta": _on_thinking_delta,
    "thinking": _on_thinking,
    "model.usage": _on_model_usage,
    "model.stopped": _on_model_stopped,
    "model.retrying": _on_model_retrying,
    "tool.requested": _on_tool_requested,
    "tool.started": _on_tool_started,
    "tool.progress": _on_tool_progress,
    "tool.completed": _on_tool_completed,
    "tool.failed": _on_tool_failed,
    "permission.requested": _on_permission_requested,
    "permission.resolved": _on_permission_resolved,
    "context.assembled": lambda s, e, d: _on_context(s, e, d, "assembled"),
    "context.compacted": lambda s, e, d: _on_context(s, e, d, "compacted"),
    "context.degraded": lambda s, e, d: _on_context(s, e, d, "degraded"),
    "skill.invoked": _on_skill_invoked,
    "skill.completed": _on_skill_completed,
    "hook.fired": lambda s, e, d: _on_hook(s, e, d, status="fired"),
    "hook.blocked": lambda s, e, d: _on_hook(s, e, d, status="blocked"),
    "presence.joined": _on_presence,
    "presence.left": _on_presence,
    "presence.changed": _on_presence,
    "input.queued": _on_input,
    "input.consumed": _on_input,
    "input.dropped": _on_input,
    "ext.loaded": _on_ext,
    "ext.unloaded": _on_ext,
    "ext.failed": _on_ext,
    "ext.tool_shadowed": _on_ext,
    "ext.manifest_changed": _on_ext,
    "mcp.connected": _on_mcp,
    "mcp.disconnected": _on_mcp,
    "mcp.failed": _on_mcp,
    "mcp.tools_changed": _on_mcp,
    "registry.refreshed": _on_registry,
    "registry.stale": _on_registry,
    "registry.failed": _on_registry,
    "registry.mismatch": _on_registry,
}

def _apply(state: ConversationView, event: Event) -> ConversationView:
    data: Mapping[str, Any] = event.data if isinstance(event.data, Mapping) else {}
    event_type = event.type

    if event_type == "agent.spawned":
        return _on_agent_spawned(state, event, data)
    if event_type == "agent.completed":
        return _on_agent_completed(state, event, data)
    if event_type == "agent.clamped":
        return _on_agent_clamped(state, event, data)

    meta = _agent_meta(data)
    if meta is not None:
        return _apply_agent(state, event, meta)

    handler = _HANDLERS.get(event_type)
    if handler is not None:
        return handler(state, event, data)
    if event_type in _LEGACY:
        return _on_legacy(state, event, data)
    if event_type == "session.opened":
        return replace(
            state,
            opened=True,
            session_id=_as_str(data.get("id")) or event.session or state.session_id,
        )
    if event_type == "session.closed":
        return replace(state, closed=True)
    if event_type == "daemon.started":
        return replace(state, opened=True) if not state.opened else state
    if event_type == "error":
        message = _as_str(data.get("message")) or _as_str(data.get("error")) or str(dict(data))
        return _error(state, event, message, data)
    if event_type == "provider.raw":
        return _diagnostic(state, event, data)
    return _diagnostic(state, event, data)

def apply(state: ConversationView, event: Event) -> ConversationView:
    """Fold one event into ``state`` and return the new state.

    Idempotent for sequenced logs: an event whose ``seq`` has already been
    applied is ignored, so replaying a snapshot plus a tail is exact. The input
    state is never mutated.
    """
    if not isinstance(event, Event):
        raise TypeError("apply expects a nexus.events.Event")
    seq = event.seq
    if isinstance(seq, int) and seq > 0 and seq <= state.last_seq:
        return state
    new_state = _apply(state, event)
    new_state = _recompute_phase(new_state)
    if new_state.session_id is None and event.session:
        new_state = replace(new_state, session_id=event.session)
    if isinstance(seq, int) and seq > 0:
        new_state = replace(new_state, last_seq=seq)
    return new_state

def apply_many(
    state: ConversationView, events: Sequence[Event]
) -> ConversationView:
    """Left-fold :func:`apply` over a sequence of events."""
    view = state
    for event in events:
        view = apply(view, event)
    return view
