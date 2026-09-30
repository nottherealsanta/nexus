"""Focused tests for the local Parakeet adapter without ML packages or weights."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nexus.voice.engine import KestrelEngine, _resolve_device


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, object]], int]] = []
        self.active = 0
        self.max_active = 0
        self.shutdown_thread: int | None = None

    def forward(self, task: str, items: list[dict[str, object]]) -> tuple[dict[str, str], ...]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append((task, items, threading.get_ident()))
        time.sleep(0.01)
        self.active -= 1
        return ({"text": "local transcript"},)

    def shutdown(self) -> None:
        self.shutdown_thread = threading.get_ident()


def model_dir(tmp_path) -> None:
    for name in ("config.json", "model.safetensors", "ternary.json", "tokenizer.json"):
        (tmp_path / name).write_bytes(b"fixture")


async def test_transcribe_passes_wav_bytes_to_single_local_worker(tmp_path) -> None:
    model_dir(tmp_path)
    runtime = FakeRuntime()
    engine = KestrelEngine(
        tmp_path,
        device="cpu",
        runtime_builder=lambda path, device: (runtime, device),
    )
    try:
        await engine.load()
        transcripts = await asyncio.gather(
            engine.transcribe(b"wav-one"),
            engine.transcribe(b"wav-two"),
        )
        assert transcripts == ["local transcript", "local transcript"]
        assert runtime.max_active == 1
        assert all(task == "transcribe" for task, _, _ in runtime.calls)
        assert [items[0]["audio"] for _, items, _ in runtime.calls] == [
            b"wav-one",
            b"wav-two",
        ]
        assert all(items[0]["timestamps"] == "none" for _, items, _ in runtime.calls)
        assert len({thread_id for _, _, thread_id in runtime.calls}) == 1
    finally:
        await engine.close()
    assert runtime.shutdown_thread == runtime.calls[0][2]


async def test_load_fails_closed_when_verified_snapshot_is_incomplete(tmp_path) -> None:
    runtime_built = False

    def build(_path, device):
        nonlocal runtime_built
        runtime_built = True
        return FakeRuntime(), device

    engine = KestrelEngine(tmp_path, runtime_builder=build)
    with pytest.raises(FileNotFoundError, match="files are incomplete"):
        await engine.load()
    assert not runtime_built
    await engine.close()


async def test_runtime_rejection_is_propagated_and_engine_closes(tmp_path) -> None:
    model_dir(tmp_path)

    class RejectingRuntime(FakeRuntime):
        def forward(self, _task, _items):
            return (ValueError("bad encoded audio"),)

    runtime = RejectingRuntime()
    engine = KestrelEngine(
        tmp_path,
        runtime_builder=lambda path, device: (runtime, device),
    )
    await engine.load()
    with pytest.raises(ValueError, match="bad encoded audio"):
        await engine.transcribe(b"bad")
    await engine.close()
    assert runtime.shutdown_thread is not None
    with pytest.raises(RuntimeError, match="closed"):
        await engine.transcribe(b"after close")


def test_resolve_device_prefers_cuda_then_mps_then_cpu() -> None:
    class Backend:
        def __init__(self, available: bool) -> None:
            self.available = available

        def is_available(self) -> bool:
            return self.available

    class Torch:
        cuda = Backend(True)
        backends = type("Backends", (), {"mps": Backend(True)})()

    assert _resolve_device(Torch, "auto") == "cuda"
    Torch.cuda.available = False
    assert _resolve_device(Torch, "auto") == "mps"
    Torch.backends.mps.available = False
    assert _resolve_device(Torch, "auto") == "cpu"


def test_resolve_device_rejects_unavailable_explicit_accelerator() -> None:
    class Torch:
        cuda = type("Cuda", (), {"is_available": staticmethod(lambda: False)})()
        backends = type(
            "Backends",
            (),
            {"mps": type("Mps", (), {"is_available": staticmethod(lambda: False)})()},
        )()

    with pytest.raises(RuntimeError, match="CUDA is not available"):
        _resolve_device(Torch, "cuda")
    with pytest.raises(RuntimeError, match="Apple Metal is not available"):
        _resolve_device(Torch, "mps")
    with pytest.raises(ValueError, match="unsupported"):
        _resolve_device(Torch, "tpu")
