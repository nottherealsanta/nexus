"""Bounded, isolated Paradee speech for completed assistant turns.

Paradee is an 8M-parameter int8 ONNX text-to-speech model distilled from Kokoro-82M
(voice af_heart). It runs on CPU through onnxruntime; phonemes come from misaki, the
same G2P Kokoro uses. No torch is involved. The worker downloads two files from the
Hugging Face Hub (the model and its config.json vocabulary), reads the text one
sentence at a time and plays each chunk as it is synthesized.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..host import protocol as p

_MAX_TEXT_CHARS = 20_000
_MAX_AUDIO_SECONDS = 180
_WORKER_TIMEOUT_SECONDS = 300
_SAMPLE_RATE = 24_000
_MAX_PHONEMES = 510  # the model has 512 positions: one pad token at each end
_LOCK = threading.Lock()
#: The warm synthesis worker: started by the first ``/speak``, kept loaded while the
#: user keeps using it, and unloaded after this many idle seconds.
IDLE_UNLOAD_SECONDS = 600
_WORKER: subprocess.Popen[str] | None = None
_WORKER_DOWNLOAD = False
_IDLE_TIMER: threading.Timer | None = None
_STOPPED = threading.Event()

_REPO_ID = "sahilmahendrakar/Paradee-8M-v1.0"
_REVISION = "v1.0"
_MODEL_FILE = "onnx/paradee_int8.onnx"  # 9 MB int8 graph: token ids in, 24 kHz waveform out
_CONFIG_FILE = "config.json"  # Kokoro's phoneme vocabulary and the sample rate
#: The model files (~9.5 MB) plus the spaCy English package misaki uses (en_core_web_sm, ~13 MB).
MODEL_BYTES = 25_000_000
_PREPARE_TIMEOUT_SECONDS = 1800
_REPO_DIR = "models--sahilmahendrakar--Paradee-8M-v1.0"
_INSTALL_HINT = (
    "Install the speak extra in the daemon's environment (uv sync --extra speak), plus "
    "portaudio (brew install portaudio), then restart the daemon."
)


class _SpeechRequestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _assistant_text(view: Any) -> str:
    """Return only the latest completed root turn's final assistant text."""
    turns = view.turns
    if getattr(view, "active_turn", None) is not None or any(turn.phase == "active" for turn in turns):
        raise _SpeechRequestError("speech_turn_active", "Wait for the current turn to finish")
    # Turn.agent is root-agent metadata, not a child marker. Child transcripts
    # are separate views and never appear in this session's turns.
    completed = next((turn for turn in reversed(turns) if turn.phase == "completed"), None)
    if completed is None:
        raise _SpeechRequestError("speech_no_answer", "There is no completed answer to speak")
    assistants = [message for message in getattr(completed, "messages", ())
                  if getattr(message, "role", None) == "assistant"]
    if not assistants:
        raise _SpeechRequestError("speech_no_answer", "There is no completed answer to speak")
    final = assistants[-1]
    if not getattr(final, "done", False) or getattr(final, "stop_reason", None) in {"tool_use", "tool_calls"}:
        raise _SpeechRequestError("speech_no_answer", "There is no completed answer to speak")
    # MessageView stores content in typed blocks. Only ordinary text blocks
    # are speakable; thinking and tool-related content must not be included.
    blocks = getattr(final, "blocks", None)
    if blocks is not None:
        text = "".join(
            block_text
            for block in blocks
            if getattr(block, "kind", None) == "text"
            and isinstance((block_text := getattr(block, "text", None)), str)
            and getattr(block, "finalized", True)
        )
    else:
        # Small compatibility seam for view fakes and the host's future view API.
        text = getattr(final, "text", "")
    if not isinstance(text, str) or not text.strip():
        raise _SpeechRequestError("speech_no_answer", "The final answer has no text to speak")
    if len(text) > _MAX_TEXT_CHARS:
        raise _SpeechRequestError("speech_text_too_long", "The answer is too long to speak (20,000 character limit)")
    return text


def _worker_environment(download: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["HF_HUB_OFFLINE"] = "0" if download else "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return env


def _start_worker(download: bool) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "nexus.host_support.speech", "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=_worker_environment(download),
        )
    except Exception:
        raise _SpeechRequestError("speech_unavailable", "Speech worker could not be started") from None


