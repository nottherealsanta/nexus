from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from dataclasses import dataclass, field
from unittest.mock import Mock

import numpy as np
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
    monkeypatch.setattr(speech, "_invoke_worker", lambda text, download: {"ok": True, "backend": "paradee-cpu"})
    command = Speak("session-1")
    result = asyncio.run(speech.dispatch_speech(command, None, View([Turn(messages=[Message("assistant", "hi")])])))
    assert result == SpeakResult("Finished speaking the latest answer", "paradee-cpu")


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
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert speech._worker_synthesize("hello") == {"ok": False, "code": "speech_dependency_missing"}


class FakeSpeechModules:
    """Fake onnxruntime, misaki and huggingface_hub in sys.modules; numpy and the vocab are real."""

    def __init__(self, monkeypatch, tmp_path, *, samples=24_000):
        self.calls: dict = {"downloads": [], "feeds": []}
        self.played: list = []
        self.samples = samples
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"vocab": {c: i + 1 for i, c in enumerate("abcdefghijklmnopqrstuvwxyz ")}}))
        self.config = config
        outer = self

        class Session:
            def __init__(self, path, options, providers):
                outer.calls["session"] = (path, options.intra_op_num_threads, providers)

            def run(self, outputs, feed):
                outer.calls["feeds"].append(feed)
                return [np.zeros((1, outer.samples), dtype=np.float32)]

        class G2P:
            def __init__(self, **kwargs):
                outer.calls["g2p"] = kwargs

            def __call__(self, text):
                return text.lower().replace(".", ""), None

        def download(repo_id, filename, revision=None):
            outer.calls["downloads"].append((repo_id, filename, revision))
            return str(outer.config) if filename == "config.json" else str(tmp_path / "paradee_int8.onnx")

        ort = types.ModuleType("onnxruntime")
        ort.SessionOptions = lambda: types.SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0)
        ort.InferenceSession = Session
        misaki = types.ModuleType("misaki")
        misaki.en = types.SimpleNamespace(G2P=G2P)
        hub = types.ModuleType("huggingface_hub")
        hub.hf_hub_download = download
        sounddevice = types.ModuleType("sounddevice")
        sounddevice.play = lambda audio, samplerate, blocking: outer.played.append((audio, samplerate, blocking))
        for name, module in {"onnxruntime": ort, "misaki": misaki, "huggingface_hub": hub, "sounddevice": sounddevice}.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.setattr(speech, "_ENGINE", None)


def test_worker_synthesizes_and_plays_on_cpu(monkeypatch, tmp_path):
    fake = FakeSpeechModules(monkeypatch, tmp_path)
    assert speech._worker_synthesize("Hello") == {"ok": True, "backend": "paradee-cpu"}
    assert fake.calls["downloads"] == [
        ("sahilmahendrakar/Paradee-8M-v1.0", "onnx/paradee_int8.onnx", "v1.0"),
        ("sahilmahendrakar/Paradee-8M-v1.0", "config.json", "v1.0"),
    ]
    assert fake.calls["session"][1:] == (0, ["CPUExecutionProvider"])  # ORT default threads
    assert fake.calls["g2p"] == {"trf": False, "british": False, "fallback": None, "unk": ""}
    feed = fake.calls["feeds"][0]
    ids = feed["input_ids"][0].tolist()
    assert feed["input_ids"].dtype == np.int64 and ids[0] == ids[-1] == 0 and len(ids) == len("hello") + 2
    assert feed["speed"].tolist() == [1.0]
    [(audio, rate, blocking)] = fake.played
    assert rate == 24_000 and blocking is True and len(audio) == 24_000 and audio.dtype == np.float32


def test_worker_keeps_the_model_warm_between_chunks_and_requests(monkeypatch, tmp_path):
    fake = FakeSpeechModules(monkeypatch, tmp_path)
    speech._worker_synthesize("one")
    speech._worker_synthesize("two")
    assert len(fake.calls["downloads"]) == 2  # loaded once: two files, not four


def test_phoneme_chunks_split_on_sentences_and_stay_within_the_model_limit(monkeypatch):
    class Engine:
        def __init__(self):
            self.g2p = lambda sentence: ("ab " * 300 if "long" in sentence else "xy", None)

    chunks = list(speech._phoneme_chunks(Engine(), "Short one. A long sentence here. Last."))
    assert chunks[0] == "xy" and chunks[-1] == "xy"
    long_parts = chunks[1:-1]
    assert len(long_parts) >= 2 and all(len(part) <= speech._MAX_PHONEMES for part in chunks)


