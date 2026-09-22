"""Phase 6 P6C: the subagent runner.

Covers the plan sections 5.6 and 15.6-15.8 contract:

* named and ad-hoc ``Task`` requests, with ``subagent_type`` defaulting to
  ``general`` and ``tools`` composing as a per-call narrowing;
* authority computed *before* the child exists -- ``parent_tools & role_tools &
  requested_tools`` -- with drops surfaced and permissions/grants inherited
  verbatim (never broadened);
* tier resolution and clamping to ``max_tier`` with an ``agent.clamped`` event;
* the shared, concurrency-safe tree budget (depth, concurrent, fan-out, and
  aggregate token/cost) and the ``<parent>/sub/<n>`` child session id;
* parent cancellation that propagates and closes the child;
* an isolated event relay carrying agent metadata so the tree replays;
* the ``<subagent_type>:<tier>`` permission key.

The child runtime is always a fake: the runner is tested through its injected
``RuntimeFactory``/``SessionFacade`` seams, never a real nested ``Runtime``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.agents import (
    AgentManager,
    ChildSpec,
    SubagentError,
    SubagentOutcome,
    SubagentRunner,
    SubagentUsage,
    TaskRequest,
)
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class Recorder:
    """A ``(event_type, data)`` sink that keeps every relayed event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, dict(data or {})))

    def types(self) -> list[str]:
        return [event_type for event_type, _data in self.events]

    def of(self, event_type: str) -> list[dict]:
        return [data for name, data in self.events if name == event_type]


class FakeSessions:
    """A session facade that records the logical ids and the closes."""

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.closed: list[str] = []

    def child_id(self, parent_id: str, index: int) -> str:
        session_id = f"{parent_id}/sub/{index}"
        self.ids.append(session_id)
        return session_id

    async def aclose(self, session_id: str) -> None:
        self.closed.append(session_id)


class Child:
    def __init__(self, spec: ChildSpec, behavior) -> None:
        self.spec = spec
        self._behavior = behavior
        self.closed = False

    async def run(self) -> SubagentOutcome:
        return await self._behavior(self.spec)

    async def aclose(self) -> None:
        self.closed = True


class Factory:
    """Builds fakes and records every spec handed to the child runtime."""

    def __init__(self, behavior=None, *, fail_on_build: bool = False) -> None:
        self._behavior = behavior or default_behavior
        self._fail_on_build = fail_on_build
        self.specs: list[ChildSpec] = []
        self.children: list[Child] = []

    def __call__(self, spec: ChildSpec) -> Child:
        if self._fail_on_build:
            raise RuntimeError("no runtime for you")
        self.specs.append(spec)
        child = Child(spec, self._behavior)
        self.children.append(child)
        return child


async def default_behavior(spec: ChildSpec) -> SubagentOutcome:
    await spec.emit("text", {"text": f"report from {spec.agent}"})
    return SubagentOutcome(
        agent=spec.agent,
        session_id=spec.session_id,
        status="completed",
        text=f"done: {spec.prompt}",
        usage=SubagentUsage(input_tokens=3, output_tokens=2),
    )


def make_manager(tmp_path: Path) -> AgentManager:
    return AgentManager.for_workspace(tmp_path / "ws")


def make_runner(
    tmp_path: Path,
    factory: Factory,
    *,
    agents: AgentManager | None = None,
    **kwargs,
) -> SubagentRunner:
    defaults: dict = {
        "agents": agents if agents is not None else make_manager(tmp_path),
        "runtime_factory": factory,
        "workspace": tmp_path,
        "parent_session": "root",
        "parent_tools": {"Read", "Grep"},
    }
    defaults.update(kwargs)
    return SubagentRunner(**defaults)


def agent_meta(data: dict) -> dict:
    meta = data.get("agent")
    assert isinstance(meta, dict), data
    return meta


# ---------------------------------------------------------------------------
# Request normalization
# ---------------------------------------------------------------------------


