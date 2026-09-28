"""Phase 3 token budget accounting and allocation (plan 5.2)."""
from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import (
    ConfigV2,
    ContextLimits,
    ContextSection,
    ModelParams,
    ModelSection,
)
from nexus.context import ContextManager
from nexus.context.budget import (
    BudgetInputs,
    ContextOverflow,
    PartRequest,
    allocate,
    compute_input_budget,
    effective_max_output_tokens,
    fit_suffix_count,
)
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.model.request import ToolSchema


def v2_config(
    *,
    max_tokens=180_000,
    safety=4000,
    max_output=None,
    compaction="hybrid",
    limits=None,
):
    return Config(
        model="anthropic/claude-test",
        version=2,
        v2=ConfigV2(
            model=ModelSection(
                default="anthropic/claude-test",
                params=ModelParams(max_output_tokens=max_output),
            ),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=safety,
                compaction=compaction,
                limits=limits or ContextLimits(),
            ),
        ),
    )


class FakeSession:
    def __init__(self, *messages):
        self.id = "s1"
        self._messages = list(messages)

    @property
    def messages(self):
        return list(self._messages)


def char_counter():
    """A deterministic counter where one token == one character."""

    def count(text: str) -> int:
        return len(text)

    return count


# ---------------------------------------------------------------------------
# Exact formula and inputs
# ---------------------------------------------------------------------------


def test_input_budget_exact_formula():
    # min(1000, 800) - min(100, 200 hard max) - 50
    assert compute_input_budget(1000, 800, 100, 4096, 200, 50) == 650


def test_input_budget_ignores_unknown_capability_ceiling():
    assert compute_input_budget(1000, 0, None, None, None, 0) == 1000


def test_effective_output_uses_request_or_provider_default_not_hard_max():
    assert effective_max_output_tokens(100, 4096, 200) == 100
    assert effective_max_output_tokens(None, 4096, 200) == 200
    assert effective_max_output_tokens(100, None, 200) == 100
    assert effective_max_output_tokens(None, None, 200) == 0
    assert effective_max_output_tokens(0, -5, 200) == 0


def test_budget_inputs_properties():
    inputs = BudgetInputs(
        config_max_tokens=10_000,
        caps_max_context_tokens=8_000,
        config_max_output_tokens=1_000,
        provider_default_max_output_tokens=4_096,
        caps_max_output_tokens=2_000,
        safety_margin_tokens=500,
    )
    assert inputs.effective_max_output_tokens == 1_000
    assert inputs.input_budget == 6_500


def test_default_output_reserve_is_clamped_to_hard_max_and_leaves_expected_budget():
    inputs = BudgetInputs(
        config_max_tokens=16_000,
        config_max_output_tokens=None,
        provider_default_max_output_tokens=4_096,
        caps_max_output_tokens=128_000,
        safety_margin_tokens=4_000,
    )
    assert inputs.effective_max_output_tokens == 4_096
    assert inputs.input_budget == 7_904


def test_explicit_output_reserve_is_clamped_and_unknown_default_reserves_nothing():
    assert BudgetInputs(
        config_max_tokens=10_000,
        config_max_output_tokens=10_000,
        provider_default_max_output_tokens=4_096,
        caps_max_output_tokens=2_000,
    ).effective_max_output_tokens == 2_000
    assert BudgetInputs(
        config_max_tokens=10_000, caps_max_output_tokens=2_000
    ).effective_max_output_tokens == 0


def test_budget_inputs_from_legacy_v1_config():
    config = Config(model="x")  # v1: context_chars = 64000
    inputs = BudgetInputs.from_config_and_caps(config, None)
    assert inputs.config_max_tokens == 16_000
    assert inputs.input_budget == 16_000


# ---------------------------------------------------------------------------
# Priority overflow
# ---------------------------------------------------------------------------


