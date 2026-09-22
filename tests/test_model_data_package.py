"""Packaging tests for the vendored model catalogue and its attribution.

Phase 5.5 ships ``nexus/model/data/models.min.json`` as the offline fallback for
:class:`~nexus.model.registry.ModelRegistry`, plus a ``NOTICE`` carrying the
models.dev MIT attribution. Both are *data* files inside the ``nexus.model``
package: if they are not declared under ``[tool.setuptools.package-data]`` a
built wheel silently omits them and the offline fallback (and the attribution)
disappear from the installed distribution.

These tests are offline. The wheel test builds the project with its declared
build backend (``setuptools``) in-process and inspects the archive; it skips
cleanly when the backend is not importable so the offline unit suite never
requires network or a build toolchain.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DATA_DIR = REPO_ROOT / "nexus" / "model" / "data"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_package_data_declares_the_catalogue_and_notice():
    declared = _pyproject()["tool"]["setuptools"]["package-data"]["nexus.model"]
    assert "data/models.min.json" in declared
    assert "data/NOTICE" in declared


def test_source_tree_contains_the_declared_data_files():
    assert (DATA_DIR / "models.min.json").is_file()
    assert (DATA_DIR / "NOTICE").is_file()


def _build_wheel(tmp_path: Path) -> Path:
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip(
            "setuptools is a dev dependency; install '.[dev]' to build the wheel"
        )
    wheel_dir = tmp_path / "dist"
    wheel_dir.mkdir()
    # Build in a subprocess: setuptools/distutils configures the logging module
    # as a side effect of a build, which would otherwise raise the root log
    # level for the rest of the pytest process and let httpx log request URLs
    # (secrets included) during unrelated tests.
    script = (
        "import sys, setuptools.build_meta as bm;"
        "print('WHEEL=' + bm.build_wheel(sys.argv[1]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(wheel_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    names = [
        line.partition("=")[2]
        for line in proc.stdout.splitlines()
        if line.startswith("WHEEL=")
    ]
    assert names, f"wheel build produced no name: {proc.stdout[-500:]}"
    return wheel_dir / names[-1]


def test_built_wheel_contains_catalogue_and_notice(tmp_path: Path):
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        payload = archive.read("nexus/model/data/models.min.json")
        notice = archive.read("nexus/model/data/NOTICE").decode("utf-8")

    assert "nexus/model/data/models.min.json" in names
    assert "nexus/model/data/NOTICE" in names
    assert b"models.dev" in payload or b"anthropic" in payload

    # The attribution ships verbatim: MIT, models.dev, and no logos/unverified
    # licence marker in the vendored snapshot.
    assert "MIT License" in notice
    assert "models.dev" in notice
    assert b"pending" not in payload.lower()
    assert b'"logo' not in payload.lower()
