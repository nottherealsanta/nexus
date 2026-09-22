"""Phase 3 token-count cache and prompt-cache boundaries (plan 5.2)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from nexus.context.cache import (
    CACHE_VERSION,
    CacheBoundary,
    TokenCountCache,
    boundaries_metadata,
    canonical_json,
    prompt_cache_boundaries,
    semantic_key,
)
from nexus.model.capabilities import Capabilities


def ticking_clock():
    state = {"t": 1000.0}

    def now():
        state["t"] += 1.0
        return state["t"]

    return now


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def test_semantic_key_is_stable_for_equal_data():
    payload = {"b": 1, "a": [1, 2, 3], "c": {"z": True}}
    assert semantic_key(payload) == semantic_key(dict(payload))


def test_semantic_key_is_independent_of_key_order():
    assert semantic_key({"a": 1, "b": 2}) == semantic_key({"b": 2, "a": 1})


def test_semantic_key_changes_with_content():
    assert semantic_key({"text": "a"}) != semantic_key({"text": "b"})


def test_semantic_key_handles_bytes_paths_and_sets():
    payload = {
        "data": b"\x00\x01",
        "path": Path("/w/x"),
        "tags": {"b", "a"},
    }
    assert semantic_key(payload) == semantic_key(
        {
            "data": b"\x00\x01",
            "path": Path("/w/x"),
            "tags": {"a", "b"},
        }
    )


def test_canonical_json_is_compact_and_sorted():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


# ---------------------------------------------------------------------------
# Hit / miss / invalidation
# ---------------------------------------------------------------------------


def test_cache_hit_after_put(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    payload = {"model": "m", "text": "hello"}
    assert cache.get(payload) is None
    cache.put(payload, 42)
    assert cache.get(payload) == 42
    assert payload in cache


def test_cache_key_is_used_directly(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    key = semantic_key({"x": 1})
    cache.put_key(key, 5)
    assert cache.get_key(key) == 5
    assert cache.invalidate_key(key) is True
    assert cache.get_key(key) is None


def test_cache_invalidation_by_payload(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    payload = {"text": "x"}
    cache.put(payload, 3)
    assert cache.invalidate(payload) is True
    assert cache.get(payload) is None
    assert cache.invalidate(payload) is False


# ---------------------------------------------------------------------------
# Corruption and atomicity
# ---------------------------------------------------------------------------


def test_corrupt_entry_is_a_miss_and_is_removed(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    payload = {"text": "x"}
    cache.put(payload, 10)
    path = cache._path(semantic_key(payload))
    path.write_text("{not json", encoding="utf-8")
    assert cache.get(payload) is None
    assert not path.exists()


def test_truncated_entry_is_a_miss(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    payload = {"text": "x"}
    cache.put(payload, 10)
    path = cache._path(semantic_key(payload))
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw[: len(raw) // 2], encoding="utf-8")
    assert cache.get(payload) is None


def test_wrong_version_or_key_is_a_miss(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    payload = {"text": "x"}
    key = semantic_key(payload)
    cache.put(payload, 10)
    path = cache._path(key)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["version"] = CACHE_VERSION + 1
    path.write_text(json.dumps(record), encoding="utf-8")
    assert cache.get(payload) is None
    assert not path.exists()


def test_missing_directory_is_a_miss_not_an_error(tmp_path):
    cache = TokenCountCache(tmp_path / "does-not-exist")
    assert cache.get({"text": "x"}) is None


def test_writes_are_atomic_and_leave_no_temp_files(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    for index in range(5):
        cache.put({"n": index}, index)
    leftovers = [p for p in cache.directory.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []
    assert len(cache) == 5


def test_cache_is_bounded_and_prunes_oldest(tmp_path):
    clock = ticking_clock()
    cache = TokenCountCache(tmp_path / "cache", max_entries=3, clock=clock)
    for index in range(6):
        cache.put({"n": index}, index)
    assert len(cache) == 3
    # The newest entries survive.
    assert cache.get({"n": 5}) == 5
    assert cache.get({"n": 4}) == 4
    assert cache.get({"n": 3}) == 3
    assert cache.get({"n": 0}) is None


# ---------------------------------------------------------------------------
# No secret material is stored
# ---------------------------------------------------------------------------


def test_cache_stores_no_content_or_secrets(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    secret = "sk-ant-SUPER-SECRET-TOKEN"
    payload = {"text": f"authorization: Bearer {secret}", "api_key": secret}
    cache.put(payload, 99)

    blobs = " ".join(
        path.read_text(encoding="utf-8") for path in cache.directory.glob("*.json")
    )
    assert secret not in blobs
    assert "authorization" not in blobs
    assert "api_key" not in blobs
    # Only metadata is present.
    record = json.loads(next(cache.directory.glob("*.json")).read_text(encoding="utf-8"))
    assert record["tokens"] == 99
    assert set(record) == {"version", "key", "tokens", "source", "created"}


def test_clear_removes_entries(tmp_path):
    cache = TokenCountCache(tmp_path / "cache")
    cache.put({"a": 1}, 1)
    cache.put({"a": 2}, 2)
    assert cache.clear() == 2
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Prompt-cache boundaries
# ---------------------------------------------------------------------------


def test_boundaries_are_empty_for_unsupported_capabilities():
    caps = Capabilities(prompt_caching=False)
    assert prompt_cache_boundaries(caps, history_position=4) == ()


def test_boundaries_are_produced_when_supported():
    caps = Capabilities(prompt_caching=True)
    boundaries = prompt_cache_boundaries(caps, history_position=4)
    assert boundaries == (
        CacheBoundary(0, "system_tools"),
        CacheBoundary(4, "history"),
    )


def test_boundaries_can_be_forced_off():
    caps = Capabilities(prompt_caching=True)
    assert prompt_cache_boundaries(caps, history_position=4, enabled=False) == ()


def test_boundary_metadata_is_plain_and_provider_neutral():
    caps = Capabilities(prompt_caching=True)
    boundaries = prompt_cache_boundaries(caps, history_position=2)
    metadata = boundaries_metadata(boundaries)
    assert metadata == [
        {"position": 0, "scope": "system_tools"},
        {"position": 2, "scope": "history"},
    ]
    rendered = json.dumps(metadata)
    # No Anthropic wire syntax leaks into the generic representation.
    assert "cache_control" not in rendered
    assert "ephemeral" not in rendered


def test_manager_emits_boundaries_only_with_capability(tmp_path):
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ContextSection, ModelSection
    from nexus.context import ContextManager
    from nexus.model.message import Message, Text

    config = Config(
        model="anthropic/x",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="anthropic/x"),
            context=ContextSection(max_tokens=50_000, safety_margin_tokens=0),
        ),
    )
    messages = [
        Message(role="user", content=[Text(text="a")]),
        Message(role="assistant", content=[Text(text="b")]),
        Message(role="user", content=[Text(text="current")]),
    ]

    class Session:
        id = "s"

        @property
        def messages(self):
            return list(messages)

    unsupported = ContextManager(
        tmp_path, config=config, capabilities=Capabilities()
    )
    request = unsupported.assemble(Session())
    assert request.metadata["cache"] == {"enabled": False, "boundaries": []}

    supported = ContextManager(
        tmp_path, config=config, capabilities=Capabilities(prompt_caching=True)
    )
    request = supported.assemble(Session())
    assert request.metadata["cache"]["enabled"] is True
    assert request.metadata["cache"]["boundaries"] == [
        {"position": 0, "scope": "system_tools"},
        {"position": 2, "scope": "history"},
    ]


def test_atomic_write_does_not_leave_a_partial_file_on_replace_failure(tmp_path, monkeypatch):
    cache = TokenCountCache(tmp_path / "cache")
    cache.put({"seed": 1}, 1)

    original_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    cache.put({"seed": 2}, 2)  # must not raise
    monkeypatch.setattr(os, "replace", original_replace)

    leftovers = [p for p in cache.directory.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []
