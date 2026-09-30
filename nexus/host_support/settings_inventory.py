"""Bounded Settings console inventory, validation and safe file mutations."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tomllib
import uuid
from pathlib import Path
from typing import Any

from ..agents import manager as _agent_manager
from ..agents.model import parse_frontmatter as parse_agent_frontmatter
from ..errors import ConfigError
from ..skills.frontmatter import parse_frontmatter as parse_skill_frontmatter
from ..util import redact_secrets
from .settings_scope import CATEGORIES, settings_target

MAX_ITEMS = 512
MAX_BODY = 256 * 1024
_LABELS = {"agents": "Agents", "skills": "Skills", "tools": "Tools", "hooks": "Hooks", "mcp": "MCP", "config": "Config", "soul": "Soul"}
_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*([=:])\s*([^\s,;]+)")


def _redact(body: str) -> str:
    def mask(match: re.Match[str]) -> str:
        value = match.group(3)
        marker = '"[redacted]"' if value.startswith('"') and value.endswith('"') else (
            "'[redacted]'" if value.startswith("'") and value.endswith("'") else "[redacted]"
        )
        return f"{match.group(1)}{match.group(2)}{marker}"

    return redact_secrets(_SECRET.sub(mask, body))


def _preserve_redactions(current: bytes, submitted: str) -> str:
    """Restore unchanged masked secret fields from the hash-checked old file."""
    try:
        old_text = current.decode("utf-8")
    except UnicodeError:
        return submitted
    previous: dict[str, list[str]] = {}
    for match in _SECRET.finditer(old_text):
        previous.setdefault(match.group(1).casefold(), []).append(match.group(3))

    def restore(match: re.Match[str]) -> str:
        value = match.group(3)
        if value not in {"[redacted]", '"[redacted]"', "'[redacted]'", "***"}:
            return match.group(0)
        values = previous.get(match.group(1).casefold(), [])
        if not values:
            return match.group(0)
        old = values.pop(0)
        return f"{match.group(1)}{match.group(2)}{old}"

    return _SECRET.sub(restore, submitted)


def _builtin_agents() -> dict[str, Path]:
    """Packaged agent definitions, ``name -> path`` (read-only defaults).

    Editing one writes ``<scope>/agents/<name>.md``; deleting that override
    restores the packaged default.
    """
    base = Path(_agent_manager.__file__).with_name(_agent_manager.DATA_DIR_NAME)
    try:
        return {
            path.stem: path
            for path in sorted(base.iterdir())
            if path.suffix == ".md" and path.is_file() and not path.name.startswith(".")
        }
    except OSError:
        return {}


def _rel(target: Any) -> str:
    return target.path.relative_to(target.root).as_posix()


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise ConfigError("settings file exceeds 256 KiB")
    return data


def _paths(root: Path, category: str, scope: str) -> list[tuple[str, Path]]:
    bases = {"agents": root / "agents", "skills": root / "skills", "tools": root / "tools"}
    if category in bases:
        base = bases[category]
        if not base.is_dir() or base.is_symlink():
            return []
        found = []
        try:
            for path in base.iterdir():
                if path.is_symlink():
                    continue
                if category == "agents" and path.is_file() and path.suffix == ".md" or category == "tools" and path.is_file() and path.suffix == ".py" and not path.name.startswith("_"):
                    found.append((path.stem, path))
                elif category == "skills" and path.is_dir():
                    skill = path / "SKILL.md"
                    if skill.is_file() and not skill.is_symlink():
                        found.append((path.name, skill))
                elif category == "skills" and path.is_file() and path.name == "SKILL.md":
                    found.append((path.parent.name, path))
        except OSError:
            return []
        return found
    config_name = "config.toml" if scope == "global" else "nexus.toml"
    fixed = {
        "hooks": ("hooks.toml", root / "hooks.toml"),
        "mcp": ("mcp.json", root / "mcp.json"),
        "soul": ("SOUL.md", root / "SOUL.md"),
        "config": (config_name, root / config_name),
    }
    name, path = fixed[category]
    return [(name, path)] if path.is_file() and not path.is_symlink() else []


def inventory(runtime: object, scope: str) -> dict[str, Any]:
    root, display = _root(runtime, scope)
    builtins = _builtin_agents()
    items = []
    counts = dict.fromkeys(CATEGORIES, 0)
    for category in CATEGORIES:
        for item_id, path in _paths(root, category, scope):
            if len(items) >= MAX_ITEMS:
                break
            try:
                body = _read_bounded(path).decode("utf-8")
            except (ConfigError, OSError, UnicodeError):
                continue
            summary = " ".join(_redact(body).split())[:200]
            counts[category] += 1
            items.append({"category": category, "id": item_id, "label": item_id, "summary": summary, "builtin": False, "rel_path": path.relative_to(root).as_posix(), "overrides_builtin": category == "agents" and item_id in builtins})
    present = {row["id"] for row in items if row["category"] == "agents"}
    for item_id, path in builtins.items():
        if item_id in present or len(items) >= MAX_ITEMS:
            continue
        try:
            body = _read_bounded(path).decode("utf-8")
        except (ConfigError, OSError, UnicodeError):
            continue
        counts["agents"] += 1
        items.append({"category": "agents", "id": item_id, "label": item_id, "summary": " ".join(body.split())[:200], "builtin": True, "rel_path": f"built-in/{item_id}.md"})
    return {"scope": scope, "root_display": display, "categories": [{"key": key, "label": _LABELS[key], "count": counts[key]} for key in CATEGORIES], "items": items}


def _root(runtime: object, scope: str) -> tuple[Path, str]:
    from .settings_scope import settings_root
    root, display = settings_root(runtime, scope)
    if root.is_symlink():
        raise ConfigError("settings root is a symlink")
    return root, display


def read(runtime: object, scope: str, category: str, item_id: str) -> dict[str, Any]:
    target = settings_target(runtime, scope, category, item_id)
    builtin = _builtin_agents().get(item_id) if category == "agents" else None
    if builtin is not None and not target.path.exists():
        # The packaged default. ``sha256`` is empty: saving creates the override.
        body = _read_bounded(builtin).decode("utf-8")
        return {"body": body, "rel_path": _rel(target), "builtin": True, "sha256": ""}
    data = _read_bounded(target.path)
    body = data.decode("utf-8")
    return {"body": _redact(body), "rel_path": _rel(target), "builtin": False, "sha256": hashlib.sha256(data).hexdigest(), "overrides_builtin": builtin is not None}


def _validate(category: str, item_id: str, body: str) -> None:
    if "\x00" in body or len(body.encode("utf-8")) > MAX_BODY:
        raise ConfigError("settings body is invalid or exceeds 256 KiB")
    if category == "agents":
        if parse_agent_frontmatter(body).name != item_id:
            raise ConfigError("agent frontmatter name must match its id")
    elif category == "skills":
        if parse_skill_frontmatter(body).name != item_id:
            raise ConfigError("skill frontmatter name must match its id")
    elif category == "tools":
        ast.parse(body, filename=f"{item_id}.py")
    elif category in {"hooks", "config"}:
        values = tomllib.loads(body)
        if category == "config":
            import msgspec

            from ..config.schema import ConfigV2
            msgspec.convert(values, type=ConfigV2, strict=False)
    elif category == "mcp":
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ConfigError("mcp.json root must be an object")
        servers = document.get("mcpServers", {})
        if not isinstance(servers, dict) or any(not isinstance(key, str) for key in servers):
            raise ConfigError("mcpServers must be an object with string keys")


def write(runtime: object, scope: str, category: str, item_id: str, body: str, expected_sha256: str | None) -> dict[str, Any]:
    target = settings_target(runtime, scope, category, item_id)
    exists = target.path.exists()
    old = _read_bounded(target.path) if exists else b""
    current = hashlib.sha256(old).hexdigest() if exists else ""
    if expected_sha256 is not None and expected_sha256 != current:
        return {"status": "conflict", "sha256": current}
    _validate(category, item_id, body)
    if exists:
        body = _preserve_redactions(old, body)
    data = body.encode("utf-8")
    target.path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = __import__("tempfile").mkstemp(prefix=f".{target.path.name}.", dir=target.path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        settings_target(runtime, scope, category, item_id)
        os.replace(temp_name, target.path)
        directory_fd = os.open(target.path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)
    return {"status": "written", "sha256": hashlib.sha256(data).hexdigest(), "loaded": [], "unloaded": [], "failed": [], "config_reloaded": category == "config"}


def delete(runtime: object, scope: str, category: str, item_id: str) -> dict[str, Any]:
    target = settings_target(runtime, scope, category, item_id)
    if not target.path.is_file() or target.path.is_symlink():
        raise ConfigError("settings item does not exist")
    trash_id = uuid.uuid4().hex
    entry = target.root / "trash" / "settings" / trash_id
    current = target.root
    for part in ("trash", "settings"):
        current = current / part
        if current.is_symlink():
            raise ConfigError("settings trash path crosses a symlink")
    entry.parent.mkdir(parents=True, exist_ok=True)
    if not entry.resolve(strict=False).is_relative_to(target.root.resolve()):
        raise ConfigError("settings trash path escapes its scope")
    entry.mkdir(mode=0o700)
    destination = entry / "item"
    os.replace(target.path, destination)
    metadata = {"rel_path": _rel(target), "scope": scope, "category": category, "id": item_id}
    metadata_path = entry / "meta.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return {"status": "trashed", "trash_id": trash_id}


_TABLE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(?:#.*)?$")


def set_toml_key(body: str, table: str, key: str, value: str) -> str:
    """Set ``[table] key = "value"`` in TOML text, leaving every other line alone."""
    literal = json.dumps(value)  # a JSON string is a valid TOML basic string
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if (m := _TABLE.match(line)) and m.group(1) == table), None)
    if start is None:
        prefix = body.rstrip("\n") + "\n\n" if body.strip() else ""
        return f"{prefix}[{table}]\n{key} = {literal}\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=")
    row = next((i for i in range(start + 1, end) if assignment.match(lines[i])), None)
    if row is None:
        lines.insert(start + 1, f"{key} = {literal}")
    else:
        lines[row] = f"{key} = {literal}"
    return "\n".join(lines) + "\n"


def set_default_agent(runtime: object, scope: str, name: str) -> dict[str, Any]:
    """Write ``[agent] name`` to the scope's config so new sessions start with ``name``."""
    agents = getattr(runtime, "agents", None)
    if agents is None:
        raise ConfigError("agent definitions are disabled")
    try:
        agents.refresh()
        resolved = agents.resolve(name, context="root").name
    except Exception as exc:  # AgentError or a stale index
        raise ConfigError(f"{name!r} is not a root agent") from exc
    target = settings_target(runtime, scope, "config", "config")
    old = _read_bounded(target.path) if target.path.exists() else b""
    try:
        text = old.decode("utf-8")
        current = tomllib.loads(text)
        if "config_version" not in current:
            if current:  # a legacy flat (v1) file cannot take an [agent] table
                raise ConfigError(f"{_rel(target)} is a v1 config; set config_version = 2 in Settings > Config first")
            text = "config_version = 2\n\n" + text.lstrip("\n")
        body = set_toml_key(text, "agent", "name", resolved)
        written = tomllib.loads(body).get("agent", {}).get("name")
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot update {_rel(target)}; fix it in Settings > Config") from exc
    if written != resolved:
        raise ConfigError(f"cannot update [agent] name in {_rel(target)}; edit it in Settings > Config")
    expected = hashlib.sha256(old).hexdigest() if old else ""
    if write(runtime, scope, "config", "config", body, expected)["status"] != "written":
        raise ConfigError(f"{_rel(target)} changed while saving; try again")
    effective = getattr(runtime, "default_root_agent", None)
    return {"name": resolved, "effective": effective() if callable(effective) else resolved,
            "scope": scope, "rel_path": _rel(target)}


