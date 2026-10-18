"""Automatic same-session handoff driven through a real offline ``Runtime``.

``tests/test_session_handoff.py`` exercises ``ContextManager`` directly; these
tests run the whole stack (Runtime -> session turn -> assembler -> provider)
with a scripted provider, a temporary ``NEXUS_HOME`` and a small
``context.max_tokens`` with ``on_overflow = "handoff"``.

The compact-versus-handoff test is a loop-integrity check on one fixed script
(same tool calls, same results, bounded provider requests). It is explicitly
not a completion-rate or quality measurement.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ContextSection, ModelSection
from nexus.events import Event
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.runtime import Runtime
from nexus.view import fold

BRIEF_PREFIX = "Handoff brief v1"
PAD = "word " * 300
#: History tokens the budget leaves above the fixed (system + tools) overhead.
HEADROOM = 3000


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "nexus-home"))


def _config(max_tokens: int, *, overflow: str = "handoff", brief: str = "deterministic") -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="research"),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=0,
                compact_at_fraction=1,
                on_overflow=overflow,
                handoff_brief=brief,
                compaction="drop_oldest",
            ),
        ),
    )


def _runtime(root: Path, provider: ScriptedProvider, config: Config) -> Runtime:
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return Runtime(workspace, home=root / "home", config=config, providers={"scripted": provider})


async def _budget(tmp_path: Path) -> int:
    """A max_tokens that leaves ``HEADROOM`` above this runtime's fixed overhead."""
    provider = ScriptedProvider(text_response("ok"))
    runtime = _runtime(tmp_path / "probe", provider, _config(100_000))
    events = [e async for e in runtime.session("probe").send("probe")]
    await runtime.aclose()
    assembled = next(e for e in events if e.type == "context.assembled")
    return int(assembled.data["context"]["required_tokens"]) + 200 + HEADROOM


def _seed(session, count: int = 16) -> list[str]:
    texts = []
    for i in range(count):
        text = f"seed-{i} {PAD}"
        session.append_message(
            Message(role="user" if i % 2 == 0 else "assistant", content=[Text(text=text)])
        )
        texts.append(text)
    return texts


def _text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, Text))


def _assert_pair_safe(request) -> None:
    open_ids: set[str] = set()
    for message in request.messages:
        for block in message.content:
            if isinstance(block, ToolUse):
                open_ids.add(block.id)
            elif isinstance(block, ToolResult):
                assert block.tool_use_id in open_ids, "tool result without its call in the request"
                open_ids.discard(block.tool_use_id)
    assert not open_ids, "tool call without its result in the request"


def _handoffs(session) -> list[Event]:
    return [e for e in session.events if e.type == "context.handoff"]


def _divider_count(session) -> int:
    view = fold(session.events)
    return sum(1 for turn in view.turns for c in turn.context if c.kind == "handoff")


async def test_automatic_handoff_sends_brief_first_then_pair_safe_suffix(tmp_path):
    budget = await _budget(tmp_path)
    provider = ScriptedProvider(*(text_response(f"answer {i}") for i in range(6)))
    runtime = _runtime(tmp_path / "run", provider, _config(budget))
    session = runtime.session("s")
    seeded = _seed(session)
    todo = {
        "session": "s", "agent_id": "root", "revision": 3,
        "todos": [{"id": "t1", "content": "keep me", "status": "pending"}],
    }
    session.append_event(Event(type="todo.updated", session="s", data=todo))
    queued_id = session.enqueue("queued for later")

    events = [e async for e in session.send("go")]

    first = provider.requests[0]
    brief = _text(first.messages[0])
    assert first.messages[0].role == "user" and brief.startswith(BRIEF_PREFIX)
    kept = first.messages[1:-1]
    assert 0 < len(kept) < len(seeded)
    assert [_text(m) for m in kept] == seeded[-len(kept):]  # contiguous newest suffix
    assert _text(first.messages[-1]) == "go"
    _assert_pair_safe(first)

    assert len(_handoffs(session)) == 1
    assert _divider_count(session) == 1
    assert runtime.sessions.store.session_row("s")["handoff_count"] == 1

    # Session state is not moved or dropped by the handoff.
    assert not any(e.type == "input.dropped" for e in session.events)
    # The queued submission is promoted to a detached turn at the turn boundary;
    # wait for it to settle (event-driven, no sleeps) before asserting delivery.
    await session.wait_idle()
    assert queued_id not in session.queued_ids
    assert any(
        e.type == "input.consumed" and e.data.get("queued_id") == queued_id
        for e in session.events
    )
    assert provider.calls == 2
    assert "queued for later" in _text(provider.requests[1].messages[-1])
    todos = [e for e in session.events if e.type == "todo.updated"]
    assert todos[-1].data["todos"] == todo["todos"] and todos[-1].data["revision"] == 3
    assert events[-1].type == "turn.completed"

    # A small follow-up turn must not immediately roll over again.
    head_seq = runtime.sessions.store.session_row("s")["context_head_seq"]
    follow_up = [e async for e in session.send("ok")]
    assert follow_up[-1].type == "turn.completed"
    assert len(_handoffs(session)) == 1 and _divider_count(session) == 1
    row = runtime.sessions.store.session_row("s")
    assert row["handoff_count"] == 1 and row["context_head_seq"] == head_seq
    assert _text(provider.requests[-1].messages[0]) == brief
    for request in provider.requests:
        _assert_pair_safe(request)
    await runtime.aclose()


