"""`[mcp.servers.<name>]` entries: dialects, interpolation and validation.

Entries use the keys other MCP clients write, so a server copied from VS Code,
Claude Code or Desktop, Cursor, Cline, Windsurf, Gemini CLI, OpenCode or Zed
works as it is (`nexus config import-mcp` copies a whole file). A bad entry is
an error for that server only.

Adapted from old Nexus `nexus/mcp/client.py` `normalize_server_entry`, cut down
to the keys that act: no `envFile`, no `${input:…}`; OAuth is `oauth.py`. Keys that other
clients write for approval or UI (`autoApprove`, `trust`, `description`…) are
accepted and reported, never acted on; a tool call stays with the permission rules.
"""
from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}\Z")
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_CALL_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
TRANSPORTS = ("stdio", "http", "sse")
_TRANSPORT_NAMES = {"stdio": "stdio", "local": "stdio", "http": "http", "streamable-http": "http",
                    "streamablehttp": "http", "remote": "http", "sse": "sse"}
_KEYS = {"command", "args", "env", "environment", "cwd", "url", "serverUrl", "httpUrl", "headers", "transport", "type",
         "transportType", "enabled", "disabled", "includeTools", "excludeTools",
         "disabledTools", "timeout", "call_timeout_s", "connect_timeout_s", "oauth"}
_SHOWN_ONLY = {"autoApprove", "alwaysAllow", "trust", "auth", "headersHelper", "description", "gallery",
               "version", "dev", "icon", "source", "networkTimeout", "settings", "sandboxEnabled", "watchPaths",
               "$schema", "tool_loading", "alwaysLoad"}  # the last two: loading is a global setting now ([mcp] full_definitions)
