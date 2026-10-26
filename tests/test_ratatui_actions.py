"""Native shell host actions never leak slash commands into model input."""
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import json

import msgspec
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
    client.enqueue.assert_awaited_once_with("s", "hello", mode="steer", attachments=[], attachment_labels=[], client_request_id=ANY)


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
    client.enqueue.assert_awaited_once_with("s", "hello", mode="interrupt", attachments=["draft"], attachment_labels=["document 1"], client_request_id=ANY)
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
    assert shell.attachments == [first, second]
    assert not shell.tabs
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
                                 switch_session=AsyncMock(), bootstrap=AsyncMock(), forget=Mock())
    return ShellActions(controller)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1, 2])
async def test_close_current_tab_selects_neighbor_without_cancelling(tmp_path, monkeypatch, index):
    shell = _shell(tmp_path, monkeypatch, enqueue=AsyncMock(), cancel=AsyncMock(),
                   delete_session=AsyncMock(), archive_session=AsyncMock())
    shell.workspace = "current"
    shell.tabs = [dict(id="left", workspace="other"),
                  dict(id="middle", workspace="current"),
                  dict(id="right", workspace="third")]
    shell.controller.session = shell.tabs[index]["id"]
    shell.workspace = shell.tabs[index]["workspace"]
    shell.controller.forget = Mock()
    shell.controller.cancel = AsyncMock()
    shell.switch_project = AsyncMock()
    closed = shell.tabs[index]
    assert await shell.submit("/close") is True
    assert closed not in shell.tabs
    neighbor = shell.tabs[min(index, len(shell.tabs) - 1)]
    shell.switch_project.assert_awaited_once_with(neighbor["workspace"], neighbor["id"])
    shell.controller.forget.assert_called_once_with(closed["id"])
    shell.controller.cancel.assert_not_awaited()
    for method in vars(shell.client).values():
        method.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_only_tab_uses_new_and_retains_original(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch, select_agent=AsyncMock(), enqueue=AsyncMock())
    shell.workspace = "current"
    original = dict(id="s", workspace=shell.workspace, state="running")
    shell.tabs = [original]
    shell.controller.agent_name = "plan"
    shell.controller.forget = Mock()
    shell.controller.cancel = AsyncMock()

    async def switch(session):
        shell.controller.session = session
        shell.tabs.append(dict(id=session, workspace=shell.workspace))

    shell.switch = AsyncMock(side_effect=switch)
    assert await shell.submit("/close") is True
    assert shell.tabs[0] is original
    assert len(shell.tabs) == 2
    fresh = shell.controller.session
    assert fresh != "s" and len(fresh) == 12
    shell.client.select_agent.assert_awaited_once_with(fresh, "plan")
    shell.controller.bootstrap.assert_awaited_once()
    shell.controller.forget.assert_not_called()
    shell.controller.cancel.assert_not_awaited()
    shell.client.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_rejects_arguments_and_routes_current_workspace(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch, enqueue=AsyncMock())
    shell.workspace = "project"
    shell.close_tab = AsyncMock()
    with pytest.raises(ValueError, match="Usage: /close"):
        await shell.submit("/close other")
    shell.close_tab.assert_not_awaited()
    await shell.submit("/close")
    shell.close_tab.assert_awaited_once_with("project", "s")
    shell.client.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_close_missing_and_background_tabs(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.tabs = [dict(id="s", workspace="current"), dict(id="s", workspace="other")]
    shell.controller.forget = Mock()
    shell.switch_project = AsyncMock()
    await shell.close_tab("missing", "s")
    assert len(shell.tabs) == 2
    shell.controller.forget.assert_not_called()
    await shell.close_tab("other", "s")
    assert shell.tabs == [dict(id="s", workspace="current")]
    shell.controller.forget.assert_called_once_with("s")


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
async def test_failed_new_session_keeps_selector_open(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch)
    shell.controller.agent_name = "code"
    shell.close_selector = AsyncMock()
    shell.switch = AsyncMock(side_effect=RuntimeError("session unavailable"))
    with pytest.raises(RuntimeError, match="session unavailable"):
        await shell.command("/new", ("fresh",))
    shell.close_selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_edge_cases_use_shared_contract(tmp_path, monkeypatch):
    rows = [SimpleNamespace(id="alpha-1"), SimpleNamespace(id="beta-2")]
    diff = SimpleNamespace(patch="", truncated=False)
    shell = _shell(tmp_path, monkeypatch, list_sessions=AsyncMock(return_value=rows),
                   fork=AsyncMock(return_value=SimpleNamespace(id="f")), git_diff=AsyncMock(return_value=diff),
                   inspect_context=AsyncMock(side_effect=RuntimeError("busy while the session is active")))
    shell.switch = AsyncMock()
    await shell.command("/sessions", ("beta",))
    shell.switch.assert_awaited_once_with("beta-2")
    await shell.command("/sessions", ("zzz",))
    assert shell.notice == "No session matching zzz"
    await shell.command("/fork", ("nope",))
    shell.controller.client.fork.assert_awaited_with("s", None)
    await shell.command("/diff", ())
    shell.controller.client.git_diff.assert_awaited_once_with(staged=False, ref="", session="s")
    assert shell.panel_lines == ["No changes"] or "No changes" in "\n".join(shell.panel_lines)
    with pytest.raises(ValueError, match="/diff"):
        await shell.command("/diff", ("a", "b"))
    await shell.command("/tools", ())
    assert shell.notice == "No tools used in this transcript"
    await shell.command("/tasks", ())
    assert shell.notice == "No subagents"
    with pytest.raises(ValueError, match="/theme dark"):
        await shell.command("/theme", ("purple",))
    with pytest.raises(ValueError, match="unavailable"):
        await shell.command("/copy", ())


class _Preview(msgspec.Struct):
    system: str
    nested: dict


@pytest.mark.asyncio
async def test_copy_falls_back_to_terminal_clipboard_over_ssh(tmp_path, monkeypatch):
    """/copy used to fail over SSH / without pbcopy even though OSC 52 works."""
    from nexus.ui.ratatui import desktop
    preview = _Preview(system="héllo", nested={"a": [1]})
    shell = _shell(tmp_path, monkeypatch, inspect_context=AsyncMock(return_value=preview))
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    sent = []
    monkeypatch.setattr(desktop, "copy_via_terminal", sent.append)
    await shell.command("/copy", ())
    assert shell.notice == "Requested terminal copy of context JSON"
    assert json.loads(sent[0]) == {"system": "héllo", "nested": {"a": [1]}}


@pytest.mark.asyncio
async def test_copy_uses_desktop_clipboard_and_cached_preview_during_a_turn(tmp_path, monkeypatch):
    from nexus.ui.ratatui import desktop
    shell = _shell(tmp_path, monkeypatch, inspect_context=AsyncMock(side_effect=RuntimeError("refused while the session is active")))
    shell.preview = _Preview(system="x", nested={})
    copied = []

    async def fake(text):
        copied.append(text)
    monkeypatch.setattr(desktop, "copy_text", fake)
    await shell.command("/copy", ())
    assert shell.notice == "Copied context JSON" and json.loads(copied[0])["system"] == "x"


@pytest.mark.asyncio
async def test_copy_reports_when_no_clipboard_path_exists(tmp_path, monkeypatch):
    from nexus.ui.ratatui import desktop
    shell = _shell(tmp_path, monkeypatch, inspect_context=AsyncMock(return_value=_Preview(system="x", nested={})))
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    monkeypatch.setattr(desktop, "copy_via_terminal", Mock(side_effect=ValueError("No terminal available for clipboard copy")))
    with pytest.raises(ValueError, match="SSH.*No terminal"):
        await shell.command("/copy", ())


@pytest.mark.asyncio
async def test_copy_text_reports_missing_executable_and_timeout(monkeypatch):
    import asyncio
    from nexus.ui.ratatui import desktop
    for name in ("SSH_TTY", "SSH_CONNECTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("pbcopy")))
    with pytest.raises(ValueError, match="No local clipboard command"):
        await desktop.copy_text("x")

    process = Mock(returncode=None)
    process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    process.wait = AsyncMock()
    monkeypatch.setattr(desktop.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(ValueError, match="timed out"):
        await desktop.copy_text("x")
    process.kill.assert_called_once()


def test_copy_via_terminal_writes_osc52(tmp_path, monkeypatch):
    import base64
    from nexus.ui.ratatui import desktop
    real_open = open
    target = tmp_path / "tty"
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: real_open(target if path == "/dev/tty" else path, *a, **k))
    desktop.copy_via_terminal("héllo")
    assert target.read_text() == "\x1b]52;c;" + base64.b64encode("héllo".encode()).decode() + "\x07"
    with pytest.raises(ValueError, match="too large"):
        desktop.copy_via_terminal("x" * (desktop.OSC52_LIMIT + 1))


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


def test_shared_shortcuts_have_native_keymap_coverage():
    import importlib.util
    from pathlib import Path

    from tests.test_desktop_keymap_parity import NATIVE_ONLY_LEADER

    spec = importlib.util.spec_from_file_location(
        "shared_shortcuts", Path("nexus/ui_support/shortcuts.py")
    )
    shortcuts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shortcuts)

    source = Path("rust/desktop/src/keymap.rs").read_text()
    bindings = set(__import__("re").findall(r'KeyBinding::new\("([^\"]+)"', source))
    # `enter` is bound by the shared editor (input.rs), not the shell keymap.
    baseline = {
        "ctrl+enter": "ctrl-enter", "alt+enter": "alt-enter",
        "ctrl+p": "ctrl-p", "ctrl+n": "ctrl-n", "ctrl+o": "ctrl-o",
        "ctrl+g": "ctrl-g", "ctrl+b": "ctrl-b",
        "ctrl+l": "ctrl-l", "ctrl+s": "ctrl-s", "ctrl+i": "ctrl-i",
        "ctrl+t": "ctrl-t", "ctrl+space": "ctrl-space", "ctrl+e": "ctrl-e",
        "ctrl+u": "ctrl-u", "ctrl+c": "ctrl-c", "ctrl+r": "ctrl-r",
        "ctrl+q": "ctrl-q", "escape": "escape",
    }
    for key, _, _ in shortcuts.SHORTCUTS:
        if key in baseline:
            if key == "ctrl+f":
                # The terminal implements fork_session; no equivalent native
                # action is currently declared in main.rs.
                assert baseline[key] not in bindings
            else:
                assert baseline[key] in bindings, f"{key} is missing from native keymap"
    for letter, action_id, _ in shortcuts.LEADER_SHORTCUTS:
        if letter in NATIVE_ONLY_LEADER:
            continue  # native-only alias (plan 10); the desktop binds the primary key
        assert f"ctrl-x {letter}" in bindings, f"Ctrl+X {letter} is missing from native keymap"


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
    assert "out of date" in shell.panel_lines[0] and shell.panel_tones[0] == "bad"  # cached numbers are not claimed fresh


@pytest.mark.asyncio
async def test_usage_open_fetches_again_even_with_a_fresh_cache(tmp_path, monkeypatch):
    fetch = AsyncMock(return_value={"providers": [{"id": "codex", "label": "Codex"}], "fetched_at": 1})
    shell = _shell(tmp_path, monkeypatch, providers_usage=fetch)
    shell.on_update = AsyncMock()
    shell.usage_cache = {"providers": [{"label": "Cached"}], "fetched_at": 9e9}
    shell.open_usage()
    assert shell.panel_loading and "Cached" in shell.panel_lines
    await shell.usage_task
    fetch.assert_awaited_once()
    assert "Codex" in shell.panel_lines and not shell.panel_loading


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
async def test_usage_modal_dismisses_while_its_fetch_is_pending(tmp_path, monkeypatch):
    import asyncio
    from nexus.ui.ratatui.prototype import dismiss

    gate = asyncio.Event()

    async def fetch():
        await gate.wait()
        return {"providers": [{"label": "Fresh"}]}

    shell = _shell(tmp_path, monkeypatch, providers_usage=AsyncMock(side_effect=fetch))
    shell.on_update = AsyncMock()
    await shell.command("/usage", ())
    assert shell.panel_loading and not shell.usage_task.done()
    assert await dismiss(shell) is True
    assert shell.panel_title == "" and not shell.panel_loading
    gate.set()
    await shell.usage_task
    assert shell.panel_title == "" and shell.panel_lines == []
    shell.on_update.assert_not_awaited()

    # Any other loading panel still ignores dismiss.
    shell.show("Models", "x")
    shell.panel_loading = True
    assert await dismiss(shell) is False
    assert shell.panel_title == "Models" and shell.panel_loading


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
        from nexus.view.model import TurnView
        shell.controller.view = replace(shell.controller.view, last_seq=8, turns=[TurnView(id="t1")])  # has history
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


@pytest.mark.asyncio
async def test_submit_reuses_identity_after_lost_reply():
    client = SimpleNamespace(enqueue=AsyncMock(side_effect=[ConnectionError("lost reply"), None, None]))
    shell = ShellActions(SimpleNamespace(client=client, session="s"))
    with pytest.raises(ConnectionError):
        await shell.submit("hello")
    await shell.submit("hello")
    await shell.submit("hello")
    ids = [call.kwargs["client_request_id"] for call in client.enqueue.await_args_list]
    assert ids[0] == ids[1] and ids[2] != ids[1]


async def test_failed_project_open_toasts_the_redacted_reason_and_keeps_the_dashboard():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from nexus.errors import ConfigError
    from nexus.ui.ratatui.actions import ProjectOpenError, ShellActions
    from nexus.host_support.errors import error_text

    shell = ShellActions.__new__(ShellActions)
    shell._switch_lock = asyncio.Lock()
    shell.workspace = "/here"
    shell.attachment_draft_key = ("/here", "s")
    shell.usage_cache = None
    shell.usage_task = None
    shell.workflows = SimpleNamespace(close_dashboard=lambda: pytest.fail("dashboard must stay up"))
    flashed = []
    shell.flash = lambda text, level=None, **kw: flashed.append((text, level))
    shell.switch = AsyncMock()
    shell.controller = SimpleNamespace(session="s", client=SimpleNamespace(open_project_session=AsyncMock(
        side_effect=ConfigError("unknown key [v2] sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"))))

    with pytest.raises(ProjectOpenError):
        await shell.switch_project("/other", "s2")
    assert len(flashed) == 1 and flashed[0][1] == "error"
    assert "/other" in flashed[0][0] and "unknown key" in flashed[0][0]
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in flashed[0][0]
    shell.switch.assert_not_awaited()

    # An exception without a message (a timeout) still names itself.
    assert error_text(TimeoutError()) == "TimeoutError"


async def test_project_open_prompts_for_a_path_and_creates_a_fresh_session_there():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.workflows import Workflows

    shell = ShellActions.__new__(ShellActions)
    shell._switch_lock = asyncio.Lock()
    shell.workspace = "/here"
    shell.attachment_draft_key = ("/here", "s")
    shell.usage_cache = None
    shell.usage_task = None
    shell.controller = SimpleNamespace(session="s")
    shell.controller.client = SimpleNamespace(open_project_session=AsyncMock(return_value=SimpleNamespace(
        project=SimpleNamespace(root="/new", key="k"), session="abc123")))
    shell.switch = AsyncMock()
    shell.sidebar = SimpleNamespace(enabled=True, close=AsyncMock())
    flows = Workflows.__new__(Workflows)
    flows.shell = shell
    flows.edit = Mock()
    flows.form = {"status": ""}
    flows.form_target = {"kind": "project_open_form"}
    flows.form_id = None
    flows.stack = []
    shell.panel_title = "Open project"
    shell.panel_revision = 0
    shell.workflows = flows

    await flows._operate({"kind": "project_open"})
    assert flows.edit.call_args.args[2] == {"kind": "project_open_form"}

    flows.form = {"id": "f", "status": "", "revision": 0, "body": ""}
    await flows.save("f", "x" * 5000, 1)
    assert "4096" in flows.form["status"]
    shell.client.open_project_session.assert_not_awaited()

    await flows.save("f", "  /new\n", 2)
    shell.client.open_project_session.assert_awaited_once_with("/new", "", create=True)
    shell.switch.assert_awaited_once_with("abc123", project="k")
    shell.sidebar.close.assert_awaited_once()
    assert shell.workspace == "/new"


def _tab_shell(tmp_path, monkeypatch, current="new1"):
    shell = _shell(tmp_path, monkeypatch)
    shell.workspace = "w"
    shell.tabs = [dict(id="old", workspace="w"), dict(id="new1", workspace="w")]
    shell.controller.session = current
    shell.controller.forget = Mock()
    shell.controller.running = False
    shell.focus.report = Mock()
    return shell


@pytest.mark.asyncio
async def test_leaving_an_empty_new_tab_closes_it(tmp_path, monkeypatch):
    shell = _tab_shell(tmp_path, monkeypatch)
    await shell.switch("old")
    assert [row["id"] for row in shell.tabs] == ["old"]
    shell.controller.forget.assert_called_once_with("new1")


@pytest.mark.asyncio
async def test_leaving_a_tab_with_content_or_a_draft_keeps_it(tmp_path, monkeypatch):
    from nexus.view.model import TurnView
    shell = _tab_shell(tmp_path, monkeypatch)
    shell.note_draft_state(True)  # unsent composer text
    await shell.switch("old")
    assert [row["id"] for row in shell.tabs] == ["old", "new1"]
    shell = _tab_shell(tmp_path, monkeypatch)
    shell.controller.view.turns.append(TurnView(id="t1"))  # a message was sent
    await shell.switch("old")
    assert [row["id"] for row in shell.tabs] == ["old", "new1"]
    shell = _tab_shell(tmp_path, monkeypatch)
    shell.attachments.append(SimpleNamespace(attachment_id="a", kind="image"))
    await shell.switch("old")
    assert len(shell.tabs) == 2


@pytest.mark.asyncio
async def test_draft_state_is_per_session_and_cleared_by_emptying(tmp_path, monkeypatch):
    shell = _tab_shell(tmp_path, monkeypatch)
    shell.note_draft_state(True)
    shell.note_draft_state(False)  # the user deleted the text again
    await shell.switch("old")
    assert [row["id"] for row in shell.tabs] == ["old"]


@pytest.mark.asyncio
async def test_merge_command_sends_instruction_only_in_a_worktree_session(tmp_path, monkeypatch):
    from nexus.ui.cli.commands import SPECS
    assert "/merge" in {spec.name for spec in SPECS}
    from nexus.view.model import RootView
    shell = _shell(tmp_path, monkeypatch, enqueue=AsyncMock())
    shell.controller.view.root = RootView(kind="worktree", path_display="wt/wt", branch="nexus/wt/wt",
                                          base_ref="main", worktree_id="wt-1")
    assert await shell.submit("/merge ship it") is True
    text = shell.client.enqueue.await_args.args[1]
    assert shell.client.enqueue.await_args.args[0] == "s"
    assert "into main" in text and 'merge --ff-only' in text and text.endswith("Note from the user: ship it")
    assert "plain Git" in text and "no Nexus CLI merge command exists or is required" in text
    assert "nexus merge" not in text
    shell = _shell(tmp_path, monkeypatch, enqueue=AsyncMock())
    await shell.submit("/merge")
    shell.client.enqueue.assert_not_awaited()
    assert "main checkout" in shell.notice


@pytest.mark.asyncio
async def test_removed_worktree_commands_and_ask_modal(tmp_path, monkeypatch):
    from nexus.ui.cli.commands import SPECS
    names = {spec.name for spec in SPECS}
    assert "/worktree" not in names and "/worktrees" in names
    enqueue = AsyncMock(return_value=("q", "t"))
    shell = _shell(tmp_path, monkeypatch, enqueue=enqueue)
    assert await shell.submit("fix the bug") is True  # no worktree question before the first message
    enqueue.assert_awaited_once()
    assert shell.workflows.shell.panel_title == ""


def _git_repo(root, branch):
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return str(root)


@pytest.mark.asyncio
async def test_switch_starts_cancellable_top_bar_refresh_and_notifies_on_change(tmp_path, monkeypatch):
    import asyncio
    shell = _tab_shell(tmp_path, monkeypatch)
    shell.workspace = _git_repo(tmp_path / "w", "topic")
    shell.breadcrumb = shell.workspace + " › main"  # same workspace: labels stay until the read lands
    shell.branch = "main"
    shell.on_update = AsyncMock()
    await shell.switch("old")
    assert shell.branch == "main"  # no flicker: switch itself never waits for the read
    await shell.branch_task
    assert shell.branch == "topic" and shell.breadcrumb.endswith("› topic")
    shell.on_update.assert_awaited_once()
    await shell.switch("old")  # nothing changed: no repaint
    await shell.branch_task
    shell.on_update.assert_awaited_once()
    await shell.switch("old")
    stale = shell.branch_task
    await shell.switch("old")
    assert shell.branch_task is not stale
    await asyncio.sleep(0)
    assert stale.cancelled() or stale.done()
    await shell.aclose_branch_task()
    assert shell.branch_task is None


@pytest.mark.asyncio
async def test_switch_to_another_project_resets_top_bar_immediately(tmp_path, monkeypatch):
    shell = _tab_shell(tmp_path, monkeypatch)
    shell.workspace = _git_repo(tmp_path / "b", "feature")
    shell.breadcrumb = str(tmp_path / "a") + " › main"
    shell.branch, shell.worktree, shell.worktree_kind = "main", "⎇ wt", "worktree"
    await shell.switch("old")
    assert (shell.breadcrumb, shell.branch, shell.worktree, shell.worktree_kind) == (shell.workspace, "", "", "")
    await shell.branch_task
    assert shell.breadcrumb == f"{shell.workspace} › feature"


@pytest.mark.asyncio
async def test_refresh_branch_discards_result_when_session_or_generation_moved(tmp_path, monkeypatch):
    import asyncio
    import threading
    from nexus.ui.ratatui.prototype import refresh_branch
    gate = threading.Event()

    def slow_head(workspace):
        gate.wait(5)
        return {"branch": "late"}
    monkeypatch.setattr("nexus.host_support.git_head.git_head", slow_head)
    view = initial_state("s1")
    shell = SimpleNamespace(workspace=str(tmp_path), breadcrumb="", branch="", worktree="",
                            worktree_kind="", generation=1, controller=SimpleNamespace(session="s1", view=view))
    task = asyncio.create_task(refresh_branch(shell))
    await asyncio.sleep(0.1)
    shell.controller.session = "s2"  # same workspace, other tab
    gate.set()
    assert await task is False
    assert (shell.breadcrumb, shell.branch, shell.worktree, shell.worktree_kind) == ("", "", "", "")
    gate.clear()
    task = asyncio.create_task(refresh_branch(shell))
    await asyncio.sleep(0.1)
    shell.generation = 2
    gate.set()
    assert await task is False and not shell.branch
