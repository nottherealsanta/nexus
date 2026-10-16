"""LiveBridge: delegations become ordinary turns; session events come back as appends (M2)."""
from __future__ import annotations

import asyncio


from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ModelSection, PermissionsSection, ToolsSection
from nexus.events import Event
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.live import protocol as lp
from nexus.live.bridge import MAX_FINAL_CHARS, MAX_INPUT_BYTES, MAX_TRANSCRIPT_BYTES, LiveBridge, compose_request
from nexus.live.manager import LiveManager
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response
from nexus.runtime import Runtime
from tests.live_fakes import FakeHost, FakeLiveTransport, live_config, settle


# -- unit level: synthetic events, fake clock --------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def unit_bridge(progress=True):
    sent, emitted, phases = [], [], []
    clock = Clock()

    async def append(text, label, delegation_id):
        sent.append((text, label, delegation_id))

    bridge = LiveBridge(
        host=FakeHost(), session_id="s1", append=append, emit=lambda k, d: emitted.append((k, d)),
        set_phase=phases.append, progress=progress, clock=clock,
    )
    return bridge, sent, emitted, phases, clock


def ev(kind, on="t1", **data):
    return Event(type=kind, data=data, session="s1", turn=on)


async def delegated_turn(bridge, delegation="d1", queued="q1", turn="t1"):
    await bridge.handle_event(ev("input.queued", on=None, queued_id=queued,
                                 origin={"kind": "live", "delegation_id": delegation}))
    await bridge.handle_event(ev("input.consumed", on=turn, queued_id=queued, mode="queue", turn=turn))


async def test_progress_is_rate_limited_to_one_per_five_seconds_and_gated_by_config():
    bridge, sent, _e, _p, clock = unit_bridge()
    await delegated_turn(bridge)
    await bridge.handle_event(ev("tool.requested", tool="Read", call_id="1"))
    assert [s[1] for s in sent] == ["commentary"] and "Read" in sent[0][0] and sent[0][2] == "d1"
    for tool in ("Grep", "Glob", "Edit"):
        clock.now += 1.0
        await bridge.handle_event(ev("tool.requested", tool=tool, call_id=tool))
    assert len(sent) == 1  # inside the 5 s window: nothing more, names accumulate
    clock.now += 5.0
    await bridge.handle_event(ev("tool.requested", tool="Bash", call_id="9"))
    assert len(sent) == 2 and all(name in sent[1][0] for name in ("Grep", "Glob", "Edit", "Bash"))

    quiet, qsent, *_ = unit_bridge(progress=False)
    await delegated_turn(quiet)
    await quiet.handle_event(ev("tool.requested", tool="Read"))
    assert qsent == []


async def test_progress_only_for_delegated_turns():
    bridge, sent, *_ = unit_bridge()
    await bridge.handle_event(ev("tool.requested", tool="Read"))  # a typed turn: no delegation
    await bridge.handle_event(ev("turn.completed", on="t1"))
    assert sent == []


async def test_final_failure_cancel_and_drop_notices():
    bridge, sent, _e, phases, _c = unit_bridge()
    await delegated_turn(bridge, "d1", "q1", "t1")
    await bridge.handle_event(ev("text", text="first"))
    await bridge.handle_event(ev("text", text="the real answer"))
    await bridge.handle_event(ev("turn.completed", stop_reason="end_turn"))
    assert sent == [("the real answer", "final", "d1")] and phases[-1] == "listening"

    sent.clear()
    await delegated_turn(bridge, "d2", "q2", "t2")
    await bridge.handle_event(ev("turn.failed", on="t2", error="provider exploded Bearer abcdefghijklmnop"))
    assert sent[0][1] == "notice" and "failed" in sent[0][0] and sent[0][2] == "d2"

    sent.clear()
    await delegated_turn(bridge, "d3", "q3", "t3")
    await bridge.handle_event(ev("turn.cancelled", on="t3", reason="stopped"))
    assert "cancelled" in sent[0][0]

    sent.clear()
    await bridge.handle_event(ev("input.queued", on=None, queued_id="q4", origin={"kind": "live", "delegation_id": "d4"}))
    await bridge.handle_event(ev("input.dropped", on=None, queued_id="q4", reason="removed by user"))
    assert "not run" in sent[0][0] and sent[0][2] == "d4"


