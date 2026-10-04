from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import Mock

import pytest

from nexus.host_support import speech
from nexus.host import protocol as p


@dataclass
class Message:
    role: str
    text: str = ""
    done: bool = True


@dataclass
class Turn:
    phase: str = "completed"
    messages: list[Message] = field(default_factory=list)


@dataclass
class View:
    turns: list[Turn]
    session_id: str = "session-1"
    active_turn: Turn | None = None


def test_selects_latest_assistant_text_from_latest_completed_turn():
    view = View([
        Turn(messages=[Message("assistant", "older answer")]),
        Turn(messages=[
            Message("assistant", "tool preamble"),
            Message("tool", "tool output"),
            Message("assistant", "final answer"),
        ]),
    ])
    assert speech._assistant_text(view) == "final answer"


def test_typed_message_uses_text_blocks_but_excludes_thinking():
    @dataclass
    class Block:
        kind: str
        text: str

    message = types.SimpleNamespace(
        role="assistant", done=True,
        blocks=[Block("thinking", "private reasoning"), Block("text", "final answer")],
    )
    assert speech._assistant_text(View([Turn(messages=[message])])) == "final answer"


def test_latest_completed_root_turn_is_selected_not_older_turn():
    view = View([
        Turn(messages=[Message("assistant", "older answer")]),
        Turn(messages=[Message("assistant", "  ")]),
    ])
    with pytest.raises(speech._SpeechRequestError) as error:
        speech._assistant_text(view)
    assert error.value.code == "speech_no_answer"


def test_active_turn_rejects_even_if_previous_answer_exists():
    view = View([Turn(messages=[Message("assistant", "previous")])], active_turn=Turn())
    with pytest.raises(speech._SpeechRequestError) as error:
        speech._assistant_text(view)
    assert error.value.code == "speech_turn_active"


@pytest.mark.parametrize("turns", [[], [Turn(phase="failed", messages=[Message("assistant", "failed")])]])
def test_requires_completed_turn(turns):
    with pytest.raises(speech._SpeechRequestError) as error:
        speech._assistant_text(View(turns))
    assert error.value.code == "speech_no_answer"


def test_requires_text_on_last_assistant_not_prior_assistant():
    view = View([Turn(messages=[Message("assistant", "answer"), Message("assistant", "  ")])])
    with pytest.raises(speech._SpeechRequestError) as error:
        speech._assistant_text(view)
    assert error.value.code == "speech_no_answer"


def test_rejects_text_over_limit_without_truncating():
    with pytest.raises(speech._SpeechRequestError) as error:
        speech._assistant_text(View([Turn(messages=[Message("assistant", "x" * 20_001)])]))
    assert error.value.code == "speech_text_too_long"


def test_dispatch_uses_worker_and_returns_backend(monkeypatch):
    @dataclass
    class Speak:
        session_id: str
        download: bool = False

    @dataclass
    class SpeakResult:
        message: str
        backend: str

    monkeypatch.setattr(p, "Speak", Speak, raising=False)
    monkeypatch.setattr(p, "SpeakResult", SpeakResult, raising=False)
    monkeypatch.setattr(speech, "_invoke_worker", lambda text, download: {"ok": True, "backend": "kokoro-cpu"})
    command = Speak("session-1")
    result = asyncio.run(speech.dispatch_speech(command, None, View([Turn(messages=[Message("assistant", "hi")])])))
    assert result == SpeakResult("Finished speaking the latest answer", "kokoro-cpu")


def test_dispatch_does_not_leak_worker_exceptions(monkeypatch):
    @dataclass
    class Speak:
        session_id: str
        download: bool = False

    monkeypatch.setattr(p, "Speak", Speak, raising=False)

    def explode(_text, _download):
        raise RuntimeError("private worker detail")

    monkeypatch.setattr(speech, "_invoke_worker", explode)
    result = asyncio.run(speech.dispatch_speech(Speak("session-1"), None, View([Turn(messages=[Message("assistant", "hi")])])))
    assert isinstance(result, p.ErrorResult)
    assert "private worker detail" not in result.message


def test_worker_reports_missing_optional_dependencies(monkeypatch):
    # The worker must not import optional modules at module import time.
    monkeypatch.setitem(sys.modules, "kokoro", None)
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert speech._worker_synthesize("hello") == {"ok": False, "code": "speech_dependency_missing"}


