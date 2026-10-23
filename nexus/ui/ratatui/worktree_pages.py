"""Small native session-mode, saved-diff and checkout pages (plan 08)."""
from __future__ import annotations

import re

from ...ui_support.text import escape_controls

#: A session worktree name: it is also the session title and the `nexus/session/<name>` branch.
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}")
ASK_TITLE = "Create a worktree, or work in the main checkout?"
NAME_HINT = "Letters, digits, - and _, up to 60 · also the session title and branch nexus/session/<name>"


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


class WorktreePages:
    def worktree_name_step(self, pending, error=""):
        """Open the "Name this worktree" text modal; the pending prompt stays in ``pending``."""
        self.pending_worktree_submission = pending
        self.edit("Name this worktree · Ctrl+S to create · Esc cancels", "",
                  {"kind": "worktree_name", "session": pending["session"]}, replace=True)
        self.shell.panel_layout = "modal"
        self.shell.panel_lines = [NAME_HINT]
        self.form["status"] = error or NAME_HINT  # the modal shows only the status line under the editor

    async def submit_worktree_name(self, body):
        """Validate locally, ask the host to create the named worktree, then send the preserved prompt.

        Errors stay in the modal (``form.status``) so the user edits and retries; nothing is lost."""
        pending = self.pending_worktree_submission
        if not pending or pending["session"] != self.shell.controller.session:
            self.cancel_worktree_submission()
            raise ValueError("Session changed; open the original session and submit the prompt again")
        name = body.strip()
        if not NAME_PATTERN.fullmatch(name):
            self.form["status"] = "Use letters, digits, - and _ (start with a letter or digit), up to 60 characters."
            return
        try:
            await self.client.set_session_worktree(pending["session"], True, name=name)
        except Exception as exc:  # noqa: BLE001 - the host's refusal is shown, then edited and retried
            self.form["status"] = escape_controls(str(exc))[:300] or "The host refused this worktree name."
            return
        self.stack.clear()
        self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form = "", [], [], None
        await self.resume_worktree_submission(pending)

    def close_worktree_modal(self):
        """Drop the Yes/No or name modal; the answered question must not outlive its answer."""
        self.stack.clear()
        self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form = "", [], [], None
        self.form_target = None

    async def resume_worktree_submission(self, pending):
        self.close_worktree_modal()  # the "No" answer reaches here with the question still open
        self.shell.attachments = pending["attachments"]
        self.shell.attachment_labels = pending["attachment_labels"]
        self.shell.marker_attachments = pending["marker_attachments"]
        self.pending_worktree_submission = None
        from .worktree_bar import refresh_session_worktree
        await refresh_session_worktree(self.shell, force=True)
        # The composer retains the original attachment objects and labels while
        # the modal is open; submit consumes them only after enqueue succeeds.
        await self.shell.submit(pending["text"], mode=pending["mode"], session=pending["session"])

    def cancel_worktree_submission(self):
        """Esc: drop the question, close the modal and give the prompt back to the composer."""
        pending, self.pending_worktree_submission = self.pending_worktree_submission, None
        self.stack.clear()
        self.shell.panel_title, self.shell.panel_lines, self.shell.items, self.form = "", [], [], None
        if pending:
            self.shell.composer_restore = pending["text"]

    async def worktree_operation(self, operation):
        kind = operation["kind"]
        if kind == "worktree_mode":
            session = operation.get("id") or self.shell.controller.session
            result = await self.client.session_worktree_choice(session)
            state = result.state
            current = state.get("mode", "ask")
            rows = [(f"{'✓ ' if current == mode else ''}{label}", {"kind": "worktree_mode_set", "mode": mode, "id": session})
                    for mode, label in (("none", "No worktree"), ("ask", "Ask every time"), ("always", "Always worktree"))]
            lines = [f"Current mode: {current}", f"Root: {state.get('kind', 'main')}",
                     f"Checkout: {state.get('path_display', '')}", f"Branch: {state.get('branch') or '(none)'}"]
            if not state.get("editable"):
                lines.append("This session keeps its root; mode changes apply to new sessions.")
            if state.get("disabled_reason"):
                lines.append(state["disabled_reason"])
            self.menu("Session worktree", rows, lines, layout="modal")
        elif kind == "worktree_mode_set":
            result = await self.client.session_worktree_choice(operation["id"], operation["mode"])
            self.shell.flash(f"Worktree mode · {result.state.get('mode', operation['mode'])}", "success")
            await self.operate({"kind": "worktree_mode", "id": operation["id"]})
        elif kind == "worktree_answer":
            pending = self.pending_worktree_submission
            if not pending:
                raise ValueError("Worktree choice expired; submit the prompt again")
            if pending["session"] != self.shell.controller.session:
                self.pending_worktree_submission = None
                raise ValueError("Session changed; open the original session and submit the prompt again")
            if operation["enabled"]:
                self.worktree_name_step(pending)  # step 2: name it; the host call happens on submit
                return True
            await self.client.set_session_worktree(pending["session"], False)
            await self.resume_worktree_submission(pending)
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
            self.menu("Saved turn · checkpoint", [], lines, layout="modal")
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
