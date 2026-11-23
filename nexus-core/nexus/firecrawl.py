"""Firecrawl Local behind the `firecrawl` tool: install, start and talk to the one binary.

`firecrawl-local` (https://github.com/firecrawl/firecrawl, `apps/local`, crates.io, AGPL-3.0) scrapes, crawls
and maps from this machine. Nexus uses only those three, plus the PDF and Office parsing built into scrape;
search, Alexandria and Firecrawl's cloud hand-off are never used. It runs as a separate process (never linked).

A Nexus release ships the binary (`firecrawl-local-<os>-<arch>`, built from the pinned crates.io version by the release
workflow, installed by `install.sh` into `$PREFIX/bin`), so users build nothing. `nexus firecrawl init` is the fallback
for a platform or release without one: it builds the pinned version into `$NEXUS10_HOME/firecrawl/` with cargo. The daemon starts
`firecrawl-local serve` by the first `firecrawl` call, never at start-up, and stops it when the daemon stops. The
server listens on a free loopback port, answers only requests with its random bearer token, refuses private,
loopback and link-local targets (its default; Nexus never passes `--allow-private`) and runs with
`FIRECRAWL_CLOUD=off`, so a page this machine cannot load fails instead of being sent to Firecrawl's cloud.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from .paths import home
from .tools.base import ToolError

CRATE = "firecrawl-local"
VERSION = "0.1.1"
COMMAND = "nexus firecrawl init"
START_WAIT_S = 15.0
MAX_RESPONSE_BYTES = 20_000_000
INSTALL_TIMEOUT_S = 1800


def root() -> Path:
    return home() / "firecrawl"


def log_path() -> Path:
    return home() / "logs" / "firecrawl.log"


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def binary() -> str | None:
    """The `firecrawl-local` to run: one `nexus firecrawl init` built, else the one `install.sh` put in `$PREFIX/bin`
    (default `~/.local/bin`, which a daemon started from a GUI may not have on PATH), else one on PATH."""
    own = root() / "bin" / CRATE
    if _executable(own):
        return str(own)
    installed = Path(os.environ.get("PREFIX") or Path.home() / ".local") / "bin" / CRATE
    if _executable(installed):
        return str(installed)
    return shutil.which(CRATE)


def version(path: str) -> str | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def find_cargo() -> str | None:
    found = shutil.which("cargo")
    if found:
        return found
    candidate = Path.home() / ".cargo" / "bin" / "cargo"
    return str(candidate) if candidate.is_file() else None


def init() -> int:
    """`nexus firecrawl init`: build and install the pinned release (a few minutes; needs Rust and a C compiler)."""
    cargo = find_cargo()
    if cargo is None:
        print("cargo was not found: install Rust from https://rustup.rs (and a C compiler: `xcode-select --install` "
              "on macOS, build-essential on Debian or Ubuntu), then run this again.", file=sys.stderr)
        return 1
    print(f"› Building {CRATE} {VERSION} from crates.io into {root()} (a few minutes)...", flush=True)
    code = subprocess.run([cargo, "install", CRATE, "--version", VERSION, "--locked", "--root", str(root())],
                          check=False, timeout=INSTALL_TIMEOUT_S).returncode
    if code:
        print(f"Installing {CRATE} failed (exit {code}); see the error above.", file=sys.stderr)
        return code
    path = binary()
    print(f"\n{CRATE} {version(path) if path else '?'} installed at {path}.")
    print("It starts with the first `firecrawl` tool call. Pages that need JavaScript use the Chrome or Chromium on "
          "this machine; without one, static pages and PDFs still work.")
    return 0


def status() -> dict[str, Any]:
    """`state`: ready, or missing (not installed). Never starts the server."""
    path = binary()
    if path is None:
        return {"state": "missing", "path": None, "version": None, "detail": f"not installed; run `{COMMAND}`"}
    found = version(path)
    if found is None:
        return {"state": "broken", "path": path, "version": None, "detail": f"{path} does not run"}
    detail = None if found.endswith(VERSION) else f"Nexus pins {VERSION}; this is {found}. It is used as it is."
    return {"state": "ready", "path": path, "version": found, "detail": detail}


NOT_INSTALLED = (f"firecrawl: {CRATE} is not installed, so firecrawl will keep failing until the user installs it. "
                 "Do not install it yourself or retry; use web_fetch on a URL you know, or tell the user. "
                 f"(The user can run `{COMMAND}`.)")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    """The one `firecrawl-local serve` process of this daemon."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.port = 0
        self.token = ""
        self.client: httpx.AsyncClient | None = None
        self.lock = asyncio.Lock()

    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def ensure(self) -> httpx.AsyncClient:
        async with self.lock:
            if self.running() and self.client is not None:
                return self.client
            await self._stop()
            path = binary()
            if path is None:
                raise ToolError(NOT_INSTALLED)
            self.port, self.token = _free_port(), secrets.token_urlsafe(32)
            env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR", "USER") if key in os.environ}
            env.update(FIRECRAWL_CLOUD="off", FIRECRAWL_LOCAL_HOME=str(root() / "state"),
                       FIRECRAWL_LOCAL_TOKEN=self.token)
            log = log_path()
            log.parent.mkdir(parents=True, exist_ok=True)
            (root() / "state").mkdir(parents=True, exist_ok=True)
            with log.open("ab") as sink:
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        path, "serve", "--host", "127.0.0.1", "--port", str(self.port),
                        stdin=asyncio.subprocess.DEVNULL, stdout=sink, stderr=sink, env=env)
                except OSError as exc:
                    raise ToolError(f"firecrawl: {CRATE} could not start: {exc.strerror or exc}") from exc
            self.client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self.port}", trust_env=False, follow_redirects=False,
                headers={"Authorization": f"Bearer {self.token}"})
            await self._wait_ready()
            return self.client

    async def _wait_ready(self) -> None:
        assert self.client is not None and self.process is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_WAIT_S
        while loop.time() < deadline:
            if self.process.returncode is not None:
                break
            try:
                if (await self.client.get("/health", timeout=2)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        await self._stop()
        raise ToolError(f"firecrawl: {CRATE} did not start (see {log_path()}); the next call tries again")

    async def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                      timeout: float) -> tuple[int, dict[str, Any]]:
        client = await self.ensure()
        try:
            async with client.stream(method, path, json=body, timeout=timeout) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ToolError("firecrawl: the result was too large")
                status = response.status_code
        except httpx.TimeoutException as exc:
            raise ToolError("firecrawl: the request timed out") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"firecrawl: {CRATE} did not answer; the next call starts it again") from exc
        try:
            import json
            payload = json.loads(bytes(data))
        except ValueError as exc:
            raise ToolError(f"firecrawl: {CRATE} returned something that is not JSON (HTTP {status})") from exc
        if not isinstance(payload, dict):
            raise ToolError(f"firecrawl: {CRATE} returned an unexpected result (HTTP {status})")
        return status, payload

    async def _stop(self) -> None:
        process, client = self.process, self.client
        self.process, self.client = None, None
        if client is not None:
            await client.aclose()
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def close(self) -> None:
        async with self.lock:
            await self._stop()


_SERVER: Server | None = None


def server() -> Server:
    global _SERVER
    if _SERVER is None:
        _SERVER = Server()
    return _SERVER


async def close() -> None:
    """Stop the server (daemon shutdown). Safe when it never started."""
    global _SERVER
    current, _SERVER = _SERVER, None
    if current is not None:
        await current.close()
