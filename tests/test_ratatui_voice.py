"""Native capture teardown does not depend on host preview cancellation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nexus.ui.ratatui.voice import Voice
from nexus.ui_support.voice_capture import Recorder


@pytest.fixture
def voice():
    client = SimpleNamespace(voice_cancel=AsyncMock(), voice_transcribe=AsyncMock(
        return_value=SimpleNamespace(request_id="final", text="final transcript")))
    shell = SimpleNamespace(client=client, controller=SimpleNamespace(session="s"),
                            generation=0, panel_title="", composer_insert="")
    voice = Voice(shell)
    voice.recorder = SimpleNamespace(stop=Mock(return_value=b"whole recording"))
    voice.request = "final"
    voice.preview_request = "final-p1"
    voice.phase = "recording"
    voice.preview = "partial"
    return voice


@pytest.mark.parametrize("cancel_fails", [False, True])
@pytest.mark.parametrize("discard", [False, True])
async def test_stop_releases_capture_and_preserves_final_flow(voice, cancel_fails, discard):
    recorder = voice.recorder
    if cancel_fails:
        voice.shell.client.voice_cancel.side_effect = [RuntimeError("host unavailable"), None]
    await voice.stop(discard=discard, send=True)
    recorder.stop.assert_called_once_with()
    assert voice.recorder is None
    assert voice.phase == "idle" and voice.preview == ""
    if discard:
        voice.shell.client.voice_transcribe.assert_not_awaited()
        assert voice.shell.client.voice_cancel.await_args.args == ("final",)
        assert voice.shell.composer_insert == ""
    else:
        voice.shell.client.voice_transcribe.assert_awaited_once_with(
            b"whole recording", "final", session="s")
        assert voice.shell.composer_insert == "final transcript"
        assert voice.shell.composer_auto_send is True


async def test_stop_resets_state_when_recorder_cleanup_fails(voice):
    recorder = voice.recorder
    recorder.stop.side_effect = RuntimeError("device stop failed")
    voice.shell.client.voice_cancel.side_effect = RuntimeError("host unavailable")
    with pytest.raises(RuntimeError, match="device stop failed"):
        await voice.stop()
    recorder.stop.assert_called_once_with()
    assert voice.recorder is None
    assert voice.phase == "idle" and voice.preview == ""
    voice.shell.client.voice_transcribe.assert_not_awaited()


async def test_stop_releases_capture_when_preview_cancel_is_cancelled(voice):
    recorder = voice.recorder
    voice.shell.client.voice_cancel.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await voice.stop()
    recorder.stop.assert_called_once_with()
    assert voice.phase == "idle" and voice.preview == ""
    voice.shell.client.voice_transcribe.assert_not_awaited()


@pytest.mark.parametrize("stop_fails,close_fails", [(True, False), (False, True), (True, True)])
def test_recorder_always_closes_stream_and_preserves_primary_error(stop_fails, close_fails):
    recorder = Recorder(cues=False)
    stream = Mock()
    recorder._stream = stream
    if stop_fails:
        stream.stop.side_effect = RuntimeError("stop failed")
    if close_fails:
        stream.close.side_effect = RuntimeError("close failed")
    with pytest.raises(RuntimeError, match="stop failed" if stop_fails else "close failed"):
        recorder.stop()
    stream.stop.assert_called_once_with()
    stream.close.assert_called_once_with()
    assert recorder._stream is None
    assert recorder.stop().startswith(b"RIFF")
    stream.close.assert_called_once_with()


def test_recorder_normal_stop_returns_captured_audio():
    recorder = Recorder(cues=False)
    stream = Mock()
    recorder._stream = stream
    recorder._chunks.append(b"\x01\x00" * 10)
    recorder._size = 20
    assert recorder.stop().endswith(b"\x01\x00" * 10)
    stream.stop.assert_called_once_with()
    stream.close.assert_called_once_with()
    assert recorder._stream is None and recorder._size == 0
