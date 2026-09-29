"""Bounded workspace path search respecting host read-deny boundaries."""
from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

_MAX_FILE_SEARCH_QUERY = 256
_MAX_FILE_SEARCH_LIMIT = 100
_MAX_FILE_SEARCH_ENTRIES = 50_000
_GIT_LIST_TIMEOUT = 2.0
_GIT_LIST_MAX_BYTES = 16 * 1024 * 1024
_GIT_LIST_TTL = 3.0
_git_cache: dict[str, tuple[float, tuple[frozenset[str], frozenset[str]] | None]] = {}


def search_files(
    runtime: object,
    query: str,
    limit: int = 30,
    *,
    max_entries: int = _MAX_FILE_SEARCH_ENTRIES,
) -> list[str]:
    """Return bounded, visible workspace-relative file path matches.

    The scan never opens files. It skips hidden entries, all symlinks and,
    in a Git work tree, anything Git ignores; it enforces configured read-deny
    roots. Results rank basename-prefix matches, then basename matches, then
    path matches, shallower paths first, as stable POSIX paths.
    """
    if not isinstance(query, str) or len(query) > _MAX_FILE_SEARCH_QUERY:
        raise ValueError("file search query must be a string of at most 256 characters")
    if (
        "\x00" in query
        or query.startswith(("/", "\\"))
        or "\\" in query
        or any(part == ".." for part in query.split("/"))
    ):
        raise ValueError("file search query must be workspace-relative")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("file search limit must be a non-negative integer")
    limit = min(limit, _MAX_FILE_SEARCH_LIMIT)
    if limit == 0:
        return []

    workspace = Path(runtime.workspace).resolve()
    denied_roots = _file_search_denied_roots(runtime)
    visible = _git_visible(workspace)
    needle = query.casefold()
    found: list[str] = []
    visited = 0
    stack = [workspace]
    while stack and visited < max_entries:
        current = stack.pop()
        try:
            if current.is_symlink():
                continue
            real_current = current.resolve()
            if not real_current.is_relative_to(workspace) or _path_is_denied(
                real_current, denied_roots
            ):
                continue
            with os.scandir(current) as iterator:
                entries = sorted(
                    itertools.islice(iterator, max_entries - visited),
                    key=lambda entry: entry.name,
                )
        except (OSError, RuntimeError, ValueError):
            continue

        directories: list[Path] = []
        for entry in entries:
            visited += 1
            if visited > max_entries:
                break
            name = entry.name
            if name.startswith(".") or entry.is_symlink():
                continue
            path = Path(entry.path)
            try:
                relative = path.relative_to(workspace).as_posix()
                relative_path = PurePosixPath(relative)
                if (
                    relative_path.is_absolute()
                    or ".." in relative_path.parts
                    or any(part.startswith(".") for part in relative_path.parts)
                    or any(ord(char) < 32 or ord(char) == 127 for char in relative)
                ):
                    continue
                relative.encode("utf-8", errors="strict")
                resolved = path.resolve()
                if not resolved.is_relative_to(workspace) or _path_is_denied(
                    resolved, denied_roots
                ):
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except (OSError, RuntimeError, ValueError):
                continue
            if visible is not None and relative not in (
                visible[1] if is_directory else visible[0]
            ):
                continue
            if is_directory:
                directories.append(path)
            elif is_file and needle in relative.casefold():
                found.append(relative)
        stack.extend(reversed(directories))
    return sorted(found, key=lambda path: _rank(path, needle))[:limit]


def _rank(path: str, needle: str) -> tuple[int, int, str]:
    name = path.rsplit("/", 1)[-1].casefold()
    tier = 0 if name.startswith(needle) else 1 if needle in name else 2
    return tier, path.count("/"), path


def _git_visible(workspace: Path) -> tuple[frozenset[str], frozenset[str]] | None:
    """Files Git would not ignore, plus their parent directories.

    Paths are relative to the workspace even when it is a subdirectory of a
    repository. ``None`` means "not a Git work tree or Git unavailable": the
    search falls back to the plain walk. The list is cached briefly so
    per-keystroke searches do not spawn Git each time.
    """
    key = str(workspace)
    now = time.monotonic()
    cached = _git_cache.get(key)
    if cached is not None and now - cached[0] < _GIT_LIST_TTL:
        return cached[1]
    result: tuple[frozenset[str], frozenset[str]] | None = None
    if shutil.which("git") is not None:
        try:
            completed = subprocess.run(
                ["git", "-C", key, "ls-files", "-z", "--cached", "--others",
                 "--exclude-standard", "--", "."],
                capture_output=True,
                timeout=_GIT_LIST_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if (
            completed is not None
            and completed.returncode == 0
            and len(completed.stdout) <= _GIT_LIST_MAX_BYTES
        ):
            files = frozenset(
                name for name in completed.stdout.decode("utf-8", "replace").split("\0")
                if name
            )
            directories = frozenset(
                "/".join(parts[:index])
                for parts in (name.split("/") for name in files)
                for index in range(1, len(parts))
            )
            result = (files, directories)
    if len(_git_cache) > 16:
        _git_cache.clear()
    _git_cache[key] = (now, result)
    return result


def _file_search_denied_roots(runtime: object) -> tuple[Path, ...]:
    """Read current hard read boundaries without exposing other config."""
    context = getattr(runtime, "context", None)
    effective_config = getattr(context, "effective_config", None)
    try:
        config = effective_config() if callable(effective_config) else None
    except Exception:  # noqa: BLE001 - fail closed if policy cannot load
        raise ValueError("cannot load workspace file search policy") from None
    permissions = getattr(getattr(config, "v2", None), "permissions", None)
    raw_roots = getattr(permissions, "read_denyroots", ()) or ()
    if not raw_roots:
        return ()
    from ..tools.permissions import PathGuard

    guard = PathGuard(runtime.workspace, read_denyroots=tuple(raw_roots))
    return guard.read_denyroots


def _path_is_denied(path: Path, denied_roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in denied_roots)


__all__ = ["search_files"]
