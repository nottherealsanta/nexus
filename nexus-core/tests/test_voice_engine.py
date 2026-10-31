"""Focused tests for the Photon adapter and isolated worker, without ML packages or weights."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from nexus.voice.audio import silence
from nexus.voice.engine import (
    PhotonWorkerEngine,
    _anchor,
    _build_runtime,
    _PhotonRuntime,
    _resolve_device,
)
from nexus.voice.store import MANIFEST, MODEL_ID


def model_dir(tmp_path) -> None:
    for name in MANIFEST:
        (tmp_path / name).write_bytes(b"fixture")


def test_runtime_validates_the_wav_before_calling_photon(tmp_path) -> None:
    model_dir(tmp_path)
    anchor = _anchor(tmp_path)

    class Client:
        def __init__(self) -> None:
            self.prompts: list[dict] = []

        def transcribe(self, **prompt):
            self.prompts.append(prompt)
            return {"text": " hello "}

        def close(self) -> None:
            pass

    client = Client()
    runtime = _PhotonRuntime(client, "cpu", anchor)
    wav = silence(0.3)
    assert runtime.transcribe(wav) == "hello"
    assert client.prompts == [{"audio": wav, "timestamps": "none"}]
    with pytest.raises(ValueError):
        runtime.transcribe(b"not a wav")
    runtime.close()
    assert not Path(anchor.name).exists()


def test_runtime_rejects_a_non_text_result(tmp_path) -> None:
    model_dir(tmp_path)

    class Client:
        def transcribe(self, **_prompt):
            return {"segments": []}

        def close(self) -> None:
            pass

    runtime = _PhotonRuntime(Client(), "cpu", _anchor(tmp_path))
    with pytest.raises(TypeError, match="invalid transcript"):
        runtime.transcribe(silence(0.3))
    runtime.close()


@pytest.mark.parametrize("missing", ["moondream", "kestrel", "kestrel_native", "kestrel_kernels", "torch", "numpy", "huggingface_hub"])
def test_available_requires_every_runtime_package(monkeypatch, missing) -> None:
    from nexus.voice import engine

    present = {"moondream", "kestrel", "kestrel_native", "kestrel_kernels", "torch", "numpy", "huggingface_hub"}
    monkeypatch.setattr(engine, "find_spec", lambda name: object() if name in present else None)
    assert PhotonWorkerEngine.available()
    present.discard(missing)
    assert not PhotonWorkerEngine.available()


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


def test_photon_loads_an_owned_local_anchor_without_repo_resolution(monkeypatch, tmp_path) -> None:
    model_dir(tmp_path)
    calls: list[tuple[str, dict]] = []

    class Client:
        def __init__(self) -> None:
            self.prompts: list[dict] = []
            self.closed = False

        def transcribe(self, **prompt):
            self.prompts.append(prompt)
            return {"text": " hello "}

        def close(self) -> None:
            self.closed = True

    client = Client()

    class Backend:
        def is_available(self) -> bool:
            return False

    class Torch:
        cuda = Backend()
        backends = type("Backends", (), {"mps": Backend()})()

    def photon(model, **kwargs):
        calls.append((model, kwargs))
        return client

    monkeypatch.setitem(sys.modules, "torch", Torch)
    monkeypatch.setitem(sys.modules, "moondream", type("Moondream", (), {"photon": staticmethod(photon)})())
    runtime, device = _build_runtime(tmp_path, "auto")
    assert device == "cpu"
    assert calls[0][0] == MODEL_ID
    kwargs = calls[0][1]
    assert kwargs["device"] == "cpu"
    anchor = Path(kwargs["model_path"])
    assert anchor != tmp_path and anchor.is_dir()
    assert (anchor / "model.safetensors").is_symlink()
    wav = silence(0.3)
    assert runtime.transcribe(wav) == "hello"
    assert client.prompts == [{"audio": wav, "timestamps": "none"}]
    runtime.close()
    assert client.closed
    assert not anchor.exists()


_FAKE_WORKER = r"""
import json, sys
inp = sys.stdin.buffer
out = sys.stdout.buffer
out.write(b'{"ready":true,"device":"cpu"}\n'); out.flush()
while True:
    line = inp.readline()
    if not line:
        break
    request = json.loads(line)
    if request.get("close"):
        break
    data = inp.read(request["bytes"])
    if data == b"bad":
        out.write(b'{"error":"transcribe_failed","message":"boom"}\n'); out.flush()
    else:
        out.write(json.dumps({"text": "local transcript"}).encode() + b"\n"); out.flush()
