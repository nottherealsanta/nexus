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
async def test_voice_finish_inserts_final_not_partial(shell, monkeypatch):
    from nexus.ui.ratatui import voice
    recorder = SimpleNamespace(start=lambda: None, stop=lambda: b"wav", snapshot=lambda: b"wav", full=False, duration=0)
    monkeypatch.setattr(voice, "Recorder", lambda **kwargs: recorder)
    shell.client.voice_status = AsyncMock(return_value=p.VoiceStatusResult(enabled=True, state="ready"))
    shell.client.voice_cancel = AsyncMock()
    async def transcribe(audio, request_id, **kwargs):
        return p.VoiceTranscribeResult(request_id=request_id, text="final transcript", duration_s=1, elapsed_s=.1)
    shell.client.voice_transcribe = AsyncMock(side_effect=transcribe)
    await shell.voice.start()
    assert shell.voice.phase == "recording"
    await shell.voice.stop()
    assert shell.composer_insert == "final transcript"
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
