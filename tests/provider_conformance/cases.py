"""The adapter-neutral conformance case catalogue.

Each case is a logical scenario: a request, a plan of wire steps (normalized
events and/or transport faults), and an expectation expressed in normalized
terms. It names no adapter.

Two axes keep the catalogue honest across wire dialects:

* ``requires`` names *capabilities* (stream, tools, vision, count_tokens, ...).
  An adapter lacking one is skipped.
* ``dialects`` names *wire shapes*. ``None`` means the case is normalized and
  runs on every adapter; a set gates it to those dialects so an Anthropic frame
  never has to satisfy Gemini's body shape (or vice versa).

``expect`` holds the shared, dialect-neutral assertions. ``overlays`` adds
dialect-specific assertions on top (for example the exact request body or the
usage fields a dialect can carry). Both are checked, so a case can assert
"everyone gets a tool call" and "Gemini drops the cache-write field" in one
place.

Scenarios covered (plan section 9): text, single and parallel tool calls,
streamed partial JSON, malformed arguments, thinking with signature replay,
refusal, 429-then-success, mid-stream disconnect, usage accounting, request
schema translation, multimodal input, count_tokens, and cross-provider
degradation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nexus.errors import MalformedToolCall, ProviderError
from nexus.model.message import (
    Document,
    Image,
    Message,
    MessageMeta,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.gemini import SYNTHETIC_CALL_PREFIX
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    ToolCallAccumulator,
    ToolCallDelta,
    ToolCallStart,
    Usage,
)

from .contract import (
    ANTHROPIC_DIALECT,
    DEGRADATION,
    DOCUMENTS,
    GEMINI_DIALECT,
    HTTP,
    IR_DIALECT,
    OPENAI_CHAT_DIALECT,
    OPENAI_RESPONSES_DIALECT,
    STREAM,
    THINKING,
    TOOLS,
    VISION,
    WIRE_REQUEST,
    EventsStep,
    FaultStep,
    StatusStep,
    WireStep,
)
from .events import (
    reply_parallel_tools,
    reply_text,
    reply_thinking,
    reply_tool,
)
from .fixtures import load_anthropic, load_gemini, load_openai

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .harness import CaseResult

__all__ = ["CASES", "ConformanceCase", "Expectation"]

_PARTIAL_JSON_DIALECTS = frozenset(
    {
        ANTHROPIC_DIALECT,
        OPENAI_RESPONSES_DIALECT,
        OPENAI_CHAT_DIALECT,
        IR_DIALECT,
    }
)


@dataclass(frozen=True)
class Expectation:
    """What the collected stream must look like, in normalized terms."""

    text: str | None = None
    text_chunks: tuple[str, ...] | None = None
    thinking: tuple[str, ...] | None = None
    signatures: tuple[str, ...] | None = None
    tool_calls: tuple[tuple[str, str, dict[str, Any]], ...] | None = None
    #: Tool calls by ``(name, input)``, ignoring the id. Use when the dialect is
    #: allowed to mint or rewrite ids; a separate check pins the id's shape.
    tool_calls_loose: tuple[tuple[str, dict[str, Any]], ...] | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    error: type[BaseException] | None = None
    error_match: str | None = None
    requests: int | None = None
    retry_delays: tuple[float, ...] | None = None
    request_contains: dict[str, Any] | None = None
    request_excludes: tuple[str, ...] = ()
    saw_message_start: bool | None = None


@dataclass(frozen=True)
class ConformanceCase:
    """One logical provider scenario."""

    id: str
    requires: frozenset[str]
    request: Callable[[], ModelRequest]
    wire: tuple[WireStep, ...] = ()
    expect: Expectation | None = None
    overlays: Mapping[str, Expectation] = field(default_factory=dict)
    dialects: frozenset[str] | None = None
    check: Callable[[CaseResult], None] | None = None
    description: str = ""

    def applies_to_dialect(self, dialect: str) -> bool:
        return self.dialects is None or dialect in self.dialects


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------

READ_TOOL = ToolSchema(
    name="Read",
    description="reads files",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)

IMAGE = Image(media_type="image/png", data=b"png-bytes")
DOCUMENT = Document(media_type="application/pdf", data=b"%PDF-1.4", title="spec")

#: A usage record that is richer than any single dialect: Anthropic carries
#: cache-write but not reasoning, Gemini carries reasoning but not cache-write,
#: and the in-process IR carries both. The per-dialect overlays pin the loss.
DIALECT_USAGE = Usage(input=12, output=7, cache_read=3, cache_write=1, reasoning=2)

#: A synthetic Gemini tool id, built from the adapter's own prefix so this case
#: tracks the adapter's scheme rather than hardcoding it.
SYNTHETIC_READ_ID = f"{SYNTHETIC_CALL_PREFIX}Read_0"


def _request(
    messages: list[Message] | None = None,
    *,
    system: str | None = None,
    tools: tuple[ToolSchema, ...] = (),
    params: SamplingParams | None = None,
    metadata: dict[str, Any] | None = None,
) -> ModelRequest:
    return ModelRequest(
        messages=messages
        if messages is not None
        else [Message(role="user", content=[Text(text="hi")])],
        system=system,
        tools=list(tools),
        params=params or SamplingParams(),
        metadata=dict(metadata or {}),
    )


def _user(text: str = "hi") -> Message:
    return Message(role="user", content=[Text(text=text)])


def _assistant(*blocks: Any, provider: str | None = None) -> Message:
    return Message(
        role="assistant",
        content=list(blocks),
        meta=MessageMeta(provider=provider),
    )


# ---------------------------------------------------------------------------
# Custom checks
# ---------------------------------------------------------------------------


def check_malformed_tool_arguments(result: CaseResult) -> None:
    """Reject malformed streamed arguments, whichever way the adapter surfaces it.

    A wire adapter raises :class:`MalformedToolCall` while finalizing the block.
    An in-process provider yields the raw start/delta events, so the shared
    :class:`ToolCallAccumulator` (the real contract) is applied here.
    """
    if result.error is not None:
        assert isinstance(result.error, MalformedToolCall), result.error
        assert result.tool_calls() == []
        return
    accumulator = ToolCallAccumulator()
    try:
        for event in result.events:
            accumulator.handle(event)
        # A provider that streams raw deltas may end without a tool_call_end; the
        # loop finalizes the dangling call, which is where malformed JSON raises.
        for call_id in list(accumulator.pending):
            accumulator.finish(call_id)
    except MalformedToolCall:
        return
    raise AssertionError(
        "malformed tool arguments were neither raised nor rejected by the accumulator"
    )


def check_degradation_policy(result: CaseResult) -> None:
    caps = result.adapter.provider.capabilities(result.adapter.model)
    assert caps.degradation.get("thinking") in {"drop", "to_text", "error"}, (
        f"adapter declares no usable thinking degradation policy: {caps.degradation!r}"
    )


def check_minted_tool_ids(result: CaseResult) -> None:
    """Every surfaced tool id is adapter-minted, non-empty, and unique.

    The provider supplied no id in the fixture, so the adapter must mint one.
    The prefix is the adapter's documented ``SYNTHETIC_CALL_PREFIX``; the exact
    token is deliberately not pinned (it may embed a uuid).
    """
    calls = result.tool_calls()
    assert calls, "expected at least one tool call"
    ids = [call_id for call_id, _name, _input in calls]
    assert all(ids), ids
    assert len(set(ids)) == len(ids), ids
    for call_id in ids:
        assert call_id.startswith(SYNTHETIC_CALL_PREFIX), call_id


# ---------------------------------------------------------------------------
# Shared, normalized cases (dialects=None): the semantic contract
# ---------------------------------------------------------------------------

_SHARED: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        id="text_only",
        requires=frozenset({STREAM}),
        request=_request,
        wire=(EventsStep(reply_text("Hello, world.")),),
        expect=Expectation(
            text="Hello, world.",
            stop_reason="end_turn",
            requests=1,
            saw_message_start=True,
        ),
        description="text-only response round-trips",
    ),
    ConformanceCase(
        id="usage_accounting",
        requires=frozenset({STREAM}),
        request=_request,
        wire=(EventsStep(reply_text("Hel", "lo", usage=DIALECT_USAGE)),),
        expect=Expectation(text="Hello", stop_reason="end_turn", requests=1),
        overlays={
            ANTHROPIC_DIALECT: Expectation(usage=Usage(12, 7, 3, 1, 0)),
            GEMINI_DIALECT: Expectation(usage=Usage(12, 7, 3, 0, 2)),
            # OpenAI reports cache reads and reasoning tokens but has no
            # cache-write field on either dialect.
            OPENAI_RESPONSES_DIALECT: Expectation(usage=Usage(12, 7, 3, 0, 2)),
            OPENAI_CHAT_DIALECT: Expectation(usage=Usage(12, 7, 3, 0, 2)),
            IR_DIALECT: Expectation(usage=Usage(12, 7, 3, 1, 2)),
        },
        description="usage is combined per dialect; each drops what it cannot carry",
    ),
    ConformanceCase(
        id="partial_json_single_tool",
        requires=frozenset({STREAM, TOOLS}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(
            EventsStep(
                reply_tool(
                    "toolu_1",
                    "Read",
                    input={"path": "a.txt"},
                    arg_chunks=('{"path"', ': "a.txt"}'),
                )
            ),
        ),
        expect=Expectation(
            text="",
            tool_calls=(("toolu_1", "Read", {"path": "a.txt"}),),
            stop_reason="tool_use",
            requests=1,
        ),
        description="streamed partial-JSON tool arguments are accumulated",
    ),
    ConformanceCase(
        id="parallel_tool_calls",
        requires=frozenset({STREAM, TOOLS}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(
            EventsStep(
                reply_parallel_tools(
                    ("toolu_a", "Read", {"path": "a"}),
                    ("toolu_b", "Read", {"path": "b"}),
                )
            ),
        ),
        expect=Expectation(
            tool_calls=(
                ("toolu_a", "Read", {"path": "a"}),
                ("toolu_b", "Read", {"path": "b"}),
            ),
            stop_reason="tool_use",
            requests=1,
        ),
        description="two tool calls in one response are both surfaced",
    ),
    ConformanceCase(
        id="thinking_signature_then_text",
        requires=frozenset({STREAM, THINKING}),
        request=_request,
        wire=(
            EventsStep(
                reply_thinking(
                    "Let me think.", " Carefully.", signature="sig-abc", text="Answer"
                )
            ),
        ),
        expect=Expectation(
            thinking=("Let me think.", " Carefully."),
            signatures=("sig-abc",),
            text="Answer",
            stop_reason="end_turn",
            requests=1,
        ),
        description="thinking deltas and an opaque signature round-trip",
    ),
    ConformanceCase(
        id="refusal",
        requires=frozenset({STREAM}),
        request=_request,
        wire=(
            EventsStep(reply_text("I can't help with that.", stop_reason="refusal")),
        ),
        expect=Expectation(
            text="I can't help with that.",
            stop_reason="refusal",
            requests=1,
        ),
        description="a refusal completes the turn with stop_reason=refusal",
    ),
    ConformanceCase(
        id="max_tokens_stop",
        requires=frozenset({STREAM}),
        request=_request,
        wire=(EventsStep(reply_text("truncated…", stop_reason="max_tokens")),),
        expect=Expectation(stop_reason="max_tokens", requests=1),
        description="normalized max_tokens stop reason",
    ),
    ConformanceCase(
        id="malformed_tool_arguments",
        requires=frozenset({STREAM, TOOLS}),
        dialects=_PARTIAL_JSON_DIALECTS,
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(
            EventsStep(
                (
                    MessageStart(),
                    ToolCallStart(id="bad", name="Read"),
                    ToolCallDelta(id="bad", partial_json="{not json"),
                    MessageStop(stop_reason="tool_use"),
                )
            ),
        ),
        check=check_malformed_tool_arguments,
        description="unparseable streamed arguments are rejected, never executed",
    ),
    ConformanceCase(
        id="http_error_status",
        requires=frozenset({HTTP}),
        request=_request,
        wire=(
            StatusStep(
                401,
                body=b'{"error":{"type":"authentication_error","message":"bad"}}',
            ),
        ),
        expect=Expectation(error=ProviderError, requests=1),
        description="a non-retryable HTTP status becomes a typed ProviderError",
    ),
    ConformanceCase(
        id="retry_after_429_then_success",
        requires=frozenset({HTTP}),
        request=_request,
        wire=(
            StatusStep(429, body=b"slow down", headers=(("retry-after", "3"),)),
            EventsStep(reply_text("recovered")),
        ),
        expect=Expectation(
            text="recovered",
            requests=2,
            retry_delays=(3.0,),
            saw_message_start=True,
        ),
        description="429 honours Retry-After then succeeds on one bounded retry",
    ),
    ConformanceCase(
        id="midstream_disconnect",
        requires=frozenset({HTTP}),
        request=_request,
        wire=(FaultStep("midstream_disconnect", events=(MessageStart(),)),),
        expect=Expectation(
            error=ProviderError,
            requests=1,
            saw_message_start=True,
        ),
        description="a drop after the first event is surfaced, never replayed",
    ),
    ConformanceCase(
        id="malformed_wire_payload",
        requires=frozenset({HTTP}),
        request=_request,
        wire=(FaultStep("malformed_wire"),),
        expect=Expectation(error=ProviderError, requests=1),
        description="non-JSON SSE payload raises a typed ProviderError",
    ),
    ConformanceCase(
        id="request_schema_translation",
        requires=frozenset({WIRE_REQUEST}),
        request=lambda: _request(
            system="SYS",
            tools=(READ_TOOL,),
            params=SamplingParams(
                temperature=0.2,
                max_output_tokens=100,
                top_p=0.9,
                stop_sequences=["STOP"],
            ),
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(text="ok", requests=1),
        overlays={
            ANTHROPIC_DIALECT: Expectation(
                request_contains={
                    "stream": True,
                    "system": "SYS",
                    "max_tokens": 100,
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "stop_sequences": ["STOP"],
                    "tools": [
                        {
                            "name": "Read",
                            "description": "reads files",
                            "input_schema": {"type": "object"},
                        }
                    ],
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        }
                    ],
                },
                request_excludes=("cache_control",),
            ),
            GEMINI_DIALECT: Expectation(
                request_contains={
                    "systemInstruction": {"parts": [{"text": "SYS"}]},
                    "tools": [
                        {
                            "functionDeclarations": [
                                {"name": "Read", "description": "reads files"}
                            ]
                        }
                    ],
                    "contents": [
                        {"role": "user", "parts": [{"text": "hi"}]}
                    ],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 100,
                        "topP": 0.9,
                        "stopSequences": ["STOP"],
                    },
                },
            ),
            OPENAI_RESPONSES_DIALECT: Expectation(
                request_contains={
                    "stream": True,
                    "instructions": "SYS",
                    "max_output_tokens": 100,
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "tools": [
                        {
                            "type": "function",
                            "name": "Read",
                            "description": "reads files",
                            "parameters": {"type": "object"},
                        }
                    ],
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        }
                    ],
                },
                # Responses has no stop parameter; the sequence is dropped.
                request_excludes=("stop_sequences", '"stop"'),
            ),
            OPENAI_CHAT_DIALECT: Expectation(
                request_contains={
                    "stream": True,
                    "max_tokens": 100,
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "stop": ["STOP"],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "description": "reads files",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    "messages": [
                        {"role": "system", "content": "SYS"},
                        {"role": "user", "content": "hi"},
                    ],
                },
            ),
        },
        description="system, tools, and sampling reach each dialect's body",
    ),
    ConformanceCase(
        id="multimodal_request",
        requires=frozenset({WIRE_REQUEST, VISION, DOCUMENTS}),
        request=lambda: _request(
            messages=[Message(role="user", content=[Text(text="look"), IMAGE, DOCUMENT])]
        ),
        wire=(EventsStep(reply_text("seen")),),
        expect=Expectation(text="seen", requests=1),
        overlays={
            ANTHROPIC_DIALECT: Expectation(
                request_contains={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                    },
                                },
                                {
                                    "type": "document",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "application/pdf",
                                    },
                                },
                            ],
                        }
                    ]
                }
            ),
            GEMINI_DIALECT: Expectation(
                request_contains={
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {"text": "look"},
                                {"inlineData": {"mimeType": "image/png"}},
                                {"inlineData": {"mimeType": "application/pdf"}},
                            ],
                        }
                    ]
                }
            ),
            OPENAI_RESPONSES_DIALECT: Expectation(
                request_contains={
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "look"},
                                {"type": "input_image"},
                                {"type": "input_file"},
                            ],
                        }
                    ]
                }
            ),
        },
        description="image and document blocks translate into each dialect",
    ),
)


# ---------------------------------------------------------------------------
# Anthropic-dialect cases
# ---------------------------------------------------------------------------

_ANTHROPIC: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        id="replay_signed_thinking",
        requires=frozenset({WIRE_REQUEST, THINKING}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(
                    Thinking(text="reasoning", signature="sig-1"),
                    Text(text="visible"),
                ),
                _user("next"),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            request_contains={
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "reasoning",
                                "signature": "sig-1",
                            },
                            {"type": "text", "text": "visible"},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "next"}],
                    },
                ]
            },
        ),
        description="a signed thinking block is replayed verbatim",
    ),
    ConformanceCase(
        id="cross_provider_degradation",
        requires=frozenset({WIRE_REQUEST, DEGRADATION}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(
                    Thinking(text="secret reasoning"),  # no signature: unrepresentable
                    Text(text="visible"),
                ),
                _user("next"),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            request_excludes=("secret reasoning",),
            request_contains={
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "visible"}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "next"}],
                    },
                ]
            },
        ),
        check=check_degradation_policy,
        description="an unrepresentable block degrades per the declared policy",
    ),
    ConformanceCase(
        id="wire_stream_error_event",
        requires=frozenset({HTTP}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=_request,
        wire=(
            StatusStep(
                200,
                body=(
                    b'data: {"type":"error","error":'
                    b'{"type":"overloaded_error","message":"Overloaded"}}\n\n'
                ),
            ),
        ),
        expect=Expectation(
            error=ProviderError,
            error_match="overloaded_error",
            requests=1,
        ),
        description="an in-stream error frame becomes a typed ProviderError",
    ),
    ConformanceCase(
        id="recorded_text_usage",
        requires=frozenset({HTTP}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_anthropic("text_usage.sse")),),
        expect=Expectation(
            text="Recorded",
            usage=Usage(input=9, output=6, cache_read=4, cache_write=2),
            stop_reason="end_turn",
            requests=1,
            saw_message_start=True,
        ),
        description="decode a recorded Anthropic text+usage frame",
    ),
    ConformanceCase(
        id="recorded_single_tool",
        requires=frozenset({HTTP}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_anthropic("single_tool.sse")),),
        expect=Expectation(
            tool_calls=(("toolu_recorded", "Read", {"path": "b.txt"}),),
            stop_reason="tool_use",
            requests=1,
        ),
        description="decode a recorded streamed-partial-JSON tool call",
    ),
    ConformanceCase(
        id="recorded_parallel_tools",
        requires=frozenset({HTTP}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_anthropic("parallel_tools.sse")),),
        expect=Expectation(
            tool_calls=(
                ("toolu_p1", "Read", {"path": "one"}),
                ("toolu_p2", "Read", {"path": "two"}),
            ),
            stop_reason="tool_use",
            requests=1,
        ),
        description="decode two sequential tool_use blocks",
    ),
    ConformanceCase(
        id="recorded_thinking_signed",
        requires=frozenset({HTTP, THINKING}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_anthropic("thinking_signed.sse")),),
        expect=Expectation(
            thinking=("Weighing options.",),
            signatures=("sig_recorded_123",),
            text="Here is the answer.",
            stop_reason="end_turn",
            requests=1,
        ),
        description="decode a recorded thinking block with signature",
    ),
    ConformanceCase(
        id="recorded_refusal",
        requires=frozenset({HTTP}),
        dialects=frozenset({ANTHROPIC_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_anthropic("refusal.sse")),),
        expect=Expectation(
            text="I can't assist with that.",
            stop_reason="refusal",
            requests=1,
        ),
        description="decode a recorded refusal frame",
    ),
)


# ---------------------------------------------------------------------------
# Gemini-dialect cases (the distinctive shape of a second dialect)
# ---------------------------------------------------------------------------

_GEMINI: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        id="gemini_synthetic_tool_id",
        requires=frozenset({HTTP, TOOLS}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_gemini("no_tool_id.sse")),),
        expect=Expectation(
            # The exact synthetic id is intentionally not pinned (it may embed a
            # uuid); the name and input are, and the id must be adapter-minted.
            tool_calls_loose=(("Read", {"path": "a.txt"}),),
            stop_reason="tool_use",
            requests=1,
        ),
        check=check_minted_tool_ids,
        description="Gemini omits tool ids; the adapter mints a stable synthetic one",
    ),
    ConformanceCase(
        id="gemini_malformed_function_call",
        requires=frozenset({HTTP, TOOLS}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_gemini("malformed_function_call.sse")),),
        expect=Expectation(stop_reason="error", requests=1),
        description="MALFORMED_FUNCTION_CALL normalizes to stop_reason=error",
    ),
    ConformanceCase(
        id="gemini_stream_error_event",
        requires=frozenset({HTTP}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=_request,
        wire=(
            StatusStep(
                200,
                body=(
                    b'data: {"error":{"code":429,"status":"RESOURCE_EXHAUSTED",'
                    b'"message":"quota"}}\n\n'
                ),
            ),
        ),
        expect=Expectation(
            error=ProviderError,
            error_match="RESOURCE_EXHAUSTED",
            requests=1,
        ),
        description="a Gemini in-stream error object becomes a ProviderError",
    ),
    ConformanceCase(
        id="gemini_recorded_thinking_tool",
        requires=frozenset({HTTP, THINKING, TOOLS}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_gemini("thinking_tool.sse")),),
        expect=Expectation(
            thinking=("Let me check.",),
            signatures=("c2lnbmF0dXJl",),
            tool_calls=(("call_abc", "Read", {"path": "a.txt"}),),
            stop_reason="tool_use",
            requests=1,
            saw_message_start=True,
        ),
        overlays={
            GEMINI_DIALECT: Expectation(usage=Usage(20, 33, 0, 0, 5)),
        },
        description="decode a recorded Gemini thought signature, tool call, and usage",
    ),
    ConformanceCase(
        id="gemini_foreign_thinking_dropped",
        requires=frozenset({WIRE_REQUEST, THINKING, DEGRADATION}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(
                    Thinking(text="foreign reasoning", signature="anthropic-sig"),
                    Text(text="visible"),
                    provider="anthropic",
                ),
                _user("next"),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            request_excludes=("foreign reasoning", "anthropic-sig"),
            request_contains={
                "contents": [
                    {"role": "model", "parts": [{"text": "visible"}]},
                    {"role": "user", "parts": [{"text": "next"}]},
                ]
            },
        ),
        check=check_degradation_policy,
        description="a foreign thought signature is dropped, never sent and rejected",
    ),
    ConformanceCase(
        id="gemini_native_signature_replay",
        requires=frozenset({WIRE_REQUEST, THINKING}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(
                    Thinking(text="plan", signature="sig-g"),
                    ToolUse("call_1", "Read", {"path": "a"}),
                    provider="gemini",
                ),
                _user("go"),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            request_contains={
                "contents": [
                    {
                        "role": "model",
                        "parts": [
                            {"text": "plan", "thought": True},
                            {
                                "functionCall": {
                                    "name": "Read",
                                    "args": {"path": "a"},
                                    "id": "call_1",
                                },
                                "thoughtSignature": "sig-g",
                            },
                        ],
                    },
                    {"role": "user", "parts": [{"text": "go"}]},
                ]
            },
        ),
        description="a native signature rides on the function call on replay",
    ),
    ConformanceCase(
        id="gemini_tool_name_id_recovery",
        requires=frozenset({WIRE_REQUEST, TOOLS}),
        dialects=frozenset({GEMINI_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(ToolUse(SYNTHETIC_READ_ID, "Read", {"path": "a"})),
                Message(
                    role="user",
                    content=[ToolResult(SYNTHETIC_READ_ID, [Text("contents")])],
                ),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            # The synthetic id never reaches the wire; the function name does.
            request_excludes=(SYNTHETIC_READ_ID,),
            request_contains={
                "contents": [
                    {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "Read", "args": {"path": "a"}}}
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": "Read",
                                    "response": {"content": "contents"},
                                }
                            }
                        ],
                    },
                ]
            },
        ),
        description="the name survives even when the id is synthetic or foreign",
    ),
)


# ---------------------------------------------------------------------------
# OpenAI-dialect cases (the two wire protocols behind one adapter)
# ---------------------------------------------------------------------------

_OPENAI_RESPONSES: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        id="openai_responses_recorded_text_usage",
        requires=frozenset({HTTP}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_openai("responses_text_usage.sse")),),
        expect=Expectation(
            text="Recorded",
            usage=Usage(input=9, output=6, cache_read=4),
            stop_reason="end_turn",
            requests=1,
            saw_message_start=True,
        ),
        description="decode a recorded Responses text+usage event log",
    ),
    ConformanceCase(
        id="openai_responses_recorded_single_tool",
        requires=frozenset({HTTP, TOOLS}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_openai("responses_single_tool.sse")),),
        expect=Expectation(
            tool_calls=(("call_recorded", "Read", {"path": "b.txt"}),),
            stop_reason="tool_use",
            requests=1,
        ),
        description="decode a recorded Responses streamed-partial-JSON tool call",
    ),
    ConformanceCase(
        id="openai_responses_recorded_parallel_tools",
        requires=frozenset({HTTP, TOOLS}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_openai("responses_parallel_tools.sse")),),
        expect=Expectation(
            tool_calls=(
                ("call_p1", "Read", {"path": "one"}),
                ("call_p2", "Read", {"path": "two"}),
            ),
            stop_reason="tool_use",
            requests=1,
        ),
        description="decode two Responses function_call items",
    ),
    ConformanceCase(
        id="openai_responses_recorded_reasoning",
        requires=frozenset({HTTP, THINKING, TOOLS}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_openai("responses_reasoning.sse")),),
        expect=Expectation(
            thinking=("Weighing options.",),
            signatures=("sig_responses_recorded",),
            text="Here is the answer.",
            stop_reason="end_turn",
            requests=1,
            saw_message_start=True,
        ),
        overlays={
            OPENAI_RESPONSES_DIALECT: Expectation(usage=Usage(20, 33, 0, 0, 5)),
        },
        description="decode a recorded Responses reasoning item with encrypted content",
    ),
    ConformanceCase(
        id="openai_responses_recorded_refusal",
        requires=frozenset({HTTP}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_openai("responses_refusal.sse")),),
        expect=Expectation(
            text="I can't assist with that.",
            stop_reason="refusal",
            requests=1,
        ),
        description="decode a recorded Responses refusal",
    ),
    ConformanceCase(
        id="openai_responses_stream_error_event",
        requires=frozenset({HTTP}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=_request,
        wire=(
            StatusStep(
                200,
                body=(
                    b'event: error\n'
                    b'data: {"type":"error","error":{"code":"server_error",'
                    b'"message":"boom"}}\n\n'
                ),
            ),
        ),
        expect=Expectation(
            error=ProviderError,
            error_match="server_error",
            requests=1,
        ),
        description="a Responses in-stream error event becomes a ProviderError",
    ),
    ConformanceCase(
        id="openai_responses_reasoning_replay_drops_foreign",
        requires=frozenset({WIRE_REQUEST, THINKING, DEGRADATION}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(
                    Thinking(text="foreign reasoning", signature="anthropic-sig"),
                    Text(text="visible"),
                    provider="anthropic",
                ),
                _user("next"),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            # A foreign reasoning blob is dropped, never sent and rejected.
            request_excludes=("foreign reasoning", "anthropic-sig"),
            request_contains={
                "input": [
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "visible"}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "next"}],
                    },
                ]
            },
        ),
        check=check_degradation_policy,
        description="a foreign reasoning blob degrades per the declared policy",
    ),
    ConformanceCase(
        id="openai_responses_tool_result_replay",
        requires=frozenset({WIRE_REQUEST, TOOLS}),
        dialects=frozenset({OPENAI_RESPONSES_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(ToolUse("call_1", "Read", {"path": "a"})),
                Message(
                    role="user",
                    content=[ToolResult("call_1", [Text("contents")])],
                ),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            request_contains={
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "Read",
                        # Responses carries arguments as a JSON-encoded string on
                        # the wire, not a decoded object (unlike Gemini's args).
                        "arguments": '{"path":"a"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "contents",
                    },
                ]
            },
        ),
        description="tool calls and results replay as Responses input items",
    ),
)


_OPENAI_CHAT: tuple[ConformanceCase, ...] = (
    ConformanceCase(
        id="openai_chat_recorded_text_usage",
        requires=frozenset({HTTP}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_openai("chat_text_usage.sse")),),
        expect=Expectation(
            text="Recorded",
            usage=Usage(input=9, output=6, cache_read=4),
            stop_reason="end_turn",
            requests=1,
            saw_message_start=True,
        ),
        description="decode a recorded Chat Completions text+usage stream",
    ),
    ConformanceCase(
        id="openai_chat_recorded_single_tool",
        requires=frozenset({HTTP, TOOLS}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_openai("chat_single_tool.sse")),),
        expect=Expectation(
            tool_calls=(("call_recorded", "Read", {"path": "b.txt"}),),
            stop_reason="tool_use",
            requests=1,
        ),
        description="decode a recorded Chat Completions partial-JSON tool call",
    ),
    ConformanceCase(
        id="openai_chat_recorded_parallel_tools",
        requires=frozenset({HTTP, TOOLS}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=lambda: _request(tools=(READ_TOOL,)),
        wire=(StatusStep(200, body=load_openai("chat_parallel_tools.sse")),),
        expect=Expectation(
            tool_calls=(
                ("call_p1", "Read", {"path": "one"}),
                ("call_p2", "Read", {"path": "two"}),
            ),
            stop_reason="tool_use",
            requests=1,
        ),
        description="decode two Chat Completions tool_calls by index",
    ),
    ConformanceCase(
        id="openai_chat_recorded_reasoning",
        requires=frozenset({HTTP, THINKING}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_openai("chat_reasoning.sse")),),
        expect=Expectation(
            thinking=("Weighing options.",),
            signatures=("sig_chat_recorded",),
            text="Here is the answer.",
            stop_reason="end_turn",
            requests=1,
        ),
        description="decode a recorded Chat Completions reasoning field",
    ),
    ConformanceCase(
        id="openai_chat_recorded_refusal",
        requires=frozenset({HTTP}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=_request,
        wire=(StatusStep(200, body=load_openai("chat_refusal.sse")),),
        expect=Expectation(
            text="I can't assist with that.",
            stop_reason="refusal",
            requests=1,
        ),
        description="a content_filter finish_reason normalizes to refusal",
    ),
    ConformanceCase(
        id="openai_chat_stream_error_object",
        requires=frozenset({HTTP}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=_request,
        wire=(
            StatusStep(
                200,
                body=(
                    b'data: {"error":{"type":"invalid_request_error",'
                    b'"code":"context_length_exceeded","message":"too long"}}\n\n'
                ),
            ),
        ),
        expect=Expectation(
            error=ProviderError,
            error_match="context_length_exceeded",
            requests=1,
        ),
        description="a Chat Completions in-stream error object becomes a ProviderError",
    ),
    ConformanceCase(
        id="openai_chat_tool_result_replay",
        requires=frozenset({WIRE_REQUEST, TOOLS}),
        dialects=frozenset({OPENAI_CHAT_DIALECT}),
        request=lambda: _request(
            messages=[
                _assistant(ToolUse("call_1", "Read", {"path": "a"})),
                Message(
                    role="user",
                    content=[ToolResult("call_1", [Text("contents")])],
                ),
            ]
        ),
        wire=(EventsStep(reply_text("ok")),),
        expect=Expectation(
            text="ok",
            requests=1,
            request_contains={
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path":"a"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "content": "contents",
                    },
                ]
            },
        ),
        description="tool calls and results replay as assistant/tool messages",
    ),
)

CASES: tuple[ConformanceCase, ...] = (
    _SHARED + _ANTHROPIC + _GEMINI + _OPENAI_RESPONSES + _OPENAI_CHAT
)

assert len({case.id for case in CASES}) == len(CASES), "duplicate conformance case id"
