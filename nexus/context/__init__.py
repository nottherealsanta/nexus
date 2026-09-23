"""Context management (plan section 5.2).

:class:`~nexus.context.manager.ContextManager` is the assembler the Nexus-owned
loop drives: composable parts, priority budgets, token-aware counting,
cache-aware breakpoints, and explicit compaction. The package re-exports the
manager contracts lazily so importing a single symbol does not pull the whole
manager graph.
"""
from __future__ import annotations

from typing import Any

#: Manager symbols are resolved lazily so importing a single symbol (for
#: example ``ContextManager``) does not pull in the config/model modules the
#: whole manager graph depends on. This mirrors PEP 562 on the root package;
#: ``__all__`` still advertises every name.
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
    "ContextManager",
    "ContextOverflow",
    "ContextPart",
    "EnvironmentInfo",
    "MappingNoteResolver",
    "NoteResolver",
    "PartOutput",
    "PartRequest",
    "RequestTokenCounter",
    "Summarizer",
    "SummaryArtifact",
    "TokenCountCache",
    "allocate",
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
