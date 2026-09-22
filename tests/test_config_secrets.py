"""Configuration secrets must never be revealed through repr (plan section 7)."""
from nexus.config import Config
from nexus.config.schema import ConfigV2, ProviderSection

SECRET = "sk-literal-secret-value"


def test_provider_section_repr_redacts_api_key():
    section = ProviderSection(api_key=SECRET, kind="anthropic")
    text = repr(section)
    assert SECRET not in text
    assert "***" in text


def test_provider_section_repr_redacts_env_reference_too():
    section = ProviderSection(api_key="${env:ANTHROPIC_API_KEY}")
    text = repr(section)
    assert "ANTHROPIC_API_KEY" not in text
    assert "***" in text


def test_config_v2_repr_does_not_reveal_literal_key():
    v2 = ConfigV2(providers={"anthropic": ProviderSection(api_key=SECRET)})
    assert SECRET not in repr(v2)


def test_config_facade_repr_hides_provider_sections():
    config = Config(
        model="anthropic/x",
        version=2,
        v2=ConfigV2(providers={"anthropic": ProviderSection(api_key=SECRET)}),
    )
    assert SECRET not in repr(config)
