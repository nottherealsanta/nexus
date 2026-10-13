"""Offline packaging contract for GPT Live: no builds, only file inspection."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSOCKETS = "websockets>=14,<16"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _workflow() -> str:
    return (ROOT / ".github/workflows/native-wheels.yml").read_text(encoding="utf-8")


def test_websockets_is_a_base_dependency():
    project = _pyproject()["project"]
    assert WEBSOCKETS in project["dependencies"]


def test_live_extra_remains_a_compatible_alias():
    extras = _pyproject()["project"]["optional-dependencies"]
    assert extras["live"] == [WEBSOCKETS]


def test_uv_lock_records_websockets_as_base_dependency():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["name"] == "nexus-harness")
    assert {"name": "websockets"} in package["dependencies"]
    requires = package["metadata"]["requires-dist"]
    assert {"name": "websockets", "specifier": ">=14,<16"} in requires
    assert "live" in package["optional-dependencies"]


def test_nexus_live_binary_is_declared_from_a_locked_manifest():
    bins = {b["target"]: b for b in _pyproject()["tool"]["setuptools-rust"]["bins"]}
    live = bins["nexus-live"]
    assert live["path"] == "rust/live/Cargo.toml"
    assert "--locked" in live["cargo-manifest-args"]
    assert (ROOT / "rust/live/Cargo.lock").is_file()


def test_sdist_ships_live_rust_sources():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "rust/live/Cargo.toml" in manifest and "rust/live/Cargo.lock" in manifest
    assert "recursive-include rust/live/src" in manifest


def test_wheel_ci_smoke_tests_the_live_helper():
    # The bin is `optional`, so a failed Cargo build would otherwise ship silently.
    workflow = _workflow()
    assert "nexus-live --version" in workflow
    assert "live_helper" in workflow and "binary_path" in workflow


def test_wheel_ci_installs_alsa_and_pkgconfig_for_both_linux_libcs():
    workflow = _workflow()
    for needle in ("alsa-lib-dev", "alsa-lib-devel", "apk add", "pkg"):
        assert needle in workflow
    # Regression: musl defaults to crt-static, so ld looked for a static libasound, which
    # Alpine does not ship (no alsa-lib-static). The CRT must stay dynamic on Linux.
    assert "-C target-feature=-crt-static" in workflow
    assert "alsa-lib-static" not in workflow.replace("no `alsa-lib-static`", "")
    # Opus is pure Rust (unsafe-libopus); CMake or a system libopus is not expected.
    manifest = (ROOT / "rust/live/Cargo.toml").read_text(encoding="utf-8")
    assert "unsafe-libopus" in manifest


def test_cargo_lock_keeps_uuid_within_the_crate_msrv():
    # Regression: the locked uuid 1.28 needs Rust 1.89, but CI and the crate use 1.88.
    # The optional bin was then silently skipped and the wheel shipped without the helper.
    lock = tomllib.loads((ROOT / "rust/live/Cargo.lock").read_text(encoding="utf-8"))
    uuid = next(p for p in lock["package"] if p["name"] == "uuid")
    assert uuid["version"] == "1.18.1"


def test_native_toolchain_pin_matches_the_crate_msrv():
    manifest = tomllib.loads((ROOT / "rust/live/Cargo.toml").read_text(encoding="utf-8"))
    rust_version = manifest["package"]["rust-version"]
    assert f"{rust_version}.0" in _workflow()
