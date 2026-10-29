"""Text to speech: a warm ptts worker whose audio streams back to the client.

Adapted from old Nexus nexus/host_support/speech.py. The worker plays nothing: it
writes framed PCM16 to the daemon, which streams a WAV to whichever client asked
(PLAN.md I). The model is read from the HF cache only, never downloaded here.

Worker protocol, over a pipe: the daemon writes one JSON line {"text": ...} or the
line "stop"; the worker answers with frames, each a JSON header line
{"rate": N, "bytes": M} followed by M bytes of mono PCM16, then {"done": true} or
{"error": "<code>"} for the end of that request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import struct
import sys
import threading
from collections.abc import AsyncIterator
from typing import Any

MAX_TEXT_CHARS = 20_000
MAX_AUDIO_SECONDS = 180
MAX_CHUNK_CHARS = 400
IDLE_UNLOAD_SECONDS = 600

MESSAGES = {
    "speech_dependency_missing": "Speech needs the voice extra: nexus voice init",
    "speech_model_not_cached": "The speech model is not downloaded",
    "speech_model_unavailable": "The speech model could not be loaded",
    "speech_unavailable": "Speech could not be completed",
    "speech_busy": "Speech is already playing",
    "speech_no_answer": "There is no completed answer to speak",
    "speech_turn_active": "Wait for the current turn to finish",
    "speech_text_too_long": "The answer is too long to speak (20,000 character limit)",
}


class SpeechError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or MESSAGES.get(code, code))
        self.code = code


def final_answer(store: Any, session_id: str) -> str:
    """The final text of the latest completed turn, and nothing else."""
    turns = store.turns_for_session(session_id)
    if any(t["status"] == "running" for t in turns):
        raise SpeechError("speech_turn_active")
    done = next((t for t in reversed(turns) if t["status"] == "completed"), None)
    if done is None:
        raise SpeechError("speech_no_answer")
    messages = [m for m in store.messages_after_seq(session_id, done["start_seq"] or 0) if m.message.role == "assistant"]
    if not messages:
        raise SpeechError("speech_no_answer")
    parts = messages[-1].message.parts
    if any(type(part).__name__ == "ToolCall" for part in parts):
        raise SpeechError("speech_no_answer")
    text = "".join(part.text for part in parts if type(part).__name__ == "Text")
    if not text.strip():
        raise SpeechError("speech_no_answer")
    if len(text) > MAX_TEXT_CHARS:
        raise SpeechError("speech_text_too_long")
    return text


def wav_header(rate: int) -> bytes:
    """A streaming WAV header: the sizes are unknown, so both are 0xFFFFFFFF."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


