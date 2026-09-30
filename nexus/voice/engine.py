"""Local Parakeet Redux adapter (VOICE_PLAN.md §7).

Kestrel 0.8.0's high-level Photon ``InferenceEngine`` emits startup and
periodic telemetry without an opt-out. This adapter uses Kestrel's registered
Parakeet single-pass runtime with a verified local checkpoint directory, which
keeps audio and runtime setup local while retaining Kestrel's decoder.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable

MODEL_ID = "moondream/parakeet-redux"
_REQUIRED_FILES = (
    "config.json",
    "model.safetensors",
    "ternary.json",
    "tokenizer.json",
)


def _build_runtime(model_dir: Path, device: str) -> tuple[Any, str]:
    """Build the local runtime without importing Photon or creating a reporter."""
    import torch
    from kestrel.config import RuntimeConfig
    from kestrel.models.parakeet_tdt import ParakeetTdtRuntime

    resolved_device = _resolve_device(torch, device)
    config = RuntimeConfig(
        model=MODEL_ID,
        model_path=str(model_dir),
        device=resolved_device,
    )
    runtime = ParakeetTdtRuntime(config)
    return runtime, str(config.resolved_device())


def _resolve_device(torch: Any, device: str) -> str:
    if device != "auto":
        if device not in {"cpu", "mps", "cuda"}:
            raise ValueError("unsupported voice inference device")
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available")
        if device == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("Apple Metal is not available")
        return device
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


class KestrelEngine:
    """Serialize local WAV transcription through one dedicated worker thread."""

    def __init__(
        self,
        model_dir: Path,
        *,
        device: str = "auto",
        runtime_builder: Callable[[Path, str], tuple[Any, str]] | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.requested_device = device
        self.device = device
        self._runtime_builder = runtime_builder or _build_runtime
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nexus-voice",
        )
        self._runtime: Any | None = None
        self._loaded = False
        self._closed = False

    @staticmethod
    def available() -> bool:
        """Return whether the optional local inference runtime is installed."""
        try:
            return find_spec("kestrel") is not None
        except (ImportError, ValueError):
            return False

    async def load(self) -> None:
        if self._closed:
            raise RuntimeError("voice engine is closed")
        if self._loaded:
            return
        loop = asyncio.get_running_loop()

        def create() -> tuple[Any, str]:
            if self.model_dir.is_symlink() or not self.model_dir.is_dir():
                raise FileNotFoundError("verified voice model directory is missing")
            for name in _REQUIRED_FILES:
                path = self.model_dir / name
                if path.is_symlink() or not path.is_file():
                    raise FileNotFoundError("verified voice model files are incomplete")
            return self._runtime_builder(self.model_dir, self.requested_device)

        self._runtime, self.device = await loop.run_in_executor(self._executor, create)
        self._loaded = True

    async def transcribe(self, audio: bytes) -> str:
        if self._closed:
            raise RuntimeError("voice engine is closed")
        if not self._loaded or self._runtime is None or self._closed:
            raise RuntimeError("voice engine is not ready")
        if not isinstance(audio, bytes):
            raise TypeError("voice audio must be bytes")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._executor,
            self._runtime.forward,
            "transcribe",
            [{"audio": audio, "timestamps": "none"}],
        )
        if not isinstance(result, tuple) or len(result) != 1:
            raise RuntimeError("Parakeet returned an invalid result")
        item = result[0]
        if isinstance(item, BaseException):
            raise item
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise RuntimeError("Parakeet returned an invalid transcript")
        return item["text"]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        runtime, self._runtime = self._runtime, None
        try:
            if runtime is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, runtime.shutdown)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._loaded = False


__all__ = ["KestrelEngine"]
