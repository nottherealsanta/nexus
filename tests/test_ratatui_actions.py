"""Native shell host actions never leak slash commands into model input."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import PermissionView


@pytest.mark.asyncio
async def test_submit_modes_attachments_and_cancel_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    client = SimpleNamespace(enqueue=AsyncMock())
    controller = SimpleNamespace(client=client, session="s",
        cancel=AsyncMock(return_value=SimpleNamespace(returned_messages=["one", "two"])))
    shell = ShellActions(controller)
    shell.attachments.append(SimpleNamespace(attachment_id="draft", kind="document"))
    await shell.submit("hello", "interrupt")
    client.enqueue.assert_awaited_once_with("s", "hello", mode="interrupt", attachments=["draft"], attachment_labels=["document 1"])
    assert not shell.attachments
    await shell.cancel()
    assert shell.composer_restore == "one\n\ntwo"


@pytest.mark.asyncio
async def test_commands_never_become_prompts():
    controller = SimpleNamespace(client=SimpleNamespace(enqueue=AsyncMock()), session="s")
    shell = ShellActions(controller)
    await shell.submit("/help")
    assert shell.panel_title == "Commands"
    with pytest.raises(ValueError, match="not implemented"):
        await shell.submit("/unknown")
    controller.client.enqueue.assert_not_awaited()


def test_permissions_have_daemon_choices_and_safe_text():
    view = initial_state("s")
    view.permissions.append(PermissionView(id="p", tool="Shell", preview="\x1b[2J"))
    result = project(SimpleNamespace(view=view), 1)
    assert result["prompt"]["id"] == "p"
    assert {choice["value"] for choice in result["prompt"]["choices"]} == {
        "allow_once", "allow_always", "deny_once", "deny_always"}
    assert "\x1b" not in "\n".join(result["prompt"]["lines"])


@pytest.mark.asyncio
async def test_native_follow_reduces_after_terminal_event():
    from nexus.ui.ratatui.controller import NativeController
    from nexus.events import Event
    events = [Event(type="turn.completed", session="s", seq=1, data={}),
              Event(type="turn.started", session="s", seq=2, data={})]
    async def stream(*args, **kwargs):
        for event in events:
            yield event
    controller = NativeController(SimpleNamespace(stream=stream), "s")
    received = []
    async def update(event):
        received.append(event.seq)
        controller.ingest(event)
    controller.resume(update)
    await controller._task
    assert received == [1, 2]


def _shell(tmp_path, monkeypatch, **client):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    controller = SimpleNamespace(client=SimpleNamespace(**client), session="s", view=view,
                                 switch_session=AsyncMock(), bootstrap=AsyncMock())
    return ShellActions(controller)


@pytest.mark.asyncio
async def test_command_edge_cases_match_textual(tmp_path, monkeypatch):
    rows = [SimpleNamespace(id="alpha-1"), SimpleNamespace(id="beta-2")]
    diff = SimpleNamespace(patch="", truncated=False)
    shell = _shell(tmp_path, monkeypatch, list_sessions=AsyncMock(return_value=rows),
                   fork=AsyncMock(return_value=SimpleNamespace(id="f")), git_diff=AsyncMock(return_value=diff))
    shell.switch = AsyncMock()
    await shell.command("/sessions", ("beta",))
    shell.switch.assert_awaited_once_with("beta-2")
    await shell.command("/sessions", ("zzz",))
    assert shell.notice == "No session matching zzz"
    await shell.command("/fork", ("nope",))
    shell.controller.client.fork.assert_awaited_with("s", None)
    await shell.command("/diff", ())
    assert shell.panel_lines == ["No changes"] or "No changes" in "\n".join(shell.panel_lines)
    with pytest.raises(ValueError, match="/diff"):
        await shell.command("/diff", ("a", "b"))
    await shell.command("/tools", ())
    assert shell.notice == "No tools used in this transcript"
    await shell.command("/tasks", ())
    assert shell.notice == "No background tasks"
    with pytest.raises(ValueError, match="/theme dark"):
        await shell.command("/theme", ("purple",))
    with pytest.raises(ValueError, match="unavailable"):
        await shell.command("/copy", ())


def test_every_shared_shortcut_is_bound_natively():
    """The Rust key handler must bind every row of the shared shortcut tables."""
    import re
    from pathlib import Path
    from nexus.ui_support.shortcuts import LEADER_SHORTCUTS, SHORTCUTS
    source = Path("rust/tui/src/main.rs").read_text()
    for letter, _, _ in LEADER_SHORTCUTS:
        assert f"KeyCode::Char('{letter}')" in source, f"Ctrl+X {letter} is not bound"
    for key, _, _ in SHORTCUTS:
        match = re.fullmatch(r"ctrl\+([a-z])", key)
        if match:
            assert f"KeyCode::Char('{match.group(1)}')" in source, f"{key} is not bound"
    assert "KeyModifiers::ALT" in source and "KeyCode::BackTab" in source and "1500" in source
