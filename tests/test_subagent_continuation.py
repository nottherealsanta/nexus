"""Continuing a finished subagent: ``subagent(resume=<agent_id>)``.

The runner is tested through its injected seams (a fake child runtime, a fake
session facade that stores the ``agent.config`` record, and the recorded parent
events as the lifecycle authority). The last tests drive the real runtime with
a scripted provider to prove the stored transcript is reopened, not recreated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_phase6_integration import drain, make_runtime, tool_result_for
from test_subagent_runner import FakeSessions, Factory, Recorder, make_manager, make_runner

from nexus.agents import SubagentOutcome, SubagentUsage, TaskRequest
from nexus.agents.runner import (
    _LIVE_CHILDREN,
    CHILD_CONFIG_VERSION,
    ChildLifecycle,
    SubagentBudget,
    fold_child_lifecycle,
)
from nexus.events import Event
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.view import fold

SECRET_BODY = "Use token: abcd1234efgh when calling the service.\n"


class ContinuableSessions(FakeSessions):
    """A facade that allocates ids and keeps the ``agent.config`` records."""

    def __init__(self) -> None:
        super().__init__()
        self.allocated = 0
        self.configs: dict[str, dict] = {}
        self.foreign: dict[str, dict] = {}

    def allocate_child_id(self, parent_id: str) -> tuple[int, str]:
        self.allocated += 1
        return self.allocated, f"{parent_id}/sub/{self.allocated}"

    def child_config(self, parent_id: str, identity: str):
        # Another session's child is simply not found, like an unknown id.
        return self.configs.get(identity) if parent_id == "root" else None


class World:
    """One root session's runner, its recorded parent log and child sessions."""

    def __init__(self, tmp_path: Path, *, status: str = "completed", delay: float = 0.0, **kwargs):
        tmp_path.mkdir(parents=True, exist_ok=True)  # a child's workspace must exist
        self.tmp_path = tmp_path
        self.rec = Recorder()
        self.sessions = ContinuableSessions()
        self.status = status
        self.delay = delay

        async def behavior(spec):
            if spec.config_record is not None and not spec.resume:
                self.sessions.configs[spec.session_id] = dict(spec.config_record)
            if self.delay:
                await asyncio.sleep(self.delay)
            return SubagentOutcome(
                agent=spec.agent, session_id=spec.session_id, status=self.status,
                is_error=self.status != "completed", text=f"done: {spec.prompt}",
                usage=SubagentUsage(input_tokens=3, output_tokens=2),
            )

        self.factory = Factory(behavior)
        self.runner = self.make_runner(**kwargs)

    def events(self) -> list[Event]:
        return [
            Event(type=name, data=data, seq=index, ts=float(index), session="root")
            for index, (name, data) in enumerate(self.rec.events, start=1)
        ]

    def make_runner(self, **kwargs):
        kwargs.setdefault("agents", make_manager(self.tmp_path))
        return make_runner(
            self.tmp_path, self.factory, sessions=self.sessions, event_sink=self.rec,
            parent_events=self.events, **kwargs,
        )

    async def spawn(self, **request) -> SubagentOutcome:
        return await self.runner.spawn({"prompt": "first question", "subagent_type": "scout", **request})

    async def resume(self, prompt: str = "and the tests?", agent_id: str = "root/sub/1", **extra):
        return await self.runner.spawn({"prompt": prompt, "resume": agent_id, **extra})


def types(world: World) -> list[str]:
    return world.rec.types()


# -- request shape and report footer ---------------------------------------------------


def test_omitted_subagent_type_differs_from_explicit_only_with_resume():
    assert TaskRequest.from_value({"prompt": "x"}).subagent_type == "task"
    resumed = TaskRequest.from_value({"prompt": "x", "resume": " root/sub/1 "})
    assert resumed.subagent_type is None and resumed.resume == "root/sub/1"
    explicit = TaskRequest.from_value({"prompt": "x", "resume": "root/sub/1", "subagent_type": "scout"})
    assert explicit.subagent_type == "scout"
    with pytest.raises(Exception):
        TaskRequest.from_value({"prompt": "x", "resume": " "})


