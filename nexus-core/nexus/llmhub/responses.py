"""OpenAI Responses dialect, shared by Codex and grok (PLAN.md step 1, step 3).

One request builder and one stream parser; what differs per host is data in
`ResponsesQuirks`. Requests are always `store: false` and streamed, so the
whole history is resent each time.

Replay rule: an item this same provider produced is sent back verbatim (`raw`)
when the host is known to accept that (`replay_raw`); otherwise it is rebuilt
from the normalized fields. Reasoning is sent back only as a raw item that
carries `encrypted_content`. Without it the server has nothing to resume from,
so the item is dropped.
"""
from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol

import httpx

from .sse import parse_sse
from .types import (
    Done,
    Event,
    Image,
    Message,
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

REDACTED_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "chatgpt-account-id",
                    "x-openai-internal-codex-residency", "x-api-key"}
ERROR_BODY_CHARS = 4_000
CACHE_KEY_MAX = 64


class HeaderSource(Protocol):
    async def headers(self) -> Mapping[str, str]: ...


@dataclass(frozen=True, slots=True)
class ResponsesQuirks:
    encrypted_reasoning: bool = False  # ask for `reasoning.encrypted_content` and replay it
    replay_raw: bool = False  # send this provider's own output items back verbatim
    prompt_cache_key: bool = False  # send the conversation id as `prompt_cache_key`
    session_header: str | None = None  # header that carries the conversation id


# -- Request ----------------------------------------------------------------


def input_items(messages: tuple[Message, ...] | list[Message], *, origin: str, quirks: ResponsesQuirks) -> list[dict[str, Any]]:
    """Translate the conversation into a Responses `input` item list, in order."""
    items: list[dict[str, Any]] = []
    for message in messages:
        start = len(items)
        for part in message.parts:
            own = getattr(part, "origin", None) == origin and getattr(part, "raw", None) is not None
            if isinstance(part, Text):
                if own and quirks.replay_raw:
                    items.append(part.raw)  # type: ignore[arg-type]
                elif message.role == "assistant":
                    items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": part.text}]})
                else:
                    items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": part.text}]})
            elif isinstance(part, Image):
                content = {"type": "input_image", "image_url": part.data_url}
                if len(items) > start and items[-1].get("role") == "user":
                    items[-1]["content"].append(content)
                else:
                    items.append({"type": "message", "role": "user", "content": [content]})
            elif isinstance(part, Reasoning):
                if own and quirks.encrypted_reasoning and part.raw.get("encrypted_content"):  # type: ignore[union-attr]
                    items.append(part.raw)  # type: ignore[arg-type]
            elif isinstance(part, ToolCall):
                if own and quirks.replay_raw:
                    items.append(part.raw)  # type: ignore[arg-type]
                else:
                    items.append({"type": "function_call", "call_id": part.id, "name": part.name, "arguments": part.arguments})
            elif isinstance(part, ToolResult):
                items.append({"type": "function_call_output", "call_id": part.call_id, "output": part.output})
    return items


def build_body(req: Request, *, origin: str, quirks: ResponsesQuirks) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": req.model,
        "instructions": req.instructions,
        "input": input_items(req.messages, origin=origin, quirks=quirks),
        "tools": [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters, "strict": False}
            for t in req.tools
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"summary": "auto", **({"effort": req.reasoning_effort} if req.reasoning_effort else {})},
        "store": False,
        "stream": True,
    }
    if quirks.encrypted_reasoning:
        body["include"] = ["reasoning.encrypted_content"]
    if quirks.prompt_cache_key and req.conversation_id:
        body["prompt_cache_key"] = req.conversation_id[:CACHE_KEY_MAX]
    return body


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: ("***" if k.lower() in REDACTED_HEADERS else v) for k, v in headers.items()}


