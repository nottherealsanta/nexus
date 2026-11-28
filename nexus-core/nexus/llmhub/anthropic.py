"""Native Anthropic Messages with Nexus-owned context, tools and streaming."""
from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .anthropic_policy import image_limit, thinking_policy
from .anthropic_schema import normalize_tool_schema, strict_schema_cost
from .responses import HeaderSource, redact_headers, retry_after_seconds
from .sse import parse_sse
from .types import (
    Done,
    Event,
    Image,
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
    ToolSpec,
    Usage,
    WireEvent,
    WireRequest,
    WireResponse,
    tool_images,
)

REQUEST_BYTE_LIMIT = 128000000
COMPATIBILITY_INSTRUCTION = "You are Claude Code, Anthropic's official CLI for Claude."


def compatibility_prompt(instructions: str) -> str:
    return COMPATIBILITY_INSTRUCTION + ("\n\n" + instructions if instructions else "")


async def http_error(response: httpx.Response, *, origin: str) -> ProviderError:
    descriptions = {
        400: "invalid request",
        401: "authentication failed; sign in to Claude Direct again",
        403: "access denied",
        404: "model or endpoint not available",
        413: "request too large; no context was removed",
        429: "rate limit reached; retry later",
        500: "Anthropic service error",
        529: "Anthropic service overloaded; retry later",
    }
    description = descriptions.get(response.status_code, "request rejected")
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        if len(raw) + len(chunk) > 65536:
            break
        raw.extend(chunk)
    else:
        try:
            data = json.loads(raw)
            error = data.get("error") if isinstance(data, dict) else None
            kind = error.get("type") if isinstance(error, dict) else None
            # Only known error types are safe: service messages can echo credentials or context.
            if kind in {"invalid_request_error", "authentication_error", "permission_error", "not_found_error",
                        "request_too_large", "rate_limit_error", "api_error", "overloaded_error"}:
                description += f" ({kind})"
        except (ValueError, TypeError):
            pass
    return ProviderError(f"{origin}: HTTP {response.status_code}; {description}",
                         status=response.status_code, phase="response",
                         retry_after=retry_after_seconds(response.headers.get("retry-after")))


@dataclass(frozen=True, slots=True)
class TokenTaskBudget:
    total: int
    remaining: int | None = None

    def wire(self) -> dict[str, Any]:
        if isinstance(self.total, bool) or not isinstance(self.total, int) or self.total <= 0:
            raise ProviderError("claude-direct: task budget total must be a positive integer")
        if self.remaining is not None and (isinstance(self.remaining, bool) or not isinstance(self.remaining, int)
                                           or not 0 <= self.remaining <= self.total):
            raise ProviderError("claude-direct: task budget remaining must be between zero and total")
        return {"type": "tokens", "total": self.total,
                **({"remaining": self.remaining} if self.remaining is not None else {})}


@dataclass(frozen=True, slots=True)
class DirectRequest(Request):
    cache_retention: str | None = None
    prewarm: bool = False
    tool_choice: str | None = None
    forced_tool: str | None = None
    stable_system_chars: int | None = None
    task_budget: TokenTaskBudget | None = None


def image_block(url: str) -> dict[str, Any]:
    header, separator, data = url.partition(",")
    if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
        raise ProviderError("claude-direct: images must be base64 data URLs")
    return {"type": "image", "source": {"type": "base64", "media_type": header[5:-7], "data": data}}


def output_limit(model: str) -> int:
    from .catalog import _read_cache, cache_path

    try:
        entry = _read_cache(cache_path())["anthropic"]["models"][model]
        limit = entry.get("limit", {}).get("output")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            return limit
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 64000


