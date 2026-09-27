"""Built-in ``Read`` and the private helpers shared by the fs tools.

Everything in the ``_``-prefixed section is package-private: the sibling
builtin modules import it so path resolution, permission-config extraction,
result capping, and the bounded directory walk have exactly one implementation.
Nothing here reaches upward into ``core``/``runtime``/``ui``; the only
contracts used are :mod:`nexus.tools.spec` and :mod:`nexus.tools.permissions`.
"""
from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ...config.schema import PermissionsSection
from ...errors import ToolError
from ..permissions import PathGuard, PathSecurityError, ResolvedPath
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from ._anydoc_client import AnyDocError, convert_document

# ---------------------------------------------------------------------------
# Shared helpers (private to nexus.tools.builtin)
# ---------------------------------------------------------------------------

#: The context builder's legacy 4 chars/token approximation.
_CHARS_PER_TOKEN = 4
_FALLBACK_MAX_RESULT_TOKENS = 25_000
#: Absolute ceiling so a generous config cannot make a tool allocate forever.
_HARD_MAX_RESULT_BYTES = 1_000_000
#: Bytes inspected for a NUL to classify a file as binary.
_BINARY_SNIFF_BYTES = 8192
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf", ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot",
        ".pptx", ".pptm", ".ppsx", ".ppsm", ".xls", ".xlsx",
        ".xlsm", ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub",
    }
)
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst", ".py", ".pyi", ".js", ".jsx",
        ".ts", ".tsx", ".json", ".jsonl", ".yaml", ".yml", ".toml",
        ".ini", ".cfg", ".xml", ".html", ".htm", ".css", ".scss",
        ".sql", ".sh", ".bash", ".zsh", ".go", ".rs", ".java", ".c",
        ".h", ".cc", ".cpp", ".hpp", ".rb", ".php", ".swift", ".kt",
        ".log", ".env", ".conf", ".properties",
    }
)


def _permissions(ctx: ToolContext) -> PermissionsSection:
    """Return the permission section from ``ctx.config`` or safe defaults.

    ``Config`` may be the legacy flat shape (``v2 is None``) or a hand-built
    test double; both fall back to the built-in ``PermissionsSection`` defaults
    rather than raising, so a tool never fails merely because config is sparse.
    """
    v2 = getattr(ctx.config, "v2", None)
    permissions = getattr(v2, "permissions", None)
    if isinstance(permissions, PermissionsSection):
        return permissions
    return PermissionsSection()


def _guard(ctx: ToolContext) -> PathGuard:
    """Build the hard-boundary guard for this call from config."""
    permissions = _permissions(ctx)
    return PathGuard(
        ctx.workspace,
        write_roots=tuple(permissions.write_roots) or ("./",),
        read_denyroots=tuple(permissions.read_denyroots),
    )


def _result_token_budget(ctx: ToolContext) -> int:
    v2 = getattr(ctx.config, "v2", None)
    tools = getattr(v2, "tools", None)
    tokens = getattr(tools, "max_result_tokens", None)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        return _FALLBACK_MAX_RESULT_TOKENS
    return tokens


def _byte_budget(ctx: ToolContext, default: int) -> int:
    """A per-result byte cap: the tool default tightened by config tokens."""
    configured = _result_token_budget(ctx) * _CHARS_PER_TOKEN
    return max(1, min(default, configured, _HARD_MAX_RESULT_BYTES))


def _check_cancel(ctx: ToolContext | None) -> None:
    if ctx is None:
        return
    token = ctx.cancel_token
    if token is not None:
        token.raise_if_cancelled()


def canonical_permission_key(data: dict[str, Any]) -> str:
    """Best-effort canonical path for permission matching.

    ``permission_key`` is a pure function of the tool input and cannot see
    ``ctx.workspace``, so a relative path is left relative (the engine and the
    tool both re-canonicalize it against the workspace via :class:`PathGuard`);
    an absolute path is fully resolved here so deny rules such as
    ``Read(**/.env)`` match. An empty/invalid value fails closed because the
    engine turns a non-string/empty key into a denial.
    """
    raw = data.get("path")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return "" if not isinstance(raw, str) else raw
    try:
        if os.path.isabs(raw):
            return os.path.realpath(raw)
        return os.path.normpath(raw)
    except (OSError, ValueError):
        return raw