async def test_report_footer_names_the_handle_and_continuability(tmp_path):
    world = World(tmp_path)
    outcome = await world.spawn()
    text = outcome.render()
    assert "agent_id: root/sub/1" in text and "run: 1" in text and "continuable: yes" in text
    assert outcome.to_dict()["agent_id"] == "root/sub/1"
    completed = world.rec.of("agent.completed")[-1]
    assert completed["run"] == 1 and completed["continuable"] is True

    failed = World(tmp_path / "f", status="failed")
    outcome = await failed.spawn()
    assert "continuable: no (failed)" in outcome.render()
    assert failed.rec.of("agent.completed")[-1]["continuable"] is False


async def test_no_child_session_means_the_footer_stays_silent(tmp_path):
    # A facade that cannot store records cannot promise continuation.
    rec = Recorder()
    runner = make_runner(tmp_path, Factory(), event_sink=rec)
    outcome = await runner.spawn({"prompt": "x"})
    assert outcome.continuable is None and "continuable" not in outcome.render()


# -- resume mechanics -------------------------------------------------------------------


async def test_resume_reuses_the_session_and_the_recorded_configuration(tmp_path):
    world = World(tmp_path)
    first = await world.spawn(tools=["Read"])
    second = await world.resume()
    assert second.ok and "done: and the tests?" in second.text
    assert "run: 2" in second.render() and "agent_id: root/sub/1" in second.render()

    spawn_spec, resume_spec = world.factory.specs
    assert world.sessions.allocated == 1  # no new child id
    assert resume_spec.resume is True and resume_spec.run == 2
    assert resume_spec.session_id == spawn_spec.session_id == "root/sub/1"
    assert resume_spec.config_record is None  # the record is written once, never again
    assert resume_spec.prompt == "and the tests?"
    for field in ("agent", "system_prompt", "tools", "model", "tier", "fallback",
                  "reasoning_effort", "max_iterations", "context_tokens", "workspace"):
        assert getattr(resume_spec, field) == getattr(spawn_spec, field), field
    assert resume_spec.tools == ("read",)  # the narrower recorded set, not the role's
    assert first.session_id == second.session_id

    assert [t for t in types(world) if t.startswith("agent.")] == [
        "agent.spawned", "agent.completed", "agent.resumed", "agent.completed",
    ]
    [resumed] = world.rec.of("agent.resumed")
    assert resumed["run"] == 2 and resumed["message"] == "and the tests?"
    assert resumed["agent"]["id"] == "root/sub/1" and resumed["from"] == "root"
    assert world.rec.of("agent.completed")[-1]["run"] == 2

    view = fold(world.events()).agents["root/sub/1"]
    assert view.runs == 2 and view.status == "completed" and view.continuable is True


