"""Pinned, bounded local voice model storage (VOICE_PLAN sections 4 and 9).

Only verified files are activated. Downloads and cache removal share an
interprocess lock; injected fetchers keep unit tests completely offline.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import shutil
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

MODEL_ID = "ConwayResearch/Underdog-Bark-3.0-Clean"
REVISION = "6d34ed8639b2b9a33e764acf06748519bbb950cf"
# The Bark weights are a custom container (nexus/voice/bark.py). The tokenizer and
# feature-extractor files come from NVIDIA's processor repo at a pinned commit; no
# NVIDIA weights are fetched.
PROCESSOR_ID = "nvidia/parakeet-tdt-0.6b-v3"
PROCESSOR_REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
MODEL_FILE = "model.bark"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MANIFEST = {
    MODEL_FILE: (
        179_458_613,
        "b67ca90d94b79a36af4616f1cb3f241884925de7aa9f3e673a8d5c6c4cfd3436",
    ),
    "config.json": (
        1_153, "e747b85e1bdfd300c8b8ac63bac8dd5221f8fe9bc275b48d06c735fcd6971b6e",
    ),
    "generation_config.json": (
        289, "b141de6ec6d7f982ece13f98f604e3fe1807ea9c0e839185d0ab7064604209d0",
    ),
    "processor_config.json": (
        392, "8346a93a3b987fa1dec57a78f045cd0817d21786589a5a096b41a57a446fd1d7",
    ),
    "tokenizer.json": (
        1_159_960, "bd321b096832a3f270bd3b2a88823957920f1a5c5ada71114a26ea729d0cbe91",
    ),
    "tokenizer_config.json": (
        290, "0b2fe0037599ee335f0b972fa682bf0ece74e4ccfec755cb7daa3405d3d3e874",
    ),
}
_DIRECTORY = "bark-3.0-clean"
_LEGACY_DIRECTORIES = ("parakeet-redux",)  # the Kestrel-era cache; unused and removed


def models_root(home: Path) -> Path:
    return Path(home) / "models" / "voice"


class ModelStore:
    """Download a fixed manifest under a caller-selected machine-state home."""

    def __init__(
        self, home: Path, *, revision: str = REVISION,
        manifest: Mapping[str, tuple[int, str]] | None = None,
        fetch: Callable | None = None,
    ) -> None:
        if revision != REVISION and manifest is None:
            raise ValueError("voice revision has no trusted manifest")
        self.revision = revision
        self.manifest = dict(MANIFEST if manifest is None else manifest)
        if not self.manifest or sum(size for size, _ in self.manifest.values()) > MAX_DOWNLOAD_BYTES:
            raise ValueError("invalid voice manifest size")
        for name, (size, digest) in self.manifest.items():
            if Path(name).name != name or name in {".", ".."} or size < 0 or len(digest) != 64:
                raise ValueError("invalid voice manifest entry")
        self.root = models_root(home)
        self.path = self.root / _DIRECTORY / revision
        self._fetch = fetch or urllib.request.urlopen

    def _url(self, name: str) -> str:
        """Every file is fetched by immutable commit: the weights from Bark, the rest from NVIDIA."""
        if name == MODEL_FILE:
            repo, revision = MODEL_ID, self.revision
        else:
            repo, revision = PROCESSOR_ID, PROCESSOR_REVISION
        return f"https://huggingface.co/{repo}/resolve/{revision}/{name}"

    async def ensure(self, progress_cb: Callable[[int, int], None], *, allow_download: bool = True) -> Path:
        loop = asyncio.get_running_loop()
        def report(done: int, total: int) -> None:
            loop.call_soon_threadsafe(progress_cb, done, total)
        worker = asyncio.create_task(asyncio.to_thread(self._ensure, report, allow_download))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A thread cannot be canceled; keep its owner alive until it releases
            # the filesystem lock, so Remove cannot race an orphaned download.
            await asyncio.shield(worker)
            raise

    def cached(self) -> bool:
        """Cheap construction-time check; ensure rechecks hashes before loading."""
        return all(
            (self.path / name).is_file()
            and not (self.path / name).is_symlink()
            and (self.path / name).stat().st_size == size
            for name, (size, _) in self.manifest.items()
        )

    def _valid(self, directory: Path) -> bool:
        for name, (size, digest) in self.manifest.items():
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                return False
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    return False
        return True

    def _ensure(self, progress: Callable[[int, int], None], allow_download: bool = True) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._valid(self.path):
                return self.path
            if not allow_download:
                raise FileNotFoundError("Voice model needs download confirmation")
            partial = self.path.with_name(self.revision + ".partial")
            total = sum(size for size, _ in self.manifest.values())
            for attempt in range(3):
                shutil.rmtree(partial, ignore_errors=True)
                partial.mkdir(parents=True, mode=0o700)
                done = 0
                progress(0, total)
                try:
                    for name, (size, digest) in self.manifest.items():
                        request = urllib.request.Request(
                            self._url(name), headers={"User-Agent": "Nexus-voice"},
                        )
                        with self._fetch(request, timeout=60) as response:
                            if hasattr(response, "geturl") and not response.geturl().startswith("https://"):
                                raise ValueError("insecure model redirect")
                            target = partial / name
                            written = 0
                            checksum = hashlib.sha256()
                            with target.open("xb") as stream:
                                os.chmod(target, 0o600)
                                while chunk := response.read(min(64 * 1024, size - written + 1)):
                                    written += len(chunk)
                                    if written > size:
                                        raise ValueError("voice download exceeds manifest size")
                                    stream.write(chunk)
                                    checksum.update(chunk)
                                    done += len(chunk)
                                    progress(done, total)
                            if written != size or checksum.hexdigest() != digest:
                                raise ValueError("voice model verification failed")
                    if self.path.exists():
                        shutil.rmtree(self.path)
                    os.replace(partial, self.path)
                    for legacy in _LEGACY_DIRECTORIES:
                        shutil.rmtree(self.root / legacy, ignore_errors=True)
                    return self.path
                except Exception:
                    shutil.rmtree(partial, ignore_errors=True)
                    if attempt == 2:
                        raise
                    time.sleep(0.25 * 2 ** attempt)
        raise RuntimeError("voice model download failed")

    async def remove(self) -> None:
        await asyncio.to_thread(self._remove)

    def _remove(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            shutil.rmtree(self.path, ignore_errors=True)
            for legacy in _LEGACY_DIRECTORIES:
                shutil.rmtree(self.root / legacy, ignore_errors=True)
            shutil.rmtree(self.path.with_name(self.revision + ".partial"), ignore_errors=True)