def test_task_request_defaults_and_mapping_coercion():
    req = TaskRequest.from_value({"prompt": "go", "tools": ["Read"]})
    assert req.subagent_type == "general"
    assert req.tools == ("Read",)
    assert req.model is None
    assert req.to_dict()["tools"] == ["Read"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": "x", "tools": "Read"},
        {"prompt": "x", "tools": [""]},
        {"prompt": "x", "model": "two words"},
        {"prompt": "x", "description": ""},
    ],
)
def test_task_request_rejects_invalid(payload):
    with pytest.raises(SubagentError):
        TaskRequest.from_value(payload)


# ---------------------------------------------------------------------------
# Authority: tools, permissions, drops
# ---------------------------------------------------------------------------


async def test_named_spawn_intersects_and_reports_drops(tmp_path):
    factory = Factory()
    permissions = object()
    runner = make_runner(
        tmp_path, factory, permissions=permissions, grants=("grant-1",)
    )
    outcome = await runner.spawn(
        TaskRequest(prompt="find X", tools=("Read", "Write"))
    )
    assert outcome.ok
    spec = factory.specs[-1]
    assert spec.tools == ("Read",)  # Write is not a parent tool
    assert "Write" in spec.dropped_tools
    assert "dropped" in outcome.render()
    # Permissions/grants are the parent's own objects, passed through verbatim.
    assert spec.permissions is permissions
    assert spec.grants == ("grant-1",)
    assert spec.session_id == "root/sub/1"
    assert spec.parent_session == "root"
    assert spec.depth == 1


async def test_ad_hoc_defaults_to_general_and_inherits_the_ceiling(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory)
    outcome = await runner.spawn(TaskRequest(prompt="do it"))
    assert outcome.ok
    spec = factory.specs[-1]
    assert spec.agent == "general"
    assert set(spec.tools) == {"Read", "Grep"}
    assert spec.dropped_tools == ()


async def test_parent_tools_are_a_hard_ceiling(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory, parent_tools={"Read"})
    outcome = await runner.spawn(
        TaskRequest(prompt="write", tools=("Write", "Edit"))
    )
    spec = factory.specs[-1]
    assert spec.tools == ()
    assert set(spec.dropped_tools) == {"Write", "Edit"}
    assert not outcome.is_error  # a narrowed child is not a failure


async def test_read_only_parent_cannot_write(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory, parent_tools={"Read", "Grep"})
    outcome = await runner.spawn(
        TaskRequest(prompt="mutate", tools=("Read", "Write", "Bash"))
    )
    spec = factory.specs[-1]
    assert spec.tools == ("Read",)
    assert not (set(spec.tools) & {"Write", "Edit", "MultiEdit", "Bash"})
    assert "Write" in spec.dropped_tools and "Bash" in spec.dropped_tools
    assert not outcome.is_error


async def test_read_only_role_strips_write_even_when_parent_has_it(tmp_path):
    factory = Factory()
    runner = make_runner(
        tmp_path,
        factory,
        parent_tools={"Read", "Write", "Edit", "Bash", "Grep"},
    )
    outcome = await runner.spawn(
        TaskRequest(prompt="explore", subagent_type="explore")
    )
    spec = factory.specs[-1]
    assert set(spec.tools) <= {"Read", "Glob", "Grep", "LS"}
    assert not (set(spec.tools) & {"Write", "Edit", "MultiEdit", "Bash"})
    assert not outcome.is_error


async def test_tampered_read_only_declaration_still_cannot_write(tmp_path):
    ws = tmp_path / "ws"
    agents_dir = ws / ".nexus" / "agents"
    agents_dir.mkdir(parents=True)
    # A workspace copy of planner that *asks* for the write path.
    (agents_dir / "planner.md").write_text(
        "---\nname: planner\ndescription: tampered\n"
        "bundles: [shell, fs]\ntools: [Bash, Write, Read]\n---\nbody\n",
        encoding="utf-8",
    )
    manager = AgentManager.for_workspace(ws)
    factory = Factory()
    runner = make_runner(
        tmp_path,
        factory,
        agents=manager,
        parent_tools={"Read", "Write", "Bash", "Grep"},
    )
    outcome = await runner.spawn(
        TaskRequest(prompt="plan", subagent_type="planner")
    )
    spec = factory.specs[-1]
    assert not (set(spec.tools) & {"Bash", "Write", "Edit", "MultiEdit"})
    assert not outcome.is_error


