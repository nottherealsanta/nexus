"""Pinned HF-cache manifest and hash verification, without downloads."""

from __future__ import annotations

import hashlib
import os
import sys
import time
import types

import pytest

from nexus.voice import download, store


def test_pinned_repository_and_allowlist() -> None:
    assert store.MODEL_ID == "moondream/parakeet-redux"
    assert store.REVISION == "2bf128600aac4b16946f7ed8372e56117fe5e23b"
    assert store.REPOS == (store.RepoSnapshot(store.MODEL_ID, store.REVISION, tuple(store.MANIFEST)),)
    assert set(store.MANIFEST) == {"model.safetensors", "config.json", "ternary.json", "tokenizer.json"}
    assert store.MANIFEST["model.safetensors"] == (
        177_774_490,
        "78ec25733ee0d0c1586d1346fc86db9d0c2e436e3a8ab1d32a82d1bb8f848d21",
    )
    assert store.MANIFEST["ternary.json"][0] == 57_970
    assert store.EXPECTED_SIZE == 179_005_408 == sum(size for size, _ in store.MANIFEST.values())


def test_bark_repo_has_an_explicit_migration_message() -> None:
    reason = store.unsupported_reason(store.BARK_MODEL_ID)
    assert reason is not None
    assert "no longer supported" in reason
    assert "moondream/parakeet-redux" in reason
    assert "config.toml" in reason
    assert "left in place" in reason
    assert store.unsupported_reason(store.MODEL_ID) is None
    assert "Only the pinned" in (store.unsupported_reason("org/other") or "")


def test_verify_snapshot_checks_size_and_sha256(tmp_path) -> None:
    payload = b"verified local model data"
    manifest = {"weights": (len(payload), hashlib.sha256(payload).hexdigest())}
    (tmp_path / "weights").write_bytes(payload)
    assert store.verify_snapshot("test/repo", tmp_path, manifest) == tmp_path

    (tmp_path / "weights").write_bytes(payload + b"!")
    with pytest.raises(store.ModelVerificationError, match="size mismatch"):
        store.verify_snapshot("test/repo", tmp_path, manifest)
    (tmp_path / "weights").write_bytes(b"x" * len(payload))
    with pytest.raises(store.ModelVerificationError, match="hash mismatch"):
        store.verify_snapshot("test/repo", tmp_path, manifest)


def test_verify_snapshot_rejects_unknown_repo_and_missing_files(tmp_path) -> None:
    with pytest.raises(ValueError, match="no pinned"):
        store.verify_snapshot("org/unknown", tmp_path)
    with pytest.raises(store.ModelVerificationError, match="missing"):
        store.verify_snapshot("test/repo", tmp_path, {"missing": (1, "0" * 64)})


def test_download_uses_pinned_revision_allowlist_and_standard_cache(monkeypatch, tmp_path) -> None:
    model_bytes, config_bytes = b"model", b"config"
    manifest = {
        "model.safetensors": (len(model_bytes), hashlib.sha256(model_bytes).hexdigest()),
        "config.json": (len(config_bytes), hashlib.sha256(config_bytes).hexdigest()),
    }
    repos = (store.RepoSnapshot("org/photon", "a" * 40, tuple(manifest)),)
    monkeypatch.setattr(store, "MODEL_ID", "org/photon")
    monkeypatch.setattr(store, "REPOS", repos)
    monkeypatch.setattr(store, "MANIFEST", manifest)
    monkeypatch.setattr(download, "REPOS", repos)
    monkeypatch.setattr("nexus.voice.hf_cache.cache_dir", lambda: tmp_path / "hub")
    calls = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        repo = repos[0]
        root = tmp_path / "hub" / ("models--" + repo.repo.replace("/", "--"))
        (root / "refs").mkdir(parents=True, exist_ok=True)
        (root / "refs" / repo.revision).write_text(repo.revision)
        snapshot = root / "snapshots" / repo.revision
        snapshot.mkdir(parents=True, exist_ok=True)
        for name in kwargs["allow_patterns"]:
            (snapshot / name).write_bytes(model_bytes if name == "model.safetensors" else config_bytes)
        return str(snapshot)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    paths = download.download_stt(store.ModelStore())
    assert paths.model_dir.name == "a" * 40
    assert calls == [{
        "repo_id": "org/photon",
        "revision": "a" * 40,
        "allow_patterns": ["model.safetensors", "config.json"],
        "cache_dir": str(tmp_path / "hub"),
        "local_files_only": False,
    }]


def test_metadata_memo_detects_changed_bytes_with_restored_mtime(monkeypatch, tmp_path):
    payload, replacement = b"good", b"evil"
    manifest = {"weights": (len(payload), hashlib.sha256(payload).hexdigest())}
    repo = store.RepoSnapshot("org/model", "a" * 40, ("weights",))
    monkeypatch.setattr(store, "MODEL_ID", repo.repo)
    monkeypatch.setattr(store, "REPOS", (repo,))
    monkeypatch.setattr(store, "MANIFEST", manifest)
    monkeypatch.setattr("nexus.voice.hf_cache.cache_dir", lambda: tmp_path / "hub")
    root = tmp_path / "hub" / "models--org--model"
    (root / "refs").mkdir(parents=True)
    (root / "refs" / repo.revision).write_text(repo.revision)
    snapshot = root / "snapshots" / repo.revision
    snapshot.mkdir(parents=True)
    weights = snapshot / "weights"
    weights.write_bytes(payload)

    model_store = store.ModelStore()
    assert model_store.verified_paths().model_dir == snapshot
    before = weights.stat()
    time.sleep(0.02)
    weights.write_bytes(replacement)
    os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert weights.stat().st_size == before.st_size
    with pytest.raises(store.ModelVerificationError, match="hash mismatch"):
        model_store.verified_paths()
