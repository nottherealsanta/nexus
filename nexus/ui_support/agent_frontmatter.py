"""Read and rewrite the simple ``key: value`` frontmatter of an agent ``*.md``.

The Settings page edits an agent's model, provider, reasoning effort, and
fallback list as form fields while the prompt stays in the text editor. This is
a deliberately small text transform, not a parser: the host validates the saved
file with the real restricted grammar (``nexus.agents.model``) and rejects
anything invalid, so a bad value surfaces as a save error. Only single-line
``key: value`` entries between the leading ``---`` delimiters are touched; every
other line, including the prompt body, is preserved byte for byte.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The fields the Settings form edits, in the order they are inserted.
FORM_FIELDS = ("model", "provider", "reasoning_effort", "fallback")
_MAX_FALLBACKS = 8


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
    return [item for item in items if item][:_MAX_FALLBACKS]


def set_agent_fields(body: str, updates: Mapping[str, str]) -> str:
    """Return ``body`` with each form field set, or removed when blank.

    ``fallback`` accepts a comma-separated list and is written as a flow list.
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
        if key == "fallback" and value:
            value = "[" + ", ".join(fallback_items(value)) + "]"
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


__all__ = ["FORM_FIELDS", "agent_fields", "fallback_items", "set_agent_fields"]
