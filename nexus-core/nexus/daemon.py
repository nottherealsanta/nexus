"""Running Nexus Core as a daemon, and finding it.

One daemon per Nexus home. `serve()` runs it in the foreground on 127.0.0.1 and
writes `<home>/daemon.json` (`url`, `pid`, `token`; mode 0600) while it runs.
Clients read that file (`discover()`), and `ensure()` starts the daemon in the
background when none answers. The port is `$NEXUS_PORT`, default 4747.
Logs go to `<home>/logs/daemon.log`.

`serve()` binds the port before it writes `daemon.json`, so a second daemon that
cannot get the port fails without touching the running daemon's file. `stop()`
kills the pid in that file even when the daemon no longer answers, and escalates
to SIGKILL when a live event stream holds shutdown past the grace period.
`start()` never spawns a daemon while one is registered and starting, and says
so plainly when an unregistered process already holds the port.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from .paths import home

DEFAULT_PORT = 4747
START_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 10.0
KILL_TIMEOUT_S = 2.0


def info_path() -> Path:
    return home() / "daemon.json"


def log_path() -> Path:
    return home() / "logs" / "daemon.log"


def _write_info(data: dict[str, Any]) -> None:
    path = info_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(temp, path)


def _read_info() -> dict[str, Any] | None:
    """daemon.json as written, whether or not its daemon still answers."""
    try:
        data = json.loads(info_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _port() -> int:
    return int(os.environ.get("NEXUS_PORT") or DEFAULT_PORT)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_daemon(pid: int) -> bool:
    """Whether `pid` still looks like a `nexus serve`, so a reused pid is never killed."""
    try:
        done = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return "nexus" in done.stdout and "serve" in done.stdout


def _registered_pid() -> int | None:
    """The pid `daemon.json` names, if that process is still a live `nexus serve`."""
    try:
        pid = int((_read_info() or {})["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    return pid if _running(pid) else None


def _running(pid: int) -> bool:
    """A live `nexus serve`: a zombie still answers `kill(pid, 0)`, so look at the command too."""
    return _alive(pid) and _is_daemon(pid)


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _bind(port: int) -> socket.socket:
    """Take the port before anything else runs, so a failed bind writes no state."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as exc:
        listener.close()
        reason = exc.strerror or str(exc)
        raise SystemExit(
            f"nexus: cannot listen on 127.0.0.1:{port} ({reason}); another daemon or program holds it "
            f"(see `nexus ps` and {log_path()}), or set $NEXUS_PORT"
        ) from None
    return listener


def port_conflict() -> str | None:
    """A sentence for a listener on the daemon port that this home's `daemon.json` does not name."""
    port = _port()
    if _registered_pid() is not None or not _listening(port):
        return None
    return (
        f"127.0.0.1:{port} is already in use by a process that is not this home's daemon: "
        f"no daemon.json points at it. Stop that process, or set $NEXUS_PORT to another port."
    )


def serve(port: int | None = None) -> None:
    import uvicorn

    from .server import build_app, make_runtime

    port = port or _port()
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    listener = _bind(port)
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    try:
        app = build_app(make_runtime(), token=token, url=url)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=5
        )
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        if (_read_info() or {}).get("pid") == os.getpid():
            with suppress(OSError):
                info_path().unlink()
        listener.close()


def discover() -> dict[str, Any] | None:
    """daemon.json if its daemon answers, else None."""
    data = _read_info()
    if data is None:
        return None
    try:
        response = httpx.get(f"{data['url']}/api/info", headers={"authorization": f"Bearer {data['token']}"}, timeout=2)
    except (KeyError, httpx.HTTPError):
        return None
    return data if response.status_code == 200 else None


def start() -> dict[str, Any]:
    """Start the daemon in the background and wait until it answers."""
    if (found := discover()) is not None:
        return found
    if (conflict := port_conflict()) is not None:
        raise OSError(conflict)
    registered = _registered_pid()
    child = None
    if registered is None:
        log_path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(log_path(), "a") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "nexus", "serve"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=str(home()),
            )
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if (found := discover()) is not None:
            return found
        if child is not None and child.poll() is not None:
            if (found := discover()) is not None:
                return found
            raise OSError(f"the daemon exited while starting (status {child.returncode}); see {log_path()}")
        time.sleep(0.2)
    raise OSError(f"the daemon did not start within {START_TIMEOUT_S:.0f}s; see {log_path()}")


def ensure() -> dict[str, Any]:
    return discover() or start()


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _running(pid):
            return True
        time.sleep(0.15)
    return not _running(pid)


def stop() -> bool:
    """Stop the daemon `daemon.json` names, even when it no longer answers."""
    pid = _registered_pid()
    if pid is None:
        # A file that names no live daemon is litter; never let it block the next start.
        with suppress(OSError):
            if info_path().exists():
                info_path().unlink()
        return False
    os.kill(pid, signal.SIGTERM)
    if not _wait_gone(pid, STOP_TIMEOUT_S):
        # An open event stream can outlive the graceful window and keep the port.
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
        _wait_gone(pid, KILL_TIMEOUT_S)
    if (_read_info() or {}).get("pid") == pid:
        with suppress(OSError):
            info_path().unlink()
    return True
