"""LiveManager: state machine, exactly one live.ended per live.started, busy, deadline, detach."""
from __future__ import annotations

import asyncio

import pytest

from nexus.live import protocol as lp
from nexus.live.manager import MAX_SDP_BYTES, LiveManager, carried_block, load_instructions
from tests.live_fakes import FakeHost, FakeLiveTransport, live_config, settle

OFFER = "v=0\r\no=- offer\r\n"


def make(transport=None, config=None, **kw):
    transports = []

    def factory(raw_log=None):
        t = transport if transport is not None else FakeLiveTransport()
        transports.append(t)
        return t

    manager = LiveManager(config or live_config(), transport_factory=factory, environ={}, **kw)
    return manager, transports


async def start(manager, host=None, session="s1", **kw):
    host = host or FakeHost()
    result = await manager.start(session, OFFER, host=host, **kw)
    return host, result


def assert_one_ended(host, reason=None):
    assert host.kinds().count("live.started") == 1
    ended = host.of("live.ended")
    assert len(ended) == 1, host.kinds()
    if reason:
        assert ended[0]["reason"] == reason
    return ended[0]


async def test_start_returns_answer_and_goes_active():
    manager, transports = make()
    host, result = await start(manager)
    t = transports[0]
    assert result.answer_sdp == "v=0 answer" and result.call_id
    assert t.offer == OFFER and t.session.model == "gpt-live-1-codex" and t.session.voice == lp.DEFAULT_VOICE
    info = manager.snapshot()
    assert info.state == "active" and info.phase == "listening" and info.active_session == "s1"
    started = host.of("live.started")[0]
    assert started["call_id"] == result.call_id and started["instructions_source"] == "bundled"
    assert "build" in started["instructions"] and started["model"] == "gpt-live-1-codex"
    assert host.kinds().count("live.ended") == 0
    await manager.stop("s1")
    assert_one_ended(host, "user")
    assert manager.snapshot().state == "idle"


async def test_busy_names_the_owner_and_does_not_disturb_the_call():
    manager, _ = make()
    host, _result = await start(manager)
    other = FakeHost(sessions=("s2",))
    with pytest.raises(lp.LiveError) as err:
        await manager.start("s2", OFFER, host=other)
    assert err.value.code == "live_busy" and "s1" in err.value.message
    assert other.events == []
    status = await manager.status()
    assert status.reason == "busy" and status.active_session == "s1" and not status.available
    await manager.shutdown()


async def test_concurrent_starts_reserve_the_slot_synchronously():
    manager, _ = make(FakeLiveTransport(hang_start=True), start_timeout=5)
    host = FakeHost(sessions=("s1", "s2"))
    first = asyncio.create_task(manager.start("s1", OFFER, host=host))
    await asyncio.sleep(0)
    with pytest.raises(lp.LiveError) as err:
        await manager.start("s2", OFFER, host=host)
    assert err.value.code == "live_busy"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await settle(lambda: host.of("live.ended"))
    assert_one_ended(host)


@pytest.mark.parametrize(("offer", "code"), [("", "live_bad_offer"), ("x" * (MAX_SDP_BYTES + 1), "live_bad_offer")])
async def test_offer_is_bounded_and_nothing_is_recorded(offer, code):
    manager, _ = make()
    host = FakeHost()
    with pytest.raises(lp.LiveError) as err:
        await manager.start("s1", offer, host=host)
    assert err.value.code == code and host.events == [] and manager.snapshot().state == "idle"


async def test_unknown_session_voice_and_disabled_are_rejected_before_any_event():
    manager, _ = make()
    host = FakeHost()
    for kwargs, code in [({"session": "nope"}, "live_no_session"), ({"voice": "robot"}, "live_bad_voice")]:
        with pytest.raises(lp.LiveError) as err:
            await manager.start(kwargs.get("session", "s1"), OFFER, host=host, voice=kwargs.get("voice", ""))
        assert err.value.code == code
    off, _ = make(config=live_config(enabled=False))
    with pytest.raises(lp.LiveError) as err:
        await off.start("s1", OFFER, host=host)
    assert err.value.code == "live_disabled"
    assert (await off.status()).reason == "disabled"
    env_off = LiveManager(live_config(), transport_factory=lambda **k: None, environ={"NEXUS_LIVE": "off"})
    assert (await env_off.status()).reason == "disabled"
    assert host.events == []


