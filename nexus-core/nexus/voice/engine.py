"""Local Photon dictation adapter.

The model is ``moondream/parakeet-redux``: NVIDIA Parakeet TDT 0.6B v3 with a
1.58-bit packed ternary encoder. The reviewed Photon stack (``moondream 2.6.1`` /
``kestrel 0.9.1``) reads that packed checkpoint, so Nexus does not decode a custom
container or build an f32 Transformers model.

Photon/Kestrel starts a telemetry reporter and an HF config probe when an engine
is created, and Kestrel has no public way to turn either off. STT therefore runs
in a dedicated child process (``nexus.voice.worker``) whose environment is
offline, whose telemetry reporter is replaced by a no-op, and whose Python-level
socket calls are guarded (best effort; native libraries are not sandboxed).

``MODEL_ID`` selects the registered Photon family. The verified local
``model_path`` is authoritative for the checkpoint, tokenizer and ternary
manifest; no remote resolution is intended.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from .store import MANIFEST, MODEL_ID, ModelPaths

_REQUIRED_FILES = tuple(MANIFEST)
_PACKAGES = ("moondream", "kestrel", "kestrel_native", "kestrel_kernels", "torch", "numpy", "huggingface_hub")
_PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}
LOAD_TIMEOUT_SECONDS = 180.0
TRANSCRIBE_TIMEOUT_SECONDS = 120.0
_STOP_TIMEOUT_SECONDS = 5.0


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


def _anchor(model_dir: Path) -> tempfile.TemporaryDirectory[str]:
    """A directory of links we own, so Kestrel cannot fall back to a remote repo.

    Kestrel's local checkpoint resolver only checks that the path exists before
    using it. Pointing it at our own directory of symlinks means a cache removal
    or a concurrent delete fails as a missing file instead of turning into an
    implicit ``snapshot_download``. Only four small links are created; the
    weights stay in the HF cache.
    """
    anchor = tempfile.TemporaryDirectory(prefix="nexus-photon-")
    root = Path(anchor.name)
    try:
        for name in _REQUIRED_FILES:
            (root / name).symlink_to(model_dir / name)
    except BaseException:
        anchor.cleanup()
        raise
    return anchor


def _build_runtime(model_dir: Path, device: str) -> tuple[Any, str]:
    """Load the verified local snapshot through Photon; never resolve a repo id."""
    import torch

    resolved = _resolve_device(torch, device)
    anchor = _anchor(model_dir)
    try:
        import moondream as md

        client = md.photon(MODEL_ID, device=resolved, model_path=str(anchor.name))
    except BaseException:
        anchor.cleanup()
        raise
    return _PhotonRuntime(client, resolved, anchor), resolved


class _PhotonRuntime:
    """One loaded Photon client plus the local anchor it reads."""

    def __init__(self, client: Any, device: str, anchor: tempfile.TemporaryDirectory[str]) -> None:
        self._client = client
        self._device = device
        self._anchor = anchor

    def transcribe(self, wav: bytes) -> str:
        from .audio import parse_wav

        parse_wav(wav)  # the manager validates first; direct callers must not skip it
        result = self._client.transcribe(audio=wav, timestamps="none")
        if not isinstance(result, dict):
            raise TypeError("Photon returned an unsupported transcription result")
        text = result.get("text")
        if not isinstance(text, str):
            raise TypeError("Photon returned an invalid transcript")
        return text.strip()

    def close(self) -> None:
        client, self._client = self._client, None
        try:
            if client is not None:
                client.close()
        finally:
            self._anchor.cleanup()
            gc.collect()
            try:
                import torch

                if self._device == "cuda":
                    torch.cuda.empty_cache()
                elif self._device == "mps":
                    torch.mps.empty_cache()
            except Exception:  # noqa: BLE001, S110 - cache release is best effort
                pass


def _worker_command() -> list[str]:
    return [sys.executable, "-m", "nexus.voice.worker"]


def _worker_env(model_dir: Path, device: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in _PROXY_ENV and key not in {"MOONDREAM_API_KEY", "MOONDREAM_API_BASE_URL"}}
    env.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "OMP_WAIT_POLICY": "passive",
        "NEXUS_VOICE_MODEL_DIR": str(model_dir),
        "NEXUS_VOICE_DEVICE": device,
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _worker_error(message: dict[str, Any]) -> BaseException:
    code = str(message.get("error") or "voice_worker_failed")
    text = str(message.get("message") or code)
    if code == "dependency_missing":
        return ImportError(text)
    if code == "model_not_cached":
        return FileNotFoundError(text)
    return RuntimeError(text)


class PhotonWorkerEngine:
    """Serialize one local Photon worker process; the worker owns the model."""

    model_id = MODEL_ID

    def __init__(
        self,
        model_dir: Path | ModelPaths,
        *,
        device: str = "auto",
        worker_command: list[str] | None = None,
        load_timeout_s: float = LOAD_TIMEOUT_SECONDS,
        transcribe_timeout_s: float = TRANSCRIBE_TIMEOUT_SECONDS,
    ) -> None:
        self.model_dir = Path(model_dir.model_dir if isinstance(model_dir, ModelPaths) else model_dir)
        self.requested_device = device
        self.device = device
        self._worker_command = worker_command or _worker_command()
        self._load_timeout_s = load_timeout_s
        self._transcribe_timeout_s = transcribe_timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def available() -> bool:
        """Return whether the Photon runtime is installed."""
        try:
            return all(find_spec(name) is not None for name in _PACKAGES)
        except (ImportError, ValueError):
            return False

    def _validate_model(self) -> None:
        if not self.model_dir.is_dir():
            raise FileNotFoundError("verified Photon model directory is missing")
        missing = [name for name in _REQUIRED_FILES if not (self.model_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f"verified Photon model files are incomplete: {', '.join(missing)}")

    def _env(self) -> dict[str, str]:
        return _worker_env(self.model_dir, self.requested_device)

    async def load(self) -> None:
        if self._closed:
            raise RuntimeError("voice engine is closed")
        if self._process is not None:
            return
        self._validate_model()
        process = await asyncio.create_subprocess_exec(
            *self._worker_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env(),
            limit=1 << 20,
        )
        self._process = process
        try:
            message = await self._read(self._load_timeout_s)
            if "error" in message:
                raise _worker_error(message)
            if message.get("ready") is not True:
                raise RuntimeError("voice worker did not report ready")
            self.device = str(message.get("device") or self.requested_device)
        except BaseException:
            self._invalidate()
            raise

    async def transcribe(self, audio: bytes) -> str:
        if self._closed:
            raise RuntimeError("voice engine is closed")
        if self._process is None:
            raise RuntimeError("voice engine is not ready")
        if not isinstance(audio, bytes):
            raise TypeError("voice audio must be bytes")
        async with self._lock:
            process = self._process
            if process is None or process.returncode is not None or process.stdin is None:
                self._invalidate()
                raise RuntimeError("voice worker is not running")
            try:
                process.stdin.write(json.dumps({"bytes": len(audio)}).encode() + b"\n" + audio)
                await process.stdin.drain()
                message = await self._read(self._transcribe_timeout_s)
                if "error" in message:
                    raise _worker_error(message)
                text = message.get("text")
                if not isinstance(text, str):
                    raise TypeError("Photon returned an invalid transcript")
                return text.strip()
            except BaseException:
                self._invalidate()
                raise

    def _invalidate(self) -> None:
        """Kill the worker so a cancelled or timed-out response cannot be read later."""
        process, self._process = self._process, None
        self._closed = True
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001, S110 - already gone
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            asyncio.get_running_loop().create_task(process.wait())
        except RuntimeError:  # no running loop (defensive; callers are async)
            pass

    async def _read(self, timeout_s: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("voice worker is not running")
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout_s)
        except TimeoutError as exc:
            raise TimeoutError("the voice worker did not respond in time") from exc
        if not line:
            code = process.returncode
            raise RuntimeError(f"voice worker exited ({code})" if code is not None else "voice worker stopped")
        try:
            message = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("voice worker returned invalid data") from exc
        if not isinstance(message, dict):
            raise TypeError("voice worker returned invalid data")
        return message

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop()

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except Exception:  # noqa: BLE001, S110 - already gone
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            except ProcessLookupError:
                pass
        if process.stdout is not None:
            process.stdout.feed_eof()


__all__ = ["PhotonWorkerEngine"]
