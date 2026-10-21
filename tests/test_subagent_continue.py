"""Phase 4: continuation (``subagent_send``) and persistent children.

Runner-level cases use a fake child factory and a real ``RootAgents``; runtime-level
cases run a real ``Runtime`` with a scripted provider (no network). The blocking path
(``tests/test_subagent_runner.py``) stays untouched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.agents import SubagentBudget, SubagentOutcome, SubagentUsage
from nexus.agents.pool import RootAgents
from nexus.agents.runner import ContinuationRefused
from nexus.events import Event
from nexus.model.message import Message, ToolUse
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response, tool_response
from nexus.runtime import Runtime
from nexus.view import fold
from test_subagent_background import SPAWN, Routed, _config
from test_subagent_runner import Factory, make_runner

# ---------------------------------------------------------------------------
# Runtime level
# ---------------------------------------------------------------------------


def _send(agent_id, message, **extra):
    return ("send", "subagent_send", {"agent_id": agent_id, "message": message, **extra})


def _record(runtime, session_id):
    [record] = list(runtime.agent_pool.get(session_id).records.values())
    return record


def _texts(request) -> str:
    return "\n".join(
        getattr(block, "text", None) or str(getattr(block, "content", ""))
        for message in request.messages
        for block in message.content
    )


async def _first_run(tmp_path, root, child, session_id="c1", **agents):
    (tmp_path / "notes.txt").write_text("the notes", encoding="utf-8")
    provider = Routed(root, child)
    runtime = Runtime(tmp_path, config=_config(**agents), providers={"scripted": provider})
    session = runtime.session(session_id)
    [event async for event in session.send("go")]
    record = _record(runtime, session_id)
    await asyncio.wait_for(record.task, 10)
    return runtime, session, provider, record


READ = ("t1", "read", {"path": "notes.txt"})


async def test_send_runs_the_same_child_again_on_its_whole_history(tmp_path):
    runtime, session, provider, record = await _first_run(
        tmp_path,
        [tool_response(SPAWN), text_response("spawned"),
         tool_response(_send("c1/sub/1", "now check the second thing")), text_response("sent")],
        [tool_response(READ), text_response("first report"), text_response("second report")],
    )
    assert record.state == "idle" and record.continuable and record.runs == 1
    child_id = record.agent_id

    [event async for event in session.send("please continue it")]
    ack = _texts(provider.root.requests[-1])
    assert "Sent your message" in ack and "run 2" in ack
    await asyncio.wait_for(record.task, 10)

    assert record.agent_id == child_id and record.runs == 2 and record.state == "idle"
    seen = _texts(provider.child.requests[-1])
    assert "CHILD-TASK do the work" in seen            # run 1's prompt
    assert "the notes" in seen                          # run 1's tool result
    assert "first report" in seen                       # run 1's answer
    assert seen.index("first report") < seen.index("now check the second thing")

    resumed = [e for e in session.events if e.type == "agent.resumed"]
    assert len(resumed) == 1
    assert resumed[0].data["run"] == 2 and resumed[0].data["from"] == "root"
    assert resumed[0].data["message"] == "now check the second thing"
    completed = [e.data["run"] for e in session.events if e.type == "agent.completed"]
    assert completed == [1, 2]

    agent = fold(session.events).agents[child_id]
    assert agent.runs == 2 and agent.status == "completed" and agent.continuable
    followups = [m for t in agent.body.turns for m in t.messages
                 if (m.origin or {}).get("kind") == "root_followup"]
    assert len(followups) == 1 and "second thing" in followups[0].blocks[0].text

    # The child's own durable log carries both user inputs under the same session.
    child_session = runtime._ensure_child_sessions().manager.open(
        runtime._ensure_child_sessions().manager.store.resolve_child_id(child_id),
        create=False, recover=False,
    )
    users = ["".join(getattr(b, "text", "") for b in m.content)
             for m in child_session.messages if m.role == "user" and m.meta.notice is None]
    assert any("CHILD-TASK" in u for u in users) and any("second thing" in u for u in users)
    # Every run assembles its request from the whole log through the normal context
    # path (budget and compaction included), so the message count only grows.
    sizes = [e.data["messages"] for e in child_session.events if e.type == "context.assembled"]
    assert len(sizes) >= 3 and sizes == sorted(sizes) and sizes[-1] > sizes[1]
    await runtime.aclose()


async def test_report_of_a_continued_run_carries_the_run_number(tmp_path):
    runtime, session, provider, record = await _first_run(
        tmp_path,
        [tool_response(SPAWN), text_response("spawned"),
         tool_response(_send("c1/sub/1", "more please")), text_response("sent")],
        [text_response("first report"), text_response("second report")],
        report_coalesce_ms=0,
    )
    [event async for event in session.send("continue")]
    await asyncio.wait_for(record.task, 10)
    flush = runtime.agent_pool.get("c1")._flush_task
    if flush is not None:
        await asyncio.wait_for(flush, 10)
    queued = [e.data["origin"] for e in session.events
              if e.type == "input.queued" and (e.data.get("origin") or {}).get("kind") == "subagent_report"]
    assert [(o["agent"], o["run"]) for o in queued] == [("c1/sub/1", 1), ("c1/sub/1", 2)]
    await runtime.aclose()


async def test_sending_to_a_running_child_is_refused(tmp_path):
    gate = asyncio.Event()
    provider = Routed(
        [tool_response(SPAWN), text_response("spawned"),
         tool_response(_send("c2/sub/1", "too early")), text_response("ok")],
        [],
    )
    provider.child = ScriptedProvider([Wait(gate), *text_response("late")])
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("c2")
    [event async for event in session.send("go")]
    record = _record(runtime, "c2")
    [event async for event in session.send("now send")]
    assert "is still running" in _texts(provider.root.requests[-1])
    assert [e for e in session.events if e.type == "agent.resumed"] == []
    gate.set()
    await asyncio.wait_for(record.task, 10)
    await runtime.aclose()


async def test_unknown_and_oversize_sends_are_refused_with_a_reason(tmp_path):
    runtime, session, provider, record = await _first_run(
        tmp_path,
        [tool_response(SPAWN), text_response("spawned"),
         tool_response(_send("c1/sub/99", "hi")), text_response("a"),
         tool_response(_send("c1/sub/1", "x" * 50)), text_response("b")],
        [text_response("first report")],
        max_followup_chars=20,
    )
    [event async for event in session.send("unknown")]
    assert "no subagent 'c1/sub/99'" in _texts(provider.root.requests[-1])
    [event async for event in session.send("oversize")]
    assert "limit is 20" in _texts(provider.root.requests[-1])
    assert record.runs == 1 and record.state == "idle"
    await runtime.aclose()


async def test_crashed_mid_run_child_is_repaired_then_continued(tmp_path):
    runtime, session, provider, record = await _first_run(
        tmp_path,
        [tool_response(SPAWN), text_response("spawned"),
         tool_response(_send("c1/sub/1", "carry on after the crash")), text_response("sent")],
        [text_response("first report"), text_response("recovered report")],
    )
    facade = runtime._ensure_child_sessions()
    real = facade.manager.store.resolve_child_id(record.agent_id)
    child = facade.manager.open(real, create=False, recover=False)
    # A crash left a tool call with no result and an open turn in the child's log.
    child.append_event(Event(type="turn.started", data={}, session=real, turn="crashed-turn"))
    child.append_message(Message(role="assistant", content=[ToolUse(id="dangling", name="read", input={})]))
    facade.manager.evict(real)

    [event async for event in session.send("continue")]
    await asyncio.wait_for(record.task, 10)
    assert record.runs == 2 and record.last_status == "completed"
    seen = _texts(provider.child.requests[-1])
    assert "dangling" in seen and "interrupted" in seen.lower()      # the repaired tool result
    assert seen.index("dangling") < seen.index("carry on after the crash")
    await runtime.aclose()


async def test_restart_rebuilds_idle_children_and_continues_them(tmp_path):
    runtime, session, provider, record = await _first_run(
        tmp_path,
        [tool_response(SPAWN), text_response("spawned")],
        [text_response("first report")],
    )
    child_id = record.agent_id
    await runtime.aclose()

    again = Routed(
        [tool_response(_send(child_id, "welcome back")), text_response("sent")],
        [text_response("second report")],
    )
    restarted = Runtime(tmp_path, config=_config(), providers={"scripted": again})
    reopened = restarted.session("c1")
    assert restarted.agent_pool.get("c1") is None          # nothing known yet
    [event async for event in reopened.send("resume it")]
    pool = restarted.agent_pool.get("c1")
    rebuilt = pool.get(child_id)
    assert rebuilt is not None and rebuilt.runs == 2
    await asyncio.wait_for(rebuilt.task, 10)
    assert "welcome back" in _texts(again.child.requests[-1])
    assert "first report" in _texts(again.child.requests[-1])    # the old conversation survived
    assert pool.budget.live == 1                               # an idle child holds its slot
    await restarted.aclose()


# ---------------------------------------------------------------------------
# Runner level (fake child factory, real RootAgents)
# ---------------------------------------------------------------------------


class Log:
    """One ordered root log fed by the launch emitter and the root event port."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event_type: str, data: dict | None = None) -> None:
        self.events.append(Event(type=event_type, data=dict(data or {}), session="root", seq=len(self.events) + 1, ts=1.0))

    async def port(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, event_type: str) -> list[dict]:
        return [e.data for e in self.events if e.type == event_type]


