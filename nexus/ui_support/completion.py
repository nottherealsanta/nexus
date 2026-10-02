"""Toolkit-free composer completion shared by the native shell (PLAN §14.11).

Mirrors what the Textual composer completes: ``/`` completes visible command
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

def root_agents(rows):
    """Agent definitions eligible to own a conversation."""
    return [row for row in rows if "root" in row.get("contexts", ())]


FILE_LIMIT = 30
ARGUMENT_LIMIT = 100

#: Fixed argument words per command, taken from each ``CommandSpec.usage``.
STATIC_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "/theme": ("dark", "light"),
    "/export": ("json", "markdown", "jsonl"),
    "/voice": ("status", "download", "on", "off"),
    "/attach": ("clear",),
    "/diff": ("--staged",),
}


def command_matches(token: str) -> list[str]:
    """Visible command names whose name or alias starts with ``token`` (sorted, like Textual)."""
    needle = token.casefold()
    return sorted(spec.name for spec in commands.SPECS
                  if not spec.hidden and any(n.casefold().startswith(needle) for n in (spec.name, *spec.aliases)))


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
            return list(await client.search_files(query[1:], limit=FILE_LIMIT))[:FILE_LIMIT]
    except (ClientError, TransportError, OSError, ValueError):
        return []
    return []


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
        return filter_prefix(["clear"], query) + list(await client.search_files(query, limit=FILE_LIMIT))[:FILE_LIMIT]
    else:
        values = list(STATIC_ARGUMENTS.get(name, ()))
    return filter_prefix([v for v in values if v], query)
