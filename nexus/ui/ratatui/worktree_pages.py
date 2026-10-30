"""Small native worktree list, move, saved-diff and checkout pages (plan 08, agent-managed worktrees)."""
from __future__ import annotations


def outcome(state):
    """Present bounded review facts without exposing internal confirmation tokens."""
    lines = []
    for key, label in (("reason", "Reason"), ("state", "State"), ("kind", "Result"),
                       ("target_branch", "Target branch"), ("session_branch", "Session branch"),
                       ("branch", "Branch"), ("path_display", "Checkout"), ("after", "Commit")):
        if state.get(key):
            lines.append(f"{label}: {state[key]}")
    files = state.get("files", [])
    if isinstance(files, list):
        lines.extend(f"File: {row.get('path', '') if isinstance(row, dict) else row}" for row in files[:80])
        if len(files) > 80 or state.get("clipped"):
            lines.append("… file list clipped")
    return lines or ["No further action is available."]


def _row_label(row):
    """One ``/worktrees`` list row: ``← name · merged|N unmerged · dirty · sessions``."""
    name = row.get("name") or ("main" if row.get("kind") == "main" else "(unnamed)")
    parts = [("← " if row.get("current") else "") + name]
    if row.get("kind") != "main":
        if row.get("present") is False:
            parts.append("checkout removed")
        parts.append("merged" if row.get("merged") else f"{row.get('unmerged_commits', 0)} unmerged")
        if row.get("dirty"):
            parts.append("dirty")
    elif row.get("dirty"):
        parts.append("dirty")
    sessions = [s for s in row.get("sessions") or [] if isinstance(s, dict)]
    if sessions:
        parts.append(f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}"
                     + (f" ({sum(1 for s in sessions if s.get('active'))} active)" if any(s.get("active") for s in sessions) else ""))
    return " · ".join(parts)