def _gated(gate: asyncio.Event | None = None, *, tokens: int = 5):
    calls: list[tuple[int, str]] = []

    async def behavior(spec):
        calls.append((spec.resume, spec.prompt))
        if gate is not None:
            await gate.wait()
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, status="completed",
            text=f"run {spec.resume or 1} done", usage=SubagentUsage(input_tokens=tokens),
        )

    factory = Factory(behavior)
    factory.calls = calls
    return factory


def _persistent(tmp_path, factory, *, root=None, log=None, budget=None, **kw):
    log = log or Log()
    root = root or RootAgents("root", event_port=log.port)
    runner = make_runner(tmp_path, factory, pool=root, persistent=True, budget=budget, **kw)
    return runner, root, log


async def _started(runner, log, **request):
    started = await runner.start({"prompt": "go", **request}, emit=log.emit)
    assert started.started
    return started


async def test_idle_child_keeps_its_slot_and_resume_reuses_it(tmp_path):
    budget = SubagentBudget(max_concurrent=4, max_depth=1, max_live=1)
    factory = _gated()
    runner, root, log = _persistent(tmp_path, factory, budget=budget)
    started = await _started(runner, log)
    record = root.get(started.agent_id)
    await asyncio.wait_for(record.task, 5)
    assert record.state == "idle" and record.continuable and budget.live == 1
    assert log.of("agent.spawned")[0]["continuable"] is True
    assert log.of("agent.completed")[0]["continuable"] is True
    # The cap is full while the child idles, but continuing it needs no second slot
    # (a *new* spawn would evict it instead: see the eviction tests below).
    resumed = await runner.resume(started.agent_id, "again", emit=log.emit)
    assert resumed.run == 2
    await asyncio.wait_for(record.task, 5)
    assert budget.live == 1 and record.runs == 2
    assert factory.calls == [(0, "go"), (2, "again")]
    stopped = await runner.stop(started.agent_id, reason="done")
    assert stopped.stopped and budget.live == 0
    third = await runner.start({"prompt": "three"}, emit=log.emit)
    assert third.started
    await asyncio.wait_for(root.get(third.agent_id).task, 5)