def test_worker_synthesizes_and_plays_on_cpu(monkeypatch):
    played = []

    class Pipeline:
        def __init__(self, **kwargs):
            assert kwargs == {"lang_code": "a", "repo_id": "hexgrad/Kokoro-82M", "device": "cpu"}

        def __call__(self, text, voice):
            assert text == "hello"
            assert voice == "af_heart"
            yield "hello", "hello", [0.0] * 24_000

    fake_kokoro = types.ModuleType("kokoro")
    fake_kokoro.KPipeline = Pipeline
    fake_numpy = types.ModuleType("numpy")
    fake_numpy.asarray = lambda audio: audio
    fake_sounddevice = types.ModuleType("sounddevice")
    fake_sounddevice.play = lambda audio, samplerate, blocking: played.append((len(audio), samplerate, blocking))
    monkeypatch.setitem(sys.modules, "kokoro", fake_kokoro)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)
    monkeypatch.setenv("NEXUS_SPEAK_DEVICE", "cpu")
    assert speech._worker_synthesize("hello") == {"ok": True, "backend": "kokoro-cpu"}
    assert played == [(24_000, 24_000, True)]


def test_worker_stdin_protocol_is_bounded_and_redacts_exceptions(monkeypatch):
    class Input:
        def read(self, size):
            assert size == speech._MAX_TEXT_CHARS * 4 + 1024
            return json.dumps({"text": "hello"})

    output = Mock()
    output.write = Mock()
    output.flush = Mock()
    monkeypatch.setattr(speech.sys, "stdin", Input())
    monkeypatch.setattr(speech.sys, "stdout", output)
    monkeypatch.setattr(speech, "_worker_synthesize", lambda _text: (_ for _ in ()).throw(RuntimeError("secret")))
    assert speech._worker_main() == 0
    output.write.assert_called_once_with('{"ok":false,"code":"speech_unavailable"}')