_VAR = re.compile(r"\$\{([^}]+)\}|\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


_ALLOW_MISSING_ENV: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_allow_missing_env", default=False)


def validate(name: str, entry: Any, *, workspace: str = "") -> ServerConfig:
    """`parse` for a write: a `${env:NAME}` that is not set here yet is accepted (the row then says which one is missing).

    Everything else is checked as it is when a server starts, so a typo is refused when it is saved.
    """
    token = _ALLOW_MISSING_ENV.set(True)
    try:
        return parse(name, entry, workspace=workspace)
    finally:
        _ALLOW_MISSING_ENV.reset(token)


class McpConfigError(ValueError):
    """One server's entry is unusable."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    name: str
    transport: str  # stdio | http | sse
    enabled: bool = True
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    ignored: tuple[str, ...] = ()  # accepted keys that Nexus does not act on
    oauth: dict[str, Any] | None = None  # remote only: None is off; a table (maybe empty) signs in through OAuth

    def describe(self) -> dict[str, Any]:
        """Facts for status and `/api/mcp/{name}`: no argument values, env values, header values or URL credentials."""
        info: dict[str, Any] = {"transport": self.transport, "enabled": self.enabled,
                                "include": list(self.include), "exclude": list(self.exclude),
                                "connect_timeout": self.connect_timeout, "call_timeout": self.call_timeout,
                                "ignored_keys": list(self.ignored)}
        if self.transport == "stdio":
            info |= {"command": Path(self.command).name, "arg_count": len(self.args), "env_names": sorted(self.env),
                     "cwd": self.cwd}
        else:
            parts = urlsplit(self.url)
            try:
                port = parts.port
            except ValueError:
                port = None
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):  # a bracketed IPv6 literal stays a valid URL
                host = f"[{host}]"
            info |= {"url": f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path}",
                     "header_names": sorted(self.headers), "oauth": self.oauth is not None}
        return info


def interpolate(text: str, *, workspace: str = "") -> str:
    """`${env:VAR}`, `${VAR}`, `${VAR:-default}`, `{env:VAR}`, `${userHome}`; a missing variable is an error."""
    def one(match: re.Match[str]) -> str:
        if match.group(2):
            expr = match.group(2)
        else:
            expr = match.group(1)
            if expr.startswith("input:"):
                raise McpConfigError("${input:…} is not supported: Nexus never prompts for server inputs")
            if expr in ("workspaceFolder", "workspaceFolderBasename"):
                if not workspace:
                    raise McpConfigError("${workspaceFolder} is not available: servers are shared by every project")
                return workspace if expr == "workspaceFolder" else Path(workspace).name
            if expr == "userHome":
                return str(Path.home())
            if expr in ("pathSeparator", "/"):
                return os.sep
            expr = expr.removeprefix("env:")
        name, sep, default = expr.partition(":-")
        value = os.environ.get(name)
        if value is None:
            if sep:
                return default
            if _ALLOW_MISSING_ENV.get():
                return ""
            raise McpConfigError(f"environment variable {name} is not set")
        return value
    return _VAR.sub(one, text)


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise McpConfigError(f"{what} must be a list of strings")
    return tuple(value)


def _table(value: Any, what: str) -> dict[str, Any]:
    """A raw table, checked before its entries are merged or interpolated."""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise McpConfigError(f"{what} must be a table of strings")
    return value


def _mapping(value: Any, what: str, workspace: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, item in _table(value, what).items():
        if isinstance(item, bool | int | float):
            item = str(item)
        if not isinstance(item, str):
            raise McpConfigError(f"{what}.{key} must be a string")
        out[key] = interpolate(item, workspace=workspace)
    return out


def _seconds(entry: dict[str, Any], default: float, *keys: str) -> float:
    for key in keys:
        if key in entry:
            value = entry[key]
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                raise McpConfigError(f"{key} must be a positive number")
            if key == "timeout" and value >= 1000:  # milliseconds (Claude Code, OpenCode, Gemini)
                value /= 1000
            return min(float(value), MAX_TIMEOUT)
    return default


_OAUTH_KEYS = {"scope", "client_id", "client_secret", "redirect_port"}


def _oauth(entry: dict[str, Any], headers: dict[str, str]) -> dict[str, Any] | None:
    """`oauth = true`, `false` or a table (`scope`, `client_id`, `client_secret`). Unset: on unless the entry sends its own `Authorization`."""
    if "oauth" not in entry:
        return None if any(k.lower() == "authorization" for k in headers) else {}
    value = entry["oauth"]
    if isinstance(value, bool):
        return {} if value else None
    if not isinstance(value, dict):
        raise McpConfigError("oauth must be true, false or a table")
    unknown = sorted(set(value) - _OAUTH_KEYS)
    if unknown:
        raise McpConfigError(f"unknown oauth keys: {', '.join(unknown)}")
    port = value.get("redirect_port")
    if "redirect_port" in value and (isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535):
        raise McpConfigError("oauth.redirect_port must be a port from 1024 to 65535")
    if not all(isinstance(v, str) and v for k, v in value.items() if k != "redirect_port"):
        raise McpConfigError("oauth.scope, oauth.client_id and oauth.client_secret must be non-empty strings")
    if "client_secret" in value and "client_id" not in value:
        raise McpConfigError("oauth.client_secret needs oauth.client_id")
    return {k: v if k == "redirect_port" else interpolate(v, workspace="") for k, v in value.items()}


def parse(name: str, entry: Any, *, workspace: str = "") -> ServerConfig:
    """Validate one entry. Raises McpConfigError naming the problem (never including a secret value)."""
    if not NAME.fullmatch(name):
        raise McpConfigError("server names use letters, digits, '.', '_' and '-' (up to 32 characters)")
    if not isinstance(entry, dict):
        raise McpConfigError("a server entry must be a table")
    unknown = sorted(k for k in entry if k not in _KEYS and k not in _SHOWN_ONLY)
    if unknown:
        raise McpConfigError(f"unknown keys: {', '.join(unknown)}")
    ignored = tuple(sorted(k for k in entry if k in _SHOWN_ONLY))
    declared = next((entry[k] for k in ("transport", "type", "transportType") if k in entry), None)
    command = entry.get("command")
    url = next((entry[k] for k in ("url", "serverUrl", "httpUrl") if k in entry), "")
    if declared is not None:
        if not isinstance(declared, str) or declared.lower() not in _TRANSPORT_NAMES:
            raise McpConfigError(f"transport {declared!r} is not supported (stdio, http or sse)")
        transport = _TRANSPORT_NAMES[declared.lower()]
    elif command:
        transport = "stdio"
    elif url:
        transport = "sse" if isinstance(url, str) and url.split("?")[0].rstrip("/").endswith("/sse") else "http"
    else:
        raise McpConfigError("give `command` (a local server) or `url` (a remote one)")
    enabled = entry.get("enabled", not entry.get("disabled", False))
    if not isinstance(enabled, bool):
        raise McpConfigError("enabled and disabled must be true or false")
    include = _strings(entry.get("includeTools", []), "includeTools")
    exclude = _strings(entry.get("excludeTools", entry.get("disabledTools", [])), "excludeTools")
    base: dict[str, Any] = {
        "name": name, "transport": transport, "enabled": enabled, "include": include, "exclude": exclude,
        "connect_timeout": _seconds(entry, DEFAULT_CONNECT_TIMEOUT, "connect_timeout_s"),
        "call_timeout": _seconds(entry, DEFAULT_CALL_TIMEOUT, "call_timeout_s", "timeout"), "ignored": ignored}
    if transport == "stdio":
        if isinstance(command, list):  # OpenCode: the whole argv
            argv = _strings(command, "command")
            command, args = (argv[0] if argv else ""), argv[1:]
        elif isinstance(command, dict):  # Zed: {path, args, env}
            args = _strings(command.get("args", []), "command.args")
            nested = _table(command.get("env", {}), "command.env")
            outer = _table(entry.get("env", entry.get("environment", {})), "env")
            entry = {**entry, "env": {**nested, **outer}}
            command = command.get("path")
            args = args + _strings(entry.get("args", []), "args") if "args" in entry else args
        else:
            args = _strings(entry.get("args", []), "args")
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError("command must be a non-empty string")
        env = _mapping(entry.get("env", entry.get("environment", {})), "env", workspace)
        cwd = entry.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise McpConfigError("cwd must be a string")
            cwd = interpolate(cwd, workspace=workspace)
            if workspace and not Path(cwd).is_absolute():
                cwd = str(Path(workspace) / cwd)
        elif workspace:
            cwd = workspace
        return ServerConfig(command=interpolate(command, workspace=workspace),
                            args=tuple(interpolate(a, workspace=workspace) for a in args), env=env, cwd=cwd, **base)
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise McpConfigError("url must start with http:// or https://")
    headers = _mapping(entry.get("headers", {}), "headers", workspace)
    return ServerConfig(url=interpolate(url, workspace=workspace), headers=headers, oauth=_oauth(entry, headers), **base)


_GENERIC_LABELS = {"www", "mcp", "api", "app", "server", "sse", "com", "net", "org", "io", "dev", "ai", "co"}


def name_from_url(url: str, taken: Iterable[str] = ()) -> str:
    """A server name from its address: `https://mcp.linear.app/sse` is `linear`; a number is added when it is taken."""
    parts = urlsplit(url if "//" in url else f"https://{url}")
    labels = [label for label in (parts.hostname or "").split(".") if label]
    useful = [label for label in labels if label.lower() not in _GENERIC_LABELS]
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", (useful or labels or ["mcp"])[0]).strip("-._") or "mcp"
    base = base[:28]
    if not base[0].isalnum():
        base = f"mcp-{base}"[:28]
    taken = set(taken)
    name, number = base, 2
    while name in taken:
        name = f"{base}-{number}"
        number += 1
    return name


def parse_all(entries: Any, *, workspace: str = "") -> tuple[dict[str, ServerConfig], dict[str, str]]:
    """`(valid servers, {name: error})` for the whole `[mcp.servers]` table."""
    servers: dict[str, ServerConfig] = {}
    errors: dict[str, str] = {}
    if not isinstance(entries, dict):
        return servers, errors
    for name, entry in entries.items():
        try:
            servers[name] = parse(name, entry, workspace=workspace)
        except McpConfigError as exc:
            errors[name] = str(exc)
    return servers, errors
