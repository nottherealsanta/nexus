import pytest

from nexus.llmhub.anthropic_policy import image_limit, model_version, thinking_policy


@pytest.mark.parametrize("alias,canonical", [
    ("claude-3-7-sonnet-20250219", "claude-sonnet-3-7"),
    ("claude-3.7-sonnet", "claude-sonnet-3-7"),
    ("claude-sonnet-4-20250514", "claude-sonnet-4"),
    ("claude-opus-45", "claude-opus-4-5"),
    ("claude-sonnet-46", "claude-sonnet-4-6"),
    ("claude-opus-47-fast", "claude-opus-4-7"),
    ("claude-haiku-55", "claude-haiku-5-5"),
    ("claude-fable-51", "claude-fable-5-1"),
])
def test_aliases_have_identical_thinking_and_image_policies(alias, canonical):
    assert model_version(alias) == model_version(canonical)
    assert thinking_policy(alias) == thinking_policy(canonical)
    assert image_limit(alias) == image_limit(canonical)


@pytest.mark.parametrize("model", ["not-claude-sonnet-4-6", "claude-sonnet-4-6evil", "claude-3-7-unknown", "unknown"])
def test_unrecognized_models_do_not_guess_capabilities(model):
    assert model_version(model) is None
    assert thinking_policy(model).mode == "none"
    assert image_limit(model) == 100
