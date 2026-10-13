"""The live.* events fold into ConversationView.live_calls; replay equals the live fold."""
from __future__ import annotations

from nexus.events import Event
from nexus.live import protocol as lp
from nexus.view import apply, fold, initial_state
from tests.live_fakes import FakeHost, FakeLiveTransport, live_config, settle
from nexus.live.manager import LiveManager


async def recorded_call() -> tuple[FakeHost, LiveManager]:
    transport = FakeLiveTransport()
    manager = LiveManager(live_config(), transport_factory=lambda **k: transport, environ={}, flush_interval=0.0)
    host = FakeHost()
    await manager.start("s1", "v=0\r\n", host=host)
    transport.push(lp.TranscriptDelta("user", "read the"))
    transport.push(lp.TurnDone("user", "read the readme"))
    transport.push(lp.DelegationCreated("d1", "read the README"))
    await settle(lambda: host.of("live.delegated"))
    await manager._append(manager._call, "Agent progress: working with Read.", "commentary", "d1")
    await manager._append(manager._call, '"Agent Final Message":\n\nIt is a harness.', "final", "d1")
    transport.push(lp.TranscriptDelta("assistant", "It is"))
    transport.push(lp.TurnDone("assistant", "It is a harness."))
    transport.push(lp.ErrorEvent("minor hiccup"))
    await manager.stop("s1", "user")
    return host, manager


async def test_replay_equals_live_fold_at_every_prefix():
    host, _ = await recorded_call()
    events = host.as_events()
    live = initial_state("s1")
    for index, event in enumerate(events, start=1):
        live = apply(live, event)
        assert live.to_dict() == fold(events[:index]).to_dict(), f"diverged at {event.type}"
    # replaying again over the same state is a no-op (sequenced events are idempotent)
    again = live
    for event in events:
        again = apply(again, event)
    assert again.to_dict() == live.to_dict()


async def test_the_call_view_has_everything_the_user_needs_to_see():
    host, _ = await recorded_call()
    view = fold(host.as_events())
    call = view.live
    assert call is not None and len(view.live_calls) == 1
    assert call.status == "ended" and call.end_reason == "user" and call.duration_s is not None
    assert call.model == "gpt-live-1-codex" and call.voice == lp.DEFAULT_VOICE
    assert "build" in call.instructions  # the instructions the voice model sees are in the view
    assert [(t.role, t.text) for t in call.transcript] == [("user", "read the readme"), ("assistant", "It is a harness.")]
    assert call.caption is None  # partials are superseded by finals
    assert [(d.delegation_id, d.text) for d in call.delegations] == [("d1", "read the README")]
    assert [(a.channel, a.delegation_id) for a in call.appends] == [("commentary", "d1"), ("final", "d1")]
    assert call.appends[1].text.startswith('"Agent Final Message":')
    assert call.errors == ["minor hiccup"] and call.phase == "ended"
    payload = view.to_dict()
    assert payload["live_calls"][0]["transcript"][0]["text"] == "read the readme"


def test_views_without_live_events_are_unchanged_on_the_wire():
    view = fold([Event(type="session.opened", data={"id": "s"}, seq=1, session="s")])
    assert "live_calls" not in view.to_dict() and view.live is None


def test_two_calls_are_kept_in_order_and_unknown_live_events_are_harmless():
    def ev(seq, kind, **data):
        return Event(type=kind, data=data, seq=seq, session="s", ts=100.0 + seq)

    events = [
        ev(1, "live.started", call_id="a", model="m", voice="sol", instructions="i"),
        ev(2, "live.ended", call_id="a", reason="user", duration_s=3.0),
        ev(3, "live.started", call_id="b", model="m", voice="cove", instructions="j", carried_turns=2),
        ev(4, "live.levels", call_id="b", **{"in": 0.1}),
        ev(5, "live.future_thing", call_id="b"),
        ev(6, "live.transcript", call_id="b", role="user", text="hi", turn=1),
    ]
    view = fold(events)
    assert [c.call_id for c in view.live_calls] == ["a", "b"]
    assert view.live_calls[0].status == "ended" and view.live_calls[1].status == "active"
    assert view.live_calls[1].carried_turns == 2 and view.live_calls[1].transcript[0].text == "hi"
    assert any("live.future_thing" in str(d) for d in view.diagnostics)  # unknown types stay diagnostics, never errors


def test_orphan_live_events_before_a_start_are_ignored():
    view = fold([Event(type="live.transcript", data={"role": "user", "text": "x"}, seq=1, session="s")])
    assert view.live_calls == []


def test_rows_are_bounded():
    events = [Event(type="live.started", data={"call_id": "a"}, seq=1, session="s")]
    events += [Event(type="live.transcript", data={"call_id": "a", "role": "user", "text": str(i), "turn": i},
                     seq=i + 2, session="s") for i in range(700)]
    call = fold(events).live
    assert len(call.transcript) == 500 and call.transcript[-1].text == "699"


def test_delegation_origin_labels_the_user_message_but_not_its_text():
    def queued(origin):
        data = {"queued_id": "q1", "content": [{"type": "text", "text": "read the README"}], "queue_depth": 1, "mode": "queue"}
        if origin:
            data["origin"] = origin
        return [
            Event(type="input.queued", data=data, seq=1, session="s", ts=5.0, id="e1"),
            Event(type="turn.started", data={}, seq=2, session="s", turn="t1", ts=5.0, id="e2"),
            Event(type="input.consumed", data={"queued_id": "q1", "turn": "t1", "mode": "queue"}, seq=3, session="s", turn="t1", ts=5.0, id="e3"),
        ]

    labelled = fold(queued({"kind": "live", "delegation_id": "d1"}))
    typed = fold(queued(None))
    assert labelled.messages[0].origin == {"kind": "live", "delegation_id": "d1"}
    assert typed.messages[0].origin is None
    # identical apart from the origin label
    a, b = labelled.to_dict(), typed.to_dict()
    for payload in (a, b):
        for message in payload["messages"]:
            message.pop("origin", None)
        for turn in payload["turns"]:
            for message in turn["messages"]:
                message.pop("origin", None)
    assert a == b
    assert labelled.messages[0].text == typed.messages[0].text == "read the README"
