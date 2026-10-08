"""Shell output limit shared by the ``bash`` tool and the composer's ``!`` mode.

Contract: text shown to the model is at most ``MAX_LINES`` lines and
``MAX_BYTES`` UTF-8 bytes. Larger output is written in full to a private
temporary file (directory ``0700``, file ``0600``) and the shown text keeps the
head and the tail (test runners and builds print their summary last) plus a
notice naming that file, so the agent can ``read`` or ``grep`` the rest. The
spill directory keeps at most ``MAX_SPILL_FILES`` files; the oldest go first.
Spilling is best effort: when the file cannot be written the notice says so.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MAX_BYTES",
    "MAX_LINES",
    "MAX_SPILL_FILES",
    "LimitedOutput",
    "limit_output",
    "saved_path",
    "spill_dir",
]

#: Lines of output shown before the rest moves to a file.
MAX_LINES = 2000
#: UTF-8 bytes of output shown before the rest moves to a file (50 KiB).
MAX_BYTES = 50 * 1024
#: Spill files kept on disk; older ones are removed when a new one is written.
MAX_SPILL_FILES = 200
#: Share of the shown budget given to the head; the tail gets the rest.
_HEAD_SHARE = 2 / 5
_SAVED = "Full output saved to"
_SAVED_END = ";"
#: Room reserved for the omission marker inside the byte budget.
_MARKER_RESERVE = 256


@dataclass(frozen=True)
class LimitedOutput:
    text: str
    truncated: bool = False
    total_lines: int = 0
    total_bytes: int = 0
    shown_lines: int = 0
    path: str | None = None


def spill_dir() -> Path:
    """The per-user directory holding full copies of oversized output."""
    override = os.environ.get("NEXUS_OUTPUT_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"nexus-output-{os.getuid()}"


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _prune(directory: Path, keep: int) -> None:
    try:
        files = sorted(
            (entry for entry in directory.iterdir() if entry.is_file()),
            key=lambda entry: entry.stat().st_mtime,
        )
    except OSError:
        return
    for stale in files[: max(len(files) - keep, 0)]:
        try:
            stale.unlink()
        except OSError:
            pass


def _spill(text: str, label: str) -> str:
    directory = spill_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _prune(directory, MAX_SPILL_FILES - 1)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40] or "output"
    fd, name = tempfile.mkstemp(prefix=f"{safe}-", suffix=".txt", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
    return name


def _clip_bytes(text: str, budget: int, *, from_end: bool) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    piece = raw[len(raw) - budget :] if from_end else raw[:budget]
    return piece.decode("utf-8", "ignore")


def limit_output(text: str, *, label: str = "output") -> LimitedOutput:
    """Bound ``text`` to the shell output limit, spilling the full text if needed."""
    lines = text.splitlines(keepends=True)
    total_bytes = len(text.encode("utf-8"))
    if len(lines) <= MAX_LINES and total_bytes <= MAX_BYTES:
        return LimitedOutput(text, False, len(lines), total_bytes, len(lines))

    head_lines = int(MAX_LINES * _HEAD_SHARE)
    tail_lines = MAX_LINES - head_lines
    if len(lines) > MAX_LINES:
        head, tail = lines[:head_lines], lines[len(lines) - tail_lines :]
    else:
        split = max(1, int(len(lines) * _HEAD_SHARE))
        head, tail = lines[:split], lines[split:]
    budget = MAX_BYTES - _MARKER_RESERVE
    head_text = _clip_bytes("".join(head), int(budget * _HEAD_SHARE), from_end=False)
    tail_text = _clip_bytes("".join(tail), budget - len(head_text.encode("utf-8")), from_end=True)
    shown_lines = len(head_text.splitlines()) + len(tail_text.splitlines())

    try:
        path: str | None = _spill(text, label)
        where = f"{_SAVED} {path}{_SAVED_END} read it with offsets or grep it there."
    except OSError as exc:
        path = None
        where = f"The full output could not be saved ({exc.strerror or exc})."
    marker = (
        f"\n[output too large: {len(lines)} lines, {_human(total_bytes)}; "
        f"limit {MAX_LINES} lines or {_human(MAX_BYTES)}. Showing the first and "
        f"last parts ({shown_lines} lines). {where}]\n"
    )
    if head_text and not head_text.endswith("\n"):
        head_text += "\n"
    return LimitedOutput(
        head_text + marker.lstrip("\n") + tail_text,
        True,
        len(lines),
        total_bytes,
        shown_lines,
        path,
    )


def saved_path(text: str) -> str | None:
    """The spill-file path named by a ``limit_output`` notice, if any."""
    start = text.find(_SAVED + " ")
    if start < 0:
        return None
    start += len(_SAVED) + 1
    end = text.find(_SAVED_END + " read it", start)
    return text[start:end] if end > start else None
