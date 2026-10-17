"""Pure cross-project session grouping shared by terminal surfaces (PLAN §14.4)."""
from __future__ import annotations
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


def _order_time(summary: Any) -> float:
    """Newest user prompt, else creation, else activity; never agent activity first."""
    return (
        getattr(summary, "last_user_at", 0.0)
        or getattr(summary, "created_at", 0.0)
        or getattr(summary, "last_activity", 0.0)
        or 0.0
    )


def _sort_key(pair: tuple[str, Any]) -> tuple[float, str, str]:
    """Newest first; equal times tie-break by workspace then id (ascending)."""
    workspace, summary = pair
    return (-_order_time(summary), workspace, str(getattr(summary, "id", "")))


def _day_label(ts: float | None, today: date | None = None) -> str:
    if not ts:
        return "Earlier"
    day = datetime.fromtimestamp(ts, tz=UTC).astimezone().date()
    today = today or datetime.now(tz=UTC).astimezone().date()
    if day == today:
        return "Today"
    return day.strftime("%a %b %-d %Y")


def _session_groups(rows: list[tuple[str, Any]], query: str) -> list[tuple[str, list[Any]]]:
    """Group filtered project identities by newest user prompt, then local date."""
    groups: list[tuple[str, list[Any]]] = []
    projects: dict[str, list[Any]] = {}
    for workspace, summary in sorted(rows, key=_sort_key):
        if query and query not in f"{summary.title} {summary.id} {workspace}".casefold():
            continue
        projects.setdefault(workspace, []).append(summary)
    names = [Path(workspace).name for workspace in projects]
    for workspace, summaries in projects.items():
        for summary in summaries:
            day = _day_label(_order_time(summary))
            name = workspace if names.count(Path(workspace).name) > 1 else Path(workspace).name or workspace
            label = f"{name} · {day}" if workspace else day
            if not groups or groups[-1][0] != label:
                groups.append((label, []))
            groups[-1][1].append(summary)
    return groups

