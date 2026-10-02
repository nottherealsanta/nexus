"""Native workflow correctness at the host boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))


@pytest.mark.asyncio
async def test_settings_save_advances_hash_and_preserves_conflicting_draft(shell):
    shell.client.settings_read = AsyncMock(return_value=p.SettingsReadResult(body="old", rel_path="SOUL.md", builtin=False, sha256="old-hash"))
    shell.client.settings_write = AsyncMock(side_effect=[p.SettingsWriteResult(status="saved", sha256="new-hash"), p.SettingsWriteResult(status="conflict")])
    await shell.workflows.operate({"kind": "settings_read", "scope": "project", "category": "soul", "id": "SOUL.md"})
    form = shell.workflows.form
    await shell.workflows.save(form["id"], "new", 1)
    assert shell.workflows.form_target["sha256"] == "new-hash"
    await shell.workflows.save(form["id"], "unsaved", 2)
    assert form["body"] == "unsaved"
    assert not form["autosave"]
    assert "Conflict" in form["status"]
    assert shell.client.settings_write.await_args.kwargs["expected_sha256"] == "new-hash"


@pytest.mark.asyncio
async def test_stale_form_does_not_save(shell):
    shell.client.settings_write = AsyncMock()
    shell.workflows.edit("Edit", "body", {"kind": "settings_read", "scope": "project", "category": "soul", "id": "SOUL.md", "sha256": "h"})
    await shell.workflows.save("wrong", "bad", 1)
    shell.client.settings_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_secret_not_returned_in_snapshot(shell):
    shell.client.provider_key_set = AsyncMock()
    shell.client.providers_status = AsyncMock(return_value=p.ProvidersStatusResult())
    await shell.workflows.operate({"kind": "provider_key", "id": "openai"})
    form = shell.workflows.form
    await shell.workflows.save(form["id"], "test-secret", 1)
    shell.client.provider_key_set.assert_awaited_once_with("openai", "test-secret")
    assert shell.workflows.form is None
    assert "test-secret" not in repr(shell.panel_lines)


@pytest.mark.asyncio
async def test_failed_submit_keeps_draft_and_attachments(shell):
    shell.client.enqueue = AsyncMock(side_effect=ValueError("unavailable"))
    shell.attachments = [p.AttachmentPrepareResult(attachment_id="a", name="a.txt", kind="document", preview="a")]
    with pytest.raises(ValueError):
        await shell.submit("preserve me")
    assert shell.composer_restore == "preserve me"
    assert shell.attachments[0].attachment_id == "a"


@pytest.mark.asyncio
async def test_settings_validation_keeps_editor_and_disables_autosave(shell):
    shell.client.settings_write = AsyncMock(side_effect=ValueError("invalid config"))
    shell.workflows.edit("Config", "old", {"kind": "settings_read", "scope": "project", "category": "config", "id": "nexus.toml", "sha256": "h"}, autosave=True)
    form = shell.workflows.form
    with pytest.raises(ValueError):
        await shell.workflows.save(form["id"], "invalid", 1)
    assert form["body"] == "invalid"
    assert not form["saved"]
    assert not form["autosave"]


@pytest.mark.asyncio
@pytest.mark.parametrize("send", [False, True])
async def test_voice_finish_inserts_final_not_partial(shell, monkeypatch, send):
    from nexus.ui.ratatui import voice
    recorder = SimpleNamespace(start=lambda: None, stop=lambda: b"wav", snapshot=lambda: b"wav", full=False, duration=0)
    monkeypatch.setattr(voice, "Recorder", lambda **kwargs: recorder)
    shell.client.voice_status = AsyncMock(return_value=p.VoiceStatusResult(enabled=True, state="ready"))
    shell.client.voice_cancel = AsyncMock()
    async def transcribe(audio, request_id, **kwargs):
        return p.VoiceTranscribeResult(request_id=request_id, text="final transcript", duration_s=1, elapsed_s=.1)
    shell.client.voice_transcribe = AsyncMock(side_effect=transcribe)
    await shell.voice.open()
    assert shell.panel_title == ""
    assert shell.voice.phase == "recording"
    await shell.voice.stop(send=send)
    assert shell.composer_insert == "final transcript"
    assert shell.composer_auto_send is send
    assert shell.voice.phase == "idle"
    assert not shell.client.voice_transcribe.await_args.kwargs.get("partial", False)


def test_preferences_preserve_textual_keys_and_bound_favorites(tmp_path):
    import json
    from nexus.ui.ratatui.preferences import Preferences
    path = tmp_path / "tui.json"
    path.write_text(json.dumps({"theme": "nexus-light", "sessions_sidebar": False, "model_favorites": ["p/m"]}))
    prefs = Preferences(path)
    assert prefs.values["theme"] == "nexus-light"
    prefs.set("model_favorites", ["p/m"] * 200)
    assert prefs.values["model_favorites"] == ["p/m"]
    assert json.loads(path.read_text())["sessions_sidebar"] is False


@pytest.mark.asyncio
async def test_force_discard_requires_host_issued_confirmation(shell):
    shell.client.discard_worktree = AsyncMock(side_effect=[
        SimpleNamespace(status="requires_confirmation", confirmation_token="host-token", impact={"removed": "child files"}),
        SimpleNamespace(status="discarded")])
    await shell.workflows.operate({"kind": "worktree_force_discard", "id": "child"})
    assert shell.client.discard_worktree.await_count == 1
    operation = shell.items[1]["operation"]
    assert operation["token"] == "host-token"
    await shell.workflows.operate(operation)
    assert shell.client.discard_worktree.await_args.kwargs == {"force": True, "confirmation_token": "host-token"}


@pytest.mark.asyncio
async def test_removing_attachment_keeps_existing_numbered_references(shell):
    first = SimpleNamespace(attachment_id="a", kind="image", name="first.png")
    second = SimpleNamespace(attachment_id="b", kind="image", name="second.png")
    shell.attachments = [first, second]
    assert shell.attachment_label(0) == "image 1"
    assert shell.attachment_label(1) == "image 2"
    await shell.workflows.operate({"kind": "attachment_remove", "id": "a"})
    assert shell.attachment_label(0) == "image 2"


@pytest.mark.asyncio
async def test_verbose_toggles_tool_output_without_changing_context_preview(shell):
    preview = shell.preferences.values["context_preview"]
    await shell.command("/verbose", ())
    assert shell.verbose
    assert shell.preferences.values["context_preview"] == preview


@pytest.mark.asyncio
async def test_native_reconnect_replays_and_follows_idle_events():
    from nexus.client.protocol import ClientError
    from nexus.ui.ratatui.controller import NativeController
    async def lost(*args, **kwargs):
        raise ClientError("lost")
        yield
    async def idle_events(*args, **kwargs):
        yield "event from another surface"
    old = SimpleNamespace(stream=lost, aclose=AsyncMock())
    replacement = SimpleNamespace(stream=idle_events, aclose=AsyncMock())
    controller = NativeController(old, "s")
    controller.reconnect = AsyncMock(return_value=replacement)
    controller.bootstrap = AsyncMock()
    callback = AsyncMock()
    await controller._follow("s", callback)
    assert [call.args[0] for call in callback.await_args_list] == [None, False, "event from another surface"]
    old.aclose.assert_awaited_once()
    controller.bootstrap.assert_awaited_once()
    assert controller.client is replacement


@pytest.mark.asyncio
async def test_turn_collapse_toggle_is_session_presentation(shell):
    await shell.workflows.operate({"kind": "turn_toggle", "id": "turn"})
    assert "turn" in shell.collapsed_turns
    await shell.workflows.operate({"kind": "turn_toggle", "id": "turn"})
    assert not shell.collapsed_turns


@pytest.mark.asyncio
async def test_logs_page_cursors_fold_and_session_reset(shell):
    entry = lambda source, seq, level: p.LogEntry(source=source, seq=seq, ts=float(seq), level=level, kind="event", summary="detail")
    shell.client.read_logs = AsyncMock(side_effect=[
        p.LogsReadResult(daemon=p.DaemonLogPage(entries=[entry("daemon", 1, "info")], next_cursor="next", has_more=True),
            session=p.SessionLogPage(entries=[entry("session", 2, "error")], next_cursor=2)),
        p.LogsReadResult(daemon=p.DaemonLogPage(entries=[entry("daemon", 1, "info")], next_cursor="next"),
            session=p.SessionLogPage(entries=[], next_cursor=2))])
    await shell.logs.poll()
    assert shell.client.read_logs.await_args.kwargs["daemon_cursor"] == "next"
    assert shell.client.read_logs.await_args.kwargs["session_cursor"] == 2
    assert len(shell.logs.rows["daemon"]) == 1
    assert "[ERROR]" in "\n".join(shell.logs.lines())
    assert "[INFO]" not in "\n".join(shell.logs.lines())
    shell.logs.show_all = True
    assert "[INFO]" in "\n".join(shell.logs.lines())
    shell.logs.reset_session()
    assert not shell.logs.rows["session"]
    assert shell.logs.cursors["session"] is None
    assert shell.logs.rows["daemon"]


@pytest.mark.asyncio
async def test_log_page_from_previous_session_is_discarded(shell):
    async def changed(*args, **kwargs):
        shell.generation += 1
        return p.LogsReadResult(daemon=p.DaemonLogPage(next_cursor="old"), session=p.SessionLogPage(next_cursor=10))
    shell.client.read_logs = changed
    await shell.logs.poll()
    assert shell.logs.cursors == {"daemon": None, "session": None}


@pytest.mark.asyncio
async def test_model_effort_commit_uses_guarded_shared_controller(shell):
    shell.controller.select_model_and_effort = AsyncMock()
    shell.client.inspect_context = AsyncMock(return_value="preview")
    await shell.workflows.operate({"kind": "model_choose", "ref": "p/m", "levels": ["high"]})
    operation = shell.items[-1]["operation"]
    await shell.workflows.operate(operation)
    shell.controller.select_model_and_effort.assert_awaited_once_with("p/m", "high")
    assert shell.preferences.values["model_recent"][0] == "p/m"
    assert not shell.panel_title


@pytest.mark.asyncio
async def test_voice_config_retries_a_host_hash_conflict(shell):
    from nexus.ui_support.voice_settings import set_voice_config
    shell.client.settings_read = AsyncMock(side_effect=[
        p.SettingsReadResult(body="config_version = 2\n", rel_path="config.toml", builtin=False, sha256="old"),
        p.SettingsReadResult(body="config_version = 2\n[voice]\nauto_send = true\n", rel_path="config.toml", builtin=False, sha256="new")])
    shell.client.settings_write = AsyncMock(side_effect=[p.SettingsWriteResult(status="conflict"), p.SettingsWriteResult(status="saved")])
    await set_voice_config(shell.client, enabled=True)
    assert shell.client.settings_write.await_count == 2
    args = shell.client.settings_write.await_args.args
    assert args[-1] == "new"
    assert "auto_send = true" in args[3]
    assert "enabled = true" in args[3]


@pytest.mark.asyncio
async def test_tools_dialog_groups_expands_and_opens_a_definition(shell):
    tools = [{"name": "read", "group": "files", "description": "Read a file", "parameters": {"type": "object"}},
             {"name": "write", "group": "files", "description": "Write a file", "parameters": {"type": "object"}},
             {"name": "mcp__fs__list", "description": "List", "parameters": {"type": "object"}}]
    shell.preview = SimpleNamespace(tools=tools, tools_supported=True)
    shell.refresh_preview = AsyncMock(return_value=True)
    await shell.workflows.operate({"kind": "context_show", "key": "tools"})
    labels = [item["label"] for item in shell.items]
    assert shell.panel_title.startswith("Tools · 3 definitions · ~")
    assert labels[0].startswith("▸ ■ files") and "read  write" in labels[0] and labels[1].startswith("▸ ■ fs")
    assert labels[-1] == "Edit tools…"
    await shell.workflows.operate(shell.items[0]["operation"])
    labels = [item["label"] for item in shell.items]
    assert labels[0].startswith("▾ ■ files") and labels[1].strip().startswith("read")
    await shell.workflows.operate(shell.items[1]["operation"])
    assert shell.panel_title.startswith("Tool · read · ~") and "Read a file" in "\n".join(shell.panel_lines)


@pytest.mark.asyncio
async def test_context_click_while_a_turn_runs_says_so(shell):
    shell.refresh_preview = AsyncMock(return_value=False)
    await shell.workflows.operate({"kind": "context_show", "key": "tools"})
    assert "unavailable while a turn is running" in shell.notice


@pytest.mark.asyncio
async def test_layout_and_appearance_toggle_and_reset_to_defaults(shell):
    await shell.workflows.operate({"kind": "layout"})
    assert [item["label"] for item in shell.items][:2] == ["Sessions sidebar  ctrl+b · on", "Details sidebar  ctrl+l · on"]
    await shell.workflows.operate(shell.items[0]["operation"])
    assert shell.preferences.values["sessions_sidebar"] is False
    assert shell.items[0]["label"].endswith("· off")
    await shell.workflows.operate(shell.items[-1]["operation"])  # Reset to default
    assert shell.preferences.values["sessions_sidebar"] is True
    await shell.workflows.operate({"kind": "theme", "value": "nexus-light"})
    assert shell.panel_title == "Appearance" and shell.items[1]["label"] == "Light · selected"
    await shell.workflows.operate(shell.items[-1]["operation"])
    assert shell.preferences.values["theme"] == "nexus-dark"


@pytest.mark.asyncio
async def test_settings_pages_carry_the_area_list_and_switching_replaces_the_page(shell):
    from nexus.ui.ratatui.prototype import _settings_nav

    shell.client.settings_inventory = AsyncMock(return_value=p.SettingsInventoryResult(scope="global", categories=[], items=[], root_display="~/.nexus"))
    assert _settings_nav(shell) is None
    await shell.workflows.operate({"kind": "layout"})
    nav = _settings_nav(shell)
    labels = [item[0] for item in nav["items"]]
    assert labels[:3] == ["GENERAL", "Appearance", "Layout"] and nav["items"][nav["selected"]][1] == "layout"
    assert nav["items"][0][2] is True  # a heading
    await shell.workflows.settings_area("appearance")
    assert shell.panel_title == "Appearance" and _settings_nav(shell)["items"][_settings_nav(shell)["selected"]][1] == "appearance"
    assert shell.workflows.stack == []
    await shell.workflows.settings_area("tools")
    assert shell.panel_title == "Settings · global · tools" and _settings_nav(shell)["items"][_settings_nav(shell)["selected"]][1] == "tools"
    shell.refresh_preview = AsyncMock(return_value=True)
    shell.preview = SimpleNamespace(system_text="prompt")
    await shell.workflows.operate({"kind": "context_show", "key": "system"})
    assert _settings_nav(shell) is None  # a non-Settings panel does not keep the area list


@pytest.mark.asyncio
async def test_agents_document_uses_markdown_and_preserves_newlines(shell):
    preview = p.ContextInspectResult(session="s", included_parts=[
        {"name": "agents_md", "text": "# Project rules\n\n- Keep context visible\n\n```py\nx = 1\n```"}],
        system_files={"agents": {"source": "/workspace/AGENTS.md"}})
    shell.client.inspect_context = AsyncMock(return_value=preview)
    await shell.workflows.operate({"kind": "context_show", "key": "agents"})
    assert shell.panel_format == "markdown" and shell.panel_layout == "modal"
    assert "/workspace/AGENTS.md" in shell.panel_title
    assert "\n".join(shell.panel_lines) == preview.included_parts[0]["text"]
    shell.workflows.menu("Next", [("Back", {"kind": "back"})])
    shell.workflows.back()
    assert shell.panel_format == "markdown" and shell.panel_layout == "modal"


@pytest.mark.asyncio
async def test_voice_settings_never_starts_capture_and_saves_options(shell):
    shell.client.voice_status = AsyncMock(return_value=p.VoiceStatusResult(enabled=True, state="ready", configured_device="cpu"))
    shell.client.settings_read = AsyncMock(return_value=p.SettingsReadResult(body="config_version = 2\n", rel_path="config.toml", builtin=False, sha256="h"))
    shell.client.settings_write = AsyncMock(return_value=p.SettingsWriteResult(status="saved"))
    await shell.workflows.settings_area("voice")
    assert shell.voice.phase == "idle"
    assert shell.settings_nav == "voice"
    assert any("Processing device · cpu" in item["label"] for item in shell.items)
    await shell.workflows.operate(shell.items[1]["operation"])
    assert "auto_send = true" in shell.client.settings_write.await_args.args[3]
    assert shell.settings_nav == "voice"
    assert shell.voice.phase == "idle"


@pytest.mark.asyncio
async def test_provider_options_have_headings_and_connection_status(shell):
    shell.client.providers_status = AsyncMock(return_value=p.ProvidersStatusResult(providers=[
        {"id": "openai", "label": "OpenAI", "connected": True, "methods": ["api_key"]}]))
    await shell.workflows.settings_area("providers")
    await shell.workflows.operate(shell.items[0]["operation"])
    assert shell.panel_lines[0] == "Connection: connected"
    assert [item["group"] for item in shell.items] == ["Sign-in options", "Connection management"]
    assert shell.settings_nav == "providers"


async def test_subagent_page_context_nested_back_and_child_tool_details(shell):
    from nexus.view import initial_state
    from nexus.view.model import AgentView, TurnView, ToolCallView
    body = initial_state("child")
    body.turns = [TurnView(id="ct", tools=[ToolCallView(call_id="read-child", name="Read", status="failed", error="missing")])]
    nested = AgentView(id="nested", body=initial_state("nested"))
    body.agents = {"nested": nested}
    root = initial_state("s")
    root.agents = {"child": AgentView(id="child", body=body)}
    shell.controller.view = root
    shell.client.agent_transcript = AsyncMock(return_value={"found": True, "context": {"system_text": "Child system"}})
    await shell.workflows.operate({"kind": "agent_page", "id": "child"})
    assert shell.panel_title == "" and shell.workflows.active_view is body
    await shell.workflows.operate({"kind": "context_show", "key": "system"})
    assert shell.panel_lines == ["Child system"]
    shell.workflows.back()
    assert shell.workflows.agent_page_id == "child"
    await shell.workflows.operate({"kind": "tool_page", "id": "read-child"})
    assert shell.panel_title == "Read"
    shell.workflows.back()
    await shell.workflows.operate({"kind": "agent_page", "id": "nested"})
    assert shell.workflows.selected_agent is nested
    shell.workflows.back()
    assert shell.workflows.agent_page_id == "child"
    shell.workflows.back()
    assert shell.workflows.agent_page_id is None


async def test_subagent_page_rejects_stale_session_response(shell):
    async def fetch(session, agent):
        shell.generation += 1
        return {"found": True, "context": {"system_text": "stale"}}
    shell.client.agent_transcript = AsyncMock(side_effect=fetch)
    await shell.workflows.operate({"kind": "agent_page", "id": "child"})
    assert shell.workflows.agent_page_id is None


async def test_remembered_model_skips_effort_prompt(shell):
    shell.controller.select_model_and_effort = AsyncMock()
    shell.client.inspect_context = AsyncMock(return_value={})
    await shell.workflows.operate({"kind": "model_choose", "ref": "openai/example",
        "levels": ["low", "high"], "selected": "high", "remembered": True})
    shell.controller.select_model_and_effort.assert_awaited_once_with("openai/example", "high")