async def test_final_without_text_and_oversize_final():
    bridge, sent, *_ = unit_bridge()
    await delegated_turn(bridge)
    await bridge.handle_event(ev("turn.completed"))
    assert "without a written reply" in sent[0][0]
    sent.clear()
    await delegated_turn(bridge, "d2", "q2", "t2")
    await bridge.handle_event(ev("text", on="t2", text="x" * (MAX_FINAL_CHARS + 500)))
    await bridge.handle_event(ev("turn.completed", on="t2"))
    assert "500 more characters omitted" in sent[0][0] and len(sent[0][0]) < MAX_FINAL_CHARS + 200


async def test_merged_delegations_close_every_request():
    bridge, sent, *_ = unit_bridge()
    await delegated_turn(bridge, "d1", "q1", "t1")
    await delegated_turn(bridge, "d2", "q2", "t1")  # steered into the same turn
    await bridge.handle_event(ev("text", text="answer"))
    await bridge.handle_event(ev("turn.completed"))
    assert [(label, d) for _t, label, d in sent] == [("notice", "d1"), ("final", "d2")]


async def test_permission_and_question_announce_on_screen_only():
    bridge, sent, *_ = unit_bridge()
    await delegated_turn(bridge)
    await bridge.handle_event(ev("permission.requested", id="r1", tool="bash", preview="rm -rf build"))
    await bridge.handle_event(ev("question.requested", question_id="x", prompt="Which branch?"))
    assert [label for _t, label, _d in sent] == ["notice", "notice"]
    assert "on screen" in sent[0][0] and "rm -rf build" in sent[0][0] and "cannot be approved by voice" in sent[0][0]
    assert "Which branch?" in sent[1][0] and "on screen" in sent[1][0]


async def test_permission_preview_is_redacted_and_bounded():
    bridge, sent, *_ = unit_bridge()
    await delegated_turn(bridge)
    secret = "sk-" + "a1" * 20
    await bridge.handle_event(ev("permission.requested", id="r", tool="bash", preview=f"curl -H 'Authorization: Bearer {secret}' " + "x" * 500))
    assert secret not in sent[0][0] and len(sent[0][0]) < 400


async def test_sub_agent_and_foreign_events_are_ignored():
    bridge, sent, *_ = unit_bridge()
    await delegated_turn(bridge)
    await bridge.handle_event(ev("tool.requested", tool="Read", agent={"id": "a1"}))
    await bridge.handle_event(ev("permission.requested", tool="bash", agent={"id": "a1"}))
    await bridge.handle_event(ev("input.queued", on=None, queued_id="qx"))  # typed input: no origin
    await bridge.handle_event(ev("turn.completed", on="other"))
    assert sent == []


async def test_empty_delegation_is_refused_politely():
    bridge, sent, *_ = unit_bridge()
    await bridge.on_delegation("d9", "   ")
    assert sent and sent[0][1] == "notice" and bridge._host.enqueued == []


async def test_delegation_mode_follows_whether_a_turn_runs():
    bridge, _s, emitted, phases, _c = unit_bridge()
    host = bridge._host
    await bridge.on_delegation("d1", " read README ")
    host.running = True
    await bridge.on_delegation("d2", "also tests")
    assert [(e[1].split("\n")[1], e[2]) for e in host.enqueued] == [("read README", "queue"), ("also tests", "steer")]
    assert all(e[3]["kind"] == "live" for e in host.enqueued)
    assert [(d["delegation_id"], d["mode"], d["queued_id"]) for k, d in emitted if k == "live.delegated"] == [
        ("d1", "queue", "q1"), ("d2", "steer", "q2")]
    assert phases == ["working", "working"]


async def test_enqueue_failure_gives_a_speakable_notice_and_a_durable_error():
    bridge, sent, emitted, phases, _c = unit_bridge()

    async def boom(*args, **kwargs):
        raise RuntimeError("queue full Bearer abcdefghijklmnop1234")

    bridge._host.enqueue = boom
    await bridge.on_delegation("d1", "do it")
    assert len(sent) == 1 and sent[0][1] == "notice" and sent[0][2] == "d1" and "try again" in sent[0][0]
    errors = [d for k, d in emitted if k == "live.error"]
    assert len(errors) == 1 and "abcdefghijklmnop1234" not in errors[0]["message"]
    assert not any(k == "live.delegated" for k, _d in emitted) and phases == []
    assert bridge._queued == {}