class SpeechService:
    def __init__(self, model_repo: str = "gradium/phonon-31m-en") -> None:
        self.model_repo = model_repo
        self.worker: asyncio.subprocess.Process | None = None
        self.model_in_worker = ""
        self.busy = False
        self.idle: asyncio.TimerHandle | None = None

    async def _start(self, repo: str) -> asyncio.subprocess.Process:
        env = os.environ | {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "NEXUS_TTS_REPO": repo}
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "nexus.voice.speech", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=1 << 20)

    def unload(self) -> None:
        worker, self.worker = self.worker, None
        if self.idle:
            self.idle.cancel()
            self.idle = None
        if worker and worker.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                worker.kill()

    def stop(self) -> bool:
        if not self.busy or self.worker is None or self.worker.stdin is None:
            return False
        with contextlib.suppress(Exception):
            self.worker.stdin.write(b"stop\n")
        return True

    async def speak(self, text: str, repo: str | None = None) -> AsyncIterator[bytes]:
        """Yield a streaming WAV. The first frame is read before returning, so failures raise SpeechError early."""
        repo = repo or self.model_repo
        if self.busy:
            raise SpeechError("speech_busy")
        self.busy = True
        try:
            if self.idle:
                self.idle.cancel()
            if self.worker is not None and (self.worker.returncode is not None or self.model_in_worker != repo):
                self.unload()
            if self.worker is None:
                self.worker, self.model_in_worker = await self._start(repo), repo
            worker = self.worker
            worker.stdin.write(json.dumps({"text": text}, ensure_ascii=False).encode() + b"\n")
            await worker.stdin.drain()
            first = await self._frame(worker)
        except BaseException:
            self.busy = False
            self.unload()
            raise
        return self._stream(worker, first)

    async def _frame(self, worker: asyncio.subprocess.Process) -> tuple[int, bytes] | None:
        line = await worker.stdout.readline()
        if not line:
            raise SpeechError("speech_unavailable")
        head = json.loads(line)
        if "error" in head:
            raise SpeechError(head["error"])
        if head.get("done"):
            return None
        return head["rate"], await worker.stdout.readexactly(head["bytes"])

    async def _stream(self, worker: asyncio.subprocess.Process, first: tuple[int, bytes] | None) -> AsyncIterator[bytes]:
        try:
            if first is None:
                return
            yield wav_header(first[0])
            yield first[1]
            while (frame := await self._frame(worker)) is not None:
                yield frame[1]
        except SpeechError:
            self.unload()
        except (GeneratorExit, asyncio.CancelledError):
            self.stop()
            raise
        finally:
            self.busy = False
            if self.worker is worker and worker.returncode is None:
                self.idle = asyncio.get_running_loop().call_later(IDLE_UNLOAD_SECONDS, self.unload)


# -- worker process ---------------------------------------------------------

_STOP = threading.Event()


def text_chunks(text: str):
    """One sentence at a time, each at most MAX_CHUNK_CHARS long."""
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text.strip()):
        sentence = sentence.strip()
        while len(sentence) > MAX_CHUNK_CHARS:
            cut = sentence.rfind(" ", 0, MAX_CHUNK_CHARS)
            cut = cut if cut > 0 else MAX_CHUNK_CHARS
            yield sentence[:cut]
            sentence = sentence[cut:].lstrip()
        if sentence:
            yield sentence


def _emit(out: Any, header: dict[str, Any], payload: bytes = b"") -> None:
    out.write(json.dumps(header, separators=(",", ":")).encode() + b"\n" + payload)
    out.flush()


def _serve() -> int:
    import queue

    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing a library prints may corrupt the frames
    requests: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            if line.strip() == "stop":
                _STOP.set()
            else:
                _STOP.clear()
                requests.put(line)
        requests.put(None)

    threading.Thread(target=reader, daemon=True).start()
    tts = None
    while (line := requests.get()) is not None:
        try:
            try:
                import numpy as np
                import ptts
            except ImportError:
                _emit(out, {"error": "speech_dependency_missing"})
                continue
            if tts is None:
                try:
                    tts = ptts.TTS("hf://" + os.environ["NEXUS_TTS_REPO"], lang="en", quant="q8")
                except Exception:  # noqa: BLE001
                    _emit(out, {"error": "speech_model_not_cached" if os.environ.get("HF_HUB_OFFLINE") == "1" else "speech_model_unavailable"})
                    continue
            text = json.loads(line).get("text", "")
            rate, seconds = int(tts.sample_rate), 0.0
            for chunk in text_chunks(text):
                if _STOP.is_set():
                    break
                audio = np.clip(np.asarray(tts.synth(chunk), dtype=np.float32), -1.0, 1.0)
                seconds += len(audio) / rate
                if seconds > MAX_AUDIO_SECONDS:
                    break
                pcm = (audio * 32767).astype("<i2").tobytes()
                _emit(out, {"rate": rate, "bytes": len(pcm)}, pcm)
            _emit(out, {"done": True})
        except Exception:  # noqa: BLE001
            _emit(out, {"error": "speech_unavailable"})
    return 0


if __name__ == "__main__":
    raise SystemExit(_serve())