def test_worker_stop_before_playback_plays_nothing(monkeypatch, tmp_path):
    fake = FakeSpeechModules(monkeypatch, tmp_path)
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(speech, "_WORKER_STOP", stop)
    assert speech._worker_synthesize("hello") == {"ok": True, "backend": "stopped"}
    assert fake.played == []  # the stop flag is checked before each chunk is played


def test_worker_refuses_audio_over_the_cap(monkeypatch, tmp_path):
    fake = FakeSpeechModules(monkeypatch, tmp_path, samples=24_000 * 2)
    monkeypatch.setattr(speech, "_MAX_AUDIO_SECONDS", 1)
    assert speech._worker_synthesize("hello") == {"ok": False, "code": "speech_unavailable"}
    assert fake.played == []


def test_worker_offline_without_cached_spacy_model_is_refused(monkeypatch, tmp_path):
    FakeSpeechModules(monkeypatch, tmp_path)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    spacy = types.ModuleType("spacy")
    spacy.util = types.SimpleNamespace(is_package=lambda name: False)
    monkeypatch.setitem(sys.modules, "spacy", spacy)
    assert speech._worker_synthesize("hello") == {"ok": False, "code": "speech_model_not_cached"}


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


def test_cached_mode_and_download_mode_env(monkeypatch):
    monkeypatch.setenv("NEXUS_SPEAK_DEVICE", "mps")  # retired: no device choice any more
    assert speech._worker_environment(False)["HF_HUB_OFFLINE"] == "1"
    env = speech._worker_environment(True)
    assert env["HF_HUB_OFFLINE"] == "0"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert "PYTORCH_ENABLE_MPS_FALLBACK" not in env


async def test_host_routes_speak_to_the_latest_completed_answer(tmp_path, monkeypatch):
    """HostFacade -> dispatch_speech with the session's real folded view."""
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ModelSection
    from nexus.host import HostFacade
    from nexus.model.providers.scripted import ScriptedProvider, text_response
    from nexus.runtime import Runtime

    spoken = []
    monkeypatch.setattr(speech, "_invoke_worker", lambda text, download: spoken.append((text, download)) or {"ok": True, "backend": "paradee-cpu"})
    config = Config(model="scripted/m", version=2, v2=ConfigV2(model=ModelSection(default="scripted/m")))
    runtime = Runtime(tmp_path, config=config, providers={"scripted": ScriptedProvider(text_response("the final answer"))})
    facade = HostFacade(runtime)
    facade.open_session("s")
    early = await facade.handle(p.Speak(session_id="s"))
    assert isinstance(early, p.ErrorResult) and early.kind == "speech_no_answer"
    await facade.start_turn("s", "hello")
    await facade.wait_idle(timeout=5.0)
    result = await facade.handle(p.Speak(session_id="s", download=True))
    assert result == p.SpeakResult(message="Finished speaking the latest answer", backend="paradee-cpu")
    assert spoken == [("the final answer", True)]
    await facade.shutdown()
    await runtime.aclose()


# -- model status and the one-time download -----------------------------------


def _fake_cache(tmp_path, monkeypatch, *, model=True, config=True, phonemizer=True, blob_bytes=0, consent=False):
    repo = tmp_path / "hub" / "models--sahilmahendrakar--Paradee-8M-v1.0"
    (repo / "blobs").mkdir(parents=True, exist_ok=True)
    snapshot = repo / "snapshots" / "abc"
    (snapshot / "onnx").mkdir(parents=True, exist_ok=True)
    if model:
        (snapshot / "onnx" / "paradee_int8.onnx").write_bytes(b"m")
    if config:
        (snapshot / "config.json").write_text("{}")
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
    if not consent:
        monkeypatch.setattr(speech, "has_consent", lambda: False)


def _no_threads(monkeypatch):
    started = []

    class Thread:
        def __init__(self, target, **kwargs):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(speech.threading, "Thread", Thread)
    return started


def test_status_reports_missing_packages_with_the_install_hint(monkeypatch):
    monkeypatch.setattr(speech, "_missing_packages", lambda: ["onnxruntime", "sounddevice"])
    status = speech.speech_status()
    assert status["state"] == "unsupported"
    assert "onnxruntime, sounddevice" in status["message"] and "uv sync --extra speak" in status["message"]