async def test_resume_events_and_the_completed_run_number(tmp_path):
    runner, root, log = _persistent(tmp_path, _gated())
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    await runner.resume(started.agent_id, "z" * 5000, emit=log.emit)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    [resumed] = log.of("agent.resumed")
    assert resumed["run"] == 2 and resumed["from"] == "root" and resumed["message_chars"] == 5000
    assert resumed["agent"]["id"] == started.agent_id and resumed["background"] is True
    assert len(resumed["message"]) < 4000 and "follow-up clipped" in resumed["message"]
    assert [d["run"] for d in log.of("agent.completed")] == [1, 2]
    # The launch event used the call's emitter; the run's events used the root port.
    assert log.types().index("agent.resumed") < log.types().index("agent.completed", 3)
    view = fold(log.events)
    assert view.agents[started.agent_id].runs == 2


async def test_refusals_are_specific(tmp_path):
    gate = asyncio.Event()
    runner, root, log = _persistent(tmp_path, _gated(gate), max_followup_chars=10)
    started = await _started(runner, log)
    with pytest.raises(ContinuationRefused, match="still running"):
        await runner.resume(started.agent_id, "hi", emit=log.emit)
    gate.set()
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    with pytest.raises(ContinuationRefused, match="no subagent 'nope'"):
        await runner.resume("nope", "hi", emit=log.emit)
    with pytest.raises(ContinuationRefused, match="limit is 10"):
        await runner.resume(started.agent_id, "x" * 11, emit=log.emit)
    with pytest.raises(ContinuationRefused, match="non-empty"):
        await runner.resume(started.agent_id, "  ", emit=log.emit)
    await runner.stop(started.agent_id)
    with pytest.raises(ContinuationRefused, match="was stopped"):
        await runner.resume(started.agent_id, "hi", emit=log.emit)
    assert [d["run"] for d in log.of("agent.completed")] == [1]


