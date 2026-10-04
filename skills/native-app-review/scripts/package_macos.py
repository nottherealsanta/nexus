#!/usr/bin/env python3
"""Wrap a built executable in a local .app so native automation can bind it.

This does not sign, install, launch, or change system permissions.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shutil


def package(binary: Path, output: Path, name: str, bundle_id: str) -> Path:
    binary = binary.resolve(strict=True)
    if not binary.is_file():
        raise ValueError("binary must be a file")
    if output.suffix != ".app":
        raise ValueError("output must end with .app")
    executable = output / "Contents" / "MacOS" / binary.name
    executable.parent.mkdir(parents=True, exist_ok=True)
    if binary == executable.resolve():
        raise ValueError("source and destination executable must differ")
    # Replacing the inode avoids stale macOS code-signing cache after rebuilds.
    executable.unlink(missing_ok=True)
    shutil.copy2(binary, executable)
    executable.chmod(0o755)
    with (output / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleName": name,
                      "CFBundleDisplayName": name, "CFBundleExecutable": binary.name,
                      "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                      "CFBundleShortVersionString": "0.1.0", "NSHighResolutionCapable": True,
                      "NSSupportsAutomaticGraphicsSwitching": True,
                      "NSMicrophoneUsageDescription": "Dictate messages into the Nexus composer."}, stream)
    return executable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--bundle-id", required=True)
    args = parser.parse_args()
    print(package(args.binary, args.output, args.name, args.bundle_id))


if __name__ == "__main__":
    main()
