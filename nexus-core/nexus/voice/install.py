"""`nexus voice init`: install the heavy voice runtime into the environment Nexus runs from.

The packages (moondream's Photon runtime, torch, ptts, ...) are the optional
`voice` extra in pyproject.toml; they are never base dependencies, so status
checks and installs on unsupported platforms stay light.

A successful init leaves `$NEXUS10_HOME/voice.json` naming that environment. The
development launcher (`scripts/nexus`) reads it and passes `--extra voice` to its
`uv sync` and `uv run`, so a build or daemon restart never prunes the runtime.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from ..paths import home

COMMAND = "nexus voice init"
_EXTRA = re.compile(r"""\s*and\s*extra\s*==\s*['"]voice['"]|extra\s*==\s*['"]voice['"]\s*and\s*|extra\s*==\s*['"]voice['"]""")


def requirements() -> list[str]:
    """The `voice` extra's requirements, with the extra marker removed and platform markers kept."""
    found = []
    for line in metadata.requires("nexus10") or []:
        if not _EXTRA.search(line):
            continue
        spec, _, marker = line.partition(";")
        marker = _EXTRA.sub("", marker).strip()
        if marker.startswith("(") and marker.count("(") > marker.count(")"):
            marker = marker[1:]
        found.append(f"{spec.strip()}; {marker}" if marker else spec.strip())
    return found


def install_command(reqs: list[str]) -> list[str]:
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, *reqs]
    return [sys.executable, "-m", "pip", "install", *reqs]


def marker() -> Path:
    """Where an install is remembered, for the launcher to keep the extra in its syncs."""
    return home() / "voice.json"


def initialised() -> bool:
    return marker().is_file()


def environment() -> str:
    """The environment this process runs from, as `scripts/nexus` resolves the project venv."""
    return str(Path(sys.prefix).resolve())


def remember() -> None:
    path = marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"environment": environment(), "python": sys.executable}) + "\n", encoding="utf-8")


def init() -> int:
    reqs = requirements()
    if not reqs:
        print("This install of Nexus lists no voice packages.", file=sys.stderr)
        return 1
    print(f"Installing the voice runtime ({len(reqs)} packages, including Photon and torch) into {sys.prefix}")
    code = subprocess.run(install_command(reqs), check=False).returncode
    if code:
        print(f"The install failed (exit {code}).", file=sys.stderr)
        return code
    remember()
    from . import status
    for name, entry in status.status().items():
        print(f"{name}: {entry['state']} ({entry['repo']})" + (f" - {entry['error']}" if entry["error"] else ""))
    print("Voice stays initialised: `nexus build` and `nexus restart` keep the extra installed. "
          "Restart the daemon (nexus daemon stop) so it sees the new packages. "
          "Models download only after you ask for it in Settings or with POST /api/voice/stt/download.")
    return 0