"""

_FAILED_WORKER = r"""
import sys
sys.stdout.buffer.write(b'{"error":"load_failed","message":"boom"}\n')
sys.stdout.buffer.flush()
raise SystemExit(1)
"""

_DELAYED_WORKER = r"""
import json, os, sys, time
inp = sys.stdin.buffer
out = sys.stdout.buffer
out.write(b'{"ready":true,"device":"cpu"}\n'); out.flush()
marker = os.environ.get("FAKE_WORKER_MARKER", "")
while True:
    line = inp.readline()
    if not line:
        break
    request = json.loads(line)
    data = inp.read(request["bytes"])
    if marker and not os.path.exists(marker):
        open(marker, "w").write("started")
        time.sleep(1.0)
        text = "stale"
    else:
        text = "fresh"
    out.write(json.dumps({"text": text}).encode() + b"\n"); out.flush()
"""

_NEVER_WORKER = r"""
import json, sys, time
inp = sys.stdin.buffer
out = sys.stdout.buffer
out.write(b'{"ready":true,"device":"cpu"}\n'); out.flush()
while True:
    line = inp.readline()
    if not line:
        break
    request = json.loads(line)
    inp.read(request["bytes"])
    time.sleep(60)
"""


def fake_worker(source: str) -> list[str]:
    return [sys.executable, "-c", source]


async def wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"{path} was not created")


async def test_worker_protocol_is_offline_and_reports_errors(tmp_path) -> None:
    model_dir(tmp_path)
    engine = PhotonWorkerEngine(tmp_path, worker_command=fake_worker(_FAKE_WORKER))
    env = engine._env()
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert env["OMP_WAIT_POLICY"] == "passive"
    assert env["NEXUS_VOICE_MODEL_DIR"] == str(tmp_path)
    assert "MOONDREAM_API_KEY" not in env
    assert "HTTPS_PROXY" not in env and "https_proxy" not in env
    try:
        await engine.load()
        assert engine.device == "cpu"
        assert await engine.transcribe(b"RIFF-data") == "local transcript"
        with pytest.raises(RuntimeError, match="boom"):
            await engine.transcribe(b"bad")
    finally:
        await engine.close()


async def test_worker_reports_startup_failure(tmp_path) -> None:
    model_dir(tmp_path)
    engine = PhotonWorkerEngine(tmp_path, worker_command=fake_worker(_FAILED_WORKER))
    with pytest.raises(RuntimeError, match="boom"):
        await engine.load()
    await engine.close()


async def test_cancelled_transcription_kills_the_worker_and_the_next_load_is_fresh(tmp_path, monkeypatch) -> None:
    model_dir(tmp_path)
    marker = tmp_path / "started"
    monkeypatch.setenv("FAKE_WORKER_MARKER", str(marker))
    engine = PhotonWorkerEngine(tmp_path, worker_command=fake_worker(_DELAYED_WORKER))
    await engine.load()
    process = engine._process
    task = asyncio.create_task(engine.transcribe(silence(0.5)))
    await wait_for_file(marker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine._closed and engine._process is None
    async with asyncio.timeout(2):
        while process is not None and process.returncode is None:
            await asyncio.sleep(0.01)
    assert process is not None and process.returncode is not None
    with pytest.raises(RuntimeError, match="closed"):
        await engine.transcribe(silence(0.5))
    await engine.close()

    fresh = PhotonWorkerEngine(tmp_path, worker_command=fake_worker(_DELAYED_WORKER))
    try:
        await fresh.load()
        assert await fresh.transcribe(silence(0.5)) == "fresh"
    finally:
        await fresh.close()


async def test_timed_out_transcription_reaps_the_worker(tmp_path) -> None:
    model_dir(tmp_path)
    engine = PhotonWorkerEngine(tmp_path, worker_command=fake_worker(_NEVER_WORKER), transcribe_timeout_s=0.05)
    await engine.load()
    process = engine._process
    with pytest.raises(TimeoutError):
        await engine.transcribe(silence(0.5))
    assert engine._closed and engine._process is None
    async with asyncio.timeout(2):
        while process is not None and process.returncode is None:
            await asyncio.sleep(0.01)
    assert process is not None and process.returncode is not None
    await engine.close()


def test_worker_version_check_fails_closed_on_an_unreviewed_runtime(monkeypatch) -> None:
    from nexus.voice import worker

    monkeypatch.setattr(worker.metadata, "version", lambda name: "2.6.1" if name == "moondream" else "0.9.1")
    worker._check_runtime_versions()
    monkeypatch.setattr(worker.metadata, "version", lambda name: "2.7.0" if name == "moondream" else "0.9.1")
    with pytest.raises(RuntimeError, match="2.7.0 is not supported"):
        worker._check_runtime_versions()

    def missing(name):
        raise worker.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(worker.metadata, "version", missing)
    with pytest.raises(ImportError, match="is not installed"):
        worker._check_runtime_versions()


async def test_real_worker_loads_fake_photon_offline_and_from_the_local_anchor(tmp_path, monkeypatch) -> None:
    model = tmp_path / "model"
    model.mkdir()
    model_dir(model)
    fake = tmp_path / "fake"
    (fake / "torch").mkdir(parents=True)
    (fake / "moondream").mkdir(parents=True)
    for name, version in (("moondream", "2.6.1"), ("kestrel", "0.9.1")):
        info = fake / f"{name}-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (fake / "torch" / "__init__.py").write_text(
        "class _Backend:\n"
        "    @staticmethod\n"
        "    def is_available():\n"
        "        return False\n"
        "    @staticmethod\n"
        "    def empty_cache():\n"
        "        pass\n"
        "class cuda(_Backend):\n"
        "    pass\n"
        "class backends:\n"
        "    mps = _Backend()\n"
    )
    (fake / "moondream" / "__init__.py").write_text(
        "import os, socket\n"
        "class Client:\n"
        "    def transcribe(self, **prompt):\n"
        "        os.write(1, b'native transcribe log\\n')\n"
        "        assert prompt['audio'].startswith(b'RIFF')\n"
        "        assert prompt['timestamps'] == 'none'\n"
        "        return {'text': ' worker transcript '}\n"
        "    def close(self):\n"
        "        pass\n"
        "def photon(model, **kwargs):\n"
        "    os.write(1, b'native load log\\n')\n"
        "    assert model == 'moondream/parakeet-redux'\n"
        "    assert kwargs['device'] == 'cpu'\n"
        "    path = kwargs['model_path']\n"
        "    assert os.path.isdir(path)\n"
        "    assert os.path.isfile(os.path.join(path, 'ternary.json'))\n"
        "    assert os.environ.get('HF_HUB_OFFLINE') == '1'\n"
        "    try:\n"
        "        socket.getaddrinfo('example.com', 80)\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('network guard missing')\n"
        "    return Client()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(fake))
    engine = PhotonWorkerEngine(model)
    try:
        await engine.load()
        assert engine.device == "cpu"
        assert await engine.transcribe(silence(0.3)) == "worker transcript"
    finally:
        await engine.close()


async def test_worker_engine_rejects_missing_files_before_spawning(tmp_path) -> None:
    model_dir(tmp_path)
    (tmp_path / "ternary.json").unlink()
    engine = PhotonWorkerEngine(tmp_path, worker_command=fake_worker(_FAKE_WORKER))
    with pytest.raises(FileNotFoundError, match="incomplete"):
        await engine.load()
    assert engine._process is None
    await engine.close()
