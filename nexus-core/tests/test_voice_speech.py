import asyncio
import struct

import pytest

from nexus.llmhub.types import Message, Text, ToolCall
from nexus.store import Store
from nexus.voice import speech


def test_chunks_split_sentences_and_long_text():
    assert list(speech.text_chunks("One. Two!\nThree")) == ["One.", "Two!", "Three"]
    assert all(len(c) <= speech.MAX_CHUNK_CHARS for c in speech.text_chunks("word " * 500))


def test_wav_header():
    h = speech.wav_header(24000)
    assert h[:4] == b"RIFF" and h[8:12] == b"WAVE" and len(h) == 44
    assert struct.unpack("<I", h[24:28])[0] == 24000


def test_final_answer(tmp_path):
    store = Store(tmp_path / "n.db")

    class T:
        pass
    sid = "s1"
    turns = [{"status": "completed", "start_seq": 0}]
    store.turns_for_session = lambda s: turns
    store.messages_after_seq = lambda s, seq: [
        type("M", (), {"message": Message("assistant", (Text("hi there"),))})(),
    ]
    assert speech.final_answer(store, sid) == "hi there"
    store.messages_after_seq = lambda s, seq: [
        type("M", (), {"message": Message("assistant", (Text("x"), ToolCall("1", "bash", "{}")))})()]
    with pytest.raises(speech.SpeechError):
        speech.final_answer(store, sid)
    turns[0]["status"] = "running"
    with pytest.raises(speech.SpeechError) as e:
        speech.final_answer(store, sid)
    assert e.value.code == "speech_turn_active"


FAKE_WORKER = '''
import json, sys
sys.stdin.readline()
for _ in range(2):
    sys.stdout.buffer.write(b'{"rate":16000,"bytes":4}\\n' + b"\\x01\\x00\\x02\\x00")
sys.stdout.buffer.write(b'{"done":true}\\n'); sys.stdout.buffer.flush()
sys.stdin.readline()
'''


async def test_speak_streams_wav(tmp_path, monkeypatch):
    script = tmp_path / "w.py"
    script.write_text(FAKE_WORKER)
    service = speech.SpeechService()

    async def start(repo):
        return await asyncio.create_subprocess_exec(
            "python3", str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=1 << 20)
    monkeypatch.setattr(service, "_start", start)
    stream = await service.speak("hello")
    with pytest.raises(speech.SpeechError):
        await service.speak("again")
    data = b"".join([c async for c in stream])
    assert data[:4] == b"RIFF" and data[44:] == b"\x01\x00\x02\x00" * 2
    assert not service.busy
    service.unload()


async def test_worker_error_is_raised(tmp_path, monkeypatch):
    script = tmp_path / "w.py"
    script.write_text('import sys\nsys.stdin.readline()\nprint(\'{"error":"speech_model_not_cached"}\')\n')
    service = speech.SpeechService()

    async def start(repo):
        return await asyncio.create_subprocess_exec(
            "python3", str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=1 << 20)
    monkeypatch.setattr(service, "_start", start)
    with pytest.raises(speech.SpeechError) as e:
        await service.speak("hello")
    assert e.value.code == "speech_model_not_cached" and not service.busy