async def test_external_worker_is_not_continuable(tmp_path):
    blocker = "Claude Code workers run one assignment per session; start a new subagent"
    runner, root, log = _persistent(
        tmp_path, _gated(), continuation_blocker=lambda model, provider: blocker
    )
    budget = runner.budget
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    assert log.of("agent.spawned")[0]["continuable"] is False
    assert log.of("agent.completed")[0]["continuable"] is False
    assert budget.live == 0                        # nothing to hold a slot for
    with pytest.raises(ContinuationRefused, match="one assignment per session"):
        await runner.resume(started.agent_id, "again", emit=log.emit)


async def test_external_outcome_is_detected_even_when_the_probe_said_yes(tmp_path):
    async def external(spec):
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, status="completed", text="x",
            metrics={"usage": "unknown", "external_kind": "claude-code", "external_label": "Claude Code"},
        )

    runner, root, log = _persistent(tmp_path, Factory(external))
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    assert log.of("agent.completed")[0]["continuable"] is False and runner.budget.live == 0
    with pytest.raises(ContinuationRefused, match="Claude Code workers run one assignment"):
        await runner.resume(started.agent_id, "again", emit=log.emit)


async def test_a_child_that_failed_to_build_is_not_continuable(tmp_path):
    runner, root, log = _persistent(tmp_path, Factory(fail_on_build=True))
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    assert log.of("agent.completed")[0]["continuable"] is False and runner.budget.live == 0
    with pytest.raises(ContinuationRefused, match="never started"):
        await runner.resume(started.agent_id, "again", emit=log.emit)


async def test_non_persistent_runner_refuses_and_children_do_not_keep_slots(tmp_path):
    root = RootAgents("root", event_port=Log().port)
    runner = make_runner(tmp_path, _gated(), pool=root)
    log = Log()
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    assert log.of("agent.spawned")[0]["continuable"] is False and runner.budget.live == 0
    with pytest.raises(ContinuationRefused, match="not persistent"):
        await runner.resume(started.agent_id, "again", emit=log.emit)


async def test_token_budget_spans_continuations(tmp_path):
    budget = SubagentBudget(max_concurrent=2, max_depth=1, token_budget=12)
    runner, root, log = _persistent(tmp_path, _gated(tokens=5), budget=budget)
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    await runner.resume(started.agent_id, "two", emit=log.emit)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    assert budget.spent_tokens == 10
    with pytest.raises(ContinuationRefused, match="budget is exhausted"):
        # 10 spent + a reservation that no longer fits keeps the third run out.
        runner._reserved_tokens = 5
        await runner.resume(started.agent_id, "three", emit=log.emit)
    assert root.get(started.agent_id).state == "idle" and root.get(started.agent_id).runs == 2


async def test_wait_true_runs_inline_and_returns_the_outcome(tmp_path):
    runner, root, log = _persistent(tmp_path, _gated())
    outcome = await runner.spawn({"prompt": "go"}, emit=log.emit)
    assert outcome.status == "completed" and outcome.continuable
    assert "subagent_send" in outcome.render() or outcome.handoff == ""
    agent_id = outcome.session_id
    assert root.get(agent_id).state == "idle" and runner.budget.live == 1
    again = await runner.resume(agent_id, "next", wait=True, emit=log.emit)
    assert again.outcome.status == "completed" and again.outcome.text == "run 2 done"
    assert [d["run"] for d in log.of("agent.completed")] == [1, 2]
    # Waited runs report through the call's result, never as a pushed report.
    assert root.report_pending == 0 and log.of("agent.resumed")[0]["background"] is False


