"""Isolated Photon worker for speech to text.

The parent (``PhotonWorkerEngine``) starts this module once, waits for a
``{"ready": true, "device": ...}`` line, then writes one
``{"bytes": N}\\n`` header followed by N bytes of a validated WAV and reads a
``{"text": ...}`` or ``{"error": code, "message": ...}`` line.

The worker is configured offline: Hugging Face is offline, Kestrel's telemetry
reporter is replaced by a no-op, proxy variables are absent, and a best-effort
Python socket guard rejects non-loopback DNS lookups and connections. Native
runtime libraries are not sandboxed; the exact reviewed runtime versions and the
pinned local checkpoint path are the primary guarantees.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from . import offline
from .engine import _build_runtime
from .store import MODEL_ID, REVIEWED_RUNTIME

_MAX_AUDIO_BYTES = 4 * 1024 * 1024


def _emit(out: Any, message: dict[str, Any]) -> None:
    out.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    out.flush()


def _failure(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ImportError):
        code = "dependency_missing"
    elif isinstance(exc, FileNotFoundError):
        code = "model_not_cached"
    else:
        code = "load_failed"
    text = " ".join(str(exc).split())
    return {"error": code, "message": text[:300] or type(exc).__name__}


def _check_runtime_versions() -> None:
    """Fail closed rather than applying reviewed shims to an unreviewed runtime."""
    for package, expected in REVIEWED_RUNTIME.items():
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError as exc:
            raise ImportError(f"{package} is not installed") from exc
        if actual != expected:
            raise RuntimeError(
                f"{package} {actual} is not supported; Nexus reviews {expected} for Photon STT"
            )


def _serve() -> int:
    # Keep a private descriptor for JSON replies, then point fd 1 at stderr so
    # native Photon/Torch writes cannot corrupt the parent's line protocol.
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), 1)
    out = os.fdopen(protocol_fd, "wb", buffering=0)
    inp = sys.stdin.buffer
    sys.stdout = sys.stderr  # Python writes follow fd 1 to stderr too

    model_dir_value = os.environ.get("NEXUS_VOICE_MODEL_DIR", "")
    if not model_dir_value:
        _emit(out, {"error": "model_not_cached", "message": "no verified STT model directory"})
        return 1
    model_dir = Path(model_dir_value)
    device = os.environ.get("NEXUS_VOICE_DEVICE", "auto")
    try:
        _check_runtime_versions()
    except BaseException as exc:  # noqa: BLE001 - fail closed before importing an unreviewed runtime
        _emit(out, _failure(exc))
        return 1
    with offline.guard():
        offline.disable_photon_network()
        try:
            runtime, resolved = _build_runtime(model_dir, device)
        except BaseException as exc:  # noqa: BLE001 - report every startup failure to the parent
            _emit(out, _failure(exc))
            return 1
        _emit(out, {"ready": True, "device": resolved, "model": MODEL_ID})
        try:
            while True:
                line = inp.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                except (TypeError, ValueError):
                    _emit(out, {"error": "bad_request", "message": "invalid worker request"})
                    continue
                if not isinstance(request, dict):
                    _emit(out, {"error": "bad_request", "message": "invalid worker request"})
                    continue
                if request.get("close"):
                    break
                size = request.get("bytes")
                if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= _MAX_AUDIO_BYTES:
                    _emit(out, {"error": "bad_request", "message": "invalid audio length"})
                    continue
                wav = inp.read(size)
                if len(wav) != size:
                    _emit(out, {"error": "bad_request", "message": "truncated audio request"})
                    break
                try:
                    text = runtime.transcribe(wav)
                except BaseException as exc:  # noqa: BLE001 - one bad WAV must not kill the worker silently
                    _emit(out, {"error": "transcribe_failed", "message": " ".join(str(exc).split())[:300]})
                    continue
                _emit(out, {"text": text})
        finally:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_serve())
