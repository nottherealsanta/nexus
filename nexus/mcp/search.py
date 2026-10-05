"""Pure, bounded MCP catalogue search (MCP_SEARCH_PLAN section 5).

Indexes use immutable registered descriptors; no transport or model calls.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

MAX_SERVERS = 64
MAX_TOOLS = 2000


def tokens(text: str) -> frozenset[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


@dataclass(frozen=True)
class Match:
    server: str
    tool: Any

    @property
    def local_name(self) -> str:
        return self.tool.local_name or self.tool.name.split("__", 2)[-1]

    @property
    def name(self) -> str:
        return f"{self.server}/{self.local_name}"


@dataclass(frozen=True)
class SearchResult:
    matches: tuple[Match, ...]
    total: int
    errors: tuple[str, ...] = ()


class SearchIndex:
    """Stable ordering and IDF weights over at most 64 × 2000 tools."""

    def __init__(self, servers: dict[str, Any]):
        self.matches = tuple(Match(name, tool)
            for name in sorted(servers)[:MAX_SERVERS]
            for tool in sorted(servers[name].tools, key=lambda item: item.name)[:MAX_TOOLS])
        self.errors = tuple(f"server {name}: +{len(server.tools)-MAX_TOOLS} tools omitted (index limit)"
            for name, server in sorted(servers.items())[:MAX_SERVERS] if len(server.tools) > MAX_TOOLS)
        if len(servers) > MAX_SERVERS:
            self.errors += (f"+{len(servers)-MAX_SERVERS} servers omitted (index limit)",)
        self.fields = tuple((tokens(match.local_name),
            tokens(" ".join(match.tool.spec.input_schema.get("properties", {}))),
            tokens(match.tool.spec.description)) for match in self.matches)
        counts = Counter(token for fields in self.fields for token in set().union(*fields))
        self.idf = {token: 1 + math.log((1+len(self.matches))/(1+count)) for token, count in counts.items()}

    def search(self, query: str, *, server: str | None = None, limit: int = 5) -> SearchResult:
        candidates = [(match, fields) for match, fields in zip(self.matches, self.fields)
                      if server is None or match.server == server]
        errors = list(self.errors)
        if query.startswith("select:"):
            found = []
            names = query[7:].split(",")
            for name in names:
                name = name.strip()
                matches = [match for match, _ in candidates if name in (match.name, match.local_name, match.tool.name)]
                if matches:
                    found.extend(matches)
                else:
                    closest = difflib.get_close_matches(name, [match.name if "/" in name else match.local_name for match, _ in candidates], n=3)
                    errors.append(f"not found: {name}" + (f" (closest: {', '.join(closest)})" if closest else ""))
            # Exact selection never silently drops names due to a keyword limit.
            return SearchResult(tuple({match.name: match for match in found}.values()), len(candidates), tuple(errors))
        words = tokens(query)
        ranked = []
        for match, fields in candidates:
            score = 10000 if query.casefold() in (match.name.casefold(), match.local_name.casefold()) else 0
            for word in words:
                score += self.idf.get(word, 1) * sum(weight for field, weight in zip(fields, (12, 5, 1)) if word in field)
                score += 0.5 * any(part.startswith(word) for part in fields[0])
            if score:
                ranked.append((score, match))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return SearchResult(tuple(match for _, match in ranked[:min(10, max(1, limit))]), len(candidates), tuple(errors))
