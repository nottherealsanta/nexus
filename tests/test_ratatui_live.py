"""Native /live: command flow, consent, errors, call bar and watch (GPT_LIVE_PLAN M4)."""
from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.client.protocol import FacadeError
from nexus.host import protocol as p
from nexus.ui.cli import commands
from nexus.ui.ratatui import live as live_module
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.ui_support import live_helper
from nexus.ui_support.live_helper import LiveHelperError
from nexus.view import initial_state
from nexus.view.model import LiveCallView

FAKE = Path(__file__).parent / "fixtures" / "live" / "fake_nexus_live.py"


class FakeCall:
    """Stands in for ``LiveCall``: records the handshake, owns a fake helper."""

    instances: list[FakeCall] = []
    fail: Exception | None = None

    def __init__(self, client, session, **kwargs):
        self.client, self.session = client, session
        self.helper = SimpleNamespace(running=True, level_in=0.3, level_out=0.1, phase="listening",
                                      closed_reason=None, error="", stop=AsyncMock(), muted=False)
        self.result = None
        self.started_at = 0.0
        self.stops: list[str] = []
        FakeCall.instances.append(self)

    async def start(self, *, voice="", new_conversation=False):
        self.new_conversation = new_conversation
        if FakeCall.fail:
            raise FakeCall.fail
        self.result = SimpleNamespace(call_id="c1", answer_sdp="a", voice="sol")
        self.started_at = 1_000_000.0
        return self.result

    async def mute(self, muted=None):
        self.helper.muted = not self.helper.muted if muted is None else muted
        return SimpleNamespace(muted=self.helper.muted, state="muted" if self.helper.muted else "active")

    async def stop(self, reason="user"):
        self.stops.append(reason)
        self.helper.running = False


def status(**over):
    base = dict(available=True, reason="", state="idle", phase="", active_session="", call_id="", voice="",
                muted=False, elapsed_s=0.0, max_minutes=30, levels_in=0.0, levels_out=0.0, dropped_frames=0,
                last_error="", consent=True)
    base.update(over)
    return p.LiveStatusResult(**base)


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    FakeCall.instances, FakeCall.fail = [], None
    cues: list[str] = []
    monkeypatch.setattr(live_module, "LiveCall", FakeCall)
    monkeypatch.setattr(live_module, "play_cue", cues.append)
    monkeypatch.setattr(live_helper, "binary_path", lambda: tmp_path / "nexus-live")
    monkeypatch.setattr(live_helper, "available", lambda: True)
    client = SimpleNamespace(
        live_status=AsyncMock(return_value=status()), live_consent=AsyncMock(return_value=p.LiveConsentResult(accepted=True)),
        live_stop=AsyncMock(), live_mute=AsyncMock(), live_helper_event=AsyncMock())
    controller = SimpleNamespace(session="s", client=client, view=initial_state("s"))
    shell = ShellActions(controller)
    shell.cues = cues
    shell.live.watch_interval = 0.02
    return shell


async def settle(predicate, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.01)


def test_live_is_registered_in_command_lists_and_help():
    spec = commands.BY_NAME["/live"]
    assert "stop" in spec.usage and "mute" in spec.usage and "new" in spec.usage
    assert "/live" in commands.help_text()


def test_leader_d_toggles_live_in_both_key_maps():
    from nexus.ui_support.shortcuts import KEYBOARD_SHORTCUTS, LEADER_SHORTCUTS

    assert ("d", "toggle_live") == next((k, a) for k, a, _ in LEADER_SHORTCUTS if a == "toggle_live")
    assert any("Ctrl+X D" in line and "Live" in line for line in KEYBOARD_SHORTCUTS)
    source = Path("rust/tui/src/main.rs").read_text()
    assert """KeyCode::Char('d') => "/live",""" in source
    # Ctrl+X L is the details sidebar, so it cannot be the Live key
    assert dict((k, a) for k, a, _ in LEADER_SHORTCUTS)["l"] == "toggle_details"


async def test_start_stop_round_trip_with_cues_and_call_bar(shell):
    await shell.command("/live", ())
    call = FakeCall.instances[0]
    assert shell.live.state == "active" and shell.cues == ["start"]
    assert call.new_conversation is False
    bar = shell.live.bar()
    assert bar["active"] and bar["state"] == "active" and bar["phase"] == "listening"
    assert bar["started"] == 1_000_000.0 and bar["in"] == 0.3 and bar["out"] == 0.1 and not bar["muted"]
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    assert snapshot["live"] == bar
    await shell.command("/live", ())  # the same command hangs up
    assert call.stops == ["user"] and shell.live.state == "idle" and shell.cues == ["start", "stop"]
    assert project(shell.controller, 2, shell=shell, literal=False)["live"] == {}
    assert shell.live.watch is None


