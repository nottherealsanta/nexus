"""Host boundary checks for bounded, private voice requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.voice.audio import silence
from nexus.voice.model import TranscribeResult, VoiceState


class FakeVoice:
    def __init__(self) -> None:
        self.config = SimpleNamespace(max_seconds=120, enabled=True)
        self.state = VoiceState(state="ready", device="cpu", revision="rev")
        self.audio_seen = b""
        self.cancelled: list[str] = []
        self.partials: list[bool] = []

    def status(self) -> VoiceState:
        return self.state

    def schedule_prepare(self, force: bool = False, *, allow_download: bool = True) -> VoiceState:
        self.allow_download = allow_download
        return self.state

    async def transcribe(
        self, audio: bytes, request_id: str, *, duration_s: float, partial: bool = False
    ) -> TranscribeResult:
        self.audio_seen = audio
        self.partials.append(partial)
        return TranscribeResult("private transcript", duration_s, 0.1)

    async def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        return True

    async def remove(self) -> VoiceState:
        self.state = VoiceState(state="absent")
        return self.state


def facade_for(voice: FakeVoice) -> HostFacade:
    return HostFacade(SimpleNamespace(voice=voice))


@pytest.mark.asyncio
async def test_voice_commands_validate_audio_and_return_transcript() -> None:
    voice = FakeVoice()
    facade = facade_for(voice)
    result = await facade.handle(p.VoiceTranscribe(audio=silence(0.25), request_id="req_1"))
    assert isinstance(result, p.VoiceTranscribeResult)
    assert result.text == "private transcript"
    assert result.duration_s == 0.25
    assert voice.audio_seen.startswith(b"RIFF")

    bad = await facade.handle(p.VoiceTranscribe(audio=b"secret audio", request_id="req_2"))
    assert isinstance(bad, p.ErrorResult)
    assert bad.kind == "voice_bad_audio"
    assert "secret audio" not in bad.message


@pytest.mark.asyncio
async def test_voice_status_prepare_cancel_remove_and_doctor() -> None:
    voice = FakeVoice()
    facade = facade_for(voice)
    assert isinstance(await facade.handle(p.VoiceStatus()), p.VoiceStatusResult)
    assert isinstance(await facade.handle(p.VoicePrepare()), p.VoiceStatusResult)
    voice.state = VoiceState(state="absent", cached=True)
    status = await facade.handle(p.VoicePrepare(allow_download=False))
    assert status.cached
    assert voice.allow_download is False
    cancelled = await facade.handle(p.VoiceCancel(request_id="req_3"))
    assert isinstance(cancelled, p.VoiceCancelResult) and cancelled.cancelled
    assert voice.cancelled == ["req_3"]
    removed = await facade.handle(p.VoiceRemove())
    assert isinstance(removed, p.VoiceStatusResult) and removed.state == "absent"


def test_voice_transcribe_command_repr_redacts_audio_and_round_trips() -> None:
    command = p.VoiceTranscribe(audio=b"secret audio", request_id="safe-id")
    assert "secret audio" not in repr(command)
    decoded = p.decode_command(p.encode_command(command))
    assert isinstance(decoded, p.VoiceTranscribe)
    assert decoded.audio == b"secret audio"


@pytest.mark.asyncio
async def test_partial_voice_preview_reaches_the_manager_and_redacts_audio() -> None:
    voice = FakeVoice()
    facade = facade_for(voice)
    command = p.VoiceTranscribe(audio=silence(0.25), request_id="req_p1", partial=True)
    assert "partial=True" in repr(command) and "RIFF" not in repr(command)
    decoded = p.decode_command(p.encode_command(command))
    assert isinstance(decoded, p.VoiceTranscribe) and decoded.partial
    result = await facade.handle(decoded)
    assert isinstance(result, p.VoiceTranscribeResult)
    assert voice.partials == [True]
