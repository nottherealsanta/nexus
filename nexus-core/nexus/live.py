"""Checkpointed diagnostic output of an unfinished request; never model context."""
from __future__ import annotations

import copy
from typing import Any

EPHEMERAL = frozenset({"text_delta", "reasoning_delta", "tool_call_started", "tool_args_delta"})
PART_EVENTS = EPHEMERAL | {"part_done"}


class LivePartial:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.seq = 0
        self.parts: list[dict[str, Any]] = []
        self.reasoning_seconds: float | None = None
        self.counts: dict[str, dict[str, int]] = {}

    def apply(self, data: dict[str, Any]) -> dict[str, Any]:
        """Fold one normalized event and attach its request/part identity."""
        kind = data["type"]
        self.seq += 1
        count = self.counts.setdefault(kind, {"count": 0, "chars": 0, "arg_chars": 0})
        count["count"] += 1
        count["chars"] += len(data.get("text", ""))
        count["arg_chars"] += len(data.get("delta", ""))
        if kind == "part_done":
            part = data["part"]
            i = next((i for i, p in enumerate(self.parts) if not p.get("done") and p["type"] == part["type"]
                      and (part["type"] != "tool_call" or p.get("id") == part["id"])), len(self.parts))
            final = {**part, "done": True}
            if i == len(self.parts):
                self.parts.append(final)
            else:
                self.parts[i] = final
        elif kind in ("text_delta", "reasoning_delta"):
            type_ = "text" if kind == "text_delta" else "reasoning"
            field = "text" if type_ == "text" else "summary"
            i = len(self.parts) - 1
            if i < 0 or self.parts[i]["type"] != type_ or self.parts[i].get("done"):
                i = len(self.parts)
                self.parts.append({"type": type_, field: "", "done": False})
            self.parts[i][field] += data["text"]
        else:
            ident = data["id"]
            i = next((i for i, p in enumerate(self.parts) if p["type"] == "tool_call" and p["id"] == ident), len(self.parts))
            if i == len(self.parts):
                self.parts.append({"type": "tool_call", "id": ident, "name": data.get("name", ""),
                                   "arguments": "", "done": False})
            if kind == "tool_args_delta":
                self.parts[i]["arguments"] += data["delta"]
        return {**data, "request_id": self.request_id, "seq": self.seq, "part_index": i}

    def json(self) -> dict[str, Any]:
        return copy.deepcopy({"request_id": self.request_id, "seq": self.seq, "parts": self.parts,
                              "reasoning_seconds": self.reasoning_seconds, "counts": self.counts,
                              "diagnostic_only": True})


def partial_summary(partial: dict[str, Any]) -> dict[str, Any]:
    parts: dict[str, int] = {}
    calls = []
    for p in partial.get("parts", []):
        if p.get("done"):
            parts[p["type"]] = parts.get(p["type"], 0) + 1
        if p["type"] == "tool_call":
            calls.append({"id": p["id"], "name": p["name"]})
    return {"counts": partial.get("counts", {}), "parts": parts, "tool_calls": calls[:50]}
