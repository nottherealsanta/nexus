"""Phase 5.5 capability-degradation retry tests (plan section 15.5).

The loop retries once when a provider rejects a capability the registry claimed.
That retry is only meaningful if the request actually changes: emitting
``context.degraded`` and resending the same body just reproduces the rejection.
These tests pin one concrete transform per claimed feature, plus the two ways a
retry must *not* happen (after output streamed, or on a second rejection).

Everything is offline and deterministic: the transforms are unit-tested through
``_request_without_feature`` and one end-to-end case drives ``run_turn`` with a
scripted provider.
"""
from __future__ import annotations

import pytest

from nexus.core.cancel import CancelToken
from nexus.core.loop import ResolvedModel, _request_without_feature, run_turn
from nexus.core.turn import TurnState
from nexus.errors import ProviderError
from nexus.events import Event
from nexus.model.capabilities import Capabilities, CapabilityRejected
from nexus.model.message import (
    Document,
    Image,
    Message,
    Text,
    Thinking,
    ToolResult,
)
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema
from nexus.model.stream import TextDelta

IMAGE = Image(media_type="image/png", url="https://example.test/x.png")
TOOL = ToolSchema(
    name="Read",
    description="read",
    input_schema={"type": "object", "properties": {}},
)


def request(
    *,
    content=None,
    tools=(),
    params=None,
    metadata=None,
) -> ModelRequest:
    return ModelRequest(
        messages=[Message(role="user", content=list(content or [Text(text="hi")]))],
        tools=list(tools),
        params=params or SamplingParams(),
        metadata=dict(metadata or {}),
    )


def caps(feature: str, policy: str | None = None) -> Capabilities:
    degradation = {} if policy is None else {feature: policy}
    return Capabilities(**{feature: True}, degradation=degradation)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# One transform per claimed feature
# ---------------------------------------------------------------------------


def test_tools_feature_strips_schemas():
    original = request(tools=[TOOL])
    adapted = _request_without_feature(original, "tools", caps("tools"))
    assert adapted.tools == []
    # The original is untouched (frozen structs, no shared mutation).
    assert original.tools == [TOOL]


def test_thinking_feature_clears_budget_and_drops_blocks_by_policy():
    original = request(
        content=[Thinking(text="why", signature="sig"), Text(text="answer")],
        params=SamplingParams(thinking_budget=4096),
        metadata={"context": {"used_tokens": 1}},
    )
    adapted = _request_without_feature(
        original, "thinking", caps("thinking", "drop")
    )
    assert adapted.params.thinking_budget is None
    assert [type(block) for block in adapted.messages[0].content] == [Text]
    assert adapted.metadata == {"context": {"used_tokens": 1}}


def test_thinking_feature_to_text_preserves_the_text():
    original = request(
        content=[Thinking(text="why", signature="sig")],
        params=SamplingParams(thinking_budget=4096),
    )
    adapted = _request_without_feature(
        original, "thinking", caps("thinking", "to_text")
    )
    assert adapted.params.thinking_budget is None
    (block,) = adapted.messages[0].content
    assert isinstance(block, Text)
    assert block.text == "why"


def test_thinking_feature_error_policy_raises():
    original = request(
        content=[Thinking(text="why", signature="sig")],
        params=SamplingParams(thinking_budget=4096),
    )
    with pytest.raises(ProviderError, match="thinking"):
        _request_without_feature(original, "thinking", caps("thinking", "error"))


def test_thinking_feature_noop_when_unused():
    original = request(content=[Text(text="hi")])
    assert _request_without_feature(original, "thinking", caps("thinking")) is original


def test_prompt_caching_feature_removes_cache_metadata():
    original = request(
        metadata={
            "context": {"used_tokens": 1},
            "cache": {"enabled": True, "boundaries": [{"position": 0}]},
        }
    )
    adapted = _request_without_feature(
        original, "prompt_caching", caps("prompt_caching")
    )
    assert "cache" not in adapted.metadata
    assert adapted.metadata["context"] == {"used_tokens": 1}


def test_vision_feature_drops_images_by_policy():
    original = request(content=[Text(text="look"), IMAGE])
    adapted = _request_without_feature(original, "vision", caps("vision", "drop"))
    assert all(not isinstance(block, Image) for block in adapted.messages[0].content)


def test_vision_feature_to_text_replaces_the_image():
    original = request(content=[Text(text="look"), IMAGE])
    adapted = _request_without_feature(
        original, "vision", caps("vision", "to_text")
    )
    blocks = adapted.messages[0].content
    assert len(blocks) == 2
    assert all(not isinstance(block, Image) for block in blocks)
    assert any(isinstance(block, Text) and "image omitted" in block.text for block in blocks)


def test_vision_feature_degrades_images_inside_tool_results():
    original = request(
        content=[
            ToolResult(
                tool_use_id="t1",
                content=[Text(text="screenshot"), IMAGE],
            )
        ]
    )
    adapted = _request_without_feature(
        original, "vision", caps("vision", "to_text")
    )
    (block,) = adapted.messages[0].content
    assert isinstance(block, ToolResult)
    assert all(not isinstance(item, Image) for item in block.content)
    assert isinstance(block.content[-1], Text)


