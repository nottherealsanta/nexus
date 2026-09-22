"""Request-aware token counting with an exact-count disk cache.

Plan section 5.2/risk table: prefer a provider's ``count_tokens`` when it offers
one, cache the exact result by a **semantic key of the request**, and fall back
to the calibrated heuristic when the provider cannot (or fails to) count. A
counting failure must never fail a turn.

The semantic key deliberately covers only request-meaningful data — provider,
model, system, message content, tool schemas, and sampling parameters — plus a
cache schema version. It excludes resolved credentials, harness metadata
(accounting/budgets), and streaming/cache-only wire fields, so two requests that
are semantically identical share one cache entry.
"""
from __future__ import annotations

from typing import Any

from ..model.request import ModelRequest
from ..model.tokenizer import DEFAULT_TOKENIZER
from .cache import TokenCountCache, canonical_json, semantic_key
from .parts import canonical_block_text, canonical_tool_text

__all__ = [
    "REQUEST_CACHE_VERSION",
    "RequestTokenCounter",
    "heuristic_request_tokens",
    "provider_token_counter",
    "request_semantic_payload",
]

#: Bumped when the semantic projection changes; stored in the payload so old
#: cache entries are not reused for a different projection.
REQUEST_CACHE_VERSION = 1


def request_semantic_payload(
    request: ModelRequest, *, provider: str | None = None
) -> dict[str, Any]:
    """A deterministic, credential-free projection of ``request``.

    Only the fields that change what the provider would tokenize are included.
    ``request.metadata`` is excluded on purpose: it carries harness accounting
    and never affects the token count.
    """
    params = request.params
    return {
        "schema": REQUEST_CACHE_VERSION,
        "provider": provider if provider is not None else request.provider,
        "model": request.model,
        "system": request.system or "",
        "messages": [
            {
                "role": message.role,
                "content": [canonical_block_text(block) for block in message.content],
            }
            for message in request.messages
        ],
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ],
        "params": {
            "temperature": params.temperature,
            "max_output_tokens": params.max_output_tokens,
            "top_p": params.top_p,
            "stop_sequences": list(params.stop_sequences),
            "thinking_budget": params.thinking_budget,
        },
    }


def request_semantic_key(request: ModelRequest, *, provider: str | None = None) -> str:
    """The stable cache key for ``request``."""
    return semantic_key(request_semantic_payload(request, provider=provider))


def _count_text(tokenizer: Any, text: str) -> int:
    count_tokens = getattr(tokenizer, "count_tokens", None)
    if callable(count_tokens):
        return int(count_tokens(text))
    if callable(tokenizer):
        return int(tokenizer(text))
    raise TypeError("tokenizer must be callable or expose count_tokens(text)")


def heuristic_request_tokens(
    request: ModelRequest, *, tokenizer: Any = DEFAULT_TOKENIZER
) -> int:
    """Estimate the input tokens of ``request`` with the calibrated heuristic."""
    total = 0
    if request.system:
        total += _count_text(tokenizer, request.system)
    for message in request.messages:
        for block in message.content:
            total += _count_text(tokenizer, canonical_block_text(block))
    for tool in request.tools:
        total += _count_text(tokenizer, canonical_tool_text(tool))
    if request.params.stop_sequences:
        total += _count_text(tokenizer, canonical_json(list(request.params.stop_sequences)))
    return total


class RequestTokenCounter:
    """Counts token usage for whole requests, preferring the provider.

    Only **provider-sourced exact counts** are cached. A provider ``None``,
    exception, or invalid value degrades to the calibrated heuristic for that
    call, and the heuristic result is deliberately **not** written to the cache,
    so a later identical request retries the provider and can still populate an
    exact entry.
    """

    def __init__(
        self,
        provider: Any,
        *,
        cache: TokenCountCache | None = None,
        provider_name: str | None = None,
        tokenizer: Any = DEFAULT_TOKENIZER,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._provider_name = provider_name or getattr(provider, "name", None)
        self._tokenizer = tokenizer if tokenizer is not None else DEFAULT_TOKENIZER

    @property
    def provider(self) -> Any:
        return self._provider

    def key_for(self, request: ModelRequest) -> str:
        return request_semantic_key(request, provider=self._provider_name)

    async def count_request(self, request: ModelRequest) -> int:
        key = self.key_for(request)
        if self._cache is not None:
            cached = self._cache.get_key(key)
            if cached is not None:
                if self._cache.source_for_key(key) == "provider":
                    return cached
                # A heuristic/unknown-tagged entry is not an exact count:
                # discard it and recompute so the provider can repopulate.
                self._cache.invalidate_key(key)
        value: int | None = None
        count_tokens = getattr(self._provider, "count_tokens", None)
        if callable(count_tokens):
            try:
                value = await count_tokens(request)
            except Exception:  # noqa: BLE001 - a count failure must not fail a turn
                value = None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            if self._cache is not None:
                try:
                    self._cache.put_key(key, value, source="provider")
                except Exception:  # noqa: BLE001 - cache writes are best-effort
                    return value
            return value
        # Heuristic fallback is advisory only and must not poison the exact
        # cache, so it is returned without being stored.
        return heuristic_request_tokens(request, tokenizer=self._tokenizer)


def provider_token_counter(
    provider: Any, *, cache: TokenCountCache | None = None
) -> RequestTokenCounter:
    """Build a :class:`RequestTokenCounter` for ``provider``."""
    return RequestTokenCounter(provider, cache=cache)
