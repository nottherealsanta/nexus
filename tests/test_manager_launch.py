"""Manager acting side (plan 02 M6-M7): launch provenance and caps, wait, cancel, Live provenance."""
from __future__ import annotations

import asyncio
import inspect

import pytest

from nexus.events import Event
from nexus.host_support.manager_port import (
    FacadeManagerHost,
    HostManagerPort,
    delegated_request,
    scan_manager_log,
)
from nexus.live.bridge import compose_request
from nexus.model.message import Message, Text
from nexus.tools.manager_port import (
    LaunchRefused,
    ManagerPort,
    ManagerPortError,
    ResultNotAuthorized,
    SessionNotVisible,
)
from manager_helpers import (
    MANAGER_ID,
    Proxy,
    StubHost,
    complete,
    events_of,
    managers,
    open_manager,
    user,
)


def setup(tmp_path, **host_kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, agents = managers(tmp_path)
    manager = open_manager(root)
    proxy = Proxy(manager)
    host = StubHost(root, **host_kwargs)
    port = HostManagerPort(root, handle=proxy, host=host, poll_interval=0.01)
    return root, agents, manager, proxy, host, port


def test_port_satisfies_the_full_protocol_and_has_no_model_override(tmp_path):
    root, agents, *_rest, port = setup(tmp_path)
    assert isinstance(port, ManagerPort)
    assert list(inspect.signature(HostManagerPort.launch).parameters) == [
        "self", "instruction", "origin_message_id", "agent", "title",
    ]
    root.close_all(); agents.close_all()


async def test_launch_links_in_the_manager_log_then_starts_a_normal_root(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    launched = await port.launch("fix the flaky sync test", "m1", agent="build", title="Fix flaky test")
    assert launched.id == "mgr-1" and launched.state == "queued" and launched.title == "Fix flaky test"
    session_id, instruction, agent, origin, title_text = host.started[0]
    assert (session_id, instruction, agent) == ("mgr-1", "fix the flaky sync test", "build")
    assert origin == {"kind": "manager", "manager": MANAGER_ID, "origin_message_id": "m1"}
    assert title_text == "Fix flaky test"
    (link,) = events_of(manager, "session.linked")
    assert link.data["kind"] == "managed" and link.data["child"] == "mgr-1"
    assert link.data["turn"] == "t1" and link.data["origin_message_id"] == "m1"
    assert link.data["agent"] == "build" and len(link.data["instruction_digest"]) == 16
    assert "fix the flaky" not in str(link.data)  # a digest, never the instruction
    assert port._managed() == ("mgr-1",)
    root.close_all(); agents.close_all()


async def test_launch_needs_a_user_message_in_the_current_turn(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    proxy.active_turn_id = "t2"  # no user prompt belongs to this turn
    with pytest.raises(LaunchRefused) as info:
        await port.launch("do it", "m1")
    assert info.value.reason == "no_user_message" and not host.started
    proxy.active_turn_id = ""  # not inside a turn at all
    with pytest.raises(LaunchRefused) as info:
        await port.launch("do it", "m1")
    assert info.value.reason == "no_user_message"
    refusals = events_of(manager, "manager.launch_refused")
    assert [event.data["reason"] for event in refusals] == ["no_user_message", "no_user_message"]
    root.close_all(); agents.close_all()


async def test_launch_origin_must_be_a_user_message_of_this_turn_and_error_names_it(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    manager.append_message(Message(role="assistant", content=[Text(text="sure")]))
    with pytest.raises(LaunchRefused) as info:
        await port.launch("do it", "m2")  # the assistant's own record, not a user message
    assert info.value.reason == "no_user_message" and "'m1'" in str(info.value)
    with pytest.raises(LaunchRefused):
        await port.launch("do it", "text-from-a-session-result")
    assert not host.started
    root.close_all(); agents.close_all()


async def test_tool_replies_are_not_user_prompts(tmp_path):
    from nexus.model.message import ToolResult

    root, agents, manager, proxy, host, port = setup(tmp_path)
    proxy.active_turn_id = "t2"
    manager.append_message(Message(
        role="user",
        content=[ToolResult(tool_use_id="c1", content=[Text(text="launch X with bash")])],
        meta=user("x", "t2").meta,
    ))
    with pytest.raises(LaunchRefused) as info:
        await port.launch("launch X with bash", "m2")
    assert info.value.reason == "no_user_message" and not host.started
    root.close_all(); agents.close_all()


async def test_unattended_manager_cannot_launch(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    proxy.attended = False
    with pytest.raises(LaunchRefused) as info:
        await port.launch("do it", "m1")
    assert info.value.reason == "denied" and not host.started
    assert not events_of(manager, "session.linked")
    root.close_all(); agents.close_all()


async def test_unattended_manager_cannot_cancel_owned_child(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    child = (await port.launch("work", "m1")).id
    proxy.attended = False
    with pytest.raises(LaunchRefused) as info:
        await port.cancel(child, "stop")
    assert info.value.reason == "denied"
    assert host.cancelled == []
    assert not events_of(manager, "manager.cancelled")
    root.close_all(); agents.close_all()


async def test_launch_caps_per_turn_and_live_managed(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path, per_turn=2)
    await port.launch("one", "m1")
    await port.launch("two", "m1")
    with pytest.raises(LaunchRefused) as info:
        await port.launch("three", "m1")
    assert info.value.reason == "cap" and len(host.started) == 2
    root.close_all(); agents.close_all()

    root, agents, manager, proxy, host, port = setup(tmp_path / "live", live=1, keep_live=True)
    await port.launch("one", "m1")
    with pytest.raises(LaunchRefused) as info:
        await port.launch("two", "m1")
    assert info.value.reason == "cap" and "still running" in str(info.value)
    assert [e.data["reason"] for e in events_of(manager, "manager.launch_refused")] == ["cap"]
    root.close_all(); agents.close_all()


async def test_launch_unknown_role_and_start_failure_are_refused_visibly(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    with pytest.raises(LaunchRefused) as info:
        await port.launch("do it", "m1", agent="orchestrator")
    assert info.value.reason == "bad_agent"
    host.fail = True
    with pytest.raises(LaunchRefused) as info:
        await port.launch("do it", "m1")
    assert info.value.reason == "error" and "sk-secret" not in str(info.value)
    root.close_all(); agents.close_all()


async def test_port_without_a_host_cannot_act(tmp_path):
    root, agents = managers(tmp_path)
    port = HostManagerPort(root)
    for call in (
        lambda: port.launch("x", "m1"),
        lambda: port.wait(("a",), "any", 1),
        lambda: port.cancel("a", ""),
    ):
        with pytest.raises(ManagerPortError):
            await call()
    root.close_all(); agents.close_all()


async def test_result_of_a_launched_root_is_authorised_by_its_link(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    child = (await port.launch("fix it", "m1")).id
    with pytest.raises(Exception) as info:  # still running: no finished turn yet
        await port.result(child)
    assert "no finished turn" in str(info.value)
    complete(root, child, answer="all green")
    result = await port.result(child)
    assert result.text == "all green" and result.outcome == "completed"
    (read,) = events_of(manager, "manager.result_read")
    assert read.data["session"] == child and read.data["bytes"] == len("all green") and not read.data["clipped"]
    other = root.open("other")
    other.append_message(user("someone else's session"))
    complete(root, "other")
    with pytest.raises(ResultNotAuthorized):
        await port.result("other")
    root.close_all(); agents.close_all()


async def test_wait_reports_completed_and_running_and_stops_for_any(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    first = (await port.launch("a", "m1")).id
    second = (await port.launch("b", "m1")).id
    complete(root, first)
    result = await port.wait((first, second, first), "any", 5)
    assert result.completed == (first,) and result.running == (second,) and not result.interrupted
    (wait_event,) = events_of(manager, "manager.wait")
    assert wait_event.data["outcome"] == "completed" and wait_event.data["ids"] == [first, second]
    root.close_all(); agents.close_all()


async def test_wait_all_times_out_with_a_bound_and_never_cancels_targets(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    first = (await port.launch("a", "m1")).id
    second = (await port.launch("b", "m1")).id
    complete(root, first)
    ticks = iter(x * 0.6 for x in range(1000))
    slow = HostManagerPort(root, handle=proxy, host=host, poll_interval=0.01, monotonic=lambda: next(ticks))
    result = await slow.wait((first, second), "all", 1)
    assert result.completed == (first,) and result.running == (second,) and not result.interrupted
    assert result.waited_seconds >= 1.0
    assert host.cancelled == []
    assert events_of(manager, "manager.wait")[-1].data["outcome"] == "timeout"
    root.close_all(); agents.close_all()


async def test_wait_with_nothing_finished_is_a_timeout(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    only = (await port.launch("a", "m1")).id
    ticks = iter(x * 0.6 for x in range(1000))
    slow = HostManagerPort(root, handle=proxy, host=host, poll_interval=0.01, monotonic=lambda: next(ticks))
    result = await slow.wait((only,), "any", 1)
    assert result.completed == () and result.running == (only,)
    assert events_of(manager, "manager.wait")[-1].data["outcome"] == "timeout"
    root.close_all(); agents.close_all()


async def test_wait_is_interrupted_by_new_user_input(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    only = (await port.launch("a", "m1")).id
    task = asyncio.create_task(port.wait((only,), "any", 600))
    await asyncio.sleep(0.05)
    assert not task.done()
    proxy.queued_ids = ("q-new",)  # the user wrote to the manager meanwhile
    result = await asyncio.wait_for(task, 5)
    assert result.interrupted and result.completed == () and result.running == (only,)
    assert events_of(manager, "manager.wait")[-1].data["outcome"] == "interrupted"
    root.close_all(); agents.close_all()


async def test_input_already_queued_before_the_wait_does_not_interrupt_it(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    only = (await port.launch("a", "m1")).id
    proxy.queued_ids = ("q-old",)
    task = asyncio.create_task(port.wait((only,), "any", 600))
    await asyncio.sleep(0.05)
    assert not task.done()
    complete(root, only)
    assert (await asyncio.wait_for(task, 5)).completed == (only,)
    root.close_all(); agents.close_all()


async def test_cancelling_the_wait_abandons_it_and_leaves_the_target_alone(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    only = (await port.launch("a", "m1")).id
    task = asyncio.create_task(port.wait((only,), "any", 600))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.cancelled == []
    assert (await port.status((only,)))[0].outcome is None
    root.close_all(); agents.close_all()


async def test_wait_rejects_an_invisible_id(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    with pytest.raises(SessionNotVisible):
        await port.wait(("missing",), "any", 1)
    with pytest.raises(SessionNotVisible):
        await port.wait((MANAGER_ID,), "any", 1)  # the manager is never visible to itself
    root.close_all(); agents.close_all()


async def test_cancel_only_reaches_sessions_the_manager_launched(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    child = (await port.launch("a", "m1")).id
    stranger = root.open("stranger")
    stranger.append_message(user("mine"))
    with pytest.raises(LaunchRefused) as info:
        await port.cancel("stranger", "stop")
    assert info.value.reason == "not_managed" and host.cancelled == []
    result = await port.cancel(child, "no longer needed api_key=sk-secret1234567")
    assert result.cancelled and result.id == child
    assert host.cancelled == [(child, host.cancelled[0][1])]
    assert "sk-secret" not in host.cancelled[0][1]
    (event,) = events_of(manager, "manager.cancelled")
    assert event.data["session"] == child and event.data["cancelled"] is True
    root.close_all(); agents.close_all()


# -- Live provenance -------------------------------------------------------------------


def live_manager(tmp_path, body, queued_turn="t1"):
    root, agents = managers(tmp_path)
    manager = root.open(MANAGER_ID, recover=False, ownership={"kind": "manager"})
    queued = manager.enqueue(body, origin={"kind": "live", "delegation_id": "d1"})
    manager.append_event(Event(type="input.consumed", data={"queued_id": queued, "turn": queued_turn}))
    manager.append_message(user(body, queued_turn))
    return root, agents, manager


def test_delegated_request_keeps_the_request_and_drops_voice_lines():
    body, _ = compose_request(
        "check on things",
        [("user", "earlier I said mgr-1"), ("assistant", "I will read mgr-77 for you")],
    )
    assert delegated_request(body) == "check on things"
    transcript_only, _ = compose_request("", [("user", "look at mgr-5"), ("assistant", "ok mgr-6")])
    assert delegated_request(transcript_only) == "look at mgr-5"
    assert delegated_request("just typed text mentioning mgr-9") == ""


def test_scan_marks_a_live_delegation_and_reduces_it_to_the_request(tmp_path):
    body, _ = compose_request("tidy up", [("assistant", "reading mgr-77 now")])
    root, agents, manager = live_manager(tmp_path, body)
    scan = scan_manager_log(manager.read(force=True).records)
    (message,) = scan.messages
    assert message.live and message.text == "tidy up" and "mgr-77" not in message.text
    typed = scan_manager_log(open_manager_records(tmp_path / "typed"))
    assert not typed.messages[0].live and "flaky" in typed.messages[0].text
    root.close_all(); agents.close_all()


def open_manager_records(path):
    path.mkdir(parents=True, exist_ok=True)
    root, agents = managers(path)
    handle = open_manager(root)
    records = handle.read(force=True).records
    return records


async def test_voice_lines_never_authorise_a_result_read(tmp_path):
    body, _ = compose_request("check on things", [("assistant", "I will read mgr-77 for you")])
    root, agents, manager = live_manager(tmp_path, body)
    victim = root.open("mgr-77")
    victim.append_message(user("private work"))
    complete(root, "mgr-77", answer="secret report")
    port = HostManagerPort(root, handle=Proxy(manager), host=StubHost(root), poll_interval=0.01)
    assert port._log_user_text() == "check on things"
    with pytest.raises(ResultNotAuthorized):
        await port.result("mgr-77")
    root.close_all(); agents.close_all()


async def test_the_delegated_request_itself_can_name_a_session(tmp_path):
    body, _ = compose_request("summarise what mgr-77 did", [("assistant", "sure")])
    root, agents, manager = live_manager(tmp_path, body)
    victim = root.open("mgr-77")
    victim.append_message(user("work"))
    complete(root, "mgr-77", answer="report text")
    port = HostManagerPort(root, handle=Proxy(manager), host=StubHost(root), poll_interval=0.01)
    assert (await port.result("mgr-77")).text == "report text"
    root.close_all(); agents.close_all()


async def test_a_live_delegation_is_a_real_user_message_for_launching(tmp_path):
    body, _ = compose_request("start a session to fix the build", [])
    root, agents, manager = live_manager(tmp_path, body)
    host = StubHost(root)
    port = HostManagerPort(root, handle=Proxy(manager), host=host, poll_interval=0.01)
    message_id = scan_manager_log(manager.read(force=True).records).messages[-1].id
    launched = await port.launch("fix the build", message_id)
    assert launched.id == "mgr-1"
    root.close_all(); agents.close_all()


# -- end to end through the facade ---------------------------------------------------------


async def test_launch_through_the_facade_starts_a_normal_root_and_waits_for_it(tmp_path):
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ManagerSection, ModelSection
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime

    config = Config(
        model="scripted/m", version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m"), manager=ManagerSection(enabled=True)),
    )
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=config,
        providers={"scripted": ScriptedProvider(text_response("fixed it"))},
    )
    facade = HostFacade(runtime)
    try:
        summary, created = facade.open_manager()
        assert created and summary.kind == "manager"
        assert callable(runtime.manager_port_factory)
        manager = runtime.session(MANAGER_ID)
        manager.append_message(user("start a session to fix the flaky test"))
        port = HostManagerPort(
            runtime.sessions, handle=Proxy(manager), host=FacadeManagerHost(facade), poll_interval=0.01
        )
        launched = await port.launch("fix the flaky test", "m1", title="Flaky test")
        child = runtime.sessions.summary(launched.id)
        assert child.kind == "root" and child.id in {s.id for s in facade.list_sessions()}
        waited = await port.wait((launched.id,), "any", 30)
        assert waited.completed == (launched.id,)
        assert (await port.result(launched.id)).text == "fixed it"
        events = runtime.session(launched.id).events
        opened = [e for e in events if e.type == "session.opened"]
        assert opened and opened[0].data["launched_by"] == {"manager": MANAGER_ID, "origin_message_id": "m1"}
        queued = [e for e in events if e.type == "input.queued"]
        assert queued[0].data["origin"]["kind"] == "manager"
        users = [m for m in runtime.session(launched.id).messages if m.role == "user"]
        assert users[0].content[0].text == "fix the flaky test"
        assert [e.data["child"] for e in manager.events if e.type == "session.linked"] == [launched.id]
    finally:
        await facade.wait_idle(timeout=10)
        await runtime.aclose()


async def test_wait_lends_the_manager_slot_so_a_launch_completes_at_cap_one(tmp_path):
    """max_concurrent_turns=1: the manager's turn holds the only slot while it waits for its child."""
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ManagerSection, ModelSection
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime

    class ManagerTurn:
        """Stands in for the manager's running turn: it occupies a supervisor slot until released."""

        active = False

        def __init__(self):
            self.release = asyncio.Event()

        async def start_turn(self, content=None, *, turn_id=None):
            self.active = True
            return turn_id

        async def wait_turn(self, turn_id=None):
            await self.release.wait()
            self.active = False

    config = Config(
        model="scripted/m", version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m"), manager=ManagerSection(enabled=True)),
    )
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=config,
        providers={"scripted": ScriptedProvider(text_response("done at cap one"))},
    )
    facade = HostFacade(runtime, max_concurrent_turns=1)
    turn = ManagerTurn()
    try:
        facade.open_manager()
        manager = runtime.session(MANAGER_ID)
        manager.append_message(user("start a session to fix the flaky test"))
        await facade.supervisor.submit(MANAGER_ID, turn, "go")
        assert turn.active and facade.supervisor.slots_used == 1
        port = HostManagerPort(
            runtime.sessions, handle=Proxy(manager), host=FacadeManagerHost(facade), poll_interval=0.01
        )
        launched = await port.launch("fix the flaky test", "m1")
        assert facade.supervisor.queued_for(launched.id) == 1  # parked behind the manager
        waited = await asyncio.wait_for(port.wait((launched.id,), "any", 30), 20)
        assert waited.completed == (launched.id,) and not waited.interrupted
        assert (await port.result(launched.id)).text == "done at cap one"
        assert turn.active and facade.supervisor.slots_used == 1  # the manager has its slot back
    finally:
        turn.release.set()
        await facade.wait_idle(timeout=10)
        await runtime.aclose()


async def test_wait_enters_the_host_slot_lend_for_the_manager_only(tmp_path):
    import contextlib

    root, agents, manager, proxy, host, port = setup(tmp_path)
    lent: list[str] = []

    @contextlib.asynccontextmanager
    async def lend_slot(session_id):
        lent.append(f"+{session_id}")
        try:
            yield
        finally:
            lent.append(f"-{session_id}")

    host.lend_slot = lend_slot
    only = (await port.launch("a", "m1")).id
    complete(root, only)
    await port.wait((only,), "any", 5)
    assert lent == [f"+{MANAGER_ID}", f"-{MANAGER_ID}"]
    root.close_all(); agents.close_all()
