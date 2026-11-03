"""M3 Core: the text form of /api/voice/speak, incremental sentences, barge-in and the redaction of speech."""
from __future__ import annotations

import asyncio
import time
from typing import ClassVar

import pytest
from manager_seed import seed
from manager_support import Harness, until

from nexus import runtime as runtime_module
from nexus import server as server_module
from nexus.llmhub.types import Done, PartDone, Text, TextDelta
from nexus.manager.redact import redact
from nexus.voice.speech import SpeechError, wav_header


class FakeSpeech:
    """A SpeechService that records what it was asked to say."""

    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs) -> None:
        self.said: list[str] = []
        self.busy = False
        self.stopped = 0
        self.hold = asyncio.Event()
        self.hold.set()
        FakeSpeech.instances.append(self)

    async def speak(self, text, repo=None):
        if self.busy:
            raise SpeechError("speech_busy")
        self.said.append(text)
        self.busy = True

        async def stream():
            try:
                yield wav_header(24000)
                await self.hold.wait()
                yield b"\x00\x00" * 8
            finally:
                self.busy = False

        return stream()

    def stop(self) -> bool:
        self.stopped += 1
        return True


@pytest.fixture
async def h(tmp_path, monkeypatch):
    FakeSpeech.instances.clear()
    monkeypatch.setattr(server_module, "SpeechService", FakeSpeech)
    harness = Harness(tmp_path, monkeypatch)
    harness.store.set_preference("model.last", "mock/hello")
    harness.speech = FakeSpeech.instances[-1]
    yield harness
    await harness.close()


async def speak(h, **body):
    return await h.client.post("/api/voice/speak", json=body)


def test_secret_shapes_are_masked_and_ordinary_text_is_not():
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    text = f"The key {secret} and ghp_{'a' * 30} and password = hunter2hunter2 and AKIAABCDEFGHIJKLMNOP end."
    out = redact(text)
    assert secret not in out and "ghp_" not in out and "hunter2" not in out and "AKIA" not in out
    assert out.count("[redacted]") == 4
    assert redact("Bearer abcdefghijklmnopqrstuvwxyz") == "[redacted]"
    assert redact("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----") == "[redacted]"
    for plain in ("The auth refactor finished.", "tokens are bounded by max_tokens", "use a password manager", ""):
        assert redact(plain) == plain


@pytest.mark.parametrize("text, expected", [
    ('{"password": "hunter2hunter2"}', '{"password": [redacted]}'),
    ("{'api_key': 'private-value'}", "{'api_key': [redacted]}"),
    ('"access_token": "another-secret"', '"access_token": [redacted]'),
    (r'{"password": "secret\"tail"}', '{"password": [redacted]}'),
])
def test_quoted_assignment_names_are_masked(text, expected):
    assert redact(text) == expected


async def test_the_holder_can_speak_one_sentence_and_the_text_is_redacted_first(h):
    h.open_stream()
    await h.start()
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    response = await speak(h, stream_id=h.stream.stream_id, text=f"Your key is {secret}. Done.")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")
    assert h.speech.said == ["Your key is [redacted]. Done."]


async def test_speech_masks_json_credentials(h):
    h.open_stream()
    await h.start()
    response = await speak(h, stream_id=h.stream.stream_id, text='{"password": "hunter2hunter2"}')
    assert response.status_code == 200
    assert h.speech.said == ['{"password": [redacted]}']


async def test_only_the_holder_may_speak_and_a_missing_stream_id_is_refused(h):
    h.open_stream()
    await h.start()
    stranger = h.open_stream([])
    for body in ({"text": "hi"}, {"stream_id": stranger.stream_id, "text": "hi"}, {"stream_id": "", "text": "hi"}):
        response = await speak(h, **body)
        assert response.status_code == 403 and response.json()["code"] == "not_holder"
        assert stranger.stream_id not in response.text
    assert h.speech.said == []
    await h.client.post("/api/manager/stop", json={"stream_id": h.stream.stream_id})
    assert (await speak(h, stream_id=h.stream.stream_id, text="hi")).status_code == 403  # no call, no holder


async def test_speech_text_is_bounded_and_must_not_be_empty(h):
    h.open_stream()
    await h.start()
    sid = h.stream.stream_id
    assert (await speak(h, stream_id=sid, text="   ")).status_code == 400
    assert (await speak(h, stream_id=sid, text=5)).status_code == 400
    long = await speak(h, stream_id=sid, text="word " * 400)
    assert long.status_code == 422 and long.json()["code"] == "speech_text_too_long"
    assert h.speech.said == []