async def test_startup_failure_emits_started_then_exactly_one_ended_with_redacted_error():
    secret = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signaturesignaturesignature1234567890"
    t = FakeLiveTransport(fail_start=RuntimeError(f"boom Bearer {secret}"))
    manager, _ = make(t)
    host = FakeHost()
    with pytest.raises(lp.LiveError) as err:
        await manager.start("s1", OFFER, host=host)
    assert secret not in err.value.message
    ended = assert_one_ended(host, "startup_failed")
    assert secret not in ended["error"] and t.close_reasons == ["startup_failed"]
    assert manager.snapshot().state == "idle"
    # the slot is free again
    manager2, _ = make()
    await start(manager2, host=FakeHost())


async def test_startup_failure_with_typed_error_keeps_the_code():
    manager, _ = make(FakeLiveTransport(fail_start=lp.LiveError("live_forbidden", "plan")))
    host = FakeHost()
    with pytest.raises(lp.LiveError) as err:
        await manager.start("s1", OFFER, host=host)
    assert err.value.code == "live_forbidden"
    assert_one_ended(host, "startup_failed")


async def test_startup_timeout():
    manager, _ = make(FakeLiveTransport(hang_start=True), start_timeout=0.05)
    host = FakeHost()
    with pytest.raises(lp.LiveError) as err:
        await manager.start("s1", OFFER, host=host)
    assert err.value.code == "live_timeout"
    assert_one_ended(host, "startup_failed")


async def test_stop_is_idempotent_and_concurrent_stops_end_once():
    manager, transports = make()
    host, _ = await start(manager)
    await asyncio.gather(manager.stop("s1"), manager.stop("s1", "again"))
    await manager.stop("s1")
    assert_one_ended(host)
    assert len(transports[0].close_reasons) == 1


async def test_stop_for_another_session_is_refused():
    manager, _ = make()
    await start(manager)
    with pytest.raises(lp.LiveError) as err:
        await manager.stop("s2")
    assert err.value.code == "live_busy"
    await manager.shutdown()


async def test_transport_drop_ends_the_call_once():
    manager, transports = make()
    host, _ = await start(manager)
    transports[0].drop("sideband_lost", "connection lost")
    transports[0].drop("sideband_lost", "again")
    await settle(lambda: host.of("live.ended"))
    ended = assert_one_ended(host, "sideband_lost")
    assert ended["error"] == "connection lost"


async def test_send_failure_ends_with_transport_error():
    manager, transports = make(FakeLiveTransport(fail_send=True))
    host, _ = await start(manager)
    await manager._append(manager._call, "hello", "notice", None)
    await settle(lambda: host.of("live.ended"))
    assert_one_ended(host, "transport_error")


async def test_remote_close_and_fatal_error_end_the_call():
    manager, transports = make()
    host, _ = await start(manager)
    transports[0].push(lp.SessionClosed("server done"))
    await settle(lambda: host.of("live.ended"))
    assert_one_ended(host, "remote_closed")

    manager2, transports2 = make()
    host2, _ = await start(manager2)
    transports2[0].push(lp.ErrorEvent("minor"))
    await asyncio.sleep(0.01)
    assert manager2.snapshot().state == "active"  # non-fatal: forwarded, call continues
    assert host2.of("live.error")[0]["message"] == "minor"
    transports2[0].push(lp.ErrorEvent("token expired", status=401, fatal=True))
    await settle(lambda: host2.of("live.ended"))
    assert_one_ended(host2, "error")


async def test_deadline_ends_the_call_without_cancelling_the_agent():
    manager, _ = make(config=live_config(max_minutes=1), minute_s=0.05)
    host, _ = await start(manager)
    await settle(lambda: host.of("live.ended"))
    assert_one_ended(host, "deadline")
    assert host.enqueued == []


