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


def test_untrusted_revision_rejected(tmp_path):
    with pytest.raises(ValueError):
        ModelStore(tmp_path, revision="main")


@pytest.mark.parametrize("name", ["../outside", "/outside", ".", ".."])
def test_manifest_paths_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError):
        ModelStore(tmp_path, manifest={name: (1, "0" * 64)})