async def test_quick_tap_consuming_input_leaves_no_stale_mapping():
    bridge, sent, *_ = unit_bridge()
    host = bridge._host

    async def fast_enqueue(session, text, mode, origin, title=None):
        # the tap delivers queued + consumed before enqueue() returns to the bridge
        await bridge.handle_event(ev("input.queued", on=None, queued_id="q1", origin=origin))
        await bridge.handle_event(ev("input.consumed", on="t1", queued_id="q1", mode="queue", turn="t1"))
        return "q1"

    host.enqueue = fast_enqueue
    await bridge.on_delegation("d1", "do it")
    assert bridge._queued == {} and bridge._turns == {"t1": ["d1"]}
    await bridge.handle_event(ev("text", text="done"))
    await bridge.handle_event(ev("turn.completed"))
    assert [label for _t, label, d in sent] == ["final"]


async def test_bridge_close_stops_the_tap():
    bridge, *_ = unit_bridge()
    bridge.start()
    await asyncio.sleep(0)
    await bridge.close()
    assert bridge._host.tap.closed


# -- integration: a real runtime and a ScriptedProvider ------------------------

def _config() -> Config:
    return Config(
        model="scripted/m", version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m"), agent=AgentSection(profile="coding"),
                    permissions=PermissionsSection(mode="ask", on_unattended="deny"), tools=ToolsSection()),
    )


async def make_call(tmp_path, provider, **config):
    from nexus.host_support.live_consent import write_consent

    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    write_consent(True)  # the daemon refuses LiveStart until the notice is recorded
    transport = FakeLiveTransport()
    runtime.live = LiveManager(live_config(**config), transport_factory=lambda **kw: transport,
                               environ={}, flush_interval=0.0)
    facade = HostFacade(runtime)
    facade.open_session("s")
    started = await facade.handle(p.LiveStart(session="s", offer_sdp="v=0\r\n"))
    assert isinstance(started, p.LiveStartResult), started
    return runtime, facade, transport


def final_sent(transport) -> bool:
    return any(m.get("type") == "session.context.append" for m in transport.sent)


def delegation(i: str, text: str) -> lp.DelegationCreated:
    return lp.DelegationCreated(i, text)


async def test_idle_delegation_runs_a_normal_turn_and_appends_a_chunked_final(tmp_path):
    answer = "Réponse détaillée. " * 80  # multibyte, far beyond one 500-byte append
    runtime, facade, transport = await make_call(tmp_path, ScriptedProvider(text_response(answer)))
    try:
        transport.push(delegation("d1", "summarise the README"))
        await settle(lambda: final_sent(transport))
        await settle(lambda: runtime.live.snapshot().phase == "listening")

        sent = [m for m in transport.sent if "content" in m]
        # Codex TUI: the final answer is a plain session-level speakable append, no id, no label
        assert {m["type"] for m in sent} == {"session.context.append"}
        assert all("delegation_item_id" not in m and m["channel"] == "speakable" for m in sent)
        assert all(len(m["content"][0]["text"].encode()) <= 500 for m in sent)
        joined = "".join(m["content"][0]["text"] for m in sent)
        assert joined == answer.strip() and len(sent) >= 3
        assert "Agent Final Message" not in joined

        handle = runtime.session("s")
        view, _ = facade.state("s")
        call = view.live
        assert [(d.delegation_id, d.mode) for d in call.delegations] == [("d1", "queue")]
        assert [(a.channel, a.delegation_id, a.chunks) for a in call.appends] == [("final", "d1", len(sent))]
        assert call.appends[0].text == joined  # recorded exactly as sent
        # the user message the delegation created: same text as a typed prompt, plus an origin label
        user = next(m for m in view.messages if m.role == "user")
        assert user.text == compose_request("summarise the README", [])[0]
        assert user.text.startswith("Voice request\nsummarise the README") and "speech recognition" in user.text
        assert user.origin == {"kind": "live", "delegation_id": "d1"}
        queued = next(e for e in handle.events if e.type == "input.queued")
        assert queued.data["origin"] == {"kind": "live", "delegation_id": "d1"}
        # the bridge is not a viewer
        assert facade.presence.viewers("s") == 0 and handle._viewers == 0
    finally:
        await runtime.aclose()


async def test_delegated_transcript_equals_a_typed_prompt_apart_from_the_origin(tmp_path):
    runtime, facade, transport = await make_call(tmp_path, ScriptedProvider(text_response("ok"), text_response("ok")))
    try:
        transport.push(delegation("d1", "do the thing"))
        await settle(lambda: final_sent(transport))
        facade.open_session("typed")
        await facade.enqueue("typed", compose_request("do the thing", [])[0], mode="queue")
        await facade.wait_idle(timeout=5.0)

        def shape(session):
            view, _ = facade.state(session)
            return [(t.kind, t.phase, [(m.role, m.text, m.done) for m in t.messages],
                     [(x.name, x.status) for x in t.tools]) for t in view.turns]

        assert shape("s") == shape("typed")
        records = [m for m in runtime.session("s").read(force=True).records if getattr(m, "message", None) is not None]
        typed = [m for m in runtime.session("typed").read(force=True).records if getattr(m, "message", None) is not None]
        assert [(r.message.role, r.message.content) for r in records] == [(r.message.role, r.message.content) for r in typed]
    finally:
        await runtime.aclose()


async def test_delegation_during_a_running_turn_steers_it(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [Wait(gate), *tool_response(("c1", "Read", {"path": "a.txt"}))],
        text_response("both requests done"),
    )
    runtime, facade, transport = await make_call(tmp_path, provider)
    try:
        transport.push(delegation("d1", "read a.txt"))
        await settle(lambda: runtime.session("s").active_turn_id is not None)
        transport.push(delegation("d2", "and mention the size"))
        await settle(lambda: len(facade.state("s")[0].live.delegations) == 2)
        gate.set()
        await settle(lambda: final_sent(transport), timeout=5.0)
        view, _ = facade.state("s")
        assert [(d.delegation_id, d.mode) for d in view.live.delegations] == [("d1", "queue"), ("d2", "steer")]
        finals = [m for m in transport.sent if m["type"] == "session.context.append"]
        assert "".join(m["content"][0]["text"] for m in finals) == "both requests done"
        view_appends = facade.state("s")[0].live.appends
        assert [(a.channel, a.delegation_id) for a in view_appends if a.channel == "final"] == [("final", "d2")]
        assert any(m["delegation_item_id"] == "d1" and "merged" in m["content"][0]["text"] for m in transport.sent if "content" in m)
        steered = [m for m in view.messages if m.role == "user" and m.origin and m.origin["delegation_id"] == "d2"]
        assert steered and steered[0].text == compose_request("and mention the size", [])[0]
    finally:
        await runtime.aclose()


async def test_permission_request_is_announced_and_never_resolved_by_the_bridge(tmp_path):
    provider = ScriptedProvider(tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})), text_response("done"))
    runtime, facade, transport = await make_call(tmp_path, provider)
    view_stream = facade.subscribe("s", 0, follow=True, client_id="user")  # the on-screen client: a viewer
    await view_stream.__anext__()
    try:
        transport.push(delegation("d1", "write b.txt"))
        await settle(lambda: any("on screen" in t for t in transport.texts()), timeout=5.0)
        notice = next(m for m in transport.sent if "content" in m and "on screen" in m["content"][0]["text"])
        assert notice["type"] == "delegation.context.append" and notice["delegation_item_id"] == "d1"
        assert notice["channel"] == "speakable"
        assert "cannot be approved by voice" in notice["content"][0]["text"]
        await asyncio.sleep(0.05)
        view, _ = facade.state("s")
        pending = view.pending_permissions
        assert len(pending) == 1  # still waiting for the user: nothing granted it
        assert not (tmp_path / "b.txt").exists()
        # the user answers on screen
        assert facade.resolve_permission("s", pending[0].id, "deny_once", client_id="user")
        await settle(lambda: final_sent(transport), timeout=5.0)
    finally:
        await view_stream.aclose()
        await runtime.aclose()