def test_priority_zero_overflow_names_every_oversized_part():
    inputs = BudgetInputs(config_max_tokens=100)
    requests = [
        PartRequest("identity", 0, 40, required=True),
        PartRequest("soul", 0, 4000, required=True),
        PartRequest("tools", 0, 30, required=True),
        PartRequest("memory", 1, 10, cap=50),
    ]
    with pytest.raises(ContextOverflow) as excinfo:
        allocate(inputs, requests)
    message = str(excinfo.value)
    assert "soul" in message
    assert "identity" in message
    assert "tools" in message
    assert "Context budget exceeded" in message


def test_required_parts_flow_into_allocation_when_they_fit():
    inputs = BudgetInputs(config_max_tokens=1000)
    requests = [
        PartRequest("identity", 0, 50, required=True),
        PartRequest("soul", 0, 40, required=True),
        PartRequest("tools", 0, 20, required=True),
        PartRequest("user", 0, 10, required=True),
        PartRequest("environment", 1, 30, cap=20),
        PartRequest("memory", 1, 100, cap=1000),
        PartRequest("history", 3, 0),
    ]
    plan = allocate(inputs, requests)
    assert plan.required_tokens == 120
    assert plan.granted("identity") == 50
    assert plan.granted("environment") == 20  # capped
    assert plan.granted("memory") == 100
    # 1000 - 120 - 20 - 100 == 760
    assert plan.history_budget == 760
    assert next(a for a in plan.allocations if a.name == "environment").truncated


def test_low_priority_parts_are_allocation_limited_by_remaining_budget():
    inputs = BudgetInputs(config_max_tokens=100)
    requests = [
        PartRequest("identity", 0, 40, required=True),
        PartRequest("environment", 1, 80),
        PartRequest("memory", 1, 80, cap=1000),
        PartRequest("history", 3, 0),
    ]
    plan = allocate(inputs, requests)
    assert plan.granted("environment") == 60
    assert plan.granted("memory") == 0
    assert plan.history_budget == 0


def test_fit_suffix_count_keeps_a_contiguous_suffix():
    costs = [10, 10, 10, 10]
    assert fit_suffix_count(costs, 25) == 2
    assert fit_suffix_count(costs, 40) == 4
    assert fit_suffix_count(costs, 5) == 0
    assert fit_suffix_count([100, 1], 1) == 1


# ---------------------------------------------------------------------------
# Manager-level allocation and caps
# ---------------------------------------------------------------------------


