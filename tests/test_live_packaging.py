"""Offline packaging contract for GPT Live: no builds, only file inspection."""

import re
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


def test_wheel_ci_skips_musllinux_x86_64_but_keeps_musl_aarch64():
    # torch/constriction (the default dictation runtime) publish no musl
    # wheels, and Python markers cannot tell musl from glibc, so the musllinux
    # x86_64 leg could never install the repaired wheel and blocked the 0.7.x
    # publishes. musllinux aarch64 must still build -- musl coverage comes
    # from that leg and the apk ALSA step above.
    workflow = _workflow()
    assert "*-musllinux_x86_64" in workflow
    assert "musllinux" in workflow.replace("*-musllinux_x86_64", "")
    for needle in ("alsa-lib-dev", "alsa-lib-devel", "apk add", "pkg"):
        assert needle in workflow


def test_cargo_lock_keeps_uuid_within_the_crate_msrv():
    # Regression: the locked uuid 1.28 needs Rust 1.89, but CI and the crate use 1.88.
    # The optional bin was then silently skipped and the wheel shipped without the helper.
    lock = tomllib.loads((ROOT / "rust/live/Cargo.lock").read_text(encoding="utf-8"))
    uuid = next(p for p in lock["package"] if p["name"] == "uuid")
    assert uuid["version"] == "1.18.1"


def test_cargo_lock_pins_webrtc_ice_past_the_consent_freshness_fix():
    # Regression: webrtc-ice 0.13 only sent an ICE consent check when no packet was sent or
    # received for the keepalive interval. Audio RTP keeps the path busy, so no check was
    # ever sent and the server dropped the call after about 43 s (RFC 7675). The fix is
    # pion/ice PR #767 (webrtc-rs/rtc PR #86), shipped in webrtc-ice 0.17.2.
    lock = tomllib.loads((ROOT / "rust/live/Cargo.lock").read_text(encoding="utf-8"))
    ice = next(p for p in lock["package"] if p["name"] == "webrtc-ice")
    version = tuple(int(part) for part in ice["version"].split("."))
    assert version >= (0, 17, 2), ice["version"]


def test_native_toolchain_pin_matches_the_crate_msrv():
    versions = [
        tomllib.loads((ROOT / f"rust/{crate}/Cargo.toml").read_text(encoding="utf-8"))["package"]["rust-version"]
        for crate in ("live", "tui")
    ]
    rust_version = max(versions, key=lambda value: tuple(map(int, value.split("."))))
    assert f"{rust_version}.0" in _workflow()


def test_release_publish_pins_python_for_uv_build_and_tests():
    # constriction has cp313/cp314-only wheels; newest-Python resolution chose cp315.
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    publish = workflow.split("\n  publish:\n", 1)[1]
    setup = re.search(
        r"      - uses: astral-sh/setup-uv@[^\n]+\n(.*?)(?=      - )",
        publish,
        re.DOTALL,
    )
    assert setup is not None
    assert re.search(r'(?m)^        with:\n(?:          [^\n]*\n)*?'
                     r'          python-version: [\"\']3\.13[\"\']$', setup.group(1))
    assert publish.index(setup.group(0)) < publish.index("uv build --sdist")
    assert publish.index(setup.group(0)) < publish.index("uv run --with pytest")
