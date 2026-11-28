"""Recorder HTTP API (plan 54, B3): transport bounds, auth, leases/recovery, events and WAV export."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import math
import threading
import time
import wave
from array import array
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from nexus import recorder_api as recorder_api_module
from nexus.recorder import RecorderCorrupt
from nexus.recorder.host import RecorderUnavailable
from nexus.recorder.service import LEASE_TTL_SECONDS
from nexus.server import build_app, make_runtime
from nexus.store import Store
from nexus.voice.manager import VoiceManager
from nexus.voice.store import MODEL_ID, ModelPaths

TOKEN = "recorder-test-token"
CLIENT = "tui"


class Clock:
    def __init__(self) -> None:
        self.mono = time.monotonic()

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.mono += seconds


class FakeStream:
    """Enough of `nexus.eventlog.Stream` for ClientViews; closing it runs the detach listeners."""

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self.listeners: list[Any] = []
        self.closed = False

    def add_close_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for listener in list(self.listeners):
            listener(self)


class TranscriptModel:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.calls = 0

    def verified_paths(self) -> ModelPaths:
        self.calls += 1
        if self.missing:
            raise FileNotFoundError("model not cached")
        return ModelPaths(Path("/fake/photon"))

    def cache_sizes(self) -> dict[str, int]:
        return {MODEL_ID: 0}


class TranscriptEngine:
    """Deterministic fake inference; no model, network or microphone."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.loads = 0
        self.fail_remaining = 0
        self.closed = False

    async def load(self) -> None:
        self.loads += 1
        self.closed = False

    async def transcribe(self, audio: bytes) -> str:
        self.calls.append(len(audio))
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise RuntimeError("injected inference failure")
        return f"kestrel transcript {len(self.calls)}"

    async def close(self) -> None:
        self.closed = True


class DownloadSentinel:
    def __init__(self) -> None:
        self.called = False

    def __call__(self, _store: Any) -> Any:
        self.called = True
        raise AssertionError("the recorder scheduler must never download a model")


def transcript_speech(samples: int = 8_000) -> bytes:
    values = array("h")
    for index in range(samples):
        amplitude = 9_000 if (index // 320) % 2 == 0 else 4_000
        values.append(int(amplitude * math.sin(index * 0.17)))
    return values.tobytes()


def transcript_silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


@contextlib.asynccontextmanager
async def transcript_daemon(tmp_path: Path, *, engine: TranscriptEngine | None = None,
                            missing_model: bool = False,
                            download: DownloadSentinel | None = None):
    """The production daemon app + lifespan with fake STT inference."""
    runtime = make_runtime(Store(tmp_path / "nexus.db"))
    engine = engine or TranscriptEngine()
    model = TranscriptModel(missing=missing_model)
    download = download or DownloadSentinel()
    voice = VoiceManager(
        engine_factory=lambda _paths: engine,
        store=model,
        download_runner=download,
        idle_unload_seconds=0,
        recorder_execute_timeout_s=2.0,
    )
    app = build_app(runtime, token=TOKEN, url="http://test", voice_manager=voice)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield runtime, app, client, engine, model, download
    runtime.store.close()


@contextlib.asynccontextmanager
async def live_transcript_daemon(tmp_path: Path, *, engine: TranscriptEngine | None = None):
    """The same app on a real loopback HTTP server, for lazy streaming and disconnects."""
    runtime = make_runtime(Store(tmp_path / "nexus.db"))
    engine = engine or TranscriptEngine()
    voice = VoiceManager(
        engine_factory=lambda _paths: engine,
        store=TranscriptModel(),
        download_runner=DownloadSentinel(),
        idle_unload_seconds=0,
        recorder_execute_timeout_s=2.0,
    )
    app = build_app(runtime, token=TOKEN, url="http://127.0.0.1", voice_manager=voice)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    if hasattr(server, "install_signal_handlers"):
        server.install_signal_handlers = lambda: None  # the test owns the loop and its signals
    serving = asyncio.create_task(server.serve())
    client: httpx.AsyncClient | None = None
    try:
        for _ in range(300):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the test daemon did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10,
                                   headers={"authorization": f"Bearer {TOKEN}"})
        yield app, client, engine
    finally:
        if client is not None:
            await client.aclose()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        runtime.store.close()


async def record_audio(app: Any, client: httpx.AsyncClient, audio: bytes, *,
                       key: str = "key-1") -> tuple[str, str, dict[str, Any]]:
    await open_stream(app, client)
    created = await create(client, key=key)
    assert created.status_code == 201, created.text
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    offset = 0
    for sequence, at in enumerate(range(0, len(audio), 64_000)):
        piece = audio[at:at + 64_000]
        response = await chunk(client, recording_id, sequence, piece, offset, lease)
        assert response.status_code == 200, response.text
        offset += len(piece) // 2
    stopped = await client.post(f"/api/recordings/{recording_id}/stop",
                                json={"stream_id": "s1", "final_sample_count": offset},
                                headers={"x-recorder-lease": lease})
    assert stopped.status_code == 200, stopped.text
    return recording_id, lease, created.json()


async def wait_transcript(client: httpx.AsyncClient, recording_id: str, state: str = "complete",
                          *, timeout: float = 10.0, limit: int = 50) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/recordings/{recording_id}/transcript",
                                    params={"limit": limit})
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] == state:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"transcript did not reach {state}: {last!r}")


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()  # the runtime and the recorder both prepare this root
    runtime = make_runtime(Store(tmp_path / "nexus.db"))
    app = build_app(runtime, token=TOKEN, url="http://test")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield runtime, app, client, tmp_path
    runtime.store.close()


async def open_stream(app: Any, client: httpx.AsyncClient, stream_id: str = "s1",
                      client_id: str = CLIENT) -> FakeStream:
    stream = FakeStream(stream_id)
    app.state.client_views.attach(stream)
    response = await client.post("/api/client/view",
                                 json={"stream_id": stream_id, "client_id": client_id})
    assert response.status_code == 200, response.text
    return stream


