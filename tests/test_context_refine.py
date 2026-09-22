"""Phase 3 exact-count refinement and assembly accounting metadata (exit criterion 4)."""
from __future__ import annotations

import asyncio

from nexus.config import Config
from nexus.config.schema import ConfigV2, ContextSection, ModelSection
from nexus.context import ContextManager
from nexus.model.message import Message, Text


class RequestCounter:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def count_request(self, request):
        self.calls += 1
        return self.value


class Session:
    id = "s"

    def __init__(self, messages):
        self._messages = messages

    @property
    def messages(self):
        return list(self._messages)


def _config(max_tokens=300, compaction="drop_oldest"):
    return Config(
        model="scripted/x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/x"),
            context=ContextSection(
                max_tokens=max_tokens,
                safety_margin_tokens=0,
                compaction=compaction,
            ),
        ),
    )


def _history(count=20):
    messages = [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=[Text(text=f"m{index} " * 10)],
        )
        for index in range(count)
    ]
    messages.append(Message(role="user", content=[Text(text="current")]))
    return messages


def test_provider_overflow_triggers_one_deterministic_compaction_pass(tmp_path):
    counter = RequestCounter(10_000)
    manager = ContextManager(
        tmp_path, config=_config(), counter=len, request_counter=counter
    )
    request = asyncio.run(manager.assemble(Session(_history())))

    context = request.metadata["context"]
    assert context["actual_tokens"] == 10_000
    assert context["refined"] is True
    # The refinement shrank history further after the first pass.
    assert context["history_budget"] == 0
    assert request.messages[-1].content[0].text == "current"
    # The provider count is used at most once per assembly.
    assert counter.calls == 1


def test_small_drift_does_not_refine_or_fail(tmp_path):
    # Actual is within budget (250 <= 300): no second pass, no failure.
    counter = RequestCounter(250)
    manager = ContextManager(
        tmp_path, config=_config(), counter=len, request_counter=counter
    )
    request = asyncio.run(manager.assemble(Session(_history())))
    context = request.metadata["context"]
    assert context["actual_tokens"] == 250
    assert context.get("refined") is None
    assert request.messages[-1].content[0].text == "current"


def test_counting_failure_does_not_fail_assembly(tmp_path):
    class Boom:
        async def count_request(self, request):
            raise RuntimeError("count endpoint down")

    manager = ContextManager(
        tmp_path, config=_config(), counter=len, request_counter=Boom()
    )
    request = asyncio.run(manager.assemble(Session(_history())))
    assert request.messages[-1].content[0].text == "current"


def test_assembled_metadata_has_budget_and_cache_accounting(tmp_path):
    manager = ContextManager(tmp_path, config=_config(), counter=len)
    request = manager.assemble(Session(_history()))
    context = request.metadata["context"]
    assert {
        "input_budget",
        "effective_max_output_tokens",
        "safety_margin_tokens",
        "history_budget",
        "required_tokens",
        "history_retained",
        "strategy",
    }.issubset(context)
    assert "cache" in request.metadata
    # No content leaks into accounting metadata.
    assert "current" not in str(context)