# ---------------------------------------------------------------------------
# Tier clamping
# ---------------------------------------------------------------------------


def test_permission_key_uses_type_and_effective_tier(tmp_path):
    runner = make_runner(tmp_path, Factory(), max_tier="medium")
    assert runner.permission_key({"prompt": "x"}) == "general:medium"
    assert (
        runner.permission_key({"prompt": "x", "model": "low"})
        == "general:low"
    )
    # The role's declared high tier is clamped in the key, so a
    # ``deny = ["Task(*:high)"]`` rule can never be dodged by omitting ``model``.
    assert (
        runner.permission_key({"prompt": "x", "subagent_type": "planner"})
        == "planner:medium"
    )


async def test_tier_is_clamped_and_reported(tmp_path):
    recorder = Recorder()
    factory = Factory()
    runner = make_runner(
        tmp_path, factory, max_tier="medium", event_sink=recorder
    )
    outcome = await runner.spawn(
        TaskRequest(prompt="plan", subagent_type="planner")
    )
    assert outcome.clamped is True
    assert outcome.requested_tier == "high"
    assert outcome.tier == "medium"
    spec = factory.specs[-1]
    assert spec.clamped is True
    assert spec.model == "medium"  # a clamped concrete/tier becomes the cap
    assert "agent.clamped" in recorder.types()
    spawned = recorder.of("agent.spawned")[-1]
    assert spawned["clamped"] is True


async def test_cap_is_configurable_and_never_widens(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory, max_tier="high")
    outcome = await runner.spawn(
        TaskRequest(prompt="plan", subagent_type="planner")
    )
    assert outcome.tier == "high"
    assert outcome.clamped is False


# ---------------------------------------------------------------------------
# Depth / fan-out / budget
# ---------------------------------------------------------------------------


async def test_depth_limit_refuses_recursion(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory, max_depth=1)
    first = await runner.spawn(TaskRequest(prompt="one"))
    assert first.ok
    spec = factory.specs[-1]
    assert spec.depth == 1

    child_runner = runner.for_child(spec)
    assert child_runner.budget is runner.budget  # one shared tree
    assert child_runner.depth == 1
    refused = await child_runner.spawn(TaskRequest(prompt="two"))
    assert refused.is_error
    assert refused.status == "refused"
    assert "max_depth" in refused.text
    # The refused grandchild never reached the factory.
    assert len(factory.specs) == 1


async def test_recursion_within_the_depth_cap_shares_the_tree(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory, max_depth=3, parent_tools={"Read"})
    first = await runner.spawn(TaskRequest(prompt="one"))
    assert first.ok
    child = runner.for_child(factory.specs[-1])
    second = await child.spawn(TaskRequest(prompt="two"))
    assert second.ok
    grandchild = child.for_child(factory.specs[-1])
    assert grandchild.depth == 2
    assert grandchild.budget is runner.budget
    third = await grandchild.spawn(TaskRequest(prompt="three"))
    assert third.ok
    assert [spec.depth for spec in factory.specs] == [1, 2, 3]
    assert [spec.session_id for spec in factory.specs] == [
        "root/sub/1",
        "root/sub/1/sub/1",
        "root/sub/1/sub/1/sub/1",
    ]
    # The whole tree shares one fan-out counter.
    assert runner.budget.children == 3

    great = grandchild.for_child(factory.specs[-1])
    refused = await great.spawn(TaskRequest(prompt="four"))
    assert refused.is_error
    assert "max_depth" in refused.text


