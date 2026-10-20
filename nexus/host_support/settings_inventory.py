"""Bounded Settings console inventory, validation and safe file mutations."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tomllib
import uuid
from collections.abc import Mapping
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
        from ..ext.manager import _strict_json_object
        document = _strict_json_object(body)
        if not isinstance(document, dict):
            raise ConfigError("mcp.json root must be an object")
        from ..ext.manager import _MCP_SERVER_MAPS
        from ..mcp.client import normalize_server_entry, server_enabled
        from ..mcp.errors import MCPConfigError
        for key in _MCP_SERVER_MAPS:
            servers = document.get(key, {})
            if not isinstance(servers, dict) or any(not isinstance(name, str) for name in servers):
                raise ConfigError(f"{key} must be an object with string keys")
            for name, entry in servers.items():
                try:
                    server_enabled(name, entry if isinstance(entry, dict) else {})
                    normalize_server_entry(name, entry)
                except MCPConfigError as exc:
                    raise ConfigError(str(exc)) from None


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
    _atomic_write(runtime, scope, category, item_id, target, data)
    return {"status": "written", "sha256": hashlib.sha256(data).hexdigest(), "loaded": [], "unloaded": [], "failed": [], "config_reloaded": category == "config"}


def _atomic_write(runtime: object, scope: str, category: str, item_id: str, target: Any, data: bytes,
                  *, unchanged_from: bytes | None | bool = False) -> None:
    """Replace ``target.path`` with ``data`` via a fsynced temp file in the same directory.

    The path policy is re-checked right before the rename. ``unchanged_from`` (the bytes read
    earlier, or ``None`` for "did not exist") additionally refuses the replace when the file
    changed in between; the default ``False`` skips that check.
    """
    target.path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = __import__("tempfile").mkstemp(prefix=f".{target.path.name}.", dir=target.path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        settings_target(runtime, scope, category, item_id)
        if unchanged_from is not False:
            now = _read_bounded(target.path) if target.path.exists() else None
            if now != unchanged_from:
                raise _ChangedOnDisk
        os.replace(temp_name, target.path)
        directory_fd = os.open(target.path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)


class _ChangedOnDisk(Exception):
    """The settings file changed between the read and the atomic replace."""


def patch_mcp_loading(body: str, server: str, mode: str) -> str:
    if mode not in ("search", "all"):
        raise ConfigError("Invalid MCP tool loading mode")
    return _patch_mcp_value(body, server, "tool_loading", mode)


def patch_mcp_enabled(body: str, server: str, enabled: bool) -> str:
    if not isinstance(enabled, bool):
        raise ConfigError("MCP enabled must be a bool")
    from ..ext.manager import _strict_json_object
    try:
        document = _strict_json_object(body)
    except (ValueError, TypeError):
        document = {}
    entry = next((block.get(server) for block in (document.get(key) for key in _server_maps(document))
                  if isinstance(block, dict) and isinstance(block.get(server), dict)), None) if isinstance(document, dict) else None
    if isinstance(entry, dict) and "disabled" in entry and "enabled" not in entry:
        return _patch_mcp_value(body, server, "disabled", not enabled)  # Claude Desktop / Cline dialect
    return _patch_mcp_value(body, server, "enabled", enabled)


def _server_maps(document: dict) -> list[str]:
    from ..ext.manager import _MCP_SERVER_MAPS
    return [key for key in _MCP_SERVER_MAPS if key in document]


def _patch_mcp_value(body: str, server: str, key: str, new_value: object) -> str:
    """Locate one JSONC object and replace/insert only its requested value.

    A token scan preserves every original byte around the edit. Strict parsing
    rejects duplicate keys and ambiguous aliases before any mutation.
    """
    from ..ext.manager import _strict_json_object

    try:
        document = _strict_json_object(body)
    except (ValueError, TypeError) as exc:
        raise ConfigError("Cannot safely locate MCP server: invalid or ambiguous JSONC") from exc
    if not isinstance(document, dict):
        raise ConfigError("Cannot safely locate MCP server: root must be an object")
    aliases = [key for key in _server_maps(document)
               if isinstance(document[key], dict) and isinstance(document[key].get(server), dict)]
    if len(aliases) != 1:
        raise ConfigError(f"Cannot safely locate MCP server {server!r}")
    pattern = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,]+')
    tokens = [(match.group(), match.start(), match.end()) for match in pattern.finditer(body)
              if not match.group().isspace() and not match.group().startswith(("//", "/*"))]
    spans = {}
    cursor = 0

    def value(path):
        nonlocal cursor
        token, start, end = tokens[cursor]
        cursor += 1
        if token in ("{", "["):
            close = "}" if token == "{" else "]"
            while tokens[cursor][0] != close:
                if tokens[cursor][0] == ",":
                    cursor += 1
                    continue
                if token == "{":
                    key = json.loads(tokens[cursor][0])
                    cursor += 2  # key and colon (syntax was already validated)
                    value((*path, key))
                else:
                    value((*path, "[]"))
            end = tokens[cursor][2]
            cursor += 1
        spans[path] = (start, end)

    value(())
    path = (aliases[0], server)
    existing = spans.get((*path, key))
    if existing:
        start, end = existing
        return body[:start] + json.dumps(new_value) + body[end:]
    start, _ = spans[path]
    # Inserting immediately after { handles trailing comments and commas without
    # moving them. Existing properties still keep their exact formatting.
    comma = "," if document[aliases[0]][server] else ""
    return body[:start+1] + json.dumps(key) + ': ' + json.dumps(new_value) + comma + body[start+1:]


def set_mcp_loading(runtime, scope, server, mode, expected_sha256):
    return _set_mcp_value(runtime, scope, server, mode, expected_sha256, patch_mcp_loading)


def set_mcp_enabled(runtime, scope, server, enabled, expected_sha256):
    return _set_mcp_value(runtime, scope, server, enabled, expected_sha256, patch_mcp_enabled)


def _set_mcp_value(runtime, scope, server, value, expected_sha256, patch):
    target = settings_target(runtime, scope, "mcp", "mcp.json")
    old = _read_bounded(target.path)
    current = hashlib.sha256(old).hexdigest()
    if expected_sha256 != current:
        return {"status": "conflict", "sha256": current}
    body = patch(old.decode("utf-8"), server, value)
    return write(runtime, scope, "mcp", "mcp.json", body, expected_sha256)


# ---------------------------------------------------------------------------
# Add one MCP server (SettingsMcpAdd)
# ---------------------------------------------------------------------------

MCP_ADD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MAX_MCP_ENTRY = 16 * 1024
#: Text the Settings editor shows instead of a stored secret; never a real value.
_MASKED = ("[redacted]", "***")
_FOLD = re.compile(r"[.-]")
_JSON_TOKEN = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,]+')


class McpServerExists(ConfigError):
    """The server name is already defined in the target ``mcp.json``."""


class _AnyEnv(Mapping):
    """Every ``${env:NAME}`` "exists": validation must not depend on the host's environment."""

    def __getitem__(self, key: str) -> str:
        return "x"

    def __contains__(self, key: object) -> bool:
        return True

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _walk_values(value: Any):
    """Every string *value* (never a key) at any depth, including in unknown or mistyped fields."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_values(item)


def _draft_secrets(entry: dict) -> list[str]:
    """Every string value in a draft entry, plus URL userinfo/query parts.

    The draft is untrusted free-form JSON: a secret can sit in a field the loader rejects (unknown
    key, wrong type), and the loader's error may echo it, so nothing is assumed to be harmless.
    """
    from urllib.parse import parse_qsl, urlsplit
    found = list(_walk_values(entry))
    for value in list(found):
        if "://" not in value:
            continue
        try:
            parts = urlsplit(value)
            found += [parts.username or "", parts.password or "", *(item for _, item in parse_qsl(parts.query))]
        except ValueError:
            continue
    return [value for value in dict.fromkeys(found) if value]


def validate_mcp_add(name: object, entry: object, workspace: object = None) -> tuple[dict, list[str]]:
    """Fully validate a draft ``(name, entry)`` exactly as the loader would; returns ``(entry, secrets)``.

    ``envFile``/``env_file`` is type-checked but never read (it is removed before parsing), every
    ``${env:VAR}`` counts as set, and the error text never carries a draft secret value.
    """
    from ..mcp.client import parse_server_config, redact_secrets, server_enabled
    from ..mcp.errors import MCPConfigError
    if not isinstance(name, str) or not MCP_ADD_NAME.fullmatch(name):
        raise ConfigError("MCP server name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
    if not isinstance(entry, dict) or not entry or any(not isinstance(key, str) for key in entry):
        raise ConfigError("MCP server entry must be a non-empty object")
    try:
        text = json.dumps(entry, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ConfigError("MCP server entry must be plain JSON") from None
    if len(text.encode("utf-8")) > MAX_MCP_ENTRY:
        raise ConfigError("MCP server entry exceeds 16 KiB")
    secrets = _draft_secrets(entry)
    try:
        for string in _walk_strings(entry):
            if any(mark in string for mark in _MASKED):
                raise ConfigError("MCP entry contains a masked placeholder; enter the real value or a ${env:VAR} reference")
            if "${input:" in string:
                raise ConfigError("${input:…} prompts are not supported; use ${env:VAR}")
        rest = dict(entry)
        present = [key for key in ("env_file", "envFile") if key in rest]
        if len(present) > 1:
            raise ConfigError("set only one of env_file, envFile")
        for key in present:
            if not isinstance(rest.pop(key), str):
                raise ConfigError("envFile must be a string")
        server_enabled(name, rest)
        workspace_arg = workspace if isinstance(workspace, (str, Path)) else None
        parse_server_config(name, rest, environ=_AnyEnv(), workspace=workspace_arg, home="/")
    except (ConfigError, MCPConfigError) as exc:
        raise ConfigError(redact_secrets(str(exc), secrets, min_length=1)) from None
    return entry, secrets


def _json_tokens(body: str) -> list[tuple[str, int, int]]:
    return [(match.group(), match.start(), match.end()) for match in _JSON_TOKEN.finditer(body)
            if not match.group().isspace() and not match.group().startswith(("//", "/*"))]


def _object_spans(tokens: list[tuple[str, int, int]]) -> dict[tuple, tuple[int, int]]:
    """``path -> (open_index, close_index)`` of the root object and its direct object values."""
    spans: dict[tuple, tuple[int, int]] = {}
    cursor = 0

    def value(path: tuple) -> None:
        nonlocal cursor
        index = cursor
        token = tokens[cursor][0]
        cursor += 1
        if token in ("{", "["):
            close = "}" if token == "{" else "]"
            while tokens[cursor][0] != close:
                if tokens[cursor][0] == ",":
                    cursor += 1
                    continue
                if token == "{":
                    key = json.loads(tokens[cursor][0])
                    cursor += 2  # key and colon (syntax was already validated)
                    value((*path, key))
                else:
                    value((*path, "[]"))
            if token == "{" and len(path) <= 1:
                spans[path] = (index, cursor)
            cursor += 1

    value(())
    return spans


def _insert_property(body: str, tokens: list[tuple[str, int, int]], open_index: int, close_index: int,
                     key: str, value: Any, nl: str) -> str:
    """Append ``"key": value`` as the last property of one object, touching no other byte.

    A multi-line object gets the new property on its own line, indented like the first existing
    one; a one-line object stays on one line. A trailing comma, if the file uses one, is kept.
    """
    open_token, close_token = tokens[open_index], tokens[close_index]
    prev = tokens[close_index - 1]
    empty = close_index - open_index == 1
    trailing = prev[0] == ","
    multiline = "\n" in body[open_token[1]:close_token[2]]
    line_start = body.rfind("\n", 0, close_token[1]) + 1
    before_close = body[line_start:close_token[1]]
    own_line = multiline and not before_close.strip()
    lead = before_close if own_line else re.match(r"[ \t]*", body[line_start:]).group()
    rendered = json.dumps(value, ensure_ascii=False, indent=2 if multiline else None)
    prop = json.dumps(key) + ": " + rendered
    if not multiline:
        if empty:
            return body[:prev[2]] + prop + body[prev[2]:]
        text = (" " + prop + ",") if trailing else ("," + " " + prop)
        return body[:prev[2]] + text + body[prev[2]:]
    child = lead + "  "
    if not empty:
        first = tokens[open_index + 1]
        first_line = body.rfind("\n", 0, first[1]) + 1
        indent = body[first_line:first[1]]
        if not indent.strip():
            child = indent
    lines = prop.split("\n")
    text = lines[0] + "".join(nl + child + line for line in lines[1:])
    if own_line:
        insert = child + text + ("," if trailing else "") + nl
        edits = [(line_start, insert)]
        if not trailing and not empty:
            edits.append((prev[2], ","))
    else:
        edits = [(prev[2], ("" if trailing or empty else ",") + nl + child + text + ("," if trailing else ""))]
    for position, piece in sorted(edits, reverse=True):
        body = body[:position] + piece + body[position:]
    return body


def insert_mcp_server(body: str, name: str, entry: dict) -> tuple[str, str, str]:
    """Insert one server into ``mcp.json`` text; returns ``(new_body, server_map_key, note)``.

    Every other byte, comment and trailing comma is kept. The server goes into the first existing
    map in ``_MCP_SERVER_MAPS`` order (a new ``"mcpServers"`` when there is none). An existing name,
    in any map, raises :class:`McpServerExists`; a name that differs only in ``.``/``-`` versus ``_``
    is refused as a tool-name collision. Siblings are never validated: an already invalid server
    in the file does not block the add and is not modified.
    """
    from ..ext.manager import _MCP_SERVER_MAPS, _strict_json_object
    if not body.strip():
        return json.dumps({"mcpServers": {name: entry}}, indent=2, ensure_ascii=False) + "\n", "mcpServers", ""
    refuse = "mcp.json is not valid JSONC or has duplicate keys; fix it with Edit… first"
    try:
        document = _strict_json_object(body)
    except (ValueError, TypeError, RecursionError):
        raise ConfigError(refuse) from None
    if not isinstance(document, dict):
        raise ConfigError("mcp.json root must be an object")
    maps = [key for key in _MCP_SERVER_MAPS if key in document]
    if any(not isinstance(document[key], dict) for key in maps):
        raise ConfigError("an mcp.json server map is not an object; fix it with Edit… first")
    folded = _FOLD.sub("_", name)
    for key in maps:
        for existing in document[key]:
            if existing == name:
                raise McpServerExists(f"MCP server {name!r} already exists in {key}")
            if _FOLD.sub("_", existing) == folded:
                raise ConfigError(f"MCP server {name!r} collides with {existing[:64]!r}: tool names fold '.' and '-' to '_'")
    key = maps[0] if maps else "mcpServers"
    note = f"mcp.json has several server maps ({', '.join(maps)}); added to {key}" if len(maps) > 1 else ""
    nl = "\r\n" if "\r\n" in body else "\n"
    try:
        tokens = _json_tokens(body)
        spans = _object_spans(tokens)
        if maps:
            result = _insert_property(body, tokens, *spans[(key,)], name, entry, nl)
        else:
            result = _insert_property(body, tokens, *spans[()], key, {name: entry}, nl)
        expected = json.loads(json.dumps(document))
        expected.setdefault(key, {})[name] = entry
        if _strict_json_object(result) != json.loads(json.dumps(expected)):
            raise ValueError("insert verification failed")
    except (IndexError, KeyError, ValueError, TypeError, RecursionError):
        raise ConfigError("cannot safely insert into this mcp.json; use Edit… instead") from None
    return result, key, note


def _check_global_root(runtime: object, target: Any) -> None:
    """The global add must land where the host reads (``$NEXUS_HOME`` vs ``~/.nexus``)."""
    from ..config.paths import nexus_home
    files = getattr(getattr(runtime, "extensions", None), "mcp_config_files", None)
    try:
        read_root = next((Path(path).parent for scope, path, _ in files() if scope == "global"), None) if callable(files) else None
        same = (read_root or nexus_home()).resolve() == target.root.resolve()
    except (OSError, StopIteration, TypeError, ValueError):
        same = False
    if not same:
        raise ConfigError("the host reads global MCP servers from a different folder than Settings writes (NEXUS_HOME is set); "
                          "unset NEXUS_HOME or edit $NEXUS_HOME/mcp.json by hand")


_ENV_REF = re.compile(r"\$\{env:[A-Za-z_][A-Za-z0-9_]*\}")


def _literal_secret(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not _ENV_REF.fullmatch(value)


def mcp_add_preview(scope: str, display: str, name: str, entry: dict, secrets: list[str]) -> tuple[list[str], str]:
    """Masked confirmation ``(lines, transport)`` for a validated draft.

    No env/header value and no URL credential is ever included; terminal-control escaping is the
    surface's job. ``display`` is the scope's root as shown to the user (``~/.nexus``).
    """
    from urllib.parse import urlsplit

    from ..mcp.client import normalize_server_entry, redact_secrets, server_enabled
    from ..mcp.errors import MCPConfigError
    rest = {key: value for key, value in entry.items() if key not in ("env_file", "envFile")}
    try:
        data, _ = normalize_server_entry(name, rest)
        enabled = server_enabled(name, rest)
    except MCPConfigError as exc:
        raise ConfigError(redact_secrets(str(exc), secrets, min_length=1)) from None
    transport = str(data.get("transport", "stdio"))
    lines = [f"Name: {name}", f"File: {display}/mcp.json ({scope})", f"Transport: {transport}"]
    literal = any(_literal_secret(value) for key in ("env", "headers") if isinstance(data.get(key), dict) for value in data[key].values())
    if transport == "stdio":
        command = str(data.get("command") or "")
        args = data.get("args") or ()
        lines.append(f"Command: {command.replace(chr(92), '/').rsplit('/', 1)[-1]} · {len(args)} argument{'' if len(args) == 1 else 's'}")
    else:
        url = str(data.get("url") or "")
        try:
            parts = urlsplit(url)
            shown = f"{parts.scheme}://{parts.hostname or ''}{':' + str(parts.port) if parts.port else ''}{parts.path}"
            literal = literal or bool(parts.username or parts.password or parts.query)
        except ValueError:
            shown = "(unreadable URL)"
        lines.append(f"URL: {shown}")
    for key, label in (("env", "Environment"), ("headers", "Headers")):
        names = [str(item) for item in (data.get(key) or {})]
        if names:
            lines.append(f"{label}: {', '.join(names[:12])}" + (f" +{len(names) - 12}" if len(names) > 12 else "") + " (values hidden)")
    lines += [f"Enabled: {'yes' if enabled else 'no'}", f"Tool loading: {data.get('tool_loading', 'search')}"]
    if literal:
        lines.append("Literal secret values are saved as plain text in " + (
            "this project file, which may be committed. Prefer ${env:VAR}." if scope == "project" else "mcp.json. Prefer ${env:VAR}."))
    lines.append("Nexus starts this command now and in new sessions." if transport == "stdio"
                 else "Nexus connects to this URL now and in new sessions.")
    return lines, transport


def _add_mcp(runtime: object, scope: str, name: str, entry: dict, expected_sha256: str | None,
             dry_run: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Write one new server (or only check it); returns the result fields and the draft's secrets."""
    from ..mcp.client import redact_secrets
    target = settings_target(runtime, scope, "mcp", "mcp.json")
    workspace = getattr(runtime, "workspace", None)
    entry, secrets = validate_mcp_add(name, entry, workspace)
    try:
        if scope == "global":
            _check_global_root(runtime, target)
        exists = target.path.exists()
        if exists and not target.path.is_file():
            raise ConfigError("mcp.json is not a regular file")
        if scope == "project" and not exists and os.path.lexists(target.root.parent / ".nexus" / "mcp.json"):
            return {"status": "legacy", "message": "This project's servers are in the legacy .nexus/mcp.json. Adding here would "
                    "hide them: copy that file to .agents/mcp.json (Settings → MCP → New file), then add."}, secrets
        old = _read_bounded(target.path) if exists else b""
        current = hashlib.sha256(old).hexdigest() if exists else ""
        if expected_sha256 is not None and expected_sha256 != current:
            return {"status": "conflict", "sha256": current, "message": "mcp.json changed on disk; reload and try again"}, secrets
        try:
            text = old.decode("utf-8")
        except UnicodeError:
            raise ConfigError("mcp.json is not valid UTF-8") from None
        try:
            body, _, note = insert_mcp_server(text, name, entry)
        except McpServerExists as exc:
            return {"status": "exists", "sha256": current, "message": str(exc)}, secrets
        data = body.encode("utf-8")
        if len(data) > MAX_BODY:
            raise ConfigError("mcp.json would exceed 256 KiB")
        if dry_run:
            lines, transport = mcp_add_preview(scope, target.display, name, entry, secrets)
            return {"status": "preview", "sha256": current, "message": note, "transport": transport, "lines": lines}, secrets
        try:
            _atomic_write(runtime, scope, "mcp", "mcp.json", target, data, unchanged_from=old if exists else None)
        except _ChangedOnDisk:
            return {"status": "conflict", "sha256": hashlib.sha256(_read_bounded(target.path)).hexdigest() if target.path.exists() else "",
                    "message": "mcp.json changed on disk; reload and try again"}, secrets
    except ConfigError as exc:
        raise ConfigError(redact_secrets(str(exc), secrets, min_length=1)) from None
    message = " · ".join(part for part in (f"{name} added to {_rel(target)}", note) if part)
    return {"status": "written", "sha256": hashlib.sha256(data).hexdigest(), "message": message}, secrets


