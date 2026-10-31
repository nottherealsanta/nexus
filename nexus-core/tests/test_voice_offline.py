"""The Photon worker's process-local offline guard and telemetry shim."""

from __future__ import annotations

import socket
import sys
import types

import pytest

from nexus.voice import offline


def test_guard_blocks_non_loopback_dns_and_connections() -> None:
    with offline.guard():
        with pytest.raises(OSError):
            socket.getaddrinfo("example.com", 80)
        with pytest.raises(OSError):
            socket.create_connection(("example.com", 80), timeout=0.1)
        assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo is offline._ORIGINAL_GETADDRINFO
    assert socket.create_connection is offline._ORIGINAL_CREATE_CONNECTION


async def test_no_telemetry_shim_is_inert() -> None:
    reporter = offline._NoTelemetry(object(), object(), api_key=None, api_base_url="https://example.invalid")
    assert await reporter.validate_api_key() is False
    reporter.start()
    reporter.record_success(finetune=None, input_tokens=1, output_tokens=2)
    reporter.record_error(finetune=None)
    await reporter.shutdown()
    assert reporter._normalize_api_key(" key ") == "key"
    assert reporter._normalize_api_key("  ") is None


def test_disable_photon_network_patches_the_runtime_entry_points(monkeypatch) -> None:
    photon = types.ModuleType("kestrel.photon")
    photon.PhotonReporter = object
    core = types.ModuleType("kestrel.engine.core")
    core.PhotonReporter = object
    downloads = types.ModuleType("kestrel.model_download")
    downloads.probe_supported_model_configs = lambda *_args, **_kwargs: "probe"
    package = types.ModuleType("kestrel")
    package.__path__ = []
    for name, module in {
        "kestrel": package,
        "kestrel.photon": photon,
        "kestrel.engine": types.ModuleType("kestrel.engine"),
        "kestrel.engine.core": core,
        "kestrel.model_download": downloads,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    offline.disable_photon_network()

    assert photon.PhotonReporter is offline._NoTelemetry
    assert core.PhotonReporter is offline._NoTelemetry
    assert downloads.probe_supported_model_configs("x") is None