async def test_fanout_and_concurrency_are_shared_and_bounded(tmp_path):
    state = {"active": 0, "peak": 0}

    async def behavior(spec: ChildSpec) -> SubagentOutcome:
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, text="ok"
        )

    runner = make_runner(
        tmp_path,
        Factory(behavior),
        max_concurrent=2,
        max_fanout=3,
    )
    outcomes = await asyncio.gather(
        *(runner.spawn(TaskRequest(prompt=f"t{i}")) for i in range(3))
    )
    assert all(outcome.ok for outcome in outcomes)
    assert state["peak"] <= 2
    assert runner.budget.children == 3

    refused = await runner.spawn(TaskRequest(prompt="fourth"))
    assert refused.is_error
    assert "budget" in refused.text or "exhausted" in refused.text


async def test_aggregate_token_budget_stops_new_children(tmp_path):
    async def behavior(spec: ChildSpec) -> SubagentOutcome:
        return SubagentOutcome(
            agent=spec.agent,
            session_id=spec.session_id,
            text="ok",
            usage=SubagentUsage(input_tokens=3),
        )

    runner = make_runner(
        tmp_path,
        Factory(behavior),
        token_budget=10,
        reserved_tokens=6,
    )
    assert (await runner.spawn(TaskRequest(prompt="a"))).ok
    assert (await runner.spawn(TaskRequest(prompt="b"))).ok
    refused = await runner.spawn(TaskRequest(prompt="c"))
    assert refused.is_error
    assert "budget" in refused.text
    assert runner.budget.spent_tokens == 6


async def test_aggregate_cost_budget_stops_new_children(tmp_path):
    async def behavior(spec: ChildSpec) -> SubagentOutcome:
        return SubagentOutcome(
            agent=spec.agent,
            session_id=spec.session_id,
            text="ok",
            usage=SubagentUsage(output_tokens=1, cost_usd=0.4),
        )

    runner = make_runner(
        tmp_path,
        Factory(behavior),
        cost_budget=1.0,
        reserved_cost=0.5,
    )
    assert (await runner.spawn(TaskRequest(prompt="a"))).ok
    assert (await runner.spawn(TaskRequest(prompt="b"))).ok
    refused = await runner.spawn(TaskRequest(prompt="c"))
    assert refused.is_error
    assert runner.budget.spent_cost == pytest.approx(0.8)


async def test_budget_reservation_is_released_on_failure(tmp_path):
    factory = Factory(fail_on_build=True)
    runner = make_runner(tmp_path, factory, token_budget=10, reserved_tokens=6)
    outcome = await runner.spawn(TaskRequest(prompt="boom"))
    assert outcome.is_error
    assert outcome.status == "failed"
    # The failed build spent nothing and reserved nothing.
    assert runner.budget.spent_tokens == 0
    assert runner.budget.remaining_tokens == 10


# ---------------------------------------------------------------------------
# Unknown roles and factory failures
# ---------------------------------------------------------------------------


async def test_unknown_role_is_refused_with_the_defined_set(tmp_path):
    runner = make_runner(tmp_path, Factory())
    outcome = await runner.spawn(
        TaskRequest(prompt="x", subagent_type="does-not-exist")
    )
    assert outcome.is_error
    assert outcome.status == "refused"
    assert "unknown subagent_type" in outcome.text
    assert "general" in outcome.text


async def test_child_runtime_exception_becomes_an_error_outcome(tmp_path):
    async def behavior(spec: ChildSpec) -> SubagentOutcome:
        raise RuntimeError("child exploded")

    runner = make_runner(tmp_path, Factory(behavior))
    outcome = await runner.spawn(TaskRequest(prompt="x"))
    assert outcome.is_error
    assert outcome.status == "failed"
    assert "RuntimeError" in outcome.text


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_parent_cancel_propagates_and_closes_the_child(tmp_path):
    started = asyncio.Event()

    async def behavior(spec: ChildSpec) -> SubagentOutcome:
        started.set()
        await asyncio.sleep(30)
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, text="late"
        )

    factory = Factory(behavior)
    sessions = FakeSessions()
    runner = make_runner(tmp_path, factory, sessions=sessions)
    token = CancelToken()

    async def cancel_soon() -> None:
        await started.wait()
        token.cancel("user stop")

    task = asyncio.ensure_future(cancel_soon())
    with pytest.raises(OperationCancelled) as excinfo:
        await runner.spawn(TaskRequest(prompt="slow"), cancel=token)
    assert "user stop" in str(excinfo.value)
    await task

    assert factory.children and factory.children[-1].closed is True
    assert sessions.closed == ["root/sub/1"]
    assert runner.budget.active == 0  # the concurrency slot was released


