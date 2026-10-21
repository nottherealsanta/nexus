"""Background subagents, plan phase 2: the pool, ``start``, root event port, ``wait``.

Runner-level cases drive ``SubagentRunner.start`` through a fake child factory and a
real :class:`~nexus.agents.pool.RootAgents`; runtime-level cases run a real
``Runtime`` with a scripted provider (no network). The blocking path
(``tests/test_subagent_runner.py``) is the regression proof that ``spawn`` is unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from nexus.agents import AgentPool, SubagentBudget, SubagentOutcome, SubagentUsage
from nexus.agents.pool import RootAgents
from nexus.config import Config
from nexus.config.schema import AgentsSection, AgentSection, ConfigV2, ModelSection, PermissionsSection
from nexus.events import Event
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response
from nexus.runtime import Runtime
from nexus.session.session import is_live_agent
from nexus.view import fold
from test_subagent_runner import Factory, Recorder, make_runner


class Port:
    """A root event port that keeps every persisted event."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


def _root(port=None, **kw) -> tuple[RootAgents, Port]:
    port = port or Port()
    root = RootAgents("root", event_port=port, **kw)
    return root, port


def _gated_factory(gate: asyncio.Event) -> Factory:
    async def behavior(spec):
        await spec.emit("text", {"text": "working"})
        await gate.wait()
        await spec.emit("text", {"text": "finished"})
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, status="completed",
            text="the report", usage=SubagentUsage(input_tokens=3, output_tokens=2),
        )

    return Factory(behavior)


# ---------------------------------------------------------------------------
# Runner-level
# ---------------------------------------------------------------------------


async def test_start_returns_before_the_child_finishes_and_events_use_the_root_port(tmp_path):
    gate = asyncio.Event()
    root, port = _root()
    launch = Recorder()
    runner = make_runner(tmp_path, _gated_factory(gate), pool=root)

    started = await asyncio.wait_for(
        runner.start({"prompt": "go", "subagent_type": "task"}, emit=launch, call_id="call-1"), 5
    )
    assert started.started and started.agent_id == started.session_id
    record = root.get(started.agent_id)
    assert record.state == "running" and not record.task.done()
    # Launch events went through the spawning call's emitter, not the port.
    assert launch.types() == ["agent.spawned"]
    assert launch.of("agent.spawned")[0]["background"] is True
    assert port.events == [] or "agent.completed" not in port.types()

    gate.set()
    await asyncio.wait_for(record.task, 5)
    assert record.state == "idle" and record.last_status == "completed" and record.tokens == 5
    assert port.types()[-1] == "agent.completed"
    completed = port.events[-1].data
    assert completed["run"] == 1 and completed["status"] == "completed"
    assert all(e.session == "root" for e in port.events)
    assert [e.data["agent"]["id"] for e in port.events] == [started.agent_id] * len(port.events)


async def test_blocking_spawn_is_unchanged_and_never_touches_the_port(tmp_path):
    root, port = _root()
    recorder = Recorder()
    runner = make_runner(tmp_path, Factory(), pool=root)
    outcome = await runner.spawn({"prompt": "go"}, emit=recorder)
    assert outcome.status == "completed"
    assert port.events == []
    assert recorder.types()[0] == "agent.spawned" and recorder.types()[-1] == "agent.completed"
    assert recorder.of("agent.spawned")[0]["background"] is False
    assert recorder.of("agent.completed")[0]["run"] == 1


async def test_start_without_a_pool_is_an_error(tmp_path):
    runner = make_runner(tmp_path, Factory())
    with pytest.raises(Exception, match="agent pool"):
        await runner.start({"prompt": "go"}, emit=Recorder())


async def test_start_refusal_returns_an_outcome_and_runs_nothing(tmp_path):
    root, _port = _root()
    factory = Factory()
    runner = make_runner(tmp_path, factory, pool=root)
    started = await runner.start({"prompt": "go", "subagent_type": "nope"}, emit=Recorder())
    assert not started.started and started.outcome.status == "refused"
    assert factory.specs == [] and root.running() == []


