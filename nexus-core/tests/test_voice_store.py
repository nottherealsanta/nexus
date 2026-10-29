"""Pinned HF-cache manifest and hash verification, without downloads.

Adapted from old Nexus tests/test_voice_store.py (manifest and hashes only).
"""

from __future__ import annotations

import hashlib
import sys
import types

import pytest

from nexus.voice import download, store


def test_pinned_repositories_and_allowlists() -> None:
    assert store.REPOS[0].repo == "ConwayResearch/Underdog-Bark-3.0-Clean"
    assert store.REPOS[0].revision == "6d34ed8639b2b9a33e764acf06748519bbb950cf"
    assert store.REPOS[0].files == ("model.bark",)
    assert store.REPOS[1].repo == "nvidia/parakeet-tdt-0.6b-v3"
    assert store.REPOS[1].revision == "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
    assert set(store.REPOS[1].files) == {
        "config.json", "generation_config.json", "processor_config.json", "tokenizer.json", "tokenizer_config.json",
    }
    assert store.MANIFEST["model.bark"][0] == 179_458_613
    assert store.EXPECTED_SIZE == sum(size for size, _ in store.MANIFEST.values())


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


def test_download_uses_pinned_revisions_allowlists_and_standard_cache(monkeypatch, tmp_path) -> None:
    bark_bytes, processor_bytes = b"bark", b"processor"
    bark_name, processor_name = "model.bark", "config.json"
    manifest = {
        bark_name: (len(bark_bytes), hashlib.sha256(bark_bytes).hexdigest()),
        processor_name: (len(processor_bytes), hashlib.sha256(processor_bytes).hexdigest()),
    }
    repos = (
        store.RepoSnapshot("org/bark", "a" * 40, (bark_name,)),
        store.RepoSnapshot("org/processor", "b" * 40, (processor_name,)),
    )
    monkeypatch.setattr(store, "MODEL_ID", "org/bark")
    monkeypatch.setattr(store, "PROCESSOR_ID", "org/processor")
    monkeypatch.setattr(store, "REPOS", repos)
    monkeypatch.setattr(store, "MANIFEST", manifest)
    monkeypatch.setattr(download, "REPOS", repos)
    monkeypatch.setattr("nexus.voice.hf_cache.cache_dir", lambda: tmp_path / "hub")
    calls = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        repo = next(item for item in repos if item.repo == kwargs["repo_id"])
        commit = repo.revision
        root = tmp_path / "hub" / ("models--" + repo.repo.replace("/", "--"))
        (root / "refs").mkdir(parents=True, exist_ok=True)
        (root / "refs" / repo.revision).write_text(commit)
        snapshot = root / "snapshots" / commit
        snapshot.mkdir(parents=True, exist_ok=True)
        data = bark_bytes if repo.repo == "org/bark" else processor_bytes
        for name in kwargs["allow_patterns"]:
            (snapshot / name).write_bytes(data)
        return str(snapshot)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    paths = download.download_stt(store.ModelStore())
    assert paths.model_dir.name == "a" * 40 and paths.processor_dir.name == "b" * 40
    assert len(calls) == 2
    assert [(c["repo_id"], c["revision"], c["allow_patterns"]) for c in calls] == [
        ("org/bark", "a" * 40, ["model.bark"]),
        ("org/processor", "b" * 40, ["config.json"]),
    ]
    assert all(c["cache_dir"] == str(tmp_path / "hub") and not c["local_files_only"] for c in calls)
