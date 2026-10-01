"""Redacted host projection and dispatch for local voice commands (VOICE_PLAN §6)."""

from __future__ import annotations

import re
from typing import Any

from ..voice.audio import VoiceAudioError, parse_wav
from ..voice.model import VoiceError
from ..host import protocol as p

_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_VOICE_ERRORS = {
    "voice_unavailable", "voice_not_ready", "voice_busy", "voice_too_long",
    "voice_bad_audio", "voice_timeout",
}


def voice_status_result(manager: Any) -> p.VoiceStatusResult:
    state = manager.status()
    config = getattr(manager, "config", None)
    max_seconds = getattr(config, "max_seconds", 120)
    return p.VoiceStatusResult(
        state=str(state.state), progress=float(state.progress),
        bytes_done=int(state.bytes_done), bytes_total=int(state.bytes_total),
        device=str(state.device)[:64], revision=str(state.revision)[:128],
        message=str(state.message)[:240], max_seconds=int(max_seconds),
        enabled=str(state.state) != "disabled",
        auto_send=bool(getattr(config, "auto_send", False)),
        configured_device=str(getattr(config, "device", "auto")),
    )


def doctor_voice(runtime: Any) -> dict[str, Any]:
    manager = getattr(runtime, "voice", None)
    if manager is None:
        return {"state": "unsupported", "enabled": False}
    result = voice_status_result(manager)
    report = {name: getattr(result, name) for name in (
        "state", "progress", "bytes_done", "bytes_total", "device",
        "revision", "message", "max_seconds", "enabled",
    )}
    report["cache_path"] = str(getattr(getattr(manager, "store", None), "path", ""))
    report["real_time_factor"] = getattr(manager, "real_time_factor", None)
    return report


async def dispatch_voice(command: Any, runtime: Any) -> p.Result | None:
    """Handle voice requests without exposing model exceptions or audio content."""
    if not isinstance(command, (p.VoiceStatus, p.VoicePrepare, p.VoiceTranscribe, p.VoiceCancel, p.VoiceRemove)):
        return None
    manager = getattr(runtime, "voice", None)
    if manager is None:
        return p.ErrorResult(kind="voice_unavailable", message="Voice input is unavailable")
    if isinstance(command, p.VoiceStatus):
        return voice_status_result(manager)
    if isinstance(command, p.VoicePrepare):
        manager.schedule_prepare(force=command.force)
        # schedule_prepare updates the snapshot synchronously.
        return voice_status_result(manager)
    if isinstance(command, p.VoiceTranscribe):
        if not _REQUEST_ID.fullmatch(command.request_id):
            return p.ErrorResult(kind="voice_bad_audio", message="Invalid voice request")
        try:
            limit = min(120, max(1, int(getattr(manager.config, "max_seconds", 120))))
            info = parse_wav(command.audio, max_seconds=limit)
            result = await manager.transcribe(
                command.audio, command.request_id, duration_s=info.duration_s, partial=command.partial
            )
            return p.VoiceTranscribeResult(
                request_id=command.request_id, text=str(result.text)[:100_000],
                duration_s=float(result.duration_s), elapsed_s=float(result.elapsed_s),
                language=str(result.language)[:64],
            )
        except (VoiceAudioError, VoiceError) as exc:
            code = getattr(exc, "code", "voice_bad_audio")
            if code not in _VOICE_ERRORS:
                code = "voice_unavailable"
            message = str(exc) if isinstance(exc, (VoiceAudioError, VoiceError)) else "Voice request failed"
            return p.ErrorResult(kind=code, message=message[:240])
        except Exception:
            return p.ErrorResult(kind="voice_unavailable", message="Voice request failed")
    if isinstance(command, p.VoiceCancel):
        if not _REQUEST_ID.fullmatch(command.request_id):
            return p.ErrorResult(kind="voice_bad_audio", message="Invalid voice request")
        return p.VoiceCancelResult(cancelled=await manager.cancel(command.request_id))
    try:
        await manager.remove()
    except VoiceError as exc:
        code = exc.code if exc.code in _VOICE_ERRORS else "voice_unavailable"
        return p.ErrorResult(kind=code, message=str(exc)[:240])
    except Exception:
        return p.ErrorResult(kind="voice_unavailable", message="Voice model could not be removed")
    return voice_status_result(manager)
