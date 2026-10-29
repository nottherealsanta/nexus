"""Single-file configuration primitives (PLAN.md A1).

Adapted from old Nexus nexus/config/layers.py and schema.py: nexus10 uses one
file and keeps invalid keys isolated rather than rejecting the entire config.
The daemon exposes this inventory through its configuration API.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import ParseError

from .paths import home

# Only settings whose semantics are already defined here are exposed. Later
# increments can extend this inventory without changing the file format.
DEFAULTS: dict[str, dict[str, Any]] = {
    "defaults": {"model": "", "effort": "", "agent": "", "permission": "allow"},
    "tools": {"disabled": []},
    "ui": {"detail": "medium", "reduced_motion": False},
}


SCHEMA: dict[str, dict[str, Any]] = {
    "ui.detail": {"type": "string", "enum": ["low", "medium", "high"]},
    "ui.reduced_motion": {"type": "boolean"},
    "defaults.model": {"type": "string"},
    "defaults.effort": {"type": "string"},
    "defaults.agent": {"type": "string"},
    "defaults.permission": {"type": "string", "enum": ["allow", "ask"]},
    "tools.disabled": {"type": "array", "items": {"type": "string"}},
    "mcp.servers": {"type": "object", "masked_fields": ["env", "headers", "credential fields"]},
}


def _valid(section: str, key: str, value: Any) -> bool:
    if section == "ui":
        return (isinstance(value, bool) if key == "reduced_motion"
                else isinstance(value, str) and value in ("low", "medium", "high"))
    if section == "defaults":
        if not isinstance(value, str):
            return False
        if key == "permission":
            return value in ("allow", "ask")
        if key == "model" and value:
            from .llmhub.hub import split_ref
            try:
                split_ref(value)
            except ValueError:
                return False
        return True
    if section == "tools" and key == "disabled":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return False
        from .runtime import Runtime
        try:
            Runtime.check_tool_names(value)
        except ValueError:
            return False
        return True
    return False


MASK = "********"


def _mask(value: Any, *, secret: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _mask(item, secret=secret or key.lower() in {
            "env", "headers", "api_key", "token", "password", "secret", "authorization",
        }) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item, secret=secret) for item in value]
    return MASK if secret and isinstance(value, str) else value


def _restore(value: Any, original: Any) -> Any:
    if value == MASK:
        if not isinstance(original, str):
            raise ValueError("masked secret has no existing value")
        return original
    if isinstance(value, dict):
        old = original if isinstance(original, dict) else {}
        return {key: _restore(item, old.get(key)) for key, item in value.items()}
    if isinstance(value, list):
        old = original if isinstance(original, list) else []
        return [_restore(item, old[i] if i < len(old) else None) for i, item in enumerate(value)]
    return value


class ConfigConflict(ValueError):
    """The file no longer matches the revision supplied by the caller."""


class ConfigFile:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else home() / "config.toml"

    def _read(self) -> tuple[bytes, Any, list[str]]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raw = b""
        try:
            doc = tomlkit.parse(raw.decode("utf-8"))
        except (UnicodeDecodeError, ParseError):
            return raw, tomlkit.document(), ["config file is not valid UTF-8 TOML"]
        return raw, doc, []

    def read(self) -> dict[str, Any]:
        raw, doc, errors = self._read()
        values = copy.deepcopy(DEFAULTS)
        for section, keys in DEFAULTS.items():
            table = doc.get(section, {})
            if not isinstance(table, dict):
                errors.append(f"{section}: expected a table")
                continue
            for unknown in table.keys() - keys.keys():
                errors.append(f"{section}.{unknown}: unknown setting")
            for key in keys:
                if key in table:
                    value = table[key]
                    if _valid(section, key, value):
                        values[section][key] = value.unwrap() if hasattr(value, "unwrap") else value
                    else:
                        errors.append(f"{section}.{key}: invalid value")
        mcp = doc.get("mcp", {})
        if isinstance(mcp, dict) and isinstance(mcp.get("servers", {}), dict):
            values["mcp"] = {"servers": _mask(dict(mcp.get("servers", {})))}
        else:
            errors.append("mcp.servers: expected a table")
        return {"revision": hashlib.sha256(raw).hexdigest(), "values": values,
                "defaults": copy.deepcopy(DEFAULTS), "schema": copy.deepcopy(SCHEMA), "errors": errors}

    def patch(self, revision: str, set_values: dict[str, Any], unset: list[str]) -> dict[str, Any]:
        raw, doc, errors = self._read()
        if hashlib.sha256(raw).hexdigest() != revision:
            raise ConfigConflict("config revision changed")
        if errors:
            raise ValueError("cannot edit invalid TOML")
        set_values = dict(set_values)
        mcp_value = set_values.pop("mcp.servers", None)
        if mcp_value is not None:
            if not isinstance(mcp_value, dict) or not all(isinstance(v, dict) for v in mcp_value.values()):
                raise ValueError("mcp.servers: expected named server objects")
            mcp_value = _restore(mcp_value, doc.get("mcp", {}).get("servers", {}))
        for path, value in set_values.items():
            parts = path.split(".")
            if len(parts) != 2 or parts[0] not in DEFAULTS or parts[1] not in DEFAULTS[parts[0]] or not _valid(*parts, value):
                raise ValueError(f"invalid setting: {path}")
        for path in unset:
            parts = path.split(".")
            if len(parts) != 2 or parts[0] not in DEFAULTS or parts[1] not in DEFAULTS[parts[0]]:
                raise ValueError(f"invalid setting: {path}")
        for path, value in set_values.items():
            section, key = path.split(".")
            if section not in doc:
                doc[section] = tomlkit.table()
            if not isinstance(doc[section], dict):
                raise TypeError(f"{section}: expected a table")
            doc[section][key] = value
        for path in unset:
            section, key = path.split(".")
            if isinstance(doc.get(section), dict):
                doc[section].pop(key, None)
        if mcp_value is not None:
            if "mcp" not in doc:
                doc["mcp"] = tomlkit.table()
            doc["mcp"]["servers"] = mcp_value
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.parent / f".config-{secrets.token_hex(6)}.tmp"
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(tomlkit.dumps(doc))
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
        return self.read()


def import_mcp(path: Path, config: ConfigFile | None = None) -> list[str]:
    """Import the supported MCP wrapper maps, retaining each dialect's entry."""
    source = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise TypeError("MCP document must be an object")
    entries: dict[str, Any] = {}
    for wrapper in ("servers", "mcpServers", "mcp", "context_servers"):
        servers = source.get(wrapper, {})
        if not isinstance(servers, dict):
            raise TypeError(f"{wrapper} must be an object")
        for name, entry in servers.items():
            if name in entries:
                raise ValueError(f"duplicate MCP server: {name}")
            if not name or not isinstance(entry, dict):
                raise ValueError("MCP server entries must be named objects")
            entries[name] = entry
    if not entries:
        raise ValueError("no MCP servers found")
    config = config or ConfigFile()
    raw, doc, errors = config._read()
    if errors:
        raise ValueError("cannot edit invalid TOML")
    if "mcp" not in doc:
        doc["mcp"] = tomlkit.table()
    mcp = doc["mcp"]
    if not isinstance(mcp, dict):
        raise TypeError("mcp must be a table")
    if "servers" not in mcp:
        mcp["servers"] = tomlkit.table()
    servers = mcp["servers"]
    if not isinstance(servers, dict):
        raise TypeError("mcp.servers must be a table")
    for name, entry in entries.items():
        if name in servers:
            raise ValueError(f"MCP server already exists: {name}")
        try:
            servers[name] = entry
        except (TypeError, ValueError) as exc:
            raise ValueError(f"MCP server {name} cannot be represented in TOML") from exc
    rendered = tomlkit.dumps(doc)
    if config._read()[0] != raw:
        raise ConfigConflict("config revision changed")
    config.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = config.path.parent / f".config-{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(temp, config.path)
    finally:
        temp.unlink(missing_ok=True)
    return list(entries)