def retry_after_seconds(value: str | None) -> float | None:
    """A bounded Retry-After value (seconds or HTTP date), or None when unusable."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        from datetime import datetime
        from email.utils import parsedate_to_datetime

        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = (moment - datetime.now(UTC)).total_seconds()
    return max(0.0, min(seconds, 3600.0)) if math.isfinite(seconds) else None


# -- Response ---------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.call_ids: dict[str, str] = {}  # output item id -> call_id
        self.summary_parts = 0
        self.saw_tool_call = False


def _join_text(content: Any, kind: str) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(c.get("text", "") for c in content if isinstance(c, Mapping) and c.get("type") == kind)


def _usage(response: Mapping[str, Any]) -> Usage | None:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    inp = usage.get("input_tokens_details") or {}
    out = usage.get("output_tokens_details") or {}
    return Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_input_tokens=int(inp.get("cached_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(out.get("reasoning_tokens") or 0),
    )


def events_for(name: str, data: Any, state: _State, *, origin: str) -> list[Event]:
    """Normalize one Responses stream event (the wire event is emitted separately)."""
    if not isinstance(data, Mapping):
        return []
    kind = data.get("type") or name
    if kind in ("error", "response.failed"):
        error = data.get("error") or (data.get("response") or {}).get("error") or {}
        code = error.get("code") or error.get("type") or kind
        raise ProviderError(f"{origin}: stream error {code}: {str(error.get('message') or '')[:ERROR_BODY_CHARS]}")
    if kind == "response.output_item.added":
        item = data.get("item") or {}
        if item.get("type") == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or "")
            state.call_ids[str(item.get("id") or call_id)] = call_id
            state.saw_tool_call = True
            return [ToolCallStarted(id=call_id, name=str(item.get("name") or ""))]
        if item.get("type") == "reasoning":
            state.summary_parts = 0
        return []
    if kind == "response.reasoning_summary_part.added":
        state.summary_parts += 1
        return [ReasoningDelta("\n\n")] if state.summary_parts > 1 else []
    if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        return [ReasoningDelta(str(data.get("delta") or ""))]
    if kind == "response.output_text.delta":
        return [TextDelta(str(data.get("delta") or ""))]
    if kind == "response.function_call_arguments.delta":
        item_id = str(data.get("item_id") or "")
        return [ToolArgsDelta(id=state.call_ids.get(item_id, item_id), delta=str(data.get("delta") or ""))]
    if kind == "response.output_item.done":
        item = dict(data.get("item") or {})
        item_type = item.get("type")
        if item_type == "message":
            return [PartDone(Text(_join_text(item.get("content"), "output_text"), origin=origin, raw=item))]
        if item_type == "reasoning":
            texts = item.get("summary") or item.get("content") or []
            summary = "\n\n".join(s.get("text", "") for s in texts if isinstance(s, Mapping))
            return [PartDone(Reasoning(summary, origin=origin, raw=item))]
        if item_type == "function_call":
            return [PartDone(ToolCall(id=str(item.get("call_id") or ""), name=str(item.get("name") or ""),
                                      arguments=str(item.get("arguments") or ""), origin=origin, raw=item))]
        return []
    if kind in ("response.completed", "response.incomplete"):
        response = data.get("response") or {}
        out: list[Event] = []
        usage = _usage(response)
        if usage:
            out.append(usage)
        if kind == "response.incomplete":
            reason = str((response.get("incomplete_details") or {}).get("reason") or "")
            out.append(Done("max_tokens" if reason == "max_output_tokens" else "other", response.get("id"), reason or None))
        else:
            out.append(Done("tool_calls" if state.saw_tool_call else "end_turn", response.get("id")))
        return out
    return []


# -- Provider ---------------------------------------------------------------


class ResponsesProvider:
    def __init__(self, name: str, base_url: str, auth: HeaderSource, quirks: ResponsesQuirks,
                 *, client: httpx.AsyncClient | None = None) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._quirks = quirks
        self._client = client

    def body(self, req: Request) -> dict[str, Any]:
        return build_body(req, origin=self.name, quirks=self._quirks)

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        url = f"{self._base_url}/responses"
        body = self.body(req)
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self._quirks.session_header and req.conversation_id:
            headers[self._quirks.session_header] = req.conversation_id[:CACHE_KEY_MAX]
        headers.update(await self._auth.headers())
        yield WireRequest("POST", url, redact_headers(headers), body)

        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        state = _State()
        finished = False
        try:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                yield WireResponse(response.status_code, redact_headers(dict(response.headers)))
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", "replace")[:ERROR_BODY_CHARS]
                    raise ProviderError(f"{self.name}: HTTP {response.status_code}", status=response.status_code,
                                        body=raw, phase="response",
                                        retry_after=retry_after_seconds(response.headers.get("retry-after")))
                async for name, text in parse_sse(response.aiter_lines()):
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = text
                    yield WireEvent(name, data)
                    for event in events_for(name, data, state, origin=self.name):
                        finished = finished or isinstance(event, Done)
                        yield event
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: network error: {type(exc).__name__}", phase="transport") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if not finished:
            raise ProviderError(f"{self.name}: stream ended before response.completed", phase="stream")
