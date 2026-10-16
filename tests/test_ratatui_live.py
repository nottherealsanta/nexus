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

    async def pause(self, paused=None):
        self.paused = (not getattr(self, "paused", False)) if paused is None else paused
        return SimpleNamespace(paused=self.paused, state="paused" if self.paused else "active",
                               pause_remaining_s=120.0)

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


async def test_pause_shows_in_the_bar_and_toggles_independently_of_stop(shell):
    await shell.command("/live", ())
    await shell.command("/live", ("pause",))
    bar = shell.live.bar()
    assert bar["phase"] == "paused" and bar["paused"] and bar["in"] == 0.0 and 0 < bar["pause_left"] <= 120
    assert "paused" in shell.notice and "Ctrl+X P" in shell.notice and shell.live.state == "active"
    await shell.command("/live", ("pause",))  # toggles back
    assert not shell.live.bar()["paused"] and shell.live.bar()["phase"] != "paused"
    await shell.command("/live", ("pause",))
    await shell.command("/live", ("resume",))
    assert not shell.live.paused
    await shell.command("/live", ("pause",))
    await shell.command("/live", ())  # the toggle (Ctrl+X D) still stops a paused call
    assert shell.live.state == "idle" and not shell.live.paused
    assert FakeCall.instances[0].stops == ["user"]


async def test_pause_with_no_call_is_a_notice_and_a_timed_out_pause_restarts(shell):
    await shell.command("/live", ("pause",))
    assert shell.notice == "No active Live call" and not FakeCall.instances
    shell.live.hibernated = True  # the last call ended with reason pause_timeout
    await shell.command("/live", ("pause",))
    assert len(FakeCall.instances) == 1 and shell.live.state == "active" and not shell.live.hibernated
    await shell.live.shutdown()


async def test_pause_timeout_end_names_the_resume_key(shell):
    from nexus.ui.ratatui.live import Live
    assert "Ctrl+X P" in Live._ended_text(LiveCallView(status="ended", end_reason="pause_timeout"))


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
    ("unsupported", "uv tool install --upgrade --force nexus-harness"),
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
    assert "nexus-live" in text and "uv tool install --upgrade --force nexus-harness" in text
    assert "restart the daemon" in text and "cargo" not in text and FakeCall.instances == []
    assert any(t["level"] == "error" for t in shell.toasts)


async def test_first_use_asks_for_consent_and_remembers_it(shell):
    shell.client.live_status.return_value = status(consent=False)
    await shell.command("/live", ())
    assert FakeCall.instances == [] and shell.panel_title == "Use GPT Live voice?"
    text = "\n".join(shell.panel_lines)
    assert "OpenAI" in text and "ChatGPT account" in text and "audio" in text and "summaries" in text
    shell.client.live_consent.assert_not_awaited()  # nothing is recorded by merely asking
    labels = [item["label"] for item in shell.items]
    assert labels == ["Before you start: what is sent", "Agree and start the call", "Cancel"]
    assert "OpenAI" in "\n".join(shell.items[0]["lines"])  # the notice is visible in the list dialog itself
    agree = shell.items[1]["operation"]
    await shell.workflows.operate(agree)
    shell.client.live_consent.assert_awaited_once_with(True)
    assert shell.live.state == "active" and shell.panel_title == ""
    await shell.live.shutdown()


async def test_cancelling_the_consent_dialog_starts_nothing(shell):
    shell.client.live_status.return_value = status(consent=False)
    await shell.command("/live", ())
    await shell.workflows.operate(shell.items[2]["operation"])
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


async def test_daemon_consent_refusal_reopens_the_consent_dialog(shell):
    # consent looked given, but the daemon refused the start (revoked elsewhere, or a stale client)
    FakeCall.fail = FacadeError("live_consent_required", "GPT Live needs your consent first")
    await shell.command("/live", ())
    assert FakeCall.instances[0].helper.stop.await_count == 1  # the helper is released
    assert shell.panel_title == "Use GPT Live voice?" and shell.live.state == "idle"
    assert [item["label"] for item in shell.items][1] == "Agree and start the call"
    assert "GPT Live could not start" not in "\n".join(shell.panel_lines)


