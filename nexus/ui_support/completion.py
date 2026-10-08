"""Toolkit-free composer completion shared by the native shell (PLAN §14.11).

Mirrors what the terminal composer completes: ``/`` completes visible command
names by case-insensitive prefix, ``@`` completes workspace files through the
host (30 results), and ``/model`` and ``/agent`` complete their arguments
(models as ``provider/id``, capped at 100). The native shell adds static
arguments for the commands whose usage lists fixed words.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..client.protocol import ClientError
from ..host import TransportError
from ..ui.cli import commands
from .fuzzy import fuzzy_match

def root_agents(rows):
    """Agent definitions eligible to own a conversation."""
    return [row for row in rows if "root" in row.get("contexts", ())]


FILE_LIMIT = 30
#: Rows the ``/`` and ``@`` menus always show (fewer only when fewer exist), so
#: the popup never changes height while typing.
MENU_LIMIT = 10
ARGUMENT_LIMIT = 100

#: Fixed argument words per command, taken from each ``CommandSpec.usage``.
STATIC_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "/theme": ("dark", "light"),
    "/export": ("json", "markdown", "jsonl"),
    "/voice": ("status", "download", "on", "off"),
    "/speak": ("download",),
    "/attach": ("clear",),
    "/diff": ("--staged",),
}


def _edit_distance(a: str, b: str) -> int:
    """Damerau (adjacent swap) edit distance; catches ``mew`` for ``new``."""
    prev2, prev = [], list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            cost = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cost = min(cost, prev2[j - 2] + 1)
            row.append(cost)
        prev2, prev = prev, row
    return prev[-1]


def rank_menu(query: str, rows: Sequence[tuple[str, Sequence[str]]], limit: int = MENU_LIMIT) -> list[str]:
    """Rank ``(value, match names)`` rows for ``query`` and pad to ``limit``.

    Tiers: prefix, substring/subsequence, typo (edit distance <= 1 or 2 for
    longer words), then the remaining rows in their given order.
    """
    needle = query.casefold()
    scored = []
    for index, (value, names) in enumerate(rows):
        best = 3
        for name in (n.casefold() for n in names):
            if name.startswith(needle):
                tier = 0
            elif needle in name or fuzzy_match(needle, name) is not None:
                tier = 1
            elif needle and _edit_distance(needle, name[: len(needle) + 1]) <= (1 if len(needle) < 5 else 2) or (
                    needle and _edit_distance(needle, name) <= (1 if len(needle) < 5 else 2)):
                tier = 2
            else:
                tier = 3
            best = min(best, tier)
        scored.append((best, index, value))
    scored.sort()
    return [value for _, _, value in scored[:limit]]


def command_matches(token: str) -> list[str]:
    """Ten visible command names: prefix matches first, then near matches."""
    rows = sorted(((spec.name, (spec.name.lstrip("/"), *(a.lstrip("/") for a in spec.aliases)))
                   for spec in commands.SPECS if not spec.hidden))
    return rank_menu(token.lstrip("/"), rows)


def file_menu(query: str, hits: Sequence[str], pool: Sequence[str]) -> list[str]:
    """Exact hits first, then typo/fuzzy matches and padding from ``pool``."""
    seen = set(hits)
    rest = [p for p in pool if p not in seen]
    rows = [(p, (p.rsplit("/", 1)[-1],)) for p in rest]
    return (list(hits) + rank_menu(query, rows, MENU_LIMIT))[:max(MENU_LIMIT, len(hits))] if len(hits) < MENU_LIMIT \
        else list(hits)


def canonical_command(head: str) -> str:
    """Resolve an alias such as ``/reasoning`` to its command name."""
    for spec in commands.SPECS:
        if head in (spec.name, *spec.aliases):
            return spec.name
    return head


def model_refs(rows: Sequence[dict[str, Any]]) -> list[str]:
    return [f"{row['provider']}/{row['id']}" for row in rows if row.get("provider") and row.get("id")]


def filter_prefix(values: Sequence[str], query: str, limit: int = ARGUMENT_LIMIT) -> list[str]:
    needle = query.casefold()
    seen: set[str] = set()
    out = [v for v in values if v.casefold().startswith(needle) and not (v in seen or seen.add(v))]
    return out[:limit]


async def complete(client: Any, prefix: str, query: str, *, efforts: Sequence[str] = ()) -> list[str]:
    """Candidates for the token ``query`` at the end of composer text ``prefix``.

    File completions are returned without the leading ``@`` (the editor
    re-inserts it). Host failures give an empty list, never an error.
    """
    head, _, _ = prefix.partition(" ")
    try:
        if " " in prefix and head.startswith("/"):
            return await _arguments(client, canonical_command(head), query, efforts)
        if query.startswith("/"):
            return command_matches(query)
        if query.startswith("@"):
            return await _files(client, query[1:])
    except (ClientError, TransportError, OSError, ValueError):
        return []
    return []


async def _files(client: Any, query: str) -> list[str]:
    hits = list(await client.search_files(query, limit=FILE_LIMIT))[:FILE_LIMIT]
    if len(hits) >= MENU_LIMIT:
        return hits
    pool = list(await client.search_files("", limit=100))
    return file_menu(query, hits, pool)


async def _arguments(client: Any, name: str, query: str, efforts: Sequence[str]) -> list[str]:
    if name == "/model":
        values = ["list", *model_refs(await client.list_models(selectable_only=True))]
    elif name == "/agent":
        values = ["list", "current", "reset", *[str(r.get("name", "")) for r in root_agents(await client.list_agents())]]
    elif name == "/effort":
        values = ["default", *efforts]
    elif name == "/sessions":
        values = [row.id for row in await client.list_sessions()]
    elif name == "/attach":
        return filter_prefix(["clear"], query) + await _files(client, query)
    else:
        values = list(STATIC_ARGUMENTS.get(name, ()))
    return filter_prefix([v for v in values if v], query)
