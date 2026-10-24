"""Bounded, read-only directory listing and Git status of the workspace for the native sidebar.

Contract: paths are workspace-relative POSIX strings; nothing is read outside the workspace,
symlinks are listed but never followed, hidden entries and configured read-deny roots are
omitted, and every count, byte and second is capped. Names that look like credentials are
dropped rather than shown. The UI never touches the filesystem: it asks for these results.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import stat
from pathlib import Path
from typing import Any

from ..util import redact_secrets
from .workspace import _file_search_denied_roots, _path_is_denied, session_workspace

MAX_PATH_CHARS = 1024
MAX_LIST_ENTRIES = 500
DEFAULT_LIST_ENTRIES = 200
#: Directory entries read before the listing is cut off (a huge directory never stalls the host).
MAX_SCANNED_ENTRIES = 5_000

#: Bytes of a file the Files tab previews by default, and the most a caller may ask for.
DEFAULT_PREVIEW_BYTES = 64 * 1024
MAX_PREVIEW_BYTES = 256 * 1024

MAX_STATUS_ENTRIES = 500
MAX_STATUS_BYTES = 512 * 1024
_STATUS_SECONDS = 5.0

_STATUS_WORDS = {
    "M": "modified", "T": "type changed", "A": "added", "D": "deleted",
    "R": "renamed", "C": "copied", "U": "unmerged", "?": "untracked",
}


def normalize_relative(path: object) -> str:
    """Return the canonical workspace-relative POSIX form of ``path`` or raise ``ValueError``.

    ``""`` is the workspace root. Absolute paths, ``..`` segments, backslashes, NUL and control
    characters, empty segments and hidden segments are refused.
    """
    if not isinstance(path, str) or len(path) > MAX_PATH_CHARS:
        raise ValueError("directory path must be a string of at most 1024 characters")
    if path in {"", "."}:
        return ""
    if path.startswith(("/", "\\")) or "\\" in path:
        raise ValueError("directory path must be workspace-relative")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("directory path contains control characters")
    parts = path.rstrip("/").split("/")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("directory path must be a plain workspace-relative path")
    return "/".join(parts)


def list_directory(
    runtime: object, path: str = "", limit: int = DEFAULT_LIST_ENTRIES, *, session: str = "",
    project_root: Path | None = None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """List one workspace directory: ``(path, entries, truncated)``.

    Entries are ``{"name", "path", "kind", "size"}`` with ``kind`` one of ``dir``, ``file``
    or ``link`` (a symlink, never followed or navigable). Directories sort first, then names
    case-insensitively. Hidden entries, control-character names, credential-looking names and
    anything under a read-deny root are omitted.
    """
    relative = normalize_relative(path)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("directory limit must be a positive integer")
    limit = min(limit, MAX_LIST_ENTRIES)
    workspace = project_root if project_root is not None else session_workspace(runtime, session)
    denied = _file_search_denied_roots(runtime, session=session, workspace=workspace)
    target = workspace
    for part in relative.split("/") if relative else ():
        target = target / part
        try:
            if target.is_symlink():
                raise ValueError("directory path crosses a symbolic link")
        except OSError as exc:
            raise ValueError("directory is unavailable") from exc
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("directory is unavailable") from exc
    if resolved != workspace and not resolved.is_relative_to(workspace):
        raise ValueError("directory is outside the workspace")
    if _path_is_denied(resolved, denied):
        raise ValueError("directory is not readable")
    try:
        with os.scandir(resolved) as iterator:
            scanned = list(itertools.islice(iterator, MAX_SCANNED_ENTRIES + 1))
    except (OSError, ValueError) as exc:
        raise ValueError("directory is unavailable") from exc
    cut = len(scanned) > MAX_SCANNED_ENTRIES
    entries: list[dict[str, Any]] = []
    for entry in scanned[:MAX_SCANNED_ENTRIES]:
        name = entry.name
        if name.startswith(".") or any(ord(char) < 32 or ord(char) == 127 for char in name):
            continue
        if redact_secrets(name) != name:
            continue
        try:
            name.encode("utf-8", errors="strict")
            info = entry.stat(follow_symlinks=False)
        except (OSError, UnicodeError):
            continue
        child = str(resolved / name) if project_root is not None else (f"{relative}/{name}" if relative else name)
        if stat.S_ISLNK(info.st_mode):
            kind, size = "link", None
        elif stat.S_ISDIR(info.st_mode):
            kind, size = "dir", None
            try:
                if _path_is_denied((resolved / name).resolve(), denied):
                    continue
            except (OSError, RuntimeError):
                continue
        elif stat.S_ISREG(info.st_mode):
            kind, size = "file", int(info.st_size)
        else:
            continue
        entries.append({"name": name, "path": child, "kind": kind, "size": size})
    entries.sort(key=lambda row: (row["kind"] != "dir", row["name"].casefold(), row["name"]))
    truncated = cut or len(entries) > limit
    return str(resolved) if project_root is not None else relative, entries[:limit], truncated


def _walk_workspace_path(
    runtime: object, relative: str, noun: str, *, session: str = ""
) -> tuple[Path, Path]:
    """Resolve ``relative`` below the workspace without crossing a symlink or a read-deny root."""
    workspace = session_workspace(runtime, session)
    denied = _file_search_denied_roots(runtime, session=session, workspace=workspace)
    target = workspace
    for part in relative.split("/"):
        target = target / part
        try:
            if target.is_symlink():
                raise ValueError(f"{noun} path crosses a symbolic link")
        except OSError as exc:
            raise ValueError(f"{noun} is unavailable") from exc
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{noun} is unavailable") from exc
    if not resolved.is_relative_to(workspace):
        raise ValueError(f"{noun} is outside the workspace")
    if _path_is_denied(resolved, denied):
        raise ValueError(f"{noun} is not readable")
    return workspace, resolved


def preview_file(
    runtime: object, path: str, max_bytes: int = DEFAULT_PREVIEW_BYTES, *, session: str = ""
) -> dict[str, Any]:
    """Read the start of one workspace file as text: ``{path, status, text, size, shown, truncated, message}``.

    ``status`` is ``text`` (``text`` holds the redacted, control-escaped start of the file),
    ``binary`` (NUL bytes or not UTF-8: nothing is shown) or ``empty``. Path problems (absolute,
    ``..``, hidden, a symlink anywhere on the path, outside the workspace, under a read-deny root,
    a credential-looking name, not a regular file) raise ``ValueError``. At most ``max_bytes``
    (1..``MAX_PREVIEW_BYTES``) are read from a descriptor opened with ``O_NOFOLLOW``.
    """
    relative = normalize_relative(path)
    if not relative:
        raise ValueError("file path must name a file")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("preview size must be a positive integer")
    max_bytes = min(max_bytes, MAX_PREVIEW_BYTES)
    if any(redact_secrets(part) != part for part in relative.split("/")):  # names the listing omits
        raise ValueError("file is not readable")
    _, resolved = _walk_workspace_path(runtime, relative, "file", session=session)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ValueError("file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("path is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError("file is unavailable") from exc
    finally:
        os.close(descriptor)
    size = int(info.st_size)
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    result: dict[str, Any] = {"path": relative, "status": "text", "text": "", "size": size,
                              "shown": len(data), "truncated": truncated, "message": ""}
    if not data:
        return {**result, "status": "empty", "message": "The file is empty"}
    if b"\0" in data:
        return {**result, "status": "binary", "shown": 0, "message": "Binary file: not shown"}
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # A cut in the middle of a multi-byte character is only the clip; anything else is not text.
        if truncated and exc.start >= len(data) - 3:
            text = data[:exc.start].decode("utf-8", errors="strict")
            result["shown"] = exc.start
        else:
            return {**result, "status": "binary", "shown": 0, "message": "Not UTF-8 text: not shown"}
    return {**result, "text": redact_secrets(text)}


def _status_label(code: str) -> str:
    return _STATUS_WORDS.get(code, "changed")


def parse_status(data: bytes) -> dict[str, Any]:
    """Parse ``git status --porcelain=v2 --branch -z`` output into labelled, bounded rows."""
    branch, upstream, ahead, behind = "", "", 0, 0
    rows: list[dict[str, str]] = []
    fields = data.decode("utf-8", "replace").split("\0")
    index = 0
    total = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if record.startswith("# branch.head "):
            branch = record[len("# branch.head "):]
        elif record.startswith("# branch.upstream "):
            upstream = record[len("# branch.upstream "):]
        elif record.startswith("# branch.ab "):
            for token in record[len("# branch.ab "):].split():
                try:
                    if token.startswith("+"):
                        ahead = int(token[1:])
                    elif token.startswith("-"):
                        behind = int(token[1:])
                except ValueError:
                    pass
        elif record[0] in "12":
            parts = record.split(" ", 8 if record[0] == "1" else 9)
            if len(parts) < (9 if record[0] == "1" else 10):
                continue
            code, path = parts[1], parts[-1]
            origin = ""
            if record[0] == "2":
                origin = fields[index] if index < len(fields) else ""
                index += 1
            total += 1
            staged, unstaged = code[0], code[1]
            if staged != ".":
                rows.append({"group": "staged", "code": staged, "label": _status_label(staged), "path": path, "from": origin})
            if unstaged != ".":
                rows.append({"group": "unstaged", "code": unstaged, "label": _status_label(unstaged), "path": path, "from": ""})
        elif record[0] == "u":
            parts = record.split(" ", 10)
            if len(parts) >= 11:
                total += 1
                rows.append({"group": "conflicts", "code": "U", "label": "unmerged", "path": parts[10], "from": ""})
        elif record[0] == "?":
            total += 1
            rows.append({"group": "untracked", "code": "?", "label": "untracked", "path": record[2:], "from": ""})
    return {"branch": branch, "upstream": upstream, "ahead": ahead, "behind": behind, "rows": rows, "total": total}


async def git_status(workspace: Path) -> dict[str, Any]:
    """Run ``git status`` without a shell, with a deadline and a byte cap; never mutates the repo.

    ``available`` is false (with a ``message``) outside a Git work tree or when Git is missing.
    Rows are capped at ``MAX_STATUS_ENTRIES`` with ``truncated`` set; paths are redacted.
    Git reports paths relative to the repository root, which the rows keep.
    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    args = ["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=off", "--no-pager",
            "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=normal", "--no-renames", "--", "."]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, cwd=workspace, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return _unavailable("Git is not available")
    data = bytearray()
    cut = False

    async def collect() -> None:
        nonlocal cut
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            room = MAX_STATUS_BYTES + 1 - len(data)
            data.extend(chunk[:room])
            if len(data) > MAX_STATUS_BYTES:
                cut = True
                process.kill()
                break

    try:
        await asyncio.wait_for(collect(), timeout=_STATUS_SECONDS)
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError as exc:
        raise ValueError("Git status timed out") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode and not cut:
        return _unavailable("This workspace is not a Git repository")
    parsed = parse_status(bytes(data[:MAX_STATUS_BYTES]))
    rows = [
        {**row, "path": redact_secrets(row["path"]), "from": redact_secrets(row["from"])}
        for row in parsed["rows"][:MAX_STATUS_ENTRIES]
    ]
    counts: dict[str, int] = {}
    for row in parsed["rows"]:
        counts[row["group"]] = counts.get(row["group"], 0) + 1
    return {
        "available": True, "message": "", "branch": redact_secrets(parsed["branch"]),
        "upstream": redact_secrets(parsed["upstream"]), "ahead": parsed["ahead"], "behind": parsed["behind"],
        "rows": rows, "counts": counts, "truncated": cut or len(parsed["rows"]) > MAX_STATUS_ENTRIES,
    }


def _unavailable(message: str) -> dict[str, Any]:
    return {"available": False, "message": message, "branch": "", "upstream": "", "ahead": 0, "behind": 0,
            "rows": [], "counts": {}, "truncated": False}


__all__ = ["git_status", "list_directory", "normalize_relative", "parse_status", "preview_file"]
