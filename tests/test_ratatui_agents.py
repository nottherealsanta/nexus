"""Ratatui projection of historical background subagent events.

Subagents are foreground-only now, but old session logs still hold background children: the card
keeps the spinner while such a child runs, a completed one reads as finished (no `idle` state),
`stopped by …` stays, and a delivered report is a labelled notification row, never a user prompt.
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


def test_completed_legacy_persistent_child_reads_as_finished_not_idle(tmp_path, monkeypatch):
    view = _root([_completed(8)])
    agent = view.agents[CHILD]
    assert agent_state(agent) == "finished" and agent_state_label(agent) == "completed"
    snapshot = _snapshot(tmp_path, monkeypatch, view)
    block = _task(snapshot)
    assert block["status"] == "done" and block["state"] == ""
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
    row = next(b for b in snapshot["blocks"] if b["kind"] == "agent_report")
    assert row["agent_name"] == "explorer" and row["status"] == "completed"
    assert "task" in row, "the task title travels with the agent name"
    assert row["text"] == "Found x in a.py:3", "the header line is not previewed"
    assert row["count"] == 0
    assert row["operation"] == {"kind": "message_page", "id": row["operation"]["id"]}
    assert row["inspect_operation"] == {"kind": "agent_page", "id": CHILD}
    prompts = [b for b in snapshot["blocks"] if b["kind"] == "user"]
    assert [b["number"] for b in prompts] == [1], "a report-only wake turn takes no #N"
    assert all(b.get("status") != "report" for b in prompts)


def test_report_preview_is_two_lines_and_the_full_text_stays_on_the_message(tmp_path, monkeypatch):
    origin = {"kind": "subagent_report", "agent": CHILD, "run": 1, "status": "completed", "agents": [f"{CHILD}:1"]}
    body = "[Subagent report: s/child/1]\none\n\ntwo\nthree\nfour"
    extra = [
        _completed(8),
        _ev(9, "input.queued", {"queued_id": "q1", "mode": "steer", "origin": origin, "content": [{"type": "text", "text": body}]}, turn=None),
        _ev(10, "turn.started", turn="t2"),
        _ev(11, "input.consumed", {"queued_id": "q1", "turn": "t2", "mode": "steer", "content": [{"type": "text", "text": body}]}, turn="t2"),
    ]
    view = _root(extra)
    row = next(b for b in _snapshot(tmp_path, monkeypatch, view)["blocks"] if b["kind"] == "agent_report")
    assert row["text"] == "one\ntwo" and row["count"] == 2
    message = next(m for t in view.turns for m in t.messages if m.id == row["operation"]["id"])
    assert message.text == body, "the full report stays reachable through the message page"


def test_report_preview_does_not_silently_slice_long_lines_at_300_chars(tmp_path, monkeypatch):
    origin = {"kind": "subagent_report", "agent": CHILD, "run": 1, "status": "completed", "agents": [f"{CHILD}:1"]}
    long_line = "x" * 350
    body = f"[Subagent report: s/child/1]\n{long_line}"
    extra = [
        _completed(8),
        _ev(9, "input.queued", {"queued_id": "q1", "mode": "steer", "origin": origin, "content": [{"type": "text", "text": body}]}, turn=None),
        _ev(10, "turn.started", turn="t2"),
        _ev(11, "input.consumed", {"queued_id": "q1", "turn": "t2", "mode": "steer", "content": [{"type": "text", "text": body}]}, turn="t2"),
    ]
    view = _root(extra)
    row = next(b for b in _snapshot(tmp_path, monkeypatch, view)["blocks"] if b["kind"] == "agent_report")
    assert row["text"] == long_line, "long lines are not sliced at 300 chars in Python; Rust bounds display"


def test_ordinary_user_message_is_not_an_agent_report(tmp_path, monkeypatch):
    blocks = _snapshot(tmp_path, monkeypatch, _root([_completed(8)]))["blocks"]
    assert not [b for b in blocks if b["kind"] == "agent_report"]
    assert [b["kind"] for b in blocks if b["kind"] == "user"]


def test_batched_report_lists_every_agent_and_run(tmp_path, monkeypatch):
    origin = {"kind": "subagent_report", "agent": "a", "run": 1, "status": "completed", "agents": ["a:1", "b:2"]}
    extra = [
        _ev(8, "input.queued", {"queued_id": "q1", "mode": "steer", "origin": origin, "content": [{"type": "text", "text": "two reports"}]}, turn=None),
        _ev(9, "turn.started", turn="t2"),
        _ev(10, "input.consumed", {"queued_id": "q1", "turn": "t2", "mode": "steer", "content": [{"type": "text", "text": "two reports"}]}, turn="t2"),
    ]
    row = next(b for b in _snapshot(tmp_path, monkeypatch, _root(extra))["blocks"] if b["kind"] == "agent_report")
    assert row["agent_name"] == "2 subagents" and row["task"] == "a:1, b:2"
    assert "inspect_operation" not in row


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


async def test_tasks_menu_offers_cancel_and_close_only_while_running(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for events, expected in (([], ["Cancel", "Close"]), ([_completed(8)], []),
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
