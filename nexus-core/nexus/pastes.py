"""Collapsed text pastes: validate them and place them among a prompt's text parts.

A client sends the display text with one unique marker per paste (for example
``[Pasted ~589 tokens]``) plus the full content behind each marker. The model
receives the content as an ordinary text part; ``collapsed`` only tells clients
to show the marker instead. Nothing is silently dropped: every paste must match
a marker in the text.
"""
from __future__ import annotations

from typing import Any

from .llmhub.types import Text

MAX_PASTES = 8
MAX_PASTE_CHARS = 1_000_000
MAX_TOTAL_CHARS = 4_000_000


def paste_parts(text: str, pastes: Any) -> tuple[Text, ...]:
    """The prompt's text split into ordinary and collapsed parts, in marker order."""
    if pastes is None:
        return (Text(text),)
    if not isinstance(pastes, list) or len(pastes) > MAX_PASTES:
        raise ValueError("pastes must be a list of at most eight pastes")
    entries: list[tuple[str, str]] = []
    total = 0
    for item in pastes:
        if not isinstance(item, dict):
            raise ValueError("each paste must have a marker and text")  # noqa: TRY004
        marker, content = item.get("marker"), item.get("text")
        if not isinstance(marker, str) or not marker or len(marker) > 128:
            raise ValueError("paste marker must be 1-128 characters")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("paste text must not be empty")
        if len(content) > MAX_PASTE_CHARS:
            raise ValueError("paste text exceeds 1,000,000 characters")
        if any(existing == marker for existing, _ in entries):
            raise ValueError("paste markers must be unique")
        total += len(content)
        entries.append((marker, content))
    if total > MAX_TOTAL_CHARS:
        raise ValueError("pastes exceed 4,000,000 characters in total")
    if not entries:
        return (Text(text),)

    parts: list[Text] = []
    pos = 0
    unused = list(entries)
    while pos < len(text) and unused:
        found = None
        for index, (marker, _) in enumerate(unused):
            at = text.find(marker, pos)
            if at >= 0 and (found is None or at < found[0]):
                found = (at, index)
        if found is None:
            break
        at, index = found
        if at > pos:
            parts.append(Text(text[pos:at]))
        marker, content = unused.pop(index)
        parts.append(Text(content, collapsed=True))
        pos = at + len(marker)
    if pos < len(text):
        parts.append(Text(text[pos:]))
    if unused:
        raise ValueError("paste marker not found in text")
    return tuple(parts) if parts else (Text(text),)