def _toml_literal(value: Any) -> str:
    """Encode parsed TOML values for the uncommon inline/dotted-table fallback."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key)} = {_toml_literal(item)}" for key, item in value.items()) + "}"
    return value.isoformat()  # TOML date, time or datetime


def reset(runtime: object, scope: str, category: str) -> dict[str, Any]:
    """Restore scoped defaults, preserving removed files in settings trash."""
    if category not in (*CATEGORIES, "voice"):
        raise ConfigError("unknown settings category")
    root, _ = _root(runtime, scope)
    paths = _paths(root, category, scope) if category != "voice" else []
    if len(paths) > MAX_ITEMS:
        raise ConfigError("too many settings items to reset")
    if category == "agents":
        paths = [(name, path) for name, path in paths if name in _builtin_agents()]
    # Validate every target before mutating any of them.
    for name, _ in paths:
        settings_target(runtime, scope, category, name)
    trash_ids = []
    if category in {"agents", "voice"}:
        target = settings_target(runtime, scope, "config", "config")
        if target.path.exists():
            old = _read_bounded(target.path)
            text = old.decode("utf-8")
            document = tomllib.loads(text)
            expected = tomllib.loads(text)
            if category == "voice":
                expected.pop("voice", None)
            elif "agent" in document:
                expected["agent"].pop("name", None)
            lines = text.splitlines(keepends=True)
            table = ""
            kept = []
            for line in lines:
                match = _TABLE.match(line)
                if match:
                    table = match.group(1)
                if category == "voice" and (table == "voice" or table.startswith("voice.")):
                    continue
                if category == "agents" and table == "agent" and re.match(r"\s*name\s*=", line):
                    continue
                kept.append(line)
            body = "".join(kept)
            if tomllib.loads(body) != expected:
                body = "".join(f"{json.dumps(key)} = {_toml_literal(value)}\n" for key, value in expected.items())
                if tomllib.loads(body) != expected:
                    raise ConfigError("cannot reset settings without changing unrelated values")
            _validate("config", "config", body)
            if body != text:
                trash_ids.append(delete(runtime, scope, "config", "config")["trash_id"])
                write(runtime, scope, "config", "config", body, "")
    for name, _ in paths:
        trash_ids.append(delete(runtime, scope, category, name)["trash_id"])
    return {"status": "reset", "trash_ids": trash_ids}


async def dispatch_settings(command: Any, runtime: object) -> Any | None:
    from ..host import protocol as p
    if isinstance(command, p.AgentDefaultSet):
        return p.AgentDefaultSetResult(**set_default_agent(runtime, command.scope, command.name))
    if isinstance(command, p.SettingsReset):
        result = reset(runtime, command.scope, command.category)
        refresh = getattr(runtime, "refresh_voice_config", None)
        if callable(refresh):
            refresh()
        reload = getattr(getattr(runtime, "extensions", None), "reload", None)
        if callable(reload):
            await reload(trigger="settings")
        return p.SettingsResetResult(**result)
    if isinstance(command, p.SettingsInventory):
        import msgspec
        return msgspec.convert(inventory(runtime, command.scope), type=p.SettingsInventoryResult)
    if isinstance(command, p.SettingsRead):
        return p.SettingsReadResult(**read(runtime, command.scope, command.category, command.id))
    if isinstance(command, p.SettingsWrite):
        result = write(runtime, command.scope, command.category, command.id, command.body, command.expected_sha256)
        if result.get("status") == "written":
            if command.category == "config":
                refresh_voice = getattr(runtime, "refresh_voice_config", None)
                if callable(refresh_voice):
                    refresh_voice()
            manager = getattr(runtime, "extensions", None)
            reload_extensions = getattr(manager, "reload", None)
            if callable(reload_extensions):
                report = await reload_extensions(trigger="settings")
                result["loaded"] = list(getattr(report, "loaded", ()) or ())
                result["unloaded"] = list(getattr(report, "unloaded", ()) or ())
                result["failed"] = [str(row) for row in (getattr(report, "failed", ()) or ())]
        return p.SettingsWriteResult(**result)
    if isinstance(command, p.SettingsDelete):
        return p.SettingsDeleteResult(**delete(runtime, command.scope, command.category, command.id))
    return None


__all__ = ["delete", "dispatch_settings", "inventory", "read", "set_default_agent", "set_toml_key", "write"]
