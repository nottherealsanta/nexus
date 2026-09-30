"""Focused coverage for the daemon-backed voice CLI surface."""
from __future__ import annotations

import argparse
import asyncio
import io
from pathlib import Path

import pytest

from nexus import cli
from nexus.client.protocol import FacadeError
from nexus.host.protocol import VoiceStatusResult, VoiceTranscribeResult
from nexus.voice.audio import silence


def test_voice_parser_actions_and_file_argument() -> None:
    parser = cli.build_parser()
    parsed = [
        parser.parse_args(["voice", action])
        for action in ("status", "download", "remove")
    ]
    assert [args.voice_action for args in parsed] == ["status", "download", "remove"]
    transcribe = parser.parse_args(["voice", "transcribe", "input.wav"])
    assert transcribe.voice_action == "transcribe"
    assert transcribe.file == "input.wav"
    with pytest.raises(SystemExit):
        parser.parse_args(["voice", "transcribe"])


class _VoiceClient:
    def __init__(self, *, status: VoiceStatusResult | None = None) -> None:
        self.status = status or VoiceStatusResult(state="ready", enabled=True, max_seconds=1)
        self.calls: list[str] = []
        self.transcript = "recognized words"

    async def voice_status(self) -> VoiceStatusResult:
        self.calls.append("status")
        return self.status

    async def voice_prepare(self) -> VoiceStatusResult:
        self.calls.append("prepare")
        return VoiceStatusResult(state="downloading", enabled=True, progress=0.2)

    async def voice_remove(self) -> VoiceStatusResult:
        self.calls.append("remove")
        return VoiceStatusResult(state="absent", enabled=True)

    async def voice_transcribe(self, audio: bytes, request_id: str) -> VoiceTranscribeResult:
        self.calls.append("transcribe")
        assert audio == silence(0.2)
        assert request_id
        return VoiceTranscribeResult(
            request_id=request_id,
            text=self.transcript,
            duration_s=0.2,
            elapsed_s=0.1,
        )


@pytest.mark.parametrize(
    ("action", "calls", "return_code"),
    [
        ("status", ["status"], 0),
        ("download", ["prepare", "status"], 0),
        ("remove", ["remove"], 0),
    ],
)
def test_voice_lifecycle_dispatch(action: str, calls: list[str], return_code: int) -> None:
    client = _VoiceClient()
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(voice_action=action)
    assert asyncio.run(cli._dispatch_voice(client, args, out, err)) == return_code
    assert client.calls == calls
    assert "Error:" not in err.getvalue()
    assert "state:" in out.getvalue()


def test_voice_download_waits_for_terminal_ready_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class ScriptedVoiceClient(_VoiceClient):
        def __init__(self) -> None:
            super().__init__()
            self.statuses = [
                VoiceStatusResult(state="downloading", enabled=True, progress=0.21),
                VoiceStatusResult(state="downloading", enabled=True, progress=0.29),
                VoiceStatusResult(state="loading", enabled=True, progress=0.9),
                VoiceStatusResult(state="ready", enabled=True, progress=1.0),
            ]

        async def voice_prepare(self) -> VoiceStatusResult:
            self.calls.append("prepare")
            return self.statuses.pop(0)

        async def voice_status(self) -> VoiceStatusResult:
            self.calls.append("status")
            return self.statuses.pop(0)

    monkeypatch.setattr(cli, "_VOICE_POLL_SECONDS", 0)
    client = ScriptedVoiceClient()
    out, err = io.StringIO(), io.StringIO()

    result = asyncio.run(
        cli._dispatch_voice(
            client, argparse.Namespace(voice_action="download"), out, err
        )
    )

    assert result == 0
    assert client.calls == ["prepare", "status", "status", "status"]
    assert "state: ready" in out.getvalue()
    # Same state/progress bucket does not repeatedly print progress.
    assert err.getvalue().count("Preparing local voice model") == 2


