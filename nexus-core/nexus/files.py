"""Read-only project file browsing, confined to a workspace root."""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import stat
from pathlib import Path

from nexus import worktrees

MAX_FILE_BYTES = 1024 * 1024


async def _git(root: Path, *args: str) -> tuple[bool, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "git", *args, cwd=root, env=worktrees.ENV,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async def collect() -> bytes:
            assert process.stdout is not None
            chunks = []
            size = 0
            while chunk := await process.stdout.read(65536):
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    process.kill()
                    await process.wait()
                    raise ValueError("Git output exceeds 32 MiB")
                chunks.append(chunk)
            await process.wait()
            return b"".join(chunks)

        try:
            stdout = await asyncio.wait_for(collect(), timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
            raise
        return process.returncode == 0, stdout.decode("utf-8", "surrogateescape")
    except (OSError, TimeoutError):
        return False, ""


def _open(root: Path, name: str, *, directory: bool) -> int:
    parts = Path(name).parts
    if Path(name).is_absolute() or any(part in ("..", ".") for part in parts):
        raise ValueError("path must stay within the workspace")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(root, flags)
        for index, part in enumerate(parts):
            is_dir = index < len(parts) - 1 or directory
            next_fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if is_dir else 0), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise ValueError(str(exc)) from exc


async def list_directory(root: Path, name: str) -> dict:
    fd = _open(root, name, directory=True)
    try:
        is_git, prefix = await _git(root, "rev-parse", "--show-prefix")
        prefix = prefix.removesuffix("\n") if is_git else ""
        _, status = await _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        _, ignored = await _git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory")
        statuses = {}
        records = iter(status.split("\0"))
        for record in records:
            if len(record) < 4:
                continue
            code, path = record[:2].strip(), record[3:].rstrip("/")
            if path.startswith(prefix):
                statuses[path[len(prefix):]] = code
            if "R" in record[:2] or "C" in record[:2]:
                next(records, None)
        ignored_paths = set(ignored.split("\0"))
        entries = []
        with os.scandir(fd) as iterator:
            directory_entries = sorted(iterator, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.casefold()))
        for entry in directory_entries:
            stat = entry.stat(follow_symlinks=False)
            rel = (Path(name) / entry.name).as_posix().removeprefix("./")
            status = statuses.get(rel) or next((code for path, code in statuses.items() if path.startswith(rel + "/")), None)
            entries.append({"name": entry.name, "kind": "symlink" if entry.is_symlink() else "directory" if entry.is_dir(follow_symlinks=False) else "file", "size": stat.st_size, "mtime": stat.st_mtime, "git_status": status, "ignored": rel == ".git" or rel in ignored_paths or rel + "/" in ignored_paths or any(rel.startswith(path.rstrip("/") + "/") for path in ignored_paths if path)})
        return {"path": name, "entries": entries}
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        os.close(fd)


def read_file(root: Path, name: str) -> dict:
    fd = _open(root, name, directory=False)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("path is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
        if b"\0" in data:
            raise ValueError("file is binary")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("file is not UTF-8 text") from exc
        return {"path": name, "content": content, "revision": "sha256:" + hashlib.sha256(data).hexdigest(), "language": mimetypes.guess_type(name)[0]}
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        os.close(fd)


async def git_status(root: Path) -> dict:
    """Return the workspace's Git branch and changed paths."""
    ok, branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        ok, branch = await _git(root, "symbolic-ref", "--short", "HEAD")
        if not ok:
            raise ValueError("workspace is not a Git repository")
    _, prefix = await _git(root, "rev-parse", "--show-prefix")
    prefix = prefix.removesuffix("\n")
    _, upstream = await _git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    counts = upstream.split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    _, raw = await _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = iter(raw.split("\0"))
    changes = []
    for record in records:
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        old_path = next(records, None) if "R" in code or "C" in code else None
        if not path.startswith(prefix):
            continue
        changes.append({"path": path[len(prefix):], "staged": code[0] not in (" ", "?"),
                        "unstaged": code[1] != " ", "status": code, "old_path": old_path})
    stats: dict[str, tuple[int, int]] = {}
    for args in (("diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z"), ("diff", "--no-ext-diff", "--no-textconv", "--cached", "--numstat", "-z")):
        _, output = await _git(root, *args)
        entries = iter(output.split("\0"))
        for entry in entries:
            fields = entry.split("\t", 2)
            if len(fields) != 3:
                continue
            added, removed, path = fields
            if not path:  # A rename's old and new paths follow separately.
                next(entries, None)
                path = next(entries, "")
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
            old_added, old_removed = stats.get(path, (0, 0))
            stats[path] = (old_added + int(added) if added.isdigit() else old_added,
                           old_removed + int(removed) if removed.isdigit() else old_removed)
    for change in changes:
        change["added"], change["removed"] = stats.get(change["path"], (0, 0))
    return {"branch": branch.removesuffix("\n"), "ahead": ahead, "behind": behind, "changes": changes}


async def git_diff(root: Path, path: str) -> dict:
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValueError("path must be relative to the workspace")
    ok, unstaged = await _git(root, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--", path)
    if not ok:
        raise ValueError("workspace is not a Git repository")
    _, staged = await _git(root, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--cached", "--", path)
    def clip(text: str) -> tuple[str, bool]:
        encoded = text.encode("utf-8", "surrogateescape")
        if len(encoded) <= MAX_FILE_BYTES:
            return text, False
        return encoded[:MAX_FILE_BYTES].decode("utf-8", "replace"), True

    staged, staged_clipped = clip(staged)
    unstaged, unstaged_clipped = clip(unstaged)
    return {"path": path, "staged": staged, "unstaged": unstaged,
            "staged_clipped": staged_clipped, "unstaged_clipped": unstaged_clipped}
