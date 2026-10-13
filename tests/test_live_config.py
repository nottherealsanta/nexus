"""[live] stays bounded and NEXUS_LIVE=off wins (docs/config.md)."""
import msgspec
import pytest

from nexus.config.layers import build_v2, env_overlay_v2
from nexus.config.schema import LiveSection
from nexus.errors import ConfigError


def test_defaults_and_disable_override():
    section = build_v2({}).live
    assert section == LiveSection()
    assert (section.enabled, section.voice, section.max_minutes, section.carry_turns) == (True, "default", 30, 12)
    assert section.progress is True and section.instructions == "" and section.log_raw is False
    assert env_overlay_v2({"NEXUS_LIVE": "off", "NEXUS_LIVE__ENABLED": "true"}) == {"live": {"enabled": False}}


def test_environment_fields_are_schema_typed():
    section = build_v2(env_overlay_v2({
        "NEXUS_LIVE__MAX_MINUTES": "5", "NEXUS_LIVE__CARRY_TURNS": "0",
        "NEXUS_LIVE__PROGRESS": "false", "NEXUS_LIVE__VOICE": "cove",
    })).live
    assert section.max_minutes == 5 and section.carry_turns == 0 and section.progress is False
    assert section.voice == "cove"


@pytest.mark.parametrize("values", [
    {"max_minutes": 0}, {"max_minutes": 241}, {"max_minutes": True}, {"carry_turns": -1},
    {"carry_turns": 101}, {"voice": "x" * 41}, {"instructions": "a\x00b"}, {"unknown": True},
])
def test_invalid_live_policy(values):
    with pytest.raises(ConfigError):
        build_v2({"live": values})


def test_live_policy_roundtrips():
    config = build_v2({"live": {"enabled": False, "voice": "sol", "log_raw": True}})
    assert msgspec.json.decode(msgspec.json.encode(config), type=type(config)) == config
