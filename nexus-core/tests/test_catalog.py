import pytest

from nexus.llmhub.catalog import models_for
from nexus.llmhub.hub import split_ref

CATALOG = {
    "openai": {"models": {
        "gpt-6.1-terra": {"name": "GPT-6.1 Terra", "tool_call": True, "reasoning": True, "release_date": "2026-09-01",
                          "modalities": {"output": ["text"]}, "limit": {"context": 400000, "output": 128000},
                          "reasoning_options": [{"type": "effort", "values": ["low", "high"]}], "cost": {"input": 1.25, "output": 10,
                          "tiers": [{"input": 2.5, "output": 20, "tier": {"type": "context", "size": 200000}},
                                    {"input": 5, "output": 40, "tier": {"type": "context", "size": 100000}}]}},
        "gpt-4.1": {"tool_call": True, "reasoning": False, "modalities": {"output": ["text"]}},
        "gpt-image-2": {"tool_call": True, "reasoning": True, "modalities": {"output": ["image"]}},
    }},
    "xai": {"models": {
        "grok-4.7": {"tool_call": True, "reasoning": True, "modalities": {"output": ["text"]},
                     "cost": {"input": 1, "tiers": [{"tier": {"type": "context", "size": 500000}}]}},
        "grok-old": {"tool_call": True, "status": "deprecated"},
        "grok-imagine-video": {"tool_call": False},
    }},
}


def test_codex_filters_openai_to_reasoning_gpt_text_models():
    [model] = models_for(CATALOG, "codex")
    assert (model.ref, model.context, model.efforts, model.input_cost) == ("codex/gpt-6.1-terra", 400000, ("low", "high"), 1.25)
    # Price tiers are prompt sizes, ascending, without duplicates.
    assert model.tiers == (100000, 200000)


def test_other_providers_keep_tool_calling_text_models():
    assert [m.id for m in models_for(CATALOG, "grok")] == ["grok-4.7"]
    assert models_for(CATALOG, "grok")[0].tiers == (500000,)
    assert models_for(CATALOG, "copilot") == []


def test_split_ref():
    assert split_ref("grok/grok-4.7") == ("grok", "grok-4.7")
    assert split_ref("gpt-6.1-terra") == ("codex", "gpt-6.1-terra")
    with pytest.raises(ValueError):
        split_ref("nope/x")
