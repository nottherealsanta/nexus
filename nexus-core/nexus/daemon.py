"""Running Nexus Core as a daemon, and finding it.

One daemon per Nexus home. `serve()` runs it in the foreground on 127.0.0.1 and
writes `<home>/daemon.json` (`url`, `pid`, `token`; mode 0600) while it runs.
Clients read that file (`discover()`), and `ensure()` starts the daemon in the
background when none answers. The port is `$NEXUS_PORT`, default 4747.
Logs go to `<home>/logs/daemon.log`.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .paths import home

DEFAULT_PORT = 4747
START_TIMEOUT_S = 15.0


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


def serve(port: int | None = None) -> None:
    import uvicorn

    from .llmhub.hub import default_model
    from .server import build_app, make_runtime

    port = port or int(os.environ.get("NEXUS_PORT") or DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    app = build_app(make_runtime(default_model()), token=token, url=url)
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=5)
    finally:
        try:
            if json.loads(info_path().read_text()).get("pid") == os.getpid():
                info_path().unlink()
        except (OSError, ValueError):
            pass


def discover() -> dict[str, Any] | None:
    """daemon.json if its daemon answers, else None."""
    try:
        data = json.loads(info_path().read_text())
        response = httpx.get(f"{data['url']}/api/info", headers={"authorization": f"Bearer {data['token']}"}, timeout=2)
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return None
    return data if response.status_code == 200 else None


def start() -> dict[str, Any]:
    """Start the daemon in the background and wait until it answers."""
    log_path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(log_path(), "a") as log:
        subprocess.Popen(
            [sys.executable, "-m", "nexus", "serve"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=str(home()),
        )
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if (found := discover()) is not None:
            return found
        time.sleep(0.2)
    raise OSError(f"the daemon did not start within {START_TIMEOUT_S:.0f}s; see {log_path()}")


def ensure() -> dict[str, Any]:
    return discover() or start()


def stop() -> bool:
    found = discover()
    if found is None:
        return False
    os.kill(int(found["pid"]), signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline and discover() is not None:
        time.sleep(0.2)
    return True
