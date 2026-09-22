"""Configuration secrets must never be revealed through repr (plan section 7)."""
import pytest

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


def test_provider_section_repr_redacts_base_url_userinfo():
    section = ProviderSection(base_url="https://user:hunter2@gateway.test/v1")
    text = repr(section)
    assert "hunter2" not in text
    assert "user" not in text
    assert "gateway.test" in text


def test_provider_section_repr_redacts_credential_patterns_in_url():
    section = ProviderSection(
        base_url="https://gateway.test/v1?api_key=sk-abcdef0123456789"
    )
    text = repr(section)
    assert "sk-abcdef0123456789" not in text


def test_provider_section_repr_redacts_command_and_args_credentials():
    section = ProviderSection(
        command=["/opt/agent", "--token", SECRET],
        args=["--api-key=" + SECRET],
    )
    text = repr(section)
    assert SECRET not in text
    assert "/opt/agent" in text


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///etc/passwd",
        "ftp://gateway.test/v1",
        "gopher://gateway.test",
        "http://remote.example.com/v1",
        "https://",
    ],
)
def test_provider_section_rejects_unsafe_base_url(base_url):
    with pytest.raises(ValueError):
        ProviderSection(base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/v1",
        "http://localhost:11434",
        "http://127.0.0.1:8080/v1",
        "http://127.1.2.3:9000",
        "http://[::1]:1234",
        "${env:MY_BASE_URL}",
    ],
)
def test_provider_section_accepts_safe_base_url(base_url):
    assert ProviderSection(base_url=base_url).base_url == base_url