def add_mcp_server(runtime: object, scope: str, name: str, entry: dict, expected_sha256: str | None = None) -> dict[str, Any]:
    """Insert one new server into the scope's ``mcp.json`` (see :func:`_add_mcp`); no hot reload here."""
    return _add_mcp(runtime, scope, name, entry, expected_sha256)[0]


def _mcp_shadow_note(runtime: object, scope: str, name: str) -> str:
    """After a reload: say when the same name in the other scope shadows or is shadowed."""
    from ..ext.manager import _strict_json_object
    files = getattr(getattr(runtime, "extensions", None), "mcp_config_files", None)
    if not callable(files):
        return ""
    other = "project" if scope == "global" else "global"
    try:
        path = next(Path(path) for item, path, _ in files() if item == other)
        document = _strict_json_object(_read_bounded(path).decode("utf-8"))
        names = {key for block in _server_maps(document) for key in (document[block] if isinstance(document[block], dict) else {})}
    except Exception:  # noqa: BLE001 - the note is best-effort
        return ""
    if name not in names:
        return ""
    return (f"A project server named {name!r} shadows this global one in this workspace." if scope == "global"
            else f"This project server replaces the global {name!r} in this workspace.")


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


def _literal(value: Any) -> str:
    """A TOML literal for a string, bool, int or list of those."""
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    return _toml_literal(value)


