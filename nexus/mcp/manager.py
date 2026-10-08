"""MCP server lifecycle manager (plan section 5.5).

The transport half of MCP lives in :mod:`nexus.mcp.client` and the translation
half in :mod:`nexus.mcp.bridge`. This is the third piece: it owns the *set* of
configured servers, their connections, their health, and the immutable snapshot
of everything they contribute to a manifest.

Responsibilities, exactly as section 5.5 lists them:

* **Lifecycle.** Lazy connect on first use (cold start is the common cost), a
  single-flight connect so N concurrent callers share one attempt, and a
  per-server serialization of listing so a lazy refresh and a
  ``list_changed``-driven refresh cannot race.
* **Health and restart.** Per-server health states with exponential backoff and
  a circuit breaker. A server that keeps failing stops being dialed until its
  cooldown elapses, then gets exactly one half-open attempt.
* **Failure isolation.** A dead server never fails a turn. Its tools vanish from
  the snapshot, ``mcp.failed`` is emitted, and every other server is untouched.
* **Snapshots.** Tools, resources, prompts and per-server state are published as
  one immutable :class:`MCPSnapshot`; tools are bridged ``RegisteredTool``
  values in bundle ``mcp`` and resources/prompts are serializable descriptors.
* **List caching.** Discovered descriptors are cached under an injected cache
  directory, keyed by server version *and* a non-secret config fingerprint, with
  atomic writes and a corruption-as-miss policy. A ``list_changed``
  notification invalidates the entry and re-lists.
* **Hot apply.** :meth:`MCPManager.apply` reconciles a whole definition set:
  added servers are parked at ``unknown`` until first use, removed/reconfigured
  servers are closed and their tools withdrawn, and the snapshot generation
  advances only when the aggregate really changed.
* **Events.** ``mcp.connected`` / ``mcp.disconnected`` / ``mcp.failed`` /
  ``mcp.tools_changed`` are published through the same defensive sink seam the
  extension manager uses (a ``Bus``, a sync callable, or an async callable).
  Every payload is JSON-safe and secret-scrubbed.
* **Deterministic close.** :meth:`aclose` stops notification watchers, closes
  every client, empties the snapshot and is idempotent.

The manager never imports the upstream ``mcp`` package, the runtime, the
extension manager, or the session layer. A client is obtained from an injected
``client_factory`` so tests (and a future integration) can supply a double or a
different backend without touching this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import inspect
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Self

from ..config.paths import project_state_dir
from ..events import Event
from .bridge import (
    DEFAULT_CAPS,
    BridgeCaps,
    BridgeIssue,
    PromptDescriptor,
    ResourceDescriptor,
    build_prompt_descriptors,
    build_read_resource_tool,
    build_resource_descriptors,
    build_tools,
    sanitize_controls,
)
from .client import MCPServerConfig, parse_server_config, redact_secrets, server_enabled
from .errors import (
    MCPClosed,
    MCPConfigError,
    MCPError,
    MCPRemoteError,
    MCPUnavailable,
)

__all__ = [
    "ApplyFailure",
    "ApplyReport",
    "MCPHealth",
    "MCPManager",
    "MCPServerSnapshot",
    "MCPServerStatus",
    "MCPSnapshot",
    "ServerDefinition",
]

#: Notification methods that mean "the server's listings changed".
LIST_CHANGED_METHODS = frozenset(
    {
        "notifications/tools/list_changed",
        "notifications/resources/list_changed",
        "notifications/prompts/list_changed",
    }
)

#: Cache schema version; a mismatch is a miss.
_CACHE_FORMAT = 1

#: Where a stdio server's bounded, redacted stderr is appended for ``doctor``,
#: relative to ``project_state_dir()`` (STATE_PLAN §5.4: machine state, not
#: project content).
_MCP_LOG_SUBDIR = Path("logs") / "mcp"
#: Per-server stderr log budget (bytes); a noisy server cannot fill the disk.
_MCP_STDERR_MAX_BYTES = 262_144

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]")


# ---------------------------------------------------------------------------
# Health and definitions
# ---------------------------------------------------------------------------


class MCPHealth(str, enum.Enum):
    """The lifecycle state of one configured server.

    ``READY``/``DEGRADED`` are connected (``DEGRADED`` means connected but the
    last listing recorded descriptor issues). ``BACKOFF`` is a failed attempt
    inside the restart window; ``FAILED`` is an open circuit awaiting cooldown.
    """

    DISABLED = "disabled"
    UNKNOWN = "unknown"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    BACKOFF = "backoff"
    FAILED = "failed"
    CLOSED = "closed"

    @property
    def connected(self) -> bool:
        return self in (MCPHealth.READY, MCPHealth.DEGRADED)

    @property
    def retryable(self) -> bool:
        return self in (MCPHealth.BACKOFF, MCPHealth.FAILED)


def _config_fingerprint(config: MCPServerConfig) -> str:
    """A non-secret identity for a server definition.

    Values of ``env``/``headers`` are folded in as a digest, not stored: a
    rotated token or changed header changes the fingerprint so the definition is
    reconnected, while the raw credential never appears in the fingerprint (and
    therefore never in a cache key or a log line). Keys are included by name;
    their order is irrelevant.
    """
    def digest(values: Mapping[str, str]) -> str:
        payload = json.dumps(
            sorted(values.items()), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    payload = {
        "tool_loading": config.tool_loading,
        "tool_loading_source": config.tool_loading_source,
        "transport": config.transport,
        "command": config.command,
        "args": list(config.args),
        "cwd": config.cwd,
        "url": config.url,
        "env_keys": sorted(config.env),
        "header_keys": sorted(config.headers),
        "env_digest": digest(config.env),
        "header_digest": digest(config.headers),
        "connect_timeout_s": config.connect_timeout_s,
        "init_timeout_s": config.init_timeout_s,
        "list_timeout_s": config.list_timeout_s,
        "call_timeout_s": config.call_timeout_s,
        "include_tools": list(config.include_tools),
        "exclude_tools": list(config.exclude_tools),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ServerDefinition:
    """One configured server: its normalized config and enabled flag."""

    name: str
    config: MCPServerConfig
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise MCPConfigError("server definition name must be a non-empty string")
        if not isinstance(self.config, MCPServerConfig):
            raise MCPConfigError("server definition config must be an MCPServerConfig")
        if not isinstance(self.enabled, bool):
            raise MCPConfigError("server definition enabled must be a bool")

    def fingerprint(self) -> str:
        """Identity of the definition's meaning (config + enabled)."""
        return f"{'on' if self.enabled else 'off'}:{_config_fingerprint(self.config)}"