class WorktreePages:
    async def worktrees_page(self):
        """``/worktrees``: the project's worktrees as the agent's ``checkout status`` sees them."""
        session = self.shell.controller.session
        result = await self.client.list_session_worktrees(session)
        state = result.state or {}
        rows = [row for row in state.get("worktrees") or [] if isinstance(row, dict)]
        lines = [f"{len(rows)} worktree(s) in this project." if rows else "No worktrees yet. The agent creates them when the work calls for it."]
        if state.get("clipped"):
            lines.append(f"… {state['clipped']} more not shown")
        for row in state.get("external") or []:
            if isinstance(row, dict):
                lines.append(f"External (read-only): {row.get('path_display') or row.get('name') or ''}")
        items = [(_row_label(row), {
            "kind": "worktree_row", "id": session, "name": str(row.get("name") or ""),
            "row": {key: row.get(key) for key in ("kind", "current", "child_id", "present", "branch", "base_ref", "merged", "dirty")},
        }) for row in rows if row.get("kind") in {"main", "worktree"}]
        try:  # isolated subagent checkouts keep their own review and discard pages
            children = [row for row in (await self.client.list_worktrees()).worktrees if row.get("kind") != "session"]
        except Exception:  # noqa: BLE001 - the session rows above are still useful
            children = []
        items += [(f"{row.get('lifecycle') or row.get('status') or 'unknown'} · {row.get('child_id') or row.get('id')}", {
            "kind": "session_worktree_actions", "id": row.get("session"), "child_id": row.get("child_id"), "record_kind": row.get("kind")}
            if row.get("kind") == "subagent" else {"kind": "worktree_actions", "id": row.get("child_id") or row.get("id")})
            for row in children]
        self.menu("Worktrees", items, lines)

    async def worktree_operation(self, operation):
        kind = operation["kind"]
        if kind == "worktree_row":
            row, name = operation.get("row") or {}, operation.get("name") or ""
            rows = []
            if not row.get("current"):
                rows.append(("Move this session here", {"kind": "worktree_move", "id": operation["id"],
                                                         "to": "main" if row.get("kind") == "main" else name}))
            elif row.get("kind") == "worktree":
                rows.append(("Integrate into base branch…", {"kind": "session_integrate", "id": operation["id"]}))
                rows.append(("Move this session to the main checkout", {"kind": "worktree_move", "id": operation["id"], "to": "main"}))
            if row.get("kind") == "worktree" and row.get("child_id") and not row.get("current"):
                rows.append(("Remove checkout…", {"kind": "session_remove", "id": operation["id"], "child_id": row["child_id"]}))
            self.menu(f"Worktree · {name or 'main checkout'}", rows or [("Refresh", {"kind": "worktrees"})],
                      [f"Branch: {row.get('branch') or '(none)'}", f"Base: {row.get('base_ref') or '(none)'}",
                       "Moving is for an idle session; the agent can also move itself."])
        elif kind == "worktree_move":
            result = await self.client.move_session_worktree(operation["id"], operation["to"], reason="Moved by the user")
            target = operation["to"] if operation["to"] != "main" else "the main checkout"
            self.shell.flash(f"Moved to {target}", "success")
            from .worktree_bar import refresh_session_worktree
            await refresh_session_worktree(self.shell)
            await self.worktrees_page()
        elif kind == "saved_turn_diff":
            result = await self.client.session_turn_diff(self.shell.controller.session, operation["seq"])
            state = result.state
            files = state.get("files", [])
            lines = [f"Sequence: {operation['seq']}", f"Source: {state.get('source', 'saved changes')}",
                     f"Unattributed files: {state.get('foreign', 0)}",
                     f"Changed files: {len(files) if isinstance(files, list) else 0}"]
            if isinstance(files, list):
                lines.extend(f"  {row.get('path', '')} · +{row.get('insertions', 0)} −{row.get('deletions', 0)}"
                             for row in files[:80] if isinstance(row, dict))
                if len(files) > 80:
                    lines.append(f"… {len(files) - 80} more files")
            patch = state.get("patch", "")
            if isinstance(patch, str) and patch:
                patch_lines = patch.splitlines()
                lines.extend(["", *patch_lines[:160]])
                if len(patch_lines) > 160 or state.get("clipped"):
                    lines.append("… diff clipped by the host")
            self.menu("Saved turn", [], lines, layout="modal")
        elif kind == "session_worktree_actions":
            session = operation["id"]
            if operation.get("record_kind") == "subagent":
                inspection = await self.client.inspect_worktree(operation["child_id"])
                state = inspection.record
            else:
                status = await self.client.session_worktree_status(session)
                state = status.state
            rows = []
            if operation.get("record_kind") == "session" and state.get("enabled"):
                rows.append(("Integrate into base branch…", {"kind": "session_integrate", "id": session}))
            if operation.get("child_id"):
                rows.append(("Remove checkout…", {"kind": "session_remove", "id": session, "child_id": operation["child_id"]}))
            self.menu("Session worktree", rows or [("Refresh", {"kind": "session_worktree_actions", "id": session})],
                      [f"Session: {session}", f"Branch: {state.get('branch') or '(none)'}", f"State: {state.get('state') or state.get('lifecycle') or 'main'}",
                       f"Checkout: {state.get('path_display', '')}"])
        elif kind == "session_integrate":
            result = await self.client.integrate_session(operation["id"], confirmation_token=operation.get("token", ""))
            if result.state.get("ready") and not operation.get("token"):
                self.menu("Confirm session integration", [("Cancel", {"kind": "worktrees"}),
                    ("Confirm", {"kind": "session_integrate", "id": operation["id"], "token": result.state.get("token", "")})],
                    outcome(result.state), layout="modal")
            else:
                self.menu("Session integration", [], outcome(result.state))
        elif kind == "session_remove":
            result = await self.client.remove_worktree(operation["child_id"], confirmation_token=operation.get("token", ""))
            if result.state.get("state") == "ready" and not operation.get("token"):
                self.menu("Confirm checkout removal", [("Cancel", {"kind": "worktrees"}),
                    ("Confirm", {"kind": "session_remove", "id": operation["id"], "child_id": operation["child_id"], "token": result.state.get("token", "")})],
                    outcome(result.state), layout="modal")
            else:
                self.menu("Checkout removal", [], outcome(result.state))
        else:
            return False
        return True
