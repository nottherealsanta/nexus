"""Toolkit-free model-picker logic shared by the Textual and native shells (PLAN §14.11).

Sorting (updated date or natural name), the six-month freshness filter, fuzzy
ranking over name/ref/provider, and the Favorites, Recent, then
Recently-updated/provider grouping with de-duplication.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import UTC, date, datetime

from .fuzzy import fuzzy_match


def _natural(text: str) -> tuple[tuple[int, str | int], ...]:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", text))


def _date(row: dict, field: str) -> tuple[int, int, int]:
    value = row.get(field)
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return (0, 0, 0)
    return tuple(-int(part) for part in value.split("-"))


def sort_models(rows: list[dict], *, by: str = "updated") -> list[dict]:
    """Order by update/release date (default), or natural alphanumeric name."""
    def key(row: dict):
        name = _natural(str(row.get("name") or row.get("id") or ""))
        ref = _natural(str(row.get("provider") or "") + "/" + str(row.get("id") or ""))
        if by == "name":
            return (name, ref)
        return (
            _date(row, "last_updated") if row.get("last_updated") else _date(row, "release_date"),
            _date(row, "release_date"),
            name, ref,
        )
    return sorted(rows, key=key)


def recent_models(rows: list[dict], *, today: date | None = None) -> list[dict]:
    """Exclude known stale entries; undated models cannot be classified as old."""
    today = today or datetime.now(UTC).date()
    month = today.month - 6
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    cutoff = date(year, month, min(today.day, monthrange(year, month)[1]))
    def fresh(row: dict) -> bool:
        stamp = row.get("last_updated") or row.get("release_date")
        if not isinstance(stamp, str):
            return True
        try:
            return date.fromisoformat(stamp) >= cutoff
        except ValueError:
            return True
    return [row for row in rows if fresh(row)]


def _ref(row: dict) -> str:
    return f"{row['provider']}/{row['id']}"


def rank_models(rows: list[dict], query: str) -> tuple[list[dict], dict[str, tuple[int, ...]]]:
    """Fuzzy matches best first plus name highlight positions keyed by ref."""
    highlights: dict[str, tuple[int, ...]] = {}
    scored = []
    for index, row in enumerate(rows):
        name = str(row.get("name") or row.get("id") or "")
        provider = str(row.get("provider", ""))
        candidates = (name, _ref(row), f"{provider} {name}", f"{name} {provider} {row.get('id', '')}")
        hits = [hit for hit in map(lambda text: fuzzy_match(query, text), candidates) if hit]
        if not hits:
            continue
        best = max(hits, key=lambda hit: hit[0])
        scored.append((-best[0], index, row))
        name_hit = fuzzy_match(query, name)
        if name_hit:
            highlights[_ref(row)] = name_hit[1]
    return [row for _, _, row in sorted(scored, key=lambda item: item[:2])], highlights


def model_groups(rows: list[dict], *, query: str = "", sort_mode: str = "updated",
                 favorites: list[str] = (), recent: list[str] = ()) -> tuple[list[tuple[str, list[dict]]], dict[str, tuple[int, ...]]]:
    """Titled, de-duplicated groups exactly as the Textual picker lists them."""
    query = query.strip()
    highlights: dict[str, tuple[int, ...]] = {}
    if query:
        matching, highlights = rank_models(rows, query)
    else:
        matching = sort_models(list(rows), by=sort_mode)
    by_ref = {_ref(row): row for row in matching}
    groups: list[tuple[str, list[dict]]] = []
    if not query:
        groups.extend((title, [by_ref[ref] for ref in refs if ref in by_ref])
                      for title, refs in (("Favorites", favorites), ("Recent", recent)))
    if query:
        groups.append((f"Best matches · {len(matching)}", matching))
    elif sort_mode == "updated":
        groups.append(("Recently updated", matching))
    else:
        providers = sorted({str(row["provider"]) for row in matching}, key=str.casefold)
        groups.extend((provider, [row for row in matching if row["provider"] == provider]) for provider in providers)
    seen: set[str] = set()
    out: list[tuple[str, list[dict]]] = []
    for title, group in groups:
        unique = [row for row in group if _ref(row) not in seen]
        seen.update(_ref(row) for row in unique)
        if unique:
            out.append((title, unique))
    return out, highlights


def preselected_effort(row: dict, *, current: str, current_effort: str | None, stored_override: str | None) -> str | None:
    """The effort the picker starts on for ``row`` (stored override, else the current one on the same model)."""
    levels = row.get("supported_efforts") or ()
    if stored_override in levels:
        return stored_override
    return current_effort if _ref(row) == current and current_effort in levels else None


def selection_effort(row: dict, *, current: str, current_effort: str | None, stored_override: str | None,
                     effort_source: str | None, pending: str | None, touched: bool) -> tuple[str | None, bool]:
    """(effort, commit) for Enter: commit atomically only if the user chose an effort or an agent effort must be kept."""
    preserve_agent = (_ref(row) == current and effort_source == "agent" and stored_override is None
                      and current_effort in (row.get("supported_efforts") or ()))
    commit = touched or preserve_agent
    return ((pending if touched else current_effort) if commit else None), commit