def apply_prompt_cache(body: dict[str, Any], *, retention: str = "long", system_anchor: int | None = -1) -> None:
    if retention not in {"short", "long"}:
        raise ProviderError("claude-direct: cache retention must be short or long")
    control = {"type": "ephemeral", **({"ttl": "1h"} if retention == "long" else {})}
    remaining = 4
    for blocks, anchor in ((body.get("tools", []), -1), (body.get("system", []), system_anchor)):
        if blocks and anchor is not None:
            blocks[anchor]["cache_control"] = dict(control)
            remaining -= 1
    messages = body["messages"]
    users = [i for i, m in enumerate(messages) if m["role"] == "user" and
             any(b["type"] in {"text", "image"} for b in m["content"])]
    candidates = [len(messages) - 1, *reversed(users[14::15]), len(messages) - 2]
    for index in dict.fromkeys(candidates):
        if remaining <= 0:
            break
        if index < 0:
            continue
        for block in reversed(messages[index]["content"]):
            if block["type"] not in {"thinking", "redacted_thinking"}:
                block["cache_control"] = dict(control)
                remaining -= 1
                break


def wire_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description,
            "input_schema": normalize_tool_schema(tool.parameters)}


def build_body(req: Request, *, origin: str, max_tokens: int | None = None, cache: bool = True,
               cache_retention: str = "long") -> dict[str, Any]:
    retention = req.cache_retention if isinstance(req, DirectRequest) else None
    if retention is not None:
        if retention not in {"none", "short", "long"}:
            raise ProviderError("claude-direct: cache retention must be none, short or long")
        cache = retention != "none"
        cache_retention = retention
    limit = output_limit(req.model)
    if max_tokens is not None and (isinstance(max_tokens, bool) or max_tokens <= 0):
        raise ProviderError("claude-direct: max_tokens must be positive")
    cap = min(max_tokens, limit) if max_tokens is not None else limit
    messages: list[dict[str, Any]] = []
    for message in req.messages:
        content: list[dict[str, Any]] = []
        for part in message.parts:
            if isinstance(part, Text):
                if part.text:
                    content.append({"type": "text", "text": part.text})
            elif isinstance(part, Image):
                content.append(image_block(part.data_url))
            elif isinstance(part, Reasoning):
                if (part.origin == origin and part.raw and part.raw.get("type") in {"thinking", "redacted_thinking"}
                        and part.raw.get("_model", req.model) == req.model):
                    content.append({k: v for k, v in part.raw.items() if not k.startswith("_")})
            elif isinstance(part, ToolCall):
                arguments = json.loads(part.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise ProviderError("claude-direct: tool input must be an object")
                content.append({"type": "tool_use", "id": part.id, "name": part.name,
                                "input": arguments})
            elif isinstance(part, ToolResult):
                result: Any = part.output
                if tool_images(part):
                    result = [{"type": "text", "text": part.output}] + [image_block(m.data_url) for m in tool_images(part)]
                content.append({"type": "tool_result", "tool_use_id": part.call_id,
                                "content": result, "is_error": part.is_error})
        if content:
            role = "user" if message.role == "tool" else message.role
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(content)
            else:
                messages.append({"role": role, "content": content})
    body: dict[str, Any] = {"model": req.model, "messages": messages, "max_tokens": cap, "stream": True}
    if req.instructions:
        body["system"] = [{"type": "text", "text": req.instructions}]
    system_anchor = -1
    if isinstance(req, DirectRequest) and req.stable_system_chars is not None:
        boundary = req.stable_system_chars
        if isinstance(boundary, bool) or not isinstance(boundary, int) or not 0 <= boundary <= len(req.instructions):
            raise ProviderError("claude-direct: stable_system_chars must be an instruction character boundary")
        body["system"] = [{"type": "text", "text": text} for text in
                          (req.instructions[:boundary], req.instructions[boundary:]) if text]
        system_anchor = 0 if boundary else None
    body.setdefault("system", []).insert(0, {"type": "text", "text": COMPATIBILITY_INSTRUCTION})
    if isinstance(req, DirectRequest) and req.stable_system_chars is not None:
        system_anchor = 1 if req.stable_system_chars else 0
    if req.tools:
        if any(t.parameters.get("type") != "object" for t in req.tools):
            raise ProviderError("claude-direct: tool schemas must have an object root")
        body["tools"] = [wire_tool(t) for t in req.tools]
    policy = thinking_policy(req.model)
    effort = req.reasoning_effort
    if effort in {"none", "off"} and policy.mode != "none":
        if policy.off == "low":
            body["output_config"] = {"effort": "low"}
        else:
            body["thinking"] = {"type": policy.off}
    elif effort and effort not in {"none", "off"}:
        if effort not in policy.efforts:
            raise ProviderError(f"claude-direct: unsupported effort {effort!r} for {req.model}")
        if policy.mode == "adaptive":
            body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": effort}
        else:
            budgets = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 16384, "xhigh": 24576, "max": 32768}
            budget = min(budgets[effort], cap - 4000)
            if budget < 1024:
                raise ProviderError("claude-direct: thinking requires at least 5024 output tokens")
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if policy.mode == "budget-effort":
                body["output_config"] = {"effort": effort}
        if policy.display:
            body["thinking"]["display"] = "summarized"
        if policy.prefix_binding:
            body["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "error"}
        body["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
    if isinstance(req, DirectRequest):
        choice = req.tool_choice
        if req.forced_tool is not None:
            if choice not in {None, "tool"}:
                raise ProviderError("claude-direct: forced_tool conflicts with tool_choice")
            choice = "tool"
        if choice is not None:
            if choice not in {"auto", "any", "none", "tool"}:
                raise ProviderError("claude-direct: unsupported tool_choice")
            if choice == "tool" and req.forced_tool not in {t.name for t in req.tools}:
                raise ProviderError("claude-direct: forced_tool must name an offered tool")
            if choice in {"any", "tool"}:
                if not req.tools:
                    raise ProviderError("claude-direct: forced tool choice requires tools")
                if body.get("thinking", {}).get("type") in {"enabled", "adaptive", "between_tools"}:
                    raise ProviderError("claude-direct: forced tool choice is incompatible with thinking")
                if req.model.startswith(("claude-fable-", "claude-mythos-")):
                    raise ProviderError("claude-direct: this model does not support forced tool choice")
                if req.prewarm:
                    raise ProviderError("claude-direct: prompt-cache prewarm is incompatible with forced tool choice")
            body["tool_choice"] = {"type": choice}
            if choice == "tool":
                body["tool_choice"]["name"] = req.forced_tool
        if req.task_budget is not None:
            if not isinstance(req.task_budget, TokenTaskBudget):
                raise ProviderError("claude-direct: task_budget must be a TokenTaskBudget")
            budget = req.task_budget.wire()
            if policy.mode != "adaptive":
                raise ProviderError("claude-direct: task budgets are restricted to known adaptive-thinking models")
            signed_replay = any(block["type"] in {"thinking", "redacted_thinking"}
                                for message in messages for block in message["content"])
            if req.task_budget.remaining is not None and signed_replay:
                raise ProviderError("claude-direct: task budget remaining is incompatible with signed thinking replay")
            body.setdefault("output_config", {})["task_budget"] = budget
    if cache:
        apply_prompt_cache(body, retention=cache_retention, system_anchor=system_anchor)
    return body


class MessagesState:
    def __init__(self, origin: str, model: str | None = None) -> None:
        self.origin = origin
        self.model = model
        self.blocks: dict[int, dict[str, Any]] = {}
        self.arguments: dict[int, str] = {}
        self.usage: dict[str, int] = {}
        self.response_id: str | None = None
        self.stop: str | None = None
        self.ended = False
        self.started = False
        self.seen_indices: set[int] = set()

    def update_usage(self, usage: Any) -> None:
        if usage is None:
            return
        if not isinstance(usage, dict):
            raise ProviderError(f"{self.origin}: malformed Messages usage")
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
            if key in usage:
                value = usage[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ProviderError(f"{self.origin}: malformed Messages usage")
                self.usage[key] = value

    def consume(self, data: Any) -> list[Event]:
        if not isinstance(data, dict):
            raise ProviderError(f"{self.origin}: invalid Messages event")
        kind = data.get("type")
        out: list[Event] = []
        if self.ended:
            raise ProviderError(f"{self.origin}: event after message_stop")
        if kind == "error":
            raise ProviderError(f"{self.origin}: Messages stream error")
        if kind == "message_start":
            if self.started:
                raise ProviderError(f"{self.origin}: duplicate message_start")
            message = data["message"]
            if not isinstance(message, dict) or not isinstance(message.get("id"), str) or not message["id"]:
                raise ProviderError(f"{self.origin}: malformed message_start")
            if message.get("content"):
                raise ProviderError(f"{self.origin}: unexpected initial message content; no context was discarded")
            self.started = True
            self.response_id = message.get("id")
            self.update_usage(message.get("usage"))
        elif kind == "content_block_start":
            index = data["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ProviderError(f"{self.origin}: invalid content block index")
            if index in self.seen_indices:
                raise ProviderError(f"{self.origin}: duplicate content block")
            self.seen_indices.add(index)
            block = self.blocks[index] = dict(data["content_block"])
            if block.get("type") not in {"text", "thinking", "redacted_thinking", "tool_use"}:
                raise ProviderError(f"{self.origin}: unsupported content block; no context was silently discarded")
            if block["type"] == "tool_use":
                if not isinstance(block.get("input", {}), dict):
                    raise ProviderError(f"{self.origin}: tool input must be an object")
                self.arguments[index] = ""
                out.append(ToolCallStarted(block["id"], block["name"]))
            elif block["type"] == "text" and block.get("text"):
                out.append(TextDelta(block["text"]))
            elif block["type"] == "thinking" and block.get("thinking"):
                out.append(ReasoningDelta(block["thinking"]))
        elif kind == "content_block_delta":
            index = data["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index not in self.blocks:
                raise ProviderError(f"{self.origin}: delta for unknown content block")
            block = self.blocks[index]
            delta = data["delta"]
            expected = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "thinking",
                        "input_json_delta": "tool_use"}.get(delta["type"])
            if expected is not None and block["type"] != expected:
                raise ProviderError(f"{self.origin}: content delta does not match its block")
            if delta["type"] == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
                out.append(TextDelta(delta["text"]))
            elif delta["type"] == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
                out.append(ReasoningDelta(delta["thinking"]))
            elif delta["type"] == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            elif delta["type"] == "input_json_delta":
                if block.get("input"):
                    raise ProviderError(f"{self.origin}: conflicting initial and streamed tool input")
                self.arguments[index] += delta["partial_json"]
                out.append(ToolArgsDelta(block["id"], delta["partial_json"]))
            else:
                raise ProviderError(f"{self.origin}: unsupported content delta; no context was silently discarded")
        elif kind == "content_block_stop":
            index = data["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index not in self.blocks:
                raise ProviderError(f"{self.origin}: stop for unknown content block")
            block = self.blocks.pop(index)
            if block["type"] == "text":
                out.append(PartDone(Text(block["text"], self.origin, block)))
            elif block["type"] in {"thinking", "redacted_thinking"}:
                if self.model:
                    block["_model"] = self.model
                out.append(PartDone(Reasoning(block.get("thinking", ""), self.origin, block)))
            elif block["type"] == "tool_use":
                arguments = self.arguments.pop(index) or json.dumps(block.get("input") or {})
                value = json.loads(arguments)
                if not isinstance(value, dict):
                    raise ProviderError(f"{self.origin}: tool input must be an object")
                block["input"] = value
                out.append(PartDone(ToolCall(block["id"], block["name"], arguments, self.origin, block)))
        elif kind == "message_delta":
            stop = data.get("delta", {}).get("stop_reason")
            if stop is not None:
                if not isinstance(stop, str) or not stop:
                    raise ProviderError(f"{self.origin}: malformed stop reason")
                if self.stop is not None and stop != self.stop:
                    raise ProviderError(f"{self.origin}: conflicting stop reasons")
                self.stop = stop
            self.update_usage(data.get("usage"))
        elif kind == "message_stop":
            if not self.started or self.blocks or self.stop is None:
                raise ProviderError(f"{self.origin}: incomplete Messages response")
            self.ended = True
            cached = self.usage.get("cache_read_input_tokens", 0)
            out.append(Usage(self.usage.get("input_tokens", 0) + cached + self.usage.get("cache_creation_input_tokens", 0),
                             cached, self.usage.get("output_tokens", 0)))
            stop = {"end_turn": "end_turn", "stop_sequence": "end_turn", "tool_use": "tool_calls",
                    "max_tokens": "max_tokens", "model_context_window_exceeded": "max_tokens"}.get(self.stop, "other")
            out.append(Done(stop, self.response_id, self.stop if stop == "other" else None))
        elif kind != "ping":
            raise ProviderError(f"{self.origin}: unsupported Messages event; no context was silently discarded")
        return out


class AnthropicProvider:
    name = "claude-direct"

    def __init__(self, auth: HeaderSource, *, client: httpx.AsyncClient | None = None,
                 max_tokens: int | None = None, cache: bool = True, cache_retention: str = "long",
                 eager_tools: tuple[str, ...] = (), strict_tools: tuple[str, ...] = (),
                 api_version: str = "2023-06-01", request_encoding: str | None = None) -> None:
        if api_version not in {"2023-06-01", "2026-10-21"}:
            raise ProviderError("claude-direct: unsupported API version")
        if request_encoding not in {None, "gzip"}:
            raise ProviderError("claude-direct: request encoding must be gzip or None")
        self._auth = auth
        self._client = client
        self._max_tokens = max_tokens
        self._cache = cache
        self._cache_retention = cache_retention
        self._eager_tools = frozenset(eager_tools)
        self._strict_tools = frozenset(strict_tools)
        self._api_version = api_version
        self._request_encoding = request_encoding

    def body(self, req: Request) -> dict[str, Any]:
        body = build_body(req, origin=self.name, max_tokens=self._max_tokens, cache=self._cache,
                          cache_retention=self._cache_retention)
        strict_count = optional_count = union_count = 0
        for spec, tool in zip(req.tools, body.get("tools", []), strict=True):
            if tool["name"] in self._eager_tools:
                tool["eager_input_streaming"] = True
            if tool["name"] not in self._strict_tools or tool["name"] in self._eager_tools:
                continue
            cost = strict_schema_cost(spec.parameters)
            if (cost is None or strict_schema_cost(tool["input_schema"]) is None or strict_count >= 20
                    or optional_count + cost[0] > 24 or union_count + cost[1] > 16):
                continue
            tool["strict"] = True
            strict_count += 1
            optional_count += cost[0]
            union_count += cost[1]
        images = sum(isinstance(part, Image) + (len(tool_images(part)) if isinstance(part, ToolResult) else 0)
                     for message in req.messages for part in message.parts)
        if images > image_limit(req.model):
            raise ProviderError(f"claude-direct: request exceeds {image_limit(req.model)} images; no context was removed")
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        if size > REQUEST_BYTE_LIMIT:
            raise ProviderError("claude-direct: request exceeds 128 MB; no context was removed")
        if isinstance(req, DirectRequest) and req.prewarm:
            if body.get("thinking", {}).get("type") == "enabled":
                raise ProviderError("claude-direct: prompt-cache prewarm is incompatible with budget thinking")
            if not any(block.get("cache_control") for group in (body.get("tools", []), body.get("system", []),
                                                               *(m["content"] for m in body["messages"])) for block in group):
                raise ProviderError("claude-direct: prompt-cache prewarm requires a cache breakpoint")
            body["max_tokens"] = 0
            body["stream"] = False
        return body

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        url = "https://api.anthropic.com/v1/messages"
        body = await asyncio.to_thread(self.body, req)
        headers = {"content-type": "application/json", "accept": "text/event-stream" if body["stream"] else "application/json",
                   "anthropic-version": self._api_version}
        headers.update(await self._auth.headers())
        betas = [headers.get("anthropic-beta", "oauth-2025-04-20")]
        if body.get("thinking", {}).get("type") in {"enabled", "adaptive"}:
            betas.extend(["interleaved-thinking-2025-05-14", "context-management-2025-06-27"])
        if "effort" in body.get("output_config", {}):
            betas.append("effort-2025-11-24")
        if "task_budget" in body.get("output_config", {}):
            betas.append("task-budgets-2026-03-13")
        if "block_binding" in body.get("thinking", {}):
            betas.append("thinking-binding-controls-2026-08-01")
        if any(tool.get("strict") is True for tool in body.get("tools", [])):
            betas.append("structured-outputs-2025-12-15")
        if any(b.get("cache_control", {}).get("ttl") == "1h"
               for group in (body.get("tools", []), body.get("system", []),
                             *(m["content"] for m in body["messages"])) for b in group):
            betas.append("extended-cache-ttl-2025-04-11")
        tokens = {}
        for group in betas:
            for token in group.split(","):
                token = token.strip()
                if token:
                    tokens.setdefault(token.lower(), token)
        headers["anthropic-beta"] = ",".join(tokens.values())
        payload = None
        if self._request_encoding == "gzip":
            def compress() -> bytes:
                return gzip.compress(json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                                                allow_nan=False).encode("utf-8"), mtime=0)
            payload = await asyncio.to_thread(compress)
            headers["content-encoding"] = "gzip"
        yield WireRequest("POST", url, redact_headers(headers), body)
        client = self._client or await asyncio.to_thread(httpx.AsyncClient, timeout=httpx.Timeout(300, connect=15), follow_redirects=False)
        state = MessagesState(self.name, req.model)
        try:
            async with client.stream("POST", url, headers=headers,
                                     **({"content": payload} if payload is not None else {"json": body})) as response:
                yield WireResponse(response.status_code, redact_headers(dict(response.headers)))
                if response.status_code != 200:
                    raise await http_error(response, origin=self.name)
                if not body["stream"]:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise ProviderError("claude-direct: prompt-cache prewarm response too large")
                    try:
                        data = json.loads(raw)
                    except ValueError as exc:
                        raise ProviderError("claude-direct: malformed prompt-cache prewarm response") from exc
                    yield WireEvent("message", data)
                    if (not isinstance(data, dict) or data.get("type") != "message" or data.get("content") != []
                            or data.get("stop_reason") != "max_tokens" or not isinstance(data.get("usage"), dict)
                            or data["usage"].get("output_tokens") != 0):
                        raise ProviderError("claude-direct: unexpected prompt-cache prewarm response; no content was discarded")
                    usage = data["usage"]
                    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
                    if any(isinstance(usage.get(k, 0), bool) or not isinstance(usage.get(k, 0), int)
                           or usage.get(k, 0) < 0 for k in keys):
                        raise ProviderError("claude-direct: malformed prompt-cache prewarm usage")
                    cached = usage.get("cache_read_input_tokens", 0)
                    yield Usage(usage.get("input_tokens", 0) + cached + usage.get("cache_creation_input_tokens", 0), cached, 0)
                    yield Done("max_tokens", data.get("id"))
                    return
                async for name, text in parse_sse(response.aiter_lines()):
                    try:
                        data = json.loads(text)
                        yield WireEvent(name, data)
                        for event in state.consume(data):
                            yield event
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ProviderError(f"{self.name}: malformed Messages stream") from exc
                    if state.ended:
                        break
            if not state.ended:
                raise ProviderError(f"{self.name}: stream ended before message_stop")
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: network error: {type(exc).__name__}",
                                phase="stream" if state.response_id else "transport",
                                network_kind=type(exc).__name__ if isinstance(exc, httpx.TransportError) else None) from exc
        finally:
            if self._client is None:
                await client.aclose()
