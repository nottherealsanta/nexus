"""Manager ``session_show``: a durable request the user's native client may act on (docs/manager.md).

Host side: provenance, visibility, the per-turn cap, the recorded ``manager.show`` and an honest
result. Client side: the event-to-switch path (``Manager.on_event`` and ``ManagerFollow``), replays,
stale and duplicate events, other clients, modals and prompts. No approvals anywhere.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection
from nexus.events import Event
from nexus.host_support.manager_port import HostManagerPort, scan_manager_log
from nexus.model.message import Message, Text
from nexus.runtime import Runtime
from nexus.tools.manager_port import (
    MAX_SHOWS_PER_TURN,
    SHOW_TTL_SECONDS,
    LaunchRefused,
    SessionNotVisible,
)
from nexus.tools.manager_tools import build_manager_catalog
from nexus.tools.permissions import Outcome
from nexus.tools.spec import ToolCall
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.manager import SHOWN_MEMORY
from nexus.view import initial_state
from manager_helpers import MANAGER_ID, Proxy, StubHost, events_of, managers, open_manager, user

NOW = 1_700_000_000.0


class ShowHost(StubHost):
    def __init__(self, sessions, clients=(), **kwargs):
        super().__init__(sessions, **kwargs)
        self.clients = tuple(clients)
        self.asked: list[str] = []

    def focused_clients(self, session_id):
        self.asked.append(session_id)
        return self.clients if session_id == MANAGER_ID else ()


def target(root, session_id="tgt", text="go"):
    handle = root.open(session_id)
    handle.append_message(Message(role="user", content=[Text(text=text)]))
    return handle


def say(root, manager, proxy, text, turn):
    manager.append_message(user(text, turn))
    proxy.active_turn_id = turn
    return scan_manager_log(root.store.read(MANAGER_ID).records).messages[-1].id


def setup(tmp_path, clients=("c1",), clock=lambda: NOW):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, agents = managers(tmp_path)
    manager = open_manager(root)
    proxy = Proxy(manager)
    host = ShowHost(root, clients)
    port = HostManagerPort(root, handle=proxy, host=host, poll_interval=0.01, clock=clock)
    return root, agents, manager, proxy, host, port


# -- host port ------------------------------------------------------------------------------


async def test_show_records_a_durable_request_with_clients_and_expiry_but_no_user_text(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path, clients=("c1", "c2"))
    target(root)
    shown = await port.show("tgt", "m1")
    assert (shown.id, shown.state) == ("tgt", "requested")
    (event,) = events_of(manager, "manager.show")
    expected = {
        "target": "tgt", "origin_message_id": "m1", "turn": "t1", "clients": ["c1", "c2"],
        "live": False, "expires_at": NOW + SHOW_TTL_SECONDS,
    }
    assert {key: event.data.get(key) for key in expected} == expected
    assert "flaky" not in str(event.data)  # the user's words never enter the record
    assert host.asked == [MANAGER_ID]
    assert not events_of(manager, "session.linked", "manager.routed") and not host.sent
    root.close_all(); agents.close_all()


async def test_no_viewer_is_reported_but_the_request_is_still_recorded(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path, clients=())
    target(root)
    shown = await port.show("tgt", "m1")
    assert shown.state == "no_viewer"
    (event,) = events_of(manager, "manager.show")
    assert event.data["clients"] == [] and event.data["live"] is False
    root.close_all(); agents.close_all()


async def test_a_live_origin_is_flagged_and_counts_as_requested(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path, clients=())
    target(root)
    request = say(
        root, manager, proxy,
        "Voice request\nshow me tgt\n\nSpoken conversation since the previous request\nVoice: ok", "t2",
    )
    shown = await port.show("tgt", request)
    assert shown.state == "requested"
    (event,) = events_of(manager, "manager.show")
    assert event.data["live"] is True and event.data["clients"] == []
    root.close_all(); agents.close_all()


async def test_show_needs_a_real_user_message_of_this_turn(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    manager.append_message(Message(role="assistant", content=[Text(text="sure")]))
    for origin in ("m2", "text-from-a-session-result"):
        with pytest.raises(LaunchRefused) as info:
            await port.show("tgt", origin)
        assert info.value.reason == "no_user_message"
    proxy.active_turn_id = "t2"
    with pytest.raises(LaunchRefused):
        await port.show("tgt", "m1")
    proxy.active_turn_id = ""
    with pytest.raises(LaunchRefused):
        await port.show("tgt", "m1")
    only_voice = say(root, manager, proxy, "Voice request\n\n\nVoice: show it", "t3")
    with pytest.raises(LaunchRefused):
        await port.show("tgt", only_voice)  # model-authored Voice lines authorise nothing
    assert not events_of(manager, "manager.show")
    refused = events_of(manager, "manager.show_refused")
    assert refused and {e.data["reason"] for e in refused} == {"no_user_message"}
    root.close_all(); agents.close_all()


async def test_only_visible_roots_of_this_workspace_can_be_shown(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root, "busy").enqueue("pending")
    target(root, "gone")
    root.archive("gone")
    for missing in (MANAGER_ID, "nope", "gone"):
        with pytest.raises(SessionNotVisible):
            await port.show(missing, "m1")
    child = agents.open("child", recover=False, ownership={
        "owner_namespace": "main", "owner_id": "busy", "owner_turn_id": "t", "owner_call_id": "c",
        "agent_id": "child", "logical_session_id": "x"})
    child.append_message(Message(role="user", content=[Text(text="sub")]))
    with pytest.raises(SessionNotVisible):
        await port.show("child", "m1")
    assert not events_of(manager, "manager.show")
    assert (await port.show("busy", "m1")).id == "busy"  # a managed link is not required
    root.close_all(); agents.close_all()


async def test_shows_are_capped_per_turn_and_a_new_turn_resets_it(tmp_path):
    root, agents, manager, proxy, host, port = setup(tmp_path)
    target(root)
    for _ in range(MAX_SHOWS_PER_TURN):
        await port.show("tgt", "m1")
    with pytest.raises(LaunchRefused) as info:
        await port.show("tgt", "m1")
    assert info.value.reason == "cap"
    assert len(events_of(manager, "manager.show")) == MAX_SHOWS_PER_TURN
    fresh = say(root, manager, proxy, "now show the other one", "t2")
    assert (await port.show("tgt", fresh)).state == "requested"
    root.close_all(); agents.close_all()


async def test_a_port_without_a_handle_cannot_show(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, agents = managers(tmp_path)
    port = HostManagerPort(root)
    with pytest.raises(Exception, match="cannot"):
        await port.show("tgt", "m1")
    root.close_all(); agents.close_all()


# -- the tool and the runtime engine -------------------------------------------------------


class FakePort:
    def __init__(self, state="requested"):
        self.calls = []
        self.state = state

    async def show(self, session_id, origin_message_id):
        self.calls.append((session_id, origin_message_id))
        return SimpleNamespace(id=session_id, title="Flaky", state=self.state)


def _tool(port):
    return {t.name: t for t in build_manager_catalog(port)}["session_show"]


@pytest.mark.parametrize("state", ["requested", "no_viewer"])
async def test_tool_result_says_requested_or_no_viewer_never_shown(state):
    port = FakePort(state)
    ok = await _tool(port).run({"id": "s1", "origin_message_id": "m1"}, SimpleNamespace())
    assert not ok.is_error
    body = json.loads(ok.content[0].text)
    assert body == {"id": "s1", "title": "Flaky", "state": state}
    assert "shown" not in ok.content[0].text
    assert port.calls == [("s1", "m1")]


async def test_tool_forces_an_honest_state_and_validates_shape():
    port = FakePort("shown")  # a port can never make the tool claim success
    tool = _tool(port)
    ok = await tool.run({"id": "s1", "origin_message_id": "m1"}, SimpleNamespace())
    assert json.loads(ok.content[0].text)["state"] == "requested"
    port.calls.clear()
    for bad in ({"id": "s1"}, {"id": "", "origin_message_id": "m1"}, {"origin_message_id": "m1"},
                {"id": "s1", "origin_message_id": "m1", "client": "c1"}):
        assert (await tool.run(bad, SimpleNamespace())).is_error
    assert port.calls == []


@pytest.mark.parametrize("attended", [True, False])
def test_show_runs_without_approval_attended_or_not(tmp_path, attended):
    config = Config(model="scripted/sm", version=2, v2=ConfigV2(model=ModelSection(default="scripted/sm")))
    runtime = Runtime(
        tmp_path, home=tmp_path / "home", config=config, providers={},
        manager_port_factory=lambda session: FakePort(),
    )
    turn = runtime._make_tool_turn(
        config=None, session=SimpleNamespace(kind="manager", id="m1"), turn_id="t1", attended=attended
    )
    prepared = turn.manager.get("session_show")
    call = ToolCall(id="c1", name="session_show", input={"id": "s1", "origin_message_id": "m1"})
    evaluation = turn.engine.evaluate(call, prepared.spec, attended=attended)
    assert evaluation.outcome is Outcome.ALLOW and evaluation.code != "unattended_deny"
    assert prepared.spec.permission_key is None
    plan = turn.gate.plan(turn.manager.prepare([call]))
    assert [e.outcome for e in plan.evaluations] == [Outcome.ALLOW]


# -- the native client ----------------------------------------------------------------------


def show_event(seq=5, *, target="tgt", clients=("me",), live=False, ts=None, expires=None, session=MANAGER_ID, **over):
    ts = time.time() if ts is None else ts
    data = {"target": target, "origin_message_id": "m1", "turn": "t1", "clients": list(clients), "live": live,
            "expires_at": ts + SHOW_TTL_SECONDS if expires is None else expires}
    data.update(over)
    return Event(type="manager.show", data=data, seq=seq, ts=ts, session=session)


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    controller = SimpleNamespace(
        session=MANAGER_ID, client=SimpleNamespace(), view=initial_state(MANAGER_ID), running=False,
        resume=Mock(),
    )
    shell = ShellActions(controller)
    shell.workspace = "/w"
    shell.focus.client_id = "me"
    shell.on_update = AsyncMock()

    async def switch_project(workspace, session):
        controller.session = session
        controller.view = initial_state(session)

    shell.switch_project = AsyncMock(side_effect=switch_project)
    return shell


async def settle(shell):
    task = shell.manager._show_task
    if task is not None:
        await task


async def test_a_fresh_request_for_this_client_switches_in_its_own_task(shell):
    assert shell.manager.on_event(show_event()) is True
    assert shell.manager._show_task is not None  # retained, not awaited inside the stream
    await settle(shell)
    shell.switch_project.assert_awaited_once_with("/w", "tgt")
    assert shell.controller.session == "tgt"
    shell.controller.resume.assert_called_once_with(shell.on_update)  # the switch cancelled the stream task
    shell.on_update.assert_awaited()


async def test_a_duplicate_is_ignored_and_memory_is_bounded(shell):
    event = show_event()
    assert shell.manager.on_event(event) is True
    await settle(shell)
    shell.controller.session = MANAGER_ID
    assert shell.manager.on_event(event) is False
    await settle(shell)
    assert shell.switch_project.await_count == 1
    for seq in range(10, 10 + SHOWN_MEMORY * 2):
        shell.manager.on_event(show_event(seq, target=MANAGER_ID))
    assert len(shell.manager._shown) <= SHOWN_MEMORY


async def test_replayed_history_never_switches(shell):
    # Not past the reduced cursor (an initial or reconnect replay), however recent its timestamp.
    assert shell.manager.on_event(show_event(), fresh=False) is False
    # Older than the Live follow that reads it.
    assert shell.manager.on_event(show_event(ts=time.time() - 5), floor=time.time()) is False
    assert shell.manager._show_task is None and not shell.switch_project.await_count


@pytest.mark.parametrize("age", [SHOW_TTL_SECONDS + 1, 3600])
async def test_stale_requests_never_switch(shell, age):
    assert shell.manager.on_event(show_event(ts=time.time() - age, expires=time.time() + 100)) is False
    assert shell.manager.on_event(show_event(2, ts=time.time() - 2, expires=time.time() - 1)) is False  # expired
    assert shell.manager.on_event(show_event(3, expires=time.time() + 3600)) is False  # absurd expiry
    assert shell.manager._show_task is None


async def test_malformed_requests_are_ignored(shell):
    for bad in (dict(target=""), dict(target=7), dict(target="x" * 129), dict(expires_at="soon")):
        assert shell.manager.on_event(show_event(**bad)) is False
    assert shell.manager.on_event(Event(type="manager.show", data={}, seq=9, session=MANAGER_ID)) is False
    assert shell.manager._show_task is None


async def test_another_client_or_session_is_ignored(shell):
    assert shell.manager.on_event(show_event(clients=("someone-else",))) is False
    assert shell.manager.on_event(show_event(clients=())) is False
    # Listed, but the manager is no longer in front of this client.
    shell.controller.session = "other"
    assert shell.manager.on_event(show_event(2), session=MANAGER_ID) is False
    # Not the manager's log at all.
    shell.controller.session = "other"
    assert shell.manager.on_event(show_event(3, session="other"), session="other") is False
    assert shell.manager._show_task is None


async def test_a_listed_client_that_left_before_the_task_ran_does_not_switch(shell):
    assert shell.manager.on_event(show_event()) is True
    shell.controller.session = "elsewhere"  # the user moved before the task got to run
    await settle(shell)
    shell.switch_project.assert_not_awaited()


async def test_live_origin_needs_a_matching_active_live_follow(shell):
    event = show_event(live=True, clients=())
    assert shell.manager.on_event(event) is False  # no call open
    shell.live.state, shell.live.session, shell.live.workspace = "active", MANAGER_ID, "/w"
    assert shell.manager.on_event(show_event(2, live=True, clients=())) is False  # not following the manager
    shell.live.follow.session = MANAGER_ID
    shell.controller.session = "other"  # the user is elsewhere; the call is what targets this client
    assert shell.manager.on_event(show_event(3, live=True, clients=()), session=MANAGER_ID) is True
    await settle(shell)
    shell.switch_project.assert_awaited_once_with("/w", "tgt")
    # A Live flag does not widen the plain case: a non-Live request for another client still does nothing.
    shell.controller.session = MANAGER_ID
    assert shell.manager.on_event(show_event(4, live=False, clients=("x",))) is False


async def test_a_live_show_is_refused_when_another_project_is_in_front(shell):
    """The target id belongs to the call's workspace: never open it in whichever project is in front."""
    shell.live.state, shell.live.session, shell.live.workspace = "active", MANAGER_ID, "/w"
    shell.live.follow.session = MANAGER_ID
    shell.controller.session = "other"
    shell.workspace = "/elsewhere"
    assert shell.manager.on_event(show_event(live=True, clients=()), session=MANAGER_ID) is False
    assert shell.manager._show_task is None
    # Left for another project between the event and the task running: the task refuses too.
    shell.workspace = "/w"
    assert shell.manager.on_event(show_event(2, live=True, clients=()), session=MANAGER_ID) is True
    shell.workspace = "/elsewhere"
    await settle(shell)
    shell.switch_project.assert_not_awaited()