def test_consent_refusal_has_a_one_line_fix():
    assert "/live" in live_helper.fix_for("live_consent_required") and "nexus live" in live_helper.fix_for("live_consent_required")


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


# ---------------------------------------------------------------------------
# Timeline: the Live card and "via voice" (projection, replay)
# ---------------------------------------------------------------------------

from nexus.events import Event  # noqa: E402
from nexus.ui_support import live_card  # noqa: E402
from nexus.view import apply, fold  # noqa: E402


def ev(seq, kind, ts=None, turn=None, **data):
    if kind == "input.consumed":
        data["turn"] = turn
    return Event(type=kind, data=data, seq=seq, session="s", ts=ts if ts is not None else 100.0 + seq,
                 turn=turn, id=f"e{seq}")


def call_events(*, ended=True, commentary=2):
    events = [
        ev(1, "live.started", call_id="c1", model="gpt-live-1-codex", voice="sol", instructions_source="bundled",
           instructions="You are the voice of build.\nBe brief.", carried_turns=2),
        ev(2, "live.transcript", call_id="c1", role="user", text="read the README", turn=1),
        ev(3, "live.delegated", call_id="c1", delegation_id="d1", text="read the README", mode="queue", queued_id="q1"),
        ev(4, "input.queued", queued_id="q1", content=[{"type": "text", "text": "read the README"}], queue_depth=1,
           mode="queue", origin={"kind": "live", "delegation_id": "d1"}),
        ev(5, "turn.started", turn="t1"),
        ev(6, "input.consumed", turn="t1", queued_id="q1", mode="queue"),
    ]
    for n in range(commentary):
        events.append(ev(10 + n, "live.appended", call_id="c1", delegation_id="d1", channel="commentary",
                         text=f"Agent progress {n}", chunks=1))
    events += [
        ev(20, "live.appended", call_id="c1", delegation_id="d1", channel="final",
           text='"Agent Final Message":\n\nIt is a harness.', chunks=1),
        ev(21, "live.transcript", call_id="c1", role="assistant", text="It is a harness.", turn=2),
        ev(22, "live.transcript.delta", call_id="c1", role="user", text="and the lic"),
    ]
    if ended:
        events.append(ev(30, "live.ended", call_id="c1", reason="deadline", duration_s=161.0, error="sideband dropped"))
    return events


def rows_of(call, **kwargs):
    return live_card.card_rows(call, **kwargs)


def labels(rows):
    return [(r.tone, r.label) for r in rows]


def test_card_rows_are_labelled_and_commentary_is_collapsed_with_a_count():
    call = fold(call_events()).live
    rows = rows_of(call, turns={"d1": 1})
    text = "\n".join(f"{r.label}|{r.text}" for r in rows)
    assert ("dim", "Started") in labels(rows)
    assert "instructions: bundled · 2 earlier turns carried over" in text
    assert "voice instructions hidden" in text and "Be brief." not in text
    assert ("you", "You") in labels(rows) and ("voice", "Voice") in labels(rows)
    assert ("sent", "Sent to agent (queue)") in labels(rows) and "read the README  → turn #1" in text
    assert "2 progress notes (commentary) hidden" in text and "Agent progress 0" not in text
    assert ("told", "Told voice [final]") in labels(rows) and "It is a harness." in text
    assert rows[-1] == live_card.CardRow("bad", "Ended", "deadline · 02:41 · error: sideband dropped")
    assert not [r for r in rows if r.tone == "caption"]  # an ended call has no streaming caption


def test_opened_card_shows_everything_nothing_dropped():
    call = fold(call_events()).live
    rows = rows_of(call, expanded=True)
    text = "\n".join(f"{r.label}|{r.text}" for r in rows)
    assert "Be brief." in text and "You are the voice of build." in text
    assert "Told voice [commentary]|Agent progress 0" in text and "Agent progress 1" in text
    assert "hidden" not in text


