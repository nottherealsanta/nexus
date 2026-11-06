"""Isolated Core server used by terminal_core.rs."""
import asyncio
import os
from pathlib import Path
import socket
import sys

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root / "nexus-core"), str(root / "nexus-core/.venv/lib/python3.14/site-packages")]

import uvicorn
from nexus.server import build_app, make_runtime
from nexus.store import Store

async def main():
    home = Path(os.environ["NEXUS10_HOME"])
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    runtime = make_runtime(Store(home / "db.sqlite"))
    project = runtime.open_project(str(workspace))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(build_app(runtime, token="test-secret", url=url), log_level="error"))
    print(f"{url} {project.id} {workspace}", flush=True)
    await server.serve(sockets=[sock])

asyncio.run(main())
