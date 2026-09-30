"""Voice policy remains bounded and environment-disableable (VOICE_PLAN §5)."""
import msgspec
import pytest

from nexus.config.layers import build_v2, env_overlay_v2
from nexus.config.schema import VoiceSection
from nexus.errors import ConfigError


def test_defaults_and_disable_override():
    assert build_v2({}).voice == VoiceSection()
    assert env_overlay_v2({"NEXUS_VOICE": "off", "NEXUS_VOICE__ENABLED": "true"}) == {
        "voice": {"enabled": False}
    }


def test_environment_voice_fields_are_schema_typed():
    section = build_v2(env_overlay_v2({
        "NEXUS_VOICE__AUTO_SEND": "true",
        "NEXUS_VOICE__MAX_SECONDS": "30",
        "NEXUS_VOICE__DEVICE": "cpu",
    })).voice
    assert section.auto_send and section.max_seconds == 30 and section.device == "cpu"


@pytest.mark.parametrize("values", [
    {"max_seconds": 0}, {"max_seconds": 121}, {"max_seconds": True},
    {"unload_after_minutes": -1}, {"device": "remote"},
    {"revision": "main"}, {"model": "untrusted/model"}, {"unknown": True},
])
def test_invalid_voice_policy(values):
    with pytest.raises(ConfigError):
        build_v2({"voice": values})


def test_voice_policy_roundtrips():
    config = build_v2({"voice": {"enabled": False, "autoload": False}})
    assert msgspec.json.decode(msgspec.json.encode(config), type=type(config)) == config
