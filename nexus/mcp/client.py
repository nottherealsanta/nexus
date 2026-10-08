"""MCP client: three transports behind one normalized, upstream-free boundary.

Plan section 5.5 approves the official ``mcp`` package for protocol types and
client sessions, but keeps the rest of Nexus from ever importing it: "that
wrapper is worth the file: it keeps an upstream breaking change confined to
``mcp/client.py``." This module is that wrapper.

Design
------
* **Normalized types.** Everything a caller sees is one of the ``MCP*`` structs
  below (or a :class:`~nexus.mcp.errors.MCPError`). No ``mcp`` object, ``httpx``
  response, ``asyncio`` subprocess, or raw JSON-RPC dict escapes.
* **Lazy, optional upstream.** The official package is imported only inside
  :func:`load_official_api`, and only when a caller selects the official
  backend. Importing this module never imports ``mcp``; the repository's
  dependency list does not include it yet, so the native backend is the default
  until integration adds the extra.
* **Native transports.** Stdio (spawned with an argv, never a shell), Streamable
  HTTP, and legacy SSE are implemented here so they are testable with no network
  and no optional dependency. Stdio children are started in their own session
  and the *whole process group* is terminated on close, cancellation, or a
  deadline -- a forked grandchild cannot outlive the client.
* **Deadlines everywhere.** ``connect``/``initialize``/``list``/``call`` each
  have a configurable deadline. A deadline is a normalized :class:`MCPTimeout`,
  never a leaked ``asyncio`` exception.
* **Bounded, injectable stderr.** A stdio child's stderr is drained into an
  injectable sink (memory by default, a file for integration), truncated to a
  fixed byte budget, redacted, and **never** placed in a result, context, or
  event.
* **Explicit secrets only.** ``${env:VAR}`` is the *only* interpolation form.
  A bare ``$VAR`` or ``${VAR}`` is left literal. Resolved env/header values are
  tracked and redacted from every diagnostic this module produces.
* **Failure isolation.** Upstream and transport exceptions are translated at
  the boundary, so a dead or hung server surfaces as a normalized error the MCP
  manager can degrade on rather than an exception that fails a turn.

The manager, bridge, and config schema are separate packets and are deliberately
not imported here.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import math
import os
import re
import signal
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self
from urllib.parse import urlsplit

import httpx
import msgspec

from .errors import (
    MCPCallError,
    MCPClosed,
    MCPConfigError,
    MCPError,
    MCPProtocolError,
    MCPRemoteError,
    MCPTimeout,
    MCPTransportError,
    MCPUnavailable,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_INIT_TIMEOUT_S",
    "DEFAULT_LIST_TIMEOUT_S",
    "MCP_PROTOCOL_VERSION",
    "FileStderrSink",
    "MCPCallResult",
    "MCPClient",
    "MCPContent",
    "MCPNotification",
    "MCPPrompt",
    "MCPPromptArgument",
    "MCPPromptResult",
    "MCPResource",
    "MCPResourceTemplate",
    "MCPServerConfig",
    "MCPServerInfo",
    "MCPTool",
    "MemoryStderrSink",
    "NullStderrSink",
    "StderrSink",
    "TransportKind",
    "load_official_api",
    "official_available",
    "parse_server_config",
    "redact_secrets",
]

#: The MCP revision this client announces. A server may negotiate a different
#: one; the value is recorded on :class:`MCPServerInfo`, not enforced.
MCP_PROTOCOL_VERSION = "2025-06-18"

DEFAULT_CONNECT_TIMEOUT_S = 20.0
DEFAULT_INIT_TIMEOUT_S = 20.0
DEFAULT_LIST_TIMEOUT_S = 20.0
DEFAULT_CALL_TIMEOUT_S = 60.0

#: A tools/resources/prompts listing that paginates forever is a hostile or
#: broken server; the client stops after this many pages rather than looping.
_MAX_LIST_PAGES = 100

#: Hard ceiling on a single JSON-RPC frame or SSE event, mirroring the stdio
#: transport's ``limit``. A server cannot grow client memory without bound.
_MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Hard ceiling on the number of queued frames awaiting the protocol reader.
#: A server that floods without ever being read is cut off rather than buffered
#: without limit.
_MAX_QUEUED_FRAMES = 1024

#: The only interpolation form. ``${env:VAR}`` where VAR is a shell-style name.
#: Every interpolation form the common ``mcp.json`` dialects use: ``${env:VAR}``
#: (VS Code, Cursor), ``${VAR}`` and ``${VAR:-default}`` (Claude Code),
#: ``{env:VAR}`` (OpenCode), the editor variables ``${workspaceFolder}``,
#: ``${workspaceFolderBasename}``, ``${userHome}``, ``${pathSeparator}`` and
#: ``${/}``, and VS Code's ``${input:id}`` (refused: Nexus never prompts). A
#: bare ``$VAR`` is never expanded.
_ENV_REF = re.compile(
    r"\$\{env:(?P<env>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|(?<!\$)\{env:(?P<oc>[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\$\{input:(?P<input>[^}]*)\}"
    r"|\$\{(?P<slash>/)\}"
    r"|\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)
_EDITOR_VARS = frozenset({"workspaceFolder", "workspaceFolderBasename", "userHome", "pathSeparator"})
#: An ``envFile`` larger than this is refused rather than read.
_MAX_ENV_FILE_BYTES = 65_536

#: Values shorter than this are not tracked as secrets: replacing "1" or "on"
#: everywhere in a diagnostic would mangle the message without protecting
#: anything. Pattern-based redaction below still catches real credentials.
_MIN_SECRET_LEN = 4

#: Credential shapes redacted from any diagnostic, mirroring
#: :mod:`nexus.model.http` so one policy covers provider and MCP surfaces.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 ***"),
    (re.compile(r"\bsk-[A-Za-z0-9._-]{4,}"), "***"),
    (re.compile(r"\b(?:pk|rk)_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "***"),
    (re.compile(r"\bAIza[A-Za-z0-9._-]{8,}"), "***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{6,}"), "***"),
    (re.compile(r"\bAKIA[A-Z0-9]{12,}"), "***"),
    (
        re.compile(
            r"(?i)(\b(?:api[-_]?key|x-api-key|authorization|access[-_]?token|"
            r"refresh[-_]?token|client[-_]?secret|secret|password|token)\b"
            r"\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"
        ),
        r"\1***",
    ),
    (re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"), "***"),
)

#: Environment variables a stdio child inherits from the parent when the caller
#: supplies no explicit base. This is an allow-list, not ``os.environ``: an
#: undeclared credential cannot leak into a server process. Explicit
#: ``config.env`` entries are added on top and may override any of these.
_BASE_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
    "SHELL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
)

TransportKind = Literal["stdio", "http", "sse"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with known secret values and credential shapes removed.

    Exact values are replaced first, longest first so a token that contains a
    shorter one is not half-replaced, then the credential-shape patterns run.
    """
    if not isinstance(text, str):
        text = str(text)
    for secret in sorted(
        {s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LEN},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "***")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _safe_url(url: str) -> str:
    """A URL with userinfo and query removed, for diagnostics."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        parts = parts._replace(netloc=netloc)
    if parts.query:
        parts = parts._replace(query=None)
    return parts.geturl()


def _url_secrets(url: str) -> list[str]:
    """Password (and user) embedded in a URL, if any."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    found = [parts.password or "", parts.username or ""]
    return [value for value in found if value]


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """The ``(scheme, host, port)`` origin of ``url``, or ``None`` if unusable.

    A missing port is normalized to the scheme default so ``https://h`` and
    ``https://h:443`` compare equal, and hostnames are case-folded.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        return None
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, host.lower(), port


def _same_origin(base: str, candidate: str) -> bool:
    """Whether ``candidate`` shares ``base``'s scheme, host, and port.

    Used to refuse an SSE ``endpoint`` event that points at another origin:
    the transport attaches the configured credentials to every request, so a
    server must not be able to redirect those credentials to a different host.
    """
    base_origin = _origin(base)
    return base_origin is not None and base_origin == _origin(candidate)


# ---------------------------------------------------------------------------
# Normalized types
# ---------------------------------------------------------------------------


class MCPTool(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized ``tools/list`` entry."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = msgspec.field(default_factory=dict)
    title: str = ""
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False


class MCPResource(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized ``resources/list`` entry."""

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""
    size: int = 0


