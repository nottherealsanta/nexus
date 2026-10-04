"""Native shell host actions never leak slash commands into model input."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import PermissionView


@pytest.mark.asyncio
async def test_submit_defaults_to_steering():
    client = SimpleNamespace(enqueue=AsyncMock())
    shell = ShellActions(SimpleNamespace(client=client, session="s"))
    await shell.submit("hello")
    client.enqueue.assert_awaited_once_with("s", "hello", mode="steer", attachments=[], attachment_labels=[])


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
async def test_session_switch_retains_images_and_stable_markers(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.refresh_preview = AsyncMock()
    async def switch(session):
        shell.controller.session = session
    shell.controller.switch_session.side_effect = switch
    first = SimpleNamespace(attachment_id="one", kind="image", name="one.png")
    second = SimpleNamespace(attachment_id="two", kind="image", name="two.png")
    shell.attachments.extend([first, second])
    assert shell.attachment_marker(0) == "[image 1]"
    assert shell.attachment_marker(1) == "[image 2]"
    assert shell.attachments == []
    assert shell.tabs[-1]["title"] == "New Session"
    await shell.switch("s")
    assert shell.attachments == [first, second]
    assert shell.attachment_marker(1) == "[image 2]"
    shell.reconcile_attachment_markers("hello [image 2]")
    assert shell.attachments == [second]
    shell.reconcile_attachment_markers("hello [image 1][image 2]")
    assert shell.attachments == [first, second]  # editor undo restores pending bytes
    assert project(shell.controller, 1, shell=shell)["composer_key"] != project(
        SimpleNamespace(view=initial_state("other")), 1)["composer_key"]


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


def test_background_tab_completion_notifies_once(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.controller.completion_bell = 0
    row = dict(id="background", workspace=".", status="working", state="running")
    shell.refresh_session_tabs([row])
    assert shell.controller.completion_bell == 0
    done = dict(row, status="done", state="complete")
    shell.refresh_session_tabs([done])
    assert shell.controller.completion_bell == 1
    assert shell.tabs[0]["status"] == "done"
    shell.refresh_session_tabs([done])
    assert shell.controller.completion_bell == 1
    shell.refresh_session_tabs([dict(done, status="idle")])
    assert shell.tabs[0]["status"] == "idle"
    shell.refresh_session_tabs([row])
    shell.refresh_session_tabs([done])
    assert shell.controller.completion_bell == 2


def test_active_tab_refresh_does_not_duplicate_sound(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.controller.completion_bell = 0
    row = dict(id="s", workspace=".", status="working", state="running")
    shell.refresh_session_tabs([row])
    shell.refresh_session_tabs([dict(row, status="done", state="complete")])
    assert shell.controller.completion_bell == 0


def test_completion_uses_generated_cue_once(tmp_path, monkeypatch):
    from nexus.ui_support import voice_capture
    calls = []
    monkeypatch.setattr(voice_capture, "play_cue", calls.append)
    shell = _shell(tmp_path, monkeypatch)
    shell.controller.completion_bell = 0
    shell.notify_completion()
    assert calls == []
    shell.controller.completion_bell = 1
    shell.notify_completion()
    shell.notify_completion()
    assert calls == ["complete"]
    shell.controller.completion_bell = 2
    shell.notify_completion()
    assert calls == ["complete", "complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [True, False])
async def test_new_session_reuses_agent_without_picker(tmp_path, monkeypatch, named):
    shell = _shell(tmp_path, monkeypatch, select_agent=AsyncMock(), list_agents=AsyncMock())
    shell.controller.agent_name = "plan"

    async def switch(session):
        shell.controller.session = session
        shell.controller.agent_name = "default"

    shell.switch = AsyncMock(side_effect=switch)
    await shell.command("/new", ("fresh",) if named else ())
    session = shell.controller.session
    if named:
        assert session == "fresh"
    else:
        assert len(session) == 12
    shell.controller.client.select_agent.assert_awaited_once_with(session, "plan")
    shell.controller.bootstrap.assert_awaited_once()
    shell.controller.client.list_agents.assert_not_awaited()
    assert shell.panel_title != "New session · choose root agent"


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


def test_reply_watched_live_is_not_unread_after_switching_away(tmp_path, monkeypatch):
    from nexus.ui.ratatui.workflows import session_rows
    shell = _shell(tmp_path, monkeypatch)
    shell.controller.completion_bell = 0
    shell.controller.completion_seq = 7  # the viewer received the whole reply while "s" was open
    summary = lambda state, seq: SimpleNamespace(session=SimpleNamespace(
        id="s", title="t", state=state, last_seq=seq, completion_seq=seq,
        updated_at=0, last_activity=0), workspace=".")
    result = lambda state, seq: SimpleNamespace(sessions=[summary(state, seq)], truncated=False)
    shell.refresh_session_tabs(session_rows(result("running", 3), "s", shell.seen_seq))
    shell.mark_current_seen()
    shell.controller.session = "other"
    shell.refresh_session_tabs(session_rows(result("complete", 7), "other", shell.seen_seq))
    assert shell.tabs[0]["status"] != "done"
    assert shell.controller.completion_bell == 0


@pytest.mark.asyncio
async def test_opening_finished_session_acknowledges_snapshot_before_switching_away(tmp_path, monkeypatch):
    from dataclasses import replace
    from nexus.ui.ratatui.workflows import session_rows

    shell = _shell(tmp_path, monkeypatch)
    shell.controller.completion_bell = 0
    shell.controller.cursor = 0
    shell.controller.completion_seq = 0
    shell.workspace = "."
    shell.refresh_preview = AsyncMock()

    def rows(state, seq):
        summary = SimpleNamespace(session=SimpleNamespace(
            id="finished", title="t", state=state, last_seq=seq, completion_seq=seq,
            updated_at=0, last_activity=0), workspace=".")
        return session_rows(SimpleNamespace(sessions=[summary], truncated=False),
                            shell.controller.session, shell.seen_seq)

    shell.refresh_session_tabs(rows("running", 3))
    shell.refresh_session_tabs(rows("complete", 8))
    assert shell.tabs[0]["status"] == "done"
    assert shell.controller.completion_bell == 1

    async def switch(session):
        shell.controller.session = session
        shell.controller.view = initial_state(session)

    async def bootstrap():
        # Snapshot replay does not advance the stream cursor.
        shell.controller.view = replace(shell.controller.view, last_seq=8)
        shell.controller.completion_seq = 8

    shell.controller.switch_session.side_effect = switch
    shell.controller.bootstrap.side_effect = bootstrap
    await shell.switch("finished")
    assert shell.seen_seq["finished"] == 8
    await shell.switch("other")
    shell.refresh_session_tabs(rows("complete", 8))
    shell.refresh_session_tabs(rows("complete", 8))
    assert shell.tabs[0]["status"] != "done"
    assert shell.controller.completion_bell == 1

    shell.refresh_session_tabs(rows("running", 9))
    shell.refresh_session_tabs(rows("complete", 12))
    assert shell.tabs[0]["status"] == "done"
    assert shell.controller.completion_bell == 2


def test_projected_attachment_operations_are_allowed_recursively():
    from nexus.ui.ratatui.prototype import _block_operations

    message = {"kind": "message_page", "id": "message-1"}
    output = {"kind": "tool_output", "id": "tool-1"}
    chip = {"kind": "message_page", "id": "message-2"}
    blocks = [{"operation": message, "output_operation": output,
               "members": [{"chip_operation": chip}, {"text": "not an operation"}]}]
    assert list(_block_operations(blocks)) == [message, output, chip]
    assert list(_block_operations([])) == []


def test_form_delete_capability_is_limited_to_settings_files(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.workflows.edit("Settings", "body", {"kind": "settings_read"}, autosave=True)
    assert project(shell.controller, 1, shell=shell)["form"]["can_delete"] is True
    shell.workflows.edit("Provider key", "", {"kind": "provider_key"}, secret=True)
    assert project(shell.controller, 2, shell=shell)["form"]["can_delete"] is False
