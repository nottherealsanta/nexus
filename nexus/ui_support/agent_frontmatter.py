"""Read and rewrite the simple ``key: value`` frontmatter of an agent ``*.md``.

The Settings page edits an agent's model, provider, reasoning effort,
fallback list and allowed tiers as form fields while the prompt stays in the text editor. This is
a deliberately small text transform, not a parser: the host validates the saved
file with the real restricted grammar (``nexus.agents.model``) and rejects
anything invalid, so a bad value surfaces as a save error. Only single-line
``key: value`` entries between the leading ``---`` delimiters are touched; every
other line, including the prompt body, is preserved byte for byte.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The fields the Settings form edits, in the order they are inserted.
FORM_FIELDS = ("model", "provider", "reasoning_effort", "fallback", "tiers")
MAX_FALLBACKS = 8
MAX_TIERS = 8
_LIST_FORM_FIELDS = ("fallback", "tiers")


def _bounds(lines: list[str]) -> tuple[int, int] | None:
    if not lines or lines[0].rstrip("\r\n").lstrip("﻿") != "---":
        return None
    for index in range(1, min(len(lines), 400)):
        if lines[index].rstrip("\r\n") == "---":
            return 1, index
    return None


def agent_fields(body: str) -> dict[str, str]:
    """Return the frontmatter ``key -> raw value`` map (empty when absent)."""
    lines = body.splitlines(keepends=True)
    bounds = _bounds(lines)
    if bounds is None:
        return {}
    fields: dict[str, str] = {}
    for line in lines[bounds[0]:bounds[1]]:
        key, sep, value = line.rstrip("\r\n").partition(":")
        if sep and key and key == key.strip() and key not in fields:
            fields[key] = value.strip()
    return fields


def fallback_items(value: str) -> list[str]:
    """Split a ``[a, b]`` or ``a, b`` fallback value into bounded references."""
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items = [item.strip() for item in inner.replace("\n", ",").split(",")]
    return [item for item in items if item][:MAX_FALLBACKS]


def tier_items(value: str) -> list[str]:
    """Split a ``[low, medium]`` tiers value into bounded names, order kept."""
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items = [item.strip() for item in inner.split(",")]
    return list(dict.fromkeys(item for item in items if item))[:MAX_TIERS]


def set_agent_fields(body: str, updates: Mapping[str, str]) -> str:
    """Return ``body`` with each form field set, or removed when blank.

    ``fallback`` and ``tiers`` accept a comma-separated list and are written as
    a flow list.
    A body without frontmatter is returned unchanged.
    """
    lines = body.splitlines(keepends=True)
    bounds = _bounds(lines)
    if bounds is None:
        return body
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    start, end = bounds
    head = lines[start:end]
    for key, raw in updates.items():
        if key not in FORM_FIELDS:
            continue
        value = " ".join(str(raw).split())
        if key in _LIST_FORM_FIELDS and value:
            items = fallback_items(value) if key == "fallback" else tier_items(value)
            value = "[" + ", ".join(items) + "]"
            if value == "[]":
                value = ""
        index = next(
            (i for i, line in enumerate(head) if line.partition(":")[0] == key),
            None,
        )
        if not value:
            if index is not None:
                del head[index]
            continue
        line = f"{key}: {value}{newline}"
        if index is not None:
            head[index] = line
        else:
            head.append(line)
    return "".join([*lines[:start], *head, *lines[end:]])


#: Run modes: a root agent inherits the session model or pins a model; a
#: subagent pins a model or picks from tiers.
RUN_MODES = ("session", "model", "tier")


def run_mode(body: str) -> str:
    """The mode a file is in: ``model`` wins when a file lists both a model and tiers."""
    fields = agent_fields(body)
    if fields.get("model"):
        return "model"
    if tier_items(fields.get("tiers", "")):
        return "tier"
    return "model" if fields.get("fallback") else "session"


def set_run_mode(body: str, mode: str, items: Mapping[str, str] | None = None) -> str:
    """Switch to ``mode`` and remove the fields that belong to the other modes.

    ``tier`` writes ``tiers`` and removes ``model`` and ``fallback``; ``model``
    writes ``model``/``fallback`` and removes ``tiers``; ``session`` removes all
    three. ``items`` supplies the values to write for the chosen mode.
    """
    if mode not in RUN_MODES:
        raise ValueError(f"unknown run mode {mode!r}")
    items = dict(items or {})
    updates = {"model": "", "fallback": "", "tiers": ""}
    if mode == "tier":
        updates["tiers"] = items.get("tiers", "")
    elif mode == "model":
        updates["model"] = items.get("model", "")
        updates["fallback"] = items.get("fallback", "")
    return set_agent_fields(body, updates)


__all__ = [
    "FORM_FIELDS", "MAX_FALLBACKS", "MAX_TIERS", "RUN_MODES", "agent_fields", "fallback_items", "run_mode",
    "set_agent_fields", "set_run_mode", "tier_items",
]
