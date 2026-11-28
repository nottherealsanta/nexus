import pytest

from nexus.llmhub.anthropic import DirectRequest, build_body
from nexus.llmhub.types import Message, ProviderError, Text, ToolSpec


def body(instructions, boundary, *, retention="long"):
    req = DirectRequest("claude-haiku-4-5", instructions, (Message("user", (Text("go"),)),),
                        (ToolSpec("probe", "", {"type": "object"}),),
                        cache_retention=retention, stable_system_chars=boundary)
    return build_body(req, origin="claude-direct")


def test_volatile_suffix_changes_do_not_change_stable_system_cache_block():
    first = body("stable\nvolatile A", 7)
    second = body("stable\nvolatile B", 7)
    assert first["system"][0] == second["system"][0] == {
        "type": "text", "text": "stable\n", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    assert "cache_control" not in first["system"][1]
    assert "".join(block["text"] for block in first["system"]) == "stable\nvolatile A"
    assert sum("cache_control" in block for group in (first["system"], first["tools"],
        *(m["content"] for m in first["messages"])) for block in group) <= 4


@pytest.mark.parametrize("boundary", [0, 3, None])
def test_edge_boundaries_preserve_all_instruction_text(boundary):
    result = body("abc", boundary)
    assert "".join(block["text"] for block in result["system"]) == "abc"
    assert len(result["system"]) == 1
    assert ("cache_control" in result["system"][0]) == (boundary != 0)


def test_boundary_counts_unicode_characters_and_disabled_cache_stays_disabled():
    result = body("こんにちは suffix", 5, retention="none")
    assert [block["text"] for block in result["system"]] == ["こんにちは", " suffix"]
    assert all("cache_control" not in block for block in result["system"])


@pytest.mark.parametrize("boundary", [-1, 4, True, 1.5, "2"])
def test_invalid_boundaries_fail_without_clipping(boundary):
    with pytest.raises(ProviderError, match="instruction character boundary"):
        body("abc", boundary)