async def test_progress_commentary_uses_the_commentary_channel(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    provider = ScriptedProvider(tool_response(("c1", "Read", {"path": "a.txt"})), text_response("fine"))
    runtime, facade, transport = await make_call(tmp_path, provider)
    try:
        transport.push(delegation("d1", "read a.txt"))
        await settle(lambda: final_sent(transport), timeout=5.0)
        progress = [m for m in transport.sent if m.get("channel") == "commentary"]
        assert len(progress) == 1 and "Read" in progress[0]["content"][0]["text"]
        view, _ = facade.state("s")
        assert [a.channel for a in view.live.appends] == ["commentary", "final"]
    finally:
        await runtime.aclose()


async def test_ending_the_call_does_not_cancel_the_running_turn(tmp_path):
    gate = asyncio.Event()
    provider = ScriptedProvider([Wait(gate), *text_response("finished after hangup")])
    runtime, facade, transport = await make_call(tmp_path, provider)
    try:
        transport.push(delegation("d1", "slow work"))
        await settle(lambda: runtime.session("s").active_turn_id is not None)
        await facade.handle(p.LiveStop(session="s"))
        assert runtime.session("s").active_turn_id is not None
        gate.set()
        await facade.wait_idle(timeout=5.0)
        view, _ = facade.state("s")
        assert view.turns[-1].phase == "completed" and view.live.status == "ended"
        assert not final_sent(transport)  # nobody left to tell
        assert [e.type for e in runtime.session("s").events].count("live.ended") == 1
    finally:
        await runtime.aclose()


async def test_delegated_event_records_source_heard_and_verbatim():
    bridge, _s, emitted, _p, _c = unit_bridge()
    await bridge.on_delegation("d1", "Okay, read the README.", heard="okay read the readme")
    await bridge.on_delegation("d2", "Summarize README.md and report only.", heard="okay read the readme")
    await bridge.on_delegation("d3", "no transcript yet")
    rows = [d for k, d in emitted if k == "live.delegated"]
    assert [r["source"] for r in rows] == ["delegation"] * 3
    assert [r["verbatim"] for r in rows] == [True, False, False]
    assert rows[1]["heard"] == "okay read the readme" and rows[2]["heard"] == ""


# -- Codex-style handoff: transcript context and the speech note -------------

async def test_delegation_message_carries_the_labelled_transcript_and_speech_note():
    bridge, _s, emitted, _p, _c = unit_bridge()
    host = bridge._host
    transcript = [("user", "hello there"), ("assistant", "Hi, what can I do?"),
                  ("user", "um check the readme"), ("assistant", "Sure."), ("user", "Check the README.")]
    await bridge.on_delegation("d1", "Check the README.", heard="check the readme", transcript=transcript)
    _sid, message, mode, origin = host.enqueued[0]
    assert message == (
        "Voice request\nCheck the README.\n\n"
        "Spoken conversation since the previous request\n"
        "You: hello there\nVoice: Hi, what can I do?\nYou: um check the readme\nVoice: Sure.\n\n"
        + message.rsplit("\n\n", 1)[1]
    )
    assert "speech recognition output" in message and origin == {"kind": "live", "delegation_id": "d1"}
    assert host.titles == ["Check the README."]  # the session title is the request, not the wrapper
    row = next(d for k, d in emitted if k == "live.delegated")
    assert row["text"] == "Check the README." and row["verbatim"] is True
    assert row["transcript"].startswith("You: hello there") and row["transcript"].endswith("Voice: Sure.")
    assert row["transcript_omitted"] == 0 and row["from_transcript"] is False


def test_empty_delegation_uses_the_transcript_as_the_request():
    message, facts = compose_request("  ", [("user", "run the tests"), ("assistant", "ok")])
    assert message.startswith("Voice request (taken from the spoken conversation)\nYou: run the tests\nVoice: ok")
    assert "Spoken conversation since" not in message and facts["from_transcript"] is True
    assert compose_request("", []) == ("", {})


def test_transcript_and_input_are_bounded_and_clipping_is_announced():
    long_turns = [("user", f"turn {i} " + "é" * 400) for i in range(40)]
    message, facts = compose_request("x" * 10_000, long_turns)
    assert facts["input_omitted"] > 0 and facts["transcript_omitted"] > 0
    assert "more characters omitted]" in message and "earlier characters omitted]" in message
    body = message.split("Spoken conversation since the previous request\n")[1].split("\n\nNote:")[0]
    kept = body.split("\n", 1)[1]
    assert len(kept.encode()) <= MAX_TRANSCRIPT_BYTES and kept.rstrip().endswith("é" * 400)  # the end is kept
    request = message.split("\n\n")[0].split("\n", 1)[1]
    assert request.startswith("xxx") and len(request.encode()) <= MAX_INPUT_BYTES + 60


async def test_empty_delegation_and_empty_transcript_is_still_rejected():
    bridge, sent, emitted, *_ = unit_bridge()
    await bridge.on_delegation("d9", "  ", transcript=[])
    assert "empty" in sent[0][0] and not bridge._host.enqueued
