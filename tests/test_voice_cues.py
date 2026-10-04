"""Start/stop dictation cues: bounded PCM, opt-out, never fatal."""
from nexus.ui_support import voice_capture as vc


def test_cue_pcm_is_short_and_distinct():
    start, stop = vc._cue_pcm("start"), vc._cue_pcm("stop")
    assert 0 < len(start) <= 44_100 * 2 * 0.3 and len(start) == len(stop)
    assert start != stop
    complete = vc._cue_pcm("complete")
    assert len(complete) == len(start)
    assert complete not in (start, stop)


def test_play_cue_opt_out_and_missing_device(monkeypatch):
    monkeypatch.setenv("NEXUS_VOICE_SOUNDS", "off")
    vc.play_cue("start", wait=True)
    monkeypatch.delenv("NEXUS_VOICE_SOUNDS")
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)  # import fails
    vc.play_cue("stop", wait=True)


def test_completion_cue_opt_out_is_independent(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setenv("NEXUS_COMPLETION_SOUNDS", "off")
    pcm = Mock()
    monkeypatch.setattr(vc, "_cue_pcm", pcm)
    vc.play_cue("complete", wait=True)
    pcm.assert_not_called()