async def test_stop_cancels_a_running_child_without_reporting_it(tmp_path):
    gate = asyncio.Event()
    reports = []
    root = RootAgents("root", event_port=Log().port)
    log = Log()
    root.configure(event_port=log.port)
    runner = make_runner(tmp_path, _gated(gate), pool=root, persistent=True)
    started = await _started(runner, log)
    await asyncio.sleep(0)
    result = await runner.stop(started.agent_id, reason="not needed")
    assert result.stopped and result.was_running and "Cancelled and stopped" in result.message
    record = root.get(started.agent_id)
    assert record.state == "stopped" and runner.budget.live == 0
    assert log.of("agent.completed")[0]["status"] == "cancelled"
    [stopped] = log.of("agent.stopped")
    assert stopped["by"] == "root" and stopped["reason"] == "not needed"
    assert root.report_pending == 0 and not reports
    again = await runner.stop(started.agent_id)
    assert again.stopped and again.already and len(log.of("agent.stopped")) == 1
    agent = fold(log.events).agents[started.agent_id]
    assert agent.stopped and agent.stopped_by == "root"


async def test_stop_of_an_unknown_child_says_so(tmp_path):
    runner, _root, _log = _persistent(tmp_path, _gated())
    result = await runner.stop("root/sub/9")
    assert not result.stopped and "No such subagent" in result.message


async def test_list_agents_and_permission_key(tmp_path):
    gate = asyncio.Event()
    runner, root, log = _persistent(tmp_path, _gated(gate))
    one = await _started(runner, log, description="first")
    gate.set()
    await asyncio.wait_for(root.get(one.agent_id).task, 5)
    two = await _started(runner, log)
    rows = runner.list_agents()
    assert [(r.agent_id, r.state) for r in rows] == [(two.agent_id, "running"), (one.agent_id, "idle")]
    assert rows[1].continuable and rows[1].tokens == 5 and rows[1].description == "first"
    key = runner.agent_permission_key(one.agent_id)
    assert key == f"task:{root.get(one.agent_id).tier}"
    assert runner.agent_permission_key("nope") == "unknown:unknown"
    await asyncio.wait_for(root.get(two.agent_id).task, 5)


# -- authority never widens -----------------------------------------------------


async def test_continuation_tools_never_exceed_the_spawn_ceiling_in_process(tmp_path):
    factory = _gated()
    runner, root, log = _persistent(tmp_path, factory, parent_tools={"Read", "Grep"})
    started = await _started(runner, log, tools=["read"])
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    first = factory.specs[0].tools
    assert first == ("read",)
    await runner.resume(started.agent_id, "again", emit=log.emit)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    assert factory.specs[1].tools == first
    assert factory.specs[1].permissions is factory.specs[0].permissions


async def test_rebuilt_children_are_held_to_current_ceiling_and_recorded_tools(tmp_path):
    factory = _gated()
    runner, root, log = _persistent(tmp_path, factory, parent_tools={"Read", "Grep"})
    started = await _started(runner, log)               # role default: read + grep
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    recorded = set(factory.specs[0].tools)
    assert recorded == {"read", "grep"}

    # Restarts: the parent now holds fewer tools, or a tool it never had at spawn.
    base = list(log.events)
    for parent, expected in (({"Read", "Write"}, {"read"}), ({"Read", "Grep", "Write"}, {"read", "grep"})):
        factory2 = _gated()
        root2 = RootAgents("root", event_port=log.port)
        root2.configure(history=lambda: list(base))
        runner2 = make_runner(tmp_path, factory2, pool=root2, persistent=True, parent_tools=parent)
        assert root2.get(started.agent_id) is None
        await runner2.resume(started.agent_id, "again", emit=log.emit)
        record = root2.get(started.agent_id)
        assert record is not None and record.recipe is not None and record.runs == 2
        await asyncio.wait_for(record.task, 5)
        assert set(factory2.specs[0].tools) == expected          # never wider than recorded
        assert factory2.specs[0].resume == 2 and factory2.specs[0].follow_up == "again"
        assert runner2.budget.live == 1                         # adopted its slot
        await root2.aclose()


async def test_rebuild_marks_open_runs_interrupted_and_stopped_children_stay_stopped(tmp_path):
    log = Log()
    runner, root, _ = _persistent(tmp_path, _gated(), log=log)
    one = await _started(runner, log)
    await asyncio.wait_for(root.get(one.agent_id).task, 5)
    two = await _started(runner, log)
    await asyncio.wait_for(root.get(two.agent_id).task, 5)
    await runner.stop(two.agent_id)
    # A third child whose run the log shows open and nothing in this process runs.
    log.emit("agent.spawned", {"agent": {"id": "root/sub/7", "type": "task", "session": "root/sub/7"},
                               "continuable": True, "tools": ["read"], "model": "m", "tier": "medium"})

    from nexus.agents.pool import records_from_events

    by_id = {r.agent_id: r for r in records_from_events(log.events)}
    assert by_id[one.agent_id].state == "idle" and by_id[one.agent_id].live_slot
    assert by_id[two.agent_id].state == "stopped" and not by_id[two.agent_id].live_slot
    assert by_id["root/sub/7"].state == "interrupted" and by_id["root/sub/7"].live_slot
    assert by_id[one.agent_id].tokens == 5 and by_id[one.agent_id].runs == 1