def test_streaming_caption_shows_only_while_the_call_is_active():
    call = fold(call_events(ended=False)).live
    assert call.status == "active"
    assert ("caption", "You…") in labels(rows_of(call))
    assert [r.text for r in rows_of(call) if r.tone == "caption"] == ["and the lic"]


def test_long_text_is_clipped_only_while_collapsed_and_the_clip_is_announced():
    events = call_events()[:1] + [ev(2, "live.appended", call_id="c1", channel="final", text="x" * 5000, chunks=11)]
    call = fold(events).live
    collapsed = "\n".join(r.text for r in rows_of(call))
    assert "+4400 more characters" in collapsed and len(collapsed) < 1500
    assert "x" * 5000 in "\n".join(r.text for r in rows_of(call, expanded=True))


def test_card_encoding_is_tab_separated_and_control_safe():
    call = fold(call_events()).live
    encoded = live_card.encode(rows_of(call))
    assert all(line.count("\t") == 2 for line in encoded.splitlines())


def test_timeline_has_the_card_in_order_the_via_voice_label_and_a_toggle(shell):
    view = fold(call_events())
    shell.controller.view = view
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    kinds = [(b["kind"], b.get("status", "")) for b in snapshot["blocks"]]
    card = next(b for b in snapshot["blocks"] if b["kind"] == "live")
    user = next(b for b in snapshot["blocks"] if b["kind"] == "user")
    assert kinds.index(("live", "ended")) < kinds.index(("user", "live"))  # the call began before the turn it caused
    assert user["status"] == "live" and user["title"] == "read the README"
    assert card["collapsed"] is True and card["title"] == "gpt-live-1-codex · sol"
    assert "Sent to agent (queue)" in card["detail"] and "→ turn #1" in card["detail"]
    assert "Agent progress 0" not in card["detail"]
    toggle = card["operation"]
    assert toggle == {"kind": "live_toggle", "id": "c1"}
    # the bridge offers exactly this operation, and running it opens the card
    from nexus.ui.ratatui.prototype import _block_operations
    assert toggle in list(_block_operations(snapshot["blocks"]))


async def test_toggle_opens_and_closes_the_card_and_replay_matches(shell):
    events = call_events()
    live = None
    from nexus.view import initial_state
    live = initial_state("s")
    for event in events:
        live = apply(live, event)
    assert live.to_dict() == fold(events).to_dict()  # replay equals live state
    shell.controller.view = live
    first = project(shell.controller, 1, shell=shell, literal=False)
    card = next(b for b in first["blocks"] if b["kind"] == "live")
    await shell.workflows.operate(card["operation"])
    opened = project(shell.controller, 2, shell=shell, literal=False)
    card2 = next(b for b in opened["blocks"] if b["kind"] == "live")
    assert card2["collapsed"] is False and "Agent progress 0" in card2["detail"] and card2["rev"] != card["rev"]
    await shell.workflows.operate(card["operation"])
    closed = project(shell.controller, 3, shell=shell, literal=False)
    assert next(b for b in closed["blocks"] if b["kind"] == "live")["rev"] == card["rev"]
    # a reconnect rebuilds the same card from the log alone
    shell.controller.view = fold(events)
    again = project(shell.controller, 4, shell=shell, literal=False)
    assert next(b for b in again["blocks"] if b["kind"] == "live")["detail"] == card["detail"]


def test_projection_without_a_live_call_is_unchanged(shell):
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    assert not [b for b in snapshot["blocks"] if b["kind"] == "live"] and snapshot["live"] == {}


async def test_terminal_pane_shows_the_open_call_and_keeps_it_out_of_the_timeline(shell):
    """The native terminal draws the open call in the Live pane, not as a timeline card."""
    shell.controller.view = fold(call_events(ended=False))
    await shell.command("/live", ())  # FakeCall answers with call_id "c1"
    assert shell.live.state == "active" and shell.live.pane is True
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    assert not [b for b in snapshot["blocks"] if b["kind"] == "live"]
    live = snapshot["live"]
    assert "Sent to agent (queue)" in live["detail"] and "→ turn #1" in live["detail"]
    assert live["operation"] == {"kind": "live_toggle", "id": "c1"}
    assert live["collapsed"] is True
    await shell.live.shutdown()


