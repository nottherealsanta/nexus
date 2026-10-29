"""Consent-gated downloads into the standard Hugging Face hub cache."""

from __future__ import annotations

from . import hf_cache
from .store import REPOS, ModelPaths, ModelStore


def download_stt(store: ModelStore) -> ModelPaths:
    """Download only the two pinned manifest allowlists, then verify every byte."""
    from huggingface_hub import snapshot_download

    for repo in REPOS:
        snapshot_download(
            repo_id=repo.repo,
            revision=repo.revision,
            allow_patterns=list(repo.files),
            cache_dir=str(hf_cache.cache_dir()),
            local_files_only=False,
        )
    return store.verified_paths()


__all__ = ["download_stt"]


def download_tts(repo: str, revision: str, files: list[str]) -> None:
    """Fetch the TTS model into the standard cache; the caller has the user's consent."""
    from huggingface_hub import snapshot_download

    snapshot_download(repo_id=repo, revision=revision, allow_patterns=files or None,
                      cache_dir=str(hf_cache.cache_dir()), local_files_only=False)


def remove(repo: str) -> int:
    """Delete one repo's folder from the cache (nothing else); returns the bytes freed."""
    import shutil

    folder = hf_cache.repo_dir(repo)
    if not folder.is_dir() or folder.parent != hf_cache.cache_dir():
        return 0
    freed = hf_cache.size(repo)
    shutil.rmtree(folder)
    return freed
