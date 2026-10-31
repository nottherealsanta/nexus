"""Project `.mcp.json`: discovery, strict parsing and one server scope per project.

A project `.mcp.json` is trusted configuration, like a `package.json` script: the
user controls it and opening a project means accepting that it may run code.
Enabled, valid entries are loaded automatically and started with a new session;
there is no approval prompt. Disabled entries are never started, and an unusable
entry is reported without hiding the others.

The file is still parsed strictly, because a repository may be committed by
someone else. A project entry may not shadow an enabled global name, so it
cannot silently replace the user's own trusted server. A disabled global entry
(including the offered `browser-use` default) holds no name: the project entry
may use it, and if the global entry is enabled later the collision invalidates
the project entry at the next read.

Only `<project>/.mcp.json` is read. It accepts the same server maps and entry
dialects as `nexus config import-mcp` (`mcpServers`, `servers`, `mcp`,
`context_servers`); project entries may use `${workspaceFolder}` and default
their working directory to the project path.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bridge
from .config import ServerConfig, parse_all
from .manager import McpManager, McpUnavailable
from .search import Result

FILE_NAME = ".mcp.json"
MAX_FILE_BYTES = 256 * 1024
WRAPPERS = ("mcpServers", "servers", "mcp", "context_servers")
_DISABLED_PREFIX = "mcp:"


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite number {value} is not allowed")


def _read(path: Path) -> tuple[bytes, bool, str | None]:
    """Bounded bytes for `<project>/.mcp.json`, opened without following a symlink.

    `O_NOFOLLOW` and the `fstat` check happen on the same descriptor, so a symlink or a
    file swapped in after the check cannot be read out of the project (there is no
    check-then-open window).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return b"", False, None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return b"", True, ".mcp.json must be a regular file, not a symlink"
        return b"", True, f"{type(exc).__name__}: {exc}"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b"", True, ".mcp.json must be a regular file"
        if info.st_size > MAX_FILE_BYTES:
            return b"", True, f".mcp.json exceeds {MAX_FILE_BYTES} bytes"
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_FILE_BYTES:
            return b"", True, f".mcp.json exceeds {MAX_FILE_BYTES} bytes"
        return raw, True, None
    except OSError as exc:
        return b"", True, f"{type(exc).__name__}: {exc}"
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class ProjectDefinition:
    project_id: str
    project_path: str
    file: str
    exists: bool
    revision: str
    entries: dict[str, Any]
    servers: dict[str, ServerConfig]
    invalid: dict[str, str]
    warnings: tuple[str, ...]
    error: str | None

    def all_names(self) -> list[str]:
        return sorted(set(self.servers) | set(self.invalid))