def test_manager_reports_budget_and_respects_part_caps(tmp_path):
    config = v2_config(
        max_tokens=600,
        safety=0,
        limits=ContextLimits(
            environment=50,
            memory=50,
            skills_index=0,
            attachments=0,
        ),
    )
    (tmp_path / "SOUL.md").write_text("S" * 10, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("M" * 500, encoding="utf-8")
    manager = ContextManager(tmp_path, config=config, counter=char_counter())

    request = manager.assemble(FakeSession(Message(role="user", content=[Text("hi")])))

    budget = manager.last_budget
    assert budget["input_budget"] == 600
    memory_alloc = next(p for p in budget["parts"] if p["name"] == "memory")
    assert memory_alloc["cap"] == 50
    assert memory_alloc["granted"] == 50
    assert memory_alloc["truncated"] is True
    assert "[truncated to fit context]" in request.system


def test_tools_are_budgeted_but_never_in_system_text(tmp_path):
    schema = ToolSchema(
        name="Read",
        description="READ-DESCRIPTION",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    manager = ContextManager(tmp_path, config=v2_config(), counter=char_counter())
    manager.freeze_tools([schema])
    request = manager.assemble(FakeSession(Message(role="user", content=[Text("hi")])))

    tools_alloc = next(p for p in manager.last_budget["parts"] if p["name"] == "tools")
    assert tools_alloc["granted"] > 0
    assert request.tools == [schema]
    assert "READ-DESCRIPTION" not in (request.system or "")


def test_priority_zero_overflow_from_a_huge_soul_names_soul(tmp_path):
    (tmp_path / "SOUL.md").write_text("S" * 5000, encoding="utf-8")
    config = v2_config(max_tokens=200, safety=0)
    manager = ContextManager(tmp_path, config=config, counter=char_counter())
    with pytest.raises(ContextOverflow, match="soul"):
        manager.assemble(FakeSession(Message(role="user", content=[Text("hi")])))


# ---------------------------------------------------------------------------
# History selection
# ---------------------------------------------------------------------------


def _conversation(count: int) -> list[Message]:
    messages = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(Message(role=role, content=[Text(text=f"m{index} " * 4)]))
    # Ensure the last message is a user turn (the current input).
    if messages[-1].role != "user":
        messages.append(Message(role="user", content=[Text(text="current")]))
    return messages


def test_200_message_constrained_assembly_succeeds(tmp_path):
    messages = _conversation(200)
    config = v2_config(max_tokens=2000, safety=0)
    manager = ContextManager(tmp_path, config=config, counter=char_counter())

    request = manager.assemble(FakeSession(*messages))

    assert request.messages  # non-empty
    assert request.messages[-1] == messages[-1]
    # A contiguous suffix of the original conversation.
    final = list(request.messages)
    start = len(messages) - len(final)
    assert messages[start:] == final
    # The current user turn appears exactly once.
    assert [m for m in final if m == messages[-1]].__len__() == 1
    accounting = manager.last_compaction
    assert accounting["dropped"] > 0
    assert manager.last_budget["history_budget"] >= 0


def test_current_user_is_never_dropped_even_with_a_tiny_budget(tmp_path):
    messages = _conversation(20)
    config = v2_config(max_tokens=260, safety=0)
    manager = ContextManager(tmp_path, config=config, counter=char_counter())

    request = manager.assemble(FakeSession(*messages))

    assert request.messages[-1] == messages[-1]
    assert request.messages[-1].content[0].text == messages[-1].content[0].text
    assert manager.last_compaction["dropped"] > 0


def test_tool_pairing_survives_whole_message_dropping(tmp_path):
    messages = [
        Message(role="user", content=[Text(text="q" * 200)]),
        Message(
            role="assistant",
            content=[
                ToolUse(id="call-1", name="Read", input={"path": "a" * 200})
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResult(
                    tool_use_id="call-1",
                    content=[Text(text="r" * 400)],
                )
            ],
        ),
        Message(role="user", content=[Text(text="current")]),
    ]
    config = v2_config(max_tokens=700, safety=0)
    manager = ContextManager(tmp_path, config=config, counter=char_counter())

    request = manager.assemble(FakeSession(*messages))

    uses = {
        block.id
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolUse)
    }
    results = [
        block
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResult)
    ]
    for result in results:
        assert result.tool_use_id in uses, "orphaned tool_result after compaction"
    # The pinned current user is still last.
    assert request.messages[-1] == messages[-1]


def test_history_gets_the_remainder(tmp_path):
    config = v2_config(max_tokens=5000, safety=0)
    (tmp_path / "SOUL.md").write_text("S" * 10, encoding="utf-8")
    manager = ContextManager(tmp_path, config=config, counter=char_counter())
    messages = _conversation(4)
    manager.assemble(FakeSession(*messages))
    budget = manager.last_budget
    used = sum(
        part["granted"]
        for part in budget["parts"]
        if part["priority"] < 3
    )
    # history gets the remainder after required and lower-priority parts.
    assert used <= budget["input_budget"]
    assert budget["history_budget"] == budget["input_budget"] - used
    assert budget["history_budget"] > 0


# ---------------------------------------------------------------------------
# Proactive compaction threshold (compact_at_fraction)
# ---------------------------------------------------------------------------


def _fraction_config(fraction=0.5, max_tokens=1000):
    return Config(
        model="x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="x"),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=0,
                compaction="drop_oldest",
                compact_at_fraction=fraction,
            ),
        ),
    )


def _small_history(count, size=100):
    messages = [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=[Text(text="h" * size)],
        )
        for index in range(count)
    ]
    messages.append(Message(role="user", content=[Text(text="u")]))
    return messages


