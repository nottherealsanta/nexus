"""Context management (plan section 5.2).

Phase 1 introduces :class:`~nexus.context.manager.ContextManager`, the new
assembler the Nexus-owned loop drives.

This package replaces the former top-level ``nexus/context.py`` module. The
legacy deterministic builder (``Exchange``/``Context``/``build_context``) lived
there and is still imported by the pre-Phase-1 ``Agent`` and ``SessionStore``
paths, so it is preserved here **verbatim** and re-exported. A package shadows a
same-named module in Python, which is why the code moved rather than both
existing; behaviour is unchanged and the legacy symbols keep working until the
legacy adapter retires at Phase 7.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Phase 1/3 manager symbols are resolved lazily so that legacy callers
#: importing only ``Exchange``/``Context``/``build_context`` do not pay for
#: loading the config/model modules the manager depends on. This mirrors PEP 562
#: on the root package. ``__all__`` still advertises them.
_LAZY = {
    "ContextManager": (".manager", "ContextManager"),
    "DEFAULT_MAX_FILE_BYTES": (".manager", "DEFAULT_MAX_FILE_BYTES"),
    "IDENTITY_PREAMBLE": (".manager", "IDENTITY_PREAMBLE"),
    "AssemblyEnvironment": (".manager", "AssemblyEnvironment"),
    "AssemblyContext": (".parts", "AssemblyContext"),
    "ContextPart": (".parts", "ContextPart"),
    "EnvironmentInfo": (".parts", "EnvironmentInfo"),
    "PART_ORDER": (".parts", "PART_ORDER"),
    "PART_PRIORITY": (".parts", "PART_PRIORITY"),
    "PartOutput": (".parts", "PartOutput"),
    "builtin_parts": (".parts", "builtin_parts"),
    "capture_environment": (".parts", "capture_environment"),
    "Allocation": (".budget", "Allocation"),
    "BudgetInputs": (".budget", "BudgetInputs"),
    "BudgetPlan": (".budget", "BudgetPlan"),
    "ContextOverflow": (".budget", "ContextOverflow"),
    "PartRequest": (".budget", "PartRequest"),
    "allocate": (".budget", "allocate"),
    "compute_input_budget": (".budget", "compute_input_budget"),
    "CompactionAction": (".compact", "CompactionAction"),
    "CompactionResult": (".compact", "CompactionResult"),
    "MappingNoteResolver": (".compact", "MappingNoteResolver"),
    "NoteResolver": (".compact", "NoteResolver"),
    "Summarizer": (".compact", "Summarizer"),
    "SummaryArtifact": (".compact", "SummaryArtifact"),
    "compact": (".compact", "compact"),
    "drop_oldest": (".compact", "drop_oldest"),
    "evict_tool_results": (".compact", "evict_tool_results"),
    "hybrid": (".compact", "hybrid"),
    "summarize": (".compact", "summarize"),
    "CacheBoundary": (".cache", "CacheBoundary"),
    "TokenCountCache": (".cache", "TokenCountCache"),
    "prompt_cache_boundaries": (".cache", "prompt_cache_boundaries"),
    "semantic_key": (".cache", "semantic_key"),
    "RequestTokenCounter": (".counting", "RequestTokenCounter"),
    "request_semantic_key": (".counting", "request_semantic_key"),
}

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "IDENTITY_PREAMBLE",
    "PART_ORDER",
    "PART_PRIORITY",
    "Allocation",
    "AssemblyContext",
    "AssemblyEnvironment",
    "BudgetInputs",
    "BudgetPlan",
    "CacheBoundary",
    "CompactionAction",
    "CompactionResult",
    "Context",
    "ContextManager",
    "ContextOverflow",
    "ContextPart",
    "EnvironmentInfo",
    "Exchange",
    "MappingNoteResolver",
    "NoteResolver",
    "PartOutput",
    "PartRequest",
    "RequestTokenCounter",
    "Summarizer",
    "SummaryArtifact",
    "TokenCountCache",
    "allocate",
    "build_context",
    "builtin_parts",
    "capture_environment",
    "compact",
    "compute_input_budget",
    "drop_oldest",
    "evict_tool_results",
    "hybrid",
    "prompt_cache_boundaries",
    "request_semantic_key",
    "semantic_key",
    "summarize",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


# ---------------------------------------------------------------------------
# Legacy deterministic context (moved verbatim from nexus/context.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exchange:
    user: str
    assistant: str


@dataclass(frozen=True)
class Context:
    prompt: str
    omitted_exchanges: int


def build_context(instructions: str, memory: str, history: list[Exchange], user: str, limit: int) -> Context:
    if not isinstance(user, str) or not user.strip():
        raise ValueError("Message must be a nonempty string")

    def render(exchanges: list[Exchange], omitted: int) -> str:
        # JSON preserves roles and boundaries even when content contains delimiters.
        return json.dumps({
            "instructions": instructions,
            "memory": memory,
            "omitted_exchanges": omitted,
            "history": [{"user": e.user, "assistant": e.assistant} for e in exchanges],
            "user": user,
        }, ensure_ascii=False)

    selected: list[Exchange] = []
    prompt = render(selected, len(history))
    if len(prompt) > limit:
        raise ValueError("Instructions, memory and new message exceed context_chars; shorten them or raise the limit")
    for exchange in reversed(history):
        candidate = [exchange, *selected]
        candidate_prompt = render(candidate, len(history) - len(candidate))
        if len(candidate_prompt) > limit:
            break
        selected, prompt = candidate, candidate_prompt
    return Context(prompt, len(history) - len(selected))
