#!/usr/bin/env python3
"""Build the real Rust NSH web package into Python package data (NSH 0.3)."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    nsh = os.environ.get("NSH", "nsh")
    if not shutil.which(nsh):
        raise SystemExit(
            "NSH 0.3.0 missing. Install via NSH_VERSION=v0.3.0 sh <downloaded https://nsh.dev/install.sh>, add ~/.local/bin to PATH; see docs/release.md."
        )
    # NSH generates a runner workspace with its own lock. Seed it from the
    # reviewed application lock; Cargo preserves pinned versions when adding
    # the generated runner packages. Never persist machine-specific runner paths.
    subprocess.run(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(ROOT / "rust/app/Cargo.toml"),
            "--locked",
            "--no-deps",
            "--format-version",
            "1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    runner = ROOT / "rust/app/.nsh/rust"
    runner.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "rust/app/Cargo.lock", runner / "Cargo.lock")
    with tempfile.TemporaryDirectory(prefix="nexus-nsh-") as tmp:
        subprocess.run(
            [nsh, "package", str(ROOT / "rust/app"), "--target", "web", "--out", tmp],
            check=True,
        )
        source = Path(tmp) / "Nexus"
        if not (source / "index.html").is_file() or not list(source.glob("*.wasm")):
            raise RuntimeError("NSH package did not contain an index and WASM engine")
        target = ROOT / "nexus/ui/nsh_web"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


if __name__ == "__main__":
    main()
