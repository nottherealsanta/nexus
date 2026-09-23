"""Pure, deterministic plain-text details of a reduced conversation view.

The status bar, bottom toolbar, ``/details``, and tests all render from here.
Values come only from the ``ConversationView`` the reducer built --
``model.started``/``model.selected`` and ``context.assembled`` -- never guessed,
and every untrusted label is sanitized before it can reach a terminal.
"""
from __future__ import annotations

from ...view import ConversationView
from .render import sanitize


def _meta(view: ConversationView) -> dict:
    context = view.context if isinstance(view.context, dict) else {}
    inner = context.get("context")
    return inner if isinstance(inner, dict) else context


def model_text(view: ConversationView) -> str:
    model = view.model if isinstance(view.model, dict) else {}
    provider, identifier = model.get("provider"), model.get("model") or model.get("id")
    if not provider and not identifier: return "-"
    label = f"{sanitize(provider, 40)}/{sanitize(identifier, 80)}" if provider else sanitize(identifier, 80)
    if model.get("tier"): label += f" [{sanitize(model['tier'], 24)}]"
    if model.get("selected"): label += " (selected, next turn)"
    return label


def _usage(view: ConversationView) -> str:
    usage = view.usage
    cache = f" cache {usage.cache_read_tokens}/{usage.cache_write_tokens}" if usage.cache_read_tokens or usage.cache_write_tokens else ""
    return f"{usage.input_tokens}\u2191 {usage.output_tokens}\u2193{cache}"


def _context(view: ConversationView) -> str:
    meta = _meta(view)
    used = meta.get("used_tokens") if isinstance(meta.get("used_tokens"), int) else 0
    budget = meta.get("input_budget") if isinstance(meta.get("input_budget"), int) else 0
    if budget > 0: return f"ctx {used}/{budget} ({used * 100 // budget}%)"
    return f"ctx {used}" if used else "ctx -"


def _queue_text(item: object) -> str:
    content = getattr(item, "content", ()) or ()
    first = content[0] if content else ""
    return sanitize(first.get("text") if isinstance(first, dict) else first, 80)


def status_line(session: str, view: ConversationView) -> str:
    """The one-line status bar: phase, model, usage, context, presence."""
    parts = [f"[{sanitize(session, 60)}]", view.phase or "idle", model_text(view), f"{_usage(view)} tok",
             _context(view), f"{view.presence.viewers} viewer(s)"]
    if view.input_queue: parts.append(f"queue {len(view.input_queue)}")
    if view.agents:
        running = sum(1 for a in view.agents.values() if a.status == "spawned")
        parts.append(f"{len(view.agents)} agent(s)" + (f" ({running} running)" if running else ""))
    return " \u00b7 ".join(parts)


def detail_lines(session: str, view: ConversationView) -> list[str]:
    """The ``/details`` panel: model, context, queue, approvals, subagents."""
    lines = [f"session {sanitize(session, 60)} \u00b7 phase {view.phase} \u00b7 seq {view.last_seq}",
             f"model: {model_text(view)}", f"usage: {_usage(view)} tok", f"context: {_context(view)}"]
    compacted = _meta(view).get("compacted")
    if isinstance(compacted, dict) and compacted:
        lines.append(f"compacted: {sanitize(compacted.get('strategy') or 'yes', 40)}")
    if not view.input_queue: lines.append("queue empty")
    lines += [f"queue #{i.depth} {_queue_text(i)}" for i in view.input_queue]
    for p in view.pending_permissions:
        detail = p.key or p.preview
        lines.append(f"approval {sanitize(p.tool or '?', 60)}" + (f" \u00b7 {sanitize(detail, 80)}" if detail else ""))
    stack = [(a, 0) for a in view.root_agents]
    if not stack: lines.append("no subagents")
    while stack:
        agent, depth = stack.pop(0)
        label = sanitize(agent.task or agent.type or agent.id, 60)
        tier = f" [{sanitize(agent.tier, 24)}]" if agent.tier else ""
        tail = " \u00b7 ".join(filter(None, [
            f"{agent.iterations} it" if agent.iterations else "",
            f"{agent.usage.total_tokens} tok" if agent.usage.total_tokens else "",
            "clamped" if agent.clamped else "",
            sanitize(agent.error, 60) if agent.error else ""]))
        lines.append(f"{'  ' * depth}- {agent.status} {label}{tier}" + (f" \u00b7 {tail}" if tail else ""))
        stack[0:0] = [(c, depth + 1) for c in view.children_of(agent.id)]
    return lines
