import dataclasses

import pytest

from nexus.llmhub.anthropic import AnthropicProvider, DirectRequest, TokenTaskBudget
from nexus.llmhub.types import Message, ProviderError, Reasoning, Request


class Auth:
    async def headers(self):
        return {}


def request(budget, **kwargs):
    return DirectRequest("claude-sonnet-4-6", "", (), task_budget=budget, **kwargs)


@pytest.mark.parametrize("remaining", [None, 0, 100])
def test_task_budget_is_native_and_immutable(remaining):
    budget = TokenTaskBudget(100, remaining)
    result = AnthropicProvider(Auth()).body(request(budget))
    assert result["output_config"] == {"task_budget": {
        "type": "tokens", "total": 100, **({"remaining": remaining} if remaining is not None else {})}}
    assert budget.total == 100 and budget.remaining == remaining
    with pytest.raises(dataclasses.FrozenInstanceError):
        budget.total = 200


def test_budget_merges_with_effort_without_changing_shared_request():
    req = DirectRequest("claude-sonnet-4-6", "", (), reasoning_effort="low", task_budget=TokenTaskBudget(1000))
    body = AnthropicProvider(Auth()).body(req)
    assert body["output_config"] == {"effort": "low", "task_budget": {"type": "tokens", "total": 1000}}
    assert "task_budget" not in {f.name for f in dataclasses.fields(Request)}


@pytest.mark.parametrize("total,remaining", [(0, None), (-1, None), (True, None), (1.5, None),
    (100, -1), (100, 101), (100, True), (100, 1.5)])
def test_invalid_budgets_fail_before_send(total, remaining):
    with pytest.raises(ProviderError, match="task budget"):
        AnthropicProvider(Auth()).body(request(TokenTaskBudget(total, remaining)))


def test_invalid_budget_type_is_not_coerced():
    with pytest.raises(ProviderError, match="must be a TokenTaskBudget"):
        AnthropicProvider(Auth()).body(request({"total": 100}))


def test_signed_replay_does_not_silently_strip_remaining():
    reasoning = Reasoning("thought", "claude-direct", {"type": "thinking", "thinking": "thought", "signature": "signed"})
    messages = (Message("assistant", (reasoning,)),)
    provider = AnthropicProvider(Auth())
    with pytest.raises(ProviderError, match="incompatible with signed thinking replay"):
        provider.body(DirectRequest("claude-sonnet-4-6", "", messages, task_budget=TokenTaskBudget(100, 50)))
    assert provider.body(DirectRequest("claude-sonnet-4-6", "", messages,
        task_budget=TokenTaskBudget(100)))["output_config"]["task_budget"] == {"type": "tokens", "total": 100}
    assert reasoning.raw["signature"] == "signed"


@pytest.mark.asyncio
@pytest.mark.parametrize("effort,expected", [(None, False), ("low", True)])
async def test_budget_beta_is_body_derived_and_effort_beta_is_separate(effort, expected):
    provider = AnthropicProvider(Auth())
    req = DirectRequest("claude-sonnet-4-6", "", (), reasoning_effort=effort, task_budget=TokenTaskBudget(100))
    stream = provider.stream(req)
    wire = await anext(stream)
    await stream.aclose()
    assert "task-budgets-2026-03-13" in wire.headers["anthropic-beta"]
    assert ("effort-2025-11-24" in wire.headers["anthropic-beta"]) == expected


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-opus-4-5", "unknown", "claude-opus-6"])
@pytest.mark.asyncio
async def test_nonadaptive_task_budget_fails_before_authentication(model):
    class ForbiddenAuth:
        async def headers(self):
            raise AssertionError("must not send unsupported budgets")
    with pytest.raises(ProviderError, match="restricted to known adaptive-thinking models"):
        _ = [e async for e in AnthropicProvider(ForbiddenAuth()).stream(
            DirectRequest(model, "", (), task_budget=TokenTaskBudget(1000)))]