# -- worktree children -------------------------------------------------------------


def _worktree_runner(tmp_path, behavior):
    from nexus.agents import worktrees
    from test_subagent_runner import _git_repo

    parent = _git_repo(tmp_path / "parent")
    service = worktrees.WorktreeService()
    root = tmp_path / "daemon"
    runner, pool, log = _persistent(
        tmp_path, Factory(behavior), workspace=parent, worktree_service=service,
        worktree_root=root, runtime_supports_workspace=True,
    )
    return parent, service, root, runner, pool, log


async def test_continuing_a_worktree_child_edits_the_same_checkout_and_voids_old_reviews(tmp_path):
    from nexus.agents import worktrees

    lifecycles: list[str] = []

    async def behavior(spec):
        record = worktrees.get(spec.session_id, root=tmp_path / "daemon")
        lifecycles.append(record.lifecycle)
        (spec.workspace / f"run{spec.resume or 1}.txt").write_text(f"run {spec.resume or 1}\n", encoding="utf-8")
        return SubagentOutcome(agent=spec.agent, session_id=spec.session_id, status="completed", text="ok")

    parent, service, root, runner, pool, log = _worktree_runner(tmp_path, behavior)
    started = await _started(runner, log, worktree=True)
    record = pool.get(started.agent_id)
    await asyncio.wait_for(record.task, 5)
    path = started.workspace
    first = service.get(started.agent_id, root=root)
    assert first.lifecycle == "finalized" and "run1.txt" in first.final_dirty_status
    old = service.review(started.agent_id, root=root)
    service.acknowledge(started.agent_id, old.review_id, old.digest, root=root)

    resumed = await runner.resume(started.agent_id, "add the second file", emit=log.emit)
    assert resumed.workspace == path                                   # the same checkout
    await asyncio.wait_for(record.task, 5)
    assert lifecycles == ["active", "active"]                          # reopened before the run
    last = service.get(started.agent_id, root=root)
    assert last.lifecycle == "finalized" and "run2.txt" in last.final_dirty_status
    assert (Path(path) / "run1.txt").exists() and (Path(path) / "run2.txt").exists()
    [resumed_event] = log.of("agent.resumed")
    assert resumed_event["worktree"]["lifecycle"] == "active"
    assert log.of("agent.completed")[-1]["worktree"]["lifecycle"] == "finalized"

    # The review made before the reopen cannot integrate the new state.
    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        service.integrate(started.agent_id, old.review_id, old.digest, root=root)
    assert not (parent / "run2.txt").exists()


async def test_integrated_worktrees_refuse_continuation(tmp_path):
    async def behavior(spec):
        (spec.workspace / "a.txt").write_text("a\n", encoding="utf-8")
        return SubagentOutcome(agent=spec.agent, session_id=spec.session_id, status="completed", text="ok")

    parent, service, root, runner, pool, log = _worktree_runner(tmp_path, behavior)
    started = await _started(runner, log, worktree=True)
    record = pool.get(started.agent_id)
    await asyncio.wait_for(record.task, 5)
    page = service.review(started.agent_id, root=root)
    service.acknowledge(started.agent_id, page.review_id, page.digest, root=root)
    service.integrate(started.agent_id, page.review_id, page.digest, root=root)

    with pytest.raises(ContinuationRefused, match="cannot be reopened"):
        await runner.resume(started.agent_id, "more", emit=log.emit)
    assert record.state == "idle" and record.runs == 1 and runner.budget.live == 1
    assert log.of("agent.resumed") == []
    assert service.get(started.agent_id, root=root).lifecycle == "integrated"


async def test_control_tools_are_advertised_only_when_background_is_on(tmp_path):
    names = {}
    for background in (False, True):
        provider = Routed([text_response("hi")], [])
        runtime = Runtime(
            tmp_path / str(background), config=_config(background=background),
            providers={"scripted": provider},
        )
        [event async for event in runtime.session(f"adv{background}").send("hello")]
        names[background] = {tool.name for tool in provider.root.requests[0].tools}
        await runtime.aclose()
    control = {"subagent_send", "subagent_stop", "subagent_list"}
    assert "subagent" in names[False] and not (control & names[False])
    assert control <= names[True]


