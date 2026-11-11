"""The local SearXNG behind `web_search`: its status, and starting it on request.

Core never starts it by itself (the tool's error says how). The user asks from
Settings, which calls `start()`. Apple `container` is preferred, then Docker, both
with the files in `nexus-search/` (see its README). The default image is pinned there.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import ConfigFile

CONTAINER_NAME = "nexus-search"
DEFAULT_IMAGE = "docker.io/searxng/searxng@sha256:eb2a404db174d33a04cd9f2e519bcae2ca722a58923510652cc1314be94e8e63"
START_WAIT_S = 30.0
COMMAND_TIMEOUT_S = 120


def directory() -> Path:
    return Path(__file__).resolve().parents[2] / "nexus-search"


def _url() -> str:
    return ConfigFile().read()["values"]["web"]["search_url"]


def _local(url: str) -> bool:
    return urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")


def _answers(url: str) -> bool:
    try:
        return httpx.get(url, params={"q": "nexus", "format": "json"}, timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def _json_forbidden(url: str) -> bool:
    try:
        return httpx.get(url, params={"q": "nexus", "format": "json"}, timeout=3).status_code == 403
    except httpx.HTTPError:
        return False


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False)


def _runtime() -> str | None:
    if shutil.which("container"):
        return "container"
    if shutil.which("docker"):
        return "docker"
    return None


def status() -> dict[str, Any]:
    """`state`: running, stopped (set up, not answering), absent (never set up), or unavailable (cannot start here)."""
    url = _url()
    if _answers(url):
        return {"state": "running", "url": url, "runtime": None, "detail": None}
    if not _local(url):
        return {"state": "unavailable", "url": url, "runtime": None,
                "detail": "[web] search_url is not a local address; Nexus can only start the local service"}
    if not directory().is_dir():
        return {"state": "unavailable", "url": url, "runtime": None, "detail": "nexus-search/ is not part of this install"}
    runtime = _runtime()
    if runtime is None:
        return {"state": "unavailable", "url": url, "runtime": None,
                "detail": "neither Apple container nor Docker is installed"}
    state = "stopped" if _exists(runtime) else "absent"
    return {"state": state, "url": url, "runtime": runtime, "detail": None}


def _exists(runtime: str) -> bool:
    try:
        if runtime == "container":
            return _run("container", "inspect", CONTAINER_NAME).stdout.strip() not in ("", "[]")
        return _run("docker", "container", "inspect", CONTAINER_NAME).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stale(folder: Path) -> bool:
    """An Apple container keeps the mount path it was created with; the repo may have moved since."""
    try:
        mounts = json.loads(_run("container", "inspect", CONTAINER_NAME).stdout)[0]["configuration"]["mounts"]
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError):
        return False
    return not any(m.get("source") == str(folder / "searxng") for m in mounts)


def _secret() -> str:
    env = directory() / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SEARXNG_SECRET=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    value = secrets.token_hex(32)
    previous = env.read_text() if env.exists() else ""
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{previous}{'' if not previous or previous.endswith(chr(10)) else chr(10)}SEARXNG_SECRET={value}\n")
    return value


def _image() -> str:
    env = directory() / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("SEARXNG_IMAGE=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return DEFAULT_IMAGE


def _fail(step: subprocess.CompletedProcess[str]) -> RuntimeError:
    text = (step.stderr or step.stdout or "").strip().splitlines()
    return RuntimeError((text[-1] if text else f"exit {step.returncode}")[:300])


def _launch(runtime: str, exists: bool) -> None:
    folder = directory()
    if runtime == "docker":
        _secret()
        step = _run("docker", "compose", "up", "-d", cwd=folder)
    else:
        if _run("container", "system", "status").returncode != 0:
            step = _run("container", "system", "start", "--enable-kernel-install")
            if step.returncode != 0:
                raise _fail(step)
        if exists and _stale(folder):
            step = _run("container", "delete", "--force", CONTAINER_NAME)
            if step.returncode != 0:
                raise _fail(step)
            exists = False
        if exists:
            step = _run("container", "start", CONTAINER_NAME)
        else:
            step = _run("container", "run", "--detach", "--name", CONTAINER_NAME,
                        "--publish", "127.0.0.1:18765:8080",
                        "--env", "SEARXNG_BASE_URL=http://127.0.0.1:18765/",
                        "--env", f"SEARXNG_SECRET={_secret()}",
                        "--volume", f"{folder / 'searxng'}:/etc/searxng", _image())
    if step.returncode != 0:
        raise _fail(step)


def start() -> dict[str, Any]:
    """Start the service and wait for it to answer. Raises RuntimeError with a short reason."""
    current = status()
    if current["state"] == "running":
        return current
    if current["state"] == "unavailable":
        raise RuntimeError(current["detail"])
    if _json_forbidden(current["url"]):
        raise RuntimeError(
            f"A service at {current['url']} returned HTTP 403 for a JSON search. "
            "An existing SearXNG may have JSON disabled or a limiter enabled. "
            "Enable json in its search.formats and check its limiter, then restart that service; "
            "or stop it before starting Nexus search. Nexus will not replace it."
        )
    try:
        _launch(current["runtime"], current["state"] == "stopped")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}"[:300]) from exc
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if _answers(current["url"]):
            return status()
        time.sleep(1)
    raise RuntimeError("started, but nothing answers yet; SearXNG can take a few more seconds")
