"""The shared workspace walk for `grep`, `glob` and `ls`.

Adapted from old Nexus nexus/tools/builtin/read.py (`_walk_entries`, `_glob_match`).
In a Git work tree the walk is what Git would show (tracked plus untracked, minus
ignored, fresh on every call so a file just written is found); elsewhere it is a
plain walk. Symlinks are never followed or listed, and `.git` is never entered.
"""
from __future__ import annotations

import asyncio
import os
import stat
from fnmatch import fnmatchcase
from pathlib import Path

from .. import files
from .base import ToolContext, ToolError, resolve

MAX_ENTRIES = 50_000
MAX_PATTERN_CHARS = 1024
MAX_STAR_STAR = 4


def resolve_dir(ctx: ToolContext, path: object) -> Path:
    target = resolve(ctx, path or ".")
    if not target.is_dir():
        raise ToolError(f"{path or '.'} is not a directory")
    return target


def pattern_error(pattern: str) -> str | None:
    if "\x00" in pattern:
        return "must not contain a NUL byte"
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"is too long ({len(pattern)} characters; limit {MAX_PATTERN_CHARS})"
    if pattern.split("/").count("**") > MAX_STAR_STAR:
        return f"has too many '**' segments (limit {MAX_STAR_STAR})"
    return None


def glob_match(rel: str, pattern: str) -> bool:
    """Shell-glob match of a `/` path; `**` spans directories. Cost is O(pattern × depth)."""
    parts = rel.split("/") if rel else []
    depth = len(parts)
    following = [False] * depth + [True]
    for head in reversed(pattern.split("/")):
        current = [False] * (depth + 1)
        if head == "**":
            matched = False
            for j in range(depth, -1, -1):
                matched = following[j] or matched
                current[j] = matched
        else:
            for j in range(depth - 1, -1, -1):
                current[j] = following[j + 1] and fnmatchcase(parts[j], head)
        following = current
    return following[0]


async def walk(workspace: Path, root: Path, *, include_hidden: bool = False) -> tuple[list[tuple[str, bool]], bool]:
    """Every visible `(path relative to workspace, is_dir)` under `root`, sorted, and whether the cap cut it."""
    return await asyncio.to_thread(_collect, await _git_files(root), workspace.resolve(), root, include_hidden)


async def _git_files(root: Path) -> list[str] | None:
    try:
        ok, output = await files._git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ".")
    except ValueError:
        return None
    return [name for name in output.split("\0") if name] if ok else None


def _collect(listed: list[str] | None, workspace: Path, root: Path, include_hidden: bool) -> tuple[list[tuple[str, bool]], bool]:
    prefix = root.relative_to(workspace).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    found: dict[str, bool] = {}
    truncated = False

    def keep(rel: str) -> bool:
        return include_hidden or not any(part.startswith(".") for part in rel.split("/"))

    def add(rel: str, is_dir: bool) -> bool:
        nonlocal truncated
        if len(found) >= MAX_ENTRIES:
            truncated = True
            return False
        found[prefix + rel] = is_dir
        return True

    if listed is not None:
        for name in listed:
            if not keep(name):
                continue
            try:
                mode = os.lstat(root / name).st_mode
            except OSError:
                continue  # tracked but deleted
            if not stat.S_ISREG(mode):
                continue
            parts = name.split("/")
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                if prefix + directory not in found and not add(directory, True):
                    break
            if not add(name, False):
                break
    else:
        stack = [root]
        while stack and not truncated:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if entry.name == ".git" or entry.is_symlink() or (not include_hidden and entry.name.startswith(".")):
                    continue
                rel = Path(entry.path).relative_to(root).as_posix()
                is_dir = entry.is_dir(follow_symlinks=False)
                if not add(rel, is_dir):
                    break
                if is_dir:
                    stack.append(Path(entry.path))
    return sorted(found.items()), truncated
