"""Ratatui projection of non-blocking, persistent subagents (plans/done/NONBLOCKING_SUBAGENTS_PLAN.md phase 7).

A background child outlives its tool call and the root turn: its card keeps the spinner while it
runs, names `idle · run N` / `stopped by …` afterwards, and a delivered report is a labelled
notification row, never a user prompt.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.events import Event
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.ui.ratatui.turn_status import running_agents
from nexus.ui_support.timeline import agent_state, agent_state_label
from nexus.view import fold

CHILD = "s/child/1"
META = {"id": CHILD, "parent": "s", "depth": 1, "type": "explorer", "task": "t1", "index": 0,
        "parent_call_id": "c1", "session": CHILD}


def _ev(seq, type_, data=None, *, turn="t1"):
    return Event(type=type_, data=dict(data or {}), seq=seq, ts=float(seq), session="s", turn=turn, id=f"e{seq:03d}")


def _root(extra=()):
    rows = [
        _ev(1, "turn.started"),
        _ev(2, "input.queued", {"queued_id": "q0", "mode": "queue", "content": [{"type": "text", "text": "look into it"}]}),
        _ev(3, "input.consumed", {"queued_id": "q0", "turn": "t1", "mode": "queue", "content": [{"type": "text", "text": "look into it"}]}),
        _ev(4, "tool.requested", {"call_id": "c1", "tool": "subagent", "input": {"prompt": "find x", "description": "find x"}}),
        _ev(5, "tool.started", {"call_id": "c1", "tool": "subagent"}),
        _ev(6, "agent.spawned", {"agent": META, **META, "background": True, "continuable": True}),
        _ev(7, "tool.completed", {"call_id": "c1", "tool": "subagent", "content": [{"type": "text", "text": "Started subagent"}]}),
    ]
    return fold([*rows, *extra])


def _completed(seq, run=1, **extra):
    return _ev(seq, "agent.completed", {"agent": META, **META, "status": "completed", "ok": True, "run": run,
                                        "continuable": True, **extra}, turn=None)


def _snapshot(tmp_path, monkeypatch, view, literal=False):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    return project(controller, 1, shell=shell, literal=literal)


def _task(snapshot):
    return next(b for b in snapshot["blocks"] if b["kind"] == "task")


def test_acked_background_call_keeps_its_spinner_while_the_child_runs(tmp_path, monkeypatch):
    view = _root()
    assert view.turns[0].tools[0].status == "completed"  # the ack finished the tool call
    block = _task(_snapshot(tmp_path, monkeypatch, view))
    assert block["status"] == "running" and block["state"] == ""
    assert _snapshot(tmp_path, monkeypatch, view)["agents_running"] == 1


def test_idle_child_card_names_the_state_and_run(tmp_path, monkeypatch):
    view = _root([_completed(8)])
    agent = view.agents[CHILD]
    assert agent_state(agent) == "idle" and agent_state_label(agent) == "idle · run 1"
    snapshot = _snapshot(tmp_path, monkeypatch, view)
    block = _task(snapshot)
    assert block["status"] == "done" and block["state"] == "idle · run 1"
    assert snapshot["agents_running"] == 0


def test_resumed_child_runs_again_and_counts_runs(tmp_path, monkeypatch):
    resumed = _ev(9, "agent.resumed", {"agent": META, **META, "run": 2, "from": "root", "message": "also check tests"}, turn=None)
    view = _root([_completed(8), resumed])
    block = _task(_snapshot(tmp_path, monkeypatch, view))
    assert block["status"] == "running" and block["state"] == "running · run 2"
    assert running_agents(view) == 1


def test_stopped_child_shows_who_stopped_it_and_why(tmp_path, monkeypatch):
    stopped = _ev(9, "agent.stopped", {"agent": META, **META, "by": "system", "reason": "evicted: least recently run"}, turn=None)
    view = _root([_completed(8), stopped])
    agent = view.agents[CHILD]
    assert agent_state(agent) == "stopped"
    assert agent_state_label(agent) == "stopped by system: evicted: least recently run"
    block = _task(_snapshot(tmp_path, monkeypatch, view))
    assert block["state"].startswith("stopped by system") and block["status"] == "done"
    assert running_agents(view) == 0


def test_not_continuable_child_keeps_its_plain_status(tmp_path, monkeypatch):
    view = _root([_completed(8, continuable=False)])
    assert agent_state(view.agents[CHILD]) == "finished"
    assert _task(_snapshot(tmp_path, monkeypatch, view))["state"] == ""


def test_interrupted_child_is_labelled(tmp_path, monkeypatch):
    view = _root([_completed(8, status="interrupted", ok=False)])
    assert agent_state_label(view.agents[CHILD]) == "interrupted · run 1"


def test_report_row_is_a_labelled_notification_not_a_numbered_prompt(tmp_path, monkeypatch):
    origin = {"kind": "subagent_report", "agent": CHILD, "run": 1, "status": "completed", "agents": [f"{CHILD}:1"]}
    body = "[Subagent report: s/child/1]\nFound x in a.py:3"
    extra = [
        _completed(8),
        _ev(9, "input.queued", {"queued_id": "q1", "mode": "steer", "origin": origin, "content": [{"type": "text", "text": body}]}, turn=None),
        _ev(10, "turn.started", turn="t2"),
        _ev(11, "input.consumed", {"queued_id": "q1", "turn": "t2", "mode": "steer", "content": [{"type": "text", "text": body}]}, turn="t2"),
    ]
    snapshot = _snapshot(tmp_path, monkeypatch, _root(extra))
    row = next(b for b in snapshot["blocks"] if b["kind"] == "user" and b.get("status") == "report")
    assert row["title"] == "report · s/child/1 · explorer · run 1 · completed"
    assert "Found x in a.py:3" in row["text"], "the report body stays readable"
    assert row["number"] == 0
    assert row["operation"] == {"kind": "agent_page", "id": CHILD}
    prompts = [b for b in snapshot["blocks"] if b["kind"] == "user" and b.get("status") != "report"]
    assert [b["number"] for b in prompts] == [1], "a report-only wake turn takes no #N"


def test_batched_report_lists_every_agent_and_run(tmp_path, monkeypatch):
    origin = {"kind": "subagent_report", "agent": "a", "run": 1, "status": "completed", "agents": ["a:1", "b:2"]}
    extra = [
        _ev(8, "input.queued", {"queued_id": "q1", "mode": "steer", "origin": origin, "content": [{"type": "text", "text": "two reports"}]}, turn=None),
        _ev(9, "turn.started", turn="t2"),
        _ev(10, "input.consumed", {"queued_id": "q1", "turn": "t2", "mode": "steer", "content": [{"type": "text", "text": "two reports"}]}, turn="t2"),
    ]
    row = next(b for b in _snapshot(tmp_path, monkeypatch, _root(extra))["blocks"] if b.get("status") == "report")
    assert row["title"] == "reports · 2 subagents · a:1, b:2"


def test_follow_up_row_is_drawn_on_the_agent_page(tmp_path, monkeypatch):
    resumed = _ev(9, "agent.resumed", {"agent": META, **META, "run": 2, "from": "root", "message": "also check tests"}, turn=None)
    view = _root([_completed(8), resumed])
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    shell.workflows.agent_page_id = CHILD
    shell.workflows.agent_context = {"messages": []}
    snapshot = project(controller, 1, shell=shell, literal=False)
    assert snapshot["agent_page"] == CHILD
    row = next(b for b in snapshot["blocks"] if b.get("status") == "followup")
    assert row["title"] == "follow-up from root · s/child/1 · run 2"
    assert "also check tests" in row["text"], "the follow-up body stays readable"
    assert row["number"] == 0


def test_stopped_child_page_ends_with_the_stop_record(tmp_path, monkeypatch):
    stopped = _ev(9, "agent.stopped", {"agent": META, **META, "by": "system", "reason": "evicted"}, turn=None)
    view = _root([_completed(8), stopped])
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    shell.workflows.agent_page_id = CHILD
    shell.workflows.agent_context = {"messages": []}
    snapshot = project(controller, 1, shell=shell, literal=False)
    last = snapshot["blocks"][-1]
    assert last["title"] == "Subagent stopped · by system" and "Reason: evicted" in last["text"]


def test_wake_pause_notice_is_a_visible_row(tmp_path, monkeypatch):
    notice = _ev(8, "context.notice", {"kind": "subagent_wake_paused", "message": "Subagent reports are paused: send a message", "pending": 2}, turn=None)
    snapshot = _snapshot(tmp_path, monkeypatch, _root([notice]))
    row = next(b for b in snapshot["blocks"] if b.get("title") == "Subagent reports paused")
    assert "send a message" in row["text"]


def test_replay_gives_the_same_projection(tmp_path, monkeypatch):
    events = [_completed(8), _ev(9, "agent.stopped", {"agent": META, **META, "by": "root", "reason": "done"}, turn=None)]
    first = _snapshot(tmp_path, monkeypatch, _root(events))
    second = _snapshot(tmp_path, monkeypatch, _root(events + events))  # duplicate seqs are ignored
    assert _task(first)["state"] == _task(second)["state"] == "stopped by root: done"


async def test_tasks_menu_offers_cancel_for_running_and_close_for_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for events, expected in (([], ["Cancel", "Close"]), ([_completed(8)], ["Close"]),
                             ([_completed(8), _ev(9, "agent.stopped", {"agent": META, **META, "by": "root"}, turn=None)], [])):
        controller = SimpleNamespace(view=_root(events), session="s")
        shell = ShellActions(controller)
        await shell.command("/tasks", [])
        labels = [item["label"] for item in shell.items]
        assert [w for w in ("Cancel", "Close") if any(w in label for label in labels)] == expected, labels
        kinds = {item["operation"]["kind"] for item in shell.items}
        assert ("agent_cancel" in kinds) == ("Cancel" in expected) and ("agent_close" in kinds) == ("Close" in expected)


async def test_close_row_calls_close_agent_and_esc_toasts_running_children(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    calls = []

    class Client:
        async def close_agent(self, session, agent_id, *, reason=""):
            calls.append((session, agent_id))
            return SimpleNamespace(closed=True, message="")

        async def cancel_to_composer(self, session, *, reason=""):
            return SimpleNamespace(returned_messages=[])

    controller = SimpleNamespace(view=_root(), session="s", client=Client())
    controller.cancel = lambda: controller.client.cancel_to_composer("s")
    shell = ShellActions(controller)
    await shell.workflows.operate({"kind": "agent_close", "id": CHILD})
    assert calls == [("s", CHILD)]
    await shell.cancel()
    assert any("1 subagent still running (/tasks to stop)" in t["title"] + t.get("body", "") for t in shell.toasts)