async def test_the_card_joins_the_timeline_once_the_call_ends(shell):
    shell.controller.view = fold(call_events(ended=False))
    await shell.command("/live", ())
    await shell.command("/live", ())  # hang up
    assert shell.live.state == "idle"
    shell.controller.view = fold(call_events(ended=True))
    snapshot = project(shell.controller, 2, shell=shell, literal=False)
    card = next(b for b in snapshot["blocks"] if b["kind"] == "live")
    assert card["status"] == "ended" and card["operation"] == {"kind": "live_toggle", "id": "c1"}
    assert snapshot["live"] == {}


async def test_desktop_keeps_the_card_in_the_timeline(shell):
    shell.live.pane = False  # prototype.run sets this for the desktop client
    shell.controller.view = fold(call_events(ended=False))
    await shell.command("/live", ())
    assert shell.live.bar()["detail"] == ""
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    card = next(b for b in snapshot["blocks"] if b["kind"] == "live")
    assert "Sent to agent (queue)" in card["detail"] and card["collapsed"] is True
    await shell.live.shutdown()


async def test_running_the_pane_operation_opens_the_rows(shell):
    shell.controller.view = fold(call_events(ended=False))
    await shell.command("/live", ())
    operation = shell.live.bar()["operation"]
    assert operation == {"kind": "live_toggle", "id": "c1"}
    assert shell.live.bar()["collapsed"] is True
    await shell.workflows.operate(operation)
    opened = shell.live.bar()
    assert opened["collapsed"] is False and "Agent progress 0" in opened["detail"]
    await shell.workflows.operate(operation)
    assert shell.live.bar()["collapsed"] is True
    await shell.live.shutdown()


def _starting(shell, begin_ts=100.0):
    """A client that is still starting: the daemon recorded ``live.started``, the result is not back."""
    shell.live.call = FakeCall(shell.controller.client, "s")
    shell.live.state, shell.live.session, shell.live.begin_ts = "starting", "s", begin_ts


async def test_pane_appears_and_card_stays_out_while_the_start_result_is_unknown(shell):
    shell.controller.view = fold(call_events(ended=False))
    _starting(shell)
    assert shell.live.call.result is None
    assert not shell.live.card_blocks(shell.controller.view)  # no flash of the timeline card
    snapshot = project(shell.controller, 1, shell=shell, literal=False)
    assert not [b for b in snapshot["blocks"] if b["kind"] == "live"]
    assert "Sent to agent (queue)" in snapshot["live"]["detail"]
    assert snapshot["live"]["operation"] == {"kind": "live_toggle", "id": "c1"}
    assert snapshot["live"]["model"] == "gpt-live-1-codex"


async def test_starting_without_the_started_event_has_no_pane_yet(shell):
    _starting(shell)
    bar = shell.live.bar()
    assert bar["state"] == "starting" and bar["detail"] == "" and bar["phase"] == "connecting"


async def test_an_older_or_ended_call_is_never_taken_for_the_one_starting(shell):
    shell.controller.view = fold(call_events(ended=True))
    _starting(shell, begin_ts=500.0)
    assert shell.live.bar()["detail"] == ""
    assert [b for _, b in shell.live.card_blocks(shell.controller.view)]  # the ended call stays a card
    shell.controller.view = fold(call_events(ended=False))  # started long before this start began
    assert shell.live.bar()["detail"] == ""


async def test_the_card_is_not_matched_in_another_session_while_starting(shell):
    shell.controller.view = fold(call_events(ended=False))
    _starting(shell)
    shell.controller.session = "other"
    assert shell.live.bar()["detail"] == "" and shell.live.bar()["elsewhere"] == "s"
