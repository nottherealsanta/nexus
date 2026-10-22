"""Pure presentation data for the native left sidebar (Projects, Sessions, Files, Git).

Toolkit-free and host-free: functions take already-fetched host results (session rows,
``DirectoryListResult``, ``GitStatusResult``) and return control-safe, bounded dicts the
Ratatui client draws. The terminal never reads the filesystem or runs Git; it shows what
these functions were given. Constants mirror ``rust/tui/src/sidebar.rs`` (pinned by tests).
"""
from __future__ import annotations

from typing import Any

from .manager import MANAGER_SESSION_ID
from .text import escape_controls

TABS = ("Projects", "Sessions", "Files", "Git")
DEFAULT_TAB = "Sessions"
MIN_WIDTH = 24
MAX_WIDTH = 60
DEFAULT_WIDTH = 34

#: Most project rows sent to the client (the host list is bounded too).
MAX_PROJECT_ROWS = 400
MAX_FILE_ROWS = 500
MAX_GIT_ROWS = 500

GIT_GROUPS = (
    ("conflicts", "Conflicts"),
    ("staged", "Staged changes"),
    ("unstaged", "Changes"),
    ("untracked", "Untracked"),
)

_RANK = {"input": 0, "working": 1}


def clamp_width(value: object) -> int:
    """A stored width bounded to ``MIN_WIDTH``..``MAX_WIDTH``; anything unusable is the default."""
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_WIDTH
    return max(MIN_WIDTH, min(MAX_WIDTH, value))


def normalize_tab(value: object) -> str:
    return value if isinstance(value, str) and value in TABS else DEFAULT_TAB


def is_manager_row(row: dict, manager_session: str = "") -> bool:
    """The workspace manager session, by its identity (fixed id, ``kind`` or the host-reported id)."""
    identity = row.get("id")
    if row.get("kind") == "manager":
        return True
    return bool(identity) and identity in {MANAGER_SESSION_ID, manager_session or MANAGER_SESSION_ID}


def is_child_row(row: dict) -> bool:
    """A subagent session (its own ``agents`` namespace or a non-root ``kind``); those live under their parent."""
    kind = row.get("kind")
    return row.get("namespace", "main") != "main" or (bool(kind) and kind not in {"root", "manager"})


def visible_session_rows(rows: list[dict], manager_session: str = "") -> list[dict]:
    """Rows the sidebar lists: root sessions only, so neither the manager (it has its own
    top-bar entry) nor subagents."""
    return [row for row in rows if not is_manager_row(row, manager_session) and not is_child_row(row)]


def sessions_payload(dashboard: dict | None, manager_session: str = "") -> dict | None:
    """The Sessions tab: the dashboard snapshot without the manager session or its pinned entry."""
    if dashboard is None:
        return None
    payload = {key: value for key, value in dashboard.items() if key != "manager"}
    payload["rows"] = visible_session_rows(list(dashboard.get("rows") or []), manager_session)
    return payload


def _text(value: Any, limit: int = 200) -> str:
    return escape_controls(" ".join(str(value or "").split()))[:limit]


def clean(value: Any, limit: int = 300) -> str:
    """One control-safe, single-line, length-capped string (error texts and labels)."""
    return _text(value, limit)


def _base_name(path: str) -> str:
    stripped = path.rstrip("/")
    return stripped.rsplit("/", 1)[-1] or stripped or path


