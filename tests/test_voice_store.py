"""No model bytes become active before verification (VOICE_PLAN sections 4/9)."""
import asyncio
import hashlib
import io

import pytest

from nexus.voice.store import ModelStore


def make_store(tmp_path, payload=b"verified model", fetch=None):
    manifest = {"weights": (len(payload), hashlib.sha256(payload).hexdigest())}
    return ModelStore(tmp_path, manifest=manifest, fetch=fetch or (lambda *_a, **_kw: io.BytesIO(payload)))


async def test_verified_cache_is_reused_and_removable(tmp_path):
    calls = []
    payload = b"model"
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(payload)
    store = make_store(tmp_path, payload, fetch)
    progress = []
    path = await store.ensure(lambda done, total: progress.append((done, total)))
    await asyncio.sleep(0)
    assert (path / "weights").read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert await store.ensure(lambda *_: None) == path
    assert len(calls) == 1
    await store.remove()
    assert not path.exists()


@pytest.mark.parametrize("bad", [b"corrupt model!", b"too long" * 100, b""])
async def test_failed_download_never_activates(tmp_path, monkeypatch, bad):
    monkeypatch.setattr("nexus.voice.store.time.sleep", lambda _: None)
    store = make_store(tmp_path, fetch=lambda *_a, **_kw: io.BytesIO(bad))
    with pytest.raises(ValueError):
        await store.ensure(lambda *_: None)
    assert not store.path.exists()
    assert not store.path.with_name(store.revision + ".partial").exists()


async def test_corrupt_cache_is_replaced(tmp_path):
    store = make_store(tmp_path)
    store.path.mkdir(parents=True)
    (store.path / "weights").write_bytes(b"corrupt")
    await store.ensure(lambda *_: None)
    assert (store.path / "weights").read_bytes() == b"verified model"


async def test_concurrent_stores_download_once(tmp_path):
    calls = []
    def fetch(*args, **kwargs):
        calls.append(True)
        return io.BytesIO(b"verified model")
    first, second = make_store(tmp_path, fetch=fetch), make_store(tmp_path, fetch=fetch)
    paths = await asyncio.gather(first.ensure(lambda *_: None), second.ensure(lambda *_: None))
    assert paths[0] == paths[1] and len(calls) == 1


def test_default_manifest_pins_bark_and_processor_files(tmp_path):
    from nexus.voice import store as voice_store

    store = ModelStore(tmp_path)
    assert store.path == tmp_path / "models" / "voice" / "bark-3.0-clean" / voice_store.REVISION
    assert voice_store.MANIFEST["model.bark"] == (
        179_458_613, "b67ca90d94b79a36af4616f1cb3f241884925de7aa9f3e673a8d5c6c4cfd3436",
    )
    assert sorted(voice_store.MANIFEST) == [
        "config.json", "generation_config.json", "model.bark", "processor_config.json",
        "tokenizer.json", "tokenizer_config.json",
    ]
    assert sum(size for size, _ in voice_store.MANIFEST.values()) <= voice_store.MAX_DOWNLOAD_BYTES
    bark = "https://huggingface.co/ConwayResearch/Underdog-Bark-3.0-Clean/resolve/"
    nvidia = "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/"
    assert store._url("model.bark") == bark + "6d34ed8639b2b9a33e764acf06748519bbb950cf/model.bark"
    assert store._url("tokenizer.json") == (
        nvidia + "541d1f99c6b0c3cd0b11a95167540bb8edefd82b/tokenizer.json"
    )


async def test_default_manifest_fetches_each_file_from_its_pinned_repo(tmp_path):
    from nexus.voice import store as voice_store

    urls = []
    payloads = {name: name.encode() for name in voice_store.MANIFEST}
    manifest = {n: (len(p), hashlib.sha256(p).hexdigest()) for n, p in payloads.items()}

    def fetch(request, **kwargs):
        urls.append(request.full_url)
        return io.BytesIO(payloads[request.full_url.rsplit("/", 1)[1]])

    store = ModelStore(tmp_path, manifest=manifest, fetch=fetch)
    path = await store.ensure(lambda *_: None)
    assert sorted(p.name for p in path.iterdir()) == sorted(payloads)
    assert len(urls) == 6
    assert sum("/ConwayResearch/Underdog-Bark-3.0-Clean/resolve/6d34ed86" in u for u in urls) == 1
    assert sum("/nvidia/parakeet-tdt-0.6b-v3/resolve/541d1f99" in u for u in urls) == 5


async def test_install_prunes_legacy_kestrel_cache(tmp_path):
    legacy = tmp_path / "models" / "voice" / "parakeet-redux" / "rev"
    legacy.mkdir(parents=True)
    (legacy / "model.safetensors").write_bytes(b"old")
    store = make_store(tmp_path)
    await store.ensure(lambda *_: None)
    assert not legacy.parent.exists()


def test_untrusted_revision_rejected(tmp_path):
    with pytest.raises(ValueError):
        ModelStore(tmp_path, revision="main")


@pytest.mark.parametrize("name", ["../outside", "/outside", ".", ".."])
def test_manifest_paths_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError):
        ModelStore(tmp_path, manifest={name: (1, "0" * 64)})
