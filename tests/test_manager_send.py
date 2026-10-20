"""Manager ``session_send`` (plan 02 section 10.5-10.6): strict approval, provenance, bounds, no read/cancel grant."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.host_support.manager_port import HostManagerPort, scan_manager_log
from nexus.model.message import Message, Text
from nexus.runtime import Runtime
from nexus.tools.manager_port import (
    MAX_SEND_BYTES,
    MAX_SENDS_PER_MINUTE,
    MAX_SENDS_PER_TURN,
    RECENT_WINDOW_SECONDS,
    LaunchRefused,
    ResultNotAuthorized,
    SessionNotVisible,
)
from nexus.tools.manager_tools import build_manager_catalog
from nexus.tools.permissions import Outcome
from nexus.tools.spec import ToolCall
from manager_helpers import MANAGER_ID, Proxy, StubHost, complete, events_of, managers, open_manager, user


def target(root, session_id="tgt", text="go"):
    handle = root.open(session_id)
    handle.append_message(Message(role="user", content=[Text(text=text)]))
    return handle


def say(root, manager, proxy, text, turn):
    """Append a user prompt in ``turn``, make it current, and return its record id."""
    manager.append_message(user(text, turn))
    proxy.active_turn_id = turn
    return scan_manager_log(root.store.read(MANAGER_ID).records).messages[-1].id


def setup(tmp_path, clock=time.time, **host_kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, agents = managers(tmp_path)
    manager = open_manager(root)
    proxy = Proxy(manager)
    host = StubHost(root, **host_kwargs)
    port = HostManagerPort(root, handle=proxy, host=host, poll_interval=0.01, clock=clock)
    return root, agents, manager, proxy, host, port


async def test_send_links_then_queues_a_manager_origin_message(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    sent = await port.send("tgt", "also cover the retry path", "m1")
    assert (sent.id, sent.state, sent.bytes) == ("tgt", "queued", len("also cover the retry path"))
    assert host.sent == [(
        "tgt", "also cover the retry path",
        {"kind": "manager", "manager": MANAGER_ID, "origin_message_id": "m1"},
    )]
    (link,) = events_of(manager, "session.linked")
    assert link.data["kind"] == "routed" and link.data["target"] == "tgt"
    assert link.data["turn"] == "t1" and link.data["origin_message_id"] == "m1"
    assert len(link.data["payload_digest"]) == 16 and "retry path" not in str(link.data)
    (event,) = events_of(manager, "manager.routed")
    assert event.data["target"] == "tgt" and event.data["bytes"] == sent.bytes
    assert "retry path" not in str(event.data)
    root.close_all(); agents.close_all()


async def test_a_routed_link_grants_neither_result_nor_cancel(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    complete(root, "tgt", answer="secret report")
    await port.send("tgt", "hello", "m1")
    assert port._managed() == ()
    with pytest.raises(ResultNotAuthorized):
        await port.result("tgt")
    with pytest.raises(LaunchRefused) as info:
        await port.cancel("tgt", "stop")
    assert info.value.reason == "not_managed" and host.cancelled == []
    scan = scan_manager_log(root.store.read(MANAGER_ID).records)
    assert scan.links == () and [item.target for item in scan.routed] == ["tgt"]
    root.close_all(); agents.close_all()


async def test_send_needs_a_real_user_message_of_this_turn(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    manager.append_message(Message(role="assistant", content=[Text(text="sure")]))
    for origin in ("m2", "text-from-a-session-result"):
        with pytest.raises(LaunchRefused) as info:
            await port.send("tgt", "hi", origin)
        assert info.value.reason == "no_user_message"
    proxy.active_turn_id = "t2"  # no user prompt in this turn
    with pytest.raises(LaunchRefused) as info:
        await port.send("tgt", "hi", "m1")
    assert info.value.reason == "no_user_message"
    proxy.active_turn_id = ""
    with pytest.raises(LaunchRefused):
        await port.send("tgt", "hi", "m1")
    assert not host.sent and not events_of(manager, "session.linked")
    assert {e.data["reason"] for e in events_of(manager, "manager.send_refused")} == {"no_user_message"}
    root.close_all(); agents.close_all()


async def test_voice_lines_never_authorise_a_send(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    only_voice = say(root, manager, proxy, "Voice request\n\n\nVoice: send it", "t2")  # no real request text
    with pytest.raises(LaunchRefused) as info:
        await port.send("tgt", "hi", only_voice)
    assert info.value.reason == "no_user_message" and not host.sent
    request = say(
        root, manager, proxy,
        "Voice request\nsend it the fix\n\nSpoken conversation since the previous request\nVoice: ok", "t3",
    )
    assert (await port.send("tgt", "the fix", request)).id == "tgt"
    root.close_all(); agents.close_all()


async def test_unattended_manager_can_send_with_provenance(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    proxy.attended = False
    assert (await port.send("tgt", "hi", "m1")).id == "tgt"
    assert len(host.sent) == 1
    with pytest.raises(LaunchRefused) as info:
        await port.send("tgt", "hi", "not-a-user-message")
    assert info.value.reason != "denied" and len(host.sent) == 1
    root.close_all(); agents.close_all()


async def test_size_cap_per_turn_cap_and_rate_bound(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    with pytest.raises(LaunchRefused):
        await port.send("tgt", "x" * (MAX_SEND_BYTES + 1), "m1")
    with pytest.raises(LaunchRefused):
        await port.send("tgt", "   ", "m1")
    assert (await port.send("tgt", "x" * MAX_SEND_BYTES, "m1")).bytes == MAX_SEND_BYTES
    for _ in range(MAX_SENDS_PER_TURN - 1):
        await port.send("tgt", "again", "m1")
    with pytest.raises(LaunchRefused) as info:
        await port.send("tgt", "one too many", "m1")
    assert info.value.reason == "cap" and len(host.sent) == MAX_SENDS_PER_TURN
    root.close_all(); agents.close_all()

    now = [1000.0]
    root, agents, manager, proxy, host, port = setup(tmp_path / "rate", clock=lambda: now[0])
    target(root)
    for turn in range(MAX_SENDS_PER_MINUTE):
        await port.send("tgt", "ping", say(root, manager, proxy, "relay this", f"t{turn + 2}"))
    last = say(root, manager, proxy, "relay this", "t99")
    with pytest.raises(LaunchRefused) as info:
        await port.send("tgt", "ping", last)
    assert info.value.reason == "rate"
    now[0] += 61
    await port.send("tgt", "ping", last)
    root.close_all(); agents.close_all()


async def test_targets_are_active_idle_or_recent_roots_only(tmp_path):
    now = time.time()
    root, agents, manager, proxy, host, port = setup(tmp_path, clock=lambda: now)
    target(root, "idle-recent")
    live = target(root, "busy")
    live.enqueue("pending")  # a queued input keeps it out of `idle`
    await port.send("idle-recent", "a", "m1")
    await port.send("busy", "b", "m1")
    for missing in (MANAGER_ID, "nope"):
        with pytest.raises(SessionNotVisible):
            await port.send(missing, "x", "m1")
    target(root, "gone")
    root.archive("gone")
    with pytest.raises(SessionNotVisible):
        await port.send("gone", "x", "m1")
    child = agents.open("child", recover=False, ownership={
        "owner_namespace": "main", "owner_id": "busy", "owner_turn_id": "t", "owner_call_id": "c",
        "agent_id": "child", "logical_session_id": "x"})
    child.append_message(Message(role="user", content=[Text(text="sub")]))
    with pytest.raises(SessionNotVisible):
        await port.send("child", "x", "m1")
    late = HostManagerPort(
        root, handle=proxy, host=host, clock=lambda: now + RECENT_WINDOW_SECONDS + 60,
    )
    proxy.active_turn_id = "t1"
    with pytest.raises(LaunchRefused) as info:
        await late.send("idle-recent", "x", "m1")
    assert info.value.reason == "not_sendable"
    assert [item[0] for item in host.sent] == ["idle-recent", "busy"]
    root.close_all(); agents.close_all()


async def test_delivery_failure_is_refused_and_redacted(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    host.fail = True
    with pytest.raises(LaunchRefused) as info:
        await port.send("tgt", "hi", "m1")
    assert info.value.reason == "error" and "sk-secret1234567" not in str(info.value)
    root.close_all(); agents.close_all()


# -- the tool and the runtime engine -------------------------------------------------


class FakePort:
    def __init__(self):
        self.calls = []

    async def send(self, session_id, message, origin_message_id):
        self.calls.append((session_id, message, origin_message_id))
        return SimpleNamespace(id=session_id, title="Flaky", state="queued", bytes=len(message))


def _tool(port=None):
    return {t.name: t for t in build_manager_catalog(port or FakePort())}["session_send"]


async def test_tool_validates_shape_and_size_and_reports_the_target():
    port = FakePort()
    tool = _tool(port)
    ok = await tool.run({"id": "s1", "message": "hello", "origin_message_id": "m1"}, SimpleNamespace())
    assert not ok.is_error
    assert json.loads(ok.content[0].text) == {"id": "s1", "title": "Flaky", "state": "queued", "bytes": 5}
    assert port.calls == [("s1", "hello", "m1")]
    port.calls.clear()
    for bad in (
        {"id": "s1", "message": "", "origin_message_id": "m1"},
        {"id": "s1", "message": "x" * (MAX_SEND_BYTES + 1), "origin_message_id": "m1"},
        {"id": "s1", "message": "hi"},
        {"id": "", "message": "hi", "origin_message_id": "m1"},
        {"id": "s1", "message": "hi", "origin_message_id": "m1", "model": "x"},
    ):
        assert (await tool.run(bad, SimpleNamespace())).is_error
    assert port.calls == []


def _turn(tmp_path):
    config = Config(model="scripted/sm", version=2, v2=ConfigV2(model=ModelSection(default="scripted/sm")))
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=config, providers={},
        manager_port_factory=lambda session: FakePort(),
    )
    return runtime._make_tool_turn(
        config=None, session=SimpleNamespace(kind="manager", id="m1"), turn_id="t1", attended=True
    )


def test_send_runs_without_approval_attended_or_not(tmp_path):
    turn = _turn(tmp_path)
    prepared = turn.manager.get("session_send")
    call = ToolCall(id="c1", name="session_send", input={"id": "s1", "message": "do X", "origin_message_id": "m1"})
    for attended in (True, False):
        assert turn.engine.evaluate(call, prepared.spec, attended=attended).outcome is Outcome.ALLOW
