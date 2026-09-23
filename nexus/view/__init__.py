"""The pure view layer (PLAN section 14.7).

``view/`` reduces a session's append-only event stream into one renderable
:class:`ConversationView`, importing nothing but ``nexus.events`` and the
standard library::

    from nexus.view import apply, fold

    view = fold(session.events)          # replay a log
    view = apply(view, next_event)       # incremental
    payload = view.to_dict()             # JSON-safe snapshot
"""
from __future__ import annotations

from .fold import accumulate, finalize_text, finalize_thinking, fold, fold_into
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
from .reduce import apply, apply_many, initial_state

__all__ = [
    "AgentView", "BlockView", "ContextView", "ConversationView", "DiagnosticView",
    "ErrorView", "ExtensionView", "HookView", "McpView", "MessageView",
    "PermissionView", "PresenceView", "QueuedInputView", "RegistryView",
    "RetryView", "SkillView", "ToolCallView", "TurnView", "UsageTotals",
    "accumulate", "apply", "apply_many", "finalize_text", "finalize_thinking",
    "fold", "fold_into", "initial_state", "jsonable",
]