async def test_client_detach_timeout_and_reattach():
    manager, _ = make(detach_timeout=0.1, watch_interval=0.02)
    host = FakeHost()
    host.viewer_count = 0
    await start(manager, host=host)
    await asyncio.sleep(0.05)
    host.viewer_count = 1  # a view re-attaches in time
    await asyncio.sleep(0.12)
    assert host.of("live.ended") == []
    host.viewer_count = 0
    await settle(lambda: host.of("live.ended"))
    assert_one_ended(host, "client_detached")


async def test_session_deleted_ends_the_call():
    manager, _ = make(watch_interval=0.02)
    host, _ = await start(manager)
    host.sessions.clear()
    await settle(lambda: host.of("live.ended"))
    assert_one_ended(host, "session_deleted")


async def test_shutdown_ends_the_call_and_refuses_new_ones():
    manager, _ = make()
    host, _ = await start(manager)
    await manager.shutdown()
    await manager.shutdown()
    assert_one_ended(host, "daemon_shutdown")
    with pytest.raises(lp.LiveError):
        await manager.start("s1", OFFER, host=host)


async def test_an_unrecordable_event_never_breaks_the_end_path():
    manager, _ = make()
    host, _ = await start(manager)
    host.fail_emit = True  # e.g. the session was deleted: every emit raises
    await manager.stop("s1")
    assert manager.snapshot().state == "idle"
    host.fail_emit = False
    await start(manager, host=FakeHost())  # slot is free


async def test_mute_toggles_and_reports_phase():
    manager, _ = make()
    host, _ = await start(manager)
    assert manager.mute("s1", None).muted is True
    assert manager.snapshot().state == "muted" and manager.snapshot().phase == "muted"
    assert manager.mute("s1", False).muted is False
    assert manager.mute("s1", True).muted is True and manager.mute("s1", True).muted is True
    with pytest.raises(lp.LiveError):
        manager.mute("s2")
    await manager.stop("s1")
    with pytest.raises(lp.LiveError) as err:
        manager.mute("s1")
    assert err.value.code == "live_not_active"


async def test_helper_events():
    manager, _ = make()
    host, _ = await start(manager)
    manager.helper_event("s1", "levels", '{"in": 0.5, "out": 2}')
    manager.helper_event("s1", "dropped", "12")
    manager.helper_event("s1", "dc_error", "channel failed")
    info = manager.snapshot()
    assert (info.levels_in, info.levels_out, info.dropped_frames) == (0.5, 1.0, 12)
    assert "channel failed" in info.last_error and host.of("live.error")
    manager.helper_event("s1", "phase", "speaking")
    assert manager.snapshot().phase == "speaking"
    with pytest.raises(lp.LiveError):
        manager.helper_event("s1", "explode")
    manager.helper_event("s1", "closed", "mic denied")
    await settle(lambda: host.of("live.ended"))
    ended = assert_one_ended(host, "helper_closed")
    assert ended["error"] == "mic denied"


async def test_transcripts_are_final_only_and_deltas_are_coalesced():
    manager, transports = make(flush_interval=0.05)
    host, _ = await start(manager)
    t = transports[0]
    for chunk in "hello wor":
        t.push(lp.TranscriptDelta("user", chunk))
    t.push(lp.TranscriptAdded("user", "hello world"))  # cumulative: replaces, not appends
    await settle(lambda: host.of("live.transcript.delta") and host.of("live.transcript.delta")[-1]["text"] == "hello world")
    assert len(host.of("live.transcript.delta")) < 9  # coalesced, never one per fragment
    assert host.of("live.transcript") == []
    t.push(lp.TurnDone("user", "hello world!"))
    t.push(lp.TurnDone("assistant", "hi there"))
    t.push(lp.TurnDone("assistant", "   "))  # empty finals are not recorded
    rows = host.of("live.transcript")
    assert [(r["role"], r["text"], r["turn"]) for r in rows] == [("user", "hello world!", 1), ("assistant", "hi there", 2)]
    await manager.stop("s1")


