"""Focused tests for the local Bark adapter without ML packages or weights."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nexus.voice.engine import BarkEngine, _load_tensors, _resolve_device
from nexus.voice.store import MANIFEST


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int]] = []
        self.active = 0
        self.max_active = 0
        self.close_thread: int | None = None

    def transcribe(self, wav: bytes) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append((wav, threading.get_ident()))
        time.sleep(0.01)
        self.active -= 1
        return "local transcript"

    def close(self) -> None:
        self.close_thread = threading.get_ident()


def model_dir(tmp_path) -> None:
    for name in MANIFEST:
        (tmp_path / name).write_bytes(b"fixture")


async def test_transcribe_passes_wav_bytes_to_single_local_worker(tmp_path) -> None:
    model_dir(tmp_path)
    runtime = FakeRuntime()
    engine = BarkEngine(
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
        assert [wav for wav, _ in runtime.calls] == [b"wav-one", b"wav-two"]
        assert len({thread_id for _, thread_id in runtime.calls}) == 1
    finally:
        await engine.close()
    assert runtime.close_thread == runtime.calls[0][1]


@pytest.mark.parametrize("missing", ["model.bark", "tokenizer.json", "generation_config.json"])
async def test_load_fails_closed_when_verified_snapshot_is_incomplete(tmp_path, missing) -> None:
    model_dir(tmp_path)
    (tmp_path / missing).unlink()
    runtime_built = False

    def build(_path, device):
        nonlocal runtime_built
        runtime_built = True
        return FakeRuntime(), device

    engine = BarkEngine(tmp_path, runtime_builder=build)
    with pytest.raises(FileNotFoundError, match="files are incomplete"):
        await engine.load()
    assert not runtime_built
    await engine.close()


async def test_runtime_failure_is_propagated_and_engine_closes(tmp_path) -> None:
    model_dir(tmp_path)

    class RejectingRuntime(FakeRuntime):
        def transcribe(self, _wav):
            raise ValueError("bad encoded audio")

    runtime = RejectingRuntime()
    engine = BarkEngine(
        tmp_path,
        runtime_builder=lambda path, device: (runtime, device),
    )
    await engine.load()
    with pytest.raises(ValueError, match="bad encoded audio"):
        await engine.transcribe(b"bad")
    await engine.close()
    assert runtime.close_thread is not None
    with pytest.raises(RuntimeError, match="closed"):
        await engine.transcribe(b"after close")


async def test_non_text_result_is_rejected(tmp_path) -> None:
    model_dir(tmp_path)

    class BadRuntime(FakeRuntime):
        def transcribe(self, _wav):
            return None

    engine = BarkEngine(tmp_path, runtime_builder=lambda path, device: (BadRuntime(), device))
    await engine.load()
    with pytest.raises(RuntimeError, match="invalid transcript"):
        await engine.transcribe(b"wav")
    await engine.close()


def test_available_requires_every_runtime_package(monkeypatch) -> None:
    import nexus.voice.engine as engine

    present = {"numpy", "torch", "transformers", "constriction"}
    monkeypatch.setattr(engine, "find_spec", lambda name: object() if name in present else None)
    assert BarkEngine.available()
    present.discard("constriction")
    assert not BarkEngine.available()


class _Tensor:
    """Enough of a torch tensor for the loader: a shape and a recorded copy."""

    def __init__(self, shape) -> None:
        self.shape = shape
        self.copied = None

    def copy_(self, value) -> None:
        self.copied = value


class _Torch:
    @staticmethod
    def no_grad():
        import contextlib

        return contextlib.nullcontext()

    @staticmethod
    def from_numpy(value):
        return value


class _Array:
    def __init__(self, shape) -> None:
        self.shape = shape


class _Model:
    def __init__(self, state) -> None:
        self._state = state

    def state_dict(self):
        return self._state


def _state():
    return {"w": _Tensor((2, 3)), "bn.num_batches_tracked": _Tensor(())}


def test_load_tensors_copies_matching_tensors_and_tolerates_batch_counters() -> None:
    state = _state()
    _load_tensors(_Model(state), iter([("w", _Array((2, 3)))]), _Torch)
    assert state["w"].copied is not None


@pytest.mark.parametrize("tensors", [
    [("w", _Array((3, 2)))],                       # wrong shape
    [("extra", _Array((1,)))],                     # unknown name
    [("w", _Array((2, 3))), ("w", _Array((2, 3)))],  # duplicate
    [],                                            # missing weights
])
def test_load_tensors_rejects_mismatched_files(tensors) -> None:
    with pytest.raises(ValueError):
        _load_tensors(_Model(_state()), iter(tensors), _Torch)


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