async def test_call_bar_reports_view_phase_model_mute_and_other_session(shell):
    await shell.command("/live", ())
    shell.controller.view.live_calls.append(
        LiveCallView(call_id="c1", model="gpt-live-1-codex", phase="working", status="active"))
    bar = shell.live.bar()
    assert (bar["phase"], bar["model"]) == ("working", "gpt-live-1-codex")
    await shell.command("/live", ("mute",))
    assert shell.live.bar()["phase"] == "muted" and shell.live.bar()["muted"] and shell.live.bar()["in"] == 0.0
    await shell.command("/live", ("mute",))
    assert not shell.live.bar()["muted"]
    shell.controller.session = "other"
    assert shell.live.bar()["elsewhere"] == "s"  # the mic stays visible in every session
    await shell.live.shutdown()


async def test_busy_second_start_does_not_spawn_another_helper(shell):
    await shell.command("/live", ("new",))
    assert FakeCall.instances[0].new_conversation is True
    with pytest.raises(ValueError, match="/live stop"):
        await shell.command("/live", ("new",))
    await shell.live.start()
    assert len(FakeCall.instances) == 1 and "is active" in shell.notice
    await shell.live.shutdown()


async def test_daemon_busy_names_the_owning_session(shell):
    shell.client.live_status.return_value = status(available=False, reason="busy", state="active", active_session="work")
    await shell.command("/live", ())
    assert FakeCall.instances == []
    assert shell.panel_title == "GPT Live is not available" and "work" in "\n".join(shell.panel_lines)


@pytest.mark.parametrize("reason,fix", [
    ("no_credential", "nexus auth codex login"),
    ("disabled", "enabled = true"),
    ("unsupported", "nexus-harness[live]"),
])
async def test_unavailable_reasons_show_the_fix(shell, reason, fix):
    shell.client.live_status.return_value = status(available=False, reason=reason)
    await shell.command("/live", ())
    text = "\n".join(shell.panel_lines)
    assert "Fix:" in text and fix in text and FakeCall.instances == []


async def test_missing_helper_binary_is_a_clear_unsupported_message(shell, monkeypatch):
    def missing():
        raise LiveHelperError("unsupported", "The nexus-live audio helper is not installed.", hint=live_helper.INSTALL_HINT)

    monkeypatch.setattr(live_helper, "binary_path", missing)
    await shell.command("/live", ())
    text = "\n".join(shell.panel_lines)
    assert "nexus-live" in text and "cargo build" in text and FakeCall.instances == []
    assert any(t["level"] == "error" for t in shell.toasts)


async def test_first_use_asks_for_consent_and_remembers_it(shell):
    shell.client.live_status.return_value = status(consent=False)
    await shell.command("/live", ())
    assert FakeCall.instances == [] and shell.panel_title == "Use GPT Live voice?"
    text = "\n".join(shell.panel_lines)
    assert "OpenAI" in text and "ChatGPT account" in text and "audio" in text and "summaries" in text
    shell.client.live_consent.assert_not_awaited()  # nothing is recorded by merely asking
    labels = [item["label"] for item in shell.items]
    assert labels == ["Cancel", "Agree and start the call"]
    agree = shell.items[1]["operation"]
    await shell.workflows.operate(agree)
    shell.client.live_consent.assert_awaited_once_with(True)
    assert shell.live.state == "active" and shell.panel_title == ""
    await shell.live.shutdown()


async def test_cancelling_the_consent_dialog_starts_nothing(shell):
    shell.client.live_status.return_value = status(consent=False)
    await shell.command("/live", ())
    await shell.workflows.operate(shell.items[0]["operation"])
    assert FakeCall.instances == [] and shell.live.state == "idle"
    shell.client.live_consent.assert_not_awaited()


async def test_consent_already_given_goes_straight_to_the_call(shell):
    await shell.command("/live", ())
    assert shell.panel_title != "Use GPT Live voice?" and shell.live.state == "active"
    await shell.live.shutdown()


async def test_revoke_withdraws_consent(shell):
    await shell.command("/live", ("revoke",))
    shell.client.live_consent.assert_awaited_once_with(False)
    assert "withdrawn" in shell.notice