async def test_summarized_handoff_uses_runtime_summarizer_and_marks_untrusted(tmp_path):
    budget = await _budget(tmp_path)
    provider = ScriptedProvider(
        text_response("RECALL the goal"), text_response("done"), text_response("done again")
    )
    runtime = _runtime(tmp_path / "run", provider, _config(budget, brief="summarized"))
    session = runtime.session("s")
    _seed(session)

    events = [e async for e in session.send("go")]

    recall, main = provider.requests[0], provider.requests[1]
    assert not recall.tools and recall.system.startswith("Recall")
    assert _text(main.messages[0]).startswith(BRIEF_PREFIX)
    assert "Model recall (untrusted):" in _text(main.messages[0])
    assert "RECALL" in _text(main.messages[0])
    [marker] = _handoffs(session)
    assert marker.data["method"] == "summarized" and marker.data["summary_fallback"] is False
    assert events[-1].type == "turn.completed"
    await runtime.aclose()


def _tool_script(calls: int):
    scripts = [tool_response((f"call-{i}", "read", {"path": "notes.txt"})) for i in range(calls)]
    return [*scripts, text_response("all done")]


async def _run_tool_loop(root: Path, budget: int, overflow: str, calls: int):
    provider = ScriptedProvider(*_tool_script(calls))
    runtime = _runtime(root, provider, _config(budget, overflow=overflow))
    (root / "ws" / "notes.txt").write_text("needle-line one\nneedle-line two\n", encoding="utf-8")
    session = runtime.session("s")
    _seed(session)
    events = [e async for e in session.send("run the loop")]
    uses = [b for m in session.messages for b in m.content if isinstance(b, ToolUse)]
    results = [b for m in session.messages for b in m.content if isinstance(b, ToolResult)]
    outcome = {
        "terminal": events[-1].type,
        "calls": [(u.id, u.name, u.input) for u in uses],
        "errors": [r.is_error for r in results],
        "result_texts": ["".join(c.text for c in r.content if isinstance(c, Text)) for r in results],
        "requests": provider.calls,
        "handoffs": len(_handoffs(session)),
        "final": _text(session.messages[-1]),
    }
    for request in provider.requests:
        _assert_pair_safe(request)
    await runtime.aclose()
    return outcome


async def test_compact_and_handoff_run_the_same_tool_loop_intact(tmp_path):
    """Loop integrity only: one fixed script, not a completion-rate measurement."""
    budget = await _budget(tmp_path)
    calls = 4
    compact = await _run_tool_loop(tmp_path / "compact", budget, "compact", calls)
    handoff = await _run_tool_loop(tmp_path / "handoff", budget, "handoff", calls)

    for outcome in (compact, handoff):
        assert outcome["terminal"] == "turn.completed"
        assert outcome["final"] == "all done"
        # Bounded: exactly one model request per scripted response (the provider
        # raises if over-asked), so no retry, summary, or extra model call.
        assert outcome["requests"] == calls + 1
        assert outcome["errors"] == [False] * calls
        assert all("needle-line" in text for text in outcome["result_texts"])
    assert handoff["calls"] == compact["calls"]
    assert [(u[0], u[1]) for u in handoff["calls"]] == [(f"call-{i}", "read") for i in range(calls)]
    assert compact["handoffs"] == 0
    assert 1 <= handoff["handoffs"] <= calls  # rolled over, but never in a loop
