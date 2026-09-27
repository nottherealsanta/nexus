"""Exact, one-way compatibility mapping for public tool names.

Only names written by historical Nexus releases are translated. This is not a
case-folding dispatcher: unknown spellings stay unknown and fail closed.
"""
from __future__ import annotations

from collections.abc import Iterable

# BashOutput and KillShell intentionally have no dispatch target. Their
# permission rules are handled as narrow action-qualified matches in
# ``permissions.py``; the legacy calls themselves still require opt-in.
LEGACY_TOOL_NAMES = {
    "Bash": "bash",
    "Read": "read",
    "Glob": "glob",
    "Grep": "grep",
    "Edit": "edit",
    "Write": "write",
    "Task": "subagent",
    "TodoWrite": "todowrite",
    "Skill": "skill",
    "ReloadExtensions": "ReloadExtensions",
    "ListExtensions": "ListExtensions",
    "LS": "ls",
    "MultiEdit": "multiedit",
    "WriteTool": "WriteTool",
}
UNIFIED_BASH_ALIASES = frozenset({"BashOutput", "KillShell"})

# Historical job permissions translate only to these unified ``bash`` actions.
# A permission rule is not a tool-name alias, so wildcard/case variants remain
# untouched and legacy calls still dispatch only when separately opted in.
LEGACY_BASH_PERMISSION_ACTIONS = {
    "BashOutput": ("status", "wait"),
    "KillShell": ("stop",),
}

# Before the built-in bundles were split, these spellings covered both their
# current and legacy tools. A stored/new ``Bundle:shell`` or ``Bundle:fs`` is
# indistinguishable from a historical rule, so permission matching keeps that
# bounded union. Explicit legacy bundle names remain exact.
PERMISSION_BUNDLE_UNIONS = {
    "shell": ("shell", "legacy_shell"),
    "fs": ("fs", "legacy_fs"),
}


def canonical_tool_name(name: str) -> str:
    """Translate an exact historical public name, otherwise return unchanged."""
    return LEGACY_TOOL_NAMES.get(name, name)


def canonical_tool_names(names: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Canonicalize declarations and return bounded old/new collision notices."""
    ordered: list[str] = []
    origins: dict[str, str] = {}
    collisions: list[str] = []
    for raw in names:
        name = canonical_tool_name(raw)
        previous = origins.get(name)
        if previous is not None:
            if previous != raw and name not in collisions:
                collisions.append(name)
            continue
        origins[name] = raw
        ordered.append(name)
    notices = tuple(collisions[:5])
    return tuple(ordered), notices


def permission_bundle_matches(rule_bundle: str, actual_bundle: str | None) -> bool:
    """Match an exact bundle, including the two bounded historical unions."""
    return actual_bundle in PERMISSION_BUNDLE_UNIONS.get(
        rule_bundle, (rule_bundle,)
    )


def canonical_permission_rule(raw: str) -> str:
    """Translate only an exact legacy rule head; leave patterns untouched."""
    if raw.startswith("Bundle:"):
        return raw
    head, separator, tail = raw.partition("(")
    if separator:
        # Wildcard heads are never aliases. The argument/pattern is opaque and
        # remains byte-for-byte identical (including role:tier Task keys).
        if any(char in head for char in "*?[]"):
            return raw
        canonical = canonical_tool_name(head)
        return f"{canonical}({tail}" if canonical != head else raw
    if any(char in raw for char in "*?[]"):
        return raw
    return canonical_tool_name(raw)


__all__ = [
    "LEGACY_BASH_PERMISSION_ACTIONS",
    "LEGACY_TOOL_NAMES",
    "PERMISSION_BUNDLE_UNIONS",
    "UNIFIED_BASH_ALIASES",
    "canonical_permission_rule",
    "canonical_tool_name",
    "canonical_tool_names",
    "permission_bundle_matches",
]