def canonical_root_key(data: dict[str, Any]) -> str:
    """Like :func:`canonical_permission_key` but defaults a missing root to ``.``.

    Used by the directory-walking tools (Glob/Grep/LS), where an omitted
    ``path`` means the workspace root rather than a malformed call.
    """
    raw = data.get("path")
    if raw is None:
        raw = "."
    if not isinstance(raw, str):
        return ""
    return canonical_permission_key({"path": raw})


def _error(message: str, *, display: str | None = None) -> ToolExecutionResult:
    return ToolExecutionResult.text(
        message, is_error=True, display=display if display is not None else message
    )


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key} must be a non-empty string")
    return value


def _require_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    return value


def _opt_str(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    return value


def _opt_bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be a boolean")
    return value


def _opt_int(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be an integer")
    return value


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n``, normalizing CRLF, without inventing a trailing line."""
    if text == "":
        return []
    lines = text.split("\n")
    lines = [line.removesuffix("\r") for line in lines]
    if text.endswith("\n"):
        lines.pop()
    return lines


def _cap_lines(lines: list[str], max_bytes: int) -> tuple[list[str], bool]:
    """Keep the longest prefix of ``lines`` whose joined UTF-8 fits ``max_bytes``."""
    kept: list[str] = []
    used = 0
    for line in lines:
        encoded_len = len(line.encode("utf-8"))
        addition = encoded_len + (1 if kept else 0)
        if used + addition > max_bytes:
            if not kept:
                kept.append(line.encode("utf-8")[:max_bytes].decode("utf-8", "ignore"))
            return kept, True
        kept.append(line)
        used += addition
    return kept, False


def _truncation_marker(tool: str, shown: int, total: int, unit: str, rerun: str) -> str:
    return f"[{tool}: truncated, showing {shown} of {total} {unit}; {rerun}]"


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_denied(path: Path, denied_roots: tuple[Path, ...]) -> bool:
    return any(_within(path, root) for root in denied_roots)


def _read_text_file(path: Path, max_bytes: int) -> str:
    """Read a whole UTF-8 text file, rejecting oversize and binary input."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ToolError(f"Cannot stat {path}: {exc}") from exc
    if size > max_bytes:
        raise ToolError(
            f"File is too large to edit: {size} bytes exceeds the {max_bytes} byte limit"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolError(f"Cannot read {path}: {exc}") from exc
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        raise ToolError(f"Refusing to edit binary file: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"File is not valid UTF-8 text: {path}") from exc


def _read_document_bytes(
    guard: PathGuard,
    raw: str,
    resolved: ResolvedPath,
    target: Path,
    initial_stat: os.stat_result,
) -> bytes:
    """Read a regular, bounded document while checking path/inode stability."""
    if not stat_module.S_ISREG(initial_stat.st_mode):
        raise ToolError("Document path is not a regular file")
    if initial_stat.st_size > _MAX_DOCUMENT_BYTES:
        raise ToolError("Document exceeds the 16 MiB conversion limit")
    try:
        with target.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat_module.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (initial_stat.st_dev, initial_stat.st_ino)
            ):
                raise ToolError("Document changed while it was being opened")
            if opened.st_size > _MAX_DOCUMENT_BYTES:
                raise ToolError("Document exceeds the 16 MiB conversion limit")
            data = handle.read(_MAX_DOCUMENT_BYTES + 1)
            after = os.fstat(handle.fileno())
        rechecked = guard.recheck(raw, for_write=False)
        current = target.stat()
    except ToolError:
        raise
    except (OSError, PathSecurityError) as exc:
        raise ToolError("Document could not be read safely") from exc
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise ToolError("Document exceeds the 16 MiB conversion limit")
    identity = (opened.st_dev, opened.st_ino)
    if (
        (after.st_dev, after.st_ino) != identity
        or (current.st_dev, current.st_ino) != identity
        or after.st_size != opened.st_size
        or after.st_size > _MAX_DOCUMENT_BYTES
        or after.st_mtime_ns != opened.st_mtime_ns
        or rechecked.absolute != resolved.absolute
    ):
        raise ToolError("Document changed while it was being read")
    return data


def _read_text_bytes(
    guard: PathGuard,
    raw: str,
    resolved: ResolvedPath,
    target: Path,
    initial_stat: os.stat_result,
    max_bytes: int,
) -> bytes:
    """Read bounded text through a verified regular-file descriptor."""
    if not stat_module.S_ISREG(initial_stat.st_mode):
        raise ToolError(f"Not a regular file: {resolved.display}")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # A path swapped to a FIFO must not block between stat and fstat.
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        identity = (initial_stat.st_dev, initial_stat.st_ino)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != identity
            or opened.st_size != initial_stat.st_size
        ):
            raise ToolError("File changed while it was being opened")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError(f"Cannot read {resolved.display}: {exc}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    try:
        rechecked = guard.recheck(raw, for_write=False)
        current = target.stat()
    except (OSError, PathSecurityError) as exc:
        raise ToolError("File changed while it was being read") from exc
    if (
        rechecked.absolute != resolved.absolute
        or (after.st_dev, after.st_ino) != identity
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
        or not stat_module.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or current.st_size != opened.st_size
        or current.st_mtime_ns != opened.st_mtime_ns
    ):
        raise ToolError("File changed while it was being read")
    return b"".join(chunks)


@dataclass
class _WalkStatus:
    """Optional outcome metadata for callers that need to detect a capped walk."""

    truncated: bool = False


def _walk_entries(
    root: Path,
    *,
    include_hidden: bool,
    denied_roots: tuple[Path, ...],
    max_entries: int,
    ctx: ToolContext,
    status: _WalkStatus | None = None,
) -> Iterator[tuple[Path, str, bool, bool]]:
    """Deterministic, bounded, symlink-safe walk under ``root``.

    Yields ``(absolute_path, relative_posix, is_dir, is_symlink)``. Directory
    symlinks are never descended; symlinks resolving outside ``root`` or under a
    denied root are pruned entirely. Hidden entries are skipped unless
    ``include_hidden``. ``max_entries`` bounds the number of directory entries
    visited (not matches) so a huge tree cannot stall a turn.
    """
    visited = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            _check_cancel(ctx)
            if not include_hidden and entry.name.startswith("."):
                continue
            visited += 1
            if visited > max_entries:
                if status is not None:
                    status.truncated = True
                return
            path = Path(entry.path)
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            is_symlink = entry.is_symlink()
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if _is_denied(path, denied_roots):
                continue
            if is_symlink:
                try:
                    real = Path(os.path.realpath(path))
                except OSError:
                    continue
                if not _within(real, root) or _is_denied(real, denied_roots):
                    continue
            if is_dir:
                stack.append(path)
            yield path, rel, is_dir, is_symlink


def _resolve_directory(guard: PathGuard, raw: str) -> ResolvedPath:
    """Resolve a workspace-rooted directory argument (fail closed outside)."""
    resolved = guard.resolve(raw, for_write=False)
    if not resolved.inside_workspace:
        raise ToolError(f"Path is outside the workspace: {resolved.display}")
    if not resolved.absolute.is_dir():
        raise ToolError(f"Not a directory: {resolved.display}")
    return resolved


#: Maximum ``**`` (recursive) segments accepted in a glob pattern. Matching is
#: already bounded (iterative DP), but a conservative cap keeps the worst case
#: small and makes runaway patterns an explicit, model-visible error.
MAX_GLOB_STAR_STAR = 16


def glob_pattern_error(pattern: str) -> str | None:
    """Return a model-visible error for an unacceptably expensive glob, or None."""
    star_star = pattern.split("/").count("**")
    if star_star > MAX_GLOB_STAR_STAR:
        return (
            f"glob has too many '**' segments ({star_star}; limit "
            f"{MAX_GLOB_STAR_STAR})"
        )
    return None


def _glob_match(rel: str, pattern: str, *, is_dir: bool, dir_only: bool) -> bool:
    """Match a root-relative POSIX path against a shell glob (``**`` aware).

    Implemented as an iterative dynamic program over pattern/path segments, so
    the cost is ``O(len(pattern) * depth)`` and a pattern with many ``**`` cannot
    trigger exponential recursion. ``fnmatchcase`` still supplies the per-segment
    glob semantics.
    """
    if dir_only and not is_dir:
        return False
    rel_parts = rel.split("/") if rel else []
    pat_parts = pattern.split("/")
    return _match_parts(rel_parts, pat_parts)


def _match_parts(rel_parts: list[str], pat_parts: list[str]) -> bool:
    depth = len(rel_parts)
    # Rolling DP: ``next_row`` is "pattern[i+1:] matches path[j:]" and
    # ``cur_row`` is "pattern[i:] matches path[j:]". Memory is O(depth), time is
    # O(width * depth), so a path with many ``**`` cannot blow up recursively.
    next_row = [False] * (depth + 1)
    next_row[depth] = True
    for head in reversed(pat_parts):
        cur_row = [False] * (depth + 1)
        if head == "**":
            matched = False
            for j in range(depth, -1, -1):
                matched = next_row[j] or matched
                cur_row[j] = matched
        else:
            for j in range(depth - 1, -1, -1):
                if next_row[j + 1] and fnmatchcase(rel_parts[j], head):
                    cur_row[j] = True
        next_row = cur_row
    return next_row[0]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

_DEFAULT_LINES = 2000
_HARD_MAX_LINES = 20_000
_DEFAULT_MAX_BYTES = 256 * 1024
_MAX_DIRECTORY_SCAN_ENTRIES = 10_000
# Bound raw directory work independently from the public-entry cap so hidden
# and denied names do not consume result capacity or make a walk unbounded.
_MAX_DIRECTORY_SCAN_WORK = 100_000

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File or directory to read, relative to the workspace.",
        },
        "offset": {
            "type": "integer",
            "description": "1-based first line to return (default 1).",
        },
        "limit": {
            "type": "integer",
            "description": f"Maximum lines to return (default {_DEFAULT_LINES}).",
        },
        "csv_as_markdown": {
            "type": "boolean",
            "description": "Convert a CSV file to Markdown instead of reading its raw UTF-8 text.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="read",
    description=(
        "Read a UTF-8 text file or list a workspace directory, optionally "
        "starting at a 1-based line offset and limited to a number of lines. "
        "Supported local documents are converted to Markdown; other binary "
        "files are reported rather than decoded."
    ),
    input_schema=_READ_SCHEMA,
    bundle="fs",
    mutates=False,
    concurrency="parallel",
    permission_key=canonical_permission_key,
    max_result_tokens=25_000,
)


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("Read arguments must be an object")
    try:
        raw = _require_str(args, "path")
        offset = _opt_int(args, "offset", 1)
        limit = _opt_int(args, "limit", _DEFAULT_LINES)
        csv_as_markdown = _opt_bool(args, "csv_as_markdown")
    except ToolError as exc:
        return _error(str(exc))
    if offset < 1:
        return _error("Read offset must be >= 1")
    if limit < 1:
        return _error("Read limit must be >= 1")
    limit = min(limit, _HARD_MAX_LINES)

    guard = _guard(ctx)
    try:
        guard.resolve(raw, for_write=False)
        resolved = guard.recheck(raw, for_write=False)  # TOCTOU re-check
    except PathSecurityError as exc:
        return _error(str(exc))

    target = resolved.absolute
    try:
        stat = target.stat()
    except FileNotFoundError:
        return _error(f"File not found: {resolved.display}")
    except OSError as exc:
        return _error(f"Cannot stat {resolved.display}: {exc}")
    max_bytes = _byte_budget(ctx, _DEFAULT_MAX_BYTES)
    _check_cancel(ctx)
    if stat_module.S_ISDIR(stat.st_mode):
        if not resolved.inside_workspace:
            return _error(f"Path is outside the workspace: {resolved.display}")
        try:
            rechecked = guard.recheck(raw, for_write=False)
            current = target.stat()
        except (OSError, PathSecurityError) as exc:
            return _error(f"Directory changed before it could be listed: {exc}")
        if (
            rechecked.absolute != resolved.absolute
            or not rechecked.inside_workspace
            or not stat_module.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (stat.st_dev, stat.st_ino)
        ):
            return _error("Directory changed before it could be listed")
        try:
            with os.scandir(target) as iterator:
                entries: list[tuple[str, str]] = []
                total_entries = 0
                scanned_entries = 0
                scan_truncated = False
                for entry in iterator:
                    _check_cancel(ctx)
                    scanned_entries += 1
                    if scanned_entries > _MAX_DIRECTORY_SCAN_WORK:
                        scan_truncated = True
                        break
                    if entry.name.startswith("."):
                        # Keep hidden entries out of the public read result.
                        continue
                    entry_path = Path(entry.path)
                    if _is_denied(entry_path, guard.read_denyroots):
                        continue
                    if entry.is_symlink():
                        try:
                            real = Path(os.path.realpath(entry.path))
                        except OSError:
                            continue
                        if _is_denied(real, guard.read_denyroots):
                            continue
                        if not _within(real, guard.workspace):
                            description = f"{entry.name}@"
                        else:
                            description = f"{entry.name}@"
                    else:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                        description = f"{entry.name}/" if is_dir else entry.name
                    if total_entries >= _MAX_DIRECTORY_SCAN_ENTRIES:
                        scan_truncated = True
                        break
                    total_entries += 1
                    entries.append((entry.name, description))
        except OSError as exc:
            return _error(f"Cannot list {resolved.display}: {exc}")

        try:
            rechecked = guard.recheck(raw, for_write=False)
            current = target.stat()
        except (OSError, PathSecurityError) as exc:
            return _error(f"Directory changed while it was being listed: {exc}")
        if (
            rechecked.absolute != resolved.absolute
            or not rechecked.inside_workspace
            or not stat_module.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (stat.st_dev, stat.st_ino)
        ):
            return _error("Directory changed while it was being listed")

        entries.sort(key=lambda entry: entry[0])
        window = [
            description
            for _, description in entries[offset - 1 : offset - 1 + limit]
        ]
        shown, byte_truncated = _cap_lines(window, max_bytes)
        last_shown = offset - 1 + len(shown)
        line_truncated = last_shown < total_entries
        truncated = scan_truncated or byte_truncated or line_truncated
        body = "\n".join(shown)
        context_note: str | None = None
        if truncated:
            exactness = "at least " if scan_truncated else ""
            rerun = (
                "list a narrower directory"
                if scan_truncated
                else f"re-run with offset={last_shown + 1} or list a narrower directory"
            )
            marker = (
                f"[Read: truncated, showing {len(shown)} of {exactness}"
                f"{total_entries} entries; {rerun}]"
            )
            body = f"{body}\n{marker}" if body else marker
            context_note = marker
        elif shown:
            context_note = (
                f"[Read {resolved.display}: {len(shown)} line(s) shown; re-run Read "
                "to load the content again]"
            )
        display = f"Read {resolved.display}: {len(shown)} of "
        display += f"{'at least ' if scan_truncated else ''}{total_entries} entries"
        if truncated:
            display += " (truncated)"
        return ToolExecutionResult.text(
            body,
            display=display,
            context_note=context_note,
            metrics={
                "lines": len(shown),
                "total_lines": total_entries,
                "total_lines_exact": not scan_truncated,
                "bytes": len("\n".join(shown).encode("utf-8")),
                "size": stat.st_size,
                "truncated": truncated,
                "replacements": 0,
                "path": resolved.key,
            },
        )

    extension = target.suffix.lower()
    if csv_as_markdown and extension != ".csv":
        return _error("csv_as_markdown is only supported for .csv files")
    is_document = extension in _DOCUMENT_EXTENSIONS or (
        extension == ".csv" and csv_as_markdown
    )
    source_metadata: dict[str, Any] = {}
    byte_truncated = False
    replacements = 0
    source_bytes = 0
    if is_document:
        try:
            document = _read_document_bytes(guard, raw, resolved, target, stat)
        except ToolError as exc:
            return _error(f"Read: {exc}")
        try:
            markdown = await convert_document(document, extension, ctx.cancel_token)
        except AnyDocError as exc:
            return _error(f"Read: {exc}")
        text = markdown.decode("utf-8")
        source_bytes = len(markdown)
        source_metadata = {
            "source": "firecrawl-anydoc",
            "document": True,
            "extension": extension,
        }
    else:
        if not stat_module.S_ISREG(stat.st_mode):
            return _error(f"Not a regular file: {resolved.display}")
        try:
            data = _read_text_bytes(guard, raw, resolved, target, stat, max_bytes)
        except ToolError as exc:
            return _error(str(exc))

        byte_truncated = len(data) > max_bytes
        if byte_truncated:
            data = data[:max_bytes]
        source_bytes = len(data)

        if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
            return ToolExecutionResult.text(
                f"Binary file not shown: {resolved.display} ({stat.st_size} bytes)",
                is_error=True,
                display=f"Read {resolved.display}: binary ({stat.st_size} bytes)",
                metrics={
                    "binary": True,
                    "bytes": stat.st_size,
                    "path": resolved.key,
                },
            )

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if extension not in _TEXT_EXTENSIONS:
                return ToolExecutionResult.text(
                    f"Binary or non-UTF-8 file not shown: {resolved.display}",
                    is_error=True,
                    display=f"Read {resolved.display}: binary or non-UTF-8",
                    metrics={"binary": True, "bytes": stat.st_size, "path": resolved.key},
                )
            # Preserve the established tolerant decoding for known text files.
            text = data.decode("utf-8", errors="replace")
            replacements = text.count("\ufffd")
    lines = _split_lines(text)
    total_lines = len(lines)
    window = lines[offset - 1 : offset - 1 + limit]
    shown, line_truncated = _cap_lines(window, max_bytes)
    last_shown = offset - 1 + len(shown)
    truncated = byte_truncated or line_truncated or last_shown < total_lines

    body = "\n".join(shown)
    context_note: str | None = None
    if truncated:
        if byte_truncated:
            rerun = (
                f"output cut at {source_bytes} bytes; re-run with offset="
                f"{last_shown + 1} or a smaller limit"
            )
        elif is_document and line_truncated:
            rerun = (
                f"converted Markdown reached the {max_bytes}-byte result cap; "
                f"re-run with offset={last_shown + 1} or a smaller limit"
            )
        else:
            rerun = f"re-run with offset={last_shown + 1} or a smaller limit"
        marker = _truncation_marker(
            "Read", len(shown), total_lines, "lines", rerun
        )
        body = f"{body}\n{marker}" if body else marker
        context_note = marker
    elif shown:
        context_note = (
            f"[Read {resolved.display}: {len(shown)} line(s) shown; re-run Read "
            "to load the content again]"
        )
    display = f"Read {resolved.display}: {len(shown)} of {total_lines} lines"
    if is_document:
        display += f" (converted from {extension} via Firecrawl AnyDoc)"
    if truncated:
        display += " (truncated)"
    return ToolExecutionResult.text(
        body,
        display=display,
        context_note=context_note,
        metrics={
            "lines": len(shown),
            "total_lines": total_lines,
            "bytes": source_bytes,
            "size": stat.st_size,
            "truncated": truncated,
            "replacements": replacements,
            "path": resolved.key,
            **source_metadata,
        },
    )


__all__ = ["SPEC", "run"]