def _kill_worker() -> None:
    """Unload: drop the warm worker (and its model memory). Caller holds ``_LOCK`` or is the idle timer."""
    global _WORKER, _IDLE_TIMER
    worker, _WORKER = _WORKER, None
    if _IDLE_TIMER is not None:
        _IDLE_TIMER.cancel()
        _IDLE_TIMER = None
    if worker is not None and worker.poll() is None:
        worker.kill()
        with contextlib.suppress(Exception):
            worker.communicate(timeout=5)


def _unload_if_idle() -> None:
    if _LOCK.acquire(blocking=False):
        try:
            _kill_worker()
        finally:
            _LOCK.release()


def _arm_idle_timer() -> None:
    global _IDLE_TIMER
    if _IDLE_TIMER is not None:
        _IDLE_TIMER.cancel()
    _IDLE_TIMER = threading.Timer(IDLE_UNLOAD_SECONDS, _unload_if_idle)
    _IDLE_TIMER.daemon = True
    _IDLE_TIMER.start()


def _invoke_worker(text: str, download: bool) -> dict[str, Any]:
    """Speak through the warm worker, starting it on first use (loaded once, unloaded when idle)."""
    global _WORKER, _WORKER_DOWNLOAD
    if not _LOCK.acquire(blocking=False):
        raise _SpeechRequestError("speech_busy", "Speech is already playing")
    try:
        if _IDLE_TIMER is not None:
            _IDLE_TIMER.cancel()
        if _WORKER is not None and (_WORKER.poll() is not None or _WORKER_DOWNLOAD != download):
            _kill_worker()
        if _WORKER is None:
            _WORKER, _WORKER_DOWNLOAD = _start_worker(download), download
        worker = _WORKER
        watchdog = threading.Timer(_WORKER_TIMEOUT_SECONDS, worker.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            worker.stdin.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
            worker.stdin.flush()
            line = worker.stdout.readline()
        except Exception:
            line = ""
        finally:
            watchdog.cancel()
        if not line:
            timed_out = worker.poll() is not None and worker.returncode == -9 and not _STOPPED.is_set()
            _kill_worker()
            if timed_out:
                raise _SpeechRequestError("speech_timeout", "Speech generation timed out")
            raise _SpeechRequestError("speech_unavailable", "Speech worker failed")
        try:
            result = json.loads(line)
        except (TypeError, ValueError):
            _kill_worker()
            raise _SpeechRequestError("speech_unavailable", "Speech worker returned an invalid response") from None
        if not isinstance(result, dict):
            _kill_worker()
            raise _SpeechRequestError("speech_unavailable", "Speech worker returned an invalid response")
        if result.get("ok") is not True:
            _kill_worker()  # a failed load must not leave a half-initialised worker
        else:
            _arm_idle_timer()
        return result
    finally:
        _STOPPED.clear()
        _LOCK.release()


def stop_speaking() -> bool:
    """Ask the warm worker to stop playing (it stays loaded); ``False`` when idle."""
    worker = _WORKER
    if not _LOCK.locked() or worker is None or worker.poll() is not None:
        return False
    _STOPPED.set()
    try:
        worker.stdin.write("stop\n")
        worker.stdin.flush()
    except Exception:
        worker.kill()
    return True


# -- model status and one-time preparation ----------------------------------
#
# Mirrors the voice model flow (``/voice download``): the clients ask for the
# status, show the size and ask for consent, call ``SpeechPrepare`` and poll.
# Status never imports onnxruntime: it looks for the packages and the cache
# files. Preparation is the only network step, and it runs in the same isolated
# worker as synthesis.
#
# Everything downloaded must survive ``nexus update``, which replaces the tool's
# virtual environment: the model lives in the Hugging Face cache and spaCy's
# English package is unpacked under ``~/.nexus/models/speech/python`` (never
# pip-installed into the venv). Consent is durable too: once the user agreed to
# a download (recorded in ``~/.nexus/models/speech/consent``, or implied by an
# earlier Kokoro or Paradee download), a missing or newer model is fetched
# again without asking.

_PREPARE: dict[str, Any] = {"state": "idle", "message": "", "started": 0.0}
_PREPARE_LOCK = threading.Lock()
_PHONEMIZER = "en_core_web_sm"
_PHONEMIZER_MAX_BYTES = 200_000_000
_LEGACY_REPO_DIR = "models--hexgrad--Kokoro-82M"  # the pre-Paradee model; its presence is earlier consent


def _speech_home() -> Path:
    from ..config.paths import nexus_home

    return nexus_home() / "models" / "speech"


def _phonemizer_dir() -> Path:
    return _speech_home() / "python"


def _hub_cache() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    return (Path(home) if home else Path.home() / ".cache" / "huggingface") / "hub"


def _missing_packages() -> list[str]:
    names = ("onnxruntime", "misaki", "sounddevice", "numpy", "huggingface_hub")
    try:
        return [name for name in names if importlib.util.find_spec(name) is None]
    except (ImportError, ValueError):
        return list(names)


def _phonemizer_present() -> bool:
    if (_phonemizer_dir() / _PHONEMIZER).is_dir():
        return True
    try:  # an older install pip-installed it into the venv
        return importlib.util.find_spec(_PHONEMIZER) is not None
    except (ImportError, ValueError):
        return False


def _tree_bytes(root: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for path in root.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file():
                    total += path.stat().st_size
    return total


def _cache_progress() -> tuple[bool, int]:
    """``(everything cached, bytes on disk so far)`` without importing any model code."""
    repo = _hub_cache() / _REPO_DIR
    done = _tree_bytes(repo / "blobs") + _tree_bytes(_phonemizer_dir())
    model = config = False
    with contextlib.suppress(OSError):
        for snapshot in (repo / "snapshots").iterdir():
            model = model or (snapshot / _MODEL_FILE).exists()
            config = config or (snapshot / _CONFIG_FILE).exists()
    return model and config and _phonemizer_present(), done


def has_consent() -> bool:
    """Whether the user already agreed to a speech download on this machine.

    True after any ``SpeechPrepare``, or when an earlier Kokoro or Paradee model is
    in the Hugging Face cache (those were only ever fetched with consent).
    """
    if (_speech_home() / "consent").exists():
        return True
    hub = _hub_cache()
    return any((hub / name / "snapshots").is_dir() for name in (_REPO_DIR, _LEGACY_REPO_DIR))


def _record_consent() -> None:
    with contextlib.suppress(OSError):
        home = _speech_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "consent").write_text("speech model download accepted\n", encoding="utf-8")


def speech_status() -> dict[str, Any]:
    """Bounded model status: ``state`` is unsupported, absent, downloading, ready or error.

    A model that is missing after the user once consented (an upgrade, a new
    model, a cleared cache) starts downloading here, with no second prompt.
    """
    missing = _missing_packages()
    ready, done = _cache_progress()
    with _PREPARE_LOCK:
        prepare = dict(_PREPARE)
    base = {"bytes_done": min(done, MODEL_BYTES), "bytes_total": MODEL_BYTES}
    if missing:
        return {**base, "state": "unsupported", "progress": 0.0,
                "message": f"Missing {', '.join(missing)}. {_INSTALL_HINT}"}
    if not ready and prepare["state"] == "idle" and has_consent():
        _start_prepare()
        prepare["state"] = "downloading"
    if prepare["state"] == "downloading":
        return {**base, "state": "downloading", "progress": min(0.99, done / MODEL_BYTES),
                "message": "Downloading the local speech model…"}
    if ready:
        return {**base, "state": "ready", "progress": 1.0, "message": "The local speech model is available."}
    if prepare["state"] == "error":
        return {**base, "state": "error", "progress": 0.0, "message": prepare["message"]}
    return {**base, "state": "absent", "progress": min(0.99, done / MODEL_BYTES),
            "message": "The speech model is not downloaded yet."}


def _prepare_worker() -> None:
    """Download in the isolated worker (thread body); never raises."""
    message = ""
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "nexus.host_support.speech"],
            input=json.dumps({"mode": "prepare"}),
            text=True, capture_output=True, timeout=_PREPARE_TIMEOUT_SECONDS,
            env=_worker_environment(True), check=False,
        )
        result = json.loads(completed.stdout) if completed.returncode == 0 else {}
        if isinstance(result, dict) and result.get("ok") is True:
            state = "idle"
        else:
            state, message = "error", "The speech model could not be downloaded. Check the network and retry."
    except subprocess.TimeoutExpired:
        state, message = "error", "The speech model download timed out. Retry to resume it."
    except Exception:
        state, message = "error", "The speech model could not be downloaded."
    with _PREPARE_LOCK:
        _PREPARE.update(state=state, message=message)


