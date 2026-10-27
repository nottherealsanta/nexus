"""OpenAI Chat Completions dialect, shared by Copilot and OpenCode Go (PLAN.md step 3).

A streamed `/chat/completions` request. Deltas are accumulated per choice 0:
`content`, a reasoning field (`reasoning_content`, `reasoning` or
`reasoning_text`, whichever the host sends), and `tool_calls` by index. The
parts are emitted when the stream ends (`[DONE]`), because usage often arrives
in a chunk after `finish_reason`.

Replay rule: reasoning from this same provider goes back on the assistant
message under the field it arrived in (some hosts, such as Kimi- and
DeepSeek-style thinking models, require it next to tool calls). Reasoning from
another provider is dropped.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from .responses import CACHE_KEY_MAX, ERROR_BODY_CHARS, HeaderSource, redact_headers, retry_after_seconds
from .sse import parse_sse
from .types import (
    Done,
    Event,
    PartDone,
    ProviderError,
    Reasoning,
    ReasoningDelta,
    Request,
    Text,
    TextDelta,
    ToolArgsDelta,
    ToolCall,
    ToolCallStarted,
    ToolResult,
    Usage,
    WireEvent,
    WireRequest,
    WireResponse,
)

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


@dataclass(frozen=True, slots=True)
class ChatQuirks:
    session_header: str | None = None  # header that carries the conversation id
    initiator_header: bool = False  # Copilot: `x-initiator: user|agent` per request


# -- Request ----------------------------------------------------------------


def chat_messages(req: Request, *, origin: str) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = [{"role": "system", "content": req.instructions}] if req.instructions else []
    for message in req.messages:
        if message.role == "tool":
            wire.extend({"role": "tool", "tool_call_id": p.call_id, "content": p.output} for p in message.parts if isinstance(p, ToolResult))
            continue
        text = "".join(p.text for p in message.parts if isinstance(p, Text))
        if message.role == "user":
            wire.append({"role": "user", "content": text})
            continue
        entry: dict[str, Any] = {"role": "assistant", "content": text or None}
        calls = [p for p in message.parts if isinstance(p, ToolCall)]
        if calls:
            entry["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments or "{}"}} for c in calls]
        for part in message.parts:
            if isinstance(part, Reasoning) and part.origin == origin and part.raw and part.raw.get("field") in REASONING_FIELDS:
                entry[part.raw["field"]] = part.summary
        wire.append(entry)
    return wire


def build_body(req: Request, *, origin: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": req.model,
        "messages": chat_messages(req, origin=origin),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if req.tools:
        body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in req.tools]
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = False
    if req.reasoning_effort:
        body["reasoning_effort"] = req.reasoning_effort
    return body


# -- Response ---------------------------------------------------------------


@dataclass
class _Call:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _State:
    text: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    reasoning_field: str | None = None
    calls: dict[int, _Call] = field(default_factory=dict)
    finish: str | None = None
    usage: Usage | None = None
    response_id: str | None = None


def events_for_chunk(chunk: Any, state: _State, *, origin: str) -> list[Event]:
    if not isinstance(chunk, Mapping):
        return []
    if isinstance(chunk.get("error"), Mapping):
        error = chunk["error"]
        raise ProviderError(f"{origin}: stream error {error.get('code') or error.get('type') or ''}: {str(error.get('message') or '')[:ERROR_BODY_CHARS]}")
    state.response_id = state.response_id or chunk.get("id")
    usage = chunk.get("usage")
    if isinstance(usage, Mapping):
        state.usage = Usage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            cached_input_tokens=int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
        )
    out: list[Event] = []
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, Mapping) or choice.get("index", 0) != 0:
            continue
        delta = choice.get("delta") or {}
        for name in REASONING_FIELDS:
            value = delta.get(name)
            if isinstance(value, str) and value:
                state.reasoning_field = state.reasoning_field or name
                state.reasoning.append(value)
                out.append(ReasoningDelta(value))
                break
        content = delta.get("content")
        if isinstance(content, str) and content:
            state.text.append(content)
            out.append(TextDelta(content))
        for raw in delta.get("tool_calls") or []:
            index = int(raw.get("index") or 0)
            call = state.calls.get(index)
            function = raw.get("function") or {}
            if call is None:
                call = state.calls[index] = _Call(id=str(raw.get("id") or f"call_{index}"), name=str(function.get("name") or ""))
                out.append(ToolCallStarted(call.id, call.name))
            elif function.get("name") and not call.name:
                call.name = str(function["name"])
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                call.arguments += arguments
                out.append(ToolArgsDelta(call.id, arguments))
        if choice.get("finish_reason"):
            state.finish = str(choice["finish_reason"])
    return out


def finish(state: _State, *, origin: str) -> list[Event]:
    out: list[Event] = []
    if state.reasoning:
        out.append(PartDone(Reasoning("".join(state.reasoning), origin=origin, raw={"field": state.reasoning_field})))
    if state.text:
        out.append(PartDone(Text("".join(state.text), origin=origin)))
    for index in sorted(state.calls):
        call = state.calls[index]
        out.append(PartDone(ToolCall(call.id, call.name, call.arguments, origin=origin)))
    if state.usage:
        out.append(state.usage)
    stop = "tool_calls" if state.calls else {"stop": "end_turn", "length": "max_tokens"}.get(state.finish or "stop", "other")
    out.append(Done(stop, state.response_id, None if stop != "other" else state.finish))  # type: ignore[arg-type]
    return out


# -- Provider ---------------------------------------------------------------


class ChatProvider:
    def __init__(self, name: str, base_url: str, auth: HeaderSource, quirks: ChatQuirks,
                 *, client: httpx.AsyncClient | None = None) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._quirks = quirks
        self._client = client

    def body(self, req: Request) -> dict[str, Any]:
        return build_body(req, origin=self.name)

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        url = f"{self._base_url}/chat/completions"
        body = self.body(req)
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self._quirks.session_header and req.conversation_id:
            headers[self._quirks.session_header] = req.conversation_id[:CACHE_KEY_MAX]
        if self._quirks.initiator_header:
            last = req.messages[-1].role if req.messages else "user"
            headers["x-initiator"] = "user" if last == "user" else "agent"
        headers.update(await self._auth.headers())
        yield WireRequest("POST", url, redact_headers(headers), body)

        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        state = _State()
        ended = False  # `[DONE]` seen
        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                yield WireResponse(response.status_code, redact_headers(dict(response.headers)))
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", "replace")[:ERROR_BODY_CHARS]
                    raise ProviderError(f"{self.name}: HTTP {response.status_code}", status=response.status_code,
                                        body=raw, phase="response",
                                        retry_after=retry_after_seconds(response.headers.get("retry-after")))
                async for name, text in parse_sse(response.aiter_lines()):
                    if text.strip() == "[DONE]":
                        yield WireEvent(name, "[DONE]")
                        ended = True
                        break
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = text
                    yield WireEvent(name, data)
                    for event in events_for_chunk(data, state, origin=self.name):
                        yield event
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: network error: {type(exc).__name__}", phase="transport") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if not ended and state.finish is None:
            raise ProviderError(f"{self.name}: stream ended before a finish_reason or [DONE]; the reply may be cut off",
                                phase="stream")
        for event in finish(state, origin=self.name):
            yield event
