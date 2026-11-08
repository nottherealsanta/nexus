"""The retired NSH terminal launcher directs users to the maintained TUI."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "nexus"
pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not LAUNCHER.is_file(),
    reason="the launcher is a POSIX shell script",
)


def test_retired_nsh_terminal_points_to_tui_without_starting_core(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "uv-called"
    uv = bindir / "uv"
    uv.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    uv.chmod(0o755)
    result = subprocess.run(
        [str(LAUNCHER), "nsh", "tui"],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "NEXUS10_HOME": str(tmp_path / "home")},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "retired; use nexus tui" in result.stderr
    assert "archive/nsh-terminal-backend" in result.stderr
    assert not marker.exists()