async def test_resume_message_is_redacted_and_clipped_but_the_child_gets_it_whole(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    prompt = "password: hunter2hunter2 " + "x " * 3000
    await world.resume(prompt)
    [resumed] = world.rec.of("agent.resumed")
    assert "hunter2" not in resumed["message"]
    assert len(resumed["message"]) <= 3900 and resumed["message"].endswith("…")
    assert resumed["message_chars"] == len(prompt)
    assert world.factory.specs[-1].prompt == prompt


async def test_a_second_follow_up_works_and_continuation_is_not_fan_out(tmp_path):
    budget = SubagentBudget(max_fanout=1)
    world = World(tmp_path, budget=budget)
    await world.spawn()
    assert budget.children == 1
    assert (await world.resume("one")).ok
    assert (await world.resume("two")).ok
    assert budget.children == 1 and budget.continuations == 2
    # A genuinely new child is still refused by the fan-out cap.
    refused = await world.spawn()
    assert refused.is_error and "budget is exhausted" in refused.text
    assert world.rec.of("agent.resumed")[-1]["run"] == 3


# -- identity and authorization --------------------------------------------------------


async def test_permission_key_comes_from_the_recorded_role_and_tier(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    recorded = world.sessions.configs["root/sub/1"]
    expected = f"{recorded['role']}:{recorded['tier']}"
    assert expected.startswith("scout:")
    runner = world.runner
    assert runner.permission_key({"prompt": "x", "resume": "root/sub/1"}) == expected
    # The caller's claim and the default role never decide the key.
    assert runner.permission_key(
        {"prompt": "x", "resume": "root/sub/1", "subagent_type": "task", "model": "high"}
    ) == expected
    from nexus.tools.builtin import task
    from nexus.tools.permissions import parse_rule

    key = task.make_task_spec(runner).resolve_permission_key({"prompt": "x", "resume": "root/sub/1"})
    assert key == expected
    assert not parse_rule("Task(task:*)").matches("subagent", key, "task")
    assert parse_rule("Task(scout:*)").matches("subagent", key, "task")
    assert parse_rule("Task(*)").matches("subagent", key, "task")


async def test_unresolvable_ids_fail_closed_to_one_key_and_one_message(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    world.sessions.foreign["other/sub/1"] = {}
    for ident in ("root/sub/99", "other/sub/1", "../../etc", "root/sub/1/extra"):
        assert world.runner.permission_key({"prompt": "x", "resume": ident}) == "resume:unresolved"
    unknown = await world.resume(agent_id="root/sub/99")
    foreign = await world.resume(agent_id="other/sub/1")
    assert unknown.is_error and foreign.is_error and unknown.status == "refused"
    assert unknown.text.replace("'root/sub/99'", "X") == foreign.text.replace("'other/sub/1'", "X")
    assert len(world.factory.specs) == 1 and not world.rec.of("agent.resumed")


async def test_incompatible_overrides_are_refused_before_anything_happens(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    for extra in ({"subagent_type": "task"}, {"tools": ["Read"]}, {"model": "low"}, {"worktree": True}):
        outcome = await world.resume(**extra)
        assert outcome.is_error and outcome.status == "refused", extra
    assert len(world.factory.specs) == 1
    assert not world.rec.of("agent.resumed")
    assert world.runner.budget.continuations == 0
    # An explicit type that equals the recorded role is fine.
    assert (await world.resume(subagent_type="scout")).ok


# -- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
async def test_only_a_completed_child_can_be_continued(tmp_path, status):
    world = World(tmp_path, status="completed")
    await world.spawn()
    world.rec.events.append(("agent.resumed", {"agent": {"id": "root/sub/1"}, "run": 2}))
    world.rec.events.append(("agent.completed", {"agent": {"id": "root/sub/1"}, "run": 2, "status": status}))
    outcome = await world.resume()
    assert outcome.is_error and status in outcome.text and len(world.factory.specs) == 1


async def test_a_child_that_never_reported_or_is_still_open_is_refused(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    world.rec.events.append(("agent.resumed", {"agent": {"id": "root/sub/1"}, "run": 2}))
    outcome = await world.resume()
    assert outcome.is_error and "still running" in outcome.text


async def test_a_closed_child_is_refused(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    world.rec.events.append(("agent.stopped", {"agent": {"id": "root/sub/1"}, "by": "user"}))
    outcome = await world.resume()
    assert outcome.is_error and "closed" in outcome.text
    view = fold(world.events()).agents["root/sub/1"]
    assert view.stopped is True and view.continuable is False


async def test_legacy_background_and_recordless_children_are_refused(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    del world.sessions.configs["root/sub/1"]  # created before the record existed
    outcome = await world.resume()
    assert outcome.is_error and "no recorded configuration" in outcome.text
    assert world.runner.permission_key({"prompt": "x", "resume": "root/sub/1"}) == "resume:unresolved"

    legacy = World(tmp_path / "legacy")
    legacy.rec.events.append(("agent.spawned", {"agent": {"id": "root/sub/7"}, "background": True}))
    legacy.rec.events.append(("agent.completed", {"agent": {"id": "root/sub/7"}, "status": "completed"}))
    legacy.sessions.configs["root/sub/7"] = {"version": CHILD_CONFIG_VERSION, "role": "scout", "tier": "low"}
    outcome = await legacy.resume(agent_id="root/sub/7")
    assert outcome.is_error and "background" in outcome.text


async def test_external_workers_and_worktree_children_are_not_continuable(tmp_path):
    external = World(tmp_path / "ext")

    async def worker(spec):
        return SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, text="ok",
            metrics={"authority": "its own tools", "usage": "unknown"},
        )

    external.factory._behavior = worker
    outcome = await external.spawn()
    assert outcome.continuable is False and "external worker" in outcome.render()

    isolated = World(tmp_path / "wt")
    await isolated.spawn()
    isolated.sessions.configs["root/sub/1"]["worktree"] = True
    outcome = await isolated.resume()
    assert outcome.is_error and "worktree" in outcome.text


async def test_missing_role_unrunnable_tier_and_moved_workspace_are_refused(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    record = world.sessions.configs["root/sub/1"]
    original = dict(record)

    record["role"] = "vanished"
    assert "no longer available" in (await world.resume()).text
    record["role"] = original["role"]

    record["workspace"] = str(tmp_path / "elsewhere")
    assert "workspace" in (await world.resume()).text
    record["workspace"] = original["workspace"]

    unrunnable = world.make_runner(tier_probe=lambda tier: False)
    outcome = await unrunnable.spawn({"prompt": "x", "resume": "root/sub/1"})
    assert outcome.is_error and "no model is available" in outcome.text
    assert len(world.factory.specs) == 1


async def test_a_child_that_ran_on_the_parent_model_continues_on_that_same_model(tmp_path):
    # No tier has a runnable model, so the first run falls back to the parent's
    # model and records it; the follow-up must keep it, not refuse or switch.
    world = World(tmp_path, tier_probe=lambda tier: False, parent_model="scripted/m")
    assert (await world.spawn(subagent_type="task")).ok
    record = world.sessions.configs["root/sub/1"]
    assert record["tier"] == "medium" and record["model"] == "scripted/m"

    outcome = await world.resume()
    assert outcome.ok, outcome.text
    assert world.factory.specs[-1].model == "scripted/m"
    assert world.factory.specs[-1].tier == "medium"


async def test_a_parent_model_fallback_child_is_refused_once_the_parent_model_changes(tmp_path):
    world = World(tmp_path, tier_probe=lambda tier: False, parent_model="scripted/m")
    await world.spawn(subagent_type="task")
    moved = world.make_runner(tier_probe=lambda tier: False, parent_model="scripted/other")
    outcome = await moved.spawn({"prompt": "x", "resume": "root/sub/1"})
    assert outcome.is_error and "no model is available" in outcome.text
    assert len(world.factory.specs) == 1  # never silently moved to another model

    gone = world.make_runner(tier_probe=lambda tier: False)
    outcome = await gone.spawn({"prompt": "x", "resume": "root/sub/1"})
    assert outcome.is_error and "no model is available" in outcome.text


async def test_a_tier_routed_child_is_refused_when_its_tier_stops_being_runnable(tmp_path):
    world = World(tmp_path, tier_probe=lambda tier: True, parent_model="scripted/m")
    await world.spawn(subagent_type="task")
    assert world.sessions.configs["root/sub/1"]["model"] == "medium"
    dark = world.make_runner(tier_probe=lambda tier: False, parent_model="scripted/m")
    outcome = await dark.spawn({"prompt": "x", "resume": "root/sub/1"})
    assert outcome.is_error and "no model is available" in outcome.text
    assert len(world.factory.specs) == 1


async def test_concurrent_follow_ups_to_one_child_run_once(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    world.delay = 0.05
    first, second = await asyncio.gather(world.resume("a"), world.resume("b"))
    results = sorted([first, second], key=lambda outcome: outcome.is_error)
    assert results[0].ok and results[1].is_error and "still running" in results[1].text
    assert len(world.factory.specs) == 2 and len(world.rec.of("agent.resumed")) == 1
    # The slot is free again afterwards.
    world.delay = 0
    assert (await world.resume("c")).ok


# -- budgets ----------------------------------------------------------------------------


async def test_per_child_and_per_turn_run_caps(tmp_path):
    world = World(tmp_path, budget=SubagentBudget(max_runs_per_child=2))
    await world.spawn()
    assert (await world.resume("one")).ok
    refused = await world.resume("two")
    assert refused.is_error and "already ran 2 times" in refused.text
    assert len(world.rec.of("agent.resumed")) == 1

    turn = World(tmp_path / "turn", budget=SubagentBudget(max_continuations=1))
    await turn.spawn()
    assert (await turn.resume("one")).ok
    capped = await turn.resume("two")
    assert capped.is_error and "follow-ups" in capped.text
    turn.runner.budget.begin_window()  # the next root turn
    assert (await turn.resume("three")).ok


async def test_tokens_settle_per_run_and_an_exhausted_budget_refuses(tmp_path):
    world = World(tmp_path, token_budget=12, reserved_tokens=10)
    await world.spawn()
    assert world.runner.budget.spent_tokens == 5
    refused = await world.resume()
    assert refused.is_error and "budget is exhausted" in refused.text
    assert world.runner.budget.continuations == 0 and not world.rec.of("agent.resumed")

    roomy = World(tmp_path / "roomy", token_budget=100, reserved_tokens=10)
    await roomy.spawn()
    await roomy.resume()
    assert roomy.runner.budget.spent_tokens == 10  # 5 per run, settled from measured usage
    assert roomy.runner.budget._reserved_tokens == 0


async def test_a_failed_follow_up_still_counts_and_ends_continuability(tmp_path):
    world = World(tmp_path, budget=SubagentBudget(max_continuations=5))
    await world.spawn()
    world.status = "failed"
    failed = await world.resume()
    assert failed.is_error and failed.run == 2 and "continuable: no (failed)" in failed.render()
    assert world.runner.budget.continuations == 1
    world.status = "completed"
    again = await world.resume()  # nothing retries or revives it
    assert again.is_error and "failed" in again.text


# -- authority --------------------------------------------------------------------------


async def test_authority_is_reintersected_never_widened(tmp_path):
    world = World(tmp_path)
    await world.spawn(tools=["Read", "Grep"])
    narrowed = world.make_runner(parent_tools={"Read"}, permissions="perms-now", grants=("grant-now",))
    outcome = await narrowed.spawn({"prompt": "again", "resume": "root/sub/1"})
    assert outcome.ok
    spec = world.factory.specs[-1]
    assert spec.tools == ("read",) and "grep" in spec.dropped_tools
    assert spec.permissions == "perms-now" and spec.grants == ("grant-now",)

    wider = world.make_runner(parent_tools={"Read", "Grep", "Write", "Bash"})
    await wider.spawn({"prompt": "again", "resume": "root/sub/1"})
    assert set(world.factory.specs[-1].tools) == {"read", "grep"}  # not what the parent now has


async def test_a_lowered_max_tier_moves_the_child_down_never_up(tmp_path):
    world = World(tmp_path)
    await world.spawn(subagent_type="task", model="high")
    assert world.sessions.configs["root/sub/1"]["tier"] == "high"
    lowered = world.make_runner(max_tier="medium")
    # `task` allows medium and high, so the clamped tier is still allowed.
    outcome = await lowered.spawn({"prompt": "x", "resume": "root/sub/1"})
    assert outcome.ok and world.factory.specs[-1].tier == "medium"
    assert world.factory.specs[-1].model == "medium"


# -- the stored record ------------------------------------------------------------------


async def test_the_record_is_complete_secret_safe_and_never_relayed(tmp_path):
    workspace = tmp_path / "ws" / ".agents" / "agents"
    workspace.mkdir(parents=True)
    (workspace / "helper.md").write_text(
        "---\nname: helper\ndescription: a helper\ncontexts: [subagent]\n---\nBe careful.\n",
        encoding="utf-8",
    )
    world = World(tmp_path, permissions=object(), grants=("secret-grant",))
    await world.spawn(subagent_type="helper", description="label")
    record = world.sessions.configs["root/sub/1"]
    assert record["version"] == CHILD_CONFIG_VERSION and record["role"] == "helper"
    assert record["system_prompt"].strip() == "Be careful."
    for key in ("role_sha", "tools", "dropped_tools", "model", "fallback", "tier", "requested_tier",
                "reasoning_effort", "max_iterations", "context_tokens", "workspace", "context_read",
                "worktree", "description", "task", "index"):
        assert key in record, key
    assert record["workspace"] == str(tmp_path) and record["worktree"] is False
    blob = repr(record)
    assert "secret-grant" not in blob and "permissions" not in record and "grants" not in record

    [spawned] = world.rec.of("agent.spawned")
    assert len(spawned["config_sha"]) == 16
    assert "Be careful" not in repr(world.rec.events)  # no event carries the prompt
    assert "agent.config" not in types(world)


async def test_role_file_edits_do_not_change_a_continued_child(tmp_path):
    directory = tmp_path / "ws" / ".agents" / "agents"
    directory.mkdir(parents=True)
    role = directory / "helper.md"
    role.write_text("---\nname: helper\ndescription: h\ncontexts: [subagent]\n---\nversion one\n", encoding="utf-8")
    world = World(tmp_path)
    await world.spawn(subagent_type="helper")
    role.write_text("---\nname: helper\ndescription: h\ncontexts: [subagent]\n---\nversion TWO\n", encoding="utf-8")
    fresh = world.make_runner(agents=make_manager(tmp_path))
    assert (await fresh.spawn({"prompt": "x", "resume": "root/sub/1"})).ok
    prompts = [spec.system_prompt for spec in world.factory.specs]
    assert "version one" in prompts[0] and prompts[1] == prompts[0]


async def test_a_prompt_that_redaction_would_change_is_not_continuable(tmp_path):
    directory = tmp_path / "ws" / ".agents" / "agents"
    directory.mkdir(parents=True)
    (directory / "leaky.md").write_text(
        f"---\nname: leaky\ndescription: l\ncontexts: [subagent]\n---\n{SECRET_BODY}", encoding="utf-8"
    )
    world = World(tmp_path)
    outcome = await world.spawn(subagent_type="leaky")
    assert outcome.ok  # it still runs on its real prompt
    assert "abcd1234efgh" in world.factory.specs[0].system_prompt
    record = world.sessions.configs["root/sub/1"]
    assert "abcd1234efgh" not in record["system_prompt"] and record["prompt_redacted"] is True
    assert outcome.continuable is False
    assert "redacted" in (await world.resume()).text


# -- lifecycle fold and reducer ---------------------------------------------------------


async def test_a_failure_while_preparing_a_follow_up_frees_the_claim_and_reservation(tmp_path):
    world = World(tmp_path)
    await world.spawn()
    released = []
    budget = world.runner._budget
    original = budget.release_reservation

    async def spy(reservation):
        released.append(reservation)
        await original(reservation)

    budget.release_reservation = spy
    world.sessions.root_id = lambda parent_id: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await world.resume("and the tests?")
    assert len(released) == 1
    assert "root/sub/1" not in _LIVE_CHILDREN
    del world.sessions.root_id  # the instance override only
    assert (await world.resume("again")).ok


def test_the_child_config_record_is_not_a_conversation_diagnostic():
    record = Event(
        type="agent.config", seq=1, session="root/sub/1",
        data={"role": "task", "system_prompt": "SECRET ROLE PROMPT", "tools": ["read"]},
    )
    view = fold([record])
    assert view.diagnostics == [] and not view.turns and not view.agents
    assert "SECRET ROLE PROMPT" not in repr(view)


def _ev(name: str, **data) -> Event:
    return Event(type=name, data={"agent": {"id": "root/sub/1"}, **data})


def test_lifecycle_fold_matches_exact_ids_and_counts_every_run():
    other = Event(type="agent.completed", data={"agent": {"id": "root/sub/10"}, "status": "failed"})
    events = [
        _ev("agent.spawned"), other, _ev("agent.completed", status="completed", run=1),
        _ev("agent.resumed", run=2), _ev("agent.completed", status="failed", run=2),
    ]
    assert fold_child_lifecycle(events, "root/sub/1") == ChildLifecycle(
        found=True, runs=2, status="failed", open=False
    )
    assert fold_child_lifecycle(events[:4], "root/sub/1").open is True
    assert fold_child_lifecycle(events, "root/sub").found is False
    assert fold_child_lifecycle(events, "other/sub/1").found is False
    legacy = [_ev("agent.spawned", background=True), _ev("agent.stopped")]
    assert fold_child_lifecycle(legacy, "root/sub/1") == ChildLifecycle(
        found=True, runs=1, open=True, stopped=True, background=True
    )


def test_a_run_in_flight_is_not_continuable_in_the_view():
    meta = {"id": "s/sub/1", "parent": "s", "type": "scout", "task": "t", "depth": 1}

    def ev(seq, name, **data):
        return Event(type=name, data={"agent": meta, **meta, **data}, seq=seq, ts=float(seq), session="s")

    done = fold([ev(1, "agent.spawned"), ev(2, "agent.completed", status="completed", run=1, continuable=True)])
    assert done.agents["s/sub/1"].continuable is True
    again = fold([ev(1, "agent.spawned"), ev(2, "agent.completed", status="completed", run=1, continuable=True),
                  ev(3, "agent.resumed", run=2, message="more")])
    assert again.agents["s/sub/1"].continuable is False and again.agents["s/sub/1"].runs == 2


# -- the real runtime -------------------------------------------------------------------


def _request_text(request) -> str:
    return repr(request)


def _user_texts(records) -> list[str]:
    return [
        block.text
        for record in records
        if getattr(record, "message", None) is not None and record.message.role == "user"
        for block in record.message.content
        if getattr(block, "text", None) is not None
    ]


async def test_runtime_reopens_the_stored_child_transcript(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "survey the repo"})),
        text_response("first child report"),
        tool_response(("t2", "subagent", {"prompt": "now the tests", "resume": "root/sub/1"})),
        text_response("second child report"),
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    first = tool_result_for(session, "t1")
    second = tool_result_for(session, "t2")
    assert first is not None and "agent_id: root/sub/1" in first.content[0].text
    assert "continuable: yes" in first.content[0].text
    assert second is not None and not second.is_error, second
    assert "second child report" in second.content[0].text and "first child report" not in second.content[0].text
    assert "run: 2" in second.content[0].text

    assert [e.type for e in events if e.type in ("agent.spawned", "agent.resumed")] == [
        "agent.spawned", "agent.resumed",
    ]
    [resumed] = [e for e in events if e.type == "agent.resumed"]
    assert resumed.data["run"] == 2 and resumed.data["message"] == "now the tests"
    assert "survey the repo" not in repr([e.data for e in events if e.type == "agent.spawned"])
    completed = [e for e in events if e.type == "agent.completed"]
    assert [e.data["run"] for e in completed] == [1, 2]

    # The follow-up request carried the first run's history, and the child's own
    # log holds both user messages in order plus exactly one immutable record.
    child_request = provider.requests[3]
    text = _request_text(child_request)
    assert "survey the repo" in text and "first child report" in text and "now the tests" in text
    store = runtime._ensure_child_sessions().manager.store
    real = store.resolve_child_id("root/sub/1")
    records = list(store.records(real))
    assert _user_texts(records) == ["survey the repo", "now the tests"]
    configs = [r for r in records if getattr(getattr(r, "event", None), "type", "") == "agent.config"]
    assert len(configs) == 1 and configs[0].event.data["role"] == "task"
    assert not [e for e in events if e.type == "agent.config"]  # never in the parent log
    assert all("system_prompt" not in e.data for e in events if e.type.startswith("agent."))
    assert runtime.agent_pool.budget("root").children == 1
    await runtime.aclose()


def _event_types(records) -> list[str]:
    return [r.event.type for r in records if getattr(r, "event", None) is not None]


async def test_a_follow_up_only_appends_to_the_stored_transcript(tmp_path):
    """No context is touched: same session, old records intact, one appended message."""
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "survey the repo"})),
        text_response("first child report"),
        tool_response(("t2", "subagent", {"prompt": "now the tests", "resume": "root/sub/1"})),
        text_response("second child report"),
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    store = runtime._ensure_child_sessions().manager.store
    seen: dict[str, list] = {}
    original = runtime._build_child_assembler

    def spy(spec, config, **kwargs):
        if getattr(spec, "resume", False):  # snapshot the log as the follow-up starts
            real = store.resolve_child_id("root/sub/1")
            seen["real"] = real
            seen["before"] = [repr(r) for r in store.records(real)]
        return original(spec, config, **kwargs)

    runtime._build_child_assembler = spy
    session = runtime.session("root")
    await drain(session)
    assert not tool_result_for(session, "t2").is_error

    real = store.resolve_child_id("root/sub/1")
    assert seen["real"] == real  # the same durable child, not a second allocation
    before = seen["before"]
    after = [repr(r) for r in store.records(real)]
    assert after[: len(before)] == before  # every earlier record is byte-for-byte unchanged
    appended = list(store.records(real))[len(before):]
    assert _user_texts(appended) == ["now the tests"]  # exactly one follow-up message
    # No second setup: one agent.config, no compaction or handoff, and the only
    # system prompt is the one the first run's request carried.
    types = _event_types(store.records(real))
    assert types.count("agent.config") == 1
    assert not [t for t in types if "compact" in t or "handoff" in t]
    assert [r for r in appended if getattr(r, "event", None) is not None and r.event.type == "agent.config"] == []
    assert _user_texts(store.records(real)) == ["survey the repo", "now the tests"]
    await runtime.aclose()


async def test_a_refused_follow_up_leaves_the_child_continuable_and_the_log_untouched(tmp_path, monkeypatch):
    from dataclasses import replace

    from nexus.context.manager import ContextManager

    original = ContextManager._assemble_untouched

    def squeeze(self, env, ctx, outputs, costs, plan, inputs):
        # Only the resumed child (compaction refused) sees a budget with no room for history.
        return original(self, env, ctx, outputs, costs, replace(plan, history_budget=0), inputs)

    monkeypatch.setattr(ContextManager, "_assemble_untouched", squeeze)
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "survey the repo"})),
        text_response("first child report"),
        tool_response(("t2", "subagent", {"prompt": "now the tests", "resume": "root/sub/1"})),
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    store = runtime._ensure_child_sessions().manager.store
    session = runtime.session("root")
    events = await drain(session)

    real = store.resolve_child_id("root/sub/1")
    records = list(store.records(real))
    result = tool_result_for(session, "t2")
    assert result is not None and "start a new subagent" in result.content[0].text.lower()
    assert len(provider.requests) == 4  # the refused follow-up never reached the model
    types = _event_types(records)
    assert not [t for t in types if "compact" in t or "handoff" in t]
    assert not [e for e in events if e.type == "context.compacted"]
    assert types.count("agent.config") == 1
    # Refused before anything was appended: the child log is exactly the first run's,
    # the root still holds the prompt it sent, and the child stays continuable.
    assert _user_texts(records) == ["survey the repo"]
    completed = [e for e in events if e.type == "agent.completed"]
    assert completed[-1].data["run"] == 2 and completed[-1].data["unchanged"] is True
    assert completed[-1].data["continuable"] is True
    from nexus.agents.runner import fold_child_lifecycle

    assert fold_child_lifecycle(events, "root/sub/1").status == "completed"
    await runtime.aclose()