async def test_factory_failure_is_reported_by_the_background_run(tmp_path):
    root, port = _root()
    runner = make_runner(tmp_path, Factory(fail_on_build=True), pool=root)
    started = await runner.start({"prompt": "go"}, emit=Recorder())
    assert started.started  # the child is built inside the run, after launch
    record = root.get(started.agent_id)
    await asyncio.wait_for(record.task, 5)
    assert record.last_status == "failed" and root.running() == []
    assert port.events[-1].type == "agent.completed" and port.events[-1].data["status"] == "failed"
    assert runner.budget.live == 0 and runner.budget.active == 0


async def test_failure_while_preparing_concludes_inline_and_releases_the_slot(tmp_path):
    root, _port = _root()
    runner = make_runner(tmp_path, Factory(), pool=root)
    started = await runner.start({"prompt": "go", "worktree": True}, emit=Recorder())
    # Worktree requested with no isolation support: refused during preparation.
    assert not started.started and started.outcome.status == "refused"
    assert root.running() == [] and runner.budget.live == 0


async def test_agent_cancel_stops_only_that_child(tmp_path):
    from nexus.agents.runner import cancel_child

    gate = asyncio.Event()
    root, port = _root()
    runner = make_runner(tmp_path, _gated_factory(gate), pool=root)
    one = await runner.start({"prompt": "one"}, emit=Recorder())
    two = await runner.start({"prompt": "two"}, emit=Recorder())
    await asyncio.sleep(0)
    assert cancel_child(one.agent_id, "stop one")
    await asyncio.wait_for(root.get(one.agent_id).task, 5)
    assert root.get(one.agent_id).last_status == "cancelled"
    assert root.get(two.agent_id).state == "running"
    gate.set()
    await asyncio.wait_for(root.get(two.agent_id).task, 5)
    statuses = {e.data["agent"]["id"]: e.data["status"] for e in port.events if e.type == "agent.completed"}
    assert statuses == {one.agent_id: "cancelled", two.agent_id: "completed"}


async def test_max_live_refuses_the_next_child_and_a_finished_slot_frees_up(tmp_path):
    gate = asyncio.Event()
    root, _port = _root()
    budget = SubagentBudget(max_concurrent=4, max_depth=1, max_live=1)
    runner = make_runner(tmp_path, _gated_factory(gate), pool=root, budget=budget)
    first = await runner.start({"prompt": "one"}, emit=Recorder())
    second = await runner.start({"prompt": "two"}, emit=Recorder())
    assert first.started and not second.started
    assert second.outcome.status == "refused" and "live 1/1" in second.outcome.error
    gate.set()
    await asyncio.wait_for(root.get(first.agent_id).task, 5)
    assert budget.live == 0
    third = await runner.start({"prompt": "three"}, emit=Recorder())
    assert third.started
    await asyncio.wait_for(root.get(third.agent_id).task, 5)


async def test_budget_window_resets_per_root_turn_but_concurrency_spans_turns(tmp_path):
    root, _port = _root()
    made: list[SubagentBudget] = []

    def factory():
        made.append(SubagentBudget(max_concurrent=1, max_depth=1, max_fanout=2, token_budget=100))
        return made[-1]

    one = root.budget_window("t1", signature="s", factory=factory)
    assert await one.reserve(tokens=10) is not None and await one.reserve(tokens=10) is not None
    assert await one.reserve(tokens=10) is None  # fan-out 2 in this window
    await one.acquire()  # a child still running when the next turn starts
    again = root.budget_window("t1", signature="s", factory=factory)
    assert again is one and one.children == 2  # same turn: no reset
    two = root.budget_window("t2", signature="s", factory=factory)
    assert two is one and one.children == 0 and one.spent_tokens == 0
    assert one.active == 1  # concurrency follows the child across turns
    assert await one.reserve(tokens=10) is not None
    # A changed limit applies only once no child is running or held.
    assert root.budget_window("t3", signature="other", factory=factory) is one
    one.release()
    for _ in range(one.live):
        one.release_live()
    assert root.budget_window("t4", signature="other", factory=factory) is not one


