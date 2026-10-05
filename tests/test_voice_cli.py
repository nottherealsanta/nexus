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
    assert parser.parse_args(["voice"]).voice_action == "status"
    parsed = [
        parser.parse_args(["voice", action])
        for action in ("status", "init", "download", "remove")
    ]
    assert [args.voice_action for args in parsed] == ["status", "init", "download", "remove"]
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
    assert "nexus voice init" in err.getvalue()
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


def test_voice_download_preserves_host_install_guidance_once() -> None:
    message = (
        "Voice runtime is not installed. Run `nexus voice init` to install it "
        "and download the model."
    )
    out, err = io.StringIO(), io.StringIO()
    result = asyncio.run(cli._wait_voice_download(
        _VoiceClient(),
        VoiceStatusResult(state="unsupported", enabled=True, message=message),
        out, err,
    ))
    assert result == 1
    assert err.getvalue() == f"Error: {message}\n"


def test_voice_init_with_runtime_only_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus.host_support import install

    seen: list[str] = []

    async def fake_voice_command(workspace, args, stdout, stderr):
        seen.append(args.voice_action)
        return 0

    monkeypatch.setattr(install, "voice_runtime_installed", lambda: True)
    monkeypatch.setattr(install, "run_update", lambda command: pytest.fail("installed"))
    monkeypatch.setattr(cli, "_voice_command", fake_voice_command)
    out, err = io.StringIO(), io.StringIO()
    assert asyncio.run(cli._voice_init(tmp_path, out, err)) == 0
    assert seen == ["download"]
    assert "Voice runtime is installed." in out.getvalue()


def test_voice_init_installs_then_restarts_and_downloads_in_fresh_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess as sp

    from nexus.host_support import install

    commands: list[list[str]] = []
    children: list[list[str]] = []
    monkeypatch.setattr(install, "voice_runtime_installed", lambda: False)
    monkeypatch.setattr(install, "is_musl", lambda: False)
    monkeypatch.setattr(install, "install_method", lambda: "uv-tool")
    monkeypatch.setattr(install, "install_source", lambda: "pypi")
    monkeypatch.setattr(install, "installed_extras", lambda: ["documents"])
    monkeypatch.setattr(install, "find_uv", lambda environ=None: "/u/uv")
    monkeypatch.setattr(install, "package_version", lambda: "0.3.2")
    monkeypatch.setattr(install, "run_update", lambda command: commands.append(command) or 0)
    monkeypatch.setattr(
        sp, "run",
        lambda argv, **kw: children.append(argv) or sp.CompletedProcess(argv, 0),
    )
    out, err = io.StringIO(), io.StringIO()
    assert asyncio.run(cli._voice_init(tmp_path, out, err)) == 0, err.getvalue()
    assert commands[0][-1] == "nexus-harness[documents,voice]==0.3.2"
    assert "Installing the voice runtime" in out.getvalue()
    assert [argv[-2:] for argv in children] == [["daemon", "restart"], ["voice", "download"]]
    assert children[0][1:5] == ["-m", "nexus", "--workspace", str(tmp_path)]


def test_voice_init_refuses_musl_and_reports_install_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nexus.host_support import install

    monkeypatch.setattr(install, "voice_runtime_installed", lambda: False)
    monkeypatch.setattr(install, "is_musl", lambda: True)
    out, err = io.StringIO(), io.StringIO()
    assert asyncio.run(cli._voice_init(tmp_path, out, err)) == 1
    assert "musl" in err.getvalue()

    monkeypatch.setattr(install, "is_musl", lambda: False)
    monkeypatch.setattr(install, "install_method", lambda: "pip")
    monkeypatch.setattr(install, "find_uv", lambda environ=None: None)
    monkeypatch.setattr(install, "voice_requirements", lambda: ["moondream==2.4.0"])
    monkeypatch.setattr(install, "run_update", lambda command: 2)
    out, err = io.StringIO(), io.StringIO()
    assert asyncio.run(cli._voice_init(tmp_path, out, err)) == 1
    assert "-m pip install moondream==2.4.0" in out.getvalue()
    assert "status 2" in err.getvalue()
