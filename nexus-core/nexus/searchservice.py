"""The local SearXNG behind `web_search`: its status, and starting it on request.

Core never starts it by itself (the tool's error says how). The user starts it from Settings or onboarding:
`start_async()` returns at once with state `starting` and the daemon launches it in the background, while the
legacy `start()` waits until it answers. Apple `container` is preferred, then Docker.

The configuration is packaged with Nexus (`nexus/search/settings.yml`) and copied into `<home>/search/`, so an
installed wheel needs no checkout. The secret is generated once into a 0600 file beside it and reused. The image
is pinned, and its published port is loopback only.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import ConfigFile
from .paths import home

SERVICE_ID = "search"
LABEL = "Web search"
CONTAINER_NAME = "nexus-search"
DEFAULT_IMAGE = "docker.io/searxng/searxng@sha256:eb2a404db174d33a04cd9f2e519bcae2ca722a58923510652cc1314be94e8e63"
CONTAINER_PORT = 8080
START_WAIT_S = 90.0
COMMAND_TIMEOUT_S = 120
PULL_TIMEOUT_S = 1800
SETTINGS = Path(__file__).parent / "search" / "settings.yml"

_start_lock = threading.Lock()  # one launch at a time
_dispatch_lock = threading.Lock()  # one background start at a time
_lock = threading.Lock()  # the lifecycle fields below
_starting = False
_step: str | None = None
_error: str | None = None


def directory() -> Path:
    """Core's own folder for the service, inside Nexus's home (never the checkout)."""
    return home() / "search"


def _url() -> str:
    return ConfigFile().read()["values"]["web"]["search_url"]


def _local(url: str) -> bool:
    return urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")


def _host_port(url: str) -> int | None:
    try:
        return urlsplit(url).port
    except ValueError:
        return None


def _answers(url: str) -> bool:
    """The URL answers a JSON search: a 200 alone could be an error page or another service."""
    try:
        response = httpx.get(url, params={"q": "nexus", "format": "json"}, timeout=3)
        if response.status_code != 200:
            return False
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("results"), list)


def _json_forbidden(url: str) -> bool:
    try:
        return httpx.get(url, params={"q": "nexus", "format": "json"}, timeout=3).status_code == 403
    except httpx.HTTPError:
        return False


def _run(*args: str, cwd: Path | None = None, timeout: float = COMMAND_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _runtime() -> str | None:
    if shutil.which("container"):
        return "container"
    if shutil.which("docker"):
        return "docker"
    return None


def runtime_status() -> dict[str, Any]:
    """`kind` is Apple container, Docker or None; `state` is ready, stopped or missing."""
    kind = _runtime()
    if kind is None:
        return {"kind": None, "state": "missing", "detail": "neither Apple container nor Docker is installed"}
    if kind == "container":
        try:
            step = _run("container", "system", "status", timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"kind": kind, "state": "stopped", "detail": f"Apple container is not usable: {exc}"}
        if step.returncode == 0:
            return {"kind": kind, "state": "ready", "detail": "Apple container"}
        return {"kind": kind, "state": "stopped",
                "detail": "Apple container is installed but its service is not running; Nexus starts it when the service starts"}
    try:
        step = _run("docker", "info", timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"kind": kind, "state": "stopped", "detail": f"Docker is not usable: {exc}"}
    if step.returncode == 0:
        return {"kind": kind, "state": "ready", "detail": "Docker"}
    return {"kind": kind, "state": "stopped",
            "detail": "Docker is installed but its daemon is not running; start Docker Desktop (or the Docker service) and try again"}


def status(info: dict[str, Any] | None = None) -> dict[str, Any]:
    """`state`: running, starting, stopped (set up, not answering), absent (never set up), or unavailable.

    `detail` explains an unavailable service or a failed start. This never starts or launches anything.
    """
    url = _url()
    if _answers(url):
        _clear_start()
        return {"state": "running", "url": url, "runtime": None, "detail": None}
    if not _local(url):
        return {"state": "unavailable", "url": url, "runtime": None,
                "detail": "[web] search_url is not a local address; Nexus can only start the local service"}
    if _host_port(url) is None:
        return {"state": "unavailable", "url": url, "runtime": None,
                "detail": "[web] search_url needs an explicit loopback port (for example http://127.0.0.1:18765/search)"}
    if not SETTINGS.is_file():
        return {"state": "unavailable", "url": url, "runtime": None,
                "detail": "the packaged SearXNG settings are missing from this install; reinstall Nexus"}
    kind = _runtime()
    if kind is None:
        return {"state": "unavailable", "url": url, "runtime": None,
                "detail": "neither Apple container nor Docker is installed"}
    info = info or runtime_status()
    if kind == "docker" and info["state"] != "ready":
        return {"state": "unavailable", "url": url, "runtime": kind, "detail": info["detail"]}
    state = "stopped" if _exists(kind) else "absent"
    return _overlay({"state": state, "url": url, "runtime": kind, "detail": None})


def services() -> dict[str, Any]:
    """The contract the clients read: the shared runtime and one row per managed service (search only)."""
    info = runtime_status()
    return {"runtime": info, "services": [service_row(status(info))]}


def service_row(found: dict[str, Any] | None = None) -> dict[str, Any]:
    found = found or status()
    return {"id": SERVICE_ID, "label": LABEL, "state": found["state"], "url": found["url"], "detail": found["detail"]}


def _overlay(row: dict[str, Any]) -> dict[str, Any]:
    if row["state"] in ("running", "unavailable"):
        return row
    with _lock:
        starting, step, error = _starting, _step, _error
    if starting:
        return {**row, "state": "starting", "detail": step or "starting"}
    if error:
        return {**row, "detail": error}
    return row


def _clear_start(error: str | None = None) -> None:
    global _starting, _step, _error
    with _lock:
        _starting, _step, _error = False, None, error


def _set_starting(step: str) -> None:
    global _starting, _step, _error
    with _lock:
        _starting, _step, _error = True, step, None


def _set_step(step: str) -> None:
    global _step
    with _lock:
        _step = step


def start_async() -> dict[str, Any]:
    """Begin starting the service and return at once with state `starting` (or the current state).

    The work continues on a daemon thread; `status()` reports progress and the failure reason.
    Raises RuntimeError when the service cannot be started here at all.
    """
    with _dispatch_lock:
        current = status()
        if current["state"] in ("running", "starting"):
            return current
        if current["state"] == "unavailable":
            raise RuntimeError(current["detail"])
        _set_starting("starting")
        threading.Thread(target=_background, name="nexus-search-start", daemon=True).start()
        return {**current, "state": "starting", "detail": "starting"}


def _background() -> None:
    with _start_lock:
        try:
            latest = status()
            if latest["state"] == "running":
                _clear_start()
                return
            if latest["state"] == "unavailable":
                raise RuntimeError(latest["detail"])
            _run_start(latest)
        except Exception as exc:  # noqa: BLE001 - never leave clients stuck on `starting`
            _clear_start(str(exc)[:300])
        else:
            _clear_start()


def start() -> dict[str, Any]:
    """Start the service and wait for it to answer (the legacy blocking call). Raises RuntimeError."""
    deadline = time.monotonic() + 2 * START_WAIT_S
    while time.monotonic() < deadline:
        with _start_lock:
            current = status()
            if current["state"] == "running":
                return current
            if current["state"] == "unavailable":
                raise RuntimeError(current["detail"])
            if current["state"] != "starting":
                _set_starting("starting")
                try:
                    _run_start(current)
                except Exception as exc:
                    message = str(exc)[:300]
                    _clear_start(message)
                    if isinstance(exc, RuntimeError):
                        raise
                    raise RuntimeError(message) from exc
                else:
                    _clear_start()
                return status()
        time.sleep(0.2)
    with _lock:
        error = _error
    raise RuntimeError(error or "the search service is still starting; check `nexus search status`")


def _run_start(current: dict[str, Any]) -> None:
    if _json_forbidden(current["url"]):
        raise RuntimeError(
            f"A service at {current['url']} returned HTTP 403 for a JSON search. "
            "An existing SearXNG may have JSON disabled or a limiter enabled. "
            "Enable json in its search.formats and check its limiter, then restart that service; "
            "or stop it before starting Nexus search. Nexus will not replace it."
        )
    _launch(current["runtime"])
    _wait(current["url"])


def _wait(url: str) -> None:
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if _answers(url):
            return
        time.sleep(1)
    detail = _last_log()
    raise RuntimeError("started, but nothing answers yet; SearXNG can take a few more seconds"
                       + (f" (last log line: {detail})" if detail else ""))


def _last_log() -> str | None:
    kind = _runtime()
    if kind is None:
        return None
    try:
        step = (_run("container", "logs", CONTAINER_NAME, timeout=10) if kind == "container"
                else _run("docker", "logs", "--tail", "20", CONTAINER_NAME, timeout=10))
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (step.stdout + step.stderr).strip().splitlines()
    return lines[-1][:200] if lines else None


def _ensure_config() -> Path:
    """Create the service folder and write the packaged settings on first use; keep later user edits."""
    folder = directory()
    searxng = folder / "searxng"
    searxng.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    target = searxng / "settings.yml"
    if not target.exists():
        target.write_bytes(SETTINGS.read_bytes())
    return folder


def _secret() -> str:
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    env = folder / ".env"
    previous = env.read_text() if env.exists() else ""
    for line in previous.splitlines():
        if line.startswith("SEARXNG_SECRET=") and line.split("=", 1)[1].strip():
            with contextlib.suppress(OSError):
                env.chmod(0o600)
            return line.split("=", 1)[1].strip()
    value = secrets.token_hex(32)
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{previous}{'' if not previous or previous.endswith(chr(10)) else chr(10)}SEARXNG_SECRET={value}\n")
    with contextlib.suppress(OSError):
        env.chmod(0o600)
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


def _exists(runtime: str) -> bool:
    try:
        if runtime == "container":
            return _run("container", "inspect", CONTAINER_NAME, timeout=30).stdout.strip() not in ("", "[]")
        return _run("docker", "container", "inspect", CONTAINER_NAME, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stale(runtime: str, folder: Path, port: int) -> bool:
    """A container keeps the mount path and published port it was created with; either may have changed since."""
    try:
        if runtime == "container":
            configuration = json.loads(
                _run("container", "inspect", CONTAINER_NAME, timeout=30).stdout)[0]["configuration"]
            sources = {m.get("source") for m in configuration["mounts"]}
            published = configuration.get("publishedPorts")
            ok_port = published is None or any(
                p.get("hostPort") == port and p.get("containerPort") == CONTAINER_PORT for p in published)
        else:
            data = json.loads(_run("docker", "container", "inspect", CONTAINER_NAME, timeout=30).stdout)[0]
            sources = {m.get("Source") for m in data.get("Mounts") or []}
            bindings = (data.get("HostConfig") or {}).get("PortBindings")
            ok_port = bindings is None or any(
                b.get("HostPort") == str(port) for b in bindings.get(f"{CONTAINER_PORT}/tcp") or [])
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError):
        return False
    return (str(folder / "searxng") not in {str(source) for source in sources if source}) or not ok_port


def _image_present(runtime: str, image: str) -> bool:
    try:
        if runtime == "container":
            return _run("container", "image", "inspect", image, timeout=30).returncode == 0
        return _run("docker", "image", "inspect", image, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _remove(runtime: str) -> subprocess.CompletedProcess[str]:
    if runtime == "container":
        return _run("container", "delete", "--force", CONTAINER_NAME, timeout=60)
    return _run("docker", "rm", "--force", CONTAINER_NAME, timeout=60)


def _launch(runtime: str) -> None:
    folder = _ensure_config()
    _secret()
    image = _image()
    port = _host_port(_url())
    if port is None:
        raise RuntimeError("[web] search_url needs an explicit loopback port")
    if runtime == "container":
        if _run("container", "system", "status", timeout=15).returncode != 0:
            _set_step("starting Apple container")
            step = _run("container", "system", "start", "--enable-kernel-install")
            if step.returncode != 0:
                raise _fail(step)
    else:
        if _run("docker", "info", timeout=15).returncode != 0:
            raise RuntimeError("Docker is installed but its daemon is not running; start Docker Desktop "
                               "(or the Docker service) and try again")
    # Inspection can fail while the container system is stopped. Check again once it is running.
    exists = _exists(runtime)
    if exists and _stale(runtime, folder, port):
        _set_step("replacing the container (its configuration moved)")
        step = _remove(runtime)
        if step.returncode != 0:
            raise _fail(step)
        exists = False
    if exists:
        _set_step("starting the container")
        step = _run(runtime, "start", CONTAINER_NAME)
    else:
        if not _image_present(runtime, image):
            _set_step("downloading the SearXNG image (first start)")
            pull = (("container", "image", "pull", image) if runtime == "container" else ("docker", "pull", image))
            step = _run(*pull, timeout=PULL_TIMEOUT_S)
            if step.returncode != 0:
                raise _fail(step)
        _set_step("starting the container")
        args = [runtime, "run", "--detach", "--name", CONTAINER_NAME,
                "--publish", f"127.0.0.1:{port}:{CONTAINER_PORT}",
                "--env-file", str(folder / ".env"),
                "--env", f"SEARXNG_BASE_URL=http://127.0.0.1:{port}/",
                "--volume", f"{folder / 'searxng'}:/etc/searxng"]
        if runtime == "docker":
            args += ["--restart", "unless-stopped"]
        step = _run(*args, image)
    if step.returncode != 0:
        raise _fail(step)