def _start_prepare() -> None:
    with _PREPARE_LOCK:
        if _PREPARE["state"] == "downloading":
            return
        _PREPARE.update(state="downloading", message="", started=time.time())
    threading.Thread(target=_prepare_worker, name="speech-prepare", daemon=True).start()


def schedule_prepare() -> dict[str, Any]:
    """Record consent and start the download if it is needed and not already running."""
    _record_consent()
    if not _missing_packages() and not _cache_progress()[0]:
        _start_prepare()
    return speech_status()


async def dispatch_speech(command: Any, runtime: Any, view: Any) -> p.Result | None:
    """Speak a completed answer; optional speech dependencies stay subprocess-only."""
    if isinstance(command, p.SpeechStatus):
        return p.SpeechStatusResult(**speech_status())
    if isinstance(command, p.SpeechPrepare):
        return p.SpeechStatusResult(**schedule_prepare())
    if isinstance(command, getattr(p, "SpeakStop", ())):
        return p.SpeakResult(message="Stopped speaking" if stop_speaking() else "", backend="")
    speak_type = getattr(p, "Speak", ())
    if not speak_type or not isinstance(command, speak_type):
        return None
    try:
        session_id = getattr(command, "session_id", "")
        view_session_id = getattr(view, "session_id", getattr(view, "id", None))
        if view_session_id is not None and session_id != view_session_id:
            raise _SpeechRequestError("speech_no_answer", "The requested session is unavailable")
        text = _assistant_text(view)
        result = await asyncio.to_thread(_invoke_worker, text, bool(command.download))
        if result.get("ok") is not True:
            code = result.get("code")
            allowed = {
                "speech_dependency_missing", "speech_model_not_cached",
                "speech_model_unavailable", "speech_playback_failed",
                "speech_unavailable",
            }
            if code not in allowed:
                code = "speech_unavailable"
            messages = {
                "speech_dependency_missing": "Install the speak extra (onnxruntime, misaki, sounddevice) to use speech",
                "speech_model_not_cached": "Speech model is not cached; run /speak download first",
                "speech_model_unavailable": "Speech model could not be loaded",
                "speech_playback_failed": "Audio playback failed",
                "speech_unavailable": "Speech could not be completed",
            }
            return p.ErrorResult(kind=code, message=messages[code])
        backend = result.get("backend")
        if backend == "stopped":
            return p.SpeakResult(message="Stopped speaking", backend="")
        if backend not in {"paradee-cpu"}:
            return p.ErrorResult(kind="speech_unavailable", message="Speech worker returned an invalid response")
        return p.SpeakResult(message="Finished speaking the latest answer", backend=backend)
    except _SpeechRequestError as exc:
        return p.ErrorResult(kind=exc.code, message=exc.message)
    except Exception:
        # The host boundary deliberately does not forward implementation errors.
        return p.ErrorResult(kind="speech_unavailable", message="Speech request failed")


