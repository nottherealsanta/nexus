"""Shell notices are dismissible toasts (plan RATATUI_DESIGN_MOCKUPS_PLAN §8)."""
from types import SimpleNamespace

import pytest

from nexus.ui.ratatui.actions import ShellActions, notice_level, split_toast
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state


def _shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    controller = SimpleNamespace(client=SimpleNamespace(), session="s", view=initial_state("s"))
    return ShellActions(controller)


@pytest.mark.parametrize("text,level", [
    ("Copied 120 characters", "success"), ("Saved · applies next turn", "success"),
    ("Theme saved", "success"), ("Reconnected · durable state replayed", "success"),
    ("No session matching zzz", "warning"), ("UI background actions busy; try again", "warning"),
    ("Display update failed: ValueError: x", "error"), ("Conflict: the agent file changed on disk.", "error"),
    ("Sessions could not be shown: boom", "error"), ("Full tool output previews on", "info"),
    ("No background tasks", "info"),
])
def test_legacy_notice_text_maps_to_a_level(text, level):
    assert notice_level(text) == level


def test_notice_setter_raises_a_toast_and_keeps_the_legacy_string(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.notice = "Copied 4 characters"
    assert shell.notice == "Copied 4 characters"
    [toast] = shell.toasts
    assert toast["level"] == "success" and toast["title"] == "Copied 4 characters" and toast["id"] > 0
    shell.notice = ""
    assert len(shell.toasts) == 1, "clearing is not an event"


def test_disconnected_is_a_banner_not_a_toast(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.notice = "Disconnected: use /reconnect to replay and reattach"
    assert not shell.toasts and shell.notice.startswith("Disconnected")
    snapshot = project(shell.controller, 1, shell=shell)
    assert snapshot["disconnected"] is True and snapshot["toasts"] == []
    shell.notice = "Reconnected · durable state replayed"
    assert shell.toasts[-1]["level"] == "success"


def test_exceptions_are_error_toasts_and_ids_increase(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.flash("boom", "error")
    shell.flash("again", "warning")
    ids = [t["id"] for t in shell.toasts]
    assert ids == sorted(ids) and len(set(ids)) == 2
    assert shell.toasts[0]["level"] == "error" and shell.notice == "again"


def test_the_same_toast_is_not_repeated_by_a_refresh_loop(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    for _ in range(5):
        shell.notice = "Sessions could not be shown: boom"
    assert len(shell.toasts) == 1


def test_toast_list_is_bounded(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    for i in range(60):
        shell.toast(f"message {i}", "info")
    assert len(shell.toasts) == 20 and shell.toasts[-1]["title"] == "message 59"


def test_long_messages_split_into_title_and_body_without_losing_text():
    text = "Conflict: the agent file changed on disk. Reopen it to load the current file."
    title, body = split_toast(text)
    assert title and body and (title + " " + body).replace(": ", " ").split()[0] == "Conflict"
    assert len(title) <= 80
    assert split_toast("Voice off") == ("Voice off", "")


def test_controls_are_escaped_in_toasts(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.toast("bad \x1b[31mred\x1b[0m", "error")
    assert "\x1b" not in shell.toasts[-1]["title"]


def test_snapshot_carries_toasts_and_no_longer_a_notice_block(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.toast("Removed model", "warning", action={"label": "Undo", "operation": {"kind": "undo"}})
    snapshot = project(shell.controller, 1, shell=shell)
    [toast] = snapshot["toasts"]
    assert toast["action"]["label"] == "Undo" and toast["level"] == "warning"
    assert not any(block["id"] == "notice" for block in snapshot["blocks"])
    assert not any(line.startswith("Error:") for line in snapshot["lines"])


def test_every_toast_is_also_in_the_logs_tab(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.toast("Could not reach OpenAI", "error")
    shell.toast("Copied 3 characters", "success")
    folded = "\n".join(shell.logs.lines(show_all=False))
    everything = "\n".join(shell.logs.lines(show_all=True))
    assert "[ERROR] client · toast · Could not reach OpenAI" in folded
    assert "Copied 3 characters" not in folded and "1 routine entries folded" in folded
    assert "[SUCCESS] client · toast · Copied 3 characters" in everything


@pytest.mark.asyncio
async def test_sessions_command_opens_the_sidebar_instead_of_a_dialog(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    shell = _shell(tmp_path, monkeypatch)
    row = SimpleNamespace(id="s2", title="Docs", state="idle", workspace="/w", session=None, last_seq=0, completion_seq=0)
    row.session = SimpleNamespace(id="s2", title="Docs", state="idle", last_seq=0, completion_seq=0, group="~/w", archived=False, updated_at=0)
    shell.controller.client = SimpleNamespace(project_sessions=AsyncMock(return_value=SimpleNamespace(sessions=[], truncated=False)))
    assert shell.sessions_request == 0
    await shell.command("/sessions", ())
    await shell.command("/sessions", ())
    assert shell.sessions_request == 2, "each request asks the client to open and focus the sidebar"
    assert not shell.panel_title, "there is no separate Sessions dialog"
    snapshot = project(shell.controller, 1, shell=shell)
    assert snapshot["sessions_request"] == 2
