"""Process-local, best-effort offline protection for the isolated Photon worker.

Kestrel (the engine behind ``moondream.photon``) starts a telemetry reporter and
an HF config probe as soon as an engine is created, and has no public opt-out.
Nexus runs STT in a dedicated worker process so it can pin these settings for
that process only: Hugging Face is offline, the telemetry reporter is replaced by
a no-op, and Python-level socket calls are guarded.

This is not an OS sandbox. Native libraries loaded by Torch, Kestrel or its
kernels can open sockets without going through Python's socket module. The
primary guarantees are the exact reviewed runtime versions, the pinned local
checkpoint path and Hugging Face's offline mode; the socket guard is defence in
depth for Python-level calls.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from collections.abc import Iterator, Sequence
from typing import Any


def _loopback(host: object) -> bool:
    if host is None or host == "":
        return True
    text = str(host).split("%", 1)[0]
    if text == "localhost" or text.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _guarded_getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
    if not _loopback(host):
        raise socket.gaierror(socket.EAI_NONAME, "voice worker network access is disabled")
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def _guarded_create_connection(address: Sequence[Any], *args: Any, **kwargs: Any) -> Any:
    if not _loopback(address[0] if address else None):
        raise OSError("voice worker network access is disabled")
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if self.family == socket.AF_UNIX or _loopback(address[0] if isinstance(address, tuple) else address):
        return _ORIGINAL_CONNECT(self, address)
    raise OSError("voice worker network access is disabled")


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if self.family == socket.AF_UNIX or _loopback(address[0] if isinstance(address, tuple) else address):
        return _ORIGINAL_CONNECT_EX(self, address)
    return 1


_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


@contextlib.contextmanager
def guard() -> Iterator[None]:
    """Block non-loopback DNS and connections in this process, then restore."""
    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
    socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.getaddrinfo = _ORIGINAL_GETADDRINFO
        socket.create_connection = _ORIGINAL_CREATE_CONNECTION
        socket.socket.connect = _ORIGINAL_CONNECT  # type: ignore[method-assign]
        socket.socket.connect_ex = _ORIGINAL_CONNECT_EX  # type: ignore[method-assign]


class _NoTelemetry:
    """A drop-in for Kestrel's ``PhotonReporter`` that sends and records nothing."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    @staticmethod
    def _normalize_api_key(api_key: str | None) -> str | None:
        normalized = api_key.strip() if api_key else ""
        return normalized or None

    @staticmethod
    def _is_api_key_header_safe(api_key: str) -> bool:
        return api_key.isascii() and all(not c.isspace() and c.isprintable() for c in api_key)

    async def validate_api_key(self) -> bool:
        return False

    def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def record_success(self, **_kwargs: Any) -> None:
        pass

    def record_error(self, **_kwargs: Any) -> None:
        pass


def disable_photon_network() -> None:
    """Best-effort replacement of Kestrel's telemetry and HF probe in this process."""
    try:
        from kestrel import photon

        photon.PhotonReporter = _NoTelemetry  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001, S110 - reviewed versions and the local path are the primary guarantees
        pass
    try:
        import kestrel.model_download as downloads
        from kestrel.engine import core

        core.PhotonReporter = _NoTelemetry  # type: ignore[attr-defined]
        downloads.probe_supported_model_configs = lambda *_args, **_kwargs: None
    except Exception:  # noqa: BLE001, S110 - optional hardening across Kestrel revisions
        pass


__all__ = ["disable_photon_network", "guard"]
