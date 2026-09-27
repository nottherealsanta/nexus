"""Isolated, killable regex-scan worker for the ``Grep`` tool.

Model-supplied regular expressions are executed here, in a short-lived
subprocess, never in the harness process. Catastrophic backtracking can
therefore burn CPU only in a child that the parent kills on a strict deadline:
it can neither block the event loop nor leak a runaway thread (the reason a
``asyncio.to_thread`` timeout is not sufficient). The parent also bounds every
input before it is handed over: pattern length, per-line scan length, stored
line length, total stored match bytes, match count, files scanned, and file
size.

The worker is invoked as ``python -m nexus.tools.builtin._grep_scan`` and speaks
one JSON request/response on stdin/stdout. It imports the same path-pruning and
line helpers the in-process scanner used, so symlink/deny-root behavior is
unchanged.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .read import (
    _BINARY_SNIFF_BYTES,
    _glob_match,
    _split_lines,
    _walk_entries,
    _WalkStatus,
)

__all__ = [
    "DEFAULT_MAX_MATCHES",
    "HARD_MAX_MATCHES",
    "MAX_FILE_BYTES",
    "MAX_LINE_SCAN_CHARS",
    "MAX_MATCH_STORE_BYTES",
    "MAX_PATTERN_CHARS",
    "MAX_SCAN_FILES",
    "MAX_STORED_LINE_CHARS",
    "main",
    "scan",
]

DEFAULT_MAX_MATCHES = 100
HARD_MAX_MATCHES = 5000
MAX_SCAN_FILES = 5000
MAX_FILE_BYTES = 2 * 1024 * 1024
#: Longest accepted pattern; longer ones are rejected before any process starts.
MAX_PATTERN_CHARS = 4096
#: Only this many leading characters of a line are ever scanned. A match beyond
#: it is missed, which is reported explicitly via ``scan_truncated``.
MAX_LINE_SCAN_CHARS = 10_000
#: Longest stored (returned) line; the rest is elided with an ellipsis.
MAX_STORED_LINE_CHARS = 2000
#: Total bytes of stored match lines, bounding result memory before formatting.
MAX_MATCH_STORE_BYTES = 512 * 1024


def scan(request: dict[str, Any]) -> dict[str, Any]:
    """Run one bounded scan. Pure: reads files, writes nothing."""
    root = Path(str(request["root"]))
    denied_roots = tuple(Path(str(item)) for item in request.get("denied_roots") or ())
    include_hidden = bool(request.get("include_hidden", False))
    file_glob = request.get("file_glob")
    pattern = str(request["pattern"])
    regex = bool(request.get("regex", True))
    case_insensitive = bool(request.get("case_insensitive", False))
    max_matches = int(request.get("max_matches", DEFAULT_MAX_MATCHES))
    max_matches = max(1, min(max_matches, HARD_MAX_MATCHES))
    max_scan_entries = int(request.get("max_scan_entries", MAX_SCAN_FILES * 20))
    max_scan_entries = max(1, min(max_scan_entries, MAX_SCAN_FILES * 20))

    flags = re.IGNORECASE if case_insensitive else 0
    compiled = re.compile(pattern if regex else re.escape(pattern), flags)

    matches: list[list[Any]] = []
    match_count = 0
    files_with_matches: set[str] = set()
    files_scanned = 0
    binary_skipped = 0
    input_truncated = False
    truncated = False
    line_truncated = False
    store_truncated = False
    scan_truncated = False
    stored_bytes = 0
    stop = False
    walk_status = _WalkStatus()

    for path, rel, is_dir, _is_symlink in _walk_entries(
        root,
        include_hidden=include_hidden,
        denied_roots=denied_roots,
        max_entries=max_scan_entries,
        ctx=None,
        status=walk_status,
    ):
        if stop:
            break
        if is_dir:
            continue
        if file_glob is not None and not _glob_match(
            rel, file_glob, is_dir=False, dir_only=False
        ):
            continue
        files_scanned += 1
        if files_scanned > MAX_SCAN_FILES:
            truncated = True
            break
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            input_truncated = True
        if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
            binary_skipped += 1
            continue
        text = data.decode("utf-8", errors="replace")
        file_matched = False
        for lineno, line in enumerate(_split_lines(text), start=1):
            scan_line = line
            if len(scan_line) > MAX_LINE_SCAN_CHARS:
                scan_line = scan_line[:MAX_LINE_SCAN_CHARS]
                scan_truncated = True
            if compiled.search(scan_line):
                match_count += 1
                file_matched = True
                if match_count > HARD_MAX_MATCHES:
                    truncated = True
                    stop = True
                    break
                if len(matches) < max_matches:
                    stored = line
                    if len(stored) > MAX_STORED_LINE_CHARS:
                        stored = stored[:MAX_STORED_LINE_CHARS] + "…"
                        line_truncated = True
                    encoded = len(stored.encode("utf-8"))
                    if stored_bytes + encoded > MAX_MATCH_STORE_BYTES:
                        store_truncated = True
                    else:
                        matches.append([rel, lineno, stored])
                        stored_bytes += encoded
                else:
                    truncated = True
        if file_matched:
            files_with_matches.add(rel)

    matches.sort(key=lambda item: (item[0], item[1]))
    return {
        "matches": matches,
        "total_matches": match_count,
        "files": len(files_with_matches),
        "files_scanned": files_scanned,
        "binary_skipped": binary_skipped,
        "walk_truncated": walk_status.truncated,
        "truncated": (
            truncated
            or walk_status.truncated
            or input_truncated
            or scan_truncated
            or line_truncated
            or store_truncated
        ),
        "total_matches_exact": not (
            walk_status.truncated
            or files_scanned > MAX_SCAN_FILES
            or input_truncated
            or scan_truncated
            or stop
        ),
        "input_truncated": input_truncated,
        "scan_truncated": scan_truncated,
        "line_truncated": line_truncated,
        "store_truncated": store_truncated,
    }


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        json.dump({"error": f"invalid worker request: {exc}"}, sys.stdout)
        return 0
    if not isinstance(request, dict):
        json.dump({"error": "worker request must be an object"}, sys.stdout)
        return 0
    try:
        result = scan(request)
    except re.error as exc:
        json.dump({"error": f"invalid pattern: {exc}"}, sys.stdout)
        return 0
    except Exception as exc:  # noqa: BLE001 - report any failure as data
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        return 0
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