async def test_pool_aclose_cancels_children_and_leaves_no_task_or_live_marker(tmp_path):
    gate = asyncio.Event()  # never set
    live: list[tuple[str, bool]] = []
    root, port = _root(live_hook=lambda agent_id, on: live.append((agent_id, on)))
    pool = AgentPool()
    pool._roots["root"] = root
    runner = make_runner(tmp_path, _gated_factory(gate), pool=root)
    started = await runner.start({"prompt": "go"}, emit=Recorder())
    await asyncio.sleep(0)
    assert live == [(started.agent_id, True)]
    await pool.aclose(grace=5)
    task = root.get(started.agent_id).task
    assert task.done() and live[-1] == (started.agent_id, False)
    completed = [e for e in port.events if e.type == "agent.completed"]
    assert completed[-1].data["status"] == "cancelled"
    assert pool.get("root") is None


# ---------------------------------------------------------------------------
# Runtime-level
# ---------------------------------------------------------------------------


def _config(*, background: bool = True, **agents) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="allow"),
            agents=AgentsSection(background=background, **agents),
        ),
    )


class Routed(ScriptedProvider):
    """Root and child calls draw from separate scripts so their interleaving cannot race."""

    def __init__(self, root, child):
        super().__init__()
        self.root = ScriptedProvider(*root)
        self.child = ScriptedProvider(*child)

    def stream(self, req):
        first = next((m for m in req.messages if m.role == "user"), None)
        text = "".join(getattr(b, "text", "") for b in (first.content if first else []))
        return (self.child if "CHILD-TASK" in text else self.root).stream(req)

    async def aclose(self):
        await self.root.aclose()
        await self.child.aclose()
        await super().aclose()


SPAWN = ("spawn", "subagent", {"prompt": "CHILD-TASK do the work", "subagent_type": "general"})


def _agent_task(runtime: Runtime, session_id: str):
    [record] = list(runtime.agent_pool.get(session_id).records.values())
    return record


async def test_child_outlives_the_root_turn_and_its_events_replay(tmp_path):
    gate = asyncio.Event()
    provider = Routed(
        [tool_response(SPAWN), text_response("root is done")],
        [[Wait(gate)], text_response("child report")],
    )
    # The child's first call parks; the router hands it a script that waits then answers.
    provider.child = ScriptedProvider([Wait(gate), *text_response("child report")])
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("bg")
    sub = session.observe()

    sent = [event async for event in session.send("go")]
    assert any(e.type == "turn.completed" for e in sent)
    ack = str(provider.root.requests[-1].messages)
    assert "Started subagent" in ack and "running in the background" in ack

    record = _agent_task(runtime, "bg")
    assert record.state == "running" and is_live_agent("bg", record.agent_id)
    assert "agent.completed" not in [e.type for e in session.events]

    gate.set()
    await asyncio.wait_for(record.task, 10)
    assert record.state == "idle" and not is_live_agent("bg", record.agent_id)

    events = session.events
    types = [e.type for e in events]
    assert types.index("turn.completed") < types.index("agent.completed")
    assert [e.seq for e in events] == sorted({e.seq for e in events})
    completed = next(e for e in events if e.type == "agent.completed")
    assert completed.data["status"] == "completed" and completed.data["run"] == 1

    view = fold(events)
    agent = view.agents[record.agent_id]
    assert agent.status == "completed" and agent.background is True and agent.runs == 1
    assert "child report" in "".join(m.text for t in agent.body.turns for m in t.messages)
    # Replay of the persisted log equals the live reduction of what subscribers saw.
    seen = []
    while sub._buffer:
        seen.append(sub._buffer.popleft())
    assert fold(sorted(seen, key=lambda e: e.seq)).agents[record.agent_id].to_dict() == agent.to_dict()
    await runtime.aclose()