def recovery_for(key: str) -> str:
    """A deterministic client capability per test key, so replays prove the same capability."""
    return f"{key}-recovery-capability".ljust(43, "x")


async def create(client: httpx.AsyncClient, stream_id: str = "s1", *,
                 key: str = "key-1", client_id: str = CLIENT,
                 recovery: str | None = None, mode: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"stream_id": stream_id, "client_id": client_id}
    if mode is not None:
        body["mode"] = mode
    response = await client.post(
        "/api/recordings", json=body,
        headers={"idempotency-key": key,
                 "x-recorder-recovery": recovery if recovery is not None else recovery_for(key)})
    response.recording_recovery = recovery if recovery is not None else recovery_for(key)
    return response


async def chunk(client: httpx.AsyncClient, recording_id: str, sequence: int, data: bytes, start: int,
                lease: str, stream_id: str = "s1", *, sha: str | None = None,
                content_type: str = "application/octet-stream") -> httpx.Response:
    return await client.put(
        f"/api/recordings/{recording_id}/chunks/{sequence}", content=data,
        headers={
            "content-type": content_type,
            "x-recorder-lease": lease,
            "x-recorder-stream": stream_id,
            "x-sample-start": str(start),
            "x-content-sha256": sha or hashlib.sha256(data).hexdigest(),
        },
    )


async def wait_event(subscription: Any, type_: str, *, timeout: float = 2.0) -> dict[str, Any]:
    async def seek() -> dict[str, Any]:
        while True:
            event = await subscription.get()
            if event is None:
                raise AssertionError("event stream ended")
            if event["type"] == type_:
                return event

    return await asyncio.wait_for(seek(), timeout)


