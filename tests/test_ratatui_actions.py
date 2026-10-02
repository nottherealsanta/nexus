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


async def test_preview_refusal_while_a_turn_runs_is_not_an_error(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nexus.ui.ratatui.actions import ShellActions

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    class Client:
        outcome: object = RuntimeError("context preview is unavailable while the session is active")

        async def inspect_context(self, session):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    client = Client()
    shell = ShellActions(SimpleNamespace(client=client, session="s"))
    shell.preview = "stale"
    assert await shell.refresh_preview() is False and shell.preview == "stale"
    client.outcome = "ready"
    assert await shell.refresh_preview() is True and shell.preview == "ready"
    client.outcome = RuntimeError("daemon exploded")
    with pytest.raises(RuntimeError, match="exploded"):
        await shell.refresh_preview()


@pytest.mark.asyncio
async def test_usage_opens_before_fetch_and_keeps_cached_data_on_error(tmp_path, monkeypatch):
    import asyncio

    gate = asyncio.Event()
    result = {"providers": [{"id": "codex", "label": "Codex", "plan": "Plus"}], "fetched_at": 1}

    async def fetch():
        await gate.wait()
        return result

    shell = _shell(tmp_path, monkeypatch, providers_usage=AsyncMock(side_effect=fetch))
    shell.on_update = AsyncMock()
    await shell.command("/usage", ())
    assert shell.panel_title == "Provider usage" and shell.panel_layout == "modal"
    assert shell.panel_loading and "No cached" in shell.panel_lines[0]
    assert not shell.usage_task.done()
    gate.set()
    await shell.usage_task
    assert not shell.panel_loading and "Codex · Plus" in shell.panel_lines
    shell.on_update.assert_awaited_once()

    shell.client.providers_usage = AsyncMock(side_effect=RuntimeError("offline"))
    await shell.command("/usage", ())
    assert shell.panel_loading and "Codex · Plus" in shell.panel_lines
    await shell.usage_task
    assert not shell.panel_loading and "Codex · Plus" in shell.panel_lines
    assert "offline" in shell.panel_lines[-1]


@pytest.mark.asyncio
async def test_usage_refresh_cannot_replace_a_dismissed_or_new_panel(tmp_path, monkeypatch):
    import asyncio

    gate = asyncio.Event()

    async def fetch():
        await gate.wait()
        return {"providers": [{"label": "Fresh"}]}

    shell = _shell(tmp_path, monkeypatch, providers_usage=AsyncMock(side_effect=fetch))
    shell.on_update = AsyncMock()
    await shell.command("/usage", ())
    shell.workflows.back()
    shell.show("Different panel", "Keep this")
    gate.set()
    await shell.usage_task
    assert shell.panel_title == "Different panel" and shell.panel_lines == ["Keep this"]
    shell.on_update.assert_not_awaited()
    assert shell.usage_cache is not None


@pytest.mark.asyncio
async def test_context_usage_modal_preserves_document_newlines(tmp_path, monkeypatch):
    from nexus.host.protocol import ContextInspectResult

    preview = ContextInspectResult(session="s", system_text="first\nsecond",
                                   included_parts=[{"name": "agents_md", "text": "# Rules\n\n- One\n- Two"}])
    shell = _shell(tmp_path, monkeypatch, inspect_context=AsyncMock(return_value=preview))
    shell.controller.view = initial_state("s")
    await shell.command("/context", ())
    assert shell.panel_title == "Context usage" and shell.panel_layout == "modal"
    body = "\n".join(shell.panel_lines)
    assert "# Rules\n\n- One\n- Two" in body and "first\nsecond" in body
    assert "Tools" in body and "Request details" in body

    shell.client.inspect_context = AsyncMock(side_effect=RuntimeError("session is active"))
    await shell.command("/context", ())
    assert shell.panel_title == "Context usage"
    assert "session is active" in "\n".join(shell.panel_lines)


@pytest.mark.asyncio
async def test_older_usage_reply_cannot_overwrite_the_newer_cache(tmp_path, monkeypatch):
    import asyncio

    old_gate = asyncio.Event()
    started = asyncio.Event()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await old_gate.wait()
            except asyncio.CancelledError:
                await old_gate.wait()  # Simulate a transport that finishes despite cancellation.
            return {"providers": [{"label": "Older"}]}
        return {"providers": [{"label": "Newest"}]}

    shell = _shell(tmp_path, monkeypatch, providers_usage=AsyncMock(side_effect=fetch))
    shell.open_usage()
    older = shell.usage_task
    await started.wait()
    shell.open_usage()
    await shell.usage_task
    old_gate.set()
    await older
    assert "Newest" in shell.panel_lines
    assert shell.usage_cache["providers"][0]["label"] == "Newest"
