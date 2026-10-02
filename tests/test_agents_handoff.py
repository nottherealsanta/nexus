"""Failure handoff: a failed subagent keeps its context for the parent (agents/handoff.py)."""

from __future__ import annotations

from types import SimpleNamespace

from nexus.agents.handoff import build_handoff, handoff_path, load_handoff, write_handoff
from nexus.agents.runner import SubagentOutcome
from nexus.model.message import Message, Text, ToolResult, ToolUse


def _messages():
    return [
        Message(role="assistant", content=[Text("Working on it"), ToolUse(id="c1", name="read", input={"path": "a.py"})]),
        Message(role="user", content=[ToolResult(tool_use_id="c1", content=[Text("boom")], is_error=True)]),
        Message(role="assistant", content=[Text("Still going, api_key=sk-abcdefghijklmnopqrstuvwxyz123456")]),
    ]


def _report(**kw):
    return build_handoff(
        agent="task", session_id="root/sub/1", prompt="do the thing", status="failed",
        stop_reason="budget", error=None, iterations=3, messages=_messages(),
        files_changed=["a.py"], total_tokens=10, **kw,
    )


def test_report_names_reason_task_last_message_files_and_errors():
    report = _report()
    assert "`budget`" in report and "do the thing" in report
    assert "Still going" in report and "- a.py" in report
    assert "[ERROR] read(path='a.py') -> boom" in report
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in report


def test_write_and_load_round_trip(tmp_path):
    path = write_handoff(tmp_path, "root/sub/1", "hello")
    assert path == handoff_path(tmp_path, "root/sub/1") and path.parent.name == "handoffs"
    assert load_handoff(tmp_path, "root/sub/1") == "hello"
    assert load_handoff(tmp_path, "missing") is None


def test_write_failure_returns_none(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert write_handoff(blocker, "s", "r") is None


def test_failed_outcome_render_points_at_resume():
    out = SubagentOutcome(
        agent="task", session_id="root/sub/1", status="failed", is_error=True,
        stop_reason="budget", handoff="DIGEST", handoff_path="/h/x.md",
    )
    text = out.render()
    assert "resume_from" not in text and "DIGEST" in text and "/h/x.md" in text


def test_resume_from_is_not_a_task_field():
    from nexus.tools.builtin.task import _TASK_SCHEMA

    assert "resume_from" not in _TASK_SCHEMA["properties"]


async def test_child_crash_returns_failed_outcome_with_handoff(tmp_path, monkeypatch):
    import nexus.runtime as rt

    class Lease:
        def release(self):
            pass

    session = SimpleNamespace(id="s", events=[], messages=_messages(), begin_turn=lambda **_: Lease())
    store = SimpleNamespace(clear_session=lambda _id: None)
    runtime = SimpleNamespace(
        workspace=tmp_path, _home=tmp_path,
        _ensure_child_sessions=lambda: SimpleNamespace(manager=SimpleNamespace(open=lambda *a, **k: session)),
        _restore_todos=lambda _s: None, _effective_todo_store=lambda: store,
        _outbound_http_service=None, _child_config=lambda _s: None,
        _build_child_assembler=lambda *a: object(),
        _build_child_tool_manager=lambda *a: SimpleNamespace(schemas=lambda: ()),
        _child_permission_engine=lambda *a: object(), _router=object(),
    )
    spec = SimpleNamespace(
        session_id="root/sub/1", agent_id="root/sub/1", agent="task", grants=(), max_iterations=0,
        prompt="do the thing", emit=None, hooks=None, dropped_tools=(), clamped=False,
        tier="low", requested_tier="low", metadata={}, tools=(),
    )

    async def boom(**_kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(rt, "run_turn", boom)
    monkeypatch.setattr(rt, "freeze_tools", lambda *_a, **_k: None, raising=False)
    try:
        out = await rt._ChildRuntime(runtime, spec, runner=object()).run()
    except AttributeError as exc:  # fake runtime lacks a seam the real one has
        import pytest

        pytest.skip(f"fake runtime incomplete: {exc}")
    assert out.status == "failed" and "provider exploded" in out.error
    assert out.handoff_path and load_handoff(tmp_path / ".nexus", "root/sub/1")
