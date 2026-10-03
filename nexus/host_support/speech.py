"""Bounded, isolated Kokoro speech for completed assistant turns."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
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
_DEFAULT_VOICE = "af_heart"
_LOCK = threading.Lock()
#: The warm synthesis worker: started by the first ``/speak``, kept loaded while the
#: user keeps using it, and unloaded after this many idle seconds.
IDLE_UNLOAD_SECONDS = 600
_WORKER: subprocess.Popen[str] | None = None
_WORKER_DOWNLOAD = False
_IDLE_TIMER: threading.Timer | None = None
_STOPPED = threading.Event()

#: Kokoro-82M weights (~327 MB) plus the voice and the spaCy English package.
MODEL_BYTES = 345_000_000
_PREPARE_TIMEOUT_SECONDS = 1800
_REPO_DIR = "models--hexgrad--Kokoro-82M"
_INSTALL_HINT = (
    "Install the speak extra in the daemon's environment (uv sync --extra speak), plus "
    "espeak-ng and portaudio (brew install espeak-ng portaudio), then restart the daemon."
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
    # The worker repeats this before importing torch, but setting it here also
    # ensures any imported startup code sees the intended opt-in fallback.
    if env.get("NEXUS_SPEAK_DEVICE") == "mps":
        env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
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
# Status never imports torch: it looks for the packages and the Hugging Face
# cache files. Preparation is the only network step, and it runs in the same
# isolated worker as synthesis.

_PREPARE: dict[str, Any] = {"state": "idle", "message": "", "started": 0.0}
_PREPARE_LOCK = threading.Lock()


def _hub_cache() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    return (Path(home) if home else Path.home() / ".cache" / "huggingface") / "hub"


def _missing_packages() -> list[str]:
    names = ("kokoro", "sounddevice", "numpy")
    try:
        return [name for name in names if importlib.util.find_spec(name) is None]
    except (ImportError, ValueError):
        return list(names)


def _cache_progress() -> tuple[bool, int]:
    """``(everything cached, bytes on disk so far)`` without importing any model code."""
    repo = _hub_cache() / _REPO_DIR
    done = 0
    with contextlib.suppress(OSError):
        for blob in (repo / "blobs").iterdir():
            with contextlib.suppress(OSError):
                done += blob.stat().st_size
    weights = voice = False
    with contextlib.suppress(OSError):
        for snapshot in (repo / "snapshots").iterdir():
            weights = weights or (snapshot / "kokoro-v1_0.pth").exists()
            voice = voice or (snapshot / "voices" / f"{_DEFAULT_VOICE}.pt").exists()
    try:
        phonemizer = importlib.util.find_spec("en_core_web_sm") is not None
    except (ImportError, ValueError):
        phonemizer = False
    return weights and voice and phonemizer, done


def speech_status() -> dict[str, Any]:
    """Bounded model status: ``state`` is unsupported, absent, downloading, ready or error."""
    missing = _missing_packages()
    ready, done = _cache_progress()
    with _PREPARE_LOCK:
        prepare = dict(_PREPARE)
    base = {"bytes_done": min(done, MODEL_BYTES), "bytes_total": MODEL_BYTES}
    if missing:
        return {**base, "state": "unsupported", "progress": 0.0,
                "message": f"Missing {', '.join(missing)}. {_INSTALL_HINT}"}
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


def schedule_prepare() -> dict[str, Any]:
    """Start the one-time download if it is needed and not already running."""
    status = speech_status()
    if status["state"] in {"unsupported", "ready", "downloading"}:
        return status
    with _PREPARE_LOCK:
        _PREPARE.update(state="downloading", message="", started=time.time())
    threading.Thread(target=_prepare_worker, name="speech-prepare", daemon=True).start()
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
                "speech_dependency_missing": "Install Kokoro and sounddevice to use speech",
                "speech_model_not_cached": "Speech model is not cached; run /speak download first",
                "speech_model_unavailable": "Speech model could not be loaded",
                "speech_playback_failed": "Audio playback failed",
                "speech_unavailable": "Speech could not be completed",
            }
            return p.ErrorResult(kind=code, message=messages[code])
        backend = result.get("backend")
        if backend == "stopped":
            return p.SpeakResult(message="Stopped speaking", backend="")
        if backend not in {"kokoro-cpu", "kokoro-mps"}:
            return p.ErrorResult(kind="speech_unavailable", message="Speech worker returned an invalid response")
        return p.SpeakResult(message="Finished speaking the latest answer", backend=backend)
    except _SpeechRequestError as exc:
        return p.ErrorResult(kind=exc.code, message=exc.message)
    except Exception:
        # The host boundary deliberately does not forward implementation errors.
        return p.ErrorResult(kind="speech_unavailable", message="Speech request failed")


def _worker_synthesize(text: str) -> dict[str, Any]:
    """Synthesize and play in this child process; dependencies load on demand."""
    device = "mps" if os.environ.get("NEXUS_SPEAK_DEVICE") == "mps" else "cpu"
    if device == "mps":
        os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
    try:
        from kokoro import KPipeline
        import numpy as np
        import sounddevice
    except ImportError:
        return {"ok": False, "code": "speech_dependency_missing"}

    try:
        if device in _PIPELINES:  # warm: the model stays loaded between requests
            return _worker_play(_PIPELINES[device], text, device, np, sounddevice)
        # English default voice/pipeline; Kokoro assets resolve from the official
        # Hugging Face repository and respect HF_HUB_OFFLINE in this process.
        # Misaki downloads spaCy's English package outside Hugging Face. Prevent
        # this separate network path unless download was explicitly requested.
        if os.environ.get("HF_HUB_OFFLINE") == "1":
            import spacy

            if not spacy.util.is_package("en_core_web_sm"):
                return {"ok": False, "code": "speech_model_not_cached"}
        pipeline = _PIPELINES[device] = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=device)
    except Exception:
        code = "speech_model_not_cached" if os.environ.get("HF_HUB_OFFLINE") == "1" else "speech_model_unavailable"
        return {"ok": False, "code": code}

    return _worker_play(pipeline, text, device, np, sounddevice)


_PIPELINES: dict[str, Any] = {}
_WORKER_STOP = threading.Event()  # set by the serve loop's reader when the parent sends "stop"


def _worker_play(pipeline: Any, text: str, device: str, np: Any, sounddevice: Any) -> dict[str, Any]:
    total_seconds = 0.0
    try:
        for _graphemes, _phonemes, audio in pipeline(text, voice=_DEFAULT_VOICE):
            try:
                seconds = float(len(audio)) / 24_000
            except Exception:
                return {"ok": False, "code": "speech_unavailable"}
            if _WORKER_STOP.is_set():
                return {"ok": True, "backend": "stopped"}
            total_seconds += seconds
            if total_seconds > _MAX_AUDIO_SECONDS:
                return {"ok": False, "code": "speech_unavailable"}
            with contextlib.redirect_stdout(sys.stderr):
                sounddevice.play(np.asarray(audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio), samplerate=24_000, blocking=True)
            if _WORKER_STOP.is_set():
                return {"ok": True, "backend": "stopped"}
    except Exception:
        return {"ok": False, "code": "speech_playback_failed"}
    return {"ok": True, "backend": f"kokoro-{device}"}


def _worker_prepare() -> dict[str, Any]:
    """Fetch the model, the default voice and the English phonemizer (network on)."""
    try:
        from kokoro import KPipeline
    except ImportError:
        return {"ok": False, "code": "speech_dependency_missing"}
    try:
        pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cpu")
        pipeline.load_voice(_DEFAULT_VOICE)
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