async def test_already_cancelled_token_raises_before_building(tmp_path):
    factory = Factory()
    runner = make_runner(tmp_path, factory)
    token = CancelToken()
    token.cancel("stop")
    with pytest.raises(OperationCancelled):
        await runner.spawn(TaskRequest(prompt="x"), cancel=token)
    assert factory.specs == []


# ---------------------------------------------------------------------------
# Event relay and tree replay
# ---------------------------------------------------------------------------


async def test_event_relay_carries_agent_metadata_and_replays(tmp_path):
    async def behavior(spec: ChildSpec) -> SubagentOutcome:
        await spec.emit("text", {"text": f"hello from {spec.agent}"})
        await spec.emit("tool.started", {"tool": "Read"})
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, text="done"
        )

    recorder = Recorder()
    runner = make_runner(tmp_path, Factory(behavior), event_sink=recorder)
    outcomes = await asyncio.gather(
        runner.spawn(TaskRequest(prompt="a")),
        runner.spawn(TaskRequest(prompt="b")),
    )
    assert all(outcome.ok for outcome in outcomes)

    spawned = recorder.of("agent.spawned")
    completed = recorder.of("agent.completed")
    assert len(spawned) == 2 and len(completed) == 2

    # The tree: every spawned id has the root as parent and a distinct session.
    parent_of = {agent_meta(d)["id"]: agent_meta(d)["parent"] for d in spawned}
    assert set(parent_of.values()) == {"root"}
    assert len(set(parent_of)) == 2

    # Every child event is isolated: it carries the meta of the child that
    # emitted it, and the child session id, never the parent's identity.
    child_ids = set(parent_of)
    text_events = recorder.of("text")
    assert len(text_events) == 2
    for data in text_events:
        assert agent_meta(data)["id"] in child_ids
        assert data["agent"]["session"] == data["agent"]["id"]
    # Reconstruct the tree from the parent links alone.
    children = {
        agent_id for agent_id, parent in parent_of.items() if parent == "root"
    }
    assert children == child_ids


async def test_child_session_ids_are_sequential_per_parent(tmp_path):
    runner = make_runner(tmp_path, Factory())
    sessions = [await runner.spawn(TaskRequest(prompt=str(i))) for i in range(3)]
    assert [o.session_id for o in sessions] == [
        "root/sub/1",
        "root/sub/2",
        "root/sub/3",
    ]


async def test_emit_override_isolates_this_spawn(tmp_path):
    base = Recorder()
    override = Recorder()
    runner = make_runner(tmp_path, Factory(), event_sink=base)
    await runner.spawn(TaskRequest(prompt="x"), emit=override)
    assert base.events == []
    assert "agent.spawned" in override.types()


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_unknown_max_tier_is_a_config_error(tmp_path):
    from nexus.errors import ConfigError

    with pytest.raises(ConfigError):
        make_runner(tmp_path, Factory(), max_tier="impossible")


def test_negative_depth_is_rejected(tmp_path):
    with pytest.raises(SubagentError):
        make_runner(tmp_path, Factory(), parent_depth=-1)


def test_budget_validates_its_bounds():
    from nexus.agents import SubagentBudget

    with pytest.raises(SubagentError):
        SubagentBudget(max_concurrent=0)
    with pytest.raises(SubagentError):
        SubagentBudget(max_depth=-1)
    with pytest.raises(SubagentError):
        SubagentBudget(token_budget=-1)
    budget = SubagentBudget(max_concurrent=2, token_budget=100)
    assert budget.remaining_tokens == 100
    assert budget.max_concurrent == 2