class _Engine:
    """A loaded Paradee model: the onnxruntime session, misaki G2P and the phoneme vocabulary."""

    def __init__(self, session: Any, g2p: Any, vocab: dict[str, int]) -> None:
        self.session = session
        self.g2p = g2p
        self.vocab = vocab


_ENGINE: _Engine | None = None  # warm: stays loaded between requests in the serve loop
_WORKER_STOP = threading.Event()  # set by the serve loop's reader when the parent sends "stop"


def _load_engine() -> _Engine:
    """Fetch (respecting HF_HUB_OFFLINE) and load the model, the vocabulary and the phonemizer."""
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    _ensure_phonemizer()
    from misaki import en

    model_path = hf_hub_download(_REPO_ID, _MODEL_FILE, revision=_REVISION)
    config_path = hf_hub_download(_REPO_ID, _CONFIG_FILE, revision=_REVISION)
    with open(config_path, encoding="utf-8") as handle:
        vocab = json.load(handle)["vocab"]
    options = ort.SessionOptions()
    # ORT's default intra-op threads: measured ~34x real time on an M4 vs ~18x with one thread.
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])
    # fallback=None: no espeak. Words misaki cannot place are dropped by the vocabulary filter.
    g2p = en.G2P(trf=False, british=False, fallback=None, unk="")
    return _Engine(session, g2p, vocab)


