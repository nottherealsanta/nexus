"""HTTP voice endpoints exercise Core with a fake inference engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from conftest import remembering

from nexus.server import build_app, make_runtime
from nexus.store import Store
from nexus.voice.audio import silence
from nexus.voice.manager import VoiceManager
from nexus.voice.store import EXPECTED_SIZES, MODEL_ID, PROCESSOR_ID, ModelPaths

TOKEN = "voice-test-token"


class FakeStore:
    def __init__(self) -> None:
        self.sizes = {MODEL_ID: 0, PROCESSOR_ID: 0}

    def verified_paths(self) -> ModelPaths:
        return ModelPaths(Path("/fake/bark"), Path("/fake/processor"))

    def cache_sizes(self) -> dict[str, int]:
        return dict(self.sizes)


class FakeEngine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def load(self) -> None:
        pass

    async def transcribe(self, audio: bytes) -> str:
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        return "editable transcript"

    async def close(self) -> None:
        pass


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf-hub"))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/model"))
    engine, store = FakeEngine(), FakeStore()

    def download(target):
        target.sizes = dict(EXPECTED_SIZES)
        return ModelPaths(Path("/fake/bark"), Path("/fake/processor"))

    voice = VoiceManager(
        engine_factory=lambda _paths: engine,
        store=store,
        download_runner=download,
        idle_unload_seconds=0,
    )
    app = build_app(rt, token=TOKEN, url="http://test", voice_manager=voice)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    yield rt, client, voice, engine
    await client.aclose()
    await voice.close()
    rt.store.close()


@pytest.mark.asyncio
async def test_prepare_loads_without_transcribing(setup) -> None:
    _, client, voice, engine = setup
    response = await client.post("/api/voice/stt/prepare")
    assert response.status_code == 200 and response.json() == {"state": "loaded"}
    assert voice.loaded
    assert not engine.started.is_set()


@pytest.mark.asyncio
async def test_prepare_reports_missing_model_without_loading(setup, monkeypatch) -> None:
    _, client, voice, _ = setup

    def missing():
        raise FileNotFoundError

    monkeypatch.setattr(voice.store, "verified_paths", missing)
    response = await client.post("/api/voice/stt/prepare")
    assert response.status_code == 409
    assert response.json()["code"] == "voice_model_not_cached"
    assert not voice.loaded and not voice.busy


@pytest.mark.asyncio
async def test_transcribe_returns_editable_text_and_never_sends_it(setup) -> None:
    _, client, voice, _ = setup
    response = await client.post("/api/voice/transcribe", content=silence(0.3))
    assert response.status_code == 200 and response.json() == {"text": "editable transcript"}
    assert voice.loaded


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"not wav"])
async def test_transcribe_rejects_bad_wav_as_json(setup, body) -> None:
    _, client, voice, _ = setup
    response = await client.post("/api/voice/transcribe", content=body)
    assert response.status_code == 400
    assert response.json()["code"] == "voice_bad_audio"
    assert not voice.loaded


@pytest.mark.asyncio
async def test_partial_preview_gets_voice_busy_while_inference_runs(setup) -> None:
    _, client, _, engine = setup
    engine.gate = asyncio.Event()
    final = asyncio.create_task(client.post("/api/voice/transcribe", content=silence(1.0)))
    await engine.started.wait()
    response = await client.post("/api/voice/transcribe?partial=1", content=silence(0.3))
    assert response.status_code == 409 and response.json()["code"] == "voice_busy"
    engine.gate.set()
    assert (await final).status_code == 200


@pytest.mark.asyncio
async def test_download_is_explicit_and_publishes_voice_progress(setup) -> None:
    rt, client, voice, _ = setup
    subscription = rt.bus.open()
    response = await client.post("/api/voice/stt/download")
    assert response.status_code == 202 and response.json()["state"] == "downloading"
    started = await asyncio.wait_for(subscription.get(), 1)
    assert started["data"]["state"] == "downloading"
    updates = []
    while True:
        event = await asyncio.wait_for(subscription.get(), 1)
        updates.append(event)
        if event["data"]["state"] == "ready":
            break
    assert all(event["type"] == "voice.progress" for event in updates)
    assert not voice.loaded
    subscription.close()


@pytest.mark.asyncio
async def test_partial_query_is_validated(setup) -> None:
    _, client, _, _ = setup
    response = await client.post("/api/voice/transcribe?partial=true", content=silence(0.3))
    assert response.status_code == 400


def test_remove_deletes_only_that_repo(tmp_path, monkeypatch):
    from nexus.voice import download, hf_cache

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    for repo in ("a/b", "c/d"):
        folder = hf_cache.repo_dir(repo) / "blobs"
        folder.mkdir(parents=True)
        (folder / "x").write_bytes(b"12345")
    assert download.remove("a/b") == 5
    assert not hf_cache.repo_dir("a/b").exists() and hf_cache.repo_dir("c/d").exists()
    assert download.remove("a/b") == 0


@pytest.mark.asyncio
async def test_delete_removes_the_configured_model_and_refuses_unknown_targets(setup, tmp_path) -> None:
    from nexus.voice import hf_cache

    _, client, _, _ = setup
    folder = hf_cache.repo_dir("gradium/phonon-31m-en") / "blobs"
    folder.mkdir(parents=True)
    (folder / "x").write_bytes(b"123")
    response = await client.delete("/api/voice/tts")
    assert response.status_code == 200 and response.json()["freed"] == 3
    assert not hf_cache.repo_dir("gradium/phonon-31m-en").exists()
    assert (await client.delete("/api/voice/other")).status_code == 404


@pytest.mark.asyncio
async def test_tts_download_runs_in_the_background(setup, monkeypatch) -> None:
    from nexus.voice import download

    _, client, _, _ = setup
    seen = []
    monkeypatch.setattr(download, "download_tts", lambda repo, rev, files: seen.append((repo, rev)))
    response = await client.post("/api/voice/tts/download")
    assert response.status_code == 202
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [("gradium/phonon-31m-en", "main")]
