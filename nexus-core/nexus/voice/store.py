"""Pinned STT snapshot manifest and integrity checks.

Model bytes live only in the standard Hugging Face hub cache. This module has
no downloader and never consults Nexus state directories.

The STT model is Moondream's packed ternary Parakeet Redux checkpoint. Photon
reads the base-3 packed weights directly; Nexus never expands them into an f32
Transformers model.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import hf_cache

MODEL_ID = "moondream/parakeet-redux"
REVISION = "2bf128600aac4b16946f7ed8372e56117fe5e23b"
BARK_MODEL_ID = "ConwayResearch/Underdog-Bark-3.0-Clean"
PHOTON_VERSION = "2.6.1"
KESTREL_VERSION = "0.9.1"
REVIEWED_RUNTIME = {"moondream": PHOTON_VERSION, "kestrel": KESTREL_VERSION}

# One HF snapshot is the whole runtime input. Photon loads the packed ternary
# encoder from model.safetensors and reads ternary.json for the packing layout.
MANIFEST: dict[str, tuple[int, str]] = {
    "model.safetensors": (
        177_774_490,
        "78ec25733ee0d0c1586d1346fc86db9d0c2e436e3a8ab1d32a82d1bb8f848d21",
    ),
    "config.json": (
        12_988,
        "503c653b2e3bb788adbcb04f5abdee532d958686564081baeed133ff10143f6e",
    ),
    "ternary.json": (
        57_970,
        "1221c6d3ce901ffe09c089da758a8db8b76189f80cff41c5afc244fc61e2051d",
    ),
    "tokenizer.json": (
        1_159_960,
        "bd321b096832a3f270bd3b2a88823957920f1a5c5ada71114a26ea729d0cbe91",
    ),
}


@dataclass(frozen=True)
class RepoSnapshot:
    repo: str
    revision: str
    files: tuple[str, ...]


REPOS = (RepoSnapshot(MODEL_ID, REVISION, tuple(MANIFEST)),)
EXPECTED_SIZE = sum(size for size, _ in MANIFEST.values())
EXPECTED_SIZES = {repo.repo: EXPECTED_SIZE for repo in REPOS}


@dataclass(frozen=True)
class ModelPaths:
    """The verified HF snapshot Photon loads."""

    model_dir: Path


class ModelVerificationError(ValueError):
    """A pinned snapshot is present but does not match the manifest."""


def manifest_for(repo: str) -> dict[str, tuple[int, str]]:
    spec = next((item for item in REPOS if item.repo == repo), None)
    if spec is None:
        raise ValueError(f"no pinned voice manifest for {repo}")
    return {name: MANIFEST[name] for name in spec.files}


def unsupported_reason(repo: str) -> str | None:
    """Why an STT repo cannot run, with an explicit migration path for Bark users."""
    if repo == MODEL_ID:
        return None
    if repo == BARK_MODEL_ID:
        return (
            "Underdog Bark is no longer supported. Set [voice] stt_model = "
            f'"{MODEL_ID}" in config.toml and download the new model. '
            "Cached Bark files are left in place."
        )
    return f"Only the pinned {MODEL_ID} STT model is supported"


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
    """Read and verify the pinned snapshot in the standard HF cache."""

    def __init__(self) -> None:
        self._verified: dict[Path, tuple[tuple[int, int, int, int], ...]] = {}

    def cache_sizes(self) -> dict[str, int]:
        """Current blob bytes for the pinned repository, including partial blobs."""
        return {repo.repo: hf_cache.size(repo.repo) for repo in REPOS}

    def verified_paths(self) -> ModelPaths:
        repo = REPOS[0]
        directory = hf_cache.find(repo.repo, repo.revision, list(repo.files))
        if directory is None:
            raise FileNotFoundError("voice model is not fully cached")
        signatures = []
        for name in repo.files:
            path = directory / name
            stat = path.stat()
            signatures.append((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, getattr(stat, "st_ino", 0)))
        marker = tuple(signatures)
        if self._verified.get(directory) != marker:
            verify_snapshot(repo.repo, directory)
            self._verified[directory] = marker
        return ModelPaths(directory)


__all__ = [
    "BARK_MODEL_ID",
    "EXPECTED_SIZE",
    "EXPECTED_SIZES",
    "KESTREL_VERSION",
    "MANIFEST",
    "MODEL_ID",
    "PHOTON_VERSION",
    "REPOS",
    "REVIEWED_RUNTIME",
    "REVISION",
    "ModelPaths",
    "ModelStore",
    "ModelVerificationError",
    "manifest_for",
    "unsupported_reason",
    "verify_snapshot",
]
