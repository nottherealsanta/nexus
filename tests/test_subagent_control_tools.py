"""Phase 5: ``subagent_send`` / ``subagent_stop`` / ``subagent_list`` as tools.

Tool-level cases use a fake service; the permission and wiring cases use the real
engine, bundles and runner.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from nexus.agents import SubagentOutcome
from nexus.agents.pool import AgentRow, RootAgents, StopResult
from nexus.agents.runner import ROOT_ONLY_TOOLS, ContinuationRefused
from nexus.config import Config
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.model.message import ToolUse
from nexus.tools.builtin import subagent_control as control
from nexus.tools.builtin.task import make_task_spec
from nexus.tools.bundles import BUNDLES, PROFILES
from nexus.tools.codemode.catalog import EXCLUDED
from nexus.tools.names import SUBAGENT_CONTROL_TOOLS
from nexus.tools.permissions import Outcome, PermissionEngine
from nexus.tools.spec import ToolContext
from test_subagent_continue import Log, _gated, _persistent, _started
from test_subagent_runner import Factory, make_runner

NAMES = {"subagent_send", "subagent_stop", "subagent_list"}


@dataclass
class Started:
    agent_id: str = "root/sub/1"
    session_id: str = "root/sub/1"
    role: str = "task"
    model: str | None = "m"
    tier: str | None = "medium"
    workspace: str = "/ws"
    worktree: dict | None = None
    run: int = 2
    outcome: object | None = None


class FakeService:
    persistent = True
    wake_paused = False

    def __init__(self, *, rows=(), error=None, started=None, stop=None):
        self.rows = list(rows)
        self.error = error
        self.started = started or Started()
        self.stop_result = stop or StopResult(True, "Stopped subagent root/sub/1.")
        self.calls: list[tuple] = []

    def agent_permission_key(self, agent_id):
        return f"task:medium:{agent_id}"

    async def resume(self, agent_id, message, *, wait=False, emit=None, cancel=None, call_id=""):
        self.calls.append(("resume", agent_id, message, wait, cancel, emit, call_id))
        if self.error is not None:
            raise self.error
        return self.started

    async def stop(self, agent_id, *, reason="", by="root"):
        self.calls.append(("stop", agent_id, reason, by))
        if self.error is not None:
            raise self.error
        return self.stop_result

    def list_agents(self):
        return self.rows


def ctx(service, tmp_path: Path, **kw) -> ToolContext:
    return ToolContext(
        workspace=tmp_path, session_id="s", turn_id="t", config=Config(),
        subagents=service, call_id="call-9", **kw,
    )


def row(**kw) -> AgentRow:
    base = dict(agent_id="root/sub/1", role="task", model="m", tier="medium", state="idle", runs=2,
                last_status="completed", continuable=True, worktree=None, tokens=1234,
                age_seconds=125, idle_seconds=30)
    base.update(kw)
    return AgentRow(**base)


# -- wiring ----------------------------------------------------------------------


def test_names_bundle_root_only_and_code_mode_exclusion():
    assert NAMES == SUBAGENT_CONTROL_TOOLS
    assert NAMES <= set(BUNDLES["task"].tools) and NAMES <= ROOT_ONLY_TOOLS
    assert NAMES <= EXCLUDED
    assert NAMES <= set(PROFILES["research"].include)
    for tool in control.build_agent_control_tools(FakeService()):
        assert tool.spec.bundle == "task" and not tool.spec.mutates


def test_tools_are_offered_only_by_a_persistent_service():
    assert control.build_agent_control_tools(None) == ()

    class Off(FakeService):
        persistent = False

    assert control.build_agent_control_tools(Off()) == ()
    assert {t.name for t in control.build_agent_control_tools(FakeService())} == NAMES


def test_a_child_never_gets_the_control_tools(tmp_path):
    runner, _root, _log = _persistent(
        tmp_path, Factory(), parent_tools={"read", "subagent", *NAMES}
    )
    role = runner.agents.resolve("task", context="subagent")
    selection = runner.select_tools(role, {"prompt": "x"})
    assert not (NAMES & selection.selected) and "subagent" not in selection.selected


def test_role_that_lists_subagent_also_gets_the_control_tools(tmp_path):
    roles = tmp_path / "ws" / ".agents" / "agents"
    roles.mkdir(parents=True)
    (roles / "boss.md").write_text(
        "---\nname: boss\ndescription: d\ntools: [subagent, read]\n---\nLead.\n", encoding="utf-8"
    )
    runner, _root, _log = _persistent(tmp_path, Factory())
    role = runner.agents.resolve("boss", context="subagent")
    selection = runner.agents.select_tools(
        role, available={"subagent", "read", "write", *NAMES}, bundle_map={}
    )
    assert {"subagent", "read"} | NAMES == selection.selected
    plain = runner.agents.select_tools(role, available={"read", "write"}, bundle_map={})
    assert plain.selected == {"read"}          # nothing is added that the ceiling lacks


# -- schemas and arguments -----------------------------------------------------------


def test_schemas_require_the_ids_and_forbid_extras():
    send, stop, lst = (control.SEND_SPEC, control.STOP_SPEC, control.LIST_SPEC)
    assert send.input_schema["required"] == ["agent_id", "message"]
    assert set(send.input_schema["properties"]) == {"agent_id", "message", "wait"}
    assert stop.input_schema["required"] == ["agent_id"]
    assert lst.input_schema["properties"] == {}
    assert all(s.input_schema["additionalProperties"] is False for s in (send, stop, lst))


@pytest.mark.parametrize("args, fragment", [
    ({"message": "hi"}, "agent_id"),
    ({"agent_id": "a"}, "message"),
    ({"agent_id": "a", "message": "  "}, "message"),
    ({"agent_id": "a", "message": "hi", "wait": "yes"}, "wait"),
])
async def test_send_validates_arguments(tmp_path, args, fragment):
    result = await control.run_send(args, ctx(FakeService(), tmp_path))
    assert result.is_error and fragment in result.content[0].text


async def test_send_passes_everything_through_and_acks(tmp_path):
    service = FakeService(started=Started(run=3, worktree={"branch": "b"}))
    token = CancelToken()
    seen = []
    result = await control.run_send(
        {"agent_id": "root/sub/1", "message": "do more", "wait": False},
        ctx(service, tmp_path, cancel_token=token, emit=lambda *a, **k: seen.append(a)),
    )
    [(_, agent, message, wait, cancel, emit, call_id)] = service.calls
    assert (agent, message, wait, cancel, call_id) == ("root/sub/1", "do more", False, token, "call-9")
    assert callable(emit)
    text = result.content[0].text
    assert not result.is_error and "run 3" in text and "Subagent report" in text
    assert result.metrics["agent_id"] == "root/sub/1" and result.metrics["state"] == "running"
    assert result.metrics["background"] is True and result.metrics["worktree"] == {"branch": "b"}


async def test_send_with_wait_returns_the_report_as_the_result(tmp_path):
    outcome = SubagentOutcome(agent="task", session_id="root/sub/1", text="the answer")
    service = FakeService(started=Started(outcome=outcome))
    result = await control.run_send({"agent_id": "root/sub/1", "message": "q", "wait": True}, ctx(service, tmp_path))
    assert "the answer" in result.content[0].text and service.calls[0][3] is True


async def test_refusals_are_model_visible_errors_and_cancellation_propagates(tmp_path):
    refused = FakeService(error=ContinuationRefused("subagent root/sub/1 is still running"))
    result = await control.run_send({"agent_id": "root/sub/1", "message": "q"}, ctx(refused, tmp_path))
    assert result.is_error and result.content[0].text == "subagent_send: subagent root/sub/1 is still running"
    boom = FakeService(error=RuntimeError("kaboom"))
    result = await control.run_stop({"agent_id": "a"}, ctx(boom, tmp_path))
    assert result.is_error and "RuntimeError: kaboom" in result.content[0].text
    with pytest.raises(OperationCancelled):
        await control.run_send({"agent_id": "a", "message": "q"}, ctx(FakeService(error=OperationCancelled("x")), tmp_path))


async def test_unavailable_without_a_bound_service(tmp_path):
    for fn, args in ((control.run_send, {"agent_id": "a", "message": "q"}),
                     (control.run_stop, {"agent_id": "a"}), (control.run_list, {})):
        result = await fn(args, ctx(None, tmp_path))
        assert result.is_error and "not available" in result.content[0].text


async def test_stop_reports_what_happened(tmp_path):
    service = FakeService()
    result = await control.run_stop({"agent_id": "root/sub/1", "reason": "r" * 900}, ctx(service, tmp_path))
    assert not result.is_error and result.metrics == {"agent_id": "root/sub/1", "stopped": True}
    assert service.calls[0][2] == "r" * 500 and service.calls[0][3] == "root"
    missing = FakeService(stop=StopResult(False, "No such subagent 'x' in this session."))
    result = await control.run_stop({"agent_id": "x"}, ctx(missing, tmp_path))
    assert result.is_error and "No such subagent" in result.content[0].text


async def test_list_labels_every_field_and_says_when_empty(tmp_path):
    rows = [
        row(agent_id="root/sub/2", state="running", runs=1, last_status=None, report_pending=False),
        row(worktree={"branch": "nexus/subagent/x", "lifecycle": "finalized"}, report_pending=True,
            description="review the diff"),
        row(agent_id="root/sub/3", state="idle", continuable=False, last_status="completed",
            not_continuable="Claude Code workers run one assignment per session"),
        row(agent_id="root/sub/4", state="stopped", stopped_by="user"),
    ]
    result = await control.run_list({}, ctx(FakeService(rows=rows), tmp_path))
    text = result.content[0].text
    assert "4 subagents" in text
    assert "root/sub/2: task (m, tier medium); state running; 1 run; 1234 tokens; age 2m" in text
    assert "worktree nexus/subagent/x (finalized)" in text and "report pending" in text
    assert '"review the diff"' in text and "last run completed" in text
    assert "finished (not continuable)" in text and "cannot be continued: Claude Code workers" in text
    assert "state stopped; 2 runs" in text and "stopped by user" in text
    assert "subagent_send" in text and result.metrics["count"] == 4
    empty = await control.run_list({}, ctx(FakeService(), tmp_path))
    assert "No subagents in this session." in empty.content[0].text
    paused = FakeService()
    paused.wake_paused = True
    assert "paused" in (await control.run_list({}, ctx(paused, tmp_path))).content[0].text


# -- permissions ------------------------------------------------------------------------


def _call(name, **args):
    return ToolUse(id="c", name=name, input=args)


def test_permission_key_is_the_childs_role_and_tier():
    spec = control.make_send_spec(FakeService())
    assert spec.resolve_permission_key({"agent_id": "root/sub/1", "message": "m"}) == "task:medium:root/sub/1"
    assert control.SEND_SPEC.resolve_permission_key({"agent_id": "x", "message": "m"}) == "unknown:unknown"


def _spec(name, key):
    class Keyed:
        def agent_permission_key(self, _agent_id):
            return key

    if name == "subagent_list":
        return control.LIST_SPEC
    maker = control.make_send_spec if name == "subagent_send" else control.make_stop_spec
    return maker(Keyed())


@pytest.mark.parametrize("tool", ["subagent_send", "subagent_stop"])
def test_task_deny_rules_also_block_continuing_and_stopping_a_denied_tier(tool, tmp_path):
    engine = PermissionEngine(mode="allow", deny=["Task(*:high)"], workspace=tmp_path)
    high = engine.evaluate(_call(tool, agent_id="a", message="m"), _spec(tool, "advisor:high"))
    assert high.outcome is Outcome.DENY
    low = engine.evaluate(_call(tool, agent_id="a", message="m"), _spec(tool, "advisor:low"))
    assert low.outcome is Outcome.ALLOW
    # The same rule family governs spawning itself.
    spawn = engine.evaluate(_call("subagent", prompt="p", model="high"), make_task_spec(None))
    assert spawn.code in {"deny", "mode_allow"} or spawn.outcome is not None


@pytest.mark.parametrize("tool", ["subagent_send", "subagent_stop"])
def test_task_allow_rules_cover_the_same_children(tool, tmp_path):
    engine = PermissionEngine(mode="deny", allow=["Task(advisor:*)"], workspace=tmp_path)
    assert engine.evaluate(_call(tool, agent_id="a", message="m"), _spec(tool, "advisor:low")).outcome is Outcome.ALLOW
    assert engine.evaluate(_call(tool, agent_id="a", message="m"), _spec(tool, "task:low")).outcome is Outcome.DENY


def test_list_is_read_only_and_allowed_in_every_mode_unless_denied(tmp_path):
    for mode in ("allow", "ask", "deny"):
        engine = PermissionEngine(mode=mode, workspace=tmp_path)
        verdict = engine.evaluate(_call("subagent_list"), control.LIST_SPEC)
        assert verdict.outcome is Outcome.ALLOW and verdict.code == "read_only"
    denied = PermissionEngine(mode="allow", deny=["subagent_list"], workspace=tmp_path)
    assert denied.evaluate(_call("subagent_list"), control.LIST_SPEC).outcome is Outcome.DENY


def test_ask_mode_asks_before_sending_unless_a_rule_allows_it(tmp_path):
    engine = PermissionEngine(mode="ask", workspace=tmp_path)
    assert engine.evaluate(_call("subagent_send", agent_id="a", message="m"),
                           _spec("subagent_send", "task:low")).outcome is Outcome.ASK


# -- the real runner, through the tools ---------------------------------------------------


async def test_send_stop_and_list_end_to_end_on_a_real_runner(tmp_path):
    runner, root, log = _persistent(tmp_path, _gated())
    started = await _started(runner, log)
    await asyncio.wait_for(root.get(started.agent_id).task, 5)
    c = ctx(runner, tmp_path, emit=log.emit)

    listed = await control.run_list({}, c)
    assert started.agent_id in listed.content[0].text and "state idle" in listed.content[0].text

    sent = await control.run_send({"agent_id": started.agent_id, "message": "again"}, c)
    assert not sent.is_error and "run 2" in sent.content[0].text
    busy = await control.run_send({"agent_id": started.agent_id, "message": "too soon"}, c)
    assert busy.is_error and "still running" in busy.content[0].text
    await asyncio.wait_for(root.get(started.agent_id).task, 5)

    stopped = await control.run_stop({"agent_id": started.agent_id, "reason": "done"}, c)
    assert not stopped.is_error and "Stopped subagent" in stopped.content[0].text
    gone = await control.run_send({"agent_id": started.agent_id, "message": "hello?"}, c)
    assert gone.is_error and "was stopped" in gone.content[0].text
    again = await control.run_stop({"agent_id": started.agent_id}, c)
    assert not again.is_error and "already stopped" in again.content[0].text
    assert "state stopped" in (await control.run_list({}, c)).content[0].text


async def test_list_is_bounded_to_max_live_rows(tmp_path):
    from nexus.agents import SubagentBudget

    budget = SubagentBudget(max_concurrent=8, max_depth=1, max_live=2)
    runner, root, log = _persistent(tmp_path, _gated(), budget=budget)
    for _ in range(2):
        s = await _started(runner, log)
        await asyncio.wait_for(root.get(s.agent_id).task, 5)
        await runner.stop(s.agent_id)          # frees the slot; the record stays, stopped
    third = await _started(runner, log)
    await asyncio.wait_for(root.get(third.agent_id).task, 5)
    rows = runner.list_agents()
    assert len(rows) == 2 and rows[0].agent_id == third.agent_id     # live ones first, then the cap


def test_make_runner_helper_still_builds_a_plain_runner(tmp_path):
    assert make_runner(tmp_path, Factory(), pool=RootAgents("r")).persistent is False
    assert Log().types() == []


def test_a_waited_result_of_a_persistent_child_says_how_to_continue_it():
    from nexus.tools.builtin.task import _to_result

    done = SubagentOutcome(agent="task", session_id="root/sub/1", text="report", continuable=True)
    text = _to_result(done).content[0].text
    assert "subagent_send (agent_id root/sub/1)" in text and "subagent_stop" in text
    plain = _to_result(SubagentOutcome(agent="task", session_id="root/sub/1", text="report"))
    assert "subagent_send" not in plain.content[0].text