async def test_delta_rate_is_bounded_to_four_hertz():
    manager, transports = make(flush_interval=0.25)
    host, _ = await start(manager)
    t = transports[0]
    for i in range(200):
        t.push(lp.TranscriptDelta("assistant", "x"))
        await asyncio.sleep(0.002)
    await manager.stop("s1")
    assert len(host.of("live.transcript.delta")) <= 4
    assert len(host.of("live.phase")) <= 4


async def test_phase_follows_speaking_and_listening():
    manager, transports = make(flush_interval=0.0)
    host, _ = await start(manager)
    t = transports[0]
    t.push(lp.TranscriptDelta("assistant", "hi"))
    assert manager.snapshot().phase == "speaking"
    t.push(lp.TurnDone("assistant", "hi"))
    assert manager.snapshot().phase == "listening"
    await manager.stop("s1")


async def test_delegation_without_bridge_flow_is_bounded():
    manager, transports = make()
    host, _ = await start(manager)
    for i in range(30):
        transports[0].push(lp.DelegationCreated(f"d{i}", "do x"))
    await settle(lambda: len(host.enqueued) >= 8)
    await manager.stop("s1")
    assert len(host.enqueued) <= 30
    assert_one_ended(host)


def test_instructions_bundled_and_overrides(tmp_path):
    cfg = live_config()
    text, source = load_instructions(tmp_path / "myproj", "build", cfg)
    assert source == "bundled" and "build" in text and "myproj" in text and "{{" not in text
    assert "Agent Final Message" in text and "on screen" in text
    import getpass
    assert getpass.getuser() not in text or getpass.getuser() in ("build", "myproj")
    override = tmp_path / ".agents" / "live" / "instructions.md"
    override.parent.mkdir(parents=True)
    override.write_text("Agent {{agent}} in {{workspace}}")
    text, source = load_instructions(tmp_path, "scout", cfg)
    assert text == f"Agent scout in {tmp_path.name}" and source == str(override)
    custom = tmp_path / "mine.md"
    custom.write_text("custom")
    text, source = load_instructions(tmp_path, "x", live_config(instructions="mine.md"), carried="CARRY")
    assert text == "custom\n\nCARRY" and source == str(custom)
    (tmp_path / "big.md").write_text("x" * 40_000)
    with pytest.raises(lp.LiveError):
        load_instructions(tmp_path, "x", live_config(instructions="big.md"))
    with pytest.raises(lp.LiveError):
        load_instructions(tmp_path, "x", live_config(instructions="missing.md"))


def test_carried_block_is_bounded_and_ordered():
    turns = [("user", "q1"), ("assistant", "a1"), ("user", "q2")]
    block, count = carried_block(turns, 2)
    assert count == 2 and block.index("Assistant: a1") < block.index("User: q2") and "q1" not in block
    assert carried_block(turns, 0) == ("", 0) and carried_block([], 5) == ("", 0)
    huge = [("user", "é" * 3000) for _ in range(10)]
    block, count = carried_block(huge, 10)
    assert len(block.encode()) <= 8192 and 0 < count < 10


async def test_continuity_is_carried_and_shown_and_new_conversation_skips_it():
    turns = [("user", "what is in README"), ("assistant", "A project overview")]
    manager, transports = make()
    host = FakeHost(turns=turns)
    await start(manager, host=host)
    started = host.of("live.started")[0]
    assert started["carried_turns"] == 2 and "what is in README" in started["instructions"]
    assert "what is in README" in transports[0].session.instructions  # what the model gets == what the user sees
    await manager.stop("s1")

    manager2, transports2 = make()
    host2 = FakeHost(turns=turns)
    await start(manager2, host=host2, new_conversation=True)
    started2 = host2.of("live.started")[0]
    assert started2["carried_turns"] == 0 and started2["new_conversation"] is True
    assert "README" not in transports2[0].session.instructions
    await manager2.stop("s1")

    manager3, _ = make(config=live_config(carry_turns=0))
    host3 = FakeHost(turns=turns)
    await start(manager3, host=host3)
    assert host3.of("live.started")[0]["carried_turns"] == 0
    await manager3.shutdown()