def test_cached_mode_and_download_mode_env():
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("NEXUS_SPEAK_DEVICE", "mps")
        assert speech._worker_environment(False)["HF_HUB_OFFLINE"] == "1"
        env = speech._worker_environment(True)
        assert env["HF_HUB_OFFLINE"] == "0"
        assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
        assert env["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
    finally:
        monkeypatch.undo()


async def test_host_routes_speak_to_the_latest_completed_answer(tmp_path, monkeypatch):
    """HostFacade -> dispatch_speech with the session's real folded view."""
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ModelSection
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime

    spoken = []
    monkeypatch.setattr(speech, "_invoke_worker", lambda text, download: spoken.append((text, download)) or {"ok": True, "backend": "kokoro-cpu"})
    config = Config(model="scripted/m", version=2, v2=ConfigV2(model=ModelSection(default="scripted/m")))
    runtime = Runtime(tmp_path, config=config, providers={"scripted": ScriptedProvider(text_response("the final answer"))})
    facade = HostFacade(runtime)
    facade.open_session("s")
    early = await facade.handle(p.Speak(session_id="s"))
    assert isinstance(early, p.ErrorResult) and early.kind == "speech_no_answer"
    await facade.start_turn("s", "hello")
    await facade.wait_idle(timeout=5.0)
    result = await facade.handle(p.Speak(session_id="s", download=True))
    assert result == p.SpeakResult(message="Finished speaking the latest answer", backend="kokoro-cpu")
    assert spoken == [("the final answer", True)]
    await facade.shutdown()
    await runtime.aclose()


# -- model status and the one-time download -----------------------------------


def _fake_cache(tmp_path, monkeypatch, *, weights=True, voice=True, phonemizer=True, blob_bytes=0):
    repo = tmp_path / "hub" / "models--hexgrad--Kokoro-82M"
    (repo / "blobs").mkdir(parents=True, exist_ok=True)
    snapshot = repo / "snapshots" / "abc"
    (snapshot / "voices").mkdir(parents=True, exist_ok=True)
    if weights:
        (snapshot / "kokoro-v1_0.pth").write_bytes(b"w")
    if voice:
        (snapshot / "voices" / "af_heart.pt").write_bytes(b"v")
    if blob_bytes:
        (repo / "blobs" / "partial.incomplete").write_bytes(b"x" * blob_bytes)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(speech, "_missing_packages", lambda: [])
    real = speech.importlib.util.find_spec
    monkeypatch.setattr(
        speech.importlib.util, "find_spec",
        lambda name, *a: (object() if phonemizer else None) if name == "en_core_web_sm" else real(name, *a),
    )
    monkeypatch.setattr(speech, "_PREPARE", {"state": "idle", "message": "", "started": 0.0})


def test_status_reports_missing_packages_with_the_install_hint(monkeypatch):
    monkeypatch.setattr(speech, "_missing_packages", lambda: ["kokoro", "sounddevice"])
    status = speech.speech_status()
    assert status["state"] == "unsupported"
    assert "kokoro, sounddevice" in status["message"] and "uv sync --extra speak" in status["message"]


def test_status_absent_downloading_ready_and_error(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, weights=False, voice=False, blob_bytes=34_500_000)
    status = speech.speech_status()
    assert status["state"] == "absent" and status["bytes_total"] == speech.MODEL_BYTES
    assert status["bytes_done"] == 34_500_000 and round(status["progress"], 2) == 0.1

    speech._PREPARE.update(state="downloading")
    status = speech.speech_status()
    assert status["state"] == "downloading" and 0.0 < status["progress"] < 1.0

    speech._PREPARE.update(state="error", message="The speech model could not be downloaded.")
    assert speech.speech_status()["state"] == "error"

    _fake_cache(tmp_path, monkeypatch)
    ready = speech.speech_status()
    assert ready["state"] == "ready" and ready["progress"] == 1.0


def test_status_needs_the_phonemizer_too(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, phonemizer=False)
    assert speech.speech_status()["state"] == "absent"


def test_prepare_starts_one_background_download_and_never_twice(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, weights=False, voice=False)
    started = []

    class Thread:
        def __init__(self, target, **kwargs):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(speech.threading, "Thread", Thread)
    first = speech.schedule_prepare()
    second = speech.schedule_prepare()
    assert first["state"] == second["state"] == "downloading" and len(started) == 1


def test_prepare_does_nothing_when_ready_or_unsupported(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(speech.threading, "Thread", lambda *a, **k: pytest.fail("must not download"))
    assert speech.schedule_prepare()["state"] == "ready"
    monkeypatch.setattr(speech, "_missing_packages", lambda: ["kokoro"])
    assert speech.schedule_prepare()["state"] == "unsupported"


def test_prepare_worker_runs_online_and_records_the_outcome(monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen["env"], seen["input"] = kwargs["env"], json.loads(kwargs["input"])
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true}')

    monkeypatch.setattr(speech.subprocess, "run", run)
    monkeypatch.setattr(speech, "_PREPARE", {"state": "downloading", "message": "", "started": 0.0})
    speech._prepare_worker()
    assert seen["env"]["HF_HUB_OFFLINE"] == "0" and seen["input"] == {"mode": "prepare"}
    assert speech._PREPARE["state"] == "idle"

    monkeypatch.setattr(speech.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    speech._prepare_worker()
    assert speech._PREPARE["state"] == "error" and "could not be downloaded" in speech._PREPARE["message"]


def test_worker_prepare_loads_the_pipeline_and_voice(monkeypatch):
    calls = []

    class Pipeline:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def load_voice(self, voice):
            calls.append(("voice", voice))

    fake = types.ModuleType("kokoro")
    fake.KPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "kokoro", fake)
    assert speech._worker_prepare() == {"ok": True}
    assert calls == [("init", {"lang_code": "a", "repo_id": "hexgrad/Kokoro-82M", "device": "cpu"}), ("voice", "af_heart")]
    monkeypatch.setitem(sys.modules, "kokoro", None)
    assert speech._worker_prepare() == {"ok": False, "code": "speech_dependency_missing"}


def test_worker_main_routes_the_prepare_mode(monkeypatch):
    class Input:
        def read(self, size):
            return json.dumps({"mode": "prepare"})

    output = Mock()
    monkeypatch.setattr(speech.sys, "stdin", Input())
    monkeypatch.setattr(speech.sys, "stdout", output)
    monkeypatch.setattr(speech, "_worker_prepare", lambda: {"ok": True})
    assert speech._worker_main() == 0
    output.write.assert_called_once_with('{"ok":true}')


async def test_host_dispatches_status_and_prepare(monkeypatch):
    monkeypatch.setattr(speech, "speech_status", lambda: {"state": "absent", "bytes_total": 5})
    monkeypatch.setattr(speech, "schedule_prepare", lambda: {"state": "downloading", "bytes_total": 5})
    assert await speech.dispatch_speech(p.SpeechStatus(), None, None) == p.SpeechStatusResult(state="absent", bytes_total=5)
    assert (await speech.dispatch_speech(p.SpeechPrepare(), None, None)).state == "downloading"


class _FakeWorker:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args, self.returncode, self.lines = args, None, []
        self.stdin = types.SimpleNamespace(write=self.lines.append, flush=lambda: None)
        self.stdout = types.SimpleNamespace(readline=lambda: '{"ok":true,"backend":"kokoro-cpu"}\n')
        _FakeWorker.instances.append(self)

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def communicate(self, timeout=None):
        return "", ""


def test_the_worker_stays_loaded_between_requests_and_unloads_when_idle(monkeypatch):
    _FakeWorker.instances.clear()
    monkeypatch.setattr(speech.subprocess, "Popen", _FakeWorker)
    monkeypatch.setattr(speech, "_WORKER", None)
    assert speech.IDLE_UNLOAD_SECONDS == 600
    speech._invoke_worker("one", False)
    speech._invoke_worker("two", False)
    assert len(_FakeWorker.instances) == 1  # warm: one process for both answers
    assert "--serve" in _FakeWorker.instances[0].args[0]
    assert speech.stop_speaking() is False  # nothing is playing
    speech._unload_if_idle()  # what the 10-minute timer runs
    assert speech._WORKER is None and _FakeWorker.instances[0].returncode == -9
    speech._invoke_worker("three", False)
    assert len(_FakeWorker.instances) == 2  # reloaded on the next use
    speech._kill_worker()
