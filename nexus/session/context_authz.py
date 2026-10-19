"""Fail-closed authorization for reading another session's context (plan 03 §3.2).

``ContextAuthorizer.check(caller, target)`` allows a read only when the target
is the caller itself or a descendant reached through host-owned *spawned*
ownership edges:

* same project; the path runs from the target upward through
  ``sessions.owner_namespace/owner_id`` to the caller, so forks (``parent_id``),
  manager launches, siblings and upward reads never match;
* every edge on the path has ``child_allocations.context_read = 1`` (the host
  grants it for Nexus-run children; external Claude Code / agy workers and
  legacy allocations default to deny);
* the path is at most ``max_depth`` edges, and the target and every ancestor
  must still exist and not be in the trash.

Model or provider events never grant access. The check reads only index rows,
never session logs, and runs on every context call, so an edge that ends or a
session that is trashed mid-program stops access immediately.

Store access goes through the public ``SqliteSessionStore.resolve_name`` /
``resolve_child_id`` / ``session_row`` APIs. The store has no public reader for
``child_allocations.context_read``, so ``_context_read_granted`` is the one
narrowly scoped SQL helper (a single indexed lookup on a read connection).
"""
from __future__ import annotations

from typing import Any

from ..errors import SessionError
from .db import SqliteSessionStore

_NAMESPACES = ("main", "agents")


class ContextDenied(PermissionError):
    """A context read or sub-call was refused (always a fail-closed refusal)."""


class ContextAuthorizer:
    def __init__(self, manager, *, max_depth: int = 8):
        self.manager = manager
        self.max_depth = max(0, min(int(max_depth), 64))

    # -- store access ------------------------------------------------------

    def _row(self, namespace: str, session_id: str) -> dict[str, Any] | None:
        """The live (untrashed) session row, or ``None``."""
        if namespace not in _NAMESPACES or not session_id:
            return None
        store = self.manager.store
        scoped = SqliteSessionStore(store.db, store.project_id, namespace, register_project=False)
        try:
            row = scoped.session_row(session_id)
        except (SessionError, ValueError):
            return None
        if row is None or row.get("trash_id") is not None:
            return None
        return row

    def _context_read_granted(self, project_id: str, stored_id: str) -> str | None:
        """The allocating ``parent_id`` when the child's edge grants context read."""
        with self.manager.store.db.read() as conn:
            found = conn.execute(
                "SELECT parent_id FROM child_allocations "
                "WHERE project_id=? AND stored_id=? AND context_read=1",
                (project_id, stored_id),
            ).fetchone()
        return str(found[0]) if found else None

    # -- identity ----------------------------------------------------------

    def identity(self, ref: str) -> dict[str, Any]:
        """Resolve an id or name to its live row; never grants access by itself."""
        try:
            store = self.manager.store
            try:
                stored = store.resolve_name(ref)
            except SessionError:
                stored = store.resolve_child_id(ref)
            namespace = self.manager.resolve(stored)["namespace"]
            row = self._row(namespace, stored)
            if row is None:
                raise ContextDenied("context target unavailable")
            return {**row, "logical_id": row.get("logical_session_id") or row["id"],
                    "ref": row["id"], "project": row["project_id"]}
        except ContextDenied:
            raise
        except Exception as exc:
            raise ContextDenied("unknown context target") from exc

    # -- authorization -----------------------------------------------------

    def check(self, caller: str, target: str) -> dict[str, Any]:
        """Return the target's row when ``caller`` may read it, else raise."""
        origin, dest = self.identity(caller), self.identity(target)
        if origin["project"] != dest["project"]:
            raise ContextDenied("cross-project context")

        def key(row):
            return (row["namespace"], row["id"])

        current, seen = dest, set()
        for hop in range(self.max_depth + 1):
            if key(current) == key(origin):
                return dest
            if hop == self.max_depth or key(current) in seen:
                break
            seen.add(key(current))
            if current["namespace"] != "agents" or current.get("kind") == "manager":
                break
            granted_parent = self._context_read_granted(origin["project"], current["id"])
            if granted_parent is None:
                break
            parent = self._row(current.get("owner_namespace") or "", current.get("owner_id") or "")
            if parent is None or granted_parent not in {
                parent["id"], parent.get("logical_session_id"), parent.get("agent_id")}:
                break
            current = parent
        raise ContextDenied("no authorised allocated ancestry")
