"""Phase 1 ModelRouter tests (plan section 8)."""
import pytest

from nexus.core.loop import ResolvedModel
from nexus.errors import ConfigError
from nexus.model.capabilities import Capabilities
from nexus.model.request import ModelRequest
from nexus.model.router import ModelRouter

CAPS = Capabilities(
    tools=True,
    streaming=True,
    thinking=True,
    max_context_tokens=123_456,
    max_output_tokens=4096,
)


class FakeProvider:
    def __init__(self, name, capabilities=CAPS):
        self.name = name
        self._capabilities = capabilities
        self.capability_calls = []

    def capabilities(self, model):
        self.capability_calls.append(model)
        return self._capabilities


def anthropic():
    return FakeProvider("anthropic")


def test_provider_and_model_reference_resolves(tmp_path):
    provider = anthropic()
    router = ModelRouter({"anthropic": provider})

    resolved = router.resolve(
        ModelRequest(messages=[], provider="anthropic", model="claude-opus-5")
    )

    assert isinstance(resolved, ResolvedModel)
    assert resolved.provider is provider
    assert resolved.model == "claude-opus-5"
    assert resolved.capabilities is CAPS
    assert provider.capability_calls == ["claude-opus-5"]


def test_bare_model_uses_default_provider():
    provider = anthropic()
    router = ModelRouter({"anthropic": provider}, default="anthropic/claude-opus-5")

    resolved = router.resolve(ModelRequest(messages=[], model="claude-sonnet-4"))

    assert resolved.provider is provider
    assert resolved.model == "claude-sonnet-4"


def test_prefixed_model_reference_is_parsed():
    provider = anthropic()
    router = ModelRouter({"anthropic": provider})

    resolved = router.resolve(
        ModelRequest(messages=[], model="anthropic/claude-haiku")
    )

    assert resolved.provider is provider
    assert resolved.model == "claude-haiku"


def test_config_aliases_resolve():
    provider = anthropic()
    router = ModelRouter(
        {"anthropic": provider},
        aliases={
            "default": "anthropic/claude-opus-5",
            "fast": "anthropic/claude-haiku-4-5",
            "plan": "anthropic/claude-opus-5",
        },
        default="anthropic/claude-opus-5",
    )

    fast = router.resolve(ModelRequest(messages=[], model="fast"))
    plan = router.resolve(ModelRequest(messages=[], model="plan"))
    default = router.resolve(ModelRequest(messages=[]))

    assert (fast.model, plan.model, default.model) == (
        "claude-haiku-4-5",
        "claude-opus-5",
        "claude-opus-5",
    )


def test_alias_cycle_is_rejected():
    router = ModelRouter(
        {"anthropic": anthropic()},
        aliases={"a": "b", "b": "a"},
        default="a",
    )
    with pytest.raises(ConfigError, match="cycle"):
        router.resolve(ModelRequest(messages=[]))


def test_unknown_provider_is_rejected():
    router = ModelRouter({"anthropic": anthropic()}, default="anthropic/x")
    with pytest.raises(ConfigError, match="Unknown provider"):
        router.resolve(ModelRequest(messages=[], model="openai/gpt-5"))


@pytest.mark.parametrize("ref", ["anthropic/", "/claude", "//x"])
def test_malformed_reference_is_rejected(ref):
    router = ModelRouter({"anthropic": anthropic()}, default="anthropic/x")
    with pytest.raises(ConfigError, match="Malformed|missing"):
        router.resolve(ModelRequest(messages=[], model=ref))


def test_provider_without_model_is_rejected():
    router = ModelRouter({"anthropic": anthropic()}, default="anthropic/x")
    with pytest.raises(ConfigError, match="missing a model"):
        router.resolve(ModelRequest(messages=[], provider="anthropic"))


def test_missing_model_and_default_is_rejected():
    router = ModelRouter({"anthropic": anthropic()})
    with pytest.raises(ConfigError, match="No model"):
        router.resolve(ModelRequest(messages=[]))


def test_single_provider_resolves_bare_model_without_default():
    provider = anthropic()
    router = ModelRouter({"anthropic": provider})
    resolved = router.resolve(ModelRequest(messages=[], model="claude-x"))
    assert resolved.provider is provider
    assert resolved.model == "claude-x"


def test_capabilities_come_from_the_resolved_provider():
    provider = anthropic()
    router = ModelRouter({"anthropic": provider})
    router.resolve(ModelRequest(messages=[], model="anthropic/claude-x"))
    assert provider.capability_calls == ["claude-x"]


def test_agent_fallback_from_request_metadata_is_tried_before_the_global_chain():
    providers = {"a": FakeProvider("a"), "b": FakeProvider("b"), "c": FakeProvider("c")}
    router = ModelRouter(providers, default="a/m1", fallback=["c/m3"])
    request = ModelRequest(
        messages=[],
        metadata={"agent_fallback": ["b/m2", "a/m1", "c/m3", 7, "missing/x"]},
    )
    chain = [(r.provider.name, r.model) for r in router.fallbacks(request)]
    # Agent refs first, then global; the primary and duplicates are dropped and
    # malformed or unresolvable entries are skipped.
    assert chain == [("b", "m2"), ("c", "m3")]
    no_global = ModelRouter(providers, default="a/m1")
    assert [(r.provider.name, r.model) for r in no_global.fallbacks(request)] == [
        ("b", "m2"), ("c", "m3"),
    ]
    assert no_global.fallbacks(ModelRequest(messages=[])) == []
