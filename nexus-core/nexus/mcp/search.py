"""Keyword search over the tools of connected MCP servers.

The model finds tools with `mcp_search` and calls them with `mcp_call`, so full
schemas stay out of every request until one is wanted. Ranking weighs the tool
name, its argument names and its description by how rare each word is.

Adapted from old Nexus `nexus/mcp/search.py`.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

MAX_RESULTS = 10


def tokens(text: str) -> frozenset[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


@dataclass(frozen=True, slots=True)
class Entry:
    server: str
    tool: Any  # mcp_types.Tool

    @property
    def name(self) -> str:
        return f"{self.server}/{self.tool.name}"


@dataclass(frozen=True, slots=True)
class Result:
    matches: tuple[Entry, ...]
    total: int
    errors: tuple[str, ...] = ()


class Index:
    def __init__(self, tools: dict[str, list[Any]]):
        self.entries = tuple(Entry(server, tool) for server in sorted(tools)
                             for tool in sorted(tools[server], key=lambda t: t.name))
        self.fields = tuple((tokens(e.tool.name), tokens(" ".join((e.tool.input_schema or {}).get("properties", {}))),
                             tokens(e.tool.description or "")) for e in self.entries)
        counts = Counter(word for fields in self.fields for word in set().union(*fields))
        self.idf = {word: 1 + math.log((1 + len(self.entries)) / (1 + n)) for word, n in counts.items()}

    def search(self, query: str, *, server: str | None = None, limit: int = 5) -> Result:
        pool = [(e, f) for e, f in zip(self.entries, self.fields, strict=True) if server in (None, e.server)]
        if query.startswith("select:"):
            errors: list[str] = []
            found: dict[str, Entry] = {}
            for wanted in (w.strip() for w in query[7:].split(",")):
                hits = [e for e, _ in pool if wanted in (e.name, e.tool.name)]
                for hit in hits:
                    found[hit.name] = hit
                if not hits:
                    close = difflib.get_close_matches(wanted, [e.name if "/" in wanted else e.tool.name for e, _ in pool], n=3)
                    errors.append(f"not found: {wanted}" + (f" (closest: {', '.join(close)})" if close else ""))
            return Result(tuple(found.values()), len(pool), tuple(errors))
        words = tokens(query)
        ranked: list[tuple[float, Entry]] = []
        for entry, fields in pool:
            score = 10_000.0 if query.casefold() in (entry.name.casefold(), entry.tool.name.casefold()) else 0.0
            for word in words:
                score += self.idf.get(word, 1) * sum(w for f, w in zip(fields, (12, 5, 1), strict=True) if word in f)
                score += 0.5 * any(p.startswith(word) for p in fields[0])
            if score:
                ranked.append((score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return Result(tuple(e for _, e in ranked[: min(MAX_RESULTS, max(1, limit))]), len(pool))
