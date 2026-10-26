"""The Manager screen's managed-sessions board (plan 10 §4.1).

Pure projection over data the client already polls: the managed root ids from
``ManagerState`` joined with the session rows the dashboard poll keeps
(``shell.sessions`` and the visible dashboard's rows: title, status glyph,
attention). One row per managed root, newest first, bounded to six rows plus an
overflow count. Click/Enter on a row is the ordinary ``session_open`` switch;
``Ctrl+X .`` returns to the manager.

No new host command: if the join ever needs data the rows do not carry, that is
a host field, never a storage read (AGENTS.md rule 4). Nothing here is
persisted; staleness matches the rows' poll cadence and the board labels it.
"""

from __future__ import annotations

from typing import Any

from ...ui_support.text import escape_controls

#: Rows drawn before `+N more · /sessions` (plan 10 §4.1).
BOARD_LIMIT = 6


def board(shell) -> dict[str, Any] | None:
    """The ``manager_board`` snapshot field, or ``None`` when the manager is not in front.

    ``rows`` hold ``id``/``title``/``status``/``sub``/``age``/``attention`` plus the
    ``session_open`` operation per row and ``collapsed`` for the heading toggle.
    ``total`` counts every managed id, ``updated_at`` the newest row timestamp seen.
    """
    manager = getattr(shell, "manager", None)
    if manager is None or not manager.is_active():
        return None
    state = getattr(manager, "state", None)
    if state is None or not getattr(state, "enabled", False):
        return None
    managed = [
        item
        for item in (getattr(state, "managed", None) or [])
        if isinstance(item, str) and item
    ]
    rows, missing = _join(shell, managed)
    collapsed = bool(getattr(shell, "manager_board_collapsed", False))
    shown = [] if collapsed else rows[:BOARD_LIMIT]
    return {
        "total": len(managed),
        "collapsed": collapsed,
        "updated_at": max([row["updated"] for row in rows] or [0.0]),
        "toggle": {"kind": "manager_board_toggle"},
        "rows": [
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "sub": row["sub"],
                "age": row["age"],
                "attention": row["attention"],
                "operation": {
                    "kind": "session_open",
                    "workspace": row["workspace"],
                    "id": row["id"],
                },
            }
            for row in shown
        ],
        "more": max(0, len(rows) - BOARD_LIMIT) if not collapsed else 0,
        "missing": missing,
    }


def _join(shell, managed: list[str]) -> tuple[list[dict[str, Any]], int]:
    """Managed ids joined with polled session rows, newest first; unknown ids last.

    Returns ``(rows, missing)``: ``missing`` counts managed ids with no polled row yet
    (shown id-only until the poll catches up).
    """
    by_id: dict[str, dict[str, Any]] = {}
    dashboard = getattr(getattr(shell, "workflows", None), "dashboard", None)
    for source in (
        getattr(shell, "sessions", None) or [],
        getattr(dashboard, "rows", None) or [],
    ):
        for row in source:
            if isinstance(row, dict) and row.get("id") and row["id"] not in by_id:
                by_id[row["id"]] = row
    known, unknown = [], []
    for session_id in managed:
        row = by_id.get(session_id)
        (known if row is not None else unknown).append((session_id, row))
    known.sort(
        key=lambda item: float((item[1] or {}).get("updated") or 0.0), reverse=True
    )
    out = []
    for session_id, row in (*known, *[(sid, None) for sid, _ in unknown]):
        if row is None:
            out.append(
                {
                    "id": session_id,
                    "title": session_id,
                    "status": "idle",
                    "sub": "",
                    "age": "",
                    "attention": "",
                    "workspace": getattr(shell, "workspace", ""),
                    "updated": 0.0,
                }
            )
            continue
        pending = row.get("pending") or {}
        attention = "needs you" if pending.get("request_id") else ""
        out.append(
            {
                "id": session_id,
                "title": row.get("title") or session_id,
                "status": row.get("status") or "idle",
                "sub": row.get("sub") or "",
                "age": row.get("age") or "",
                "attention": attention,
                "workspace": row.get("workspace") or getattr(shell, "workspace", ""),
                "updated": float(row.get("updated") or 0.0),
            }
        )
    return (
        [
            {
                **row,
                "title": escape_controls(str(row["title"]))[:80],
                "sub": escape_controls(str(row["sub"]))[:80],
                "age": escape_controls(str(row["age"]))[:24],
            }
            for row in out
        ],
        len(unknown),
    )