def project_rows(rows: list[dict], current_workspace: str = "", manager_session: str = "",
                 registered: list | None = None) -> list[dict]:
    """Group session rows into main repositories and their linked worktrees.

    Rows come from ``session_rows`` (``repo`` is the main checkout of a worktree, ``worktree``
    its branch name, ``workspace`` the checkout the session lives in). The result is a flat,
    ordered list: a ``project`` row per repository (current project first, then newest activity)
    followed, when it has worktrees, by one ``checkout`` row per checkout (main first). A row
    that can be opened carries ``workspace`` and the newest ``session`` there.
    """
    projects: dict[str, dict[str, Any]] = {}
    for row in visible_session_rows(rows, manager_session):
        if row.get("archived") or not row.get("id"):
            continue
        repo = str(row.get("repo") or row.get("workspace") or "")
        workspace = str(row.get("workspace") or repo)
        if not repo:
            continue
        project = projects.setdefault(repo, {"repo": repo, "checkouts": {}, "updated": 0.0})
        checkout = project["checkouts"].setdefault(workspace, {
            "workspace": workspace, "worktree": str(row.get("worktree") or ""), "count": 0,
            "updated": 0.0, "session": "", "age": "", "rank": 2,
        })
        updated = float(row.get("updated") or 0.0)
        checkout["count"] += 1
        checkout["rank"] = min(checkout["rank"], _RANK.get(str(row.get("status")), 2))
        if not checkout["session"] or updated > checkout["updated"]:
            checkout["updated"], checkout["session"] = updated, str(row["id"])
            checkout["age"] = str(row.get("age") or "")
        project["updated"] = max(project["updated"], updated)

    # Registered projects stay listed without an eligible session (all archived, or none yet);
    # such a checkout has no session to open. A registered-only root that no longer exists on
    # disk is omitted (the current workspace always stays); the entry's activity orders the project.
    for entry in registered or []:
        workspace = str(getattr(entry, "workspace", "") or "")
        repo = str(getattr(entry, "repo", "") or workspace)
        if not workspace:
            continue
        if not getattr(entry, "exists", True) and workspace != current_workspace:
            continue
        project = projects.setdefault(repo, {"repo": repo, "checkouts": {}, "updated": 0.0})
        project["updated"] = max(project["updated"], float(getattr(entry, "updated", 0.0) or 0.0))
        project["checkouts"].setdefault(workspace, {
            "workspace": workspace, "worktree": str(getattr(entry, "worktree", "") or ""), "count": 0,
            "updated": 0.0, "session": "", "age": "", "rank": 2,
        })

    def newest(project: dict) -> tuple[int, float]:
        here = current_workspace and (project["repo"] == current_workspace or current_workspace in project["checkouts"])
        return (0 if here else 1, -project["updated"])

    out: list[dict] = []
    for project in sorted(projects.values(), key=newest):
        checkouts = sorted(project["checkouts"].values(), key=lambda c: (c["worktree"] != "", -c["updated"]))
        total = sum(c["count"] for c in checkouts)
        rank = min(c["rank"] for c in checkouts)
        here = bool(current_workspace) and (project["repo"] == current_workspace or current_workspace in project["checkouts"])
        main = next((c for c in checkouts if not c["worktree"]), None)
        opener = main or checkouts[0]
        expandable = len(checkouts) > 1 or any(c["worktree"] for c in checkouts)
        out.append({
            "kind": "project", "key": project["repo"], "depth": 0, "label": _text(_base_name(project["repo"])),
            "detail": _text(project["repo"]), "count": total, "current": here, "expandable": expandable,
            "rank": rank, "age": opener["age"] if not expandable else max(checkouts, key=lambda c: c["updated"])["age"],
            "workspace": _text(opener["workspace"], 1024) if not expandable else "",
            "session": opener["session"] if not expandable else "",
        })
        if expandable:
            for checkout in checkouts:
                label = checkout["worktree"] or "main"
                out.append({
                    "kind": "checkout", "key": f"{project['repo']}|{checkout['workspace']}", "parent": project["repo"],
                    "depth": 1, "label": _text(label), "detail": _text(checkout["workspace"]),
                    "count": checkout["count"], "current": checkout["workspace"] == current_workspace,
                    "expandable": False, "rank": checkout["rank"], "age": checkout["age"],
                    "workspace": _text(checkout["workspace"], 1024), "session": checkout["session"],
                })
        if len(out) >= MAX_PROJECT_ROWS:
            break
    return out[:MAX_PROJECT_ROWS]


def openable(rows: list[dict]) -> set[tuple[str, str]]:
    """``(workspace, session)`` pairs a Projects click may open (re-checked before switching)."""
    return {(row["workspace"], row["session"]) for row in rows if row.get("workspace") and row.get("session")}


def human_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "K", "M", "G"):
        if value < 1024 or unit == "G":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}".replace(".0", "")
        value /= 1024
    return ""


def parent_path(path: str) -> str:
    return path.rpartition("/")[0]


