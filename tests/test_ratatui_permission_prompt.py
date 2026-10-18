"""Structured permission prompt fields (DESIGN OVERHAUL step 9)."""
from nexus.host_support.session_preview import pending_request
from nexus.ui.ratatui.permission_prompt import permission_fields
from nexus.view import ConversationView
from nexus.view.model import PermissionTargetView, PermissionView


def test_fields_for_a_command_request():
    permission = PermissionView(id="p1", tool="Bash", preview="git push origin main", default_rule="Bash(git push:*)", ts=1700.0)
    fields = permission_fields(permission)
    assert fields == {"title": "Allow Bash git push origin main?", "since": 1700.0,
                      "what": ["git push origin main"], "why": "Bash(git push:*)"}


def test_fields_for_path_targets_and_bounded_rows():
    targets = [PermissionTargetView(role="write", path=f"/w/f{i}.txt", reason="edit") for i in range(40)]
    fields = permission_fields(PermissionView(id="p1", tool="Edit", targets=targets, ts=None))
    assert fields["title"] == "Allow Edit /w/f0.txt (+39 more)?"
    assert fields["since"] == 0.0 and fields["why"] == ""
    assert fields["what"][0] == "write: /w/f0.txt — edit" and len(fields["what"]) == 12


def test_fields_escape_controls_and_survive_a_bare_request():
    fields = permission_fields(PermissionView(id="p1", tool="Bash", preview="rm \x1b[2J\x07"))
    assert "\x1b" not in fields["title"] and "\x07" not in fields["what"][0]
    assert permission_fields(PermissionView(id="p1"))["title"] == "Allow tool?"


def test_since_comes_from_the_reducer_event_time():
    from nexus.events import Event
    from nexus.view import apply, initial_state

    view = apply(initial_state("s"), Event(type="permission.requested", seq=1, ts=42.0, session="s", id="e001",
                                           data={"id": "p1", "tool": "Bash", "preview": "ls"}))
    pending = view.pending_permissions[0]
    assert pending.ts == 42.0 and permission_fields(pending)["since"] == 42.0


def test_dashboard_pending_request_summary():
    view = ConversationView(session_id="s", permissions=[
        PermissionView(id="done", tool="Bash", status="resolved"),
        PermissionView(id="p1", tool="Bash", preview="git push origin main", ts=9.0)])
    assert pending_request(view) == {"request_id": "p1", "kind": "permission",
                                     "summary": "Allow Bash: git push origin main", "ts": 9.0}
    assert pending_request(ConversationView(session_id="s")) == {}