async def test_child_event_while_root_is_idle_persists_and_reaches_a_subscriber(tmp_path):
    gate = asyncio.Event()
    provider = Routed([tool_response(SPAWN), text_response("ok")], [])
    provider.child = ScriptedProvider([Wait(gate), *text_response("late text")])
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("idle")
    [event async for event in session.send("go")]
    assert session.active_turn_id is None
    sub = session.observe()
    gate.set()
    await asyncio.wait_for(_agent_task(runtime, "idle").task, 10)
    delivered = []
    while sub._buffer:
        delivered.append(sub._buffer.popleft())
    assert "agent.completed" in [e.type for e in delivered]
    assert {e.seq for e in delivered} <= {e.seq for e in session.events}
    await runtime.aclose()


async def test_cancelling_the_root_turn_leaves_the_child_running(tmp_path):
    gate = asyncio.Event()
    root_gate = asyncio.Event()
    provider = Routed([tool_response(SPAWN), [Wait(root_gate)]], [])
    provider.child = ScriptedProvider([Wait(gate), *text_response("still here")])
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("esc")

    async def drain():
        return [event async for event in session.send("go")]

    turn = asyncio.ensure_future(drain())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if runtime.agent_pool.get("esc") and runtime.agent_pool.get("esc").records:
            break
    await asyncio.sleep(0.05)
    turn.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await turn
    record = _agent_task(runtime, "esc")
    assert record.state == "running" and not record.task.done()
    gate.set()
    await asyncio.wait_for(record.task, 10)
    assert record.last_status == "completed"
    await runtime.aclose()


async def test_wait_true_and_default_config_keep_the_blocking_result(tmp_path):
    for config, args in (
        (_config(background=False), {}),
        (_config(background=True), {"wait": True}),
    ):
        provider = Routed(
            [tool_response(("s", "subagent", {"prompt": "CHILD-TASK x", "subagent_type": "general", **args})),
             text_response("root done")],
            [text_response("blocking report")],
        )
        runtime = Runtime(tmp_path / str(len(args)), config=config, providers={"scripted": provider})
        [event async for event in runtime.session("blk").send("go")]
        assert "blocking report" in str(provider.root.requests[-1].messages)
        assert "Started subagent" not in str(provider.root.requests[-1].messages)
        types = [e.type for e in runtime.session("blk").events]
        assert types.index("agent.completed") < types.index("turn.completed")
        records = runtime.agent_pool.get("blk").records
        if args:
            # ``wait`` only blocks the call; the child is still persistent (idle, continuable).
            assert [r.state for r in records.values()] == ["idle"]
            assert next(iter(records.values())).continuable
        else:
            assert not records  # persistence is off with background = false
        await runtime.aclose()


async def test_runtime_aclose_drains_a_running_child(tmp_path):
    provider = Routed([tool_response(SPAWN), text_response("ok")], [])
    provider.child = ScriptedProvider([Wait(asyncio.Event())])  # never released
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("close")
    [event async for event in session.send("go")]
    record = _agent_task(runtime, "close")
    assert not record.task.done()
    await runtime.aclose()
    assert record.task.done() and not is_live_agent("close", record.agent_id)
    assert runtime.agent_pool.get("close") is None


async def test_pool_budget_is_shared_across_turns_of_a_session(tmp_path):
    runtime = Runtime(
        tmp_path, config=_config(), providers={"scripted": ScriptedProvider(text_response("x"))}
    )
    config = _config()
    first = runtime._turn_subagent_budget("sess", "turn-1", config)
    again = runtime._turn_subagent_budget("sess", "turn-1", config)
    later = runtime._turn_subagent_budget("sess", "turn-2", config)
    assert first is again is later and first.max_live == 8
    assert runtime._turn_subagent_budget("other", "turn-1", config) is not first
    await runtime.aclose()