def files_payload(result: Any, workspace: str, error: str = "") -> dict:
    """The Files tab: the host listing with a leading ``..`` row below the workspace root."""
    path = str(getattr(result, "path", "") or "") if result is not None else ""
    root = _base_name(workspace) or workspace
    rows: list[dict] = []
    if result is not None and path:
        rows.append({"name": "..", "path": parent_path(path), "kind": "up", "size": ""})
    for entry in (getattr(result, "entries", None) or [])[:MAX_FILE_ROWS]:
        kind = entry.get("kind")
        if kind not in {"dir", "file", "link"}:
            continue
        rows.append({"name": _text(entry.get("name"), 255), "path": _text(entry.get("path"), 1024),
                     "kind": kind, "size": human_size(entry.get("size")) if kind == "file" else ""})
    return {"path": _text(path, 1024), "label": _text(f"{root}/{path}" if path else f"{root}/", 400), "rows": rows,
            "truncated": bool(getattr(result, "truncated", False)), "error": _text(error, 300)}


def navigable(payload: dict) -> set[str]:
    """Directory paths the Files tab may open next (children and the parent), to validate clicks."""
    return {row["path"] for row in payload.get("rows", []) if row.get("kind") in {"dir", "up"}}


def previewable(payload: dict) -> set[str]:
    """File paths the Files tab may preview (listed regular files only), to validate double-clicks."""
    return {row["path"] for row in payload.get("rows", []) if row.get("kind") == "file"}


def preview_panel(result: Any) -> tuple[str, str]:
    """Title and body of the read-only file modal: labelled path, size and clipping, then the text.

    The body is control-escaped; a binary file, an empty one and a clipped one each say so.
    """
    path = _text(getattr(result, "path", ""), 1024)
    size, shown = int(getattr(result, "size", 0) or 0), int(getattr(result, "shown", 0) or 0)
    status = str(getattr(result, "status", "text"))
    lines = [f"Path: {path}", f"Size: {size:,} bytes ({human_size(size)})"]
    if status == "binary":
        lines.append(f"Not shown: {_text(getattr(result, 'message', ''), 200) or 'binary file'}")
    elif status == "empty":
        lines.append("The file is empty")
    elif getattr(result, "truncated", False):
        lines.append(f"Clipped: showing the first {shown:,} of {size:,} bytes")
    else:
        lines.append("Showing the whole file")
    if status == "text":
        lines += ["", *escape_controls(str(getattr(result, "text", ""))).split("\n")]
    return f"File · {path}", "\n".join(lines)


def git_payload(result: Any, error: str = "") -> dict:
    """The Git tab: read-only status as labelled sections (heading rows, then entries)."""
    if result is None:
        return {"available": False, "message": _text(error, 300), "branch": "", "summary": "", "rows": [],
                "truncated": False, "error": _text(error, 300)}
    rows: list[dict] = []
    grouped: dict[str, list[dict]] = {}
    for row in (getattr(result, "rows", None) or [])[:MAX_GIT_ROWS]:
        grouped.setdefault(str(row.get("group")), []).append(row)
    counts = getattr(result, "counts", None) or {}
    for key, title in GIT_GROUPS:
        entries = grouped.get(key)
        if not entries:
            continue
        rows.append({"kind": "heading", "text": f"{title} · {counts.get(key, len(entries))}"})
        for row in entries:
            label = _text(row.get("label"), 40)
            origin = _text(row.get("from"), 400)
            path = _text(row.get("path"), 400)
            name = f"{_base_name(origin)} → {_base_name(path)}" if origin else _base_name(path)
            rows.append({"kind": "entry", "code": _text(row.get("code"), 2), "label": label,
                         "path": f"{origin} → {path}" if origin else path, "name": name})
    branch = _text(getattr(result, "branch", ""), 200)
    upstream = _text(getattr(result, "upstream", ""), 200)
    ahead, behind = int(getattr(result, "ahead", 0) or 0), int(getattr(result, "behind", 0) or 0)
    summary = branch or "(detached)"
    if upstream:
        summary += f" → {upstream}"
    if ahead or behind:
        summary += f" · ahead {ahead} · behind {behind}"
    available = bool(getattr(result, "available", False))
    message = _text(getattr(result, "message", ""), 300)
    if available and not rows:
        message = "Working tree clean"
    return {"available": available, "message": message, "branch": branch, "summary": _text(summary, 400),
            "rows": rows, "truncated": bool(getattr(result, "truncated", False)), "error": _text(error, 300)}


__all__ = [
    "DEFAULT_TAB", "DEFAULT_WIDTH", "MAX_WIDTH", "MIN_WIDTH", "TABS",
    "clamp_width", "clean", "files_payload", "git_payload", "human_size", "is_manager_row", "navigable",
    "normalize_tab", "openable", "parent_path", "preview_panel", "previewable", "project_rows", "sessions_payload",
    "visible_session_rows",
]
