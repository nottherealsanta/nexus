import pytest

from nexus.llmhub.anthropic import AnthropicProvider, DirectRequest
from nexus.llmhub.types import ProviderError, ToolSpec


class Auth:
    async def headers(self):
        raise AssertionError("body validation must happen before authentication")


TOOL = ToolSpec("probe", "", {"type": "object", "additionalProperties": False})


@pytest.mark.parametrize("choice", ["auto", "any", "none"])
def test_explicit_tool_choice(choice):
    req = DirectRequest("claude-haiku-4-5", "", (), (TOOL,), tool_choice=choice)
    assert AnthropicProvider(Auth()).body(req)["tool_choice"] == {"type": choice}


def test_forced_tool_is_native_and_default_is_unchanged():
    provider = AnthropicProvider(Auth())
    req = DirectRequest("claude-haiku-4-5", "", (), (TOOL,), forced_tool="probe")
    assert provider.body(req)["tool_choice"] == {"type": "tool", "name": "probe"}
    assert "tool_choice" not in provider.body(DirectRequest("claude-haiku-4-5", "", (), (TOOL,)))


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,match", [
    ({"tool_choice": "invalid"}, "unsupported tool_choice"),
    ({"tool_choice": "tool"}, "must name an offered tool"),
    ({"forced_tool": "missing"}, "must name an offered tool"),
    ({"forced_tool": "probe", "tool_choice": "none"}, "conflicts"),
    ({"forced_tool": "probe", "reasoning_effort": "low"}, "incompatible with thinking"),
    ({"tool_choice": "any", "prewarm": True}, "incompatible with forced tool choice"),
])
async def test_invalid_or_incompatible_tool_choice_fails_before_credentials(kwargs, match):
    req = DirectRequest("claude-haiku-4-5", "", (), (TOOL,), **kwargs)
    with pytest.raises(ProviderError, match=match):
        _ = [e async for e in AnthropicProvider(Auth()).stream(req)]


@pytest.mark.parametrize("model,effort", [("claude-sonnet-4-6", "low"), ("claude-sonnet-5-5", "off")])
def test_forced_choice_does_not_silently_disable_thinking(model, effort):
    req = DirectRequest(model, "", (), (TOOL,), reasoning_effort=effort, tool_choice="any")
    with pytest.raises(ProviderError, match="incompatible with thinking"):
        AnthropicProvider(Auth()).body(req)


@pytest.mark.parametrize("model", ["claude-fable-5", "claude-mythos-5-1"])
def test_unsupported_model_does_not_silently_downgrade_choice(model):
    with pytest.raises(ProviderError, match="does not support forced tool choice"):
        AnthropicProvider(Auth()).body(DirectRequest(model, "", (), (TOOL,), tool_choice="any"))


def test_forced_choice_requires_tools():
    with pytest.raises(ProviderError, match="requires tools"):
        AnthropicProvider(Auth()).body(DirectRequest("claude-haiku-4-5", "", (), tool_choice="any"))