class ProjectMcp:
    """One project's `.mcp.json` servers, isolated from every other project's."""

    def __init__(self, project_id: str, project_path: str, publish: Any,
                 reserved: Callable[[], set[str]] | None = None) -> None:
        self.project_id = project_id
        self.project_path = str(Path(project_path).resolve())
        self.publish = publish
        self.reserved = reserved or (lambda: set())
        self._cache_key: tuple[Any, ...] | None = None
        self._cache: ProjectDefinition | None = None
        self._revision: str | None = None
        self.manager = McpManager(
            self.emit,
            self._entries,
            self._stamp,
            workspace=self.project_path,
            defaults=False,
            log_dir=f"projects/{hashlib.sha256(project_id.encode()).hexdigest()[:16]}",
            guard=self._require,
        )

    @property
    def file(self) -> Path:
        return Path(self.project_path) / FILE_NAME

    def definition(self) -> ProjectDefinition:
        return self._definition(frozenset(self.reserved()))

    def _definition(self, reserved: frozenset[str]) -> ProjectDefinition:
        raw, exists, read_error = _read(self.file)
        key = (hashlib.sha256(raw).hexdigest(), exists, read_error, reserved)
        if self._cache is not None and key == self._cache_key:
            return self._cache
        definition = self._parse(raw, exists, read_error, reserved)
        self._cache = definition
        self._cache_key = (definition.revision, exists, read_error, reserved)
        return definition

    def _parse(self, raw: bytes, exists: bool, read_error: str | None,
               reserved: frozenset[str]) -> ProjectDefinition:
        revision = hashlib.sha256(raw).hexdigest() if raw else "missing"
        entries: dict[str, Any] = {}
        servers: dict[str, ServerConfig] = {}
        invalid: dict[str, str] = {}
        warnings: list[str] = []
        error = read_error
        if error is None and exists:
            try:
                document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                                      parse_constant=_reject_constant)
            except (UnicodeDecodeError, ValueError) as exc:
                error = f".mcp.json is not valid JSON: {exc}"
            else:
                if not isinstance(document, dict):
                    error = ".mcp.json must contain a JSON object"
                else:
                    unknown = sorted(set(document) - set(WRAPPERS))
                    if unknown:
                        warnings.append("ignored unknown top-level keys: " + ", ".join(unknown))
                    maps = [key for key in WRAPPERS if key in document]
                    for key in maps:
                        block = document[key]
                        if not isinstance(block, dict):
                            error = f".mcp.json {key!r} must be an object"
                            entries, servers, invalid = {}, {}, {}
                            break
                        for name, entry in block.items():
                            if name in entries:
                                error = f".mcp.json defines server {name!r} more than once"
                                entries, servers, invalid = {}, {}, {}
                                break
                            entries[name] = entry
                        if error:
                            break
            if error is None:
                servers, invalid = parse_all(entries, workspace=self.project_path)
        # A project server may not replace an *enabled* global server. A disabled entry (such as
        # the offered browser-use default) holds no name, so a repository may use it. Enabling that
        # entry later changes the reserved set, invalidates this definition and closes the connection.
        for name in sorted(set(servers) & reserved):
            invalid[name] = f"name {name!r} is already used by a global MCP server; rename this project entry"
            servers.pop(name, None)
        return ProjectDefinition(
            project_id=self.project_id, project_path=self.project_path, file=str(self.file),
            exists=exists, revision=revision, entries=entries, servers=servers,
            invalid=invalid, warnings=tuple(warnings), error=error)

    def _stamp(self) -> tuple[Any, ...]:
        reserved = frozenset(self.reserved())
        definition = self._definition(reserved)
        return (definition.revision, definition.exists, definition.error, tuple(sorted(reserved)))

    def _entries(self) -> dict[str, Any]:
        """Every valid entry, so the manager can hold disabled ones without starting them."""
        definition = self.definition()
        return {name: definition.entries[name] for name in definition.servers}

    def _refresh(self) -> ProjectDefinition:
        """Read the file and reconcile connections before anything uses them.

        `McpManager.configure` closes any connection whose entry changed or disappeared, so a
        changed file cannot leave the old process running after the next read. It is called here
        rather than in `definition` to keep the manager's own stamp callback from recursing.

        The definition is read again afterwards: `configure` may have noticed a file change while
        this refresh was running, and callers must judge against the configuration the manager
        actually has now, not the one from before it reconciled.
        """
        definition = self.definition()
        self.manager.configure()
        definition = self.definition()
        if definition.revision != self._revision:
            if self._revision is not None:
                self.emit("mcp.changed", {"file": definition.file})
            self._revision = definition.revision
        return definition

    def refresh(self) -> ProjectDefinition:
        """`_refresh` for callers outside this module: read, invalidate and reconcile now."""
        return self._refresh()

    async def _refresh_async(self) -> ProjectDefinition:
        """`_refresh`, then wait for every stale connection to finish closing.

        The file can change again while a close is in flight, so the read and the
        close are repeated until both are stable. Every asynchronous entry point
        below uses this before it executes anything.
        """
        for _ in range(10):
            definition = self._refresh()
            if not self.manager.closing:
                return definition
            await self.manager.settle()
        return self._refresh()

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        """Publish one project-scoped MCP event (`project_id` and `scope` are added)."""
        self.publish(kind, {**data, "project_id": self.project_id, "scope": "project"})

    # -- state -------------------------------------------------------------

    def names(self, disabled: set[str] | frozenset[str] = frozenset(), *, enabled_only: bool = True) -> list[str]:
        definition = self._refresh()
        return sorted(name for name in definition.servers
                      if (not enabled_only or definition.servers[name].enabled)
                      and _DISABLED_PREFIX + name not in disabled)

    def known_names(self) -> list[str]:
        return self._refresh().all_names()

    def valid_names(self) -> list[str]:
        """Names of entries that parsed; invalid entries never claim a scope."""
        return sorted(self._refresh().servers)

    def status(self) -> list[dict[str, Any]]:
        definition = self._refresh()
        rows: list[dict[str, Any]] = []
        if definition.error:
            rows.append({"server": "(file)", "state": "invalid", "error": definition.error, "tools": 0,
                         "enabled": False, "source": "project", "project_id": self.project_id,
                         "file": definition.file, "warnings": list(definition.warnings)})
        for name, config in definition.servers.items():
            if name in self.manager.connections:
                row = self.manager.connections[name].status()
                if not config.enabled:
                    row["state"] = "disabled"
            else:
                row = {"server": name, "state": "idle" if config.enabled else "disabled",
                       "error": None, "tools": 0, **config.describe()}
            row |= {"source": "project", "project_id": self.project_id, "file": definition.file,
                    "warnings": list(definition.warnings)}
            rows.append(row)
        for name, error in sorted(definition.invalid.items()):
            rows.append({"server": name, "state": "invalid", "error": error, "tools": 0, "enabled": False,
                         "source": "project", "project_id": self.project_id, "file": definition.file,
                         "warnings": list(definition.warnings)})
        return rows

    def row(self, name: str) -> dict[str, Any] | None:
        return next((row for row in self.status() if row["server"] == name), None)

    def log_path(self, name: str) -> Path:
        return self.manager.log_path(name)

    # -- manager delegation -------------------------------------------------

    def _require(self, target: str | ServerConfig) -> None:
        definition = self._refresh()
        if isinstance(target, ServerConfig):
            name, captured = target.name, target
        else:
            name, captured = target, None
        if name in definition.invalid:
            raise McpUnavailable(f"server {name} has an invalid project entry: {definition.invalid[name]}")
        current = definition.servers.get(name)
        if current is None:
            raise McpUnavailable(f"no MCP server named {name!r}")
        if captured is not None and captured != current:
            raise McpUnavailable(
                f"project server {name} changed since this connection was read; use the current definition")

    def connection(self, name: str) -> Any:
        self._require(name)
        return self.manager.connection(name)

    async def connect_all(self, names: list[str]) -> dict[str, str | None]:
        definition = await self._refresh_async()
        wanted = [name for name in names if name in definition.servers and definition.servers[name].enabled]
        return await self.manager.connect_all(wanted)

    async def search(self, query: str, *, servers: list[str], server: str | None, limit: int) -> Result:
        wanted = [server] if server else servers
        await self._refresh_async()
        for name in wanted:
            self._require(name)
        return await self.manager.search(query, servers=servers, server=server, limit=limit)

    async def call(self, target: str, arguments: dict[str, Any], save: Any = None) -> tuple[str, bool]:
        server = target.partition("/")[0]
        await self._refresh_async()
        self._require(server)
        return await self.manager.call(target, arguments, save=save)

    def approval(self, target: str) -> tuple[str, bool]:
        server, _, tool = target.partition("/")
        name = bridge.qualified_name(server, tool)
        try:
            definition = self._refresh()
        except (OSError, ValueError):
            return name, False
        if server not in definition.servers:
            return name, False
        return self.manager.approval(target)

    def targets(self, names: Iterable[str] | None = None) -> list[tuple[str, str]]:
        """Every `(server, tool)` known in this project's connections."""
        self._refresh()
        return self.manager.targets(names)

    async def restart(self, name: str) -> dict[str, Any]:
        await self._refresh_async()
        self._require(name)
        return await self.manager.restart(name)

    async def sync(self) -> None:
        """Apply a changed file now (closing changed or removed connections)."""
        await self._refresh_async()

    async def close(self) -> None:
        await self.manager.close()
