"""Local Bark dictation adapter (VOICE_PLAN.md §7).

The model is Underdog Bark 3.0 Clean: NVIDIA Parakeet TDT 0.6B v3 weights in the
custom ``.bark`` container. ``nexus.voice.bark`` decodes that file as data into a
stock Transformers ``ParakeetForTDT``, and the tokenizer and feature extractor
load from the pinned processor files beside it. Everything is local: the
``from_pretrained`` calls take a verified directory with ``local_files_only``,
no remote code is trusted or executed, and nothing is unpickled.
"""

from __future__ import annotations

import asyncio
import gc
import os
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable

from .store import MANIFEST, MODEL_FILE, MODEL_ID

_REQUIRED_FILES = tuple(MANIFEST)
_PACKAGES = ("numpy", "torch", "transformers", "constriction", "librosa")
MAX_CPU_THREADS = 4  # dictation must not take over every core of the machine


def _build_runtime(model_dir: Path, device: str) -> tuple[Any, str]:
    """Load the verified checkpoint directory into a Transformers Parakeet model."""
    import numpy as np
    import torch
    from transformers import (
        AutoProcessor,
        GenerationConfig,
        ParakeetForTDT,
        ParakeetTDTConfig,
    )

    from .bark import open_bark

    resolved_device = _resolve_device(torch, device)
    if resolved_device == "cpu":
        torch.set_num_threads(max(1, min(MAX_CPU_THREADS, os.cpu_count() or 1)))
    config, tensors = open_bark(model_dir / MODEL_FILE)
    model = ParakeetForTDT(ParakeetTDTConfig.from_dict(config))
    _load_tensors(model, tensors, torch)
    model.generation_config = GenerationConfig.from_pretrained(
        str(model_dir), local_files_only=True,
    )
    processor = AutoProcessor.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False,
    )
    model = model.eval().to(resolved_device)
    return _BarkRuntime(model, processor, torch, np, resolved_device), resolved_device


def _load_tensors(model: Any, tensors: Any, torch: Any) -> None:
    """Copy decoded tensors into ``model`` one at a time; every name and shape must match."""
    state = model.state_dict()
    seen: set[str] = set()
    with torch.no_grad():
        for name, value in tensors:
            target = state.get(name)
            if target is None or tuple(target.shape) != tuple(value.shape) or name in seen:
                raise ValueError("bark tensors do not match the Parakeet model")
            target.copy_(torch.from_numpy(value))
            seen.add(name)
    missing = [n for n in state if n not in seen and "num_batches_tracked" not in n]
    if missing:
        raise ValueError("bark file is missing Parakeet tensors")


class _BarkRuntime:
    """One loaded model plus its processor; callers serialize access."""

    def __init__(self, model: Any, processor: Any, torch: Any, np: Any, device: str) -> None:
        self._model = model
        self._processor = processor
        self._torch = torch
        self._np = np
        self._device = device

    def transcribe(self, wav: bytes) -> str:
        from .audio import SAMPLE_RATE, parse_wav

        np, torch = self._np, self._torch
        pcm = parse_wav(wav).pcm
        wave = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        inputs = self._processor(
            [wave], sampling_rate=SAMPLE_RATE, return_tensors="pt", padding=True,
        )
        dtype = next(self._model.parameters()).dtype
        inputs = {
            key: value.to(self._device, dtype) if value.is_floating_point() else value.to(self._device)
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            output = self._model.generate(**inputs)
        return str(self._processor.batch_decode(output.sequences, skip_special_tokens=True)[0]).strip()

    def close(self) -> None:
        self._model = self._processor = None
        gc.collect()
        torch = self._torch
        try:
            if self._device == "cuda":
                torch.cuda.empty_cache()
            elif self._device == "mps":
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001 - cache release is best effort
            pass


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


class BarkEngine:
    """Serialize local WAV transcription through one dedicated worker thread."""

    model_id = MODEL_ID

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
        """Return whether the local inference runtime is installed."""
        try:
            return all(find_spec(name) is not None for name in _PACKAGES)
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
        text = await loop.run_in_executor(self._executor, self._runtime.transcribe, audio)
        if not isinstance(text, str):
            raise RuntimeError("Bark returned an invalid transcript")
        return text

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        runtime, self._runtime = self._runtime, None
        try:
            if runtime is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._executor, runtime.close)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._loaded = False


__all__ = ["BarkEngine"]
