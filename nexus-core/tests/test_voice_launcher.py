"""`scripts/nexus` keeps the voice extra after `nexus voice init` (docs/voice.md)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "nexus"
CORE = (ROOT / "nexus-core").resolve()
VENV = CORE / ".venv"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or not LAUNCHER.is_file(),
                                reason="the launcher is a POSIX shell script")


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def _launch(tmp_path: Path, *args: str, marker: dict | None = None, stubs: dict[str, str] | None = None) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    _stub(bindir, "uv", 'printf "uv %s\\n" "$*"')
    for name, body in (stubs or {}).items():
        _stub(bindir, name, body)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if marker is not None:
        (home / "voice.json").write_text(json.dumps(marker))
    env = {**os.environ, "HOME": str(tmp_path), "NEXUS10_HOME": str(home),
           "PATH": f"{bindir}:{os.environ['PATH']}"}
    return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, env=env, check=True).stdout


def test_core_commands_stay_plain_without_a_marker(tmp_path):
    assert "uv run nexus status" in _launch(tmp_path, "status")


def test_core_commands_carry_the_voice_extra_after_init(tmp_path):
    out = _launch(tmp_path, "status", marker={"environment": str(VENV)})
    assert "uv run --extra voice nexus status" in out


def test_a_marker_from_another_environment_is_ignored(tmp_path):
    out = _launch(tmp_path, "status", marker={"environment": "/somewhere/else"})
    assert "--extra voice" not in out


def test_the_tui_hands_core_the_extra(tmp_path):
    stubs = {"cargo": 'printf "CORE %s\\n" "$NEXUS_TUI_CORE_CMD"'}
    out = _launch(tmp_path, "tui", marker={"environment": str(VENV)}, stubs=stubs)
    assert f"CORE uv run --project {CORE} --extra voice nexus" in out
