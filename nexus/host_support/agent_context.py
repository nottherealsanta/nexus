"""The request context one subagent actually sent, shaped for the context header.

A child records a redacted, bounded snapshot of its first request (system text
and tool schemas) in its own log (``Runtime.agent_request``). This projection
turns it into the same fields a root ``ContextInspectResult`` carries, so the
subagent page renders its context header with the root's widget. It goes
through the same privacy projection as the root preview.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .context_preview import project_context_preview, safe_text


def project_agent_context(
    agent: Any,
    runtime: Any,
    roles: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """``ContextInspectResult`` fields for one agent; empty until it has sent."""
    reader = getattr(runtime, "agent_request", None)
    snapshot = reader(agent.session or "") if callable(reader) and agent.session else None
    if not isinstance(snapshot, Mapping):
        return {}
    name = safe_text(getattr(agent, "type", None), 80) or "subagent"
    color = next(
        (
            row.get("color")
            for row in roles
            if str(row.get("name", "")).casefold() == name.casefold()
            and isinstance(row.get("color"), str)
        ),
        None,
    )
    provider, model = snapshot.get("provider"), snapshot.get("model")
    reference = f"{provider}/{model}" if provider and model else model or getattr(agent, "model", None)
    tools = snapshot.get("tools")
    prompt = snapshot.get("prompt")
    messages = (
        [{"role": "user", "blocks": [{"type": "text", "text": prompt}]}]
        if isinstance(prompt, str) and prompt
        else []
    )
    return project_context_preview({
        "mode": "sent_request",
        "actually_sent": True,
        "agent": {
            "name": name,
            "color": color or None,
            "source": "subagent",
            "model": reference,
            "tier": getattr(agent, "tier", None),
        },
        "system_text": snapshot.get("system") if isinstance(snapshot.get("system"), str) else None,
        "redacted_for_display": True,
        "tools": list(tools) if isinstance(tools, (list, tuple)) else [],
        "tools_supported": bool(tools),
        "messages": messages,
        "model": reference,
        "provider": provider if isinstance(provider, str) else None,
    })


__all__ = ["project_agent_context"]