@dataclass(frozen=True)
class ApplyFailure:
    """A definition that could not be parsed during an apply/construction."""

    name: str
    error: str
    error_type: str = "MCPConfigError"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "error": self.error,
            "error_type": self.error_type,
        }


@dataclass(frozen=True)
class ApplyReport:
    """The outcome of reconciling a definition set."""

    generation: int
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    reconfigured: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    failures: tuple[ApplyFailure, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.reconfigured)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "added": list(self.added),
            "removed": list(self.removed),
            "reconfigured": list(self.reconfigured),
            "unchanged": list(self.unchanged),
            "changed": self.changed,
            "failures": [failure.to_dict() for failure in self.failures],
        }


@dataclass(frozen=True)
class MCPServerSnapshot:
    """The immutable contribution and state of one server.

    Structurally satisfies ``nexus.ext.manifest.MCPServerState`` (a ``name``)
    without importing that module, so a later packet can place these in the
    manifest.
    """

    name: str
    health: MCPHealth = MCPHealth.UNKNOWN
    version: str = ""
    tools: tuple[Any, ...] = ()
    resources: tuple[ResourceDescriptor, ...] = ()
    resource_templates: tuple[ResourceDescriptor, ...] = ()
    prompts: tuple[PromptDescriptor, ...] = ()
    issues: tuple[BridgeIssue, ...] = ()
    generation: int = 0
    cached: bool = False
    error: str = ""
    tool_loading: str = "search"
    tool_loading_source: str = "default"
    call_timeout_s: float = 60
    instructions: str = ""

    @property
    def connected(self) -> bool:
        return self.health.connected

    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "health": self.health.value,
            "version": self.version,
            "tools": list(self.tool_names()),
            "resources": [item.to_dict() for item in self.resources],
            "resource_templates": [
                item.to_dict() for item in self.resource_templates
            ],
            "prompts": [item.to_dict() for item in self.prompts],
            "issues": [issue.to_dict() for issue in self.issues],
            "generation": self.generation,
            "cached": self.cached,
            "error": self.error,
        }


@dataclass(frozen=True)
class MCPSnapshot:
    """The aggregate, immutable view of every configured MCP server."""

    generation: int = 0
    servers: tuple[MCPServerSnapshot, ...] = ()
    tools: tuple[Any, ...] = ()
    resources: tuple[ResourceDescriptor, ...] = ()
    resource_templates: tuple[ResourceDescriptor, ...] = ()
    prompts: tuple[PromptDescriptor, ...] = ()

    def server(self, name: str) -> MCPServerSnapshot | None:
        for item in self.servers:
            if item.name == name:
                return item
        return None

    def tool(self, name: str) -> Any | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.tools)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "tools": list(self.tool_names()),
            "servers": [item.to_dict() for item in self.servers],
        }


@dataclass(frozen=True)
class MCPServerStatus:
    """A point-in-time status row (for ``doctor`` and tests)."""

    name: str
    health: MCPHealth
    enabled: bool
    connected: bool
    version: str = ""
    tool_count: int = 0
    resource_count: int = 0
    prompt_count: int = 0
    attempts: int = 0
    last_error: str = ""
    retry_in_s: float = 0.0
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "health": self.health.value,
            "enabled": self.enabled,
            "connected": self.connected,
            "version": self.version,
            "tool_count": self.tool_count,
            "resource_count": self.resource_count,
            "prompt_count": self.prompt_count,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "retry_in_s": self.retry_in_s,
            "cached": self.cached,
        }


# ---------------------------------------------------------------------------
# Descriptor cache
# ---------------------------------------------------------------------------