class MCPResourceTemplate(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized ``resources/templates/list`` entry.

    Templates carry a ``uriTemplate`` (RFC 6570) rather than a concrete ``uri``;
    the field is normalized to ``uri_template`` so the bridge's template reader
    can tell the two apart.
    """

    uri_template: str
    name: str = ""
    description: str = ""
    mime_type: str = ""


class MCPPromptArgument(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One declared argument of a prompt."""

    name: str
    description: str = ""
    required: bool = False


class MCPPrompt(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized ``prompts/list`` entry."""

    name: str
    description: str = ""
    arguments: tuple[MCPPromptArgument, ...] = ()


class MCPContent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """One normalized content block from a tool/resource/prompt result.

    ``type`` is one of ``text``, ``image``, ``audio``, ``resource``,
    ``resource_link``; an unknown future type is preserved verbatim with its
    payload as text so a newer server cannot break the boundary.
    """

    type: str
    text: str = ""
    data: str = ""
    mime_type: str = ""
    uri: str = ""
    name: str = ""

    @property
    def is_text(self) -> bool:
        return self.type == "text"


class MCPCallResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized ``tools/call`` result.

    ``is_error`` is the protocol's ``isError`` flag (the tool ran and reported a
    failure). A JSON-RPC error object instead raises :class:`MCPCallError`.
    """

    content: tuple[MCPContent, ...] = ()
    is_error: bool = False
    structured: dict[str, Any] | None = None

    def text(self) -> str:
        """Concatenate the text blocks, the common case for a model-facing result."""
        return "\n".join(block.text for block in self.content if block.type == "text")


class MCPPromptResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized ``prompts/get`` result."""

    description: str = ""
    messages: tuple[MCPContent, ...] = ()


class MCPNotification(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A normalized server notification (for example ``tools/list_changed``)."""

    method: str
    params: dict[str, Any] = msgspec.field(default_factory=dict)


class MCPServerInfo(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Normalized ``initialize`` server metadata."""

    name: str = ""
    version: str = ""
    protocol_version: str = ""
    capabilities: dict[str, Any] = msgspec.field(default_factory=dict)
    instructions: str = ""


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise MCPConfigError(f"{field_name} must be a list of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise MCPConfigError(f"{field_name}[{index}] must be a string")
        if "\x00" in item:
            raise MCPConfigError(f"{field_name}[{index}] contains a NUL byte")
        out.append(item)
    return tuple(out)


def _string_map(value: object, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MCPConfigError(f"{field_name} must be an object of strings")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise MCPConfigError(f"{field_name} has a non-string or empty key")
        if not isinstance(item, str):
            raise MCPConfigError(f"{field_name}[{key!r}] must be a string")
        out[key] = item
    return out


def _timeout(raw: Mapping[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MCPConfigError(f"{key} must be a number")
    value = float(value)
    if not (value > 0) or not math.isfinite(value):
        raise MCPConfigError(f"{key} must be a positive finite number")
    return value


def _interpolate(
    value: str,
    environ: Mapping[str, str],
    *,
    field_name: str,
    secrets: list[str],
    places: Mapping[str, str] | None = None,
) -> str:
    """Resolve the ``${...}`` forms listed at :data:`_ENV_REF`.

    A missing variable without a ``:-default`` is an error naming the variable
    (never a value); every resolved environment value is tracked as a secret.
    Editor variables (``${workspaceFolder}``…) come from ``places`` and are not
    secrets. A bare ``$VAR`` is left untouched.
    """
    places = places or {}

    def replace(match: re.Match[str]) -> str:
        if match.group("input") is not None:
            raise MCPConfigError(
                f"{field_name}: ${{input:{match.group('input')}}} prompts are not "
                "supported; use ${env:VAR} instead"
            )
        if match.group("slash"):
            return os.sep
        name = match.group("env") or match.group("oc") or match.group("var")
        if match.group("var") and name in _EDITOR_VARS:
            if name not in places:
                raise MCPConfigError(f"{field_name}: ${{{name}}} is not available here")
            return places[name]
        if name not in environ:
            if match.group("default") is not None:
                return match.group("default")
            raise MCPConfigError(
                f"{field_name}: environment variable {name!r} is not set"
            )
        resolved = environ[name]
        secrets.append(resolved)
        return resolved

    result = _ENV_REF.sub(replace, value)
    if "\x00" in result:
        raise MCPConfigError(f"{field_name} contains a NUL byte")
    return result


def _places(workspace: str | os.PathLike[str] | None, home: str | os.PathLike[str] | None) -> dict[str, str]:
    places = {"userHome": str(Path(home) if home is not None else Path.home()), "pathSeparator": os.sep}
    if workspace is not None:
        places["workspaceFolder"] = str(Path(workspace))
        places["workspaceFolderBasename"] = Path(workspace).name
    return places


def _read_env_file(path_text: str, *, base: str | os.PathLike[str] | None, field_name: str) -> dict[str, str]:
    """Parse a bounded dotenv file: ``KEY=VALUE`` lines, ``export``, quotes, ``#`` comments."""
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = Path(base or ".") / path
    try:
        if path.stat().st_size > _MAX_ENV_FILE_BYTES:
            raise MCPConfigError(f"{field_name}: {path.name} exceeds {_MAX_ENV_FILE_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MCPConfigError(f"{field_name}: cannot read {path.name} ({type(exc).__name__})") from None
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if "\x00" not in value:
            out[key] = value
    return out


#: Per-server keys other MCP clients write that Nexus accepts and shows but does
#: not act on (approval lists, trust flags, OAuth blocks, catalogue metadata).
#: The permission engine stays the only approval boundary.
IGNORED_SERVER_KEYS = frozenset({
    "autoApprove", "alwaysAllow", "trust", "description", "oauth", "gallery",
    "version", "dev", "icon", "source", "networkTimeout", "settings",
    "auth", "headersHelper", "sandboxEnabled", "watchPaths",
})

_TRANSPORT_ALIASES = {
    "stdio": "stdio", "local": "stdio",
    "http": "http", "streamable-http": "http", "streamable_http": "http",
    "streamableHttp": "http", "remote": "http",
    "sse": "sse",
}


def server_enabled(name: str, raw: Mapping[str, Any]) -> bool:
    """The entry's on/off switch: ``enabled`` (Nexus, OpenCode) or ``disabled`` (Claude Desktop, Cline, Windsurf)."""
    enabled = raw.get("enabled", True)
    disabled = raw.get("disabled", False)
    if not isinstance(enabled, bool):
        raise MCPConfigError(f"server {name!r}: enabled must be a bool")
    if not isinstance(disabled, bool):
        raise MCPConfigError(f"server {name!r}: disabled must be a bool")
    return enabled and not disabled


def normalize_server_entry(name: str, raw: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Fold one entry written in any common ``mcp.json`` dialect into Nexus keys.

    Accepted besides the Nexus keys: ``type`` / ``transportType`` (``stdio``,
    ``local``, ``http``, ``streamable-http``, ``remote``, ``sse``); ``command``
    as an argv list (OpenCode) or a ``{path, args, env}`` object (Zed);
    ``environment`` (OpenCode); ``envFile``; ``serverUrl`` (Windsurf) and
    ``httpUrl`` (Gemini); ``timeout`` (milliseconds when ≥ 1000, otherwise
    seconds); ``includeTools`` / ``excludeTools`` / ``disabledTools``;
    ``enabled`` / ``disabled``; ``alwaysLoad`` (Claude Code) as
    ``tool_loading = "all"``. A URL without a transport is HTTP, or SSE when
    its path ends in ``/sse``. Returns the canonical mapping and the keys that
    were accepted but are ignored (:data:`IGNORED_SERVER_KEYS`). Structural
    errors raise :class:`MCPConfigError`; nothing is interpolated here.
    """
    if not isinstance(raw, Mapping):
        raise MCPConfigError(f"server {name!r} must be an object")
    data = dict(raw)
    data.pop("enabled", None)
    data.pop("disabled", None)
    ignored = tuple(sorted(key for key in data if key in IGNORED_SERVER_KEYS))
    for key in ignored:
        data.pop(key)

    kinds = {key: data.pop(key) for key in ("transport", "type", "transportType") if key in data}
    transports: set[str] = set()
    for key, value in kinds.items():
        if not isinstance(value, str) or value not in _TRANSPORT_ALIASES:
            raise MCPConfigError(
                f"server {name!r}: {key} {value!r} is not supported; use one of "
                + ", ".join(sorted(_TRANSPORT_ALIASES))
            )
        transports.add(_TRANSPORT_ALIASES[value])
    if len(transports) > 1:
        raise MCPConfigError(f"server {name!r}: {' and '.join(kinds)} disagree")

    def take_alias(target: str, *aliases: str) -> None:
        present = [key for key in (target, *aliases) if key in data]
        if len(present) > 1:
            raise MCPConfigError(f"server {name!r}: set only one of {', '.join(present)}")
        if present and present[0] != target:
            data[target] = data.pop(present[0])

    take_alias("env", "environment")
    if isinstance(data.get("env"), Mapping):  # VS Code allows number and null values
        data["env"] = {key: value if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else str(value)
                       for key, value in data["env"].items() if value is not None}
    if "alwaysLoad" in data:  # Claude Code: send every schema up front
        always = data.pop("alwaysLoad")
        if not isinstance(always, bool):
            raise MCPConfigError(f"server {name!r}: alwaysLoad must be a bool")
        if always:
            data.setdefault("tool_loading", "all")
    take_alias("env_file", "envFile")
    if "httpUrl" in data:
        transports.add("http")
        if len(transports) > 1:
            raise MCPConfigError(f"server {name!r}: httpUrl is Streamable HTTP but the transport says otherwise")
    take_alias("url", "serverUrl", "httpUrl")
    take_alias("exclude_tools", "excludeTools", "disabledTools")
    take_alias("include_tools", "includeTools")

    command = data.get("command")
    if isinstance(command, Mapping):  # Zed: {"path": ..., "args": [...], "env": {...}}
        extra = sorted(set(command) - {"path", "args", "env"})
        if extra:
            raise MCPConfigError(f"server {name!r}: command has unknown keys: {', '.join(extra)}")
        if "args" in data or "env" in data and "env" in command:
            raise MCPConfigError(f"server {name!r}: set args/env either inside command or beside it")
        data["command"] = command.get("path", "")
        if "args" in command:
            data["args"] = command["args"]
        if "env" in command:
            data["env"] = command["env"]
    elif isinstance(command, (list, tuple)):  # OpenCode: one argv list
        argv = _string_list(command, field_name=f"{name}.command")
        if not argv:
            raise MCPConfigError(f"server {name!r}: command list is empty")
        data["command"] = argv[0]
        data["args"] = [*argv[1:], *_string_list(data.get("args"), field_name=f"{name}.args")]

    if "timeout" in data:
        value = data.pop("timeout")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
            raise MCPConfigError(f"server {name!r}: timeout must be a positive number")
        data.setdefault("call_timeout_s", value / 1000 if value >= 1000 else float(value))

    if transports:
        data["transport"] = transports.pop()
    elif data.get("url") and not data.get("command"):
        url = data["url"]
        path = urlsplit(url).path.rstrip("/") if isinstance(url, str) else ""
        data["transport"] = "sse" if path.endswith("/sse") else "http"
    return data, ignored


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """A validated, normalized server definition.

    ``secrets`` holds every resolved env/header value (and URL userinfo) so the
    client can redact them from diagnostics. It is populated by
    :func:`parse_server_config`; construct the type directly only when no
    interpolation is needed.
    """

    name: str
    transport: TransportKind = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    init_timeout_s: float = DEFAULT_INIT_TIMEOUT_S
    list_timeout_s: float = DEFAULT_LIST_TIMEOUT_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    secrets: tuple[str, ...] = ()
    tool_loading: str = "search"
    tool_loading_source: str = "default"
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    #: Keys from another client's dialect that were accepted but are not acted on.
    ignored_keys: tuple[str, ...] = ()

    def allows_tool(self, tool: str) -> bool:
        """``includeTools`` keeps only the listed tools; ``excludeTools`` then removes some."""
        if self.include_tools and tool not in self.include_tools:
            return False
        return tool not in self.exclude_tools

    def redact(self, text: str) -> str:
        """Redact this server's secrets and credential shapes from ``text``."""
        return redact_secrets(text, self.secrets)

    def __repr__(self) -> str:
        # A config can carry credentials in ``env``/``headers``/URL userinfo.
        # Never let a traceback or a log line render them: show keys, counts,
        # and a redacted command/URL instead.
        return (
            f"MCPServerConfig(name={self.name!r}, transport={self.transport!r}, "
            f"command={self.redact(self.command)!r}, args={len(self.args)}, "
            f"env_keys={sorted(self.env)}, header_keys={sorted(self.headers)}, "
            f"url={_safe_url(self.url)!r}, "
            f"secrets=<{len(self.secrets)} redacted>)"
        )


def parse_server_config(
    name: str,
    raw: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    workspace: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> MCPServerConfig:
    """Validate one ``mcp.json`` server entry into a normalized config.

    The entry is first folded from its dialect by :func:`normalize_server_entry`;
    any other key is a :class:`MCPConfigError` rather than silently ignored.
    Interpolation (:data:`_ENV_REF`) applies to ``command``/``args``/``env``/
    ``env_file``/``cwd``/``url``/``headers``. ``env_file`` values sit under
    ``env`` (explicit ``env`` keys win).
    """
    if not isinstance(name, str) or not name.strip():
        raise MCPConfigError("MCP server name must be a non-empty string")
    if not isinstance(raw, Mapping):
        raise MCPConfigError(f"server {name!r} must be an object")
    source = os.environ if environ is None else environ
    raw, ignored = normalize_server_entry(name, raw)
    places = _places(workspace, home)

    allowed = {
        "transport",
        "command",
        "args",
        "env",
        "cwd",
        "url",
        "headers",
        "connect_timeout_s",
        "init_timeout_s",
        "list_timeout_s",
        "call_timeout_s",
        "tool_loading",
        "env_file",
        "include_tools",
        "exclude_tools",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise MCPConfigError(f"server {name!r} has unknown keys: {', '.join(unknown)}")

    transport = raw.get("transport", "stdio")
    if transport not in ("stdio", "http", "sse"):
        raise MCPConfigError(
            f"server {name!r}: transport must be 'stdio', 'http', or 'sse'"
        )

    tool_loading = raw.get("tool_loading", "search")
    if tool_loading not in ("search", "all"):
        raise MCPConfigError(f"server {name!r}: tool_loading must be 'search' or 'all'")

    secrets: list[str] = []

    command = raw.get("command", "")
    if command is not None and not isinstance(command, str):
        raise MCPConfigError(f"server {name!r}: command must be a string")
    command = _interpolate(
        command or "", source, field_name=f"{name}.command", secrets=secrets, places=places
    )

    args = _string_list(raw.get("args"), field_name=f"{name}.args")
    args = tuple(
        _interpolate(arg, source, field_name=f"{name}.args", secrets=secrets, places=places)
        for arg in args
    )

    env_file = raw.get("env_file", "")
    if env_file is not None and not isinstance(env_file, str):
        raise MCPConfigError(f"server {name!r}: envFile must be a string")
    env_file = _interpolate(env_file or "", source, field_name=f"{name}.envFile", secrets=secrets, places=places)
    env = {
        key: _interpolate(
            value, source, field_name=f"{name}.env.{key}", secrets=secrets, places=places
        )
        for key, value in _string_map(raw.get("env"), field_name=f"{name}.env").items()
    }

    if env_file:
        loaded = _read_env_file(env_file, base=workspace, field_name=f"{name}.envFile")
        env = {**loaded, **env}

    cwd = raw.get("cwd", "")
    if cwd is not None and not isinstance(cwd, str):
        raise MCPConfigError(f"server {name!r}: cwd must be a string")
    cwd = _interpolate(cwd or "", source, field_name=f"{name}.cwd", secrets=secrets, places=places)

    url = raw.get("url", "")
    if url is not None and not isinstance(url, str):
        raise MCPConfigError(f"server {name!r}: url must be a string")
    url = _interpolate(url or "", source, field_name=f"{name}.url", secrets=secrets, places=places)

    headers = {
        key: _interpolate(
            value, source, field_name=f"{name}.headers.{key}", secrets=secrets, places=places
        )
        for key, value in _string_map(
            raw.get("headers"), field_name=f"{name}.headers"
        ).items()
    }

    if transport == "stdio":
        if not command:
            raise MCPConfigError(f"server {name!r}: stdio transport requires command")
        if url:
            raise MCPConfigError(f"server {name!r}: stdio transport must not set url")
    else:
        if not url:
            raise MCPConfigError(f"server {name!r}: {transport} transport requires url")
        if command or args:
            raise MCPConfigError(
                f"server {name!r}: {transport} transport must not set command/args"
            )

    secrets.extend(_url_secrets(url))
    # Header values are credentials by nature; env values commonly are. Track
    # every non-empty one so a log line or error cannot echo it.
    secrets.extend(value for value in env.values() if value)
    secrets.extend(value for value in headers.values() if value)

    return MCPServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        url=url,
        headers=headers,
        connect_timeout_s=_timeout(raw, "connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S),
        init_timeout_s=_timeout(raw, "init_timeout_s", DEFAULT_INIT_TIMEOUT_S),
        list_timeout_s=_timeout(raw, "list_timeout_s", DEFAULT_LIST_TIMEOUT_S),
        call_timeout_s=_timeout(raw, "call_timeout_s", DEFAULT_CALL_TIMEOUT_S),
        tool_loading=tool_loading,
        tool_loading_source="config" if "tool_loading" in raw else "default",
        include_tools=_string_list(raw.get("include_tools"), field_name=f"{name}.includeTools"),
        exclude_tools=_string_list(raw.get("exclude_tools"), field_name=f"{name}.excludeTools"),
        ignored_keys=ignored,
        secrets=tuple(dict.fromkeys(secrets)),
    )


# ---------------------------------------------------------------------------
# Stderr sinks
# ---------------------------------------------------------------------------


class StderrSink(Protocol):
    """A bounded destination for a stdio child's stderr.

    Implementations must never raise: stderr from a broken server is diagnostic
    noise, and a sink fault must not fail a turn. The client redacts the chunk
    before calling :meth:`write`.
    """

    def write(self, chunk: str) -> None: ...


@dataclass
class MemoryStderrSink:
    """A bounded in-process ring of the most recent stderr bytes."""

    max_bytes: int = 8192
    _chunks: list[str] = field(default_factory=list, init=False)
    _size: int = field(default=0, init=False)
    _truncated: bool = field(default=False, init=False)

    def write(self, chunk: str) -> None:
        if not chunk or self._size >= self.max_bytes:
            if chunk:
                self._truncated = True
            return
        room = self.max_bytes - self._size
        encoded = chunk.encode("utf-8", "replace")
        if len(encoded) > room:
            chunk = encoded[:room].decode("utf-8", "ignore")
            self._truncated = True
        self._chunks.append(chunk)
        self._size += len(chunk.encode("utf-8", "replace"))

    @property
    def text(self) -> str:
        """The retained stderr, oldest first (``[truncated]`` when clipped)."""
        joined = "".join(self._chunks)
        return f"{joined}\n[truncated]" if self._truncated else joined

    def clear(self) -> None:
        self._chunks.clear()
        self._size = 0
        self._truncated = False


class FileStderrSink:
    """Append redacted, bounded stderr to a file for integration/``doctor``.

    The file is opened lazily on first write in append mode. Once the byte
    budget is spent, further output is counted and dropped rather than written;
    the child keeps draining so it never blocks on a full pipe.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 262_144) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._size = 0
        self._truncated = False
        self._failed = False

    def write(self, chunk: str) -> None:
        if self._failed or not chunk:
            return
        if self._size >= self.max_bytes:
            self._truncated = True
            return
        data = chunk.encode("utf-8", "replace")
        if len(data) > self.max_bytes - self._size:
            data = data[: self.max_bytes - self._size]
            self._truncated = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(data)
        except OSError:
            # A sink is best-effort: an unwritable log must not fail a turn.
            self._failed = True
            return
        self._size += len(data)


class NullStderrSink:
    """Discard stderr entirely (tests, or a server known to be noisy)."""

    def write(self, chunk: str) -> None:
        del chunk


# ---------------------------------------------------------------------------
# JSON-RPC framing helpers
# ---------------------------------------------------------------------------


class _SSEEvent(msgspec.Struct, frozen=True):
    event: str
    data: str
    last_id: str = ""


async def _aiter_sse(lines: AsyncIterator[str]) -> AsyncIterator[_SSEEvent]:
    """Parse an SSE byte stream into events, per the WHATWG event-stream rules.

    Only ``event``/``data``/``id`` are interpreted; comments and unknown fields
    are ignored; a blank line dispatches the buffered event. Multiple ``data:``
    lines are joined with newlines. A single event larger than
    :data:`_MAX_FRAME_BYTES` is refused rather than buffered.
    """
    event = ""
    data: list[str] = []
    last_id = ""
    buffered = 0
    async for line in lines:
        buffered += len(line)
        if buffered > _MAX_FRAME_BYTES:
            raise MCPProtocolError("MCP SSE event exceeded the frame limit")
        if line == "":
            if data:
                yield _SSEEvent(event or "message", "\n".join(data), last_id)
            event = ""
            data = []
            buffered = 0
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
        elif name == "id":
            last_id = value
    if data:
        yield _SSEEvent(event or "message", "\n".join(data), last_id)


class _FrameTransport(Protocol):
    """A byte-level JSON-RPC frame channel shared by all three transports.

    ``frames`` yields decoded JSON objects and raises a normalized
    :class:`MCPError` on EOF or fault; the protocol layer correlates them.
    """

    async def open(self) -> None: ...

    async def send(self, message: Mapping[str, Any]) -> None: ...

    def frames(self) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


class _EOF:
    """Queue sentinel marking a transport as finished."""


# ---------------------------------------------------------------------------
# Protocol layer
# ---------------------------------------------------------------------------


class _JsonRpcConnection:
    """JSON-RPC 2.0 request/response correlation over a frame transport."""

    def __init__(self, transport: _FrameTransport, *, redact: Callable[[str], str]):
        self._transport = transport
        self._redact = redact
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._notifications: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        self._fatal: MCPError | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._reader is None and not self._closed:
            self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        reader = self._reader
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._fail_all(MCPClosed(f"MCP connection to {self._transport!r} is closed"))
        self._notifications.put_nowait(_EOF())

    # -- sending -----------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float,
        deadline_method: str | None = None,
    ) -> Any:
        if self._closed or self._fatal is not None:
            raise self._fatal or MCPClosed("MCP connection is closed")
        request_id = self._new_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        label = deadline_method or method
        try:
            async with asyncio.timeout(timeout):
                await self._transport.send(message)
                return await future
        except TimeoutError as exc:
            self._schedule_cancel(request_id, "deadline exceeded")
            raise MCPTimeout(f"MCP {label} exceeded {timeout:g}s") from exc
        except asyncio.CancelledError:
            self._schedule_cancel(request_id, "cancelled")
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        if self._closed or self._fatal is not None:
            raise self._fatal or MCPClosed("MCP connection is closed")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._transport.send(message)

    # -- receiving ---------------------------------------------------------

    async def notifications(self) -> AsyncIterator[MCPNotification]:
        while True:
            item = await self._notifications.get()
            if isinstance(item, _EOF):
                return
            yield item

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _schedule_cancel(self, request_id: Any, reason: str) -> None:
        """Best-effort ``notifications/cancelled`` when a deadline or cancel fires.

        Fire-and-forget on purpose: the caller is unwinding, so it must not
        block on the transport, and a server that is already gone cannot be
        cancelled. The task is silenced so it is never reported unretrieved.
        """
        if self._closed:
            return
        coro = self.notify(
            "notifications/cancelled", {"requestId": request_id, "reason": reason}
        )
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:  # pragma: no cover - no running loop
            coro.close()
            return
        task.add_done_callback(_silence_task)

    async def _read_loop(self) -> None:
        try:
            async for frame in self._transport.frames():
                self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except MCPError as exc:
            self._fail_all(exc)
        except Exception as exc:  # noqa: BLE001 - the boundary must normalize
            self._fail_all(MCPTransportError(self._redact(str(exc))))
        else:
            self._fail_all(MCPTransportError("MCP server closed the connection"))

    def _dispatch(self, frame: Any) -> None:
        if not isinstance(frame, dict):
            self._fail_all(MCPProtocolError("MCP frame is not a JSON object"))
            return
        if "id" in frame and ("result" in frame or "error" in frame):
            future = self._pending.pop(frame["id"], None)
            if future is None or future.done():
                return
            if "error" in frame:
                future.set_exception(self._remote_error(frame["error"]))
            else:
                future.set_result(frame.get("result"))
            return
        if "method" in frame:
            if "id" in frame:
                self._spawn(self._answer_server_request(frame))
            else:
                self._notifications.put_nowait(
                    MCPNotification(
                        method=str(frame["method"]),
                        params=frame.get("params")
                        if isinstance(frame.get("params"), dict)
                        else {},
                    )
                )

    def _remote_error(self, raw: Any) -> MCPError:
        if isinstance(raw, Mapping):
            code = raw.get("code")
            message = raw.get("message")
            data = raw.get("data")
            if not isinstance(code, int) or isinstance(code, bool):
                code = -32603
            if not isinstance(message, str):
                message = "unspecified MCP error"
            return MCPRemoteError(code, self._redact(message), data)
        return MCPProtocolError(self._redact(f"malformed JSON-RPC error: {raw!r}"))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        task.add_done_callback(_silence_task)

    async def _answer_server_request(self, frame: Mapping[str, Any]) -> None:
        """Answer the few server-initiated requests a client must handle.

        The full surface (sampling, elicitation) is out of scope for this packet;
        answering method-not-found keeps such a server from stalling rather than
        pretending to support a capability. ``ping`` and an empty ``roots/list``
        are part of the base protocol.
        """
        request_id = frame.get("id")
        method = frame.get("method")
        if method == "ping":
            response: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {},
            }
        elif method == "roots/list":
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"roots": []},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {self._redact(str(method))}",
                },
            }
        try:
            await self._transport.send(response)
        except Exception:  # noqa: BLE001 - best-effort reply never fails a turn
            return

    def _fail_all(self, error: MCPError) -> None:
        if self._fatal is None:
            self._fatal = error
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


def _silence_task(task: asyncio.Task[Any]) -> None:
    """Consume a fire-and-forget task's result so it is never 'never retrieved'."""
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - own group; defensive
        return True
    except OSError:
        return False


class _StdioTransport:
    """Newline-framed JSON-RPC over a child process's stdio.

    The command is always an argv list; a shell is never involved. The child
    starts in its own session so the whole group can be terminated, and stderr
    is drained to a bounded, redacted sink.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        stderr_sink: StderrSink | None = None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._sink = stderr_sink if stderr_sink is not None else MemoryStderrSink()
        self._base_env = base_env
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"_StdioTransport(server={self._config.name!r}, "
            f"command={self._config.command!r})"
        )

    def _child_env(self) -> dict[str, str]:
        base = os.environ if self._base_env is None else self._base_env
        env = {key: base[key] for key in _BASE_ENV_KEYS if key in base}
        env.update(self._config.env)
        return env

    async def open(self) -> None:
        if self._proc is not None:
            return
        argv = [self._config.command, *self._config.args]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
                cwd=self._config.cwd or None,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            raise MCPTransportError(
                f"server {self._config.name!r}: command not found: "
                f"{self._config.command!r}"
            ) from exc
        except OSError as exc:
            raise MCPTransportError(
                self._config.redact(
                    f"server {self._config.name!r}: could not start: {exc}"
                )
            ) from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def send(self, message: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPTransportError("stdio transport is not open")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise MCPTransportError("MCP server stdin closed") from exc

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise MCPTransportError("stdio transport is not open")
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise MCPProtocolError("MCP frame exceeded the read limit") from exc
            if not line:
                code = proc.returncode
                if code is None:
                    with contextlib.suppress(Exception):
                        code = await asyncio.wait_for(proc.wait(), timeout=2)
                raise MCPTransportError(f"MCP server closed stdout (exit {code})")
            text = line.strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise MCPProtocolError(
                    self._config.redact(f"invalid JSON-RPC frame: {text[:200]!r}")
                ) from exc
            if not isinstance(frame, dict):
                raise MCPProtocolError("MCP frame is not a JSON object")
            yield frame

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                chunk = await proc.stderr.read(4096)
            except (ValueError, asyncio.LimitOverrunError):
                return
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            try:
                self._sink.write(self._config.redact(text))
            except Exception:  # noqa: BLE001 - a sink fault is never fatal
                return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            await self._terminate_group(proc)
        task = self._stderr_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _terminate_group(self, proc: asyncio.subprocess.Process) -> None:
        """TERM then KILL the whole process group, not just the leader.

        The leader can exit while leaving a forked descendant alive; because
        ``start_new_session=True`` made it a group leader, ``killpg`` still
        reaches that descendant after the leader is reaped.
        """
        if not hasattr(os, "killpg"):  # pragma: no cover - non-POSIX
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            return
        pgid = proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while _group_alive(pgid) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if _group_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2)


# ---------------------------------------------------------------------------
# HTTP transports
# ---------------------------------------------------------------------------


class _StreamableHTTPTransport:
    """Streamable HTTP: one POST per message, JSON or SSE response.

    Responses are pushed into a frame queue so the protocol layer sees the same
    channel shape as stdio. A server-assigned ``Mcp-Session-Id`` is echoed on
    later requests.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        client: httpx.AsyncClient,
        owns_client: bool,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = owns_client
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._session_id = ""
        self._closed = False

    async def open(self) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            **self._config.headers,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def send(self, message: Mapping[str, Any]) -> None:
        if self._closed:
            raise MCPTransportError("streamable HTTP transport is closed")
        task = asyncio.create_task(self._post(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, message: Mapping[str, Any]) -> None:
        try:
            async with self._client.stream(
                "POST", self._config.url, json=message, headers=self._headers()
            ) as response:
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id
                if 300 <= response.status_code < 400:
                    await response.aread()
                    self._push_error(
                        MCPTransportError(
                            f"HTTP {response.status_code} redirect refused from "
                            f"{_safe_url(self._config.url)}"
                        )
                    )
                    return
                if response.status_code in (202, 204):
                    return
                if response.status_code >= 400:
                    await response.aread()
                    self._push_error(
                        MCPTransportError(
                            f"HTTP {response.status_code} from {_safe_url(self._config.url)}"
                        )
                    )
                    return
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("application/json"):
                    self._push_json(await response.aread())
                elif content_type.startswith("text/event-stream"):
                    async for event in _aiter_sse(response.aiter_lines()):
                        if event.data:
                            self._push_json(event.data)
                else:
                    self._push_error(
                        MCPTransportError(
                            f"unexpected content-type {content_type!r} from "
                            f"{_safe_url(self._config.url)}"
                        )
                    )
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - the boundary must normalize
            self._push_error(MCPTransportError(self._config.redact(str(exc))))

    def _push_json(self, payload: Any) -> None:
        if isinstance(payload, (str, bytes, bytearray)) and len(payload) > _MAX_FRAME_BYTES:
            self._push_error(
                MCPProtocolError("MCP JSON-RPC response exceeded the frame limit")
            )
            return
        try:
            frame = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._push_error(
                MCPProtocolError(self._config.redact("invalid JSON-RPC response"))
            )
            del exc
            return
        if self._queue.qsize() >= _MAX_QUEUED_FRAMES:
            self._push_error(
                MCPProtocolError("MCP response queue exceeded its bound")
            )
            return
        self._queue.put_nowait(frame)

    def _push_error(self, error: MCPError) -> None:
        self._closed = True
        self._queue.put_nowait(error)

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if isinstance(item, MCPError):
                raise item
            if isinstance(item, _EOF):
                return
            yield item

    async def close(self) -> None:
        if self._closed and not self._tasks:
            if self._owns_client:
                await self._client.aclose()
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._queue.put_nowait(_EOF())
        if self._owns_client:
            await self._client.aclose()


class _SSETransport:
    """Legacy SSE: a GET event stream plus POSTs to a discovered endpoint.

    The first ``endpoint`` event names where messages are POSTed; server replies
    and notifications arrive on the still-open GET stream.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        client: httpx.AsyncClient,
        owns_client: bool,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = owns_client
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._endpoint_ready = asyncio.Event()
        self._post_url = ""
        self._reader: asyncio.Task[None] | None = None
        self._closed = False

    async def open(self) -> None:
        self._reader = asyncio.create_task(self._read_stream())

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "text/event-stream",
            **self._config.headers,
        }

    async def send(self, message: Mapping[str, Any]) -> None:
        if self._closed:
            raise MCPTransportError("SSE transport is closed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._endpoint_ready.wait(), timeout=self._config.connect_timeout_s
            )
        if not self._post_url:
            raise MCPTransportError("SSE endpoint was not announced by the server")
        try:
            response = await self._client.post(
                self._post_url,
                json=dict(message),
                headers={"content-type": "application/json", **self._config.headers},
            )
        except Exception as exc:
            raise MCPTransportError(self._config.redact(str(exc))) from exc
        if 300 <= response.status_code < 400:
            raise MCPTransportError(
                f"HTTP {response.status_code} redirect refused from "
                f"{_safe_url(self._post_url)}"
            )
        if response.status_code >= 400:
            raise MCPTransportError(
                f"HTTP {response.status_code} from {_safe_url(self._post_url)}"
            )

    async def _read_stream(self) -> None:
        try:
            async with self._client.stream(
                "GET", self._config.url, headers=self._headers()
            ) as response:
                if 300 <= response.status_code < 400:
                    await response.aread()
                    raise MCPTransportError(
                        f"HTTP {response.status_code} redirect refused from "
                        f"{_safe_url(self._config.url)}"
                    )
                if response.status_code >= 400:
                    await response.aread()
                    raise MCPTransportError(
                        f"HTTP {response.status_code} from {_safe_url(self._config.url)}"
                    )
                async for event in _aiter_sse(response.aiter_lines()):
                    if event.event == "endpoint" and event.data:
                        self._post_url = self._resolve_endpoint(event.data)
                        self._endpoint_ready.set()
                    elif event.data:
                        if len(event.data) > _MAX_FRAME_BYTES:
                            raise MCPProtocolError(
                                "MCP SSE event exceeded the frame limit"
                            )
                        if self._queue.qsize() >= _MAX_QUEUED_FRAMES:
                            raise MCPProtocolError(
                                "MCP SSE queue exceeded its bound"
                            )
                        try:
                            self._queue.put_nowait(json.loads(event.data))
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise MCPProtocolError(
                                self._config.redact("invalid JSON-RPC SSE frame")
                            ) from exc
        except asyncio.CancelledError:
            raise
        except MCPError as exc:
            self._queue.put_nowait(exc)
        except Exception as exc:  # noqa: BLE001 - the boundary must normalize
            self._queue.put_nowait(MCPTransportError(self._config.redact(str(exc))))
        finally:
            self._endpoint_ready.set()
            self._queue.put_nowait(_EOF())

    def _resolve_endpoint(self, endpoint: str) -> str:
        endpoint = endpoint.strip()
        if not endpoint:
            raise MCPTransportError("SSE server announced an empty endpoint")
        if "://" in endpoint:
            resolved = endpoint
        else:
            parts = urlsplit(self._config.url)
            base = f"{parts.scheme}://{parts.netloc}"
            if endpoint.startswith("/"):
                resolved = base + endpoint
            else:
                prefix = parts.path.rsplit("/", 1)[0]
                resolved = f"{base}{prefix}/{endpoint}"
        # The POST carries the configured credentials. Refuse an endpoint that
        # leaves the original origin so a server cannot exfiltrate them.
        if not _same_origin(self._config.url, resolved):
            raise MCPTransportError(
                f"SSE endpoint {_safe_url(resolved)} is not same-origin as "
                f"{_safe_url(self._config.url)}"
            )
        return resolved

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if isinstance(item, MCPError):
                raise item
            if isinstance(item, _EOF):
                return
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._endpoint_ready.set()
        reader = self._reader
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self._queue.put_nowait(_EOF())
        if self._owns_client:
            await self._client.aclose()


def _build_http_client(config: MCPServerConfig) -> httpx.AsyncClient:
    """Build the HTTP client used by the native HTTP/SSE transports.

    Redirects are disabled outright: the client attaches configured credentials
    (headers, URL userinfo) to every request, and following a redirect would
    forward those credentials to whatever host the server names. The connect
    timeout is wired from the server config; the read timeout is disabled so a
    long-lived SSE/streamable response is not cut off mid-stream.
    """
    timeout = httpx.Timeout(
        config.call_timeout_s,
        connect=config.connect_timeout_s,
        read=None,
        write=config.call_timeout_s,
        pool=config.connect_timeout_s,
    )
    return httpx.AsyncClient(follow_redirects=False, timeout=timeout)


# ---------------------------------------------------------------------------
# Native session
# ---------------------------------------------------------------------------


class _Session(Protocol):
    async def start(
        self, *, connect_timeout: float, init_timeout: float
    ) -> MCPServerInfo: ...

    async def list_tools(self, *, timeout: float) -> tuple[MCPTool, ...]: ...

    async def list_resources(self, *, timeout: float) -> tuple[MCPResource, ...]: ...

    async def list_resource_templates(
        self, *, timeout: float
    ) -> tuple[MCPResourceTemplate, ...]: ...

    async def list_prompts(self, *, timeout: float) -> tuple[MCPPrompt, ...]: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None, *, timeout: float
    ) -> MCPCallResult: ...

    async def read_resource(
        self, uri: str, *, timeout: float
    ) -> tuple[MCPContent, ...]: ...

    async def get_prompt(
        self, name: str, arguments: Mapping[str, Any] | None, *, timeout: float
    ) -> MCPPromptResult: ...

    def notifications(self) -> AsyncIterator[MCPNotification]: ...

    async def aclose(self) -> None: ...


class _NativeSession:
    """The protocol-correct session implemented entirely in this module."""

    def __init__(self, transport: _FrameTransport, config: MCPServerConfig) -> None:
        self._config = config
        self._transport = transport
        self._conn = _JsonRpcConnection(transport, redact=config.redact)
        self._info = MCPServerInfo()

    async def start(
        self, *, connect_timeout: float, init_timeout: float
    ) -> MCPServerInfo:
        try:
            async with asyncio.timeout(connect_timeout):
                await self._transport.open()
        except MCPError:
            raise
        except TimeoutError as exc:
            raise MCPTimeout(
                f"MCP connect to {self._config.name!r} exceeded {connect_timeout:g}s"
            ) from exc
        except Exception as exc:
            raise MCPTransportError(self._config.redact(str(exc))) from exc
        self._conn.start()
        params = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "nexus", "version": "0.1.0"},
        }
        result = await self._conn.request(
            "initialize", params, timeout=init_timeout, deadline_method="initialize"
        )
        self._info = _normalize_server_info(result)
        await self._conn.notify("notifications/initialized", {})
        return self._info

    @property
    def info(self) -> MCPServerInfo:
        return self._info

    async def list_tools(self, *, timeout: float) -> tuple[MCPTool, ...]:
        return await self._collect(
            "tools/list", "tools", _normalize_tool, timeout=timeout
        )

    async def list_resources(self, *, timeout: float) -> tuple[MCPResource, ...]:
        return await self._collect(
            "resources/list", "resources", _normalize_resource, timeout=timeout
        )

    async def list_resource_templates(
        self, *, timeout: float
    ) -> tuple[MCPResourceTemplate, ...]:
        return await self._collect(
            "resources/templates/list",
            "resourceTemplates",
            _normalize_resource_template,
            timeout=timeout,
        )

    async def list_prompts(self, *, timeout: float) -> tuple[MCPPrompt, ...]:
        return await self._collect(
            "prompts/list", "prompts", _normalize_prompt, timeout=timeout
        )

    async def _collect(
        self,
        method: str,
        key: str,
        normalize: Callable[[Any], Any],
        *,
        timeout: float,
    ) -> tuple[Any, ...]:
        out: list[Any] = []
        cursor: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = await self._conn.request(method, params, timeout=timeout)
            if not isinstance(result, Mapping):
                raise MCPProtocolError(f"{method} returned a non-object result")
            for raw in _as_list(result.get(key)):
                out.append(normalize(raw))
            cursor_value = result.get("nextCursor")
            if not isinstance(cursor_value, str) or not cursor_value:
                return tuple(out)
            cursor = cursor_value
        raise MCPProtocolError(f"{method} exceeded {_MAX_LIST_PAGES} pages")

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None, *, timeout: float
    ) -> MCPCallResult:
        try:
            result = await self._conn.request(
                "tools/call",
                {"name": name, "arguments": dict(arguments or {})},
                timeout=timeout,
            )
        except MCPRemoteError as exc:
            raise MCPCallError(exc.code, exc.message, exc.data) from exc
        return _normalize_call_result(result)

    async def read_resource(
        self, uri: str, *, timeout: float
    ) -> tuple[MCPContent, ...]:
        result = await self._conn.request(
            "resources/read", {"uri": uri}, timeout=timeout
        )
        if not isinstance(result, Mapping):
            raise MCPProtocolError("resources/read returned a non-object result")
        return tuple(
            _normalize_content(raw) for raw in _as_list(result.get("contents"))
        )

    async def get_prompt(
        self, name: str, arguments: Mapping[str, Any] | None, *, timeout: float
    ) -> MCPPromptResult:
        result = await self._conn.request(
            "prompts/get",
            {"name": name, "arguments": dict(arguments or {})},
            timeout=timeout,
        )
        if not isinstance(result, Mapping):
            raise MCPProtocolError("prompts/get returned a non-object result")
        messages = tuple(
            _normalize_content(raw.get("content"))
            for raw in _as_list(result.get("messages"))
            if isinstance(raw, Mapping)
        )
        description = _as_str(result.get("description"))
        return MCPPromptResult(description=description, messages=messages)

    def notifications(self) -> AsyncIterator[MCPNotification]:
        return self._conn.notifications()

    async def aclose(self) -> None:
        await self._conn.close()
        await self._transport.close()


# ---------------------------------------------------------------------------
# Official session (lazy, optional)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _OfficialAPI:
    """The handful of official SDK entry points the adapter needs."""

    ClientSession: Any
    StdioServerParameters: Any
    stdio_client: Any
    streamable_http_client: Any
    sse_client: Any


def load_official_api() -> _OfficialAPI:
    """Import the optional official ``mcp`` package and return its entry points.

    Raises :class:`MCPUnavailable` (a normalized error) when the package or one
    of its transports is missing, so a caller asking for the official backend
    gets one clear error instead of an ``ImportError`` from deep in a call.
    """
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise MCPUnavailable("the official 'mcp' package is not installed") from exc

    module = None
    try:
        module = importlib.import_module("mcp.client.streamable_http")
    except ImportError:
        module = None
    if module is None:
        raise MCPUnavailable(
            "the official 'mcp' package lacks a streamable HTTP transport"
        )
    streamable = getattr(module, "streamable_http_client", None)
    if streamable is None:  # the module was renamed across SDK majors
        streamable = getattr(module, "streamablehttp_client", None)
    if streamable is None:
        raise MCPUnavailable(
            "the official 'mcp' package lacks a streamable HTTP transport"
        )
    return _OfficialAPI(
        ClientSession=ClientSession,
        StdioServerParameters=StdioServerParameters,
        stdio_client=stdio_client,
        streamable_http_client=streamable,
        sse_client=sse_client,
    )


def official_available() -> bool:
    """Whether the official ``mcp`` package and its transports are importable."""
    try:
        load_official_api()
    except MCPUnavailable:
        return False
    return True


class _OfficialSession:
    """Adapt the official ``ClientSession`` to the normalized session protocol."""

    def __init__(self, config: MCPServerConfig, api: _OfficialAPI) -> None:
        self._config = config
        self._api = api
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None
        self._info = MCPServerInfo()

    async def start(
        self, *, connect_timeout: float, init_timeout: float
    ) -> MCPServerInfo:
        stack = contextlib.AsyncExitStack()
        try:
            async with asyncio.timeout(connect_timeout):
                await stack.__aenter__()
                transport = self._make_transport()
                streams = await stack.enter_async_context(transport)
            read_stream, write_stream = streams[0], streams[1]
            async with asyncio.timeout(init_timeout):
                session = await stack.enter_async_context(
                    self._api.ClientSession(read_stream, write_stream)
                )
                result = await session.initialize()
        except MCPError:
            await stack.aclose()
            raise
        except TimeoutError as exc:
            await stack.aclose()
            raise MCPTimeout(
                f"MCP connect to {self._config.name!r} exceeded its deadline"
            ) from exc
        except Exception as exc:
            await stack.aclose()
            raise MCPTransportError(self._config.redact(str(exc))) from exc
        self._stack = stack
        self._session = session
        self._info = _normalize_server_info(result)
        return self._info

    def _make_transport(self) -> Any:
        config = self._config
        if config.transport == "stdio":
            params = self._api.StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=dict(config.env) or None,
                cwd=config.cwd or None,
            )
            return self._api.stdio_client(params)
        if config.transport == "http":
            return self._api.streamable_http_client(
                config.url, headers=dict(config.headers) or None
            )
        return self._api.sse_client(config.url, headers=dict(config.headers) or None)

    async def _invoke(self, what: str, coro: Any, *, timeout: float) -> Any:
        if self._session is None:
            raise MCPClosed("official MCP session is not open")
        try:
            async with asyncio.timeout(timeout):
                return await coro
        except (MCPError, asyncio.CancelledError):
            raise
        except TimeoutError as exc:
            raise MCPTimeout(f"MCP {what} exceeded {timeout:g}s") from exc
        except Exception as exc:
            raise MCPTransportError(
                self._config.redact(f"MCP {what} failed: {exc}")
            ) from exc

    async def list_tools(self, *, timeout: float) -> tuple[MCPTool, ...]:
        result = await self._invoke(
            "tools/list", self._session.list_tools(), timeout=timeout
        )
        return tuple(_normalize_tool(raw) for raw in _as_list(_get(result, "tools")))

    async def list_resources(self, *, timeout: float) -> tuple[MCPResource, ...]:
        result = await self._invoke(
            "resources/list", self._session.list_resources(), timeout=timeout
        )
        return tuple(
            _normalize_resource(raw) for raw in _as_list(_get(result, "resources"))
        )

    async def list_resource_templates(
        self, *, timeout: float
    ) -> tuple[MCPResourceTemplate, ...]:
        method = getattr(self._session, "list_resource_templates", None)
        if not callable(method):
            raise MCPUnavailable(
                "official MCP session does not support resource templates"
            )
        result = await self._invoke(
            "resources/templates/list", method(), timeout=timeout
        )
        return tuple(
            _normalize_resource_template(raw)
            for raw in _as_list(_get(result, "resourceTemplates"))
        )

    async def list_prompts(self, *, timeout: float) -> tuple[MCPPrompt, ...]:
        result = await self._invoke(
            "prompts/list", self._session.list_prompts(), timeout=timeout
        )
        return tuple(
            _normalize_prompt(raw) for raw in _as_list(_get(result, "prompts"))
        )

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None, *, timeout: float
    ) -> MCPCallResult:
        result = await self._invoke(
            "tools/call",
            self._session.call_tool(name, dict(arguments or {})),
            timeout=timeout,
        )
        return _normalize_call_result(result)

    async def read_resource(
        self, uri: str, *, timeout: float
    ) -> tuple[MCPContent, ...]:
        result = await self._invoke(
            "resources/read", self._session.read_resource(uri), timeout=timeout
        )
        return tuple(
            _normalize_content(raw) for raw in _as_list(_get(result, "contents"))
        )

    async def get_prompt(
        self, name: str, arguments: Mapping[str, Any] | None, *, timeout: float
    ) -> MCPPromptResult:
        result = await self._invoke(
            "prompts/get",
            self._session.get_prompt(name, dict(arguments or {})),
            timeout=timeout,
        )
        messages = tuple(
            _normalize_content(_get(raw, "content"))
            for raw in _as_list(_get(result, "messages"))
        )
        return MCPPromptResult(
            description=_as_str(_get(result, "description")),
            messages=messages,
        )

    async def notifications(self) -> AsyncIterator[MCPNotification]:
        # The official session exposes callbacks rather than a queue; this
        # packet does not bridge them, so the stream is empty and terminates.
        return
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()


# ---------------------------------------------------------------------------
# Normalizers (accept dicts or official objects; nothing upstream escapes)
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        with contextlib.suppress(Exception):
            result = dump()
            if isinstance(result, Mapping):
                return dict(result)
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {key: item for key, item in attrs.items() if not key.startswith("_")}
    return {}


def _normalize_tool(raw: Any) -> MCPTool:
    annotations = _get(raw, "annotations")
    schema = _get(raw, "inputSchema")
    if not isinstance(schema, Mapping):
        schema = _get(raw, "input_schema")
    return MCPTool(
        name=_as_str(_get(raw, "name")),
        description=_as_str(_get(raw, "description")),
        input_schema=dict(schema) if isinstance(schema, Mapping) else {},
        title=_as_str(_get(annotations, "title")),
        read_only=_as_bool(_get(annotations, "readOnlyHint")),
        destructive=_as_bool(_get(annotations, "destructiveHint")),
        idempotent=_as_bool(_get(annotations, "idempotentHint")),
        open_world=_as_bool(_get(annotations, "openWorldHint")),
    )


def _normalize_resource(raw: Any) -> MCPResource:
    return MCPResource(
        uri=_as_str(_get(raw, "uri")),
        name=_as_str(_get(raw, "name")),
        description=_as_str(_get(raw, "description")),
        mime_type=_as_str(_get(raw, "mimeType")),
        size=_as_int(_get(raw, "size")),
    )


def _normalize_resource_template(raw: Any) -> MCPResourceTemplate:
    return MCPResourceTemplate(
        uri_template=_as_str(_get(raw, "uriTemplate", "uri_template")),
        name=_as_str(_get(raw, "name")),
        description=_as_str(_get(raw, "description")),
        mime_type=_as_str(_get(raw, "mimeType")),
    )


def _normalize_prompt_argument(raw: Any) -> MCPPromptArgument:
    return MCPPromptArgument(
        name=_as_str(_get(raw, "name")),
        description=_as_str(_get(raw, "description")),
        required=_as_bool(_get(raw, "required")),
    )


def _normalize_prompt(raw: Any) -> MCPPrompt:
    return MCPPrompt(
        name=_as_str(_get(raw, "name")),
        description=_as_str(_get(raw, "description")),
        arguments=tuple(
            _normalize_prompt_argument(item)
            for item in _as_list(_get(raw, "arguments"))
        ),
    )


def _normalize_content(raw: Any) -> MCPContent:
    content_type = _as_str(_get(raw, "type"))
    if not content_type:
        # ``resources/read`` returns flat ``TextResourceContents``/
        # ``BlobResourceContents`` objects that carry a ``uri`` but no ``type``.
        if _get(raw, "uri") is not None and (
            _get(raw, "text") is not None or _get(raw, "blob") is not None
        ):
            content_type = "resource"
        else:
            # A bare string payload (or an object with only ``text``) is text.
            text = (
                _as_str(_get(raw, "text")) if isinstance(raw, Mapping) else _as_str(raw)
            )
            return MCPContent(type="text", text=text)
    if content_type == "resource":
        resource = _get(raw, "resource")
        uri = _as_str(_get(resource, "uri")) or _as_str(_get(raw, "uri"))
        mime = _as_str(_get(resource, "mimeType")) or _as_str(_get(raw, "mimeType"))
        text = _as_str(_get(resource, "text")) or _as_str(_get(raw, "text"))
        data = _as_str(_get(resource, "blob")) or _as_str(_get(raw, "blob"))
        return MCPContent(
            type="resource", uri=uri, mime_type=mime, text=text, data=data
        )
    if content_type == "resource_link":
        return MCPContent(
            type="resource_link",
            uri=_as_str(_get(raw, "uri")),
            name=_as_str(_get(raw, "name")),
            mime_type=_as_str(_get(raw, "mimeType")),
            description="",
        )
    if content_type in ("image", "audio"):
        return MCPContent(
            type=content_type,
            data=_as_str(_get(raw, "data")),
            mime_type=_as_str(_get(raw, "mimeType")),
        )
    if content_type == "text":
        return MCPContent(type="text", text=_as_str(_get(raw, "text")))
    # An unknown future content type is preserved as text rather than dropped.
    return MCPContent(type=content_type, text=_as_str(_get(raw, "text")))


def _normalize_call_result(raw: Any) -> MCPCallResult:
    structured = _get(raw, "structuredContent")
    if not isinstance(structured, Mapping):
        structured = None
    return MCPCallResult(
        content=tuple(
            _normalize_content(item) for item in _as_list(_get(raw, "content"))
        ),
        is_error=_as_bool(_get(raw, "isError")),
        structured=dict(structured) if structured is not None else None,
    )


def _normalize_server_info(raw: Any) -> MCPServerInfo:
    if raw is None:
        return MCPServerInfo(protocol_version=MCP_PROTOCOL_VERSION)
    info = _get(raw, "serverInfo")
    return MCPServerInfo(
        name=_as_str(_get(info, "name")),
        version=_as_str(_get(info, "version")),
        protocol_version=_as_str(_get(raw, "protocolVersion"), MCP_PROTOCOL_VERSION),
        capabilities=_as_dict(_get(raw, "capabilities")),
        instructions=_as_str(_get(raw, "instructions")),
    )


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------

Backend = Literal["auto", "native", "official"]


class MCPClient:
    """One connected MCP server, behind normalized types.

    ``backend="auto"`` (the default) uses the native transports. They speak the
    same protocol as the official package but preserve ``list_changed``
    notifications and resource templates, which the official-session adapter
    cannot bridge here, so the default never silently drops a change
    notification. Pass ``backend="official"`` to opt into the optional ``mcp``
    package, ``transport=`` to inject a frame transport directly (tests), or
    ``http_client=`` to supply the ``httpx`` client used by HTTP/SSE.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        backend: Backend = "auto",
        transport: _FrameTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        stderr_sink: StderrSink | None = None,
        base_env: Mapping[str, str] | None = None,
        official_api: _OfficialAPI | None = None,
    ) -> None:
        if backend not in ("auto", "native", "official"):
            raise MCPConfigError("backend must be 'auto', 'native', or 'official'")
        self.config = config
        self._backend = backend
        self._transport = transport
        self._http_client = http_client
        self._owns_http_client = False
        self._stderr_sink = stderr_sink
        self._base_env = base_env
        self._official_api = official_api
        self._session: _Session | None = None
        self._server_info = MCPServerInfo()
        self._closed = False

    # -- properties --------------------------------------------------------

    @property
    def server_info(self) -> MCPServerInfo:
        return self._server_info

    @property
    def connected(self) -> bool:
        return self._session is not None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, *, timeout: float | None = None) -> MCPServerInfo:
        """Open the transport and complete the ``initialize`` handshake."""
        if self._closed:
            raise MCPClosed(
                f"MCP client for {self.config.name!r} is closed; create a new client"
            )
        if self._session is not None:
            return self._server_info
        session = self._build_session()
        connect_timeout = timeout or self.config.connect_timeout_s
        init_timeout = timeout or self.config.init_timeout_s
        try:
            info = await session.start(
                connect_timeout=connect_timeout, init_timeout=init_timeout
            )
        except BaseException:
            await _quiet_close(session)
            if self._owns_http_client and self._http_client is not None:
                await self._http_client.aclose()
                self._owns_http_client = False
            raise
        # ``aclose`` may have run while the handshake was in flight; it saw no
        # session to close, so close the one we just opened instead of leaking
        # a live transport. Recheck before publishing it as connected.
        if self._closed:
            await _quiet_close(session)
            raise MCPClosed(
                f"MCP client for {self.config.name!r} was closed during connect"
            )
        self._session = session
        self._server_info = info
        return info

    async def aclose(self) -> None:
        """Close the session and release every owned resource. Idempotent."""
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            await _quiet_close(session)
        if self._transport is not None:
            transport, self._transport = self._transport, None
            with contextlib.suppress(Exception):
                await transport.close()
        if self._owns_http_client and self._http_client is not None:
            client = self._http_client
            self._owns_http_client = False
            self._http_client = None
            with contextlib.suppress(Exception):
                await client.aclose()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- operations --------------------------------------------------------

    async def list_tools(self, *, timeout: float | None = None) -> tuple[MCPTool, ...]:
        return await self._require().list_tools(
            timeout=timeout or self.config.list_timeout_s
        )

    async def list_resources(
        self, *, timeout: float | None = None
    ) -> tuple[MCPResource, ...]:
        return await self._require().list_resources(
            timeout=timeout or self.config.list_timeout_s
        )

    async def list_resource_templates(
        self, *, timeout: float | None = None
    ) -> tuple[MCPResourceTemplate, ...]:
        return await self._require().list_resource_templates(
            timeout=timeout or self.config.list_timeout_s
        )

    async def list_prompts(
        self, *, timeout: float | None = None
    ) -> tuple[MCPPrompt, ...]:
        return await self._require().list_prompts(
            timeout=timeout or self.config.list_timeout_s
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> MCPCallResult:
        if not isinstance(name, str) or not name:
            raise MCPConfigError("tool name must be a non-empty string")
        return await self._require().call_tool(
            name, arguments, timeout=timeout or self.config.call_timeout_s
        )

    async def read_resource(
        self, uri: str, *, timeout: float | None = None
    ) -> tuple[MCPContent, ...]:
        if not isinstance(uri, str) or not uri:
            raise MCPConfigError("resource uri must be a non-empty string")
        return await self._require().read_resource(
            uri, timeout=timeout or self.config.call_timeout_s
        )

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> MCPPromptResult:
        if not isinstance(name, str) or not name:
            raise MCPConfigError("prompt name must be a non-empty string")
        return await self._require().get_prompt(
            name, arguments, timeout=timeout or self.config.call_timeout_s
        )

    def notifications(self) -> AsyncIterator[MCPNotification]:
        return self._require().notifications()

    # -- internals ---------------------------------------------------------

    def _require(self) -> _Session:
        if self._session is None:
            raise MCPClosed(
                f"MCP client for {self.config.name!r} is not connected; call connect()"
            )
        return self._session

    def _build_session(self) -> _Session:
        if self._transport is not None:
            return _NativeSession(self._transport, self.config)
        if self._backend == "native":
            return _NativeSession(self._native_transport(), self.config)
        # ``auto`` prefers the native transports deliberately: they preserve
        # ``list_changed`` notifications (the official adapter cannot bridge its
        # callbacks into the normalized notification stream here), so a manifest
        # refresh from a server notification is never silently lost. The
        # official backend is opt-in via ``backend="official"``.
        if self._backend == "auto":
            return _NativeSession(self._native_transport(), self.config)
        api = self._official_api
        if api is None:
            api = load_official_api()
        return _OfficialSession(self.config, api)

    def _native_transport(self) -> _FrameTransport:
        if self.config.transport == "stdio":
            return _StdioTransport(
                self.config, stderr_sink=self._stderr_sink, base_env=self._base_env
            )
        if self._http_client is None:
            self._http_client = _build_http_client(self.config)
            self._owns_http_client = True
        if self.config.transport == "http":
            return _StreamableHTTPTransport(
                self.config,
                client=self._http_client,
                owns_client=self._owns_http_client,
            )
        return _SSETransport(
            self.config, client=self._http_client, owns_client=self._owns_http_client
        )


async def _quiet_close(session: _Session) -> None:
    with contextlib.suppress(Exception):
        await session.aclose()
