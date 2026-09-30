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

REVISION = "2bf128600aac4b16946f7ed8372e56117fe5e23b"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MANIFEST = {
    "config.json": (
        12_988, "503c653b2e3bb788adbcb04f5abdee532d958686564081baeed133ff10143f6e",
    ),
    "tokenizer.json": (
        1_159_960, "bd321b096832a3f270bd3b2a88823957920f1a5c5ada71114a26ea729d0cbe91",
    ),
    "ternary.json": (
        57_970, "1221c6d3ce901ffe09c089da758a8db8b76189f80cff41c5afc244fc61e2051d",
    ),
    "model.safetensors": (
        177_774_490,
        "78ec25733ee0d0c1586d1346fc86db9d0c2e436e3a8ab1d32a82d1bb8f848d21",
    ),
}


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
        self.path = self.root / "parakeet-redux" / revision
        self._fetch = fetch or urllib.request.urlopen

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
                        url = f"https://huggingface.co/moondream/parakeet-redux/resolve/{self.revision}/{name}"
                        request = urllib.request.Request(url, headers={"User-Agent": "Nexus-voice"})
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
            shutil.rmtree(self.path.with_name(self.revision + ".partial"), ignore_errors=True)