async def test_a_provider_failure_during_preflight_is_also_an_unchanged_refusal(tmp_path, monkeypatch):
    from nexus.context.manager import ContextManager
    from nexus.errors import ProviderError

    async def broken(self, messages):
        raise ProviderError("token counter unavailable")

    monkeypatch.setattr(ContextManager, "preflight", broken)
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "survey the repo"})),
        text_response("first child report"),
        tool_response(("t2", "subagent", {"prompt": "now the tests", "resume": "root/sub/1"})),
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    store = runtime._ensure_child_sessions().manager.store
    session = runtime.session("root")
    events = await drain(session)

    records = list(store.records(store.resolve_child_id("root/sub/1")))
    assert _user_texts(records) == ["survey the repo"]
    completed = [e for e in events if e.type == "agent.completed"]
    assert completed[-1].data["unchanged"] is True and completed[-1].data["continuable"] is True
    await runtime.aclose()


async def test_runtime_refuses_a_foreign_or_unknown_resume_without_running_anything(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "again", "resume": "other/sub/1"})),
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)
    result = tool_result_for(session, "t1")
    assert result is not None and result.is_error
    assert "no finished subagent" in result.content[0].text
    assert not [e for e in events if e.type in ("agent.spawned", "agent.resumed")]
    assert len(provider.requests) == 2  # the root's two turns only: no child ran
    await runtime.aclose()
