"""Per-job shell logs, their disk bounds, and the persistent session cwd.

Layout (machine state under the project directory, never the workspace)::

    ~/.nexus/projects/<project-key>/shell/<session>/<job_id>.log
    ~/.nexus/projects/<project-key>/shell/<session>/cwd

Directories are ``0700`` and files ``0600``: a log may hold anything the program
printed. The log is one **combined** stream in arrival order (stdout and stderr
interleaved as a terminal would show them); memory keeps only the first
:attr:`JobLog.HEAD_CAP` bytes and the last :attr:`JobLog.TAIL_CAP` bytes of it.
Bytes that cannot be saved (the per-job limit, a write error) are counted and
reported, never silently dropped. Environment values are never written here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path

from ...config.paths import project_state_dir

__all__ = [
    "JOB_ID_RE",
    "JobLog",
    "log_path",
    "prune_logs",
    "read_session_cwd",
    "session_dir",
    "write_session_cwd",
]

JOB_ID_RE = re.compile(r"job_[0-9a-f]{12}\Z")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
#: Seconds between retention sweeps for one project directory.
_PRUNE_INTERVAL_S = 60.0
_last_prune: dict[str, float] = {}
_CWD_FILE = "cwd"


def _component(value: str) -> str:
    """A filesystem-safe directory name for a session id."""
    if _SAFE_COMPONENT.fullmatch(value):
        return value
    return "x-" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:32]


def shell_root(workspace: Path | str, *, home=None) -> Path:
    return project_state_dir(workspace, home) / "shell"


def session_dir(workspace: Path | str, session_id: str, *, home=None) -> Path:
    return shell_root(workspace, home=home) / _component(session_id)


def log_path(workspace: Path | str, session_id: str, job_id: str, *, home=None) -> Path:
    if JOB_ID_RE.fullmatch(job_id) is None:
        raise ValueError("invalid job id")
    return session_dir(workspace, session_id, home=home) / f"{job_id}.log"


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


class JobLog:
    """The combined output of one job: a log file plus head and tail buffers."""

    HEAD_CAP = 64 * 1024
    TAIL_CAP = 256 * 1024

    def __init__(self, path: Path | None, max_bytes: int = 0) -> None:
        #: ``None`` keeps only the in-memory head and tail (no file).
        self.path = path
        #: ``0`` means unlimited.
        self.max_bytes = max(int(max_bytes), 0)
        self.total = 0
        self.stderr_bytes = 0
        self.newlines = 0
        #: Bytes written to the file (a prefix of the stream).
        self.saved = 0
        #: Why saving stopped early (a write error), else ``None``.
        self.error: str | None = None
        self._head = bytearray()
        self._tail = bytearray()
        self._fd: int | None = None
        self._open_failed = False
        self._closed = False

    # -- writing ----------------------------------------------------------

    def open(self) -> None:
        """Create the file now; a failure disables saving but never the job."""
        if (
            self.path is None
            or self._fd is not None
            or self._open_failed
            or self._closed
        ):
            return
        try:
            _private_dir(self.path.parent)
            self._fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
        except OSError as exc:
            self._open_failed = True
            self.error = exc.strerror or str(exc)

    def append(self, chunk: bytes, *, stderr: bool = False) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        if stderr:
            self.stderr_bytes += len(chunk)
        self.newlines += chunk.count(b"\n")
        room = self.HEAD_CAP - len(self._head)
        if room > 0:
            self._head.extend(chunk[:room])
        self._tail.extend(chunk)
        if len(self._tail) > self.TAIL_CAP:
            del self._tail[: len(self._tail) - self.TAIL_CAP]
        self._save(chunk)

    def _save(self, chunk: bytes) -> None:
        if self.path is None or self.error is not None:
            return
        if self._fd is None:
            self.open()
            if self._fd is None:
                return
        if self.max_bytes:
            chunk = chunk[: max(self.max_bytes - self.saved, 0)]
            if not chunk:
                return
        try:
            view = memoryview(chunk)
            while view:
                written = os.write(self._fd, view)
                self.saved += written
                view = view[written:]
        except OSError as exc:
            # ENOSPC, EIO, ...: stop logging, keep the job running.
            self.error = exc.strerror or str(exc)

    def close(self) -> None:
        self._closed = True
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def discard_if_empty(self) -> None:
        """Close the log and remove its file when the job produced no output.

        For a job that never started: the file :meth:`open` created would
        otherwise linger as an empty orphan.
        """
        self.close()
        if self.path is not None and self.total == 0 and self.saved == 0:
            with contextlib.suppress(OSError):
                self.path.unlink()

    # -- reading ----------------------------------------------------------

    @property
    def unsaved(self) -> int:
        """Bytes that never reached the file (limit or write error)."""
        return self.total - self.saved if self.path is not None else 0

    @property
    def lines(self) -> int:
        """Line count of the whole stream (a final unterminated line counts)."""
        if self.total == 0:
            return 0
        last = self._tail[-1:] if self._tail else b""
        return self.newlines + (0 if last == b"\n" else 1)

    @property
    def tail_start(self) -> int:
        return self.total - len(self._tail)

    def view(self, offset: int = 0) -> tuple[bytes, int, bytes]:
        """``(head, omitted, tail)`` for the stream from ``offset``.

        ``head`` is what the head buffer still holds from ``offset``, ``omitted``
        the number of bytes between it and the tail that memory no longer has
        (they are in the file unless it was truncated), and ``tail`` the rest.
        With ``omitted == 0`` the pieces are contiguous: ``head + tail``.
        """
        offset = min(max(int(offset), 0), self.total)
        tail_start = self.tail_start
        if offset >= tail_start:
            return b"", 0, bytes(self._tail[offset - tail_start :])
        head_end = min(len(self._head), tail_start)
        head = bytes(self._head[offset:head_end]) if offset < head_end else b""
        omitted = tail_start - max(offset, head_end)
        return head, omitted, bytes(self._tail)

    def tail_bytes(self, count: int) -> bytes:
        return bytes(self._tail[-count:]) if count > 0 else b""

    def notices(self) -> list[str]:
        """Bounded, factual notices about bytes that were not saved."""
        out: list[str] = []
        if self.path is None:
            return out
        if self.error is not None:
            out.append(
                f"[log write failed ({self.error}); {self.unsaved} bytes not saved]"
            )
        elif self.unsaved:
            out.append(
                f"[log truncated at {self.saved} bytes; {self.unsaved} bytes not saved]"
            )
        return out


# -- retention ---------------------------------------------------------------


def _iter_logs(root: Path) -> list[tuple[Path, float, int]]:
    found: list[tuple[Path, float, int]] = []
    try:
        sessions = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return found
    for directory in sessions:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix != ".log":
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            found.append((entry, stat.st_mtime, stat.st_size))
    return found


def prune_logs(
    workspace: Path | str,
    *,
    max_total_bytes: int,
    retention_days: int,
    home=None,
    protected: Iterable[Path] = (),
    now: float | None = None,
    force: bool = False,
) -> int:
    """Delete finished logs past retention, then oldest-first past the total bound.

    ``protected`` paths (running jobs) are never deleted. Throttled to one sweep
    a minute per project unless ``force``. Returns the number of files removed.
    """
    root = shell_root(workspace, home=home)
    key = str(root)
    clock = time.time() if now is None else now
    if not force and clock - _last_prune.get(key, 0.0) < _PRUNE_INTERVAL_S:
        return 0
    _last_prune[key] = clock
    keep = {str(path) for path in protected}
    logs = [item for item in _iter_logs(root) if str(item[0]) not in keep]
    removed = 0

    def drop(path: Path) -> bool:
        try:
            path.unlink()
        except OSError:
            return False
        return True

    survivors: list[tuple[Path, float, int]] = []
    cutoff = clock - retention_days * 86400 if retention_days > 0 else None
    for item in logs:
        if cutoff is not None and item[1] < cutoff and drop(item[0]):
            removed += 1
        else:
            survivors.append(item)
    if max_total_bytes > 0:
        protected_bytes = 0
        for path in keep:
            with contextlib.suppress(OSError):
                protected_bytes += os.stat(path).st_size
        total = protected_bytes + sum(size for _, _, size in survivors)
        for path, _mtime, size in sorted(survivors, key=lambda item: item[1]):
            if total <= max_total_bytes:
                break
            if drop(path):
                removed += 1
                total -= size
    return removed


# -- session cwd -----------------------------------------------------------------


def _cwd_file(workspace: Path | str, session_id: str, *, home=None) -> Path:
    return session_dir(workspace, session_id, home=home) / _CWD_FILE


def read_session_cwd(workspace: Path | str, session_id: str, *, home=None) -> str | None:
    """The session's recorded cwd (an absolute path string), or ``None``."""
    try:
        data = json.loads(_cwd_file(workspace, session_id, home=home).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("cwd") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def write_session_cwd(workspace: Path | str, session_id: str, cwd: str, *, home=None) -> None:
    """Record the session cwd atomically (private file, rename into place)."""
    path = _cwd_file(workspace, session_id, home=home)
    _private_dir(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = json.dumps({"cwd": cwd}, ensure_ascii=False)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