async def test_the_session_form_is_unchanged(h):
    project = h.project()
    session = seed(h.store, project, title="Work", user="u", answer="The final answer.", now=time.time())
    assert (await speak(h, session_id=session.id)).status_code == 200
    assert h.speech.said == ["The final answer."]
    assert (await speak(h, session_id="ses_nope")).status_code == 404


async def test_the_next_sentence_waits_for_the_one_still_playing_instead_of_failing(h):
    h.open_stream()
    await h.start()
    sid = h.stream.stream_id
    h.speech.hold.clear()
    first = asyncio.ensure_future(speak(h, stream_id=sid, text="First."))
    await until(lambda: h.speech.busy)
    second = asyncio.ensure_future(speak(h, stream_id=sid, text="Second."))
    await asyncio.sleep(0.2)
    assert not second.done()
    h.speech.hold.set()
    assert (await first).status_code == 200 and (await second).status_code == 200
    assert h.speech.said == ["First.", "Second."]


async def test_cancel_stops_the_sentence_being_synthesised_too(h):
    h.open_stream()
    await h.start()
    response = await h.client.post("/api/manager/cancel", json={"stream_id": h.stream.stream_id})
    assert response.json()["speech_stopped"] is True and h.speech.stopped == 1


class Gated:
    """A model that says a sentence, waits for the test, then says another."""

    name = "scripted"

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    def body(self, request):
        return {"model": request.model}

    async def stream(self, request):
        yield TextDelta("The first sentence. ")
        await self.gate.wait()
        yield TextDelta("The second sentence with sk-abcdefghijklmnopqrstuvwxyz0123456789 in it.")
        yield PartDone(Text("The first sentence. The second sentence with sk-abcdefghijklmnopqrstuvwxyz0123456789 in it."))
        yield Done("end_turn")


async def test_sentences_leave_as_they_are_written_not_when_the_turn_ends(h, monkeypatch):
    model = Gated()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (model, "m"))
    h.open_stream()
    await h.start()
    await h.say("go on", wait=False)
    await until(lambda: h.spoken() == ["The first sentence."], what="the first sentence before the turn ends")
    assert h.manager.turn is not None  # the reply is still being written
    assert h.of("manager.state")[-1]["data"]["phase"] == "replying"
    model.gate.set()
    await h.settle()
    await asyncio.sleep(0.05)
    assert h.spoken()[1] == "The second sentence with [redacted] in it."
    assert "sk-abc" not in str(h.events)


async def test_cancel_mid_reply_mutes_the_rest_drops_the_queue_and_interrupts(h, monkeypatch):
    model = Gated()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (model, "m"))
    h.open_stream()
    await h.start()
    await h.say("first", wait=False)
    await until(lambda: h.spoken())
    await h.say("second, queued", wait=False)
    response = (await h.client.post("/api/manager/cancel", json={"stream_id": h.stream.stream_id})).json()
    assert response == {"cancelled": True, "muted": True, "dropped": 1, "speech_stopped": True}
    model.gate.set()
    await h.settle()
    assert h.spoken() == ["The first sentence."]
    assert h.store.turns_for_session(h.manager.current_episode().id)[-1]["status"] == "interrupted"
    # The call is still usable afterwards.
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (_Quick(), "m"))
    await h.say("and now?")
    assert h.spoken()[-1] == "All good."


class _Quick:
    name = "scripted"

    def body(self, request):
        return {}

    async def stream(self, request):
        yield TextDelta("All good.")
        yield PartDone(Text("All good."))
        yield Done("end_turn")


async def test_activity_is_stored_for_the_holder_only_and_wakes_nothing_by_itself(h):
    h.open_stream()
    await h.start()
    sid = h.stream.stream_id
    assert (await h.client.post("/api/manager/activity", json={"stream_id": sid, "listening": True, "speaking": True})).status_code == 204
    assert (h.manager.call.listening, h.manager.call.speaking) == (True, True)
    assert h.manager.last_activity > 0
    await h.client.post("/api/manager/activity", json={"stream_id": sid, "listening": False, "speaking": False})
    assert (h.manager.call.listening, h.manager.call.speaking) == (False, False)
