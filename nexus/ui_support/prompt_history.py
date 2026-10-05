"""Bounded per-user prompt history for the terminal composer (plan §5.2)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

MAX_ENTRIES = 500
MAX_PROMPT = 4096
MAX_FILE_BYTES = MAX_ENTRIES * (MAX_PROMPT + 12)


def history_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state else Path.home() / ".local" / "state"
    return root / "nexus" / "prompt_history"


def load_history(path: Path | None = None) -> list[str]:
    path = path or history_path()
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        rows = path.read_text(encoding="utf-8").splitlines()[-MAX_ENTRIES:]
    except (OSError, UnicodeError):
        return []
    prompts: list[str] = []
    for row in rows:
        try:
            value = json.loads(row)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str) and 0 < len(value) <= MAX_PROMPT:
            prompts.append(value)
    return prompts


def append_history(prompt: str, path: Path | None = None) -> None:
    if not prompt or len(prompt) > MAX_PROMPT:
        return
    path = path or history_path()
    rows = [*load_history(path), prompt][-MAX_ENTRIES:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".prompt-history-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    except OSError:
        return
