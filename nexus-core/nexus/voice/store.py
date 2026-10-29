"""Pinned STT snapshot manifest and integrity checks.

Model bytes live only in the standard Hugging Face hub cache. This module has
no downloader and never consults Nexus state directories.

Adapted from old Nexus nexus/voice/store.py (manifest and hashes only).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import hf_cache

MODEL_ID = "ConwayResearch/Underdog-Bark-3.0-Clean"
REVISION = "6d34ed8639b2b9a33e764acf06748519bbb950cf"
PROCESSOR_ID = "nvidia/parakeet-tdt-0.6b-v3"
PROCESSOR_REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
MODEL_FILE = "model.bark"

# The Bark checkpoint is a custom tensor container. Processor configuration,
# tokenizer and feature-extractor files come from the separate pinned Parakeet
# processor snapshot; no NVIDIA model weights are fetched.
MANIFEST: dict[str, tuple[int, str]] = {
    MODEL_FILE: (
        179_458_613,
        "b67ca90d94b79a36af4616f1cb3f241884925de7aa9f3e673a8d5c6c4cfd3436",
    ),
    "config.json": (
        1_153,
        "e747b85e1bdfd300c8b8ac63bac8dd5221f8fe9bc275b48d06c735fcd6971b6e",
    ),
    "generation_config.json": (
        289,
        "b141de6ec6d7f982ece13f98f604e3fe1807ea9c0e839185d0ab7064604209d0",
    ),
    "processor_config.json": (
        392,
        "8346a93a3b987fa1dec57a78f045cd0817d21786589a5a096b41a57a446fd1d7",
    ),
    "tokenizer.json": (
        1_159_960,
        "bd321b096832a3f270bd3b2a88823957920f1a5c5ada71114a26ea729d0cbe91",
    ),
    "tokenizer_config.json": (
        290,
        "0b2fe0037599ee335f0b972fa682bf0ece74e4ccfec755cb7daa3405d3d3e874",
    ),
}


@dataclass(frozen=True)
class RepoSnapshot:
    repo: str
    revision: str
    files: tuple[str, ...]


REPOS = (
    RepoSnapshot(MODEL_ID, REVISION, (MODEL_FILE,)),
    RepoSnapshot(PROCESSOR_ID, PROCESSOR_REVISION, tuple(name for name in MANIFEST if name != MODEL_FILE)),
)
EXPECTED_SIZE = sum(size for size, _ in MANIFEST.values())
EXPECTED_SIZES = {
    repo.repo: sum(MANIFEST[name][0] for name in repo.files)
    for repo in REPOS
}


@dataclass(frozen=True)
class ModelPaths:
    """The two verified HF snapshots needed by Bark and its processor."""

    model_dir: Path
    processor_dir: Path


class ModelVerificationError(ValueError):
    """A pinned snapshot is present but does not match the manifest."""


def manifest_for(repo: str) -> dict[str, tuple[int, str]]:
    spec = next((item for item in REPOS if item.repo == repo), None)
    if spec is None:
        raise ValueError(f"no pinned voice manifest for {repo}")
    return {name: MANIFEST[name] for name in spec.files}


def verify_snapshot(
    repo: str,
    directory: Path,
    manifest: Mapping[str, tuple[int, str]] | None = None,
) -> Path:
    """Check every expected file's exact size and SHA-256, then return the snapshot."""
    expected = dict(manifest if manifest is not None else manifest_for(repo))
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError("voice snapshot is missing")
    for name, (size, digest) in expected.items():
        if Path(name).name != name or name in {".", ".."}:
            raise ModelVerificationError("invalid voice manifest path")
        path = directory / name
        try:
            stat = path.stat()
        except OSError as exc:
            raise ModelVerificationError(f"voice snapshot is missing {name}") from exc
        if not path.is_file() or stat.st_size != size:
            raise ModelVerificationError(f"voice snapshot size mismatch for {name}")
        try:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise ModelVerificationError(f"cannot read voice snapshot file {name}") from exc
        if actual != digest:
            raise ModelVerificationError(f"voice snapshot hash mismatch for {name}")
    return directory


class ModelStore:
    """Read and verify the two pinned snapshots in the standard HF cache."""

    def __init__(self) -> None:
        self._verified: dict[Path, tuple[tuple[int, int, int], ...]] = {}

    def cache_sizes(self) -> dict[str, int]:
        """Current blob bytes for the pinned repositories, including partial blobs."""
        return {repo.repo: hf_cache.size(repo.repo) for repo in REPOS}

    def verified_paths(self) -> ModelPaths:
        snapshots: dict[str, Path] = {}
        for repo in REPOS:
            directory = hf_cache.find(repo.repo, repo.revision, list(repo.files))
            if directory is None:
                raise FileNotFoundError("voice model is not fully cached")
            signatures = []
            for name in repo.files:
                path = directory / name
                stat = path.stat()
                signatures.append((stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)))
            marker = tuple(signatures)
            if self._verified.get(directory) != marker:
                verify_snapshot(repo.repo, directory)
                self._verified[directory] = marker
            snapshots[repo.repo] = directory
        return ModelPaths(snapshots[MODEL_ID], snapshots[PROCESSOR_ID])


__all__ = [
    "EXPECTED_SIZE",
    "EXPECTED_SIZES",
    "MANIFEST",
    "MODEL_FILE",
    "MODEL_ID",
    "PROCESSOR_ID",
    "PROCESSOR_REVISION",
    "REPOS",
    "REVISION",
    "ModelPaths",
    "ModelStore",
    "ModelVerificationError",
    "manifest_for",
    "verify_snapshot",
]