def test_documents_feature_drops_by_policy():
    document = Document(media_type="application/pdf", data=b"%PDF", title="spec")
    original = request(content=[document])
    adapted = _request_without_feature(
        original, "documents", caps("documents", "drop")
    )
    assert adapted.messages[0].content == []


def test_documents_feature_to_text_names_the_document():
    document = Document(media_type="application/pdf", data=b"%PDF", title="spec")
    original = request(content=[document])
    adapted = _request_without_feature(
        original, "documents", caps("documents", "to_text")
    )
    (block,) = adapted.messages[0].content
    assert isinstance(block, Text)
    assert "spec" in block.text


def test_json_schema_strict_feature_removes_request_metadata():
    original = request(
        metadata={
            "response_format": {"type": "json_schema"},
            "json_schema_strict": True,
            "context": {"used_tokens": 1},
        }
    )
    adapted = _request_without_feature(
        original, "json_schema_strict", caps("json_schema_strict")
    )
    assert adapted.metadata == {"context": {"used_tokens": 1}}


def test_parallel_tool_calls_feature_removes_request_metadata():
    original = request(metadata={"parallel_tool_calls": True, "context": {}})
    adapted = _request_without_feature(
        original, "parallel_tool_calls", caps("parallel_tool_calls")
    )
    assert adapted.metadata == {"context": {}}


def test_unknown_feature_leaves_the_request_unchanged():
    original = request(tools=[TOOL], metadata={"cache": {"enabled": True}})
    assert _request_without_feature(original, "not-a-feature", caps("tools")) is original


# ---------------------------------------------------------------------------
# End-to-end: the second call really is different, and only one retry
# ---------------------------------------------------------------------------


class _Assembler:
    def assemble(self, session) -> ModelRequest:
        return ModelRequest(
            messages=[Message(role="user", content=[Text(text="look"), IMAGE])],
            tools=[TOOL],
        )


class _Session:
    id = "s1"

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._lease = _Lease()

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def append_message(self, message, *, seq=None):
        self._messages.append(message)
        return message

    def begin_turn(self, *, turn_id=None, limits=None):
        return self._lease


class _Lease:
    def __init__(self) -> None:
        self.turn_id = "turn-1"
        self.state = TurnState.new(turn_id=self.turn_id, session_id="s1").start()
        self.cancel_token = CancelToken()
        self.limits = None

    def release(self) -> None:
        return None


class _Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> Event:
        self.events.append(event)
        return event


def _resolver(provider: ScriptedProvider):
    def resolve(request: ModelRequest) -> ResolvedModel:
        return ResolvedModel(provider, "m", provider.capabilities("m"))

    return resolve


async def test_vision_rejection_retries_with_the_image_removed():
    provider = ScriptedProvider(
        [CapabilityRejected("vision")],
        text_response("recovered"),
        name="scripted",
        capabilities=Capabilities(tools=True, vision=True, streaming=True),
    )
    session = _Session()
    sink = _Sink()
    outcome = await run_turn(
        session=session,
        user_input="hi",
        assemble=_Assembler(),
        provider_for=_resolver(provider),
        emit=sink,
        lease=session.begin_turn(),
    )
    assert outcome.phase == "completed"
    assert provider.calls == 2
    first, second = provider.requests
    assert any(isinstance(block, Image) for block in first.messages[0].content)
    assert all(
        not isinstance(block, Image) for block in second.messages[0].content
    )
    types = [event.type for event in sink.events]
    assert types.count("context.degraded") == 1
    assert types.count("registry.mismatch") == 1


async def test_provider_error_detail_is_redacted_in_events():
    provider = ScriptedProvider(
        [
            CapabilityRejected(
                "vision",
                "GET https://alice:supersecret@models.example/v1 failed",
            )
        ],
        text_response("ok"),
        name="scripted",
        capabilities=Capabilities(tools=True, vision=True, streaming=True),
    )
    session = _Session()
    sink = _Sink()
    await run_turn(
        session=session,
        user_input="hi",
        assemble=_Assembler(),
        provider_for=_resolver(provider),
        emit=sink,
        lease=session.begin_turn(),
    )
    detail = " ".join(
        str(event.data.get("detail", ""))
        for event in sink.events
        if event.type in ("context.degraded", "registry.mismatch")
    )
    assert "supersecret" not in detail
    assert "models.example" in detail


async def test_mid_stream_output_never_retries():
    provider = ScriptedProvider(
        [TextDelta(text="partial"), CapabilityRejected("vision")],
        name="scripted",
        capabilities=Capabilities(tools=True, vision=True, streaming=True),
    )
    session = _Session()
    sink = _Sink()
    outcome = await run_turn(
        session=session,
        user_input="hi",
        assemble=_Assembler(),
        provider_for=_resolver(provider),
        emit=sink,
        lease=session.begin_turn(),
    )
    assert outcome.phase == "failed"
    assert provider.calls == 1
    assert "context.degraded" not in [event.type for event in sink.events]
