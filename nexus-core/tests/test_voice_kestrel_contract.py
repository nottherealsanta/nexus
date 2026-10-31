"""Source-level contracts for the exact reviewed Photon/Kestrel runtime.

These tests use the installed distributions only; they skip on a base install
without the voice extra. They verify the private integration points against the
real pinned source instead of trusting a fake that accepts any keyword.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
from pathlib import Path

import pytest

from nexus.voice import offline
from nexus.voice.store import REVIEWED_RUNTIME


def _package_root(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        pytest.skip(f"{name} is not installed")
    assert importlib.metadata.version(name) == REVIEWED_RUNTIME[name]
    return Path(next(iter(spec.submodule_search_locations)))


def test_moondream_accepts_and_forwards_the_local_runtime_config() -> None:
    root = _package_root("moondream")
    client = root / "photon_client.py"
    source = client.read_text() if client.is_file() else (root / "photon_vl.py").read_text()
    assert "RuntimeConfig(" in source
    assert "**runtime_config" in source
    assert "def photon(" in (root / "__init__.py").read_text()


def test_kestrel_parakeet_uses_the_existing_model_path() -> None:
    root = _package_root("kestrel")
    config = (root / "config.py").read_text()
    assert "model_path: str | Path | None = None" in config
    assert "if self.model_path is None:" in config

    runtime = (root / "models" / "parakeet_tdt" / "runtime.py").read_text()
    assert 'getattr(cfg, "model_path", None) or self._model_name' in runtime

    weights = (root / "models" / "parakeet_tdt" / "weights.py").read_text()
    assert "manifest.exists()" in weights

    checkpoint = (root / "models" / "asr" / "checkpoint.py").read_text()
    assert "if path.exists():" in checkpoint
    assert "snapshot_download(" in checkpoint


def test_reporter_shim_covers_the_pinned_reporter_api() -> None:
    root = _package_root("kestrel")
    source = (root / "photon.py").read_text()
    tree = ast.parse(source)
    reporter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PhotonReporter")
    methods = {
        node.name for node in reporter.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "__init__", "validate_api_key", "start", "shutdown",
        "record_success", "record_error", "_normalize_api_key", "_is_api_key_header_safe",
    }
    assert required <= methods
    for name in required:
        assert callable(getattr(offline._NoTelemetry, name, None)), name


def test_config_probe_is_patched_through_a_late_module_lookup() -> None:
    root = _package_root("kestrel")
    core = (root / "engine" / "core.py").read_text()
    initialize = core.index("async def _initialize")
    probe_import = core.index("from kestrel.model_download import probe_supported_model_configs")
    assert probe_import > initialize
    assert "probe_supported_model_configs" in (root / "model_download.py").read_text()