def wav_samples(data: bytes) -> int:
    with wave.open(io.BytesIO(data)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16_000
        assert wav.getsampwidth() == 2
        return wav.getnframes()


@pytest.mark.asyncio
async def test_create_chunks_stop_and_download_exact_wav(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    created = await create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    recording_id, lease, recovery = body["id"], body["lease_token"], created.recording_recovery
    assert body["recovery_token"] is None and body["recovery"] == "client"
    assert lease and recovery and body["active"] is True
    assert created.headers["cache-control"] == "no-store"

    pieces = [bytes([index]) * 3200 for index in range(3)]
    for sequence, piece in enumerate(pieces):
        response = await chunk(client, recording_id, sequence, piece, sequence * 1600, lease)
        assert response.status_code == 200, response.text

    heartbeat = await client.post(f"/api/recordings/{recording_id}/heartbeat",
                                  json={"stream_id": "s1"}, headers={"x-recorder-lease": lease})
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["lease_expires_in"] > 0

    stopped = await client.post(f"/api/recordings/{recording_id}/stop",
                                json={"stream_id": "s1", "final_sample_count": 4800},
                                headers={"x-recorder-lease": lease})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["complete"] is True

    state = await client.get(f"/api/recordings/{recording_id}")
    assert state.status_code == 200 and state.json()["durable_offset"] == 4800

    downloaded = await client.get(f"/api/recordings/{recording_id}/audio.wav")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == "audio/wav"
    assert downloaded.headers["content-encoding"] == "identity"
    assert downloaded.headers["content-length"] == str(44 + 9600)
    assert downloaded.headers["x-recording-complete"] == "1"
    assert downloaded.headers["x-recording-samples"] == "4800"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert wav_samples(downloaded.content) == 4800
    assert downloaded.content[44:] == b"".join(pieces)

    listed = await client.get("/api/recordings")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["recordings"]] == [recording_id]
    assert "lease_token" not in listed.text and "recovery_token" not in listed.text

    # WAV after stop carries no token material.
    assert lease not in downloaded.text and recovery not in downloaded.text


@pytest.mark.asyncio
async def test_create_is_idempotent_and_never_journals_tokens(api) -> None:
    runtime, app, client, tmp_path = api
    await open_stream(app, client)
    first = await create(client, key="same-key")
    assert first.status_code == 201
    lease = first.json()["lease_token"]
    second = await create(client, key="same-key")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotent"] is True
    assert runtime.store.mutation("same-key") is None
    database = b"".join(path.read_bytes() for path in tmp_path.glob("nexus.db*"))
    assert lease.encode() not in database

    busy = await create(client, key="other-key")
    assert busy.status_code == 409
    assert busy.json()["code"] == "recorder_busy"
    assert lease not in busy.text and "lease_token" not in busy.text


@pytest.mark.asyncio
async def test_create_requires_client_recovery_and_replays_require_its_proof(api) -> None:
    _, app, client, tmp_path = api
    await open_stream(app, client)
    missing = await client.post("/api/recordings", json={"stream_id": "s1", "client_id": CLIENT},
                                headers={"idempotency-key": "no-recovery"})
    assert missing.status_code == 400 and missing.json()["code"] == "recovery_required"
    short = await client.post("/api/recordings", json={"stream_id": "s1", "client_id": CLIENT},
                              headers={"idempotency-key": "short-recovery",
                                       "x-recorder-recovery": "too-short"})
    assert short.status_code == 400 and short.json()["code"] == "recovery_required"
    assert (await client.get("/api/recordings")).json()["recordings"] == []

    first = await create(client, key="secret-key")
    assert first.status_code == 201, first.text
    body = first.json()
    recovery = first.recording_recovery
    assert body["recovery_token"] is None and body["recovery"] == "client"
    assert recovery not in first.text
    database = b"".join(path.read_bytes() for path in tmp_path.glob("nexus.db*"))
    assert recovery.encode() not in database

    wrong_create = await create(client, key="secret-key", recovery="w" * 43)
    assert wrong_create.status_code == 409
    assert wrong_create.json()["code"] == "idempotency_conflict"
    wrong_resume = await create(client, key="secret-key", recovery="w" * 43, mode="resume")
    assert wrong_resume.status_code == 409
    assert wrong_resume.json()["code"] == "idempotency_conflict"

    resumed = await create(client, key="secret-key", mode="resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["id"] == body["id"]
    assert resumed.json()["lease_token"] == body["lease_token"]
    assert resumed.json()["recovery_token"] is None
    stopped = await client.post(f"/api/recordings/{body['id']}/stop", json={"final_sample_count": 0},
                                headers={"x-recorder-lease": resumed.json()["lease_token"]})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["sealed"] is True and stopped.json()["complete"] is True

    after_stop = await create(client, key="secret-key", mode="resume")
    assert after_stop.status_code == 200
    assert after_stop.json()["tokens_reissued"] is False
    assert after_stop.json()["lease_token"] is None
    assert after_stop.json()["recovery_token"] is None


@pytest.mark.asyncio
async def test_resume_unknown_key_never_creates_and_withdraws_the_key(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    lookup = await create(client, key="ghost-key", mode="resume")
    assert lookup.status_code == 404, lookup.text
    assert lookup.json()["code"] == "create_not_found"
    assert (await client.get("/api/recordings")).json()["recordings"] == []

    created = await create(client, key="ghost-key")
    assert created.status_code == 409, created.text
    assert created.json()["code"] == "create_withdrawn"
    assert (await client.get("/api/recordings")).json()["recordings"] == []

    fresh = await create(client, key="fresh-key")
    assert fresh.status_code == 201, fresh.text


@pytest.mark.asyncio
async def test_resume_and_create_race_through_the_api_never_duplicates(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    lookup, created = await asyncio.gather(
        create(client, key="api-race", mode="resume"),
        create(client, key="api-race"),
    )
    listed = (await client.get("/api/recordings")).json()["recordings"]
    if created.status_code == 201:
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()["id"] == created.json()["id"]
        assert len(listed) == 1
    else:
        assert created.status_code == 409 and created.json()["code"] == "create_withdrawn"
        assert lookup.status_code == 404
        assert listed == []


@pytest.mark.asyncio
async def test_chunk_conflicts_bounds_and_transport_errors(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    await open_stream(app, client, "s2")
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    piece = b"\x01\x02" * 1600
    assert (await chunk(client, recording_id, 0, piece, 0, lease)).status_code == 200

    identical = await chunk(client, recording_id, 0, piece, 0, lease)
    assert identical.status_code == 200

    conflict = await chunk(client, recording_id, 0, b"\x03\x04" * 1600, 0, lease)
    assert conflict.status_code == 409 and conflict.json()["code"] == "chunk_conflict"

    gap = await chunk(client, recording_id, 5, b"\x05\x06" * 10, 9990, lease)
    assert gap.status_code == 409 and gap.json()["code"] == "noncontiguous"
    assert gap.json()["durable_offset"] == 1600

    odd = await chunk(client, recording_id, 1, b"abc", 1600, lease)
    assert odd.status_code == 400

    oversized = await chunk(client, recording_id, 1, b"\x00" * 64_001, 1600, lease)
    assert oversized.status_code == 413 and oversized.json()["code"] == "chunk_too_large"

    # A lying Content-Length still hits the streaming cap.
    lying = await client.put(
        f"/api/recordings/{recording_id}/chunks/1", content=b"\x00" * 64_001,
        headers={
            "content-type": "application/octet-stream", "content-length": "10",
            "x-recorder-lease": lease, "x-recorder-stream": "s1", "x-sample-start": "1600",
            "x-content-sha256": hashlib.sha256(b"\x00" * 64_001).hexdigest(),
        })
    assert lying.status_code == 413

    wrong_type = await chunk(client, recording_id, 1, piece, 1600, lease,
                             content_type="application/json")
    assert wrong_type.status_code == 415

    missing_lease = await client.put(
        f"/api/recordings/{recording_id}/chunks/1", content=piece,
        headers={"content-type": "application/octet-stream", "x-recorder-stream": "s1",
                 "x-sample-start": "1600", "x-content-sha256": hashlib.sha256(piece).hexdigest()})
    assert missing_lease.status_code == 403 and missing_lease.json()["code"] == "invalid_lease"

    wrong_stream = await chunk(client, recording_id, 1, piece, 1600, lease, stream_id="s2")
    assert wrong_stream.status_code == 403 and wrong_stream.json()["code"] == "not_owner"

    for response in (identical, conflict, gap, odd, oversized, lying, wrong_type,
                     missing_lease, wrong_stream):
        assert lease not in response.text
        assert "s1" not in response.text

    oversized_json = await client.post(
        "/api/recordings", content=b'{"title": "' + b"x" * 5000 + b'"}',
        headers={"content-type": "application/json", "idempotency-key": "big"})
    assert oversized_json.status_code == 413

    wrong_json_type = await client.post(
        "/api/recordings", content=b'{"stream_id": "s1"}',
        headers={"content-type": "text/plain", "idempotency-key": "text"})
    assert wrong_json_type.status_code == 415


@pytest.mark.asyncio
async def test_chunk_backpressure_is_503_and_bodies_are_bounded(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    recorder = app.state.recorder
    original = recorder.service.put_chunk
    entered = threading.Event()
    release = threading.Event()

    def blocked(self: Any, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(timeout=5)
        return original(self, *args, **kwargs)

    recorder.service.put_chunk = blocked
    try:
        requests = [
            asyncio.create_task(chunk(client, recording_id, index, b"\x00\x01" * 100, index * 200, lease))
            for index in range(4)
        ]
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        await asyncio.sleep(0.1)  # let all four bodies acquire the small admission budget

        fifth = await chunk(client, recording_id, 9, b"\x00\x01" * 100, 400, lease)
        assert fifth.status_code == 503
        assert fifth.json()["code"] == "recorder_backpressure"
        assert fifth.headers["retry-after"] == "1"

        release.set()
        responses = await asyncio.gather(*requests)
        assert all(response.status_code in (200, 409, 500) for response in responses)
    finally:
        release.set()
        recorder.service.put_chunk = original


@pytest.mark.asyncio
async def test_stop_and_upload_race_stays_consistent(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    assert (await chunk(client, recording_id, 0, b"\x00\x01" * 1600, 0, lease)).status_code == 200
    stop, upload = await asyncio.gather(
        client.post(f"/api/recordings/{recording_id}/stop", json={"stream_id": "s1"},
                    headers={"x-recorder-lease": lease}),
        chunk(client, recording_id, 1, b"\x00\x02" * 1600, 1600, lease),
    )
    assert stop.status_code == 200
    assert upload.status_code in (200, 409)
    state = (await client.get(f"/api/recordings/{recording_id}")).json()
    assert state["sealed"] is True and state["active"] is False
    assert state["durable_offset"] in (1600, 3200)


@pytest.mark.asyncio
async def test_lease_expiry_recovery_and_stopped_only_wav(api) -> None:
    runtime, app, client, _ = api
    await open_stream(app, client)
    recorder = app.state.recorder
    clock = Clock()
    recorder.service.monotonic = clock.monotonic
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    recovery = created.recording_recovery
    assert (await chunk(client, recording_id, 0, b"\x00\x01" * 1600, 0, lease)).status_code == 200

    subscription = runtime.bus.open()
    clock.advance(LEASE_TTL_SECONDS + 1)
    await recorder.call(recorder.service.sweep)
    event = await wait_event(subscription, "recorder.state")
    subscription.close()
    assert event["data"]["id"] == recording_id
    assert event["data"]["reason"] == "lease_expired"
    assert event["data"]["active"] is False

    expired = await client.post(f"/api/recordings/{recording_id}/heartbeat",
                                json={"stream_id": "s1"}, headers={"x-recorder-lease": lease})
    assert expired.status_code == 409 and expired.json()["code"] == "lease_expired"

    # Interrupted is inactive but not stopped: WAV must wait for recovery to seal it.
    unsealed = await client.get(f"/api/recordings/{recording_id}/audio.wav")
    assert unsealed.status_code == 409
    assert unsealed.json()["code"] == "recording_not_stopped"
    assert not unsealed.content.startswith(b"RIFF")

    recovered = await client.post(
        f"/api/recordings/{recording_id}/recover",
        json={"client_id": CLIENT, "final_sample_count": 3200},
        headers={"x-recorder-recovery": recovery})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["sealed"] is True

    upload = await client.put(
        f"/api/recordings/{recording_id}/recover/chunks/1", content=b"\x00\x02" * 1600,
        headers={
            "content-type": "application/octet-stream", "x-recorder-recovery": recovery,
            "x-recorder-client": CLIENT, "x-sample-start": "1600",
            "x-content-sha256": hashlib.sha256(b"\x00\x02" * 1600).hexdigest(),
        })
    assert upload.status_code == 200, upload.text

    beyond = await client.put(
        f"/api/recordings/{recording_id}/recover/chunks/2", content=b"\x00\x03" * 10,
        headers={
            "content-type": "application/octet-stream", "x-recorder-recovery": recovery,
            "x-recorder-client": CLIENT, "x-sample-start": "3200",
            "x-content-sha256": hashlib.sha256(b"\x00\x03" * 10).hexdigest(),
        })
    assert beyond.status_code == 409 and beyond.json()["code"] == "beyond_sealed"

    downloaded = await client.get(f"/api/recordings/{recording_id}/audio.wav")
    assert downloaded.status_code == 200, downloaded.text
    assert wav_samples(downloaded.content) == 3200
    assert lease not in downloaded.text and recovery not in downloaded.text


@pytest.mark.asyncio
async def test_active_wav_is_refused_and_corruption_is_detected_before_audio(api) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    assert (await chunk(client, recording_id, 0, b"\x00\x01" * 1600, 0, lease)).status_code == 200

    active = await client.get(f"/api/recordings/{recording_id}/audio.wav")
    assert active.status_code == 409 and active.json()["code"] == "recording_active"

    stopped = await client.post(f"/api/recordings/{recording_id}/stop", json={"stream_id": "s1"},
                                headers={"x-recorder-lease": lease})
    assert stopped.status_code == 200

    path = app.state.recorder.service.store.root / recording_id / "chunks" / "00000000.pcm"
    data = bytearray(path.read_bytes())
    data[0] ^= 0xFF
    path.write_bytes(bytes(data))

    corrupt = await client.get(f"/api/recordings/{recording_id}/audio.wav")
    assert corrupt.status_code == 409
    assert corrupt.json()["code"] == "recording_corrupt"
    assert not corrupt.content.startswith(b"RIFF")


@pytest.mark.asyncio
async def test_detach_rebind_and_delete_lifecycle(api) -> None:
    runtime, app, client, _ = api
    stream = await open_stream(app, client)
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    assert (await chunk(client, recording_id, 0, b"\x00\x01" * 1600, 0, lease)).status_code == 200

    stream.close()
    detached = await app.state.recorder.call(app.state.recorder.service.get, recording_id)
    assert detached["stream_detached"] is True
    blocked = await chunk(client, recording_id, 1, b"\x00\x02" * 1600, 1600, lease)
    assert blocked.status_code == 409 and blocked.json()["code"] == "stream_detached"

    await open_stream(app, client, "s2")
    rebound = await client.post(f"/api/recordings/{recording_id}/rebind",
                                json={"stream_id": "s2", "client_id": CLIENT},
                                headers={"x-recorder-lease": lease})
    assert rebound.status_code == 200, rebound.text
    accepted = await chunk(client, recording_id, 1, b"\x00\x02" * 1600, 1600, lease, stream_id="s2")
    assert accepted.status_code == 200, accepted.text

    refused = await client.delete(f"/api/recordings/{recording_id}")
    assert refused.status_code == 409 and refused.json()["code"] == "recording_active"

    stopped = await client.post(f"/api/recordings/{recording_id}/stop", json={"stream_id": "s2"},
                                headers={"x-recorder-lease": lease})
    assert stopped.status_code == 200

    subscription = runtime.bus.open()
    deleted = await client.delete(f"/api/recordings/{recording_id}")
    assert deleted.status_code == 204
    event = await wait_event(subscription, "recorder.deleted")
    subscription.close()
    assert event["data"]["id"] == recording_id
    assert (await client.get(f"/api/recordings/{recording_id}")).status_code == 404
    assert (await client.delete(f"/api/recordings/{recording_id}")).status_code == 404


@pytest.mark.asyncio
async def test_auth_content_type_and_redaction(api, caplog) -> None:
    _, app, client, _ = api
    await open_stream(app, client)
    unauthenticated = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        for method, path in (("GET", "/api/recordings"),
                             ("POST", "/api/recordings"),
                             ("PUT", f"/api/recordings/{'0' * 32}/chunks/0"),
                             ("GET", f"/api/recordings/{'0' * 32}/audio.wav"),
                             ("DELETE", f"/api/recordings/{'0' * 32}")):
            response = await unauthenticated.request(method, path)
            assert response.status_code == 401, (method, path, response.status_code)
    finally:
        await unauthenticated.aclose()

    created = await create(client)
    _, lease = created.json()["id"], created.json()["lease_token"]
    recovery = created.recording_recovery
    malformed = await client.post("/api/recordings",
                                  content=b'{"stream_id": "s1"}',
                                  headers={"content-type": "text/plain", "idempotency-key": "plain"})
    assert malformed.status_code == 415
    missing_key = await client.post("/api/recordings",
                                    json={"stream_id": "s1", "client_id": CLIENT})
    assert missing_key.status_code == 400
    assert missing_key.json()["code"] == "idempotency_key_required"

    for response in (created, malformed, missing_key):
        if response is not created:
            assert lease not in response.text and recovery not in response.text
            assert "s1" not in response.text
    assert lease not in caplog.text
    assert recovery not in caplog.text
    assert "s1" not in caplog.text


@pytest.mark.asyncio
async def test_host_admission_backpressure_maps_to_503(api) -> None:
    _, app, client, _ = api
    recorder = app.state.recorder
    original = recorder._max_pending_calls
    recorder._max_pending_calls = 0
    try:
        response = await client.get("/api/recordings")
    finally:
        recorder._max_pending_calls = original
    assert response.status_code == 503
    assert response.json()["code"] == "recorder_backpressure"
    assert response.headers["retry-after"] == "1"


@pytest.mark.asyncio
async def test_recorder_events_are_ordered_with_loop_publish(api) -> None:
    runtime, app, client, _ = api
    await open_stream(app, client)
    subscription = runtime.bus.open()
    created = await create(client)
    recording_id, lease = created.json()["id"], created.json()["lease_token"]
    runtime.bus.publish("test.marker", {"n": 1})
    stopped = await client.post(f"/api/recordings/{recording_id}/stop", json={"stream_id": "s1"},
                                headers={"x-recorder-lease": lease})
    assert stopped.status_code == 200

    events = []
    async def collect() -> None:
        while len(events) < 3:
            event = await subscription.get()
            assert event is not None
            events.append(event)

    await asyncio.wait_for(collect(), 2)
    subscription.close()
    ids = [event["id"] for event in events]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)
    assert "test.marker" in [event["type"] for event in events]
    assert "recorder.state" in [event["type"] for event in events]


@pytest.mark.asyncio
async def test_transcript_pages_live_tail_and_streamed_exports(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    audio = (transcript_speech(8_000) + transcript_silence(12_000)
             + transcript_speech(8_000) + transcript_silence(12_000)
             + transcript_speech(4_000))
    async with transcript_daemon(tmp_path) as (_, app, client, engine, _, _):
        recording_id, _, _ = await record_audio(app, client, audio)
        body = await wait_transcript(client, recording_id)
        assert body["complete"] is True and body["state"] == "complete"
        assert body["transcript_revision"] > 0
        assert body["live"] == {"provisional": None}
        assert engine.calls, "fake inference did not run"

        first = await client.get(f"/api/recordings/{recording_id}/transcript", params={"limit": 1})
        assert first.status_code == 200
        assert len(first.json()["segments"]) == 1
        after = first.json()["next_after"]
        assert after is not None

        segments = list(first.json()["segments"])
        while after is not None:
            page = await client.get(f"/api/recordings/{recording_id}/transcript",
                                    params={"after": after, "limit": 1})
            assert page.status_code == 200
            items = page.json()["segments"]
            assert len(items) <= 1
            segments.extend(items)
            after = page.json()["next_after"]
        assert len(segments) >= 2
        assert all(item.get("text") for item in segments)
        text = " ".join(item["text"] for item in segments)

        txt = await client.get(f"/api/recordings/{recording_id}/transcript.txt")
        assert txt.status_code == 200, txt.text
        assert txt.headers["content-type"].startswith("text/plain")
        assert txt.headers["x-recorder-transcript-complete"] == "true"
        assert txt.headers["cache-control"] == "no-store"
        assert txt.text == text

        exported = await client.get(f"/api/recordings/{recording_id}/transcript.json")
        assert exported.status_code == 200
        document = json.loads(exported.text)
        assert document["complete"] is True
        assert [item["segment_id"] for item in document["segments"]] == [
            item["segment_id"] for item in segments]
        assert "kestrel transcript" in exported.text

        assert (await client.get(f"/api/recordings/{recording_id}/transcript",
                                 params={"limit": 201})).status_code == 400
        assert (await client.get(f"/api/recordings/{recording_id}/transcript",
                                 params={"after": "nope"})).status_code == 400
        assert (await client.get(f"/api/recordings/{recording_id}/transcript",
                                 params={"after": -1})).status_code == 200


@pytest.mark.asyncio
async def test_transcript_unavailable_model_never_downloads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    download = DownloadSentinel()
    engine = TranscriptEngine()
    async with transcript_daemon(tmp_path, engine=engine, missing_model=True,
                                 download=download) as (_, app, client, engine, model, _):
        recording_id, _, _ = await record_audio(
            app, client, transcript_speech(8_000) + transcript_silence(12_000))
        body = await wait_transcript(client, recording_id, "unavailable")
        assert body["complete"] is False
        assert body["segments"] == []
        assert body["live"] == {"provisional": None}
        assert engine.calls == []
        assert model.calls > 0
        assert download.called is False

        txt = await client.get(f"/api/recordings/{recording_id}/transcript.txt")
        assert txt.status_code == 200 and txt.text == ""
        assert txt.headers["x-recorder-transcript-complete"] == "false"


@pytest.mark.asyncio
async def test_transcript_retry_auth_and_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    async with transcript_daemon(tmp_path) as (_, app, client, engine, _, download):
        recording_id, _, _ = await record_audio(
            app, client, transcript_speech(8_000) + transcript_silence(12_000))
        await wait_transcript(client, recording_id)
        before = len(engine.calls)

        retried = await client.post(f"/api/recordings/{recording_id}/transcript/retry",
                                    json={"scope": "all"})
        assert retried.status_code == 202, retried.text
        assert retried.json()["accepted"] >= 1
        await wait_transcript(client, recording_id)
        assert len(engine.calls) > before
        assert download.called is False

        assert (await client.post(f"/api/recordings/{recording_id}/transcript/retry",
                                  json={"scope": "nope"})).status_code == 400
        await open_stream(app, client, "s2")
        active = await create(client, stream_id="s2", key="active-key")
        new_recording = active.json()["id"]
        active_lease = active.json()["lease_token"]
        assert (await chunk(client, new_recording, 0, transcript_speech(4_000), 0,
                            active_lease, stream_id="s2")).status_code == 200
        while_active = await client.post(f"/api/recordings/{new_recording}/transcript/retry",
                                         json={"scope": "all"})
        assert while_active.status_code == 409
        assert while_active.json()["code"] == "retry_while_active"
        await client.post(f"/api/recordings/{new_recording}/stop",
                          json={"stream_id": "s2"}, headers={"x-recorder-lease": active_lease})

        unauthenticated = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            for method, path in (("GET", f"/api/recordings/{recording_id}/transcript"),
                                 ("GET", f"/api/recordings/{recording_id}/transcript.txt"),
                                 ("GET", f"/api/recordings/{recording_id}/transcript.json"),
                                 ("POST", f"/api/recordings/{recording_id}/transcript/retry")):
                assert (await unauthenticated.request(method, path)).status_code == 401
        finally:
            await unauthenticated.aclose()


@pytest.mark.asyncio
async def test_transcript_privacy_and_delete_removes_text_and_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    async with transcript_daemon(tmp_path) as (runtime, app, client, _, _, _):
        subscription = runtime.bus.open()
        recording_id, _, _ = await record_audio(
            app, client, transcript_speech(8_000) + transcript_silence(12_000))
        body = await wait_transcript(client, recording_id)
        text = body["segments"][0]["text"]
        assert text
        # Collect the live segment events that carried this text.
        events: list[dict[str, Any]] = []
        for _ in range(20):
            try:
                events.append(await asyncio.wait_for(subscription.get(), 0.05))
            except TimeoutError:
                break
        segments = [event for event in events if event["type"] == "recorder.segment"]
        assert segments, events
        assert all(event["id"] is None for event in segments)
        assert all(event["session_id"] is None for event in segments)
        assert not any(event.get("durable") for event in segments)

        database = b"".join(path.read_bytes() for path in tmp_path.glob("nexus.db*"))
        assert text.encode() not in database

        recording_dir = tmp_path / "home" / "recordings" / recording_id
        assert recording_dir.is_dir()
        deleted = await client.delete(f"/api/recordings/{recording_id}")
        assert deleted.status_code == 204
        assert not recording_dir.exists()
        assert (await client.get(f"/api/recordings/{recording_id}/transcript")).status_code == 404
        assert (await client.get(f"/api/recordings/{recording_id}/transcript.txt")).status_code == 404
        assert (await client.post(f"/api/recordings/{recording_id}/transcript/retry",
                                  json={"scope": "all"})).status_code == 404
        await asyncio.sleep(0.1)
        assert not recording_dir.exists()

        deleted_event = None
        for _ in range(40):
            try:
                event = await asyncio.wait_for(subscription.get(), 0.05)
            except TimeoutError:
                continue
            if event["type"] == "recorder.deleted" and event["data"]["id"] == recording_id:
                deleted_event = event
                break
        assert deleted_event is not None
        after_delete: list[dict[str, Any]] = []
        for _ in range(20):
            try:
                event = await asyncio.wait_for(subscription.get(), 0.05)
            except TimeoutError:
                break
            if event is not None:
                after_delete.append(event)
        subscription.close()
        assert not any(event["type"] == "recorder.segment"
                       and event["data"]["id"] == recording_id for event in after_delete)


@pytest.mark.asyncio
async def test_transcript_survives_daemon_restart_without_reinference(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    audio = transcript_speech(8_000) + transcript_silence(12_000)
    async with transcript_daemon(tmp_path) as (_, app, client, first_engine, _, _):
        recording_id, _, _ = await record_audio(app, client, audio)
        before = await wait_transcript(client, recording_id)
        assert before["segments"]
        assert first_engine.calls

    second_engine = TranscriptEngine()
    async with transcript_daemon(tmp_path, engine=second_engine) as (_, app, client, _, _, _):
        after = await wait_transcript(client, recording_id)
        assert after["state"] == "complete" and after["complete"] is True
        assert [item["segment_id"] for item in after["segments"]] == [
            item["segment_id"] for item in before["segments"]]
        assert [item["text"] for item in after["segments"]] == [
            item["text"] for item in before["segments"]]
        assert after["transcript_revision"] >= before["transcript_revision"]
        assert second_engine.calls == []


@pytest.mark.asyncio
async def test_transcript_export_pins_delete_and_releases_on_disconnect(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    # One segment per page makes the export wait for a second page; gate it so the test can delete in between.
    monkeypatch.setattr(recorder_api_module, "TRANSCRIPT_EXPORT_PAGE", 1)
    original_reader = recorder_api_module._read_transcript_page
    gates: dict[tuple[str, int], threading.Event] = {}

    def gated_reader(store: Any, recording_id: str, after: int, limit: int) -> dict[str, Any]:
        gate = gates.get((recording_id, after))
        if gate is not None:
            gate.wait(timeout=5)
        return original_reader(store, recording_id, after, limit)

    monkeypatch.setattr(recorder_api_module, "_read_transcript_page", gated_reader)
    audio = transcript_speech(8_000) + transcript_silence(12_000)
    engine = TranscriptEngine()
    async with live_transcript_daemon(tmp_path, engine=engine) as (app, client, _):
        recording_id, _, _ = await record_audio(app, client, audio)
        await wait_transcript(client, recording_id, timeout=15)

        gate = threading.Event()
        gates[(recording_id, 0)] = gate
        try:
            async with client.stream(
                "GET", f"/api/recordings/{recording_id}/transcript.json",
                headers={"accept-encoding": "identity"},
            ) as response:
                assert response.status_code == 200
                chunks = response.aiter_bytes()
                first = await asyncio.wait_for(anext(chunks), 5)
                assert first
                deleted = await client.delete(f"/api/recordings/{recording_id}")
                assert deleted.status_code == 409
                assert deleted.json()["code"] == "recording_export_active"
                gate.set()
                rest = b"".join([chunk async for chunk in chunks])
            document = json.loads(first + rest)
            assert document["complete"] is True
            assert document["segments"], "delete between pages truncated the export"
        finally:
            gate.set()
            gates.pop((recording_id, 0), None)

        # Once the response finishes, the pin is gone and deletion succeeds.
        assert (await client.delete(f"/api/recordings/{recording_id}")).status_code == 204

        # A disconnected export must release its pin as well.
        second, _, _ = await record_audio(app, client, audio, key="disconnect-key")
        await wait_transcript(client, second, timeout=15)
        disconnect_gate = threading.Event()
        gates[(second, 0)] = disconnect_gate
        try:
            async with client.stream(
                "GET", f"/api/recordings/{second}/transcript.txt",
                headers={"accept-encoding": "identity"},
            ) as response:
                assert response.status_code == 200
                assert await asyncio.wait_for(anext(response.aiter_bytes()), 5)
            # The context exit disconnects mid-export; let the shielded page read finish.
            disconnect_gate.set()
            for _ in range(200):
                deleted = await client.delete(f"/api/recordings/{second}")
                if deleted.status_code == 204:
                    break
                assert deleted.status_code == 409
                assert deleted.json()["code"] == "recording_export_active"
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("the disconnected export never released its pin")
        finally:
            disconnect_gate.set()
            gates.pop((second, 0), None)


@pytest.mark.asyncio
async def test_wav_export_pins_delete_until_stream_finishes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    original_next = recorder_api_module._next_block
    calls = 0
    counter_lock = threading.Lock()
    gate = threading.Event()

    def gated_next(iterator: Any) -> Any:
        nonlocal calls
        with counter_lock:
            calls += 1
            call = calls
        if call >= 2:  # the first call primes the header before response headers are sent
            gate.wait(timeout=5)
        return original_next(iterator)

    monkeypatch.setattr(recorder_api_module, "_next_block", gated_next)
    audio = transcript_speech(20_000) + transcript_silence(20_000)
    async with live_transcript_daemon(tmp_path) as (app, client, _):
        recording_id, _, _ = await record_audio(app, client, audio)
        try:
            async with client.stream(
                "GET", f"/api/recordings/{recording_id}/audio.wav",
                headers={"accept-encoding": "identity"},
            ) as response:
                assert response.status_code == 200
                chunks = response.aiter_bytes()
                first = await asyncio.wait_for(anext(chunks), 5)
                assert first[:4] == b"RIFF"
                deleted = await client.delete(f"/api/recordings/{recording_id}")
                assert deleted.status_code == 409
                assert deleted.json()["code"] == "recording_export_active"
                gate.set()
                rest = b"".join([chunk async for chunk in chunks])
            assert wav_samples(first + rest) == len(audio) // 2
        finally:
            gate.set()
        assert (await client.delete(f"/api/recordings/{recording_id}")).status_code == 204


@pytest.mark.asyncio
async def test_export_error_paths_release_pins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    async with transcript_daemon(tmp_path, missing_model=True) as (_, app, client, _, _, _):
        # WAV refused before a body is prepared (active capture) must not leave a pin behind.
        await open_stream(app, client)
        active = await create(client, key="active-key")
        active_id, active_lease = active.json()["id"], active.json()["lease_token"]
        piece = transcript_speech(4_000) + transcript_silence(2_000)
        assert (await chunk(client, active_id, 0, piece, 0, active_lease)).status_code == 200
        refused = await client.get(f"/api/recordings/{active_id}/audio.wav")
        assert refused.status_code == 409 and refused.json()["code"] == "recording_active"
        assert (await client.post(f"/api/recordings/{active_id}/stop", json={"stream_id": "s1"},
                                  headers={"x-recorder-lease": active_lease})).status_code == 200
        assert (await client.delete(f"/api/recordings/{active_id}")).status_code == 204

        # A transcript reader failure after the pin is acquired must release it before answering.
        recording_id, _, _ = await record_audio(
            app, client, transcript_speech(8_000) + transcript_silence(12_000), key="error-key")
        original_reader = recorder_api_module._read_transcript_page

        def failing_reader(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RecorderCorrupt("transcript_corrupt", "injected transcript read failure")

        monkeypatch.setattr(recorder_api_module, "_read_transcript_page", failing_reader)
        try:
            failed = await client.get(f"/api/recordings/{recording_id}/transcript.json")
        finally:
            monkeypatch.setattr(recorder_api_module, "_read_transcript_page", original_reader)
        assert failed.status_code == 409
        assert failed.json()["code"] == "transcript_corrupt"
        assert (await client.delete(f"/api/recordings/{recording_id}")).status_code == 204


@pytest.mark.asyncio
async def test_transcript_export_retries_later_page_backpressure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(recorder_api_module, "TRANSCRIPT_EXPORT_PAGE", 1)
    audio = transcript_speech(8_000) + transcript_silence(12_000)
    engine = TranscriptEngine()
    async with live_transcript_daemon(tmp_path, engine=engine) as (app, client, _):
        recording_id, _, _ = await record_audio(app, client, audio)
        await wait_transcript(client, recording_id, timeout=15)
        recorder = app.state.recorder
        original_call = recorder.call
        failed_once = False
        failed = asyncio.Event()

        async def flaky_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal failed_once
            if (fn is recorder_api_module._read_transcript_page
                    and len(args) >= 3 and args[2] == 0 and not failed_once):
                failed_once = True
                failed.set()
                raise RecorderUnavailable("busy", code="recorder_backpressure", retry_after=1)
            return await original_call(fn, *args, **kwargs)

        recorder.call = flaky_call
        try:
            async with client.stream(
                "GET", f"/api/recordings/{recording_id}/transcript.json",
                headers={"accept-encoding": "identity"},
            ) as response:
                assert response.status_code == 200
                chunks = response.aiter_bytes()
                first = await asyncio.wait_for(anext(chunks), 5)
                assert first
                await asyncio.wait_for(failed.wait(), 5)
                # The retry wait keeps the export pinned; deletion is refused, not truncated.
                deleted = await client.delete(f"/api/recordings/{recording_id}")
                assert deleted.status_code == 409
                assert deleted.json()["code"] == "recording_export_active"
                rest = b"".join([chunk async for chunk in chunks])
            document = json.loads(first + rest)
            assert document["complete"] is True
            assert document["segments"]
        finally:
            recorder.call = original_call
        assert (await client.delete(f"/api/recordings/{recording_id}")).status_code == 204


@pytest.mark.asyncio
async def test_transcript_export_is_allowed_while_capturing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    async with transcript_daemon(tmp_path) as (_, app, client, _, _, _):
        await open_stream(app, client)
        created = await create(client)
        recording_id, lease = created.json()["id"], created.json()["lease_token"]
        piece = transcript_speech(4_000) + transcript_silence(8_000)
        assert (await chunk(client, recording_id, 0, piece, 0, lease)).status_code == 200

        page = await client.get(f"/api/recordings/{recording_id}/transcript")
        assert page.status_code == 200
        assert page.json()["complete"] is False
        txt = await client.get(f"/api/recordings/{recording_id}/transcript.txt")
        assert txt.status_code == 200
        assert txt.headers["x-recorder-transcript-complete"] == "false"
        exported = await client.get(f"/api/recordings/{recording_id}/transcript.json")
        assert exported.status_code == 200
        assert json.loads(exported.text)["complete"] is False


@pytest.mark.asyncio
async def test_event_stream_delivers_global_recorder_state(tmp_path, monkeypatch) -> None:
    """`GET /api/event` over real HTTP: recorder events are global and arrive live."""
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    runtime = make_runtime(Store(tmp_path / "nexus.db"))
    app = build_app(runtime, token=TOKEN, url="http://127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    if hasattr(server, "install_signal_handlers"):
        server.install_signal_handlers = lambda: None  # the test owns the loop and its signals
    serving = asyncio.create_task(server.serve())
    task: asyncio.Task[None] | None = None
    try:
        for _ in range(300):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the test daemon did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5,
                                     headers={"authorization": f"Bearer {TOKEN}"}) as client:
            await open_stream(app, client)
            connected = asyncio.Event()
            received: list[dict[str, Any]] = []

            async def listen() -> None:
                async with client.stream("GET", "/api/event") as response:
                    assert response.status_code == 200
                    event_type: str | None = None
                    async for line in response.aiter_lines():
                        if line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: "):
                            data = json.loads(line[6:])
                            if event_type == "server.connected":
                                connected.set()
                            elif event_type == "recorder.state":
                                received.append(data)
                                return

            task = asyncio.create_task(listen())
            await asyncio.wait_for(connected.wait(), 3)
            created = await create(client)
            assert created.status_code == 201, created.text
            await asyncio.wait_for(task, 5)

            assert received, "no recorder.state arrived on /api/event"
            assert received[0]["data"]["id"] == created.json()["id"]
            assert received[0]["session_id"] is None
            assert received[0]["data"]["reason"] == "created"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        runtime.store.close()


@pytest.mark.asyncio
async def test_create_and_rebind_reach_the_host_as_bound_service_methods(api) -> None:
    """The host reconciles detach/overflow by stream, so it must see the real service methods."""
    _, app, client, _ = api
    recorder = app.state.recorder
    await open_stream(app, client)
    seen: list[Any] = []
    original = recorder.call

    async def spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(fn)
        return await original(fn, *args, **kwargs)

    recorder.call = spy
    try:
        created = await create(client)
        assert created.status_code == 201, created.text
        recording_id, lease = created.json()["id"], created.json()["lease_token"]
        rebound = await client.post(f"/api/recordings/{recording_id}/rebind",
                                    json={"stream_id": "s1", "client_id": CLIENT},
                                    headers={"x-recorder-lease": lease})
        assert rebound.status_code == 200, rebound.text
    finally:
        recorder.call = original

    for name in ("create", "rebind"):
        assert any(getattr(fn, "__name__", None) == name and getattr(fn, "__self__", None) is recorder.service
                   for fn in seen), name