async def test_concurrent_project_switches_are_serialized(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    controller = SimpleNamespace(session="s", client=SimpleNamespace(), view=initial_state("s"), completion_seq=0)
    shell = ShellActions(controller)
    shell.workspace = "/w"
    running, overlap, order = 0, [], []

    async def switch(session):
        nonlocal running
        running += 1
        overlap.append(running)
        order.append(f"start:{session}")
        await asyncio.sleep(0.02)
        order.append(f"end:{session}")
        running -= 1

    shell.switch = switch
    await asyncio.gather(shell.switch_project("/w", "a"), shell.switch_project("/w", "b"))
    assert max(overlap) == 1
    assert order == ["start:a", "end:a", "start:b", "end:b"]


async def test_a_live_flag_on_a_stopped_call_is_ignored(shell):
    shell.live.state, shell.live.session = "idle", MANAGER_ID
    shell.live.follow.session = MANAGER_ID
    assert shell.manager.on_event(show_event(live=True, clients=())) is False


@pytest.mark.parametrize("setup_blocker, reason", [
    (lambda s: setattr(s.workflows, "form", {"id": "f"}), "a panel is open"),
    (lambda s: setattr(s, "panel_title", "Settings"), "a panel is open"),
    (lambda s: setattr(s, "items", [{"label": "x"}]), "a panel is open"),
    (lambda s: s.workflows.stack.append(("t",)), "a panel is open"),
    (lambda s: setattr(s.workflows, "dashboard", SimpleNamespace(visible=True)), "dashboard"),
    (lambda s: setattr(s.voice, "phase", "recording"), "voice"),
    (lambda s: setattr(s.controller, "view", SimpleNamespace(pending_permissions={"p1": 1})), "prompt"),
])
async def test_a_modal_form_or_prompt_is_never_replaced_a_toast_says_so(shell, setup_blocker, reason):
    setup_blocker(shell)
    assert shell.manager.on_event(show_event()) is False
    await settle(shell)
    shell.switch_project.assert_not_awaited()
    assert shell.controller.session == MANAGER_ID
    texts = [f"{t['title']} {t['body']}" for t in shell.toasts]
    assert any("asked to show tgt" in text and reason in text for text in texts), texts


async def test_showing_the_session_already_in_front_does_nothing(shell):
    shell.controller.session = "tgt"
    assert shell.manager.on_event(show_event(clients=("me",)), session="tgt") is False  # not the manager's log
    shell.controller.session = MANAGER_ID
    assert shell.manager.on_event(show_event(target=MANAGER_ID)) is False
    assert shell.manager._show_task is None


async def test_a_failed_switch_is_a_toast_not_a_crash(shell):
    shell.switch_project.side_effect = RuntimeError("daemon gone")
    assert shell.manager.on_event(show_event()) is True
    await settle(shell)
    assert any(t["level"] == "error" and "daemon gone" in f"{t['title']} {t['body']}" for t in shell.toasts)


async def test_only_one_switch_runs_at_a_time(shell):
    gate = asyncio.Event()

    async def slow(workspace, session):
        await gate.wait()
        shell.controller.session = session

    shell.switch_project.side_effect = slow
    assert shell.manager.on_event(show_event(1)) is True
    assert shell.manager.on_event(show_event(2, target="other")) is False
    gate.set()
    await settle(shell)
    assert shell.switch_project.await_count == 1
    await shell.manager.aclose()


async def test_follow_stream_replay_is_history_and_a_new_event_acts(shell):
    """The Live follow replays the whole manager log from seq 0: only an event after it began can act."""
    shell.live.state, shell.live.session, shell.live.workspace = "active", MANAGER_ID, "/w"
    shell.controller.session = "other"
    old = show_event(1, live=True, clients=(), ts=time.time() - 1, session=MANAGER_ID)
    delivered = asyncio.Event()

    async def stream(session, cursor, follow=True):
        yield old  # history, recent enough to be inside its own 30 s window
        await delivered.wait()
        yield show_event(2, live=True, clients=(), session=MANAGER_ID)
        await asyncio.sleep(3600)

    shell.controller.client = SimpleNamespace(stream=stream)
    shell.live.follow.start(MANAGER_ID)
    try:
        await asyncio.sleep(0.05)
        assert not shell.switch_project.await_count and shell.manager._show_task is None
        delivered.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if shell.manager._show_task is not None:
                break
        await settle(shell)
        shell.switch_project.assert_awaited_once_with("/w", "tgt")
    finally:
        await shell.live.follow.stop()
        await shell.manager.aclose()


class LogClient:
    """The client surface a controller reads, served from the real session log (no fabricated events)."""

    def __init__(self, root):
        self.root = root

    async def handshake(self): pass
    async def open_session(self, session): return None
    async def current_agent(self, session): return None

    async def stream(self, session, cursor, follow=False):
        handle = self.root.open(session)
        while True:
            for event in [e for e in handle.read(force=True).events() if e.seq > cursor]:
                cursor = event.seq
                yield event
            if not follow:
                return
            await asyncio.sleep(0.01)


async def test_real_controller_replay_is_history_live_acts_and_an_unlisted_client_stays_put(tmp_path, monkeypatch):
    """A recorded ``manager.show`` seen on bootstrap never switches; one recorded while following does, for the listed client only."""
    from nexus.ui.ratatui.controller import NativeController

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    root, agents = managers(logs)
    manager = open_manager(root)
    target(root)

    def record(clients):
        now = time.time()
        manager.append_event(Event(type="manager.show", session=MANAGER_ID, ts=now, data={
            "target": "tgt", "origin_message_id": "m1", "turn": "t1", "clients": clients, "live": False,
            "expires_at": now + SHOW_TTL_SECONDS}))

    record(["me"])  # history: recent and listed, but recorded before either client opened the log
    client = LogClient(root)

    def build(client_id):
        controller = NativeController(client, MANAGER_ID)
        shell = ShellActions(controller)
        shell.workspace = "/w"
        shell.focus.client_id = client_id
        shell.on_update = AsyncMock()

        async def switch_project(workspace, session):
            await controller.switch_session(session)
            await controller.bootstrap()

        async def post(event):
            if event:
                shell.manager.on_event(event, session=controller.session, fresh=event.seq > controller.cursor)
                controller.ingest(event)

        shell.switch_project = AsyncMock(side_effect=switch_project)
        return controller, shell, post

    mine, my_shell, my_post = build("me")
    other, other_shell, other_post = build("other")
    for controller, post in ((mine, my_post), (other, other_post)):
        await controller.bootstrap()
        controller.resume(post)
    try:
        await asyncio.sleep(0.1)
        assert not my_shell.switch_project.await_count and not other_shell.switch_project.await_count
        record(["me"])
        for _ in range(300):
            await asyncio.sleep(0.01)
            if my_shell.manager._show_task is not None:
                break
        await settle(my_shell)
        await asyncio.sleep(0.05)
        my_shell.switch_project.assert_awaited_once_with("/w", "tgt")
        assert mine.session == "tgt"
        assert not other_shell.switch_project.await_count and other.session == MANAGER_ID
        assert other_shell.manager._show_task is None
    finally:
        await mine.pause()
        await other.pause()
        await my_shell.manager.aclose()
        root.close_all(); agents.close_all()


async def test_focus_is_reported_before_the_replay_so_the_host_knows_the_manager_is_in_front(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    order: list[str] = []
    controller = SimpleNamespace(session="s", client=SimpleNamespace(), view=initial_state("s"), completion_seq=0)

    async def switch_session(session):
        controller.session = session

    async def bootstrap():
        order.append("bootstrap")

    controller.switch_session, controller.bootstrap = switch_session, bootstrap
    shell = ShellActions(controller)
    shell.workspace = "/w"
    shell.refresh_preview = AsyncMock(return_value=False)
    shell.focus.report = lambda session: order.append(f"focus:{session}")
    await shell.switch(MANAGER_ID)
    await shell.preview_task
    assert order == [f"focus:{MANAGER_ID}", "bootstrap"]
