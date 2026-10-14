"""Run the gateway: one process per Nexus home, loopback listener, graceful stop."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import TextIO

from .app import GatewayApp, Limits
from .httpd import HTTPServer
from .store import GatewayError, GatewayStore
from .upstream import Launcher, Upstream, daemon_launcher

DEFAULT_PORT = 8765


async def run_gateway(
    store: GatewayStore,
    *,
    port: int = DEFAULT_PORT,
    public_url: str = "",
    stdout: TextIO,
    stderr: TextIO,
    stop: asyncio.Event | None = None,
    launcher: Launcher = daemon_launcher,
    limits: Limits | None = None,
    started: asyncio.Future[int] | None = None,
) -> int:
    """Serve until ``stop`` is set (or SIGINT/SIGTERM); returns a process exit code."""
    try:
        lock_fd = store.acquire_serve_lock()
    except GatewayError as exc:
        stderr.write(f"Error: {exc}\n")
        return 1
    upstream = Upstream(launcher=launcher)
    try:
        store.gateway_id  # noqa: B018 - create the identity before the first pairing
        app = GatewayApp(store, upstream, public_url=public_url, limits=limits)
        server = HTTPServer(app, port=port)
        await server.start()
    except (GatewayError, OSError) as exc:
        stderr.write(f"Error: cannot start the gateway: {getattr(exc, 'strerror', None) or exc}\n")
        await upstream.aclose()
        os.close(lock_fd)
        return 1
    app.bind_port(server.port)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    stdout.write(f"nexus gateway listening on http://127.0.0.1:{server.port}\n")
    if public_url:
        stdout.write(f"advertised as {public_url} (terminate TLS in front, e.g. `tailscale serve`)\n")
    stdout.flush()
    if started is not None and not started.done():
        started.set_result(server.port)
    try:
        await stop.wait()
    finally:
        await server.aclose()
        await upstream.aclose()
        os.close(lock_fd)
    return 0