@pytest.mark.parametrize("error,fix", [
    (FacadeError("live_auth", "ChatGPT sign-in expired"), "nexus auth codex login"),
    (FacadeError("live_forbidden", "not on this plan"), "nexus auth codex login"),
    (FacadeError("live_busy", "A call is running in session x"), ""),
    (LiveHelperError("audio_error", "The audio helper closed (audio_error).", hint=live_helper.MIC_HINT), "Microphone"),
])
async def test_start_failures_show_the_host_message_and_the_fix(shell, error, fix):
    FakeCall.fail = error
    await shell.command("/live", ())
    text = "\n".join(shell.panel_lines)
    assert shell.panel_title == "GPT Live could not start" and (getattr(error, "message", None) or str(error)) in text
    assert (fix in text) if fix else ("Fix:" not in text)
    assert shell.live.state == "idle" and shell.live.bar() is None
    FakeCall.instances[0].helper.stop.assert_awaited()  # the helper never leaks


async def test_unexpected_helper_exit_ends_the_call_with_the_microphone_fix(shell):
    await shell.command("/live", ())
    call = FakeCall.instances[0]
    call.client = shell.client
    call.helper.running, call.helper.closed_reason, call.helper.error = False, "audio_error", "permission denied"
    await settle(lambda: shell.live.state == "idle")
    shell.client.live_stop.assert_awaited_with("s", "helper_closed")
    text = "\n".join(shell.panel_lines)
    assert "audio_error" in text and "Microphone" in text and shell.cues[-1] == "stop"


async def test_call_ended_by_the_daemon_releases_the_helper(shell):
    await shell.command("/live", ())
    shell.controller.view.live_calls.append(
        LiveCallView(call_id="c1", status="ended", end_reason="deadline"))
    await settle(lambda: shell.live.state == "idle")
    assert "deadline" in shell.notice and FakeCall.instances[0].helper.stop.await_count >= 1


async def test_call_ended_elsewhere_is_noticed_by_the_status_poll(shell):
    shell.live.status_interval = 0.0
    await shell.command("/live", ())
    shell.client.live_status.return_value = status(state="idle")
    await settle(lambda: shell.live.state == "idle")


async def test_status_panel_and_usage(shell):
    await shell.command("/live", ("status",))
    text = "\n".join(shell.panel_lines)
    assert shell.panel_title == "Live status" and "Consent" in text and "Audio helper" in text
    with pytest.raises(ValueError, match="/live"):
        await shell.command("/live", ("bogus",))
    with pytest.raises(ValueError):
        await shell.command("/live", ("mute", "now"))
    await shell.command("/live", ("stop",))
    assert shell.notice == "No active Live call"
    await shell.command("/live", ("mute",))
    assert shell.notice == "No active Live call"


async def test_shell_close_releases_the_call_and_tells_the_daemon(shell):
    await shell.command("/live", ())
    call = FakeCall.instances[0]
    await shell.live.shutdown()
    assert call.stops == ["client_closed"] and shell.live.state == "idle"


async def test_typing_during_a_call_is_ordinary_steering(shell):
    shell.client.enqueue = AsyncMock()
    await shell.command("/live", ())
    await shell.submit("also add tests")
    shell.client.enqueue.assert_awaited_once()
    assert shell.client.enqueue.await_args.kwargs["mode"] == "steer"
    await shell.live.shutdown()


async def test_real_helper_process_through_the_command_flow(shell, tmp_path, monkeypatch):
    """No FakeCall: the real LiveCall and LiveHelper against the fake helper script."""
    wrapper = tmp_path / "nexus-live"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE}\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.undo()  # drop fixture patches; reapply only what the test needs
    monkeypatch.setenv("NEXUS_LIVE_BINARY", str(wrapper))
    monkeypatch.setattr(live_module, "play_cue", lambda kind: None)
    client = shell.client
    client.live_start = AsyncMock(return_value=SimpleNamespace(call_id="c1", answer_sdp="v=0 answer", voice="sol"))
    client.live_stop = AsyncMock(return_value=SimpleNamespace(state="idle", muted=False))
    client.live_mute = AsyncMock(return_value=SimpleNamespace(state="muted", muted=True))
    client.live_helper_event = AsyncMock()
    shell.live.__dict__.pop("call", None)
    from nexus.ui_support.live_helper import LiveCall
    monkeypatch.setattr(live_module, "LiveCall", LiveCall)
    await shell.command("/live", ())
    assert shell.live.state == "active"
    await settle(lambda: shell.live.call.helper.level_out == 0.5)
    assert shell.live.bar()["out"] == 0.5
    await shell.command("/live", ("mute",))
    assert shell.live.bar()["muted"]
    await shell.command("/live", ())
    assert shell.live.state == "idle" and not shell.live.call
    client.live_stop.assert_awaited_with("s", "user")
