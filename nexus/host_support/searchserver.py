"""Start the loopback-only search service using packaged Compose assets (plan 5.3)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
from typing import TextIO

from ..config.paths import nexus_home


def start(stdout: TextIO, stderr: TextIO) -> int:
    """Prepare persistent service configuration and run bounded Docker Compose."""
    docker = shutil.which("docker")
    if docker is None:
        stderr.write("Error: Docker is required; install Docker and start its engine.\n")
        return 1
    try:
        running = subprocess.run(
            [docker, "ps", "--filter", "publish=18765", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if running.returncode:
            stderr.write("Error: Could not inspect Docker; check that its engine is running.\n")
            return 1
        for line in running.stdout.splitlines():
            row = json.loads(line)
            if (
                str(row.get("Image", "")).startswith("searxng/searxng:")
                and "127.0.0.1:18765->8080/tcp" in row.get("Ports", "")
            ):
                stdout.write("Search server already running at http://127.0.0.1:18765/search\n")
                return 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        stderr.write("Error: Could not inspect Docker; check that its engine is running.\n")
        return 1
    root = nexus_home() / "searchserver"
    root.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).with_suffix("")
    settings = root / "searxng" / "settings.yml"
    settings.parent.mkdir(exist_ok=True)
    if not settings.exists():
        shutil.copyfile(assets / "searxng" / "settings.yml", settings)
    shutil.copyfile(assets / "compose.yaml", root / "compose.yaml")
    secret = root / ".env"
    try:
        with open(secret, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            file.write(f"SEARXNG_SECRET={secrets.token_hex(32)}\n")
    except FileExistsError:
        pass
    try:
        result = subprocess.run(
            [docker, "compose", "--project-name", "nexus-searchserver", "-f", str(root / "compose.yaml"), "up", "-d"],
            cwd=root, timeout=180, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        stderr.write("Error: Docker Compose could not start the search service; check the Docker engine.\n")
        return 1
    if result.returncode:
        stderr.write("Error: Docker Compose failed to start the search service.\n")
        return 1
    stdout.write("Search server started at http://127.0.0.1:18765/search\n")
    return 0