def set_toml_key(body: str, table: str, key: str, value: Any) -> str:
    """Set ``[table] key = value`` in TOML text, leaving every other line alone.

    ``value`` is a string, bool, int or list of those. ``None`` removes the key
    (and does nothing when it is absent).
    """
    if value is None:
        return remove_toml_key(body, table, key)
    literal = _literal(value)
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


def remove_toml_key(body: str, table: str, key: str) -> str:
    """Delete a one-line ``[table] key = ...`` assignment; other lines are untouched."""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if (m := _TABLE.match(line)) and m.group(1) == table), None)
    if start is None:
        return body
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=")
    row = next((i for i in range(start + 1, end) if assignment.match(lines[i])), None)
    if row is None:
        return body
    del lines[row]
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
            try:
                matches = tomllib.loads(body) == expected
            except tomllib.TOMLDecodeError:
                matches = False
            if not matches:
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
    if isinstance(command, (p.SettingsMcpLoadingSet, p.SettingsMcpEnabledSet)):
        if isinstance(command, p.SettingsMcpEnabledSet):
            result = set_mcp_enabled(runtime, command.scope, command.server, command.enabled, command.expected_sha256)
        else:
            result = set_mcp_loading(runtime, command.scope, command.server, command.mode, command.expected_sha256)
        if result.get("status") == "written":
            reload = getattr(getattr(runtime, "extensions", None), "reload", None)
            if callable(reload):
                await reload(trigger="settings")
        return p.SettingsWriteResult(**result)
    if isinstance(command, p.SettingsMcpAdd):
        from ..mcp.client import redact_secrets as redact_draft
        try:
            result, secrets = _add_mcp(runtime, command.scope, command.name, command.entry, command.expected_sha256, command.dry_run)
        except ConfigError as exc:
            draft = _draft_secrets(command.entry) if isinstance(command.entry, dict) else []
            raise ConfigError(redact_draft(str(exc), draft, min_length=1)) from None
        if result.get("status") == "written":
            reload_extensions = getattr(getattr(runtime, "extensions", None), "reload", None)
            if callable(reload_extensions):
                report = await reload_extensions(trigger="settings")
                result["loaded"] = list(getattr(report, "loaded", ()) or ())
                result["unloaded"] = list(getattr(report, "unloaded", ()) or ())
                result["failed"] = [redact_draft(str(row), secrets, min_length=1) for row in (getattr(report, "failed", ()) or ())]
            note = _mcp_shadow_note(runtime, command.scope, command.name)
            result["message"] = " · ".join(part for part in (result.get("message", ""), note) if part)
        return p.SettingsWriteResult(**result)
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


__all__ = ["add_mcp_server", "delete", "dispatch_settings", "insert_mcp_server", "inventory", "read", "remove_toml_key",
           "set_default_agent", "set_toml_key", "validate_mcp_add", "write"]
