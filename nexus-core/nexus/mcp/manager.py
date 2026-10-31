"""MCP servers: lazy connections, health, restart and one place to call a tool.

A server connects the first time something needs it (a search or a call), never
at daemon start and never inside `create_session`. Each connection runs in its
own task, so the transport's cancel scopes are entered and left by one task.
A server that dies never fails a turn: its tools return an error result and an
`mcp.failed` event is published. A failed connect is not retried for a growing
delay (5 s up to 60 s) unless the user restarts it.

stderr of a stdio server goes to `~/.nexus/logs/mcp/<server>.log` (bounded,
truncated when it grows past 1 MiB at the next start), never into context.

Adapted from old Nexus `nexus/mcp/manager.py` (the immutable snapshots,
generations, circuit breaker and list cache are replaced by one connection
object per server) and `client.py` (the hand-written transports are replaced by
the `mcp` SDK's `Client`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ..paths import home
from . import bridge
from .config import ServerConfig, parse_all, with_defaults
from .search import Index, Result

HEALTH_INTERVAL = 20.0  # a tools/list round trip; `ping` is gone from newer protocol versions
HEALTH_TIMEOUT = 10.0
RELIST_AFTER = 60.0
BACKOFF = (5.0, 15.0, 30.0, 60.0)
MAX_LOG_BYTES = 1024 * 1024
MAX_TOOLS = 2000
Publish = Callable[[str, dict[str, Any]], Any]
Guard = Callable[[ServerConfig], None]


class McpUnavailable(Exception):
    """The server is off, invalid, or could not be reached; the message says why."""


def log_path(name: str) -> Path:
    return home() / "logs" / "mcp" / f"{bridge.part(name)}.log"


def _transport(config: ServerConfig, errlog: Any) -> Any:
    from mcp.client.stdio import StdioServerParameters, stdio_client
    if config.transport == "stdio":
        return stdio_client(StdioServerParameters(command=config.command, args=list(config.args),
                                                  env=config.env or None, cwd=config.cwd), errlog=errlog)
    if config.transport == "sse":
        from mcp.client.sse import sse_client
        return sse_client(config.url, headers=config.headers or None, timeout=config.connect_timeout)
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    return streamable_http_client(config.url, http_client=httpx2.AsyncClient(
        headers=config.headers or None, timeout=httpx2.Timeout(config.call_timeout, connect=config.connect_timeout,
                                                                read=max(config.call_timeout, 300.0))))


class Connection:
    def __init__(self, config: ServerConfig, publish: Publish, *, log_file: Path | None = None,
                 guard: Guard | None = None):
        self.config = config
        self.publish = publish
        self.log_file = log_file or log_path(config.name)
        self.guard = guard
        self.state = "idle"  # idle | connecting | connected | failed
        self.error: str | None = None
        self.tools: list[Any] = []
        self.instructions = ""  # what the server says about itself, sanitized and clipped
        self.listed_at = 0.0
        self.failures = 0
        self.retry_after = 0.0
        self.client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._retired = False

    def retire(self) -> None:
        """Mark this connection stale at once and cancel its startup or process."""
        self._retired = True
        self._stop.set()
        self._ready.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def _check(self) -> None:
        """Re-check that this connection's captured configuration is still current."""
        if self._retired:
            raise McpUnavailable(f"server {self.config.name} changed; use the current configuration")
        if self.guard is not None:
            self.guard(self.config)
            # The guard reconciles the current definition and can retire this connection while it
            # runs. A retired connection must not go on to start its process.
            if self._retired:
                raise McpUnavailable(f"server {self.config.name} changed; use the current configuration")

    async def connect(self) -> None:
        """Single-flight: concurrent callers share one attempt."""
        if self._retired:
            raise McpUnavailable(f"server {self.config.name} changed; use the current configuration")
        if self.state == "connected":
            return
        if self.state == "failed" and time.monotonic() < self.retry_after:
            raise McpUnavailable(f"server {self.config.name} is unavailable: {self.error}")
        if self._task is None or self._task.done():
            self.state, self.error = "connecting", None
            self._ready, self._stop = asyncio.Event(), asyncio.Event()
            self._task = asyncio.create_task(self._run(), name=f"mcp-{self.config.name}")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready.wait()), self.config.connect_timeout + 5)
        except TimeoutError:
            self._fail(f"did not connect within {self.config.connect_timeout:g} s")
        if self.state != "connected":
            raise McpUnavailable(f"server {self.config.name} is unavailable: {self.error}")
        self._check()

    def _filter(self, tools: list[Any]) -> list[Any]:
        include, exclude = set(self.config.include), set(self.config.exclude)
        return [t for t in tools if (not include or t.name in include) and t.name not in exclude][:MAX_TOOLS]

    async def _list(self) -> list[Any]:
        found: list[Any] = []
        cursor = None
        for _ in range(50):
            page = await self.client.list_tools(cursor=cursor)
            found += page.tools
            cursor = page.next_cursor
            if not cursor or len(found) >= MAX_TOOLS:
                break
        self.listed_at = time.monotonic()
        return self._filter(found)

    async def refresh(self) -> None:
        if self.state != "connected" or time.monotonic() - self.listed_at < RELIST_AFTER:
            return
        try:
            tools = await self._list()
        except Exception as exc:  # noqa: BLE001 - a failing server must not fail a search
            self._fail(f"listing tools failed: {exc}")
            return
        before = {t.name for t in self.tools}
        self.tools = tools
        after = {t.name for t in tools}
        if before != after:
            self.publish("mcp.tools_changed", {"server": self.config.name, "added": sorted(after - before),
                                               "removed": sorted(before - after)})

    async def _run(self) -> None:
        from mcp import Client
        config = self.config
        errlog = None
        try:
            if self._retired:
                return
            # Re-check in the task that actually starts the process: editing the entry between
            # `connect()` and this point must stop the spawn. The guard receives this connection's
            # captured config.
            try:
                self._check()
            except McpUnavailable as exc:
                self._fail(str(exc))
                return
            if config.transport == "stdio":
                path = self.log_file
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = "w" if path.exists() and path.stat().st_size > MAX_LOG_BYTES else "a"
                errlog = path.open(mode, encoding="utf-8", errors="replace")
            async def on_message(message: Any) -> None:
                if isinstance(message, Exception) and not self._stop.is_set():
                    self._fail(_describe(message))

            async with Client(_transport(config, errlog), read_timeout_seconds=config.call_timeout,
                              message_handler=on_message) as client:
                self.client = client
                self.tools = await self._list()
                self.instructions = bridge.sanitize(getattr(client, "instructions", None) or "")[:600].strip()
                self.state, self.error, self.failures = "connected", None, 0
                self.publish("mcp.connected", {"server": config.name, "tools": len(self.tools)})
                self._ready.set()
                while not self._stop.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), HEALTH_INTERVAL)
                        break
                    self.tools = await asyncio.wait_for(self._list(), HEALTH_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - includes ExceptionGroup from the transport's task group
            if not self._stop.is_set():
                self._fail(_describe(exc))
        finally:
            was = self.state
            self.client = None
            if errlog is not None:
                errlog.close()
            if was == "connected":
                self.state = "idle"
                self.publish("mcp.disconnected", {"server": config.name})
            self._ready.set()

    def _fail(self, reason: str) -> None:
        reason = bridge.sanitize(reason, keep_newlines=False)[:300]
        first = self.state != "failed"
        self.state, self.error = "failed", reason
        self.failures += 1
        self.retry_after = time.monotonic() + BACKOFF[min(self.failures - 1, len(BACKOFF) - 1)]
        if first:
            self.publish("mcp.failed", {"server": self.config.name, "error": reason})
        self._ready.set()
        self._stop.set()

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        await self.connect()
        self._check()
        client = self.client
        if client is None:
            raise McpUnavailable(f"server {self.config.name} is unavailable: {self.error or 'not connected'}")
        try:
            # An edit may have landed while `connect()` waited. Check again immediately before
            # handing the call to the transport so a stale connection never executes it.
            self._check()
            return await client.call_tool(tool, arguments, read_timeout_seconds=self.config.call_timeout)
        except McpUnavailable:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            text = _describe(exc)
            if self.client is None or "closed" in text.lower() or "connection" in text.lower():
                self._fail(text)
                raise McpUnavailable(f"server {self.config.name} is unavailable: {text}") from None
            raise McpUnavailable(f"{self.config.name}/{tool} failed: {text}") from None

    async def close(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, 10)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        if self.state in ("connected", "connecting"):
            self.state = "idle"

    def status(self) -> dict[str, Any]:
        return {"server": self.config.name, "state": self.state, "error": self.error, "tools": len(self.tools),
                **self.config.describe()}


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = [_describe(e) for e in exc.exceptions]
        return "; ".join(dict.fromkeys(inner))
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class McpManager:
    def __init__(self, publish: Publish, read_servers: Callable[[], Any], stamp: Callable[[], Any] = lambda: None,
                 *, workspace: str = "", defaults: bool = True, log_dir: str | None = None,
                 guard: Guard | None = None):
        self.publish = publish
        self.read_servers = read_servers
        self.stamp = stamp  # changes when the config file does, so edits apply on the next use
        self.workspace = workspace
        self.defaults = defaults
        self.log_dir = log_dir
        self.guard = guard
        self._stamped: Any = object()
        self.connections: dict[str, Connection] = {}
        self.invalid: dict[str, str] = {}
        self._index: tuple[tuple[Any, ...], Index] | None = None
        self._configured = False
        self._closing: set[asyncio.Task[None]] = set()

    def configure(self) -> None:
        """Read `[mcp.servers]` again: new servers appear, changed or removed ones close, the rest keep running."""
        self._stamped = self.stamp()
        entries = self.read_servers()
        servers, self.invalid = parse_all(with_defaults(entries) if self.defaults else entries,
                                          workspace=self.workspace)
        self._configured = True
        for name in [n for n, c in self.connections.items() if servers.get(n) != c.config]:
            old = self.connections.pop(name)
            old.retire()
            if _running():
                task = asyncio.get_running_loop().create_task(old.close())
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)
        for name, config in servers.items():
            if name not in self.connections:
                self.connections[name] = Connection(config, self.publish, log_file=self._log_file(name),
                                                    guard=self._validate)

    def _validate(self, config: ServerConfig) -> None:
        """Refuse a connection whose captured entry is no longer the current one."""
        self._ready()
        if self.guard is not None:
            self.guard(config)
            # The guard may have reconciled a changed file and replaced this connection.
            self._ready()
        current = self.connections.get(config.name)
        if current is None or current.config != config:
            raise McpUnavailable(
                f"server {config.name} changed since this connection was read; use the current definition")

    @property
    def closing(self) -> bool:
        return any(not task.done() for task in self._closing)

    async def settle(self) -> None:
        """Wait for connections retired by `configure()` to finish closing."""
        while tasks := tuple(task for task in self._closing if not task.done()):
            await asyncio.gather(*tasks, return_exceptions=True)
        self._closing = {task for task in self._closing if not task.done()}

    async def _ensure_current(self) -> None:
        """Apply an edit and finish its stale cancellations before using a connection."""
        self._ready()
        while self.closing:
            await self.settle()
            self._ready()

    def _log_file(self, name: str) -> Path:
        if self.log_dir is None:
            return log_path(name)
        return home() / "logs" / "mcp" / self.log_dir / f"{bridge.part(name)}.log"

    def log_path(self, name: str) -> Path:
        return self._log_file(name)

    def _ready(self) -> None:
        if not self._configured or self.stamp() != self._stamped:
            self.configure()

    def names(self, *, enabled_only: bool = True) -> list[str]:
        self._ready()
        return sorted(n for n, c in self.connections.items() if c.config.enabled or not enabled_only)

    def status(self) -> list[dict[str, Any]]:
        self._ready()
        rows = []
        for name, conn in sorted(self.connections.items()):
            row = conn.status()
            if not conn.config.enabled:
                row["state"] = "disabled"
            rows.append(row)
        rows += [{"server": n, "state": "invalid", "error": e, "tools": 0, "enabled": False}
                 for n, e in sorted(self.invalid.items())]
        return rows

    def connection(self, name: str) -> Connection:
        self._ready()
        if name in self.invalid:
            raise McpUnavailable(f"server {name} has an invalid entry: {self.invalid[name]}")
        conn = self.connections.get(name)
        if conn is None:
            raise McpUnavailable(f"no MCP server named {name!r}")
        if not conn.config.enabled:
            raise McpUnavailable(f"server {name} is switched off in Settings")
        return conn

    async def connect_all(self, names: list[str]) -> dict[str, str | None]:
        """Bring every named server online, in parallel. `{name: None}` when connected, else the reason it is not."""
        await self._ensure_current()

        async def one(name: str) -> tuple[str, str | None]:
            try:
                await self.connection(name).connect()
            except McpUnavailable as exc:
                return name, str(exc)
            return name, None
        return dict(await asyncio.gather(*(one(n) for n in names)))

    def listing(self, names: list[str], *, full: bool, max_listed: int) -> str:
        """The frozen context text for the servers a session starts with (fenced, untrusted)."""
        servers = {n: self.connections[n] for n in names if n in self.connections and self.connections[n].state == "connected"}
        return self.listing_connections(servers, full=full, max_listed=max_listed)

    @staticmethod
    def listing_connections(servers: dict[str, Connection], *, full: bool, max_listed: int) -> str:
        total = sum(len(c.tools) for c in servers.values())
        if full:
            rows = [{"tool": f"{n}/{t.name}", "description": t.description or "",
                     "input_schema": t.input_schema or {"type": "object"},
                     "may_change_state": not bridge.read_only(t)} for n, c in servers.items() for t in c.tools]
            return bridge.fence(json.dumps(rows, ensure_ascii=False, indent=1), server="mcp", kind="tool-definitions",
                                limit=bridge.MAX_LISTING_CHARS)
        if total <= max_listed:
            lines = [f"{n}/{t.name}: {bridge.first_line(t.description)}" for n, c in servers.items() for t in c.tools]
        else:
            lines = [f"{n} ({len(c.tools)} tools): {c.instructions or n}" for n, c in servers.items()]
            lines.append(f"{total} tools in all, more than {max_listed}: find them with `mcp_tool_search`.")
        return bridge.fence("\n".join(lines) or "(no tools)", server="mcp", kind="tool-list", limit=bridge.MAX_LISTING_CHARS)

    async def restart(self, name: str) -> dict[str, Any]:
        await self._ensure_current()
        conn = self.connection(name)
        await conn.close()
        conn.failures, conn.retry_after, conn.state, conn.error = 0, 0.0, "idle", None
        with contextlib.suppress(McpUnavailable):
            await conn.connect()
        return conn.status()

    async def drop(self, name: str) -> None:
        """Close and forget one connection now (its entry changed or was removed)."""
        conn = self.connections.pop(name, None)
        if conn is not None:
            conn.retire()
            await conn.close()

    async def search(self, query: str, *, servers: list[str], server: str | None, limit: int) -> Result:
        """Connect what is needed and rank its tools; `errors` lists servers that could not be reached."""
        await self._ensure_current()
        wanted = [server] if server else servers
        errors: list[str] = []

        async def one(name: str) -> None:
            try:
                conn = self.connection(name)
                await conn.connect()
                await conn.refresh()
            except McpUnavailable as exc:
                errors.append(str(exc))
        await asyncio.gather(*(one(n) for n in wanted))
        tools = {n: self.connections[n].tools for n in wanted if n in self.connections and self.connections[n].state == "connected"}
        key = tuple((n, tuple(t.name for t in ts)) for n, ts in sorted(tools.items()))
        if self._index is None or self._index[0] != key:
            self._index = (key, Index(tools))
        result = self._index[1].search(query, server=server, limit=limit)
        return Result(result.matches, result.total, (*result.errors, *errors))

    def find(self, target: str) -> tuple[Connection, Any]:
        """`server/tool` to its connection and tool descriptor (known only once connected)."""
        server, _, tool = target.partition("/")
        if not tool:
            raise McpUnavailable("name the tool as server/tool, as `mcp_search` shows it")
        conn = self.connection(server)
        for known in conn.tools:
            if known.name == tool:
                return conn, known
        close = [f"{server}/{t.name}" for t in conn.tools if tool.lower() in t.name.lower()][:3]
        hint = f" (closest: {', '.join(close)})" if close else ""
        if conn.state != "connected":
            raise McpUnavailable(f"server {server} is not connected yet; run `mcp_search` for it first")
        raise McpUnavailable(f"{server} has no tool {tool!r}; search again{hint}")

    async def call(self, target: str, arguments: dict[str, Any], save: Any = None) -> tuple[str, bool]:
        await self._ensure_current()
        conn, tool = self.find(target)
        result = await conn.call(tool.name, arguments)
        return bridge.convert_result(result, server=conn.config.name, tool=tool.name, save=save)

    def approval(self, target: str) -> tuple[str, bool]:
        """`(qualified name for permission rules, read-only)` of a `server/tool`; unknown means a mutating call."""
        server, _, tool = target.partition("/")
        name = bridge.qualified_name(server, tool)
        conn = self.connections.get(server)
        if conn is not None:
            for known in conn.tools:
                if known.name == tool:
                    return name, bridge.read_only(known)
        return name, False

    def targets(self, names: Iterable[str] | None = None) -> list[tuple[str, str]]:
        """Every `(server, tool)` known from the named connections (all of them when `names` is None)."""
        self._ready()
        wanted = set(self.connections) if names is None else set(names)
        return [(name, tool.name) for name in sorted(self.connections) if name in wanted
                for tool in self.connections[name].tools]

    async def close(self) -> None:
        connections = tuple(self.connections.values()) + tuple(
            task for task in self._closing if not task.done())
        for conn in self.connections.values():
            conn.retire()
        self.connections.clear()
        await asyncio.gather(*(c.close() for c in connections if isinstance(c, Connection)),
                             *(task for task in connections if not isinstance(task, Connection)),
                             return_exceptions=True)
        self._closing.clear()


def _running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