def _full_history_allowance(budget):
    optional = sum(
        part["granted"] for part in budget["parts"] if part["priority"] > 0
    )
    return budget["input_budget"] - budget["required_tokens"] - optional


def test_below_fraction_threshold_does_not_compact(tmp_path):
    manager = ContextManager(
        tmp_path,
        config=_fraction_config(),
        identity="I",
        counter=char_counter(),
    )
    manager.assemble(FakeSession(*_small_history(2)))
    budget = manager.last_budget
    assert budget["history_budget"] == _full_history_allowance(budget)
    assert manager.last_compaction["dropped"] == 0


def test_above_fraction_threshold_reduces_history_budget(tmp_path):
    manager = ContextManager(
        tmp_path,
        config=_fraction_config(),
        identity="I",
        counter=char_counter(),
    )
    manager.assemble(FakeSession(*_small_history(6)))
    budget = manager.last_budget
    optional = sum(
        part["granted"] for part in budget["parts"] if part["priority"] > 0
    )
    target = (
        int(budget["input_budget"] * 0.5)
        - budget["required_tokens"]
        - optional
    )
    assert budget["history_budget"] == target
    assert budget["history_budget"] < _full_history_allowance(budget)
    assert manager.last_compaction["dropped"] > 0


def test_fraction_threshold_is_deterministic(tmp_path):
    manager = ContextManager(
        tmp_path,
        config=_fraction_config(),
        identity="I",
        counter=char_counter(),
    )
    messages = _small_history(6)
    manager.assemble(FakeSession(*messages))
    first = manager.last_budget
    manager.assemble(FakeSession(*messages))
    second = manager.last_budget
    assert first == second


def test_fraction_one_never_reduces_below_hard_budget(tmp_path):
    manager = ContextManager(
        tmp_path,
        config=_fraction_config(fraction=1.0),
        identity="I",
        counter=char_counter(),
    )
    manager.assemble(FakeSession(*_small_history(6)))
    budget = manager.last_budget
    assert budget["history_budget"] == _full_history_allowance(budget)


def test_invalid_compaction_fraction_is_rejected():
    with pytest.raises(ValueError):
        ContextSection(compact_at_fraction=0)
    with pytest.raises(ValueError):
        ContextSection(compact_at_fraction=1.5)
    with pytest.raises(ValueError):
        ContextSection(compact_at_fraction=float("nan"))


# ---------------------------------------------------------------------------
# Hybrid eviction without dropping (review blocker 1)
# ---------------------------------------------------------------------------


def _tool_exchange(index, body_len=3000):
    call_id = f"c{index}"
    note = f"[result {index} evicted; re-run to see it]"
    return [
        Message(
            role="assistant",
            content=[ToolUse(id=call_id, name="Read", input={"path": "a"})],
        ),
        Message(
            role="user",
            content=[
                ToolResult(
                    tool_use_id=call_id,
                    content=[Text(text="R" * body_len)],
                    context_note=note,
                )
            ],
        ),
    ]


def test_hybrid_no_drop_returns_evicted_messages_and_fits(tmp_path):
    from nexus.session.manager import SessionManager

    config = v2_config(max_tokens=1000, safety=0)  # default compaction: hybrid
    context = ContextManager(
        tmp_path,
        config=config,
        counter=char_counter(),
        identity="I",
        keep_recent=0,
    )
    manager = SessionManager(tmp_path, assemble=context)
    session = manager.open("evict")
    for index in range(3):
        for message in _tool_exchange(index):
            session.append_message(message)
    session.append_message(Message(role="user", content=[Text(text="current")]))

    request = context.for_turn().assemble(session)
    ctx = request.metadata["context"]

    assert ctx["history_dropped"] == 0
    assert ctx["evicted"] > 0
    assert ctx["used_tokens"] <= ctx["input_budget"]
    assert ctx["strategy"] == "hybrid"

    # The sent request carries the evicted (note) content, not the 3000-char body.
    sent_results = [
        block
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResult)
    ]
    assert sent_results
    assert all(block.content[0].text.startswith("[result") for block in sent_results)

    # The authoritative log is unchanged and still holds the original bodies.
    log_results = [
        block
        for message in session.messages
        for block in message.content
        if isinstance(block, ToolResult)
    ]
    assert any(len(block.content[0].text) == 3000 for block in log_results)