def test_status_absent_downloading_ready_and_error(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, model=False, config=False, blob_bytes=2_500_000)
    status = speech.speech_status()
    assert status["state"] == "absent" and status["bytes_total"] == speech.MODEL_BYTES == 25_000_000
    assert status["bytes_done"] == 2_500_000 and round(status["progress"], 2) == 0.1

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
    _fake_cache(tmp_path, monkeypatch, model=False, config=False)
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
    monkeypatch.setattr(speech, "_missing_packages", lambda: ["onnxruntime"])
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


def test_worker_prepare_downloads_both_files_and_loads_the_model(monkeypatch, tmp_path):
    fake = FakeSpeechModules(monkeypatch, tmp_path)
    assert speech._worker_prepare() == {"ok": True}
    assert [name for _repo, name, _rev in fake.calls["downloads"]] == ["onnx/paradee_int8.onnx", "config.json"]
    monkeypatch.setitem(sys.modules, "misaki", None)
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
        self.stdout = types.SimpleNamespace(readline=lambda: '{"ok":true,"backend":"paradee-cpu"}\n')
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


def test_earlier_consent_downloads_a_missing_model_without_asking(tmp_path, monkeypatch):
    # An upgrade from Kokoro: the old model is cached, Paradee and the phonemizer are not.
    _fake_cache(tmp_path, monkeypatch, model=False, config=False, phonemizer=False, consent=True)
    (tmp_path / "hub" / "models--hexgrad--Kokoro-82M" / "snapshots" / "x").mkdir(parents=True)
    started = _no_threads(monkeypatch)
    assert speech.has_consent()
    assert speech.speech_status()["state"] == "downloading"
    assert speech.speech_status()["state"] == "downloading" and len(started) == 1


def test_without_consent_status_only_reports_absent(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, model=False, config=False, consent=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    started = _no_threads(monkeypatch)
    assert not speech.has_consent()
    assert speech.speech_status()["state"] == "absent" and started == []


def test_a_failed_automatic_download_is_not_retried_in_a_loop(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, model=False, config=False, consent=True)
    started = _no_threads(monkeypatch)
    speech._PREPARE.update(state="error", message="The speech model could not be downloaded.")
    assert speech.speech_status()["state"] == "error" and started == []


def test_prepare_records_consent_under_nexus_home(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, model=False, config=False, consent=True)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    _no_threads(monkeypatch)
    assert not speech.has_consent()
    speech.schedule_prepare()
    assert (speech._speech_home() / "consent").exists() and speech.has_consent()


def test_phonemizer_under_nexus_home_counts_as_cached(tmp_path, monkeypatch):
    _fake_cache(tmp_path, monkeypatch, phonemizer=False)
    assert speech.speech_status()["state"] == "absent"
    (speech._phonemizer_dir() / "en_core_web_sm").mkdir(parents=True)
    assert speech.speech_status()["state"] == "ready"


def _fake_spacy(monkeypatch, installed):
    spacy = types.ModuleType("spacy")
    spacy.util = types.SimpleNamespace(is_package=lambda name: installed())
    spacy.about = types.SimpleNamespace(__download_url__="https://github.com/explosion/spacy-models/releases/download")
    download = types.ModuleType("spacy.cli.download")
    download.get_compatibility = lambda: {}
    download.get_version = lambda name, compat: "3.8.0"
    download.get_model_filename = lambda name, version, sdist: f"{name}-{version}/{name}-{version}-py3-none-any.whl"
    cli = types.ModuleType("spacy.cli")
    cli.download = download
    spacy.cli = cli
    for name, module in {"spacy": spacy, "spacy.cli": cli, "spacy.cli.download": download}.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_offline_worker_never_downloads_the_phonemizer(monkeypatch):
    _fake_spacy(monkeypatch, lambda: False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(speech, "_download_phonemizer", lambda target: pytest.fail("must not download"))
    with pytest.raises(speech._SpeechRequestError):
        speech._ensure_phonemizer()


def test_phonemizer_wheel_is_unpacked_under_nexus_home(monkeypatch):
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("en_core_web_sm/__init__.py", "")
        archive.writestr("en_core_web_sm-3.8.0.dist-info/METADATA", "Name: en_core_web_sm\n")
    payload = buffer.getvalue()
    urls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    _fake_spacy(monkeypatch, lambda: False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: urls.append(url) or Response(payload))
    monkeypatch.setattr(sys, "path", list(sys.path))
    speech._ensure_phonemizer()
    target = speech._phonemizer_dir()
    assert urls == ["https://github.com/explosion/spacy-models/releases/download/"
                    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"]
    assert (target / "en_core_web_sm" / "__init__.py").exists() and str(target) in sys.path
    assert not (target.parent / "python.partial").exists()
