"""Killable regex scan used by `grep` when `rg` is not installed.

Adapted from old Nexus nexus/tools/builtin/_grep_scan.py. A model-supplied
regular expression runs here, in a short-lived subprocess the parent kills on a
deadline, so catastrophic backtracking can neither block the daemon nor leak a
thread. One JSON request on stdin (the file list comes from the parent's walk,
so `.gitignore` is already applied), one JSON response on stdout.
"""
from __future__ import annotations

import json
import re
import sys

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_CHARS = 2000


def scan(request: dict) -> dict:
    flags = re.IGNORECASE if request.get("ignore_case") else 0
    pattern = request["pattern"] if request.get("regex", True) else re.escape(request["pattern"])
    compiled = re.compile(pattern, flags)
    limit = int(request["limit"])
    matches: list[list] = []
    stopped = False
    for rel in request["files"]:
        try:
            with open(rel, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES)
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if compiled.search(line[:10_000]):
                if len(matches) >= limit:
                    stopped = True
                    break
                matches.append([rel, number, line[:MAX_LINE_CHARS + 1]])  # one over, so the parent can announce it
        if stopped:
            break
    return {"matches": matches, "stopped": stopped}


def main() -> int:
    try:
        json.dump(scan(json.load(sys.stdin)), sys.stdout)
    except re.error as exc:
        json.dump({"error": f"invalid pattern: {exc}"}, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - runs as a subprocess
    raise SystemExit(main())