def _ensure_phonemizer() -> None:
    """Make spaCy's English package importable, downloading it under ``~/.nexus`` when online.

    It is unpacked from spaCy's official wheel into ``~/.nexus/models/speech/python``
    rather than pip-installed: uv tool environments have no pip, and ``nexus update``
    replaces the environment, which would drop the package and ask again.
    """
    target = _phonemizer_dir()
    _add_to_path(target)
    import spacy

    if spacy.util.is_package(_PHONEMIZER):
        return
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        raise _SpeechRequestError("speech_model_not_cached", "The English phonemizer is not downloaded")
    _download_phonemizer(target)
    _add_to_path(target)


def _add_to_path(target: Path) -> None:
    if target.is_dir() and str(target) not in sys.path:
        sys.path.insert(0, str(target))
        importlib.invalidate_caches()


def _download_phonemizer(target: Path) -> None:
    """Fetch the spaCy-compatible ``en_core_web_sm`` wheel and unpack it into ``target`` (bounded)."""
    import shutil
    import urllib.request
    import zipfile

    from spacy import about
    from spacy.cli.download import get_compatibility, get_model_filename, get_version

    base = about.__download_url__.rstrip("/") + "/"
    url = base + get_model_filename(_PHONEMIZER, get_version(_PHONEMIZER, get_compatibility()), False)
    if not url.startswith(base) or ".." in url[len(base):]:
        raise ValueError("unexpected phonemizer download URL")
    staging = target.parent / "python.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        wheel = staging / "phonemizer.whl"
        with urllib.request.urlopen(url, timeout=60) as response, wheel.open("wb") as out:
            written = 0
            while chunk := response.read(1 << 20):
                written += len(chunk)
                if written > _PHONEMIZER_MAX_BYTES:
                    raise ValueError("phonemizer download is too large")
                out.write(chunk)
        unpacked = staging / "python"
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                    raise ValueError("unsafe path in the phonemizer wheel")
            archive.extractall(unpacked)
        shutil.rmtree(target, ignore_errors=True)
        unpacked.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _phoneme_chunks(engine: _Engine, text: str):
    """Yield phoneme strings, one sentence at a time, each at most ``_MAX_PHONEMES`` long."""
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text.strip()):
        if not sentence.strip():
            continue
        phonemes, _ = engine.g2p(sentence)
        while len(phonemes) > _MAX_PHONEMES:  # a very long sentence: cut at the last space that fits
            cut = phonemes.rfind(" ", 0, _MAX_PHONEMES)
            cut = cut if cut > 0 else _MAX_PHONEMES
            yield phonemes[:cut]
            phonemes = phonemes[cut:].lstrip()
        if phonemes:
            yield phonemes


def _synthesize_chunk(engine: _Engine, phonemes: str, np: Any) -> Any:
    ids = [0] + [engine.vocab[c] for c in phonemes if c in engine.vocab][:_MAX_PHONEMES] + [0]
    feed = {"input_ids": np.array([ids], dtype=np.int64), "speed": np.array([1.0], dtype=np.float32)}
    return engine.session.run(None, feed)[0][0]


