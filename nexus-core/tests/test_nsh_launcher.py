"""The development launcher can run the shared NSH app in a terminal."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "nexus"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not LAUNCHER.is_file(),
    reason="the launcher is a POSIX shell script",
)


def launch(tmp_path, *args, supported=True):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "uv": 'printf "uv %s\\n" "$*"',
        "python3": 'printf "python3 %s\\n" "$*"',
        "nsh": (
            'if [ "$*" = "run --help" ]; then '
            f'echo {"--points-per-cell" if supported else "old-cli"}; '
            'else printf "nsh %s\\n" "$*"; fi'
        ),
    }.items():
        path = bindir / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    return subprocess.run(
        [str(LAUNCHER), *args],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "NEXUS10_HOME": str(tmp_path / "home")},
        capture_output=True, text=True, check=False,
    )


def test_nsh_tui_forwards_backend_flags(tmp_path):
    result = launch(tmp_path, "nsh", "tui", "--fps", "--points-per-cell", "10x20")
    assert result.returncode == 0, result.stderr
    assert "python3 scripts/gen.py" in result.stdout
    assert "nsh run App.tsx --target tui --fps --points-per-cell 10x20" in result.stdout


def test_nsh_tui_requires_an_updated_cli(tmp_path):
    result = launch(tmp_path, "nsh", "tui", supported=False)
    assert result.returncode != 0
    assert "nsh needs an update with TUI support" in result.stderr


def test_nsh_requires_the_tui_subcommand(tmp_path):
    result = launch(tmp_path, "nsh", "unknown")
    assert result.returncode != 0
    assert "usage: nexus nsh tui [flags]" in result.stderr
