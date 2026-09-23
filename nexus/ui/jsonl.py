"""JSONL passthrough: every event envelope, unattended (PLAN section 14.3).

``--json`` writes the full :class:`~nexus.events.Event` envelope for every event
in order and renders no human text. It never prompts for an approval: the
session stays unattended and the daemon's ``permissions.on_unattended`` policy
applies. That makes the mode safe for scripts and CI, where there is no one to
answer a prompt.

The writer is intentionally tiny and depends on nothing in the interactive
surface, so it can be imported and used on its own.
"""
from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from ..events import Event

#: Event types that terminate a turn, mirrored from the renderer so this module
#: never imports the client package (which imports it back).
TERMINAL_EVENTS = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})

__all__ = ["TERMINAL_EVENTS", "JsonlWriter", "exit_code", "render"]


class JsonlWriter:
    """Write one JSON object per line, flushing so a follower sees it live."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout

    def write(self, event: Event) -> None:
        self.write_dict(event.to_dict())

    def write_dict(self, payload: dict[str, Any]) -> None:
        self.stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.stream.flush()


def render(event: Event, stream: TextIO | None = None) -> None:
    """Write one event envelope as a JSON line."""
    JsonlWriter(stream).write(event)


def exit_code(terminal: Event | None) -> int:
    """Map a terminal event to a process exit code."""
    if terminal is None:
        return 1
    if terminal.type == "turn.failed":
        return 1
    if terminal.type == "turn.cancelled":
        return 130
    return 0