class _DescriptorCache:
    """Atomic, corruption-tolerant JSON cache for discovered descriptor lists."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def make_key(self, name: str, version: str, fingerprint: str) -> str:
        digest = hashlib.sha256(
            f"{version}\x00{fingerprint}".encode()
        ).hexdigest()[:16]
        safe = _SAFE_NAME_RE.sub("_", name)[:48] or "server"
        return f"{safe}-{digest}"

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        """Return a valid document, or ``None`` on miss/corruption (deleting it)."""
        path = self.path_for(key)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self._unlink(path)
            return None
        if not isinstance(document, dict) or document.get("format") != _CACHE_FORMAT:
            return None
        for field_name in ("tools", "resources", "resource_templates", "prompts"):
            if not isinstance(document.get(field_name), list):
                self._unlink(path)
                return None
        return document

    def store(self, key: str, document: Mapping[str, Any]) -> None:
        """Atomically write a cache document; a cache failure is never fatal."""
        payload = {"format": _CACHE_FORMAT, **dict(document)}
        try:
            data = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self.root.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=self.root, prefix=".tmp-", suffix=".json"
            )
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path_for(key))
            except OSError:
                self._unlink(Path(temporary))
        except OSError:
            return

    def invalidate(self, key: str) -> None:
        self._unlink(self.path_for(key))

    def clear(self) -> None:
        try:
            entries = list(self.root.glob("*.json"))
        except OSError:
            return
        for entry in entries:
            self._unlink(entry)

    @staticmethod
    def _unlink(path: Path) -> None:
        with contextlib.suppress(OSError):
            path.unlink()


# ---------------------------------------------------------------------------
# Internal mutable state
# ---------------------------------------------------------------------------


@dataclass
class _ServerState:
    definition: ServerDefinition
    client: Any | None = None
    info: Any | None = None
    health: MCPHealth = MCPHealth.UNKNOWN
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""
    cache_key: str = ""
    cached: bool = False
    dirty: bool = False
    tools: tuple[Any, ...] = ()
    resources: tuple[ResourceDescriptor, ...] = ()
    resource_templates: tuple[ResourceDescriptor, ...] = ()
    prompts: tuple[PromptDescriptor, ...] = ()
    issues: tuple[BridgeIssue, ...] = ()
    generation: int = 0
    was_connected: bool = False
    #: Bumped by ``disconnect``/``aclose`` so an in-flight connect can detect
    #: that the server was closed while its handshake was running and close the
    #: newly connected client instead of leaking it.
    epoch: int = 0
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refresh_task: asyncio.Future | None = None
    notify_task: asyncio.Task | None = None
    retry_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def config(self) -> MCPServerConfig:
        return self.definition.config


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class MCPManager:
    """Owns configured MCP servers, their health, and their snapshot."""

    def __init__(
        self,
        definitions: Mapping[str, Any] | Iterable[Any] | None = None,
        *,
        enabled: bool = True,
        cache_dir: str | os.PathLike[str] | None = None,
        workspace: str | os.PathLike[str] | None = None,
        home: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        client_factory: Callable[[MCPServerConfig], Any] | None = None,
        caps: BridgeCaps = DEFAULT_CAPS,
        sink: Any | None = None,
        restart_max: int = 5,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 30.0,
        circuit_cooldown_s: float = 60.0,
    ) -> None:
        if not isinstance(enabled, bool):
            raise MCPConfigError("enabled must be a bool")
        for label, value in (
            ("restart_max", restart_max),
            ("backoff_base_s", backoff_base_s),
            ("backoff_max_s", backoff_max_s),
            ("circuit_cooldown_s", circuit_cooldown_s),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MCPConfigError(f"{label} must be a number")
            if label == "restart_max":
                if int(value) < 1:
                    raise MCPConfigError("restart_max must be a positive integer")
            elif float(value) <= 0:
                raise MCPConfigError(f"{label} must be positive")

        self._enabled = enabled
        self._environ = environ
        self._factory = client_factory or self._default_client_factory
        self._caps = caps
        self._sink = sink
        self._restart_max = int(restart_max)
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_max_s = float(backoff_max_s)
        self._circuit_cooldown_s = float(circuit_cooldown_s)
        self._workspace = Path(workspace) if workspace is not None else None
        self._home = home

        self._cache: _DescriptorCache | None = None
        if cache_dir is not None:
            self._cache = _DescriptorCache(cache_dir)

        self._definitions: dict[str, ServerDefinition] = {}
        self._states: dict[str, _ServerState] = {}
        self._snapshots: dict[str, MCPServerSnapshot] = {}
        self._failures: tuple[ApplyFailure, ...] = ()
        self._generation = 0
        self._snapshot = MCPSnapshot(generation=0)
        self._secrets: tuple[str, ...] = ()
        self._closed = False
        self._state_lock = asyncio.Lock()

        parsed, failures = self._parse(definitions)
        self._definitions = parsed
        self._states = {
            name: _ServerState(definition=definition)
            for name, definition in parsed.items()
        }
        self._failures = failures
        self._secrets = self._collect_secrets()
        self._sync_snapshots()

    # -- construction helpers ---------------------------------------------

    def _default_client_factory(self, config: MCPServerConfig) -> Any:
        from .client import FileStderrSink, MCPClient

        sink: Any | None = None
        if self._workspace is not None:
            safe = _SAFE_NAME_RE.sub("_", config.name)[:48] or "server"
            log_path = (
                project_state_dir(self._workspace, self._home)
                / _MCP_LOG_SUBDIR
                / f"{safe}.log"
            )
            try:
                sink = FileStderrSink(log_path, max_bytes=_MCP_STDERR_MAX_BYTES)
            except ValueError:  # pragma: no cover - constant is positive
                sink = None
        return MCPClient(config, stderr_sink=sink)

    def _parse(
        self, definitions: Any
    ) -> tuple[dict[str, ServerDefinition], tuple[ApplyFailure, ...]]:
        parsed: dict[str, ServerDefinition] = {}
        failures: list[ApplyFailure] = []
        if definitions is None:
            return parsed, ()
        if isinstance(definitions, Mapping):
            items: list[tuple[Any, Any]] = list(definitions.items())
        elif isinstance(definitions, (str, bytes, bytearray)):
            raise MCPConfigError("definitions must be a mapping or an iterable")
        else:
            items = []
            for item in definitions:
                if isinstance(item, (ServerDefinition, MCPServerConfig)):
                    items.append((item.name, item))
                elif isinstance(item, tuple) and len(item) == 2:
                    items.append((item[0], item[1]))
                else:
                    raise MCPConfigError(
                        "each definition must be a ServerDefinition, an "
                        "MCPServerConfig, or a (name, raw) pair"
                    )
        for name, raw in items:
            try:
                definition = self._coerce_definition(name, raw)
                if self._workspace is not None and definition.config.transport == "stdio":
                    cwd = Path(definition.config.cwd) if definition.config.cwd else Path(".")
                    if not cwd.is_absolute():
                        cwd = self._workspace.resolve() / cwd
                    definition = replace(
                        definition, config=replace(definition.config, cwd=str(cwd))
                    )
            except Exception as exc:  # noqa: BLE001 - one bad def is not fatal
                failures.append(
                    ApplyFailure(
                        name=self._safe_name(name),
                        error=self._scrub(str(exc)),
                        error_type=type(exc).__name__,
                    )
                )
                continue
            if definition.name in parsed:
                failures.append(
                    ApplyFailure(
                        name=definition.name,
                        error="duplicate server name",
                        error_type="MCPConfigError",
                    )
                )
                continue
            parsed[definition.name] = definition
        return parsed, tuple(failures)

    def _coerce_definition(self, name: Any, raw: Any) -> ServerDefinition:
        if not isinstance(name, str) or not name.strip():
            raise MCPConfigError("server name must be a non-empty string")
        if isinstance(raw, ServerDefinition):
            if raw.name != name:
                raise MCPConfigError(
                    f"definition name {raw.name!r} does not match key {name!r}"
                )
            return raw
        if isinstance(raw, MCPServerConfig):
            return ServerDefinition(name=name, config=raw)
        if isinstance(raw, Mapping):
            enabled = server_enabled(name, raw)
            config = parse_server_config(
                name, raw, environ=self._environ, workspace=self._workspace, home=self._home
            )
            return ServerDefinition(name=name, config=config, enabled=enabled)
        raise MCPConfigError(
            f"server {name!r} must be a config mapping or an MCPServerConfig"
        )

    @staticmethod
    def _safe_name(value: Any) -> str:
        text = value if isinstance(value, str) else str(value)
        return _SAFE_NAME_RE.sub("_", text)[:64] or "server"

    # -- introspection -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def definitions(self) -> dict[str, ServerDefinition]:
        return dict(self._definitions)

    @property
    def failures(self) -> tuple[ApplyFailure, ...]:
        """Definitions the last apply could not parse (shown as invalid rows)."""
        return self._failures

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def definition(self, name: str) -> ServerDefinition:
        return self._require_state(name).definition

    def snapshot(self) -> MCPSnapshot:
        return self._snapshot

    def tools(self) -> tuple[Any, ...]:
        return self._snapshot.tools

    def resources(self) -> tuple[ResourceDescriptor, ...]:
        return self._snapshot.resources

    def prompts(self) -> tuple[PromptDescriptor, ...]:
        return self._snapshot.prompts

    def read_resource_tool(self) -> Any:
        """The global ``ReadMcpResource`` tool, bound to live clients.

        The resolver is synchronous (the bridge's contract), so it returns a
        server's *current* live client or ``None``; it never dials and never
        caches. A cached client would go stale the moment a server reconnects
        (the previous client is closed and replaced), so the lookup is done
        fresh on every call. A resource read therefore reports "unknown or
        offline" for a server that has not been lazily connected yet, which is
        the correct failure-isolated answer.
        """

        def resolve(server: Any) -> Any:
            if not isinstance(server, str):
                return None
            state = self._states.get(server)
            return state.client if state is not None else None

        return build_read_resource_tool(resolve)

    def server_snapshot(self, name: str) -> MCPServerSnapshot | None:
        return self._snapshots.get(name)

    def server_detail(self, name: str) -> dict[str, Any] | None:
        """Read existing state only; never initialize, list, or connect a server.

        This is internal data. The host must bound and redact it before display.
        Instructions and metadata come from the retained initialize result.
        """
        state = self._states.get(name)
        snapshot = self.server_snapshot(name)
        if state is None or snapshot is None:
            return None
        config = state.definition.config
        label = ""
        if config.transport == "stdio":
            # Never disclose arguments, working directories, env, headers or URLs.
            label = f"{config.command.replace(chr(92), '/').rsplit('/', 1)[-1]} ({len(config.args)} args)"
        info = state.info
        return {
            "name": name,
            "transport": config.transport,
            "command_label": label,
            "status": "connected" if snapshot.connected else "disabled" if not self._is_enabled(state) else "failed" if snapshot.error else "disconnected",
            "health": snapshot.health.value,
            "tool_loading": snapshot.tool_loading,
            "tool_loading_source": snapshot.tool_loading_source,
            "session_availability": "unavailable: session sent-tool state is not retained here",
            "enabled": self._is_enabled(state),
            "error": snapshot.error,
            "server_info": {"name": info.name, "version": info.version} if info is not None else None,
            "server_info_availability": "stored" if info is not None else "unavailable: no initialize result",
            "instructions": info.instructions if info is not None else None,
            "instructions_availability": "stored" if info is not None else "unavailable: no initialize result",
            "tools": [{"name": tool.name, "description": tool.spec.description,
                       "input_schema": tool.spec.to_schema().input_schema, "annotations": None,
                       "annotations_availability": "unavailable: bridge does not retain raw annotations",
                       "sent": None, "tokens": len(json.dumps(tool.spec.to_schema().input_schema)) // 4}
                      for tool in snapshot.tools],
            "resources": [asdict(item) for item in snapshot.resources[:256]],
            "resources_clipped": len(snapshot.resources) > 256,
            "resource_templates": [asdict(item) for item in snapshot.resource_templates[:256]],
            "resource_templates_clipped": len(snapshot.resource_templates) > 256,
            "prompts": [asdict(item) for item in snapshot.prompts[:256]],
            "prompts_clipped": len(snapshot.prompts) > 256,
            "catalog_availability": "stored" if snapshot.connected else "unavailable: server is not connected",
        }

    def redact_display(self, text: str) -> str:
        """Scrub display data with credentials from all configured servers."""
        return self._scrub(text)

    def status(self, name: str) -> MCPServerStatus:
        state = self._require_state(name)
        now = time.monotonic()
        retry_in = 0.0
        if state.health.retryable:
            retry_in = max(0.0, state.next_attempt_at - now)
        return MCPServerStatus(
            name=state.name,
            health=state.health,
            enabled=self._is_enabled(state),
            connected=state.client is not None and state.health.connected,
            version=self._version(state),
            tool_count=len(state.tools),
            resource_count=len(state.resources),
            prompt_count=len(state.prompts),
            attempts=state.attempts,
            last_error=state.last_error,
            retry_in_s=retry_in,
            cached=state.cached,
        )

    def statuses(self) -> tuple[MCPServerStatus, ...]:
        return tuple(self.status(name) for name in self.server_names)

    def diagnostics(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = [failure.to_dict() for failure in self._failures]
        for name in self.server_names:
            status = self.status(name)
            if status.last_error:
                rows.append(
                    {
                        "name": name,
                        "kind": "server",
                        "health": status.health.value,
                        "error": status.last_error,
                        "error_type": "MCPError",
                    }
                )
            snapshot = self._snapshots.get(name)
            if snapshot is None:
                continue
            for issue in snapshot.issues:
                if issue.code == "cross_server_collision":
                    rows.append(
                        {
                            "name": name,
                            "kind": "collision",
                            "code": issue.code,
                            "error": self._scrub(issue.detail),
                            "error_type": "MCPError",
                        }
                    )
        return tuple(rows)

    def backoff_delay(self, attempts: int) -> float:
        """Exponential (capped) delay before restart attempt ``attempts``.

        The exponent is bounded before evaluation: ``2 ** (attempts - 1)`` for a
        large ``attempts`` would allocate an enormous integer (or overflow a
        float), so the growth is clipped at the point the delay has already
        saturated at ``backoff_max_s``.
        """
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            raise MCPConfigError("attempts must be an integer")
        if attempts <= 0:
            return 0.0
        exponent = min(attempts - 1, 63)
        delay = self._backoff_base_s * (2 ** exponent)
        return min(self._backoff_max_s, delay)

    def cache_key(self, name: str) -> str:
        return self._require_state(name).cache_key

    def cache_path(self, name: str) -> Path | None:
        if self._cache is None:
            return None
        key = self._require_state(name).cache_key
        return self._cache.path_for(key) if key else None

    def clear_cache(self) -> None:
        if self._cache is not None:
            self._cache.clear()

    # -- health / connect --------------------------------------------------

    async def ensure_connected(self, name: str) -> Any | None:
        """Return a live client for ``name``, connecting lazily if needed.

        Never raises for a connection failure: a dead server records its health,
        emits ``mcp.failed``, withdraws its tools, and returns ``None`` so the
        caller degrades instead of failing a turn. A non-fatal error (an unknown
        name) does raise, because that is a programming error.
        """
        state = self._require_state(name)
        if state.client is not None and state.health.connected:
            return state.client
        client = await self._maybe_connect(state)
        return client

    async def connect(self, name: str) -> Any | None:
        """Explicitly connect ``name``; same isolation contract as lazy use."""
        return await self.ensure_connected(name)

    async def disconnect(self, name: str, *, reason: str = "explicit") -> bool:
        """Close one server's connection, keeping its definition and health.

        Returns whether a live client was closed. The server's tools are
        withdrawn and a ``mcp.disconnected`` event is emitted.
        """
        state = self._require_state(name)
        was_connected = state.client is not None
        state.epoch += 1
        self._cancel_retry(state)
        await self._close_client(state)
        if state.health.connected or was_connected:
            state.health = MCPHealth.UNKNOWN
        state.tools = ()
        state.resources = ()
        state.resource_templates = ()
        state.prompts = ()
        state.issues = ()
        state.cached = False
        changed = self._publish_state(state)
        if was_connected:
            await self._emit(
                "mcp.disconnected", {"server": name, "reason": reason}
            )
        if changed:
            await self._emit_tools_changed(name)
        return was_connected

    async def _maybe_connect(self, state: _ServerState) -> Any | None:
        if self._closed:
            return None
        if not self._is_enabled(state):
            state.health = MCPHealth.DISABLED
            return None
        now = time.monotonic()
        if state.health.retryable and now < state.next_attempt_at:
            return None
        async with state.connect_lock:
            if self._closed:
                return None
            if state.client is not None and state.health.connected:
                return state.client
            now = time.monotonic()
            if state.health.retryable and now < state.next_attempt_at:
                return None
            await self._connect_locked(state)
            return state.client if state.health.connected else None

    async def _connect_locked(self, state: _ServerState) -> None:
        state.health = MCPHealth.CONNECTING
        epoch = state.epoch
        try:
            client = self._factory(state.config)
        except Exception as exc:  # noqa: BLE001 - factory failure is isolated
            await self._record_failure(state, exc)
            return
        try:
            info = await client.connect()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await client.aclose()
            state.health = MCPHealth.UNKNOWN
            raise
        except BaseException as exc:  # noqa: BLE001 - normalize every failure
            await self._record_failure(state, exc, client=client)
            return

        # The handshake may have outlived a disconnect/reconfigure/close. If so,
        # the client we just opened is already orphaned: close it rather than
        # publishing it as connected, so a newly connected client is never
        # leaked behind a manager that has moved on.
        if (
            self._closed
            or state.epoch != epoch
            or self._states.get(state.name) is not state
        ):
            with contextlib.suppress(Exception):
                await client.aclose()
            state.health = MCPHealth.UNKNOWN
            return

        state.client = client
        state.info = info
        state.attempts = 0
        state.last_error = ""
        state.was_connected = True
        self._cancel_retry(state)
        state.cache_key = self._compute_cache_key(state)
        snapshot = await self._load_snapshot(state)
        if snapshot is None or state.client is not client:
            # ``_load_snapshot`` already recorded the failure and closed us.
            return
        if self._states.get(state.name) is not state:
            # A concurrent ``apply`` replaced this server; drop the orphan.
            await self._close_client(state)
            return
        self._snapshots[state.name] = snapshot
        self._start_notifications(state)
        changed = self._refresh_aggregate()
        await self._emit(
            "mcp.connected",
            {
                "server": state.name,
                "version": self._version(state),
                "tools": len(state.tools),
                "cached": state.cached,
                "generation": self._generation,
            },
        )
        if changed:
            await self._emit_tools_changed(state.name)

    async def _load_snapshot(
        self, state: _ServerState
    ) -> MCPServerSnapshot | None:
        """Populate ``state``'s descriptors and return its snapshot.

        On a fatal transport failure the client is closed, the failure recorded,
        and ``None`` returned. A per-list non-fatal error degrades that list to
        empty (recorded as an issue) without failing the server.
        """
        version = self._version(state)
        state.cache_key = state.cache_key or self._compute_cache_key(state)
        cached_document: dict[str, Any] | None = None
        if self._cache is not None and not state.dirty:
            cached_document = self._cache.load(state.cache_key)

        issues: list[BridgeIssue] = []
        if cached_document is not None:
            tools_raw = cached_document["tools"]
            resources_raw = cached_document["resources"]
            templates_raw = cached_document["resource_templates"]
            prompts_raw = cached_document["prompts"]
            # List-level issues (a non-fatal listing failure) are cached with the
            # descriptors, so a cache hit reports the same DEGRADED health the
            # live listing did instead of looking healthy. Descriptor-level
            # issues are recomputed below from the cached raw descriptors.
            cached_issues = cached_document.get("issues", [])
            if isinstance(cached_issues, list):
                issues.extend(
                    _issue_from_dict(item)
                    for item in cached_issues
                    if isinstance(item, Mapping)
                )
            state.cached = True
            state.dirty = False
        else:
            try:
                tools_raw, issue = await self._list_kind(state, "list_tools")
                issues.extend(item for item in (issue,) if item is not None)
                resources_raw, issue = await self._list_kind(state, "list_resources")
                issues.extend(item for item in (issue,) if item is not None)
                templates_raw, issue = await self._list_kind(
                    state, "list_resource_templates"
                )
                issues.extend(item for item in (issue,) if item is not None)
                prompts_raw, issue = await self._list_kind(state, "list_prompts")
                issues.extend(item for item in (issue,) if item is not None)
            except MCPError as exc:
                if _is_fatal(exc):
                    await self._record_failure(state, exc)
                    return None
                issues.append(
                    _issue(state.name, "list", "list", type(exc).__name__)
                )
                tools_raw, resources_raw, templates_raw, prompts_raw = [], [], [], []
            state.cached = False
            state.dirty = False
            if self._cache is not None:
                self._cache.store(
                    state.cache_key,
                    {
                        "version": version,
                        "tools": tools_raw,
                        "resources": resources_raw,
                        "resource_templates": templates_raw,
                        "prompts": prompts_raw,
                        "issues": [item.to_dict() for item in issues],
                    },
                )

        config = state.definition.config
        if config.include_tools or config.exclude_tools:
            tools_raw = [tool for tool in tools_raw
                         if not isinstance(tool, Mapping) or config.allows_tool(str(tool.get("name", "")))]
        call_tool = self._make_call_tool(state)
        tools, tool_issues = build_tools(
            state.name,
            tools_raw,
            call_tool,
            caps=self._caps,
            version=version or "1",
            generation=self._generation,
            source=f"mcp:{state.name}",
        )
        resources, resource_issues = build_resource_descriptors(
            state.name, resources_raw, caps=self._caps
        )
        templates, template_issues = build_resource_descriptors(
            state.name, templates_raw, caps=self._caps, template=True
        )
        prompts, prompt_issues = build_prompt_descriptors(
            state.name, prompts_raw, caps=self._caps
        )
        state.tools = tools
        state.resources = resources
        state.resource_templates = templates
        state.prompts = prompts
        state.issues = (
            *issues,
            *tool_issues,
            *resource_issues,
            *template_issues,
            *prompt_issues,
        )
        state.generation = self._generation
        state.health = MCPHealth.DEGRADED if state.issues else MCPHealth.READY
        return self._snapshot_for(state)

    async def _list_kind(
        self, state: _ServerState, method: str
    ) -> tuple[list[dict[str, Any]], BridgeIssue | None]:
        client = state.client
        caller = getattr(client, method, None)
        if not callable(caller):
            return [], None
        try:
            value = caller()
            if inspect.isawaitable(value):
                value = await value
        except MCPRemoteError as exc:
            if exc.code == -32601:
                # Method not found: the server simply does not offer this
                # optional listing (for example resource templates). That is not
                # a degradation.
                return [], None
            if _is_fatal(exc):
                raise
            return [], _issue(state.name, "list", method, type(exc).__name__)
        except MCPUnavailable:
            # A backend explicitly reports the listing as unsupported.
            return [], None
        except MCPError as exc:
            if _is_fatal(exc):
                raise
            return [], _issue(state.name, "list", method, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - a listing fault degrades
            return [], _issue(state.name, "list", method, type(exc).__name__)
        return [self._descriptor_dict(item) for item in value or ()], None

    @classmethod
    def _descriptor_dict(cls, value: Any) -> Any:
        """Recursively normalize a descriptor (struct, mapping, or scalar)."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {key: cls._descriptor_dict(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._descriptor_dict(item) for item in value]
        fields = getattr(value, "__struct_fields__", None)
        if isinstance(fields, tuple):
            return {
                name: cls._descriptor_dict(getattr(value, name, None))
                for name in fields
            }
        attrs = getattr(value, "__dict__", None)
        if isinstance(attrs, dict):
            return {
                key: cls._descriptor_dict(item)
                for key, item in attrs.items()
                if not key.startswith("_")
            }
        return value

    def _make_call_tool(self, state: _ServerState) -> Callable[..., Awaitable[Any]]:
        async def call_tool(name: str, arguments: Mapping[str, Any]) -> Any:
            client = await self.ensure_connected(state.name)
            if client is None:
                raise MCPUnavailable(
                    f"MCP server {state.name!r} is {state.health.value}"
                )
            try:
                return await client.call_tool(name, dict(arguments))
            except MCPError as exc:
                if _is_fatal(exc):
                    await self._record_failure(state, exc)
                raise

        return call_tool

    async def call_tool(
        self, server: str, tool: str, arguments: Mapping[str, Any] | None = None
    ) -> Any:
        """Call a raw server tool by name, connecting lazily if needed."""
        state = self._require_state(server)
        client = await self.ensure_connected(server)
        if client is None:
            raise MCPUnavailable(f"MCP server {server!r} is {state.health.value}")
        return await client.call_tool(tool, dict(arguments or {}))

    # -- refresh / notifications ------------------------------------------

    async def refresh(self, name: str, *, force: bool = False) -> MCPServerSnapshot:
        """Re-list one server, coalescing concurrent callers.

        With ``force`` the cache/version short-circuit is bypassed. A failure is
        isolated exactly like a lazy connect.
        """
        state = self._require_state(name)
        if force:
            state.dirty = True
        if state.client is None or not state.health.connected:
            await self.ensure_connected(name)
        if state.client is None or not state.health.connected:
            return self._snapshots.get(name) or self._snapshot_for(state)
        await self._run_refresh(state)
        return self._snapshots.get(name) or self._snapshot_for(state)

    async def invalidate(self, name: str) -> None:
        """Mark a server's listings stale and clear its cache entry."""
        state = self._require_state(name)
        state.dirty = True
        if self._cache is not None and state.cache_key:
            self._cache.invalidate(state.cache_key)

    async def _run_refresh(self, state: _ServerState) -> None:
        task = state.refresh_task
        if task is not None and not task.done():
            await asyncio.shield(task)
            return
        task = asyncio.ensure_future(self._do_refresh(state))
        state.refresh_task = task
        try:
            await asyncio.shield(task)
        finally:
            if state.refresh_task is task:
                state.refresh_task = None

    async def _do_refresh(self, state: _ServerState) -> None:
        if self._closed or state.client is None:
            return
        snapshot = await self._load_snapshot(state)
        if self._states.get(state.name) is not state:
            return
        if snapshot is not None:
            self._snapshots[state.name] = snapshot
        changed = self._refresh_aggregate()
        if changed:
            await self._emit_tools_changed(state.name)

    def _start_notifications(self, state: _ServerState) -> None:
        if state.notify_task is not None and not state.notify_task.done():
            return
        state.notify_task = asyncio.ensure_future(self._watch(state))

    async def _watch(self, state: _ServerState) -> None:
        client = state.client
        try:
            async for notification in client.notifications():
                method = getattr(notification, "method", "")
                if method in LIST_CHANGED_METHODS:
                    await self._on_list_changed(state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a watcher fault never fails a turn
            return

    async def _on_list_changed(self, state: _ServerState) -> None:
        if self._closed or state.client is None:
            return
        state.dirty = True
        if self._cache is not None and state.cache_key:
            self._cache.invalidate(state.cache_key)
        await self._run_refresh(state)

    # -- failure handling --------------------------------------------------

    async def _record_failure(
        self, state: _ServerState, exc: BaseException, *, client: Any | None = None
    ) -> None:
        state.attempts += 1
        state.last_error = self._scrub(_error_text(exc))
        circuit_open = state.attempts >= self._restart_max
        delay = (
            self._circuit_cooldown_s
            if circuit_open
            else self.backoff_delay(state.attempts)
        )
        state.next_attempt_at = time.monotonic() + delay
        state.health = MCPHealth.FAILED if circuit_open else MCPHealth.BACKOFF
        await self._close_client(state)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        state.tools = ()
        state.resources = ()
        state.resource_templates = ()
        state.prompts = ()
        state.issues = ()
        state.cached = False
        state.dirty = True
        changed = self._publish_state(state)
        await self._emit(
            "mcp.failed",
            {
                "server": state.name,
                "error": state.last_error,
                "error_type": type(exc).__name__,
                "attempts": state.attempts,
                "health": state.health.value,
                "retry_in_s": round(delay, 6),
            },
        )
        if changed:
            await self._emit_tools_changed(state.name)
        self._schedule_restart(state)

    # -- restart -----------------------------------------------------------

    def _cancel_retry(self, state: _ServerState) -> None:
        task = state.retry_task
        if task is None:
            return
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
        state.retry_task = None

    def _schedule_restart(self, state: _ServerState) -> None:
        """Schedule background restart for a server that was live and died.

        An initial connect failure is *not* auto-retried: lazy use is the
        contract and there is nothing to restore. A previously-connected server
        that failed is retried after its backoff (or circuit cooldown) so its
        tools return without any caller having to ask.
        """
        if not state.was_connected or not self._is_enabled(state):
            return
        existing = state.retry_task
        if existing is not None and not existing.done():
            return
        state.retry_task = asyncio.ensure_future(self._restart_loop(state))

    async def _restart_loop(self, state: _ServerState) -> None:
        try:
            while (
                not self._closed
                and self._is_enabled(state)
                and self._states.get(state.name) is state
            ):
                if state.client is not None and state.health.connected:
                    return
                if not state.health.retryable:
                    return
                delay = max(0.0, state.next_attempt_at - time.monotonic())
                await asyncio.sleep(delay)
                if (
                    self._closed
                    or not self._is_enabled(state)
                    or self._states.get(state.name) is not state
                ):
                    return
                await self._maybe_connect(state)
                if state.client is not None and state.health.connected:
                    return
        finally:
            if state.retry_task is asyncio.current_task():
                state.retry_task = None

    async def _close_client(self, state: _ServerState) -> None:
        task, state.notify_task = state.notify_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        client, state.client = state.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    # -- hot apply ---------------------------------------------------------

    async def apply(
        self, definitions: Mapping[str, Any] | Iterable[Any] | None
    ) -> ApplyReport:
        """Reconcile the whole definition set, hot.

        Added servers are parked at ``unknown`` until first use. Removed and
        reconfigured servers are closed (emitting ``mcp.disconnected`` when they
        were live) and their tools withdrawn. The snapshot generation advances
        only when the aggregate actually changed.
        """
        if self._closed:
            raise MCPClosed("MCP manager is closed")
        parsed, failures = self._parse(definitions)
        async with self._state_lock:
            if self._closed:
                raise MCPClosed("MCP manager is closed")
            old = self._definitions
            added = tuple(sorted(set(parsed) - set(old)))
            removed = tuple(sorted(set(old) - set(parsed)))
            reconfigured = tuple(
                sorted(
                    name
                    for name in set(old) & set(parsed)
                    if old[name].fingerprint() != parsed[name].fingerprint()
                )
            )
            unchanged = tuple(
                sorted(
                    name
                    for name in set(old) & set(parsed)
                    if old[name].fingerprint() == parsed[name].fingerprint()
                )
            )

            for name in removed:
                state = self._states.pop(name, None)
                self._snapshots.pop(name, None)
                if state is not None and state.client is not None:
                    self._cancel_retry(state)
                    await self._close_client(state)
                    await self._emit(
                        "mcp.disconnected", {"server": name, "reason": "removed"}
                    )
                elif state is not None:
                    self._cancel_retry(state)
                    await self._close_client(state)

            for name in reconfigured:
                state = self._states.get(name)
                if state is not None and state.client is not None:
                    self._cancel_retry(state)
                    await self._close_client(state)
                    await self._emit(
                        "mcp.disconnected",
                        {"server": name, "reason": "reconfigured"},
                    )
                elif state is not None:
                    self._cancel_retry(state)
                self._states[name] = _ServerState(definition=parsed[name])
                self._snapshots[name] = self._empty_snapshot(self._states[name])

            for name in added:
                self._states[name] = _ServerState(definition=parsed[name])
                self._snapshots[name] = self._empty_snapshot(self._states[name])

            self._definitions = parsed
            self._failures = failures
            self._secrets = self._collect_secrets()
            changed = self._refresh_aggregate()
            report = ApplyReport(
                generation=self._generation,
                added=added,
                removed=removed,
                reconfigured=reconfigured,
                unchanged=unchanged,
                failures=failures,
            )
        if changed:
            await self._emit_tools_changed("*")
        return report

    # -- shutdown ----------------------------------------------------------

    async def aclose(self) -> None:
        """Close every server, stop watchers, and empty the snapshot. Idempotent."""
        if self._closed:
            return
        self._closed = True
        disconnected: list[str] = []
        retry_tasks: list[asyncio.Task] = []
        async with self._state_lock:
            for name, state in list(self._states.items()):
                if state.client is not None:
                    disconnected.append(name)
                if state.retry_task is not None and not state.retry_task.done():
                    retry_tasks.append(state.retry_task)
                state.epoch += 1
                self._cancel_retry(state)
                await self._close_client(state)
                state.health = MCPHealth.CLOSED
                state.tools = ()
                state.resources = ()
                state.resource_templates = ()
                state.prompts = ()
                state.issues = ()
                state.cached = False
            self._snapshots = {
                name: self._empty_snapshot(state, health=MCPHealth.CLOSED)
                for name, state in self._states.items()
            }
            self._refresh_aggregate()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        for name in disconnected:
            await self._emit(
                "mcp.disconnected", {"server": name, "reason": "closed"}
            )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.aclose()
        return False

    # -- snapshot publication ---------------------------------------------

    def _snapshot_for(self, state: _ServerState) -> MCPServerSnapshot:
        return MCPServerSnapshot(
            name=state.name,
            tool_loading=state.config.tool_loading,
            tool_loading_source=state.config.tool_loading_source,
            call_timeout_s=state.config.call_timeout_s,
            health=state.health,
            version=self._version(state),
            instructions=sanitize_controls(str(getattr(state.info, "instructions", "")))[:2000],
            tools=state.tools,            resources=state.resources,
            resource_templates=state.resource_templates,
            prompts=state.prompts,
            issues=state.issues,
            generation=state.generation,
            cached=state.cached,
            error=state.last_error,
        )

    def _empty_snapshot(
        self, state: _ServerState, *, health: MCPHealth | None = None
    ) -> MCPServerSnapshot:
        version = self._version(state)
        return MCPServerSnapshot(
            name=state.name,
            tool_loading=state.config.tool_loading,
            tool_loading_source=state.config.tool_loading_source,
            call_timeout_s=state.config.call_timeout_s,
            health=state.health if health is None else health,
            version=version,
            generation=state.generation,
            error=state.last_error,
        )

    def _publish_state(self, state: _ServerState) -> bool:
        """Publish one server's state and return whether the tool set changed."""
        if self._states.get(state.name) is state:
            self._snapshots[state.name] = self._snapshot_for(state)
        return self._refresh_aggregate()

    def _refresh_aggregate(self) -> bool:
        """Rebuild the aggregate snapshot; return whether tool names changed.

        Two servers can normalize to the same qualified tool name (for example
        ``a-b`` and ``a.b`` both become ``a_b``); the bridge only detects
        collisions *within* one server, so the aggregate resolves cross-server
        collisions deterministically by server order, keeps the first, records a
        ``cross_server_collision`` issue on the loser, and never publishes a
        duplicate name.

        Every published ``RegisteredTool`` is stamped with the aggregate
        generation it is part of, so a tool's provenance matches its snapshot
        instead of trailing the previous generation.
        """
        previous_names = self._snapshot.tool_names()
        owners: dict[str, str] = {}
        bases: list[tuple[str, MCPServerSnapshot]] = []
        for name in sorted(self._states):
            base = self._snapshot_for(self._states[name])
            kept: list[Any] = []
            collisions: list[BridgeIssue] = []
            for tool in base.tools:
                owner = owners.get(tool.name)
                if owner is not None:
                    collisions.append(
                        BridgeIssue(
                            server=name,
                            kind="tool",
                            name=tool.name,
                            code="cross_server_collision",
                            detail=(
                                f"tool {tool.name!r} collides with a tool from "
                                f"server {owner!r}; that server's tool is used"
                            ),
                        )
                    )
                    continue
                owners[tool.name] = name
                kept.append(tool)
            if collisions:
                health = base.health
                if health is MCPHealth.READY:
                    health = MCPHealth.DEGRADED
                base = replace(
                    base,
                    tools=tuple(kept),
                    issues=base.issues + tuple(collisions),
                    health=health,
                )
            bases.append((name, base))

        names_after = tuple(
            tool.name for _name, base in bases for tool in base.tools
        )
        changed = names_after != previous_names
        if changed:
            self._generation += 1

        servers: list[MCPServerSnapshot] = []
        tools: list[Any] = []
        resources: list[ResourceDescriptor] = []
        templates: list[ResourceDescriptor] = []
        prompts: list[PromptDescriptor] = []
        for name, base in bases:
            state = self._states.get(name)
            stamped_tools = tuple(
                tool
                if tool.generation == self._generation
                else replace(tool, generation=self._generation)
                for tool in base.tools
            )
            if state is not None:
                # Persist the generation stamp on the server's full tool set so
                # the next rebuild reuses the same objects; collision filtering
                # is a view applied to the snapshot, never a destructive edit, so
                # a tool reappears if the server that shadowed it goes away.
                full_tools = tuple(
                    tool
                    if tool.generation == self._generation
                    else replace(tool, generation=self._generation)
                    for tool in state.tools
                )
                state.tools = full_tools
                state.generation = self._generation
            if base.generation != self._generation or stamped_tools != base.tools:
                base = replace(
                    base, generation=self._generation, tools=stamped_tools
                )
            servers.append(base)
            tools.extend(base.tools)
            resources.extend(base.resources)
            templates.extend(base.resource_templates)
            prompts.extend(base.prompts)
        self._snapshots = {snapshot.name: snapshot for snapshot in servers}
        self._snapshot = MCPSnapshot(
            generation=self._generation,
            servers=tuple(servers),
            tools=tuple(tools),
            resources=tuple(resources),
            resource_templates=tuple(templates),
            prompts=tuple(prompts),
        )
        return changed

    def _sync_snapshots(self) -> None:
        for name, state in self._states.items():
            self._snapshots[name] = self._empty_snapshot(state)
        self._refresh_aggregate()

    # -- events ------------------------------------------------------------

    async def _emit_tools_changed(self, server: str) -> None:
        await self._emit(
            "mcp.tools_changed",
            {
                "server": server,
                "generation": self._generation,
                "tools": list(self._snapshot.tool_names()),
            },
        )

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        sink = self._sink
        if sink is None:
            return
        event = Event(type=event_type, data=self._scrub_payload(data))
        try:
            if hasattr(sink, "publish"):
                outcome = sink.publish(event)
            elif hasattr(sink, "emit"):
                outcome = sink.emit(event)
            elif callable(sink):
                outcome = sink(event)
            else:
                return
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - a broken sink never breaks a turn
            return

    # -- helpers -----------------------------------------------------------

    def _require_state(self, name: str) -> _ServerState:
        state = self._states.get(name)
        if state is None:
            raise MCPConfigError(f"unknown MCP server {name!r}")
        return state

    def _is_enabled(self, state: _ServerState) -> bool:
        return self._enabled and state.definition.enabled and not self._closed

    def _version(self, state: _ServerState) -> str:
        info = state.info
        version = getattr(info, "version", "") if info is not None else ""
        return version if isinstance(version, str) else ""

    def _compute_cache_key(self, state: _ServerState) -> str:
        if self._cache is None:
            return ""
        fingerprint = state.definition.fingerprint()
        return self._cache.make_key(state.name, self._version(state), fingerprint)

    def _collect_secrets(self) -> tuple[str, ...]:
        found: list[str] = []
        for definition in self._definitions.values():
            found.extend(definition.config.secrets)
        return tuple(dict.fromkeys(found))

    def _scrub(self, text: Any) -> str:
        if not isinstance(text, str):
            text = str(text)
        return redact_secrets(text, self._secrets)

    def _scrub_payload(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, Mapping):
            return {key: self._scrub_payload(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._scrub_payload(item) for item in value)
        return value


def _error_text(exc: BaseException) -> str:
    text = str(exc)
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


def _is_fatal(exc: BaseException) -> bool:
    """Whether a server failure indicates the connection is unusable.

    A JSON-RPC *remote* error is a tool-level answer and leaves the connection
    healthy; every other normalized MCP error (transport, timeout, closed,
    malformed protocol) means the server is gone.
    """
    if isinstance(exc, MCPRemoteError):
        return False
    return isinstance(exc, MCPError)


def _issue(server: str, kind: str, name: str, detail: str) -> BridgeIssue:
    return BridgeIssue(
        server=_SAFE_NAME_RE.sub("_", server)[:64] or "server",
        kind=kind,
        name=str(name)[:64],
        code="list_failed",
        detail=str(detail)[:300],
    )


def _issue_from_dict(value: Mapping[str, Any]) -> BridgeIssue:
    """Rebuild a :class:`BridgeIssue` from its cached JSON form."""
    return BridgeIssue(
        server=_SAFE_NAME_RE.sub("_", str(value.get("server", "")))[:64] or "server",
        kind=str(value.get("kind", "list"))[:64],
        name=str(value.get("name", ""))[:64],
        code=str(value.get("code", "list_failed"))[:64],
        detail=str(value.get("detail", ""))[:300],
    )
