"""Phase 3 request-aware token counting and its exact-count cache (exit criterion 3)."""
from __future__ import annotations

import json

from nexus.context.cache import TokenCountCache, semantic_key
from nexus.context.counting import (
    RequestTokenCounter,
    heuristic_request_tokens,
    request_semantic_key,
    request_semantic_payload,
)
from nexus.model.message import Document, Image, Message, Text, ToolUse
from nexus.model.request import ModelRequest, SamplingParams, ToolSchema


class FakeProvider:
    name = "fake"

    def __init__(self, value=42, *, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    async def count_tokens(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def request(*, system="sys", text="hello", metadata=None, temperature=None):
    return ModelRequest(
        messages=[Message(role="user", content=[Text(text=text)])],
        system=system,
        params=SamplingParams(temperature=temperature),
        model="m",
        provider="fake",
        metadata=metadata or {},
    )


async def test_provider_called_once_for_identical_semantics(tmp_path):
    provider = FakeProvider(123)
    cache = TokenCountCache(tmp_path / "cache")
    counter = RequestTokenCounter(provider, cache=cache)

    first = await counter.count_request(request())
    second = await counter.count_request(request())
    third = await counter.count_request(request())

    assert first == second == third == 123
    assert provider.calls == 1


async def test_cache_invalidates_on_semantic_change(tmp_path):
    provider = FakeProvider(1)
    counter = RequestTokenCounter(provider, cache=TokenCountCache(tmp_path / "c"))
    await counter.count_request(request(text="a"))
    await counter.count_request(request(text="b"))
    await counter.count_request(request(system="different"))
    assert provider.calls == 3


async def test_metadata_does_not_affect_the_semantic_key():
    a = request(metadata={"context": {"used_tokens": 1}})
    b = request(metadata={"context": {"used_tokens": 999}})
    assert request_semantic_key(a) == request_semantic_key(b)
    # Sampling is semantic and must be part of the key.
    assert request_semantic_key(a) != request_semantic_key(
        request(temperature=0.5)
    )


async def test_tools_are_part_of_the_semantic_key():
    tool = ToolSchema(name="Read", description="d", input_schema={"type": "object"})
    with_tool = ModelRequest(
        messages=[Message(role="user", content=[Text(text="hi")])],
        tools=[tool],
        model="m",
    )
    without = ModelRequest(
        messages=[Message(role="user", content=[Text(text="hi")])], model="m"
    )
    assert request_semantic_key(with_tool) != request_semantic_key(without)


async def test_tool_use_input_is_part_of_the_semantic_key():
    def with_input(value):
        return ModelRequest(
            messages=[
                Message(
                    role="assistant",
                    content=[ToolUse(id="c", name="Read", input={"path": value})],
                )
            ],
            model="m",
        )

    assert request_semantic_key(with_input("a")) != request_semantic_key(
        with_input("b")
    )


async def test_provider_none_falls_back_to_heuristic(tmp_path):
    counter = RequestTokenCounter(FakeProvider(None), cache=TokenCountCache(tmp_path / "c"))
    value = await counter.count_request(request())
    assert value == heuristic_request_tokens(request())
    assert value > 0


async def test_provider_failure_falls_back_to_heuristic(tmp_path):
    counter = RequestTokenCounter(
        FakeProvider(error=RuntimeError("boom")),
        cache=TokenCountCache(tmp_path / "c"),
    )
    value = await counter.count_request(request())
    assert value == heuristic_request_tokens(request())


async def test_corrupt_cache_entry_is_a_miss_and_provider_is_recalled(tmp_path):
    provider = FakeProvider(7)
    cache = TokenCountCache(tmp_path / "c")
    counter = RequestTokenCounter(provider, cache=cache)
    await counter.count_request(request())
    assert provider.calls == 1

    key = counter.key_for(request())
    cache._path(key).write_text("{not json", encoding="utf-8")
    value = await counter.count_request(request())
    assert value == 7
    assert provider.calls == 2


async def test_heuristic_is_deterministic_and_positive():
    req = request()
    assert heuristic_request_tokens(req) == heuristic_request_tokens(req)
    assert heuristic_request_tokens(req) > 0


def test_payload_excludes_resolved_secrets_from_storage(tmp_path):
    # The cache stores only a hash and the count, never the payload.
    cache = TokenCountCache(tmp_path / "c")
    secret = "sk-ant-SECRET-VALUE"
    req = request(system=f"authorization: Bearer {secret}")
    payload = request_semantic_payload(req)
    key = semantic_key(payload)
    cache.put_key(key, 5)
    blob = " ".join(p.read_text() for p in cache.directory.glob("*.json"))
    assert secret not in blob
    assert "authorization" not in blob


# ---------------------------------------------------------------------------
# Cache correctness: only exact provider counts are stored
# ---------------------------------------------------------------------------


class FlakyProvider:
    name = "flaky"

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    async def count_tokens(self, request):
        self.calls += 1
        return self.values.pop(0) if self.values else None


async def test_heuristic_fallback_is_not_cached_and_provider_is_retried(tmp_path):
    provider = FlakyProvider([None, 77])
    cache = TokenCountCache(tmp_path / "c")
    counter = RequestTokenCounter(provider, cache=cache)

    first = await counter.count_request(request())
    assert first == heuristic_request_tokens(request())
    # The heuristic result must not poison the exact cache.
    assert cache.get_key(counter.key_for(request())) is None

    second = await counter.count_request(request())
    assert second == 77
    assert provider.calls == 2
    assert cache.get_key(counter.key_for(request())) == 77
    assert cache.source_for_key(counter.key_for(request())) == "provider"


async def test_cached_provider_count_is_reused_without_retry(tmp_path):
    provider = FlakyProvider([42, 99])
    counter = RequestTokenCounter(provider, cache=TokenCountCache(tmp_path / "c"))
    assert await counter.count_request(request()) == 42
    assert await counter.count_request(request()) == 42
    assert provider.calls == 1


def test_non_utf8_corruption_is_a_miss_and_is_deleted(tmp_path):
    cache = TokenCountCache(tmp_path / "c")
    payload = {"text": "x"}
    cache.put(payload, 10)
    path = cache._path(semantic_key(payload))
    path.write_bytes(b"\xff\xfe\x00not-utf8")
    assert cache.get(payload) is None
    assert not path.exists()


def test_semantic_projection_hashes_image_bytes_and_url():
    def with_image(data, url=None):
        return ModelRequest(
            messages=[
                Message(
                    role="user",
                    content=[Image(media_type="image/png", data=data, url=url)],
                )
            ],
            model="m",
        )

    # Equal-length, different bytes must invalidate.
    assert request_semantic_key(with_image(b"\x00" * 8)) != request_semantic_key(
        with_image(b"\x01" * 8)
    )
    # Equal bytes, different URL must invalidate.
    assert request_semantic_key(
        with_image(b"\x00" * 8, "https://a/1")
    ) != request_semantic_key(with_image(b"\x00" * 8, "https://b/2"))
    # Identical input stays stable.
    assert request_semantic_key(with_image(b"\x00" * 8)) == request_semantic_key(
        with_image(b"\x00" * 8)
    )


def test_semantic_projection_hashes_document_bytes():
    def with_document(data):
        return ModelRequest(
            messages=[
                Message(
                    role="user",
                    content=[Document(media_type="application/pdf", data=data)],
                )
            ],
            model="m",
        )

    assert request_semantic_key(with_document(b"a" * 16)) != request_semantic_key(
        with_document(b"b" * 16)
    )


async def test_heuristic_tagged_cache_entry_is_ignored_and_replaced(tmp_path):
    provider = FakeProvider(55)
    cache = TokenCountCache(tmp_path / "c")
    counter = RequestTokenCounter(provider, cache=cache)
    key = counter.key_for(request())
    cache.put_key(key, 999, source="heuristic")

    assert await counter.count_request(request()) == 55
    assert provider.calls == 1
    # The stale heuristic entry was discarded and replaced by the exact count.
    assert cache.source_for_key(key) == "provider"
    assert cache.get_key(key) == 55


async def test_unknown_source_cache_entry_is_ignored(tmp_path):
    provider = FakeProvider(7)
    cache = TokenCountCache(tmp_path / "c")
    counter = RequestTokenCounter(provider, cache=cache)
    key = counter.key_for(request())
    cache.put_key(key, 123, source="guess")
    assert await counter.count_request(request()) == 7
    assert cache.source_for_key(key) == "provider"


async def test_entry_without_source_tag_is_ignored(tmp_path):
    provider = FakeProvider(9)
    cache = TokenCountCache(tmp_path / "c")
    counter = RequestTokenCounter(provider, cache=cache)
    key = counter.key_for(request())
    # A structurally valid v2 record with no source tag is treated as inexact.
    cache.directory.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "key": key, "tokens": 321, "created": 1.0}
    cache._path(key).write_text(json.dumps(payload), encoding="utf-8")
    assert await counter.count_request(request()) == 9
    assert cache.source_for_key(key) == "provider"