def test_voice_download_unsupported_is_actionable_and_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedVoiceClient(_VoiceClient):
        async def voice_prepare(self) -> VoiceStatusResult:
            self.calls.append("prepare")
            return VoiceStatusResult(state="downloading", enabled=True)

        async def voice_status(self) -> VoiceStatusResult:
            self.calls.append("status")
            return VoiceStatusResult(
                state="unsupported", enabled=True, message="Runtime unavailable"
            )

    monkeypatch.setattr(cli, "_VOICE_POLL_SECONDS", 0)
    client = UnsupportedVoiceClient()
    out, err = io.StringIO(), io.StringIO()

    result = asyncio.run(
        cli._dispatch_voice(
            client, argparse.Namespace(voice_action="download"), out, err
        )
    )

    assert result == 1
    assert client.calls == ["prepare", "status"]
    assert "state: unsupported" in out.getvalue()
    assert "Runtime unavailable" in err.getvalue()
    assert "nexus-harness[voice]" in err.getvalue()
    assert "nexus daemon restart" in err.getvalue()


@pytest.mark.parametrize("state", ["error", "disabled"])
def test_voice_download_stops_on_terminal_failure(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    class FailedVoiceClient(_VoiceClient):
        async def voice_prepare(self) -> VoiceStatusResult:
            self.calls.append("prepare")
            return VoiceStatusResult(state="loading", enabled=True)

        async def voice_status(self) -> VoiceStatusResult:
            self.calls.append("status")
            return VoiceStatusResult(state=state, enabled=state != "disabled")

    monkeypatch.setattr(cli, "_VOICE_POLL_SECONDS", 0)
    client = FailedVoiceClient()
    out, err = io.StringIO(), io.StringIO()

    result = asyncio.run(
        cli._dispatch_voice(
            client, argparse.Namespace(voice_action="download"), out, err
        )
    )

    assert result == 1
    assert client.calls == ["prepare", "status"]
    assert f"state: {state}" in out.getvalue()


def test_voice_transcribe_outputs_only_text_and_does_not_prepare(tmp_path: Path) -> None:
    audio_file = tmp_path / "input.wav"
    audio_file.write_bytes(silence(0.2))
    client = _VoiceClient()
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(voice_action="transcribe", file=str(audio_file))

    result = asyncio.run(cli._dispatch_voice(client, args, out, err))

    assert result == 0
    assert client.calls == ["status", "transcribe"]
    assert out.getvalue() == "recognized words\n"
    assert err.getvalue() == ""


def test_voice_transcribe_rejects_oversized_file_before_rpc(tmp_path: Path) -> None:
    audio_file = tmp_path / "oversized.wav"
    audio_file.write_bytes(b"x" * (32_000 + cli._VOICE_WAV_HEADER_ALLOWANCE + 1))
    client = _VoiceClient()
    out, err = io.StringIO(), io.StringIO()

    result = asyncio.run(
        cli._dispatch_voice(
            client,
            argparse.Namespace(voice_action="transcribe", file=str(audio_file)),
            out,
            err,
        )
    )

    assert result == 1
    assert client.calls == ["status"]
    assert out.getvalue() == ""
    assert "size limit" in err.getvalue()


def test_voice_transcribe_missing_cache_reports_daemon_error_without_download(
    tmp_path: Path,
) -> None:
    audio_file = tmp_path / "input.wav"
    audio_file.write_bytes(silence(0.2))

    class NotReadyClient(_VoiceClient):
        async def voice_transcribe(self, audio: bytes, request_id: str) -> VoiceTranscribeResult:
            self.calls.append("transcribe")
            raise FacadeError("voice_not_ready", "Voice model is not ready")

    client = NotReadyClient()
    out, err = io.StringIO(), io.StringIO()
    result = asyncio.run(
        cli._dispatch_voice(
            client,
            argparse.Namespace(voice_action="transcribe", file=str(audio_file)),
            out,
            err,
        )
    )

    assert result == 1
    assert client.calls == ["status", "transcribe"]
    assert out.getvalue() == ""
    assert "Voice model is not ready" in err.getvalue()


def test_voice_error_redacts_paths_and_doctor_prints_voice_status() -> None:
    report = {
        "workspace": "workspace",
        "voice": {
            "state": "error",
            "enabled": True,
            "progress": 0.5,
            "message": "failed at /private/user/model",
        },
    }
    out = io.StringIO()

    cli._print_doctor(report, out)

    assert "voice: state=error enabled=yes progress=50%" in out.getvalue()
    assert "failed at [path]" in out.getvalue()
    assert "/private/user/model" not in out.getvalue()