def test_hybrid_no_drop_preserves_keep_recent(tmp_path):
    from nexus.session.manager import SessionManager

    config = v2_config(max_tokens=2000, safety=0)
    context = ContextManager(
        tmp_path,
        config=config,
        counter=char_counter(),
        identity="I",
        keep_recent=2,
    )
    manager = SessionManager(tmp_path, assemble=context)
    session = manager.open("keeprecent")
    for index in range(4):
        for message in _tool_exchange(index, body_len=300):
            session.append_message(message)
    session.append_message(Message(role="user", content=[Text(text="current")]))

    request = context.for_turn().assemble(session)
    ctx = request.metadata["context"]
    assert ctx["history_dropped"] == 0
    assert ctx["evicted"] == 3  # the last tool result is inside keep_recent
    sent_results = [
        block
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResult)
    ]
    # Exactly one retained result keeps its original body.
    assert any(len(block.content[0].text) == 300 for block in sent_results)


# ---------------------------------------------------------------------------
# End-to-end: a 200-message scripted session under a constrained context
# ---------------------------------------------------------------------------


def test_200_message_scripted_session_completes_with_suffix_history(tmp_path):
    import asyncio

    from nexus.core.loop import ResolvedModel
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.session.manager import SessionManager

    config = v2_config(max_tokens=1200, safety=0)
    context = ContextManager(tmp_path, config=config, counter=char_counter())
    provider = ScriptedProvider(*[text_response("ok") for _ in range(100)])

    class Resolver:
        def resolve(self, request):
            return ResolvedModel(provider, "m", provider.capabilities("m"))

    manager = SessionManager(tmp_path, assemble=context, provider_for=Resolver())
    session = manager.open("long")

    async def run():
        for index in range(100):
            events = [event async for event in session.send(f"turn {index}")]
            assert events[-1].type == "turn.completed"

    asyncio.run(run())

    # Full original history remains authoritative: 100 user + 100 assistant.
    assert len(session.messages) == 200
    assert session.path.read_bytes().count(b'"type":"message"') == 200
    # The last assembled request is a contiguous suffix of the history that
    # existed at assembly time (before the final assistant reply was appended),
    # and the current user turn appears exactly once.
    final = provider.requests[-1].messages
    history_at_assembly = session.messages[:-1]
    assert final[-1].role == "user"
    assert final[-1].content[0].text == "turn 99"
    assert final == history_at_assembly[len(history_at_assembly) - len(final) :]
    assert [m for m in final if m.content[0].text == "turn 99"] == [final[-1]]


def test_catalogue_input_limit_caps_the_budget_but_not_the_window():
    from nexus.model.registry import _parse_model

    model = _parse_model("gpt-6-luna", {"id": "gpt-6-luna", "limit": {"context": 1_050_000, "input": 922_000, "output": 128_000}})
    assert (model.context, model.max_input, model.max_output) == (1_050_000, 922_000, 128_000)
    inputs = BudgetInputs(2_000_000, 1_050_000, None, 4_096, 128_000, 4_000, caps_max_input_tokens=922_000)
    assert inputs.input_budget == 918_000
    assert inputs.context_window == 1_050_000
    # Without a separate input limit, the window minus the output reserve applies.
    assert BudgetInputs(2_000_000, 400_000, None, 4_096, 128_000, 4_000).input_budget == 391_904
    # A smaller configured context.max_tokens wins over the catalogue.
    assert BudgetInputs(16_000, 1_050_000, None, 4_096, 128_000, 4_000, caps_max_input_tokens=922_000).context_window == 16_000
