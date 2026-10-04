import pytest

from nexus.ui_support.speech_settings import (
    SPEECH_DEFAULTS,
    _set_speech_toml_value,
    compatible_voices,
    read_speech_settings,
    reset_speech_config,
    set_speech_config,
)


def test_speech_choices_and_language_pairing():
    assert compatible_voices("a") == ("af_heart", "af_bella", "af_nicole", "am_adam", "am_michael")
    assert compatible_voices("b") == ("bf_emma", "bm_george")


def test_toml_updates_preserve_other_sections_and_comments():
    original = '# managed\n[model]\nname = "local"\n\n[speech]\nvoice = "af_heart" # note\nlanguage = "a"\n\n[other]\nvalue = true\n'
    updated = _set_speech_toml_value(original, "speed", 1.25)
    assert '[model]\nname = "local"' in updated
    assert '[speech]\nvoice = "af_heart" # note\nlanguage = "a"\nspeed = 1.25' in updated
    assert '[other]\nvalue = true' in updated
    assert _set_speech_toml_value(updated, "device", "mps").count("[speech]") == 1


def test_invalid_choice_does_not_modify_body():
    with pytest.raises(ValueError):
        _set_speech_toml_value("[speech]\nvoice = 'af_heart'\n", "voice", "bf_emma")


class FakeClient:
    def __init__(self, body=""):
        from types import SimpleNamespace
        self.body = body
        self.sha = "0"
        self.writes = 0
        self.SimpleNamespace = SimpleNamespace

    async def settings_read(self, scope, category, item):
        assert (scope, category, item) == ("global", "config", "config")
        return self.SimpleNamespace(body=self.body, sha256=self.sha)

    async def settings_write(self, scope, category, item, body, expected):
        assert expected == self.sha
        self.body = body
        self.sha = str(int(self.sha) + 1)
        self.writes += 1
        return self.SimpleNamespace(status="saved")


@pytest.mark.asyncio
async def test_host_settings_persist_and_reset_only_speech_section():
    client = FakeClient('[model]\nname = "test"\n')
    await set_speech_config(client, language="b")
    values = await read_speech_settings(client)
    assert values == {**SPEECH_DEFAULTS, "language": "b", "voice": "bf_emma"}
    assert '[model]\nname = "test"' in client.body
    await reset_speech_config(client)
    assert await read_speech_settings(client) == SPEECH_DEFAULTS
