"""`nexus voice init`: install the heavy voice runtime into the environment Nexus runs from.

The packages (moondream's Photon runtime, torch, ptts, ...) are the optional
`voice` extra in pyproject.toml; they are never base dependencies, so status
checks and installs on unsupported platforms stay light.

A successful init leaves `$NEXUS10_HOME/voice.json` naming that environment. The
development launcher (`scripts/nexus`) reads it and passes `--extra voice` to its
`uv sync` and `uv run`, so a build or daemon restart never prunes the runtime.
"""

from __future__ import annotations

import importlib
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
        return ["uv", "pip", "install", "--quiet", "--python", sys.executable, *reqs]
    return [sys.executable, "-m", "pip", "install", "--quiet", *reqs]


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
    print("› Installing the optional voice runtime (this may take a few minutes)...", flush=True)
    code = subprocess.run(install_command(reqs), check=False).returncode
    if code:
        print(f"Voice runtime installation failed (exit {code}); see the error above.", file=sys.stderr)
        return code
    remember()
    importlib.invalidate_caches()
    from . import download, status
    from .store import ModelStore

    print("\nVoice runtime installed. Preparing models:")
    labels = {"stt": "Speech to text", "tts": "Text to speech"}
    entries = status.status()
    failed = False
    for name in ("stt", "tts"):
        entry = entries[name]
        label = labels[name]
        if entry["state"] == "unsupported":
            print(f"  {label}: unsupported — {entry['error'] or 'not supported on this platform'}", file=sys.stderr)
            failed = True
            continue
        print(f"› Preparing {label.lower()} model ({entry['repo']})...", flush=True)
        try:
            if name == "stt":
                download.download_stt(ModelStore())
            else:
                repo, revision, files = status._chosen(status.TTS, entry["repo"])["repos"][0]
                download.download_tts(repo, revision, files)
            print(f"  {label}: model downloaded.")
        except Exception as exc:  # noqa: BLE001 - report each model and allow retry
            print(f"  {label}: download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed = True
    if failed:
        print("Some models could not be prepared. Run `nexus voice init` again to retry.", file=sys.stderr)
    print("Restart Core to use the new runtime: `nexus daemon stop` then `nexus daemon start`.")
    return 1 if failed else 0