async def test_crash_during_a_continuation_closes_run_two_and_queues_its_report_without_waking(tmp_path):
    from test_subagent_reports import _facade, until

    from nexus.model.message import Message, Text

    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": ScriptedProvider(text_response("x"))})
    session = runtime.session("cr")
    meta = {"id": "cr/sub/1", "session": "cr/sub/1", "type": "general", "tier": "low"}
    session.append_message(Message(role="user", content=[Text(text="delegate")]))
    base = {"agent": meta, **meta}
    session.append_event(Event(type="agent.spawned", session="cr", data={**base, "background": True, "continuable": True}))
    session.append_event(Event(type="agent.completed", session="cr", data={**base, "run": 1, "status": "completed", "continuable": True}))
    session.append_event(Event(type="input.queued", session="cr", data={
        "queued_id": "q1", "content": "r1", "mode": "steer",
        "origin": {"kind": "subagent_report", "agent": "cr/sub/1", "run": 1, "status": "completed", "agents": ["cr/sub/1:1"]},
    }))
    session.append_event(Event(type="agent.resumed", session="cr", data={**base, "run": 2, "from": "root", "message": "more", "background": True}))
    await runtime.aclose()                      # the daemon dies with run 2 open in the log

    provider = Routed([text_response("never")], [])
    facade, runtime2 = _facade(tmp_path, provider)
    handle = facade._session("cr")
    completed = [e.data for e in handle.events if e.type == "agent.completed"]
    assert [(d["run"], d["status"]) for d in completed] == [(1, "completed"), (2, "interrupted")]
    await until(lambda: handle.queued_report_count >= 1)
    await asyncio.sleep(0.2)
    assert provider.root.requests == [] and not handle.active          # a restart never wakes the root
    origins = [e.data["origin"] for e in handle.events
               if e.type == "input.queued" and e.data["origin"]["kind"] == "subagent_report"]
    assert [(o["agent"], o["run"], o["status"]) for o in origins] == [
        ("cr/sub/1", 1, "completed"), ("cr/sub/1", 2, "interrupted"),
    ]
    # The child is rebuilt as interrupted and can still be continued.
    agent = fold(handle.events).agents["cr/sub/1"]
    assert agent.runs == 2 and agent.continuable
    await facade.shutdown()


# -- Phase 6: the idle-slot safety valve -----------------------------------------


async def test_spawn_at_max_live_evicts_the_least_recently_run_idle_child(tmp_path):
    budget = SubagentBudget(max_concurrent=4, max_depth=1, max_live=2)
    runner, root, log = _persistent(tmp_path, _gated(), budget=budget)
    first = await _started(runner, log)
    await asyncio.wait_for(root.get(first.agent_id).task, 5)
    second = await _started(runner, log)
    await asyncio.wait_for(root.get(second.agent_id).task, 5)
    root.get(first.agent_id).last_run_ts = 1.0      # the older run
    root.get(second.agent_id).last_run_ts = 2.0
    assert budget.live == 2

    third = await _started(runner, log)             # would exceed max_live: not refused
    await asyncio.wait_for(root.get(third.agent_id).task, 5)

    assert root.get(first.agent_id).state == "stopped"
    assert root.get(second.agent_id).state == "idle"
    assert budget.live == 2
    (stopped,) = log.of("agent.stopped")
    assert stopped["by"] == "system" and "evicted" in stopped["reason"]
    assert stopped["agent_id"] == first.agent_id
    with pytest.raises(ContinuationRefused):
        await runner.resume(first.agent_id, "again")


async def test_spawn_is_refused_when_every_live_child_is_running(tmp_path):
    budget = SubagentBudget(max_concurrent=4, max_depth=1, max_live=1)
    gate = asyncio.Event()
    runner, root, log = _persistent(tmp_path, _gated(gate), budget=budget)
    first = await _started(runner, log)
    refused = await runner.start({"prompt": "go"}, emit=log.emit)
    assert not refused.started and refused.outcome is not None
    assert root.get(first.agent_id).state == "running"
    assert log.of("agent.stopped") == []
    gate.set()
    await asyncio.wait_for(root.get(first.agent_id).task, 5)
