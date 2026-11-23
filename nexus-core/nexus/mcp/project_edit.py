"""Editing a project's MCP files (`.mcp.json`, `.agents/mcp.json`) from the API.

Reading stays in `project.py` and stays strict. Writing is deliberately narrow: it adds, replaces or removes
one named server and leaves every other key as it was. The server is written into the wrapper that already
holds it, else `mcpServers` of `.mcp.json`. A file that cannot be read strictly (a symlink, comments,
duplicate keys, too large) is never rewritten: the user fixes it first. The file is replaced atomically and
keeps its permissions.

The files are committed to repositories. Secrets belong in `${env:NAME}` references, and the clients say so.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from ..config import _restore as restore
from .config import McpConfigError, validate
from .project import FILE_NAME, FILE_NAMES, WRAPPERS, ProjectMcp, _read, _reject_constant, _strict_object


class ProjectEditError(ValueError):
    """The edit was refused; the message says why."""


def _load(path: Path) -> dict[str, Any]:
    raw, exists, error = _read(path)
    if error is not None:
        raise ProjectEditError(f"{path.name}: {error}; fix it before editing from Nexus")
    if not exists:
        return {}
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProjectEditError(f"{path.name} is not valid JSON ({exc}); fix it before editing from Nexus") from exc
    if not isinstance(document, dict):
        raise ProjectEditError(f"{path.name} must contain a JSON object")
    return document


def _save(path: Path, document: dict[str, Any]) -> None:
    mode = stat.S_IMODE(path.lstat().st_mode) if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}-{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


_SECRET_NAME = re.compile(r"authorization|token|secret|key|password|credential", re.IGNORECASE)


def _check_no_literal_secrets(entry: dict[str, Any]) -> None:
    """The file may be committed: a credential must be an `${env:NAME}` reference, never the value itself."""
    tables = [("headers", entry.get("headers")), ("env", entry.get("env", entry.get("environment"))),
              ("oauth", {k: v for k, v in (entry.get("oauth") or {}).items() if k == "client_secret"}
               if isinstance(entry.get("oauth"), dict) else None)]
    for table, values in tables:
        for key, value in (values or {}).items() if isinstance(values, dict) else ():
            if _SECRET_NAME.search(key) and isinstance(value, str) and value and "${" not in value and "{env:" not in value:
                raise ProjectEditError(
                    f"{table}.{key} would be written to {FILE_NAME}, which may be committed: "
                    "use ${env:NAME} and set NAME in the environment Nexus runs in")


def locate(project: ProjectMcp, name: str) -> tuple[Path, dict[str, Any], str] | None:
    """`(file, document, wrapper)` of the file that defines `name`, or None."""
    for file_name in FILE_NAMES:
        path = Path(project.project_path) / file_name
        document = _load(path)
        for wrapper in WRAPPERS:
            block = document.get(wrapper)
            if isinstance(block, dict) and name in block:
                return path, document, wrapper
    return None


def edit(project: ProjectMcp, name: str, entry: dict[str, Any] | None, *, create: bool = False,
         keep_secrets: bool = False) -> str:
    """Add, replace or (entry None) remove one server; returns the file name written."""
    found = locate(project, name)
    if create and found is not None:
        raise ProjectEditError(f"a project MCP server named {name} already exists in {found[0].name}")
    if not create and found is None:
        raise KeyError(name)
    if entry is not None:
        if create:
            try:
                validate(name, entry, workspace=project.project_path)
            except McpConfigError as exc:
                raise ProjectEditError(str(exc)) from exc
            if name in project.reserved():
                raise ProjectEditError(f"name {name!r} is already used by a global MCP server; choose another name")
        else:
            entry = entry if keep_secrets else restore(entry, found[1][found[2]][name])
            try:
                validate(name, entry, workspace=project.project_path)
            except McpConfigError as exc:
                raise ProjectEditError(str(exc)) from exc
    if entry is not None:
        _check_no_literal_secrets(entry)
    if found is None:
        path = Path(project.project_path) / FILE_NAME
        document = _load(path)
        wrapper = next((w for w in WRAPPERS if isinstance(document.get(w), dict)), "mcpServers")
    else:
        path, document, wrapper = found
    block = document.setdefault(wrapper, {})
    if entry is None:
        del block[name]
    else:
        block[name] = entry
    _save(path, document)
    return path.name
