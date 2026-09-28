"""The ``question`` tool end to end: broker, runtime binding, host, and view.

A root agent asks through the scripted provider, an attended view answers by
call id through ``QuestionAnswer``, and the answer reaches the model as the tool
result. Without an operator the tool fails fast instead of parking the turn.
Questions never open an approval of their own and never change grants.
"""
from __future__ import annotations

import asyncio

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime
from nexus.tools.builtin import question
from nexus.tools.permissions import PermissionEngine
from nexus.tools.spec import ToolCall, ToolContext
from nexus.ui_support.prompts import approval_choices, pending_questions
from nexus.view import fold


async def wait_for(predicate, timeout=5.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout)


def _config(mode: str = "ask") -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode=mode, on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


def _tool_result_text(events) -> str:
    for event in events:
        if event.type == "tool.result":
            content = event.data.get("content") or []
            return " ".join(str(block.get("text", "")) for block in content)
    return ""


def test_default_permission_mode_no_longer_asks():
    assert PermissionsSection().mode == "allow"


async def test_attended_question_round_trip_by_call_id(tmp_path):
    provider = ScriptedProvider(
        tool_response(("q1", "question", {
            "question": "Which database?", "options": ["Postgres", "SQLite"],
        })),
        text_response("ok"),
    )
    runtime = Runtime(tmp_path, config=_config("ask"), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    view = facade.subscribe("s", 0, follow=True, client_id="a")
    await view.__anext__()

    await facade.start_turn("s", "set it up")
    handle = runtime.session("s")
    await wait_for(lambda: runtime.questions.pending_for("s"))
    # No approval was opened even though the mode is "ask".
    assert not handle.pending_permissions
    pending = pending_questions(fold(handle.events))
    assert [(q.call_id, q.options) for q in pending] == [("q1", ("Postgres", "SQLite"))]

    stale = await facade.handle(p.QuestionAnswer(session="s", call_id="other", answer="1"))
    assert stale.resolved is False
    bad = await facade.handle(p.QuestionAnswer(session="s", call_id="q1", answer="9"))
    assert bad.resolved is False and bad.error
    result = await facade.handle(p.QuestionAnswer(session="s", call_id="q1", answer="2"))
    assert result.resolved is True
    await facade.wait_idle(timeout=5.0)

    types = [event.type for event in handle.events]
    assert types.index("question.requested") < types.index("question.resolved")
    resolved = next(e for e in handle.events if e.type == "question.resolved")
    assert resolved.data["answer_label"] == "SQLite"
    assert resolved.data["call_id"] == "q1"
    assert "The user answered: SQLite" in _tool_result_text(handle.events)
    assert pending_questions(fold(handle.events)) == []
    await view.aclose()
    await runtime.aclose()


async def test_child_agent_question_routes_to_the_root_session(tmp_path):
    provider = ScriptedProvider(
        tool_response(("root-task", "subagent", {"prompt": "decide", "subagent_type": "task"})),
        tool_response(("child-q", "question", {"question": "Ship it?", "options": ["Yes", "No"]})),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = Runtime(tmp_path, config=_config("allow"), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    view = facade.subscribe("s", 0, follow=True, client_id="a")
    await view.__anext__()
    await facade.start_turn("s", "go")
    await wait_for(lambda: runtime.questions.pending_for("s"))
    (request,) = runtime.questions.pending_for("s")
    assert request.source_session_id != "s" and request.call_id == "child-q"
    handle = runtime.session("s")
    await wait_for(lambda: pending_questions(fold(handle.events)))
    (pending,) = pending_questions(fold(handle.events))
    assert pending.call_id == "child-q" and pending.agent
    result = await facade.handle(p.QuestionAnswer(session="s", call_id="child-q", answer="1"))
    assert result.resolved is True
    await facade.wait_idle(timeout=5.0)
    assert any("The user answered: Yes" in str(block) for req in provider.requests for m in req.messages for block in getattr(m, "content", ()))
    await view.aclose()
    await runtime.aclose()


async def test_unattended_question_fails_fast(tmp_path):
    provider = ScriptedProvider(
        tool_response(("q1", "question", {"question": "Proceed?"})),
        text_response("ok"),
    )
    runtime = Runtime(tmp_path, config=_config("allow"), providers={"scripted": provider})
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.start_turn("s", "go")
    await facade.wait_idle(timeout=5.0)
    handle = runtime.session("s")
    assert "No user is attached" in _tool_result_text(handle.events)
    assert not any(event.type == "question.requested" for event in handle.events)
    await runtime.aclose()


async def test_question_tool_validates_options_before_asking(tmp_path):
    ctx = ToolContext(workspace=tmp_path, session_id="s", turn_id="t", config=_config())
    result = await question.run({"question": "Pick?", "options": ["a", "a"]}, ctx)
    assert result.is_error and "distinct" in result.content[0].text
    result = await question.run({"question": "Pick?", "options": ["a", "b", "c", "d"]}, ctx)
    assert result.is_error


def test_question_never_asks_but_deny_still_wins(tmp_path):
    call = ToolCall(id="c", name="question", input={"question": "?"})
    specs = {"question": question.SPEC}
    asking = PermissionEngine(mode="ask", workspace=tmp_path)
    assert asking.plan([call], specs).evaluations[0].outcome.value == "allow"
    denied = PermissionEngine(mode="ask", deny=["question"], workspace=tmp_path)
    assert denied.plan([call], specs).evaluations[0].outcome.value == "deny"
    assert PermissionEngine(mode="deny", workspace=tmp_path).plan(
        [call], specs
    ).evaluations[0].outcome.value == "deny"


def test_approval_choices_fail_closed_and_downgrade():
    normal = approval_choices({"id": "p", "tool": "Write", "key": "a"})
    assert [c.value for c in normal] == ["allow_once", "allow_always", "deny_once", "deny_always"]
    unavailable = approval_choices({"id": "p", "tool": "Move", "targets": []})
    assert [c.disabled for c in unavailable] == [True, True, False, False]
    once = approval_choices({"id": "p", "tool": "Write", "persistence_available": False})
    assert [c.value for c in once] == ["allow_once", "allow_once", "deny_once", "deny_once"]