def _worker_synthesize(text: str) -> dict[str, Any]:
    """Synthesize and play in this child process; dependencies load on demand."""
    try:
        import numpy as np
        import sounddevice
        import onnxruntime  # noqa: F401  (checked here so a missing package reports as such)
        import misaki  # noqa: F401
        import huggingface_hub  # noqa: F401
    except ImportError:
        return {"ok": False, "code": "speech_dependency_missing"}

    global _ENGINE
    try:
        if _ENGINE is None:
            # Offline (the default) never downloads: a missing phonemizer or model
            # raises and is reported as not cached.
            _ENGINE = _load_engine()
    except Exception:
        code = "speech_model_not_cached" if os.environ.get("HF_HUB_OFFLINE") == "1" else "speech_model_unavailable"
        return {"ok": False, "code": code}

    return _worker_play(_ENGINE, text, np, sounddevice)


def _worker_play(engine: _Engine, text: str, np: Any, sounddevice: Any) -> dict[str, Any]:
    total_seconds = 0.0
    chunks = _phoneme_chunks(engine, text)
    while True:
        try:
            phonemes = next(chunks, None)
            if phonemes is None:
                break
            audio = _synthesize_chunk(engine, phonemes, np)
        except Exception:
            return {"ok": False, "code": "speech_unavailable"}
        if _WORKER_STOP.is_set():
            return {"ok": True, "backend": "stopped"}
        total_seconds += float(len(audio)) / _SAMPLE_RATE
        if total_seconds > _MAX_AUDIO_SECONDS:
            return {"ok": False, "code": "speech_unavailable"}
        try:
            with contextlib.redirect_stdout(sys.stderr):
                sounddevice.play(np.asarray(audio, dtype=np.float32), samplerate=_SAMPLE_RATE, blocking=True)
        except Exception:
            return {"ok": False, "code": "speech_playback_failed"}
        if _WORKER_STOP.is_set():
            return {"ok": True, "backend": "stopped"}
    return {"ok": True, "backend": "paradee-cpu"}


def _worker_prepare() -> dict[str, Any]:
    """Fetch the model, its config and the English phonemizer (network on)."""
    try:
        import onnxruntime  # noqa: F401
        import misaki  # noqa: F401
        import huggingface_hub  # noqa: F401
    except ImportError:
        return {"ok": False, "code": "speech_dependency_missing"}
    try:
        _load_engine()
    except Exception:
        return {"ok": False, "code": "speech_model_unavailable"}
    return {"ok": True}


def _worker_main() -> int:
    """Worker stdin/stdout protocol: bounded JSON in, safe status JSON out."""
    try:
        raw = sys.stdin.read(_MAX_TEXT_CHARS * 4 + 1024)
        if len(raw) > _MAX_TEXT_CHARS * 4:
            result = {"ok": False, "code": "speech_unavailable"}
        elif isinstance(request := json.loads(raw), dict) and request.get("mode") == "prepare":
            with contextlib.redirect_stdout(sys.stderr):
                result = _worker_prepare()
        else:
            text = request.get("text") if isinstance(request, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS:
                result = {"ok": False, "code": "speech_unavailable"}
            else:
                with contextlib.redirect_stdout(sys.stderr):
                    result = _worker_synthesize(text)
    except Exception:
        result = {"ok": False, "code": "speech_unavailable"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.flush()
    return 0


def _worker_serve() -> int:
    """Persistent worker: one JSON request per stdin line, one JSON result per stdout line.

    The model loads on the first request and stays loaded. A ``stop`` line (read by a
    side thread, so it works mid-playback) interrupts the current answer only.
    """
    import queue

    out = sys.stdout
    requests: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            if line.strip() == "stop":
                _WORKER_STOP.set()
                with contextlib.suppress(Exception):
                    import sounddevice
                    sounddevice.stop()
            else:
                _WORKER_STOP.clear()  # before queueing, so a later stop is never lost
                requests.put(line)
        requests.put(None)  # parent closed stdin: exit

    threading.Thread(target=reader, daemon=True).start()
    while (line := requests.get()) is not None:
        try:
            request = json.loads(line)
            text = request.get("text") if isinstance(request, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS:
                result = {"ok": False, "code": "speech_unavailable"}
            else:
                with contextlib.redirect_stdout(sys.stderr):
                    result = _worker_synthesize(text)
        except Exception:
            result = {"ok": False, "code": "speech_unavailable"}
        out.write(json.dumps(result, separators=(",", ":")) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_serve() if "--serve" in sys.argv[1:] else _worker_main())
